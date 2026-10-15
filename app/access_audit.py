import json

from app.models import AccessAudit


def staff_access_state(staff):
    return {
        "is_active": staff.is_active,
        "web_access_enabled": staff.web_access_enabled,
        "mobile_access_enabled": staff.mobile_access_enabled,
        "permissions": sorted(filter(None, (staff.permissions or "").split(","))),
    }


def access_audit(action, affected, previous=None, new=None, platform_admin_id=None, actor_staff_id=None):
    return AccessAudit(
        action=action,
        platform_admin_id=platform_admin_id,
        actor_staff_id=actor_staff_id,
        affected_staff_id=affected.id,
        business_id=affected.business_id,
        previous_state=json.dumps(previous, sort_keys=True) if previous is not None else None,
        new_state=json.dumps(new, sort_keys=True) if new is not None else None,
    )
