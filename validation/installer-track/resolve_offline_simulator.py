"""Resolve/download metadata in caller-owned apt directories; never install."""
import argparse,hashlib,json,re,subprocess,tempfile
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument('--refresh-isolated',action='store_true'); args=p.parse_args()
root=Path(__file__).resolve().parents[2]
with tempfile.TemporaryDirectory(prefix='anyaicam-apt-plan-') as work:
 base=Path(work); status=base/'status'; status.touch()
 options=['-o','Dir::State::status='+str(status),'-o','Debug::NoLocking=1']
 provenance=[]
 if args.refresh_isolated:
  lists=base/'lists'; (lists/'partial').mkdir(parents=True)
  cache=base/'cache'; cache.mkdir()
  parts=base/'sourceparts'; parts.mkdir()
  sources=base/'sources.list'
  sources.write_text('deb https://archive.ubuntu.com/ubuntu noble main universe\ndeb https://archive.ubuntu.com/ubuntu noble-updates main universe\ndeb https://security.ubuntu.com/ubuntu noble-security main universe\n')
  options += ['-o','Dir::State::lists='+str(lists),'-o','Dir::Cache='+str(cache),'-o','Dir::Cache::pkgcache='+str(cache/'pkgcache.bin'),'-o','Dir::Cache::srcpkgcache='+str(cache/'srcpkgcache.bin'),'-o','Dir::Etc::sourcelist='+str(sources),'-o','Dir::Etc::sourceparts='+str(parts),'-o','Acquire::Languages=none','-o','Acquire::IndexTargets::deb::DEP-11::DefaultEnabled=false','-o','APT::Get::List-Cleanup=0']
  update=subprocess.run(['apt-get',*options,'update'],text=True,capture_output=True)
  if update.returncode or 'Failed to fetch' in update.stdout+update.stderr:
   raise RuntimeError(update.stdout+update.stderr)
  meta=root/'dist/installer-track/offline-simulator/apt-metadata'; meta.mkdir(parents=True,exist_ok=True)
  for source in lists.iterdir():
   if source.is_file() and ('InRelease' in source.name or 'Packages' in source.name):
    data=source.read_bytes(); (meta/source.name).write_bytes(data)
    provenance.append({'filename':'apt-metadata/'+source.name,'sha256':hashlib.sha256(data).hexdigest()})
  (meta/'ubuntu-archive-keyring.gpg').write_bytes(Path('/usr/share/keyrings/ubuntu-archive-keyring.gpg').read_bytes())
  print('Isolated apt refresh authenticated successfully; system apt lists unchanged.',file=__import__('sys').stderr)
 output=subprocess.check_output(['apt-get',*options,'-s','--no-install-recommends','install','ffmpeg=7:6.1.1-3ubuntu5','python3'],text=True)
 versions=dict(re.findall(r'^Inst (\S+) \((\S+) ',output,re.M))
 metadata={}
 raw=subprocess.check_output(['apt-cache',*options,'show',*versions],text=True)
 for block in raw.split('\n\n'):
  record={}
  for line in block.splitlines():
   if ': ' in line and not line.startswith(' '):
    key,value=line.split(': ',1); record[key]=value
  if record.get('SHA256'): metadata[(record.get('Package'),record.get('Version'))]=record
 packages=[]
 for name,version in sorted(versions.items()):
  rec=metadata[(name,version)]
  packages.append({'package':name,'version':version,'architecture':rec['Architecture'],'filename':rec['Filename'],'sha256':rec['SHA256'],'downloaded':False})
 print(json.dumps({'status':'resolved with empty temporary dpkg state; no installations','scope':'MediaMTX + FFmpeg + stdlib Python ONVIF simulator only; not the VMS/Docker installer dependency closure','platform':'Ubuntu 24.04 amd64','roots':['ffmpeg=7:6.1.1-3ubuntu5','python3'],'metadata_authenticated':args.refresh_isolated,'authenticated_metadata':provenance,'packages':packages,'missing_archive_digests':[]},indent=2))
