"""Config flow for the PSEG Long Island integration."""

from __future__ import annotations

import contextlib
import logging
from typing import Any, AsyncIterator, Mapping

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import PsegliClient, normalize_cookie
from .const import (
    AUTH_MODE_COOKIE,
    AUTH_MODE_PASSWORD,
    CONF_AUTH_MODE,
    CONF_BACKFILL_DAYS,
    CONF_BILLING_DAY,
    CONF_COOKIE,
    CONF_DAILY_SERVICE_CHARGE,
    CONF_DELIVERY_RATE,
    CONF_EMAIL,
    CONF_KEEP_ALIVE_MINUTES,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PEAK_DELIVERY_RATE,
    CONF_PEAK_END_HOUR,
    CONF_PEAK_START_HOUR,
    CONF_PEAK_SUPPLY_MULTIPLIER,
    CONF_PEAK_WEEKDAYS_ONLY,
    CONF_RATE_MODE,
    CONF_SUPPLY_RATE,
    CONF_TIMESTAMP_OFFSET,
    CONF_WEEK_START,
    DEFAULT_TARIFF,
    DOMAIN,
    KEEP_ALIVE_MINUTES,
    NAME,
    RATE_MODE_FLAT,
    RATE_MODE_TOU,
    WEEK_START_MONDAY,
    WEEK_START_SUNDAY,
)
from .exceptions import PsegliAuthError, PsegliError, PsegliLoginRequired

_LOGGER = logging.getLogger(__name__)


def _cookie_selector() -> selector.TextSelector:
    return selector.TextSelector(
        selector.TextSelectorConfig(
            type=selector.TextSelectorType.PASSWORD, multiline=True
        )
    )


def _password_selector() -> selector.TextSelector:
    return selector.TextSelector(
        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
    )


def _credentials_schema(current: Mapping[str, Any]) -> vol.Schema:
    """Fields shared by initial setup and re-authentication."""
    return vol.Schema(
        {
            vol.Required(
                CONF_AUTH_MODE,
                default=current.get(CONF_AUTH_MODE, AUTH_MODE_PASSWORD),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[AUTH_MODE_PASSWORD, AUTH_MODE_COOKIE],
                    mode=selector.SelectSelectorMode.LIST,
                    translation_key="auth_mode",
                )
            ),
            vol.Optional(
                CONF_EMAIL, default=str(current.get(CONF_EMAIL) or "")
            ): str,
            vol.Optional(CONF_PASSWORD): _password_selector(),
            vol.Optional(
                CONF_COOKIE, default=str(current.get(CONF_COOKIE) or "")
            ): _cookie_selector(),
        }
    )


def _user_schema(current: Mapping[str, Any] | None = None) -> vol.Schema:
    current = current or {}
    schema = dict(_credentials_schema(current).schema)
    schema[vol.Optional(CONF_NAME, default=str(current.get(CONF_NAME) or NAME))] = str
    return vol.Schema(schema)


def _reauth_schema(current: Mapping[str, Any] | None = None) -> vol.Schema:
    return _credentials_schema(current or {})


def _reauth_defaults(entry_data: Mapping[str, Any]) -> dict[str, Any]:
    """Pre-select the sign-in method the entry already uses."""
    current = dict(entry_data)
    if CONF_AUTH_MODE not in current:
        current[CONF_AUTH_MODE] = (
            AUTH_MODE_PASSWORD if current.get(CONF_PASSWORD) else AUTH_MODE_COOKIE
        )
    return current


def _number(minimum: float, maximum: float, step: float, unit: str | None = None):
    config = {
        "min": minimum,
        "max": maximum,
        "step": step,
        "mode": selector.NumberSelectorMode.BOX,
    }
    if unit:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(selector.NumberSelectorConfig(**config))


def _options_schema(options: Mapping[str, Any]) -> vol.Schema:
    def value(key: str) -> Any:
        return options.get(key, DEFAULT_TARIFF[key])

    return vol.Schema(
        {
            vol.Required(CONF_RATE_MODE, default=value(CONF_RATE_MODE)): (
                selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[RATE_MODE_FLAT, RATE_MODE_TOU],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                )
            ),
            vol.Required(CONF_DELIVERY_RATE, default=value(CONF_DELIVERY_RATE)): _number(
                0, 5, 0.0001, "USD/kWh"
            ),
            vol.Required(CONF_SUPPLY_RATE, default=value(CONF_SUPPLY_RATE)): _number(
                0, 5, 0.0001, "USD/kWh"
            ),
            vol.Required(
                CONF_PEAK_DELIVERY_RATE, default=value(CONF_PEAK_DELIVERY_RATE)
            ): _number(0, 5, 0.0001, "USD/kWh"),
            vol.Required(
                CONF_PEAK_SUPPLY_MULTIPLIER, default=value(CONF_PEAK_SUPPLY_MULTIPLIER)
            ): _number(0, 5, 0.01),
            vol.Required(
                CONF_DAILY_SERVICE_CHARGE, default=value(CONF_DAILY_SERVICE_CHARGE)
            ): _number(0, 20, 0.01, "USD/day"),
            vol.Required(CONF_PEAK_START_HOUR, default=value(CONF_PEAK_START_HOUR)): (
                _number(0, 23, 1, "h")
            ),
            vol.Required(CONF_PEAK_END_HOUR, default=value(CONF_PEAK_END_HOUR)): _number(
                0, 23, 1, "h"
            ),
            vol.Required(
                CONF_PEAK_WEEKDAYS_ONLY, default=value(CONF_PEAK_WEEKDAYS_ONLY)
            ): selector.BooleanSelector(),
            vol.Required(CONF_WEEK_START, default=value(CONF_WEEK_START)): (
                selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[WEEK_START_MONDAY, WEEK_START_SUNDAY],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                )
            ),
            vol.Required(CONF_BILLING_DAY, default=value(CONF_BILLING_DAY)): _number(
                1, 28, 1
            ),
            vol.Required(CONF_BACKFILL_DAYS, default=value(CONF_BACKFILL_DAYS)): _number(
                0, 400, 1, "days"
            ),
            vol.Required(
                CONF_TIMESTAMP_OFFSET, default=value(CONF_TIMESTAMP_OFFSET)
            ): _number(-720, 720, 15, "min"),
            vol.Required(
                CONF_KEEP_ALIVE_MINUTES,
                default=options.get(CONF_KEEP_ALIVE_MINUTES, KEEP_ALIVE_MINUTES),
            ): _number(0, 60, 5, "min"),
        }
    )


