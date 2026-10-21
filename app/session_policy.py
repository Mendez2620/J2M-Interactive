from datetime import datetime, timedelta, timezone

from flask.sessions import SecureCookieSessionInterface
from itsdangerous import BadSignature


class RoleAwareSessionInterface(SecureCookieSessionInterface):
    """Give opted-in staff cookies their own lifetime without changing globals.

    The remembered marker lives inside Flask's signed cookie. Super Admin and
    every other permanent session continue to use PERMANENT_SESSION_LIFETIME.
    Server-side StaffSession expiry remains authoritative and independently
    revocable.
    """

    @staticmethod
    def _is_remembered_staff(session):
        return bool(
            (session.get("staff_remember") and session.get("staff_session_id"))
            or (session.get("manager_remember") and session.get("manager_session_id"))
        )

    def get_expiration_time(self, app, session):
        if session.permanent and self._is_remembered_staff(session):
            return datetime.now(timezone.utc) + timedelta(
                seconds=app.config["REMEMBER_SESSION_ABSOLUTE_SECONDS"]
            )
        return super().get_expiration_time(app, session)

    def open_session(self, app, request):
        serializer = self.get_signing_serializer(app)
        if serializer is None:
            return None
        value = request.cookies.get(self.get_cookie_name(app))
        if not value:
            return self.session_class()
        try:
            data, signed_at = serializer.loads(
                value,
                max_age=app.config["REMEMBER_SESSION_ABSOLUTE_SECONDS"],
                return_timestamp=True,
            )
        except BadSignature:
            return self.session_class()
        remembered_staff = self._is_remembered_staff(data)
        allowed_seconds = (
            app.config["REMEMBER_SESSION_ABSOLUTE_SECONDS"]
            if remembered_staff else int(app.permanent_session_lifetime.total_seconds())
        )
        if (datetime.now(timezone.utc) - signed_at).total_seconds() > allowed_seconds:
            return self.session_class()
        return self.session_class(data)
