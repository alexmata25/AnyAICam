from datetime import datetime

CUSTOMER_ROLES={'customer_owner','customer_viewer'}
CAMERA_ACTION_COLUMNS={
    'live':'can_live','playback':'can_playback','download':'can_download','share':'can_share',
    'alerts':'can_alerts','settings':'can_settings',
}


def role_destination(role: str) -> str:
    return {
        'administrator':'/partner?tab=customers',
        'partner_owner':'/partner?tab=customers',
        'salesperson':'/partner-quotes',
        'technician':'/partner/appliance-dashboard',
        'customer_owner':'/customer-account',
        'customer_viewer':'/customer-account',
    }.get(role,'/partner-login')


def camera_action_allowed(role: str,action: str,permission: dict | None,configured_permissions: int) -> bool:
    if role in {'administrator','customer_owner'}: return True
    if role!='customer_viewer' or action not in CAMERA_ACTION_COLUMNS: return False
    if configured_permissions==0: return action in {'live','playback','alerts'}
    return bool(permission and permission.get(CAMERA_ACTION_COLUMNS[action],0))


def live_session_state(state: str,expires_at: str,now: datetime | None=None) -> str:
    current=now or datetime.now()
    try:
        if datetime.fromisoformat(expires_at)<=current: return 'expired'
    except (TypeError,ValueError): return 'failed'
    return state if state in {'requested','ready','failed','expired'} else 'failed'


def same_customer(resource_customer_id: str,identity_customer_id: str) -> bool:
    return bool(resource_customer_id and identity_customer_id and resource_customer_id==identity_customer_id)


def notification_scope_allowed(user_customer_id: str,resource_customer_id: str,site_id=None,allowed_sites=None,camera_id=None,allowed_cameras=None) -> bool:
    if not same_customer(resource_customer_id,user_customer_id): return False
    if site_id and allowed_sites and site_id not in set(allowed_sites): return False
    if camera_id and allowed_cameras and camera_id not in set(allowed_cameras): return False
    return True
