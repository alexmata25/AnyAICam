"""Freeze verified simulator bundle, without installation or publication."""
import hashlib,json,shutil,subprocess,sys
from pathlib import Path
root=Path(__file__).resolve().parents[2]; plans=root/'validation/installer-track'
lock=json.loads((plans/'offline-simulator.lock.json').read_text())
assert lock['metadata_authenticated'] and not lock['missing_archive_digests']
assert len(lock['packages'])==251 and all(p['downloaded'] for p in lock['packages'])
source=root/'dist/installer-track/offline-simulator'
release=root/'dist/installer-track/offline-release'; release.mkdir(exist_ok=True)
stage=release/'camera-simulator'; stage.mkdir(exist_ok=False)
(stage/'debs').mkdir()
for p in lock['packages']:
 file=source/p['bundle_path']
 assert hashlib.sha256(file.read_bytes()).hexdigest()==p['sha256']
 shutil.copy2(file,stage/p['bundle_path'])
shutil.copytree(source/'apt-metadata',stage/'apt-metadata')
for name in ['onvif_simulator.py','mediamtx.yml','publish-test-video.sh','camera.env.example','offline-simulator.lock.json']:
 shutil.copy2(plans/name,stage/name)
shutil.copy2(plans/'OFFLINE_SIMULATOR.md',stage/'README.md')
binary=root/'dist/installer-track/dependencies/mediamtx'
assert hashlib.sha256(binary.read_bytes()).hexdigest()=='9fac297a1b57762980fc1f7d91499211091366b3abecd0a7a0d8146387d901ad'
shutil.copy2(binary,stage/'mediamtx')
manifest=[]
for file in sorted(stage.rglob('*')):
 if file.is_file(): manifest.append({'path':file.relative_to(stage).as_posix(),'sha256':hashlib.sha256(file.read_bytes()).hexdigest(),'size':file.stat().st_size})
(stage/'files.json').write_text(json.dumps(manifest,indent=2)+'\n',newline='\n')
checks=[*manifest,{'path':'files.json','sha256':hashlib.sha256((stage/'files.json').read_bytes()).hexdigest()}]
(stage/'SHA256SUMS').write_text(''.join(p['sha256']+'  '+p['path']+'\n' for p in checks),newline='\n')
sys.path.insert(0,str(root/'installer'))
from build_release_installer import write_deterministic_tar
filename='anyaicam-camera-simulator-ubuntu24.04-ffmpeg6.1.1-mediamtx1.21.0-a1c81e0f4926.tar.gz'
archive=release/filename
assert not archive.exists()
epoch=int(subprocess.check_output(['git','show','-s','--format=%ct','a1c81e0f492633a80198503e721fc6c277e6a8b3'],cwd=root,text=True))
write_deterministic_tar(stage,archive,epoch,frozenset(['mediamtx','publish-test-video.sh']))
info={'filename':filename,'sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'size_bytes':archive.stat().st_size,'debian_packages':251,'manifest_files':len(manifest),'mediamtx':'v1.21.0','ffmpeg':'7:6.1.1-3ubuntu5','source_fix_commit':'a1c81e0f492633a80198503e721fc6c277e6a8b3','published':False,'scope':lock['scope'],'runtime_validation':'offline apt simulation, extracted FFmpeg and Python/ONVIF passed; actual guest installation pending'}
(plans/'offline-bundle.json').write_text(json.dumps(info,indent=2)+'\n',newline='\n')
(release/'SHA256SUMS').write_text(info['sha256']+'  '+filename+'\n',newline='\n')
print(json.dumps(info),flush=True)
