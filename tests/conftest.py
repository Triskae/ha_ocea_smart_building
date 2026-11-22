"""Pytest configuration for Ocea Smart Building tests."""

from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest
import pytest_socket
from homeassistant.components.recorder import Recorder, get_instance
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import async_initialize_recorder
from homeassistant.setup import async_setup_component

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture
def local_http_only(socket_enabled):
    """Allow local HTTP test servers without allowing external connections."""
    pytest_socket.socket_allow_hosts(["127.0.0.1"])


@pytest.fixture
async def real_recorder(hass: HomeAssistant) -> AsyncIterator[Recorder]:
    """Run Recorder with an in-memory database for integration tests."""
    async_initialize_recorder(hass)
    with patch("homeassistant.components.recorder.ALLOW_IN_MEMORY_DB", True):
        assert await async_setup_component(
            hass, "recorder", {"recorder": {"db_url": "sqlite://", "commit_interval": 0}}
        )
    instance = get_instance(hass)
    await instance.async_block_till_done()
    yield instance
    await instance._async_shutdown(None)
