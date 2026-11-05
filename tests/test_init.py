"""Tests for the AirSend integration setup and entities."""
from datetime import timedelta

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.airsend import DOMAIN
from custom_components.airsend.device import Device, TransferResult, to_bool

from .conftest import (
    ADDON_URL,
    DEVICES,
    EXPECTED_UNIQUE_IDS,
    SPURL,
    make_entry,
)


async def _setup(hass: HomeAssistant, devices=None):
    entry = make_entry(devices)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _entity_id(hass: HomeAssistant, platform: str, unique_id: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)
    assert entity_id is not None, f"{platform} {unique_id} not registered"
    return entity_id


async def test_setup_and_unload(hass: HomeAssistant, addon) -> None:
    """All entities are created with their historical unique_ids."""
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.LOADED

    registry = er.async_get(hass)
    found: dict[str, set[str]] = {}
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        found.setdefault(reg_entry.domain, set()).add(reg_entry.unique_id)
    assert found == EXPECTED_UNIQUE_IDS

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


def _device(hass: HomeAssistant, entry, identifier: str):
    """Find a device of the entry by identifier (works on all HA versions)."""
    for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id):
        if (DOMAIN, identifier) in device.identifiers:
            return device
    return None


async def test_device_registry(hass: HomeAssistant, addon) -> None:
    """Devices are registered with model, MAC and IP (DeviceInfo kwargs)."""
    entry = await _setup(hass)
    device = _device(hass, entry, "1")
    assert device is not None
    assert device.name == "AirSend box"
    assert device.manufacturer == "AirSend"
    assert device.model == "AirSend Box"
    assert device.hw_version == "IP: 192.168.1.50"
    assert (dr.CONNECTION_NETWORK_MAC, "00:11:22:33:44:55") in device.connections

    cover = _device(hass, entry, "12345")
    assert cover is not None
    assert cover.model == "AirSend Cover"
    assert cover.connections == set()


async def test_box_sensors_have_values(hass: HomeAssistant, addon) -> None:
    """Temperature and illuminance come from synchronous queries (addon #11)."""
    await _setup(hass)
    temp = hass.states.get(_entity_id(hass, "sensor", "airsend_1_temp"))
    ill = hass.states.get(_entity_id(hass, "sensor", "airsend_1_ill"))
    assert temp.state == "21.5"
    assert float(ill.state) == 123.0

    # Sensor queries wait for the answer, whatever the device "wait" option
    queries = [
        p for p in addon["payloads"]
        if p["thingnotes"]["notes"][0].get("type") in ("TEMPERATURE", "ILLUMINANCE")
    ]
    assert len(queries) == 2
    assert all(p["wait"] is True for p in queries)

    running = hass.states.get(_entity_id(hass, "binary_sensor", "airsend_1_state"))
    assert running.state == STATE_ON


async def test_box_unreachable(hass: HomeAssistant, addon) -> None:
    """An unreachable box turns "running" off; sensors become unavailable."""
    addon["down"] = True
    await _setup(hass)

    running = hass.states.get(_entity_id(hass, "binary_sensor", "airsend_1_state"))
    assert running.state == STATE_OFF
    temp = hass.states.get(_entity_id(hass, "sensor", "airsend_1_temp"))
    assert temp.state == STATE_UNAVAILABLE
    # QUERY notes are never sent to the cloud API (it would turn them into TOGGLE)
    assert not any("/device/None/" in c for c in addon["cloud_calls"])

    # Box comes back: next poll recovers
    addon["down"] = False
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=301))
    await hass.async_block_till_done()
    running = hass.states.get(_entity_id(hass, "binary_sensor", "airsend_1_state"))
    assert running.state == STATE_ON


async def test_cover_timeout_falls_back_to_cloud(hass: HomeAssistant, addon) -> None:
    """A local timeout no longer raises and the cloud API is tried."""
    await _setup(hass)
    entity_id = _entity_id(hass, "cover", "airsend_12345_cover")
    addon["down"] = True

    await hass.services.async_call(
        "cover", "open_cover", {"entity_id": entity_id}, blocking=True
    )
    assert "https://airsend.cloud/device/12345/command/5/" in addon["cloud_calls"]
    # The cloud answered: the cover stays available. Its open/closed state is
    # pushed by the addon callback for asynchronous ("wait: false") devices.
    assert hass.states.get(entity_id).state != STATE_UNAVAILABLE


