"""Opt-in Linux RTSP E2E: real MediaMTX, FFmpeg and agent socket authentication.
Each test gets loopback-only processes; no existing appliance/portal is changed.
"""
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import time
from urllib.parse import quote
import pytest
from anyaicam_agent.provisioning import verify_rtsp_credentials

pytestmark=pytest.mark.skipif(os.environ.get('ANYAICAM_RUN_RTSP_E2E')!='1',reason='requires verified MediaMTX and FFmpeg in an isolated Linux lab')

@pytest.fixture(params=[False,True],ids=['anonymous','authenticated'])
def stream(request,tmp_path):
    executable=os.environ['ANYAICAM_TEST_MEDIAMTX']
    assert Path(executable).is_file() and shutil.which('ffmpeg')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    credentials={'username':'reader','password':secrets.token_urlsafe(20)} if request.param else None
    users=[{'user':'any','pass':'','ips':['127.0.0.1'],'permissions':[{'action':'publish','path':'testcamera'}]}, {'user':credentials['username'] if credentials else 'any','pass':credentials['password'] if credentials else '', 'ips':['127.0.0.1'],'permissions':[{'action':'read','path':'testcamera'}]}]
    config={'rtspAddress':f'127.0.0.1:{port}','rtspTransports':['tcp'],'rtmp':False,'hls':False,'webrtc':False,'srt':False,'moq':False,'authInternalUsers':users,'paths':{'testcamera':{}}}
    path=tmp_path/'mediamtx.json';path.write_text(json.dumps(config));path.chmod(0o600)
    log=open(tmp_path/'media.log','wb')
    server=subprocess.Popen([executable,str(path)],stdout=log,stderr=log)
    publisher=None
    try:
        for _ in range(100):
            with socket.socket() as sock:
                if sock.connect_ex(('127.0.0.1',port))==0:break
            assert server.poll() is None, 'isolated MediaMTX failed to start'
            time.sleep(.05)
        uri=f'rtsp://127.0.0.1:{port}/testcamera'
        publisher=subprocess.Popen(['ffmpeg','-hide_banner','-loglevel','error','-re','-f','lavfi','-i','testsrc2=size=320x240:rate=10','-an','-c:v','libx264','-threads','1','-preset','ultrafast','-g','10','-tune','zerolatency','-pix_fmt','yuv420p','-f','rtsp','-rtsp_transport','tcp',uri],stdout=subprocess.DEVNULL,stderr=log)
        time.sleep(2)
        assert publisher.poll() is None, 'isolated publisher failed'
        yield port,uri,credentials
    finally:
        for process in [publisher,server]:
            if process:
                process.terminate()
                try:process.wait(timeout=10)
                except subprocess.TimeoutExpired:process.kill();process.wait()
        log.close()

def test_real_describe_respects_camera_authentication(stream):
    port,uri,creds=stream
    assert verify_rtsp_credentials('127.0.0.1',port,'','',path='/testcamera')[0] is (creds is None)
    if creds:
        assert not verify_rtsp_credentials('127.0.0.1',port,creds['username'],'wrong-password',path='/testcamera')[0]
        assert verify_rtsp_credentials('127.0.0.1',port,creds['username'],creds['password'],path='/testcamera')[0]

def test_verified_stream_decodes_real_video(stream):
    port,uri,creds=stream
    if creds:uri=uri.replace('rtsp://','rtsp://'+quote(creds['username'],safe='')+':'+quote(creds['password'],safe='')+'@')
    try:
        result=subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-threads','1','-rtsp_transport','tcp','-i',uri,'-frames:v','10','-threads','1','-f','rawvideo','-pix_fmt','gray','pipe:1'],capture_output=True,timeout=20)
    except subprocess.TimeoutExpired:
        raise AssertionError('isolated video decoding timed out; endpoint credentials withheld') from None
    assert result.returncode==0, 'real RTSP video decoding failed'
    frames=[result.stdout[i:i+320*240] for i in range(0,len(result.stdout),320*240)]
    assert len(frames)==10 and len(set(frames))>1
