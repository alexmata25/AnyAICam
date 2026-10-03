"""Verify offline simulator package closure and run extracted binaries. Never install."""
import hashlib,json,os,subprocess,tempfile
from pathlib import Path
root=Path(__file__).resolve().parents[2]
lock=json.loads((root/'validation/installer-track/offline-simulator.lock.json').read_text())
bundle=root/'dist/installer-track/offline-simulator'
files=[bundle/p['bundle_path'] for p in lock['packages']]
assert len(files)==251 and all(p['downloaded'] for p in lock['packages'])
for p,path in zip(lock['packages'],files):
 assert hashlib.sha256(path.read_bytes()).hexdigest()==p['sha256'],path
meta=bundle/'apt-metadata'
for file in meta.glob('*InRelease'):
 subprocess.run(['gpgv','--keyring',str(meta/'ubuntu-archive-keyring.gpg'),str(file)],check=True,stdout=subprocess.DEVNULL)
print('251_DEB_HASHES_AND_3_UBUNTU_SIGNATURES_PASS',flush=True)
with tempfile.TemporaryDirectory(prefix='anyaicam-offline-runtime-') as work:
 work=Path(work); status=work/'status'; status.touch(); sources=work/'sources.list'; sources.touch(); parts=work/'sourceparts'; parts.mkdir()
 planned=subprocess.check_output(['apt-get','-s','--no-download','--no-install-recommends','-o','Dir::State::status='+str(status),'-o','Debug::NoLocking=1','-o','Dir::Etc::sourcelist='+str(sources),'-o','Dir::Etc::sourceparts='+str(parts),'install',*map(str,files)],text=True)
 assert '0 to remove' in planned and '251 newly installed' in planned
 print('OFFLINE_EMPTY_STATUS_APT_SIMULATION_251_PASS',flush=True)
 extracted=work/'root'; extracted.mkdir()
 for file in files:
  subprocess.run(['dpkg-deb','-x',str(file),str(extracted)],check=True)
 loader=next(extracted.rglob('ld-linux-x86-64.so.2'))
 paths=':'.join(str(extracted/p) for p in ['usr/lib/x86_64-linux-gnu','lib/x86_64-linux-gnu','usr/lib','lib'])
 def binary(name,*args,env=None):
  return subprocess.check_output([str(loader),'--library-path',paths,str(extracted/'usr/bin'/name),*args],text=True,env=env)
 sample=work/'sample.mkv'
 binary('ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc2=size=1280x720:rate=15','-t','1','-c:v','libx264','-preset','veryfast','-tune','zerolatency','-pix_fmt','yuv420p','-profile:v','baseline','-g','30',str(sample))
 stream=json.loads(binary('ffprobe','-v','error','-select_streams','v:0','-show_entries','stream=codec_name,profile,width,height,r_frame_rate','-of','json',str(sample)))['streams'][0]
 assert stream=={'codec_name':'h264','profile':'Constrained Baseline','width':1280,'height':720,'r_frame_rate':'15/1'},stream
 print('EXTRACTED_OFFLINE_FFMPEG_H264_BASELINE_PASS',flush=True)
 env={**os.environ,'PYTHONHOME':str(extracted/'usr'),'PYTHONDONTWRITEBYTECODE':'1'}
 print(binary('python3.12','-B','-m','unittest','discover','-s',str(root/'validation/installer-track'),'-p','test_simulator.py','-q',env=env),flush=True)
 print('EXTRACTED_OFFLINE_PYTHON_ONVIF_4_PASS',flush=True)
print('No dpkg/apt installations, services, listeners or global package state changes.',flush=True)