@contextlib.asynccontextmanager
async def _async_client(
    hass: HomeAssistant,
    cookie: str = "",
    *,
    email: str | None = None,
    password: str | None = None,
) -> AsyncIterator[PsegliClient]:
    """Build a throwaway client with its own cookie jar."""
    session = async_create_clientsession(
        hass, auto_cleanup=False, cookie_jar=aiohttp.CookieJar(unsafe=False)
    )
    try:
        yield PsegliClient(session, cookie, email=email, password=password)
    finally:
        await session.close()


async def _async_validate_cookie(hass: HomeAssistant, cookie: str) -> None:
    async with _async_client(hass, cookie) as client:
        await client.async_validate()


async def _async_password_login(
    hass: HomeAssistant, email: str, password: str
) -> str:
    """Sign in and return the resulting session cookie."""
    async with _async_client(hass, email=email, password=password) as client:
        await client.async_login()
        return client.current_cookie()


class PsegliConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle initial setup and re-authentication."""

    VERSION = 1

    async def _async_resolve_auth(
        self, user_input: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Verify the chosen sign-in method and build config entry data."""
        auth_mode = str(user_input.get(CONF_AUTH_MODE) or AUTH_MODE_PASSWORD)
        email = str(user_input.get(CONF_EMAIL) or "").strip()
        password = str(user_input.get(CONF_PASSWORD) or "")
        cookie = normalize_cookie(str(user_input.get(CONF_COOKIE) or ""))
        data: dict[str, Any] = {CONF_AUTH_MODE: auth_mode}

        try:
            if auth_mode == AUTH_MODE_PASSWORD:
                if not email or not password:
                    return data, {"base": "credentials_required"}
                cookie = await _async_password_login(self.hass, email, password)
                data.update(
                    {
                        CONF_EMAIL: email,
                        CONF_PASSWORD: password,
                        CONF_COOKIE: cookie,
                    }
                )
            else:
                if not cookie:
                    return data, {"base": "cookie_required"}
                await _async_validate_cookie(self.hass, cookie)
                data[CONF_COOKIE] = cookie
        except PsegliLoginRequired as err:
            _LOGGER.warning("PSEG sign-in needs a human: %s", err)
            return data, {"base": "verification_required"}
        except PsegliAuthError:
            return data, {"base": "invalid_auth"}
        except PsegliError:
            return data, {"base": "cannot_connect"}
        except Exception:  # noqa: BLE001 - surfaced as a generic form error
            _LOGGER.exception("Unexpected error talking to PSEG")
            return data, {"base": "unknown"}

        return data, {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            data, errors = await self._async_resolve_auth(user_input)
            if not errors:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                title = str(user_input.get(CONF_NAME) or NAME)
                data[CONF_NAME] = title
                return self.async_create_entry(title=title, data=data)

        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(user_input),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start re-authentication when the session expires."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is None:
            # A stored username and password usually fix this without the user
            # doing anything at all.
            if entry.data.get(CONF_EMAIL) and entry.data.get(CONF_PASSWORD):
                data, errors = await self._async_resolve_auth(
                    {
                        CONF_AUTH_MODE: AUTH_MODE_PASSWORD,
                        CONF_EMAIL: entry.data.get(CONF_EMAIL),
                        CONF_PASSWORD: entry.data.get(CONF_PASSWORD),
                    }
                )
                if not errors:
                    return self._async_store_reauth(entry, data)
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=_reauth_schema(_reauth_defaults(entry.data)),
                errors=errors,
            )

        data, errors = await self._async_resolve_auth(user_input)
        if not errors:
            return self._async_store_reauth(entry, data)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_reauth_schema(
                {**_reauth_defaults(entry.data), **user_input}
            ),
            errors=errors,
        )

    def _async_store_reauth(
        self, entry: ConfigEntry, data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Replace the stored credentials, dropping the abandoned method."""
        new_data = {
            key: value
            for key, value in entry.data.items()
            if key not in (CONF_EMAIL, CONF_PASSWORD, CONF_COOKIE, CONF_AUTH_MODE)
        }
        new_data.update(data)
        return self.async_update_reload_and_abort(entry, data=new_data)

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return PsegliOptionsFlow()


class PsegliOptionsFlow(OptionsFlow):
    """Edit the tariff used to estimate cost."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = {**DEFAULT_TARIFF, **self.config_entry.options}
        return self.async_show_form(
            step_id="init", data_schema=_options_schema(options)
        )
