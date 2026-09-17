"""Tests for the Ocea Smart Building config flow."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import ANY, patch

import pytest
from aiohttp import ClientError
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult, FlowResultType
from homeassistant.helpers.selector import TextSelector
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ocea_smart_building.const import (
    CONF_LOCAL_ID,
    DOMAIN,
    PASSWORD_NOT_CHANGED,
    UA,
)
from custom_components.ocea_smart_building.ocea import OceaApiError, OceaAuthError

OLD_EMAIL = "resident@example.com"
OLD_PASSWORD = "stored-secret"
OLD_LOCAL_ID = "local-123"
OLD_TITLE = "Ocea - Alice"

RESIDENT_DATA = {
    "resident": {"prenom": "Alice"},
    "occupations": [{"logementId": OLD_LOCAL_ID}],
}


@pytest.fixture
def config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create an existing config entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=OLD_TITLE,
        unique_id=OLD_EMAIL,
        data={
            CONF_EMAIL: OLD_EMAIL,
            CONF_PASSWORD: OLD_PASSWORD,
            CONF_LOCAL_ID: OLD_LOCAL_ID,
            "access_token": "hidden-token",
        },
    )
    entry.add_to_hass(hass)
    return entry


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_user_flow_recovers_from_auth_error(hass: HomeAssistant) -> None:
    """A user can retry credentials and each attempt releases its own session."""
    with (
        patch(
            "custom_components.ocea_smart_building.config_flow.OceaApiClient", autospec=True
        ) as client_class,
        patch("custom_components.ocea_smart_building.async_setup_entry", return_value=True),
    ):
        client_class.return_value.validate_credentials.side_effect = [
            OceaAuthError("invalid credentials"),
            RESIDENT_DATA,
        ]
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        inputs = {CONF_EMAIL: OLD_EMAIL, CONF_PASSWORD: OLD_PASSWORD}
        result = await hass.config_entries.flow.async_configure(result["flow_id"], inputs)
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "invalid_auth"}
        result = await hass.config_entries.flow.async_configure(result["flow_id"], inputs)
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["result"].unique_id == OLD_EMAIL
        await hass.async_block_till_done()

    sessions = [call.kwargs["session"] for call in client_class.call_args_list]
    assert len(sessions) == 2 and sessions[0] is not sessions[1]
    assert all(session.closed for session in sessions)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_user_flow_does_not_log_exception_details(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Unexpected errors may contain sensitive URLs or credentials."""
    with patch(
        "custom_components.ocea_smart_building.config_flow.OceaApiClient", autospec=True
    ) as client_class:
        client_class.return_value.validate_credentials.side_effect = RuntimeError(
            "sensitive-placeholder"
        )
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_EMAIL: OLD_EMAIL, CONF_PASSWORD: OLD_PASSWORD}
        )
    assert result["errors"] == {"base": "unknown"}
    assert "sensitive-placeholder" not in caplog.text
    assert client_class.call_args.kwargs["session"].closed


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_reauth_recovers_from_auth_error(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Reauthentication retries safely and keeps unrelated entry data."""
    with (
        patch(
            "custom_components.ocea_smart_building.config_flow.OceaApiClient", autospec=True
        ) as client_class,
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        client_class.return_value.validate_credentials.side_effect = [
            OceaAuthError("invalid credentials"),
            RESIDENT_DATA,
        ]
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH, "entry_id": config_entry.entry_id},
            data=dict(config_entry.data),
        )
        inputs = {CONF_EMAIL: OLD_EMAIL, CONF_PASSWORD: "replacement"}
        result = await hass.config_entries.flow.async_configure(result["flow_id"], inputs)
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "invalid_auth"}
        result = await hass.config_entries.flow.async_configure(result["flow_id"], inputs)
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_PASSWORD] == "replacement"
    assert config_entry.data["access_token"] == "hidden-token"
    assert all(call.kwargs["session"].closed for call in client_class.call_args_list)


async def _async_init_reconfigure(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> FlowResult:
    """Start a reconfigure flow for an existing entry."""
    return await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": SOURCE_RECONFIGURE,
            "entry_id": config_entry.entry_id,
        },
    )


async def _async_init_reauth(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> FlowResult:
    """Start a reauthentication flow for an existing config entry."""
    return await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": SOURCE_REAUTH,
            "entry_id": config_entry.entry_id,
        },
    )


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_successful_reauthentication_preserves_entry_data(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Test reauth updates credentials without replacing hidden entry data."""
    with (
        patch(
            "custom_components.ocea_smart_building.config_flow.OceaApiClient", autospec=True
        ) as client_class,
        patch.object(hass.config_entries, "async_schedule_reload") as schedule_reload,
    ):
        client_class.return_value.validate_credentials.return_value = RESIDENT_DATA
        result = await _async_init_reauth(hass, config_entry)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_EMAIL: OLD_EMAIL,
                CONF_PASSWORD: "new-secret",
            },
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_PASSWORD] == "new-secret"
    assert config_entry.data["access_token"] == "hidden-token"
    assert config_entry.unique_id == OLD_EMAIL
    schedule_reload.assert_called_once_with(config_entry.entry_id)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_reauthentication_rejects_a_different_account(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Test reauth cannot replace the configured account identity."""
    with patch(
        "custom_components.ocea_smart_building.config_flow.OceaApiClient", autospec=True
    ) as client_class:
        client_class.return_value.validate_credentials.return_value = RESIDENT_DATA
        result = await _async_init_reauth(hass, config_entry)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_EMAIL: "other@example.com",
                CONF_PASSWORD: "new-secret",
            },
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"
    assert config_entry.data[CONF_PASSWORD] == OLD_PASSWORD


@pytest.mark.parametrize(
    ("exception", "expected_error"),
    [
        (OceaApiError("server unavailable"), "cannot_connect"),
        (ClientError("network unavailable"), "cannot_connect"),
        (TimeoutError("timeout"), "cannot_connect"),
    ],
)
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_reauthentication_connection_errors(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    exception: Exception,
    expected_error: str,
) -> None:
    """Test reauth maps API and network failures to connection errors."""
    with patch(
        "custom_components.ocea_smart_building.config_flow.OceaApiClient", autospec=True
    ) as client_class:
        client_class.return_value.validate_credentials.side_effect = exception
        result = await _async_init_reauth(hass, config_entry)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_EMAIL: OLD_EMAIL,
                CONF_PASSWORD: "new-secret",
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected_error}


@pytest.mark.parametrize("source", [SOURCE_USER, SOURCE_REAUTH, SOURCE_RECONFIGURE])
@pytest.mark.parametrize(
    ("failure", "error"),
    [
        (OceaAuthError("invalid"), "invalid_auth"),
        (OceaApiError("unavailable"), "cannot_connect"),
        (ClientError("unavailable"), "cannot_connect"),
        (TimeoutError(), "cannot_connect"),
        (RuntimeError("unexpected"), "unknown"),
        ({}, "no_occupation"),
    ],
)
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_flows_recover_from_errors(
    hass: HomeAssistant, source: str, failure: Exception | dict, error: str
) -> None:
    """Every form error allows the same flow to complete after a retry."""
    entry = None
    context = {"source": source}
    if source != SOURCE_USER:
        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id=OLD_EMAIL,
            title=OLD_TITLE,
            data={
                CONF_EMAIL: OLD_EMAIL,
                CONF_PASSWORD: OLD_PASSWORD,
                CONF_LOCAL_ID: OLD_LOCAL_ID,
                "access_token": "hidden-token",
            },
        )
        entry.add_to_hass(hass)
        context["entry_id"] = entry.entry_id
    with (
        patch(
            "custom_components.ocea_smart_building.config_flow.OceaApiClient", autospec=True
        ) as client_class,
        patch("custom_components.ocea_smart_building.async_setup_entry", return_value=True),
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        client_class.return_value.validate_credentials.side_effect = [failure, RESIDENT_DATA]
        result = await hass.config_entries.flow.async_init(DOMAIN, context=context)
        inputs = {CONF_EMAIL: OLD_EMAIL, CONF_PASSWORD: "replacement"}
        result = await hass.config_entries.flow.async_configure(result["flow_id"], inputs)
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": error}
        result = await hass.config_entries.flow.async_configure(result["flow_id"], inputs)
        if source == SOURCE_USER:
            assert result["type"] is FlowResultType.CREATE_ENTRY
            assert result["result"].unique_id == OLD_EMAIL
        else:
            assert result["type"] is FlowResultType.ABORT
            assert result["reason"] == f"{source}_successful"
            assert entry.data["access_token"] == "hidden-token"
        await hass.async_block_till_done()
        assert all(call.kwargs["session"].closed for call in client_class.call_args_list)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_user_flow_rejects_duplicate_account(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """An existing account cannot be added twice, regardless of email case."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_EMAIL: OLD_EMAIL.upper(), CONF_PASSWORD: "replacement"},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_reconfigure_rejects_duplicate_account(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Reconfiguration cannot take over an already configured account."""
    other = MockConfigEntry(domain=DOMAIN, unique_id="other@example.com")
    other.add_to_hass(hass)
    with patch(
        "custom_components.ocea_smart_building.config_flow.OceaApiClient", autospec=True
    ) as client_class:
        client_class.return_value.validate_credentials.return_value = RESIDENT_DATA
        result = await _async_init_reconfigure(hass, config_entry)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: "other@example.com", CONF_PASSWORD: "replacement"},
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert config_entry.data[CONF_EMAIL] == OLD_EMAIL
    assert client_class.call_args.kwargs["session"].closed


