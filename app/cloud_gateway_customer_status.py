"""Conservative customer-facing camera status; no provider calls or grants."""
from collections import Counter


def camera_statuses(customer_id, appliance, *, edition_verified, rows):
    """Show owned camera mappings; a generic camera plan is not Cloud payment proof."""
    if (edition_verified is not True or not isinstance(appliance, dict)
            or appliance.get('customer_id') != customer_id or appliance.get('gateway_edition') != 'cloud_gateway'):
        return []
    appliance_id = appliance.get('id')
    if not isinstance(appliance_id, str) or not appliance_id:
        return []
    candidates = rows("SELECT id,name,camera_number,device_key,customer_id,appliance_id,status FROM cameras "
                      "WHERE customer_id=? AND appliance_id=? AND COALESCE(status,'')!='removed' ORDER BY camera_number,id",
                      (customer_id,appliance_id))
    # The entitlement verifier rejects a device key duplicated anywhere in
    # the customer's account, including a different appliance. Match that
    # boundary before describing a mapping as identified.
    customer_cameras = rows("SELECT id,device_key,customer_id,status FROM cameras "
                            "WHERE customer_id=? AND COALESCE(status,'')!='removed'", (customer_id,))
    owned = [c for c in candidates if c.get('customer_id') == customer_id and c.get('appliance_id') == appliance_id
             and c.get('status') != 'removed' and isinstance(c.get('id'),str) and c['id']]
    ids = Counter(c['id'] for c in owned)
    numbers = Counter(c.get('camera_number') for c in owned if type(c.get('camera_number')) is int and c['camera_number'] > 0)
    keys = Counter(c.get('device_key') for c in customer_cameras
                   if c.get('customer_id') == customer_id and c.get('status') != 'removed'
                   and isinstance(c.get('device_key'),str) and c['device_key'])
    result = []
    seen = set()
    for camera in owned:
        camera_id = camera['id']
        if camera_id in seen:
            continue
        seen.add(camera_id)
        number, key = camera.get('camera_number'), camera.get('device_key')
        mapping = ('pending' if camera.get('status') not in ('configured','active') or type(number) is not int or not 1 <= number <= 10000 or not isinstance(key,str) or not key.strip() or key != key.strip() else
                   'ambiguous' if ids[camera_id] != 1 or numbers[number] != 1 or keys[key] != 1 else 'identified')
        result.append({'camera_id':camera_id, 'name':str(camera.get('name') or 'Camera'),
                       'mapping_status':mapping, 'entitlement_status':'unavailable', 'event_upload_status':'held'})
    return result
