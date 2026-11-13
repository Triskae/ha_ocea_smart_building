"""Config flow for Ocea Smart Building integration."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import voluptuous as vol
from aiohttp import ClientError
from homeassistant.config_entries import ConfigFlow
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import AbortFlow, FlowResult
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.translation import async_get_translations

from .const import CONF_LOCAL_ID, DOMAIN, PASSWORD_NOT_CHANGED, UA
from .ocea import (
    OceaApiClient,
    OceaApiError,
    OceaAuthError,
    OceaDwelling,
    OceaResident,
    dwelling_address,
)

_LOGGER = logging.getLogger(__name__)


class OceaSmartBuildingConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Ocea Smart Building."""

    VERSION = 1

    _pending_data: dict[str, str]
    _pending_name: str
    _dwelling_options: list[SelectOptionDict]

    async def _accessible_dwellings(
        self, client: OceaApiClient, dwellings: tuple[OceaDwelling, ...]
    ) -> list[OceaDwelling]:
        """Check each distinct dwelling without masking other API failures."""
        accessible = []
        for dwelling in dwellings:
            try:
                await client.check_dwelling_access(dwelling.local_id)
            except OceaApiError as err:
                if err.status != 403:
                    raise
            else:
                accessible.append(dwelling)
        return accessible

    def _create_dwelling_entry(self, local_id: str) -> FlowResult:
        """Finish initial setup using a validated dwelling."""
        data = self._pending_data
        name = self._pending_name
        self._pending_data = {}
        self._pending_name = ""
        self._dwelling_options = []
        return self.async_create_entry(
            title=f"Ocea - {name}" if name else f"Ocea - {local_id}",
            data={**data, CONF_LOCAL_ID: local_id},
        )

    async def async_step_user(
        self, user_input: dict[str, str] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_EMAIL].lower())
            self._abort_if_unique_id_configured()

            session = async_create_clientsession(
                self.hass, auto_cleanup=False, connector_owner=False
            )
            client = OceaApiClient(
                email=user_input[CONF_EMAIL],
                user_agent=UA,
                session=session,
                password=user_input[CONF_PASSWORD],
            )

            try:
                resident = OceaResident.from_api(await client.validate_credentials())
                if not resident.dwellings:
                    errors["base"] = "no_occupation"
                else:
                    dwellings = await self._accessible_dwellings(client, resident.dwellings)
                    if not dwellings:
                        errors["base"] = "no_dwelling_access"
                    else:
                        options: list[tuple[OceaDwelling, str | None]] = []
                        for dwelling in dwellings:
                            if len(dwellings) == 1:
                                options.append((dwelling, None))
                                continue
                            try:
                                details = await client.get_dwelling_details(dwelling.local_id)
                            except OceaApiError as err:
                                if err.status != 403:
                                    raise
                            else:
                                options.append((dwelling, dwelling_address(details)))
                        if not options:
                            errors["base"] = "no_dwelling_access"
                        elif len(options) > 1:
                            translations = await async_get_translations(
                                self.hass, self.hass.config.language, "selector", {DOMAIN}
                            )
                            prefix = f"component.{DOMAIN}.selector.dwelling.options"
                            now = datetime.now(timezone.utc)
                            selection_options = [
                                SelectOptionDict(
                                    value=dwelling.local_id,
                                    label=(
                                        f"{index}. {address or translations[f'{prefix}.no_address']}"
                                        f" — {translations[f'{prefix}.{dwelling.status(now)}']}"
                                    ),
                                )
                                for index, (dwelling, address) in enumerate(options, 1)
                            ]
            except OceaAuthError:
                errors["base"] = "invalid_auth"
            except OceaApiError as err:
                errors["base"] = "invalid_auth" if err.status == 401 else "cannot_connect"
            except (ClientError, TimeoutError):
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.error("Unexpected error during config flow")
                errors["base"] = "unknown"
            finally:
                session.detach()

            if not errors:
                self._pending_data = {
                    CONF_EMAIL: user_input[CONF_EMAIL],
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                }
                self._pending_name = resident.name
                if len(options) == 1:
                    return self._create_dwelling_entry(options[0][0].local_id)
                self._dwelling_options = selection_options
                return await self.async_step_dwelling()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="email")
                    ),
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(
                            type=TextSelectorType.PASSWORD, autocomplete="current-password"
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_dwelling(
        self, user_input: dict[str, str] | None = None
    ) -> FlowResult:
        """Choose explicitly among the accessible dwellings."""
        if user_input is not None:
            return self._create_dwelling_entry(user_input[CONF_LOCAL_ID])
        return self.async_show_form(
            step_id="dwelling",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_LOCAL_ID): SelectSelector(
                        SelectSelectorConfig(options=self._dwelling_options)
                    )
                }
            ),
        )

    async def async_step_reauth(self, entry_data: dict[str, object]) -> FlowResult:
        """Handle reauth."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> FlowResult:
        """Handle reauth confirmation."""
        errors: dict[str, str] = {}

        if user_input is not None:
            reauth_entry = self._get_reauth_entry()
            session = async_create_clientsession(
                self.hass, auto_cleanup=False, connector_owner=False
            )
            client = OceaApiClient(
                email=user_input[CONF_EMAIL],
                user_agent=UA,
                session=session,
                password=user_input[CONF_PASSWORD],
                local_id=reauth_entry.data[CONF_LOCAL_ID],
            )

            try:
                resident = OceaResident.from_api(await client.validate_credentials())
                await self.async_set_unique_id(user_input[CONF_EMAIL].lower())
                self._abort_if_unique_id_mismatch()
                if not resident.dwellings:
                    errors["base"] = "no_occupation"
                elif not await self._accessible_dwellings(
                    client,
                    tuple(
                        dwelling
                        for dwelling in resident.dwellings
                        if dwelling.local_id == reauth_entry.data[CONF_LOCAL_ID]
                    ),
                ):
                    errors["base"] = "no_dwelling_access"
            except OceaAuthError:
                errors["base"] = "invalid_auth"
            except OceaApiError as err:
                errors["base"] = "invalid_auth" if err.status == 401 else "cannot_connect"
            except (ClientError, TimeoutError):
                errors["base"] = "cannot_connect"
            except AbortFlow:
                raise
            except Exception:  # noqa: BLE001
                _LOGGER.error("Unexpected error during reauth")
                errors["base"] = "unknown"
            finally:
                session.detach()

            if not errors:
                local_id = reauth_entry.data[CONF_LOCAL_ID]
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    title=f"Ocea - {resident.name}" if resident.name else f"Ocea - {local_id}",
                    data_updates={
                        CONF_EMAIL: user_input[CONF_EMAIL],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="email")
                    ),
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(
                            type=TextSelectorType.PASSWORD, autocomplete="current-password"
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, str] | None = None
    ) -> FlowResult:
        """Handle reconfiguration of the integration."""
        reconfigure_entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL]
            password = user_input[CONF_PASSWORD]
            if password == PASSWORD_NOT_CHANGED:
                password = reconfigure_entry.data[CONF_PASSWORD]

            session = async_create_clientsession(
                self.hass, auto_cleanup=False, connector_owner=False
            )
            client = OceaApiClient(
                email=email,
                user_agent=UA,
                session=session,
                password=password,
                local_id=reconfigure_entry.data[CONF_LOCAL_ID],
            )

            try:
                resident = OceaResident.from_api(await client.validate_credentials())
                if not resident.dwellings:
                    errors["base"] = "no_occupation"
                elif not await self._accessible_dwellings(
                    client,
                    tuple(
                        dwelling
                        for dwelling in resident.dwellings
                        if dwelling.local_id == reconfigure_entry.data[CONF_LOCAL_ID]
                    ),
                ):
                    errors["base"] = "no_dwelling_access"
            except OceaAuthError:
                errors["base"] = "invalid_auth"
            except OceaApiError as err:
                errors["base"] = "invalid_auth" if err.status == 401 else "cannot_connect"
            except (ClientError, TimeoutError):
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                # Avoid logging exception details that could contain credentials.
                _LOGGER.error("Unexpected error during reconfiguration")
                errors["base"] = "unknown"
            else:
                if not errors:
                    local_id = reconfigure_entry.data[CONF_LOCAL_ID]
                    name = resident.name
                    title = f"Ocea - {name}" if name else f"Ocea - {local_id}"
                    unique_id = email.lower()
                    existing_entry = (
                        self.hass.config_entries.async_entry_for_domain_unique_id(
                            DOMAIN, unique_id
                        )
                    )
                    if (
                        existing_entry is not None
                        and existing_entry.entry_id != reconfigure_entry.entry_id
                    ):
                        return self.async_abort(reason="already_configured")

                    return self.async_update_reload_and_abort(
                        reconfigure_entry,
                        unique_id=unique_id,
                        title=title,
                        data_updates={
                            CONF_EMAIL: email,
                            CONF_PASSWORD: password,
                        },
                        reload_even_if_entry_is_unchanged=False,
                    )
            finally:
                session.detach()

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_EMAIL,
                        default=reconfigure_entry.data[CONF_EMAIL],
                    ): TextSelector(
                        TextSelectorConfig(
                            type=TextSelectorType.EMAIL,
                            autocomplete="email",
                        )
                    ),
                    vol.Required(
                        CONF_PASSWORD,
                        default=PASSWORD_NOT_CHANGED,
                    ): TextSelector(
                        TextSelectorConfig(
                            type=TextSelectorType.PASSWORD,
                            autocomplete="current-password",
                        )
                    ),
                }
            ),
            errors=errors,
        )