@pytest.mark.parametrize("source", [SOURCE_USER, SOURCE_REAUTH, SOURCE_RECONFIGURE])
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_flows_use_validated_dwelling_when_name_missing(
    hass: HomeAssistant, source: str
) -> None:
    """The validated dwelling and fallback title stay synchronized in all flows."""
    entry = None
    context = {"source": source}
    if source != SOURCE_USER:
        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id=OLD_EMAIL,
            title=OLD_TITLE,
            data={
                CONF_EMAIL: OLD_EMAIL,
                CONF_PASSWORD: OLD_PASSWORD,
                CONF_LOCAL_ID: OLD_LOCAL_ID,
            },
        )
        entry.add_to_hass(hass)
        context["entry_id"] = entry.entry_id
    with (
        patch(
            "custom_components.ocea_smart_building.config_flow.OceaApiClient", autospec=True
        ) as client_class,
        patch("custom_components.ocea_smart_building.async_setup_entry", return_value=True),
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        client_class.return_value.validate_credentials.return_value = {
            "occupations": [{"logementId": "new-dwelling"}]
        }
        result = await hass.config_entries.flow.async_init(DOMAIN, context=context)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: OLD_EMAIL, CONF_PASSWORD: "replacement"},
        )
        if source == SOURCE_USER:
            assert result["type"] is FlowResultType.CREATE_ENTRY
            entry = result["result"]
        else:
            assert result["type"] is FlowResultType.ABORT
            assert result["reason"] == f"{source}_successful"
        assert entry.data[CONF_LOCAL_ID] == "new-dwelling"
        assert entry.title == "Ocea - new-dwelling"
        await hass.async_block_till_done()


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_reauth_updates_validated_dwelling_and_title(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Reauthentication uses the newly validated resident data."""
    with (
        patch(
            "custom_components.ocea_smart_building.config_flow.OceaApiClient", autospec=True
        ) as client_class,
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        client_class.return_value.validate_credentials.return_value = {
            "resident": {"prenom": "Bob"},
            "occupations": [{"logementId": "new-dwelling"}],
        }
        result = await _async_init_reauth(hass, config_entry)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_EMAIL: OLD_EMAIL, CONF_PASSWORD: "replacement"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_LOCAL_ID] == "new-dwelling"
    assert config_entry.data["access_token"] == "hidden-token"
    assert config_entry.title == "Ocea - Bob"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_reconfigure_form_shows_existing_values_without_password(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Test existing values are suggested without exposing the password."""
    result = await _async_init_reconfigure(hass, config_entry)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {}
    assert result["data_schema"] is not None
    defaults = result["data_schema"]({})
    assert defaults == {
        CONF_EMAIL: OLD_EMAIL,
        CONF_PASSWORD: PASSWORD_NOT_CHANGED,
    }
    assert OLD_PASSWORD not in repr(result["data_schema"])


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_successful_reconfiguration(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Test credentials, derived data, unique ID, and title are updated."""
    new_email = "new-resident@example.com"
    new_password = "new-secret"
    resident_data = {
        "resident": {"prenom": "Bob"},
        "occupations": [{"logementId": "local-456"}],
    }

    with (
        patch(
            "custom_components.ocea_smart_building.config_flow.OceaApiClient", autospec=True
        ) as client_class,
        patch.object(hass.config_entries, "async_schedule_reload") as schedule_reload,
    ):
        client_class.return_value.validate_credentials.return_value = resident_data
        result = await _async_init_reconfigure(hass, config_entry)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_EMAIL: new_email,
                CONF_PASSWORD: new_password,
            },
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data == {
        CONF_EMAIL: new_email,
        CONF_PASSWORD: new_password,
        CONF_LOCAL_ID: "local-456",
        "access_token": "hidden-token",
    }
    assert config_entry.unique_id == new_email
    assert config_entry.title == "Ocea - Bob"
    schedule_reload.assert_called_once_with(config_entry.entry_id)
    assert client_class.call_args.kwargs["session"].closed


@pytest.mark.parametrize(
    ("exception", "expected_error"),
    [
        (OceaAuthError("invalid credentials"), "invalid_auth"),
        (OceaApiError("server unavailable"), "cannot_connect"),
        (RuntimeError("unexpected"), "unknown"),
    ],
)
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_reconfigure_validation_error(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    exception: Exception,
    expected_error: str,
) -> None:
    """Test validation errors are mapped to translated error keys."""
    with patch(
        "custom_components.ocea_smart_building.config_flow.OceaApiClient", autospec=True
    ) as client_class:
        client_class.return_value.validate_credentials.side_effect = exception
        result = await _async_init_reconfigure(hass, config_entry)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_EMAIL: OLD_EMAIL,
                CONF_PASSWORD: PASSWORD_NOT_CHANGED,
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": expected_error}
    assert config_entry.data[CONF_PASSWORD] == OLD_PASSWORD
    assert client_class.call_args.kwargs["session"].closed


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_reconfigure_preserves_secrets(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Test an unchanged password and unshown tokens remain stored."""
    new_email = "changed@example.com"

    with (
        patch(
            "custom_components.ocea_smart_building.config_flow.OceaApiClient", autospec=True
        ) as client_class,
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        client_class.return_value.validate_credentials.return_value = RESIDENT_DATA
        result = await _async_init_reconfigure(hass, config_entry)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_EMAIL: new_email,
                CONF_PASSWORD: PASSWORD_NOT_CHANGED,
            },
        )

    assert result["type"] is FlowResultType.ABORT
    assert config_entry.data[CONF_PASSWORD] == OLD_PASSWORD
    assert config_entry.data["access_token"] == "hidden-token"
    client_class.assert_called_once_with(
        email=new_email,
        user_agent=UA,
        session=ANY,
        password=OLD_PASSWORD,
        local_id=OLD_LOCAL_ID,
    )


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_reconfigure_without_changes_does_not_reload(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Test an unchanged configuration is validated but not reloaded."""
    with (
        patch(
            "custom_components.ocea_smart_building.config_flow.OceaApiClient", autospec=True
        ) as client_class,
        patch.object(hass.config_entries, "async_schedule_reload") as schedule_reload,
    ):
        client_class.return_value.validate_credentials.return_value = RESIDENT_DATA
        result = await _async_init_reconfigure(hass, config_entry)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_EMAIL: OLD_EMAIL,
                CONF_PASSWORD: PASSWORD_NOT_CHANGED,
            },
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    schedule_reload.assert_not_called()
    client_class.return_value.validate_credentials.assert_called_once_with()


def _translation_data(language: str) -> dict[str, Any]:
    """Load a translation file."""
    path = (
        Path(__file__).parents[1]
        / "custom_components"
        / DOMAIN
        / "translations"
        / f"{language}.json"
    )
    with path.open(encoding="utf-8") as translation_file:
        return json.load(translation_file)


def test_reconfigure_source_strings_are_complete() -> None:
    """Test source strings contain the complete reconfigure structure."""
    path = (
        Path(__file__).parents[1]
        / "custom_components"
        / DOMAIN
        / "strings.json"
    )
    with path.open(encoding="utf-8") as strings_file:
        config = json.load(strings_file)["config"]

    step = config["step"]["reconfigure"]
    assert step["title"] == "Reconfigurer Ocea Smart Building"
    assert set(step["data"]) == {CONF_EMAIL, CONF_PASSWORD}
    assert set(step["data_description"]) == {CONF_EMAIL, CONF_PASSWORD}
    assert config["abort"]["reconfigure_successful"] == (
        "La reconfiguration a réussi"
    )


@pytest.mark.parametrize(
    ("language", "title", "success"),
    [
        ("fr", "Reconfigurer Ocea Smart Building", "La reconfiguration a réussi"),
        ("en", "Reconfigure Ocea Smart Building", "Reconfiguration was successful"),
    ],
)
def test_reconfigure_translations(language: str, title: str, success: str) -> None:
    """Test French and English reconfigure translations are complete."""
    config = _translation_data(language)["config"]
    step = config["step"]["reconfigure"]

    assert step["title"] == title
    assert set(step["data"]) == {CONF_EMAIL, CONF_PASSWORD}
    assert set(step["data_description"]) == {CONF_EMAIL, CONF_PASSWORD}
    assert config["abort"]["reconfigure_successful"] == success
    assert {"cannot_connect", "invalid_auth", "unknown"} <= set(config["error"])


@pytest.mark.parametrize("filename", ["strings.json", "translations/en.json", "translations/fr.json"])
def test_flow_translations_cover_fields_and_abort_reasons(filename: str) -> None:
    """Every credential field and user-visible abort has a translation."""
    path = Path(__file__).parents[1] / "custom_components" / DOMAIN / filename
    config = json.loads(path.read_text(encoding="utf-8"))["config"]
    for step in ("user", "reauth_confirm", "reconfigure"):
        assert set(config["step"][step]["data"]) == {CONF_EMAIL, CONF_PASSWORD}
        assert set(config["step"][step]["data_description"]) == {CONF_EMAIL, CONF_PASSWORD}
    assert {
        "already_configured",
        "unique_id_mismatch",
        "reauth_successful",
        "reconfigure_successful",
    } <= set(config["abort"])


@pytest.mark.parametrize("source", [SOURCE_USER, SOURCE_REAUTH, SOURCE_RECONFIGURE])
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_flow_uses_email_and_password_selectors(
    hass: HomeAssistant, config_entry: MockConfigEntry, source: str
) -> None:
    """Credential inputs use appropriately typed Home Assistant selectors."""
    context = {"source": source}
    if source != SOURCE_USER:
        context["entry_id"] = config_entry.entry_id
    result = await hass.config_entries.flow.async_init(DOMAIN, context=context)
    fields = {key.schema: value for key, value in result["data_schema"].schema.items()}
    assert isinstance(fields[CONF_EMAIL], TextSelector)
    assert fields[CONF_EMAIL].config["type"] == "email"
    assert isinstance(fields[CONF_PASSWORD], TextSelector)
    assert fields[CONF_PASSWORD].config["type"] == "password"
