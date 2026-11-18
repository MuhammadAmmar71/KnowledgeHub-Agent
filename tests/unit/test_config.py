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


def _base(**overrides):
    values = dict(
        tenant_id="tenant", client_id="client", client_secret="secret",
        sharepoint_site_id="site", sharepoint_drive_id="drive", _env_file=None,
    )
    values.update(overrides)
    return values


def test_client_secret_is_not_exposed_by_repr():
    s = Settings(**_base(client_secret="  super-secret-value  "))
    assert "super-secret-value" not in repr(s)
    assert s.client_secret.get_secret_value() == "super-secret-value"


@pytest.mark.parametrize("value", ["2:00", "24:00", "02:60", "noon", ""])
def test_schedule_time_must_be_hh_mm(value):
    with pytest.raises(ValidationError):
        Settings(**_base(sync_schedule_time=value))


def test_schedule_timezone_is_validated():
    assert Settings(**_base(sync_schedule_timezone="Europe/Berlin")).sync_schedule_timezone == "Europe/Berlin"
    assert Settings(**_base(sync_schedule_timezone="")).sync_schedule_timezone == ""
    with pytest.raises(ValidationError):
        Settings(**_base(sync_schedule_timezone="Mars/Olympus_Mons"))


def test_blank_api_key_means_no_key():
    assert Settings(**_base(api_key="")).api_key is None
    assert Settings(**_base(api_key="k")).api_key.get_secret_value() == "k"


def test_log_format_is_validated():
    assert Settings(**_base(log_format="JSON")).log_format == "json"
    with pytest.raises(ValidationError):
        Settings(**_base(log_format="xml"))
