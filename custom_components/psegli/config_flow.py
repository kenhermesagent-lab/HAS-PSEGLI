"""Config flow for the PSEG Long Island integration."""

from __future__ import annotations

import logging
from typing import Any, Mapping

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import PsegliClient
from .const import (
    CONF_BACKFILL_DAYS,
    CONF_BILLING_DAY,
    CONF_COOKIE,
    CONF_DAILY_SERVICE_CHARGE,
    CONF_DELIVERY_RATE,
    CONF_NAME,
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
    NAME,
    RATE_MODE_FLAT,
    RATE_MODE_TOU,
    WEEK_START_MONDAY,
    WEEK_START_SUNDAY,
)
from .exceptions import PsegliAuthError, PsegliError

_LOGGER = logging.getLogger(__name__)


def _cookie_selector() -> selector.TextSelector:
    return selector.TextSelector(
        selector.TextSelectorConfig(
            type=selector.TextSelectorType.PASSWORD, multiline=True
        )
    )


def _user_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_COOKIE): _cookie_selector(),
            vol.Optional(CONF_NAME, default=NAME): str,
        }
    )


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
        }
    )


async def _async_validate_cookie(hass: HomeAssistant, cookie: str) -> None:
    client = PsegliClient(async_get_clientsession(hass), cookie)
    await client.async_validate()


class PsegliConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle initial setup and re-authentication."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                await _async_validate_cookie(self.hass, user_input[CONF_COOKIE])
            except PsegliAuthError:
                errors["base"] = "invalid_auth"
            except PsegliError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error validating the PSEG cookie")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                title = user_input.get(CONF_NAME) or NAME
                return self.async_create_entry(
                    title=title,
                    data={CONF_COOKIE: user_input[CONF_COOKIE], CONF_NAME: title},
                )

        return self.async_show_form(
            step_id="user", data_schema=_user_schema(), errors=errors
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start re-authentication when the cookie expires."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                await _async_validate_cookie(self.hass, user_input[CONF_COOKIE])
            except PsegliAuthError:
                errors["base"] = "invalid_auth"
            except PsegliError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error validating the PSEG cookie")
                errors["base"] = "unknown"
            else:
                entry = self._get_reauth_entry()
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_COOKIE: user_input[CONF_COOKIE]}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_COOKIE): _cookie_selector()}),
            errors=errors,
        )

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
