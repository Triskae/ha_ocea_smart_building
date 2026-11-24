"""Tests for integration setup lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from yarl import URL

from custom_components.ocea_smart_building import (
    PLATFORMS,
    async_migrate_entry,
    async_remove_entry,
    async_setup_entry,
)
from custom_components.ocea_smart_building.const import CONF_LOCAL_ID, DOMAIN
from custom_components.ocea_smart_building.history import history_store
from custom_components.ocea_smart_building.ocea.models import (
    OCEAConsumptionSeries,
    OCEADashboardData,
    OCEAPdsData,
)


async def test_migrate_entry_preserves_data_and_identity(hass: HomeAssistant) -> None:
    """Migrate an existing account entry without recreating it."""
    data = {CONF_LOCAL_ID: "local-123", CONF_EMAIL: "resident@example.com", "hidden": "value"}
    entry = MockConfigEntry(domain=DOMAIN, version=1, unique_id="resident@example.com", data=data)
    entry.add_to_hass(hass)
    entry_id = entry.entry_id

    assert await async_migrate_entry(hass, entry)
    assert entry.unique_id == "local-123"
    assert entry.version == 2
    assert entry.entry_id == entry_id
    assert dict(entry.data) == data


async def test_migrate_entry_rejects_duplicate_dwelling(hass: HomeAssistant) -> None:
    """Do not overwrite another entry for the same dwelling."""
    existing = MockConfigEntry(domain=DOMAIN, version=2, unique_id="local-123")
    existing.add_to_hass(hass)
    entry = MockConfigEntry(
        domain=DOMAIN, version=1, unique_id="resident@example.com",
        data={CONF_LOCAL_ID: "local-123"},
    )
    entry.add_to_hass(hass)

    assert not await async_migrate_entry(hass, entry)
    assert entry.version == 1
    assert entry.unique_id == "resident@example.com"


@pytest.mark.usefixtures("enable_custom_integrations", "real_recorder")
async def test_migration_and_reload_preserve_leaks_without_restoring_consumption(
    hass: HomeAssistant,
) -> None:
    """Migrate an existing registry and keep only the leak entities active."""
    data = {
        CONF_EMAIL: "resident@example.com",
        CONF_PASSWORD: "secret",
        CONF_LOCAL_ID: "local",
        "hidden": "value",
    }
    entry = MockConfigEntry(domain=DOMAIN, version=1, unique_id="resident@example.com", data=data)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    old_entities = [
        registry.async_get_or_create("sensor", DOMAIN, unique_id, config_entry=entry)
        for unique_id in (
            f"{DOMAIN}_local_eau_froide",
            f"{DOMAIN}_local_pds-1_eau_froide",
        )
    ]
    leak = registry.async_get_or_create(
        "sensor", DOMAIN, f"{DOMAIN}_local_pds-1_fuite", config_entry=entry
    )
    leak_binary = registry.async_get_or_create(
        "binary_sensor", DOMAIN, f"{DOMAIN}_local_pds-1_fuite_detectee", config_entry=entry
    )

    with (
        patch("custom_components.ocea_smart_building.OCEAApiClient", autospec=True) as client,
        patch("custom_components.ocea_smart_building.OCEAHistoryManager", autospec=True),
    ):
        client.return_value.get_pds.return_value = [
            OCEAPdsData.from_api({"id": "pds-1", "fluide": "EauFroide"})
        ]
        client.return_value.get_appareils.return_value = []
        client.return_value.get_consumptions.return_value = [
            OCEADashboardData.from_api({"fluide": "EauFroide", "valeur": "1"})
        ]
        client.return_value.get_pds_consumption.return_value = OCEAConsumptionSeries.from_api({
            "unite": "m3",
            "consommations": [{
                "date": "2024-01-01",
                "valeur": 0.1,
                "fuiteEstimee": 0.01,
                "type": "ConsommationEauResponse",
            }],
        })
        for _ in range(2):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            assert entry.version == 2
            assert entry.unique_id == "local"
            assert dict(entry.data) == data
            assert hass.states.get(leak.entity_id).state == "0.01"
            assert hass.states.get(leak_binary.entity_id).state == "on"
            for old in old_entities:
                state = hass.states.get(old.entity_id)
                assert state is None or state.state == "unavailable"
                assert registry.async_get(old.entity_id) is not None
            assert await hass.config_entries.async_unload(entry.entry_id)


async def test_removing_entry_removes_cached_history(hass: HomeAssistant) -> None:
    """Removing an entry removes its cache, not Recorder statistics."""
    entry = MockConfigEntry(domain=DOMAIN)
    await history_store(hass, entry.entry_id).async_save({"meters": {}})
    await async_remove_entry(hass, entry)
    assert await history_store(hass, entry.entry_id).async_load() is None


@pytest.mark.usefixtures("enable_custom_integrations", "real_recorder")
async def test_unload_cancels_history_task(hass: HomeAssistant) -> None:
    """An ongoing history request is cancelled before unloading the entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id="local",
        data={CONF_EMAIL: "resident@example.com", CONF_PASSWORD: "secret", CONF_LOCAL_ID: "local"},
    )
    entry.add_to_hass(hass)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_resident() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with patch("custom_components.ocea_smart_building.OCEAApiClient", autospec=True) as client:
        client.return_value.get_resident.side_effect = blocked_resident
        client.return_value.get_pds.return_value = []
        client.return_value.get_appareils.return_value = []
        client.return_value.get_consumptions.return_value = []
        assert await hass.config_entries.async_setup(entry.entry_id)
        await asyncio.wait_for(started.wait(), timeout=3)
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert cancelled.is_set()


