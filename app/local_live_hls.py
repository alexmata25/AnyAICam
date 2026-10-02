"""Local HLS reads bound to the activated appliance and exact camera slot."""
import re
import time
from pathlib import Path
from urllib.parse import quote
from fastapi import HTTPException


def require_local_camera(camera, identity, runtime_role, db):
    if runtime_role not in {'edge', 'combined'} or not identity:
        raise HTTPException(status_code=503, detail='Local live view is unavailable.')
    if not identity.get('credential') or camera['appliance_id'] != identity.get('appliance_id'):
        raise HTTPException(status_code=404, detail='Local camera not found.')
    appliance = db.execute('SELECT cloud_id,customer_id,site_id FROM appliances WHERE id=?',
                           (identity['appliance_id'],)).fetchone()
    if not appliance or appliance['cloud_id'] != identity.get('cloud_id') or appliance['customer_id'] != camera['customer_id'] or appliance['site_id'] != camera['site_id']:
        raise HTTPException(status_code=404, detail='Local camera not found.')


def segment_path(folder, camera_number, name):
    # The separator is mandatory: camera1 must never match camera10.
    if not isinstance(name,str) or not re.fullmatch(rf'camera{int(camera_number)}_[0-9]+[.]ts',name):
        raise HTTPException(status_code=404, detail='Segment not found.')
    folder=Path(folder).resolve()
    path=(folder/name).resolve()
    if path.parent != folder or not path.is_file():
        raise HTTPException(status_code=404, detail='Segment not found.')
    return path


def local_playlist(folder,camera_number,camera_id,stale_seconds=20):
    folder=Path(folder).resolve()
    path=(folder/f'camera{int(camera_number)}.m3u8').resolve()
    empty='#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n'
    if path.parent != folder:
        raise HTTPException(status_code=404, detail='Playlist not found.')
    try:
        if time.time()-path.stat().st_mtime > stale_seconds: return empty
        text=path.read_text(encoding='utf-8')
    except (OSError,UnicodeError): return empty
    # Only this pipeline's plain MPEG-TS playlist tags are accepted. Never
    # forward URI-bearing key/map tags or external URLs from a manifest.
    output=['#EXTM3U']; pending=None
    for line in text.splitlines():
        line=line.strip()
        if re.fullmatch(r'#EXT-X-(?:VERSION|TARGETDURATION|MEDIA-SEQUENCE):[0-9]+',line):
            output.append(line)
        elif re.fullmatch(r'#EXTINF:[0-9]+(?:[.][0-9]+)?,',line): pending=line
        elif line and not line.startswith('#'):
            try: segment_path(folder,camera_number,line)
            except HTTPException: pending=None; continue
            if pending:
                output.extend([pending,f'/api/customer/cameras/{quote(camera_id,safe="")}/live/segments/{quote(line,safe="")}'])
            pending=None
    return '\n'.join(output)+'\n'
