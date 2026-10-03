"""Pin/download simulator runtime debs. Writes only ignored dist, never installs."""
import concurrent.futures, hashlib, json, time, urllib.request
from pathlib import Path
root=Path(__file__).resolve().parents[2]
plan=root/'validation/installer-track/offline-simulator.lock.json'
lock=json.loads(plan.read_text())
bundle=root/'dist/installer-track/offline-simulator'; debs=bundle/'debs'; debs.mkdir(parents=True,exist_ok=True)
def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def download(p):
 dest=debs/Path(p['filename']).name
 if dest.exists() and digest(dest)==p['sha256']: return {**p,'downloaded':True,'bundle_path':'debs/'+dest.name}
 url='https://archive.ubuntu.com/ubuntu/'+p['filename']
 for attempt in range(3):
  try:
   with urllib.request.urlopen(url,timeout=90) as response, dest.with_suffix('.partial').open('wb') as output:
    while block:=response.read(1024*1024): output.write(block)
   partial=dest.with_suffix('.partial')
   if digest(partial)!=p['sha256']: raise ValueError('checksum mismatch: '+dest.name)
   partial.replace(dest)
   print('VERIFIED '+dest.name,flush=True)
   return {**p,'downloaded':True,'bundle_path':'debs/'+dest.name,'url':url}
  except Exception as err:
   if attempt==2: return {**p,'downloaded':False,'error':str(err),'url':url}
   time.sleep(attempt+1)
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
 results=list(pool.map(download,lock['packages']))
lock['packages']=results; lock['missing_archive_digests']=[p['package'] for p in results if not p['sha256']]
lock['status']='simulator runtime bundle; verified downloaded archives; no OS changes'
plan.write_text(json.dumps(lock,indent=2)+'\n',newline='\n')
(bundle/'offline-simulator.lock.json').write_text(json.dumps(lock,indent=2)+'\n',newline='\n')
failures=[{'package':p['package'],'version':p['version'],'error':p.get('error')} for p in results if not p['downloaded']]
print(json.dumps({'verified':len(results)-len(failures),'failures':failures}),flush=True)
if failures: raise SystemExit(1)
