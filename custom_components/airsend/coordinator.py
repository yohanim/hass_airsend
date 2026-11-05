"""AirSend DataUpdateCoordinator."""
import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .device import Device, TransferResult, NETWORK_ERRORS
from . import DOMAIN

_LOGGER = logging.getLogger(DOMAIN)

# AirSend note types (see the addon's convertNotesToStates)
NOTE_TYPE_TEMPERATURE = 2   # Kelvin
NOTE_TYPE_ILLUMINANCE = 3   # lux

# Devices that only push state (covers) are probed for reachability:
# retry quickly while the bridge looks down so entities recover on their own.
_AVAILABILITY_RETRY_SECONDS = 30
_AVAILABILITY_FAILURE_THRESHOLD = 2


class AirSendCoordinator(DataUpdateCoordinator):
    """Coordinator for a single AirSend device.

    - AirSend box (type 0): polls state, temperature and illuminance.
    - Covers: probe reachability so they recover from transient failures.
    """

    def __init__(self, hass: HomeAssistant, device: Device) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{device.unique_entity_name}",
            update_interval=timedelta(seconds=device.refresh_value),
        )
        self._device = device
        self._sensors_enabled = False
        self._availability_failures = 0
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
        if self._device.is_airsend:
            await self._async_update_box(data)
        elif self._device.is_cover:
            await self._async_probe_cover(data)
        return data

    async def _async_update_box(self, data: dict) -> None:
        """Poll an AirSend box: reachability, bind, then sensors."""
        # Query state: tells whether the box (through the addon) is reachable
        result = await self._device.async_transfer(
            {"method": "QUERY", "type": "STATE"},
            f"coordinator_{self._device.unique_entity_name}",
        )
        data["available"] = result != TransferResult.NETWORK_ERROR
        if not data["available"]:
            _LOGGER.debug("AirSend box '%s' is unreachable", self._device.name)
            return

        await self._device.async_bind()

        if self._sensors_enabled:
            await self._async_update_sensors(data)

    async def _async_update_sensors(self, data: dict) -> None:
        """Query temperature and illuminance (synchronous transfers)."""
        uid = self._device.unique_entity_name
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

    async def _async_probe_cover(self, data: dict) -> None:
        """Probe reachability so covers (push only) recover on their own."""
        if await self._device.async_probe():
            data["available"] = True
            self._availability_failures = 0
            self.update_interval = timedelta(seconds=self._device.refresh_value)
        else:
            self._availability_failures += 1
            if self._availability_failures >= _AVAILABILITY_FAILURE_THRESHOLD:
                data["available"] = False
            self.update_interval = timedelta(seconds=_AVAILABILITY_RETRY_SECONDS)

    @callback
    def async_set_available(self, available: bool) -> None:
        """Update availability reported by entities after a transfer."""
        if available:
            self._availability_failures = 0
        if self.data.get("available", True) == available:
            return
        self.data["available"] = available
        self.async_update_listeners()

    @property
    def _has_sensors(self) -> bool:
        return self._sensors_enabled

    def set_has_sensors(self, value: bool) -> None:
        """Called at setup when the box sensors are enabled."""
        self._sensors_enabled = bool(value)
