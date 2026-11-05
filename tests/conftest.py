"""Fixtures for the AirSend tests."""
import json
import re

import pytest

try:  # Home Assistant >= 2026.11: official test package
    from pytest_homeassistant.common import MockConfigEntry
    from pytest_homeassistant.test_util.aiohttp import AiohttpClientMockResponse
except ImportError:  # older Home Assistant: community package
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from pytest_homeassistant_custom_component.test_util.aiohttp import (
        AiohttpClientMockResponse,
    )

from custom_components.airsend import DOMAIN

ADDON_URL = "http://addon.test:33863/"
SPURL = "sp://token@[fe80::0211:22ff:fe33:4455]?gw=0&rhost=192.168.1.50"

# Kelvin value returned by the box for 21.5 °C
TEMPERATURE_K = 294.65
ILLUMINANCE_LUX = 123.0

DEVICES = {
    "AirSend box": {"type": 0, "spurl": SPURL, "sensors": True, "bind": 1},
    "Volet salon": {
        "id": 12345,
        "type": 4098,
        "apiKey": "cloudkey",
        "spurl": SPURL,
        "channel": {"id": 25455, "source": 94311},
    },
    "Volet bureau": {
        "id": 12346,
        "type": 4099,
        "spurl": SPURL,
        "invert": "true",
        "channel": {"id": 25456, "source": 94312},
    },
    "Prise": {
        "id": 22222,
        "type": 4097,
        "spurl": SPURL,
        "wait": "yes",  # used to crash with eval()
        "channel": {"id": 3000, "source": 1},
    },
    "Portail": {
        "id": 33333,
        "type": 4096,
        "spurl": SPURL,
        "channel": {"id": 4000, "source": 2},
    },
    "Lumiere pergola": {
        "id": 44444,
        "type": 4100,
        "spurl": SPURL,
        "wait": True,
        "channel": {"id": 5000, "source": 3},
    },
    "Capteur": {"type": 1, "spurl": SPURL, "channel": {"id": 6000, "source": 4}},
}

# unique_ids as created by every released version: they must never change,
# otherwise users get duplicated "_2" entities (devmel/hass_airsend#32).
EXPECTED_UNIQUE_IDS = {
    "binary_sensor": {"airsend_1_state"},
    "sensor": {"airsend_1_temp", "airsend_1_ill", "airsend_6000_4_sensor"},
    "cover": {"airsend_12345_cover", "airsend_12346_cover"},
    "switch": {"airsend_22222_switch"},
    "button": {"airsend_33333_button"},
    "light": {"airsend_44444_light"},
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading custom integrations in all tests."""
    yield


def make_entry(devices=None, url=ADDON_URL) -> MockConfigEntry:
    """Build a config entry like the config flow does."""
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        title="AirSend",
        data={"internal_url": url, "devices": devices or DEVICES},
    )


def transfer_side_effect(state: dict):
    """Answer /airsend/transfer like the AirSend web service does.

    ``state`` lets tests switch the addon behaviour and inspect payloads:
      - state["down"]: raise TimeoutError (addon not answering)
      - state["payloads"]: list of decoded request payloads
    """

    async def _side_effect(method, url, data):
        if state.get("down"):
            raise TimeoutError
        payload = json.loads(data)
        state.setdefault("payloads", []).append(payload)
        note = payload["thingnotes"]["notes"][0]
        body = {"type": 1}
        if payload.get("wait") is True and note.get("method") == "QUERY":
            value = {
                "TEMPERATURE": (2, TEMPERATURE_K),
                "ILLUMINANCE": (3, ILLUMINANCE_LUX),
            }.get(note.get("type"))
            if value is not None:
                body["thingnotes"] = {
                    "uid": payload["thingnotes"]["uid"],
                    "notes": [{"method": 1, "type": value[0], "value": value[1]}],
                }
        return AiohttpClientMockResponse(
            method, url, json=body, status=200
        )

    return _side_effect


@pytest.fixture
def addon(aioclient_mock):
    """Mock the AirSend addon (and the cloud API)."""
    state: dict = {"cloud_calls": []}

    async def _probe(method, url, data):
        if state.get("down"):
            raise TimeoutError
        return AiohttpClientMockResponse(method, url, status=404)

    async def _cloud(method, url, data):
        state["cloud_calls"].append(str(url))
        if state.get("cloud_down"):
            raise TimeoutError
        return AiohttpClientMockResponse(method, url, status=200)

    aioclient_mock.post(ADDON_URL + "airsend/transfer", side_effect=transfer_side_effect(state))
    aioclient_mock.post(ADDON_URL + "airsend/bind", status=200)
    aioclient_mock.get(ADDON_URL, side_effect=_probe)
    aioclient_mock.get(re.compile(r"^https://airsend\.cloud/"), side_effect=_cloud)
    return state
