"""Read installed FFmpeg dependency closure and cached apt package digests; no changes."""
import hashlib, json, os, subprocess
from pathlib import Path

def run(args):
    return subprocess.run(args, text=True, capture_output=True).stdout
paths=[]
for line in run(['ldd','/usr/bin/ffmpeg']).splitlines():
    parts=line.strip().split()
    if '=>' in parts and len(parts)>2 and parts[2].startswith('/'):
        paths.append(parts[2])
    elif parts and parts[0].startswith('/'):
        paths.append(parts[0])
packages={'ffmpeg'}
libraries=[]
for path in paths:
    real=os.path.realpath(path)
    providers=run(['dpkg-query','-S',real,path]).splitlines()
    names=set(line.split(': ',1)[0].split(':',1)[0] for line in providers if ': ' in line)
    packages.update(names)
    libraries.append({'path':path,'sha256':hashlib.sha256(Path(real).read_bytes()).hexdigest(),'packages':sorted(names)})
installed={}
for line in run(['dpkg-query','-W','-f=${Package}\t${Version}\n',*sorted(packages)]).splitlines():
    name,version=line.split('\t',1); installed[name]=version
metadata={}
for block in run(['apt-cache','show',*sorted(packages)]).split('\n\n'):
    record={}
    for line in block.splitlines():
        if ': ' in line and not line.startswith(' '):
            key,value=line.split(': ',1); record[key]=value
    key=(record.get('Package'),record.get('Version'))
    if record.get('SHA256'): metadata[key]=record
result=[]
for name,version in sorted(installed.items()):
    rec=metadata.get((name,version),{})
    result.append({'package':name,'version':version,'sha256':rec.get('SHA256'),'filename':rec.get('Filename'),'downloaded':name=='ffmpeg'})
print(json.dumps({'status':'read-only installed WSL runtime closure; cache digests, not a signed offline repository','platform':'Ubuntu 24.04 amd64','packages':result,'missing_archive_digests':[r['package'] for r in result if not r['sha256']], 'installed_libraries':libraries},indent=2))
