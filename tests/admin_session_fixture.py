from app.extensions import db
from app.models import PlatformAdmin
from app.services.platform_admin_session import issue


def issue_for(admin_id):
    return issue(db.session.get(PlatformAdmin, admin_id))
