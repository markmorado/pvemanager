"""Long-lived console sockets close once their session or permission is gone."""
import asyncio

from app.auth import watch_ws_access, ws_access_still_valid
from app.services.security_service import SecurityService


def test_access_revoked_by_session_termination(db_session, seed_users):
    user = seed_users["user"]
    session, _ = SecurityService.create_session(db_session, user, "127.0.0.1", "pytest")

    assert ws_access_still_valid(user.id, session.session_token)
    SecurityService.terminate_session(db_session, session.session_token)
    assert not ws_access_still_valid(user.id, session.session_token)


def test_watchdog_check_does_not_extend_the_session(db_session, seed_users):
    from datetime import timedelta
    from app.config import utcnow

    user = seed_users["user"]
    session, _ = SecurityService.create_session(db_session, user, "127.0.0.1", "pytest")
    session.last_activity = utcnow() - timedelta(hours=1)
    expires = utcnow() + timedelta(minutes=1)
    session.expires_at = expires
    db_session.commit()

    assert ws_access_still_valid(user.id, session.session_token)
    db_session.expire_all()
    assert abs((session.expires_at.replace(tzinfo=None) - expires.replace(tzinfo=None)).total_seconds()) < 1


def test_access_revoked_by_permission_check(db_session, seed_users):
    user = seed_users["user"]
    assert ws_access_still_valid(user.id, None, lambda db, u: True)
    assert not ws_access_still_valid(user.id, None, lambda db, u: False)

    user.is_active = False
    db_session.commit()
    assert not ws_access_still_valid(user.id, None)


def test_watchdog_returns_when_access_ends(db_session, seed_users):
    user = seed_users["user"]
    session, _ = SecurityService.create_session(db_session, user, "127.0.0.1", "pytest")
    SecurityService.terminate_session(db_session, session.session_token)

    asyncio.run(asyncio.wait_for(
        watch_ws_access(user.id, session.session_token, interval=0.01), timeout=5))
