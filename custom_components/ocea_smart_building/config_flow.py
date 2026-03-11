"""Config flow for Ocea Smart Building integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD

from .api import OceaApiClient, OceaAuthError
from .const import CONF_LOCAL_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)


class OceaSmartBuildingConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Ocea Smart Building."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_EMAIL].lower())
            self._abort_if_unique_id_configured()

            client = OceaApiClient(
                email=user_input[CONF_EMAIL],
                password=user_input[CONF_PASSWORD],
            )

            try:
                resident_data = await self.hass.async_add_executor_job(
                    client.validate_credentials
                )
            except OceaAuthError:
                errors["base"] = "invalid_auth"
                resident_data = None
            except Exception:
                _LOGGER.exception("Unexpected error during config flow")
                errors["base"] = "unknown"
                resident_data = None
            finally:
                client.close()

            if not errors and resident_data:
                occupations = resident_data.get("occupations", [])
                if not occupations:
                    errors["base"] = "no_occupation"
                else:
                    local_id = occupations[0].get("logementId", "")
                    resident = resident_data.get("resident", {})
                    name = resident.get("prenom", "")

                    return self.async_create_entry(
                        title=f"Ocea - {name}" if name else f"Ocea - {local_id}",
                        data={
                            CONF_EMAIL: user_input[CONF_EMAIL],
                            CONF_PASSWORD: user_input[CONF_PASSWORD],
                            CONF_LOCAL_ID: local_id,
                        },
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle reauth."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reauth confirmation."""
        errors: dict[str, str] = {}

        if user_input is not None:
            reauth_entry = self._get_reauth_entry()
            client = OceaApiClient(
                email=user_input[CONF_EMAIL],
                password=user_input[CONF_PASSWORD],
                local_id=reauth_entry.data[CONF_LOCAL_ID],
            )

            try:
                await self.hass.async_add_executor_job(
                    client.validate_credentials
                )
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data={
                        **reauth_entry.data,
                        CONF_EMAIL: user_input[CONF_EMAIL],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )
            except OceaAuthError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected error during reauth")
                errors["base"] = "unknown"
            finally:
                client.close()

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )
