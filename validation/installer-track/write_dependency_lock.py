import hashlib, json, subprocess
from pathlib import Path
root=Path(__file__).resolve().parents[2]
out=root/'validation/installer-track'
base='https://github.com/bluenviron/mediamtx/releases/download/v1.21.0/'
data={'status':'prepared, unpublished; simulator not started; not a complete offline installer',
 'base_commit':'d5af9c53cbef1f2c400f56e148c5cb080770dc07', 'source_commit':'a1c81e0f492633a80198503e721fc6c277e6a8b3',
 'mediamtx':{'version':'v1.21.0','platform':'linux_amd64','url':base+'mediamtx_v1.21.0_linux_amd64.tar.gz','archive_sha256':'e02e34c3337a35f20ac9e5aa31524566108964e6e37dbc46cf8292169f6c792b','binary_sha256':'9fac297a1b57762980fc1f7d91499211091366b3abecd0a7a0d8146387d901ad','verification':'release checksum and GitHub asset digest matched'},
 'ffmpeg':{'version':'7:6.1.1-3ubuntu5','url':'https://archive.ubuntu.com/ubuntu/pool/universe/f/ffmpeg/ffmpeg_6.1.1-3ubuntu5_amd64.deb','sha256':'1a23baace5f2688a47e28119aedae6993cd638e6279a7227dfc52d9a337a1c17','runtime_lock':'offline-simulator.lock.json','runtime_note':'251-package FFmpeg/Python dependency closure resolved with temporary empty dpkg status and freshly signature-verified Ubuntu metadata; no system packages installed or updated. See offline-simulator.lock.json for exact versions, hashes and downloaded status.'},
 'onvif':{'implementation':'onvif_simulator.py','runtime':'Ubuntu 24.04 Python 3.12 standard library; no pip dependencies','network':'opt-in helper VM only; explicit private /24, read-only ONVIF and WS-Discovery'},
 'ubuntu_iso':{'filename':'ubuntu-24.04.4-desktop-amd64.iso','url':'https://releases.ubuntu.com/24.04.4/ubuntu-24.04.4-desktop-amd64.iso','sha256':'3a4c9877b483ab46d7c3fbe165a0db275e1ae3cfe56a5657e5a47c2f99a99d1e','verification':'Canonical SHA256SUMS retrieved; ISO download and signed metadata verification pending'},
 'prepared_file_sha256':{}, 'pinned_schema_source_sha256':{}}
for name in ['onvif_simulator.py','mediamtx.yml','camera.env.example','publish-test-video.sh','portal.env.example','owner-location.plan.json']:
 data['prepared_file_sha256'][name]=hashlib.sha256((out/name).read_bytes()).hexdigest()
for name in ['app/partner_db.py','app/db_migrations.py','app/appliance_claims.py','app/cloud_config.py']:
 raw=subprocess.check_output(['git','show',data['source_commit']+':'+name],cwd=root)
 data['pinned_schema_source_sha256'][name]=hashlib.sha256(raw).hexdigest()
(out/'dependencies.lock.json').write_text(json.dumps(data,indent=2)+'\n',encoding='utf-8',newline='\n')
print('Dependency/config/source lock written; no database initialization.')
