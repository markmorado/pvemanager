"""SMTP password and Telegram token are stored encrypted and read back in clear."""
from app.models import PanelSettings
from app.services import notification_channels


def test_notification_secrets_encrypted_at_rest(client, db_session, admin_headers):
    resp = client.put("/settings/api/notification-channels/smtp", headers=admin_headers,
                      json={"host": "smtp.example.com", "username": "u", "password": "s3cret-pass"})
    assert resp.status_code == 200, resp.text
    resp = client.put("/settings/api/notification-channels/telegram", headers=admin_headers,
                      json={"telegram_bot_token": "123456:ABCDEF"})
    assert resp.status_code == 200, resp.text

    db_session.expire_all()
    raw = {s.key: s.value for s in db_session.query(PanelSettings).filter(
        PanelSettings.key.in_(["smtp_password", "telegram_bot_token"]))}
    assert raw["smtp_password"] != "s3cret-pass" and raw["telegram_bot_token"] != "123456:ABCDEF"

    assert notification_channels.get_notification_setting("smtp_password") == "s3cret-pass"
    assert notification_channels.get_notification_setting("telegram_bot_token") == "123456:ABCDEF"

    # Rows written before the change (plaintext) still read back.
    row = db_session.query(PanelSettings).filter(PanelSettings.key == "smtp_password").one()
    row.value = "legacy-plain"
    db_session.commit()
    assert notification_channels.get_notification_setting("smtp_password") == "legacy-plain"
