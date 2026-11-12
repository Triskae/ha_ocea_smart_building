"""Pytest configuration for Ocea Smart Building tests."""

import pytest
import pytest_socket

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture
def local_http_only(socket_enabled):
    """Allow local HTTP test servers without allowing external connections."""
    pytest_socket.socket_allow_hosts(["127.0.0.1"])
