from app.admin_alerts import _admin_dashboard_link
from app.core.config import settings


def test_admin_alert_link_uses_current_public_host(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://cabinet.example.test/")

    assert _admin_dashboard_link() == "https://cabinet.example.test/admin"
