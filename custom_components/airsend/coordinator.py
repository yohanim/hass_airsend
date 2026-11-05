"""AirSend DataUpdateCoordinator."""
import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .device import Device, TransferResult, NETWORK_ERRORS
from . import DOMAIN

_LOGGER = logging.getLogger(DOMAIN)

# AirSend note types (see the addon's convertNotesToStates)
NOTE_TYPE_TEMPERATURE = 2   # Kelvin
NOTE_TYPE_ILLUMINANCE = 3   # lux


class AirSendCoordinator(DataUpdateCoordinator):
    """Coordinator for a single AirSend device — polls state, temp and illuminance."""

    def __init__(self, hass: HomeAssistant, device: Device) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{device.unique_channel_name}",
            update_interval=timedelta(seconds=device.refresh_value),
        )
        self._device = device
        self._sensors_enabled = False
        # Data structure shared across all entities of this device
        self.data = {
            "state": None,
            "temperature": None,
            "illuminance": None,
            "available": True,
        }

    @property
    def device(self) -> Device:
        return self._device

    async def _async_update_data(self) -> dict:
        """Fetch data from device — called automatically by the coordinator."""
        data = dict(self.data)  # keep last known values
        if not self._device.is_airsend:
            return data

        # Query state: tells whether the box (through the addon) is reachable
        result = await self._device.async_transfer(
            {"method": "QUERY", "type": "STATE"},
            f"coordinator_{self._device.unique_channel_name}",
        )
        data["available"] = result != TransferResult.NETWORK_ERROR
        if not data["available"]:
            _LOGGER.debug("AirSend box '%s' is unreachable", self._device.name)
            return data

        await self._device.async_bind()

        if self._sensors_enabled:
            await self._async_update_sensors(data)

        return data

    async def _async_update_sensors(self, data: dict) -> None:
        """Query temperature and illuminance (synchronous transfers)."""
        uid = self._device.unique_channel_name
        try:
            response = await self._device.async_query(
                {"method": "QUERY", "type": "TEMPERATURE"},
                f"coordinator_{uid}_temp",
            )
            kelvin = Device.extract_note_value(response, NOTE_TYPE_TEMPERATURE)
            if kelvin is not None:
                data["temperature"] = round(kelvin - 273.15, 1)

            response = await self._device.async_query(
                {"method": "QUERY", "type": "ILLUMINANCE"},
                f"coordinator_{uid}_ill",
            )
            lux = Device.extract_note_value(response, NOTE_TYPE_ILLUMINANCE)
            if lux is not None:
                data["illuminance"] = lux
        except NETWORK_ERRORS as err:
            _LOGGER.warning(
                "Sensor query failed for %s: %s", self._device.name, repr(err)
            )

    @property
    def _has_sensors(self) -> bool:
        return self._sensors_enabled

    def set_has_sensors(self, value: bool) -> None:
        """Called by the sensor platform when the box sensors are enabled."""
        self._sensors_enabled = bool(value)