@pytest.mark.usefixtures("enable_custom_integrations", "real_recorder")
async def test_setup_keeps_only_water_leak_entities(
    hass: HomeAssistant,
) -> None:
    """Water leaks remain entities, while CETC cannot create leak entities."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id="local",
        data={CONF_EMAIL: "resident@example.com", CONF_PASSWORD: "secret", CONF_LOCAL_ID: "local"},
    )
    entry.add_to_hass(hass)
    with (
        patch("custom_components.ocea_smart_building.OCEAApiClient", autospec=True) as client,
        patch("custom_components.ocea_smart_building.OCEAHistoryManager", autospec=True),
    ):
        client.return_value.get_pds.return_value = [
            OCEAPdsData.from_api({"id": "pds-1", "fluide": "EauFroide"}),
            OCEAPdsData.from_api({"id": "pds-2", "fluide": "Cetc"}),
            OCEAPdsData.from_api({"id": "pds-3", "fluide": "CETC"}),
        ]
        client.return_value.get_appareils.return_value = []
        client.return_value.get_consumptions.return_value = [
            OCEADashboardData.from_api({"fluide": "EauFroide", "valeur": "1"}),
            OCEADashboardData.from_api({"fluide": "Cetc", "valeur": "2"}),
        ]
        client.return_value.get_pds_consumption.side_effect = [
            OCEAConsumptionSeries.from_api({
                "unite": "m3",
                "consommations": [
                    {
                        "date": "2024-01-01",
                        "valeur": 0.1,
                        "fuiteEstimee": 0.01,
                        "type": "ConsommationEauResponse",
                    }
                ],
            }),
            OCEAConsumptionSeries.from_api({
                "unite": "kWh",
                "consommations": [
                    {
                        "date": "2024-01-01",
                        "valeur": 1,
                        "type": "ConsommationResponse",
                    }
                ],
            }),
            OCEAConsumptionSeries.from_api({
                "unite": "m3",
                "consommations": [
                    {
                        "date": "2024-01-01",
                        "valeur": 0.1,
                        "fuiteEstimee": 0.01,
                        "type": "ConsommationEauResponse",
                    }
                ],
            }),
            OCEAConsumptionSeries.from_api({
                "unite": "kWh",
                "consommations": [
                    {
                        "date": "2024-01-01",
                        "valeur": 1,
                        "type": "ConsommationResponse",
                    }
                ],
            }),
        ]
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        assert {entity.unique_id for entity in entities} == {
            f"{DOMAIN}_local_pds-1_fuite",
            f"{DOMAIN}_local_pds-1_fuite_detectee",
        }
        coordinator = entry.runtime_data
        assert set(coordinator.data.meters) == {"pds-1", "pds-2"}
        assert coordinator.data.meters["pds-2"].unit == "kWh"
        await coordinator.async_request_refresh()
        await hass.async_block_till_done()
        entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        assert {entity.unique_id for entity in entities} == {
            f"{DOMAIN}_local_pds-1_fuite",
            f"{DOMAIN}_local_pds-1_fuite_detectee",
        }
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_setup_stores_coordinator_in_entry_runtime_data(
    hass: HomeAssistant,
) -> None:
    """Test the coordinator is stored on the loaded config entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EMAIL: "resident@example.com",
            CONF_PASSWORD: "secret",
            CONF_LOCAL_ID: "local-123",
        },
    )
    entry.add_to_hass(hass)

    with (
        patch("custom_components.ocea_smart_building.OCEAApiClient"),
        patch(
            "custom_components.ocea_smart_building.OCEADataUpdateCoordinator"
        ) as coordinator_class,
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new_callable=AsyncMock,
        ) as forward_entry_setups,
    ):
        coordinator_class.return_value.async_config_entry_first_refresh = AsyncMock()
        assert await async_setup_entry(hass, entry)

    assert entry.runtime_data is coordinator_class.return_value
    forward_entry_setups.assert_awaited_once_with(entry, PLATFORMS)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_setup_closes_client_when_first_refresh_fails(
    hass: HomeAssistant,
) -> None:
    """Test a failed initial refresh does not leak the HTTP session."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EMAIL: "resident@example.com",
            CONF_PASSWORD: "secret",
            CONF_LOCAL_ID: "local-123",
        },
    )
    entry.add_to_hass(hass)

    with (
        patch("custom_components.ocea_smart_building.OCEAApiClient") as client_class,
        patch(
            "custom_components.ocea_smart_building.OCEADataUpdateCoordinator"
        ) as coordinator_class,
    ):
        coordinator_class.return_value.async_config_entry_first_refresh.side_effect = (
            RuntimeError("refresh failed")
        )

        with pytest.raises(RuntimeError, match="refresh failed"):
            await async_setup_entry(hass, entry)

    assert client_class.call_args.kwargs["session"].closed


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_setup_detaches_session_when_cancelled(hass: HomeAssistant) -> None:
    """Cancellation during first refresh releases the isolated session."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_EMAIL: "resident@example.com", CONF_PASSWORD: "secret", CONF_LOCAL_ID: "local-123"},
    )
    entry.add_to_hass(hass)
    with (
        patch("custom_components.ocea_smart_building.OCEAApiClient") as client_class,
        patch("custom_components.ocea_smart_building.OCEADataUpdateCoordinator", autospec=True) as coordinator_class,
    ):
        coordinator_class.return_value.async_config_entry_first_refresh.side_effect = asyncio.CancelledError
        with pytest.raises(asyncio.CancelledError):
            await async_setup_entry(hass, entry)
    assert client_class.call_args.kwargs["session"].closed


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_setup_detaches_session_when_platform_setup_fails(
    hass: HomeAssistant,
) -> None:
    """A platform setup error also releases the session."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EMAIL: "resident@example.com",
            CONF_PASSWORD: "secret",
            CONF_LOCAL_ID: "local-123",
        },
    )
    entry.add_to_hass(hass)
    with (
        patch("custom_components.ocea_smart_building.OCEAApiClient") as client_class,
        patch("custom_components.ocea_smart_building.OCEADataUpdateCoordinator", autospec=True),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new_callable=AsyncMock,
            side_effect=RuntimeError("platform setup failed"),
        ),
        pytest.raises(RuntimeError, match="platform setup failed"),
    ):
        await async_setup_entry(hass, entry)
    assert client_class.call_args.kwargs["session"].closed


@pytest.mark.usefixtures("enable_custom_integrations", "real_recorder")
async def test_session_is_detached_after_entry_unload(hass: HomeAssistant) -> None:
    """The isolated session is alive during setup and detached after unload."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_EMAIL: "resident@example.com", CONF_PASSWORD: "secret", CONF_LOCAL_ID: "local-123"},
    )
    entry.add_to_hass(hass)
    with (
        patch("custom_components.ocea_smart_building.OCEAApiClient") as client_class,
        patch("custom_components.ocea_smart_building.OCEADataUpdateCoordinator", autospec=True),
        patch("custom_components.ocea_smart_building.OCEAHistoryManager", autospec=True),
        patch.object(hass.config_entries, "async_forward_entry_setups", new_callable=AsyncMock),
        patch.object(hass.config_entries, "async_unload_platforms", new_callable=AsyncMock, return_value=True),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        session = client_class.call_args.kwargs["session"]
        url = URL("https://login.example.test/")
        session.cookie_jar.update_cookies({"csrf": "test=="}, response_url=url)
        assert session.cookie_jar.filter_cookies(url)["csrf"].coded_value == "test=="
        assert not session.closed
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert session.closed


@pytest.mark.usefixtures("enable_custom_integrations", "real_recorder")
async def test_unload_removes_sensor_update_listener(hass: HomeAssistant) -> None:
    """The dynamic sensor listener is removed with the config entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EMAIL: "resident@example.com",
            CONF_PASSWORD: "secret",
            CONF_LOCAL_ID: "local-123",
        },
    )
    entry.add_to_hass(hass)
    with (
        patch("custom_components.ocea_smart_building.OCEAApiClient", autospec=True) as client,
        patch("custom_components.ocea_smart_building.OCEAHistoryManager", autospec=True),
    ):
        client.return_value.get_pds.return_value = [
            OCEAPdsData.from_api({"id": "pds-1", "fluide": "EauFroide"})
        ]
        client.return_value.get_appareils.return_value = []
        client.return_value.get_consumptions.return_value = [
            OCEADashboardData.from_api({"fluide": "EauFroide", "valeur": "1"})
        ]
        client.return_value.get_pds_consumption.return_value = OCEAConsumptionSeries.from_api(
            {"consommations": []}
        )

        assert await hass.config_entries.async_setup(entry.entry_id)
        coordinator = entry.runtime_data
        registry = er.async_get(hass)
        assert not any(
            entity.unique_id.endswith("_fuite")
            for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
        )
        data = coordinator.data
        reading = data.meters["pds-1"]
        coordinator.async_set_updated_data(
            replace(data, meters={"pds-1": replace(reading, estimated_leak=Decimal("0.1"))})
        )
        await hass.async_block_till_done()
        assert any(
            entity.unique_id.endswith("_fuite")
            for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
        )
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert not coordinator._listeners
