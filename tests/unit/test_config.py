import pytest
from pydantic import ValidationError

from app.config import Settings


def test_settings_load_from_explicit_values():
    s = Settings(
        tenant_id="tenant",
        client_id="client",
        client_secret="secret",
        sharepoint_site_id="site",
        sharepoint_drive_id="drive",
    )
    assert s.sharepoint_host == "auximotechgmbh.sharepoint.com"
    assert s.graph_base_url == "https://graph.microsoft.com/v1.0"
    assert s.authority == "https://login.microsoftonline.com/tenant"


@pytest.mark.parametrize("missing_field", ["tenant_id", "client_id", "client_secret", "sharepoint_site_id", "sharepoint_drive_id"])
def test_settings_requires_core_fields(missing_field):
    values = {
        "tenant_id": "tenant",
        "client_id": "client",
        "client_secret": "secret",
        "sharepoint_site_id": "site",
        "sharepoint_drive_id": "drive",
        "_env_file": None,
    }
    del values[missing_field]
    with pytest.raises(ValidationError):
        Settings(**values)


def test_settings_rejects_blank_secret():
    with pytest.raises(ValidationError):
        Settings(
            tenant_id="tenant",
            client_id="client",
            client_secret="   ",
            sharepoint_site_id="site",
            sharepoint_drive_id="drive",
        )