async def test_cover_timeout_without_cloud(hass: HomeAssistant, addon) -> None:
    """Without cloud API key, a timeout marks the cover unavailable, no exception."""
    await _setup(hass)
    entity_id = _entity_id(hass, "cover", "airsend_12346_cover")
    addon["down"] = True

    await hass.services.async_call(
        "cover", "close_cover", {"entity_id": entity_id}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE


async def test_button_unavailable_on_network_error(hass: HomeAssistant, addon) -> None:
    """Button availability follows the transfer result."""
    await _setup(hass)
    entity_id = _entity_id(hass, "button", "airsend_33333_button")
    addon["down"] = True
    await hass.services.async_call(
        "button", "press", {"entity_id": entity_id}, blocking=True
    )
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE


async def test_switch_and_light(hass: HomeAssistant, addon) -> None:
    """Commands reach the addon and update the state."""
    await _setup(hass)
    switch_id = _entity_id(hass, "switch", "airsend_22222_switch")
    light_id = _entity_id(hass, "light", "airsend_44444_light")

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": switch_id}, blocking=True
    )
    assert hass.states.get(switch_id).state == STATE_ON

    await hass.services.async_call(
        "light", "turn_on", {"entity_id": light_id, "brightness": 128}, blocking=True
    )
    state = hass.states.get(light_id)
    assert state.state == STATE_ON
    assert state.attributes["brightness"] == 128
    level_notes = [
        p["thingnotes"]["notes"][0] for p in addon["payloads"]
        if p["thingnotes"]["notes"][0].get("type") == 9
    ]
    assert level_notes[-1]["value"] == 50


async def test_generic_sensor_without_restore(hass: HomeAssistant, addon) -> None:
    """The type 1 sensor no longer crashes when there is no state to restore."""
    await _setup(hass)
    state = hass.states.get(_entity_id(hass, "sensor", "airsend_6000_4_sensor"))
    assert state is not None
    assert state.state == STATE_UNKNOWN


async def test_shared_channel_buttons(hass: HomeAssistant, addon) -> None:
    """Several commands on one RF channel give several entities (#39 / #40)."""
    devices = {
        "Fermer pergola": {
            "type": 4096, "spurl": SPURL,
            "channel": {"id": 25712, "source": 45409},
            "note": {"method": 1, "type": 0, "value": 18},
        },
        "Ouvrir pergola": {
            "type": 4096, "spurl": SPURL,
            "channel": {"id": 25712, "source": 45409},
            "note": {"method": 1, "type": 1, "value": 4, "value_binsize": 3},
        },
    }
    entry = await _setup(hass, devices)
    buttons = [
        e for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if e.domain == "button"
    ]
    assert len(buttons) == 2


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True), (False, False), (None, False), (1, True), (0, False),
        ("true", True), ("True", True), ("yes", True), ("on", True),
        ("false", False), ("no", False), ("off", False), ("garbage", False),
    ],
)
def test_to_bool(value, expected) -> None:
    """Options are parsed without eval()."""
    assert to_bool(value) is expected


def test_device_options() -> None:
    """String booleans are honoured (invert: "false" used to mean True)."""
    device = Device("x", {"type": 4098, "invert": "false", "wait": "yes"}, ADDON_URL)
    assert device.is_inverted is False
    assert device.is_async is False


async def test_transfer_unreachable_returns_network_error(hass: HomeAssistant, aioclient_mock) -> None:
    """TimeoutError is reported as NETWORK_ERROR instead of raising."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    aioclient_mock.post(ADDON_URL + "airsend/transfer", exc=TimeoutError())
    device = Device("x", DEVICES["Prise"], ADDON_URL, async_get_clientsession(hass))
    result = await device.async_transfer({"method": 1, "type": 0, "value": "ON"}, "switch.x")
    assert result is TransferResult.NETWORK_ERROR
