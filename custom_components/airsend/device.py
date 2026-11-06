"""AirSend device."""
import contextlib
import logging
import time
import json
import hashlib
import re
import aiohttp
from . import DOMAIN

_LOGGER = logging.getLogger(DOMAIN)

RTYPE_LABELS = {
    0: "AirSend Box",
    1: "AirSend Sensor",
    4096: "AirSend Button",
    4097: "AirSend Switch",
    4098: "AirSend Cover",
    4099: "AirSend Cover (position)",
    4100: "AirSend Light",
}

# Device types sharing the same entity platform, and therefore the same
# unique_id namespace (4098 and 4099 are both covers).
PLATFORM_KEYS = {
    0: "box",
    1: "sensor",
    4096: "button",
    4097: "switch",
    4098: "cover",
    4099: "cover",
    4100: "light",
}


def _digest(value: str) -> str:
    """Return a short, stable digest of a string."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


from enum import Enum

# Errors meaning "the remote end could not be reached". aiohttp raises
# TimeoutError (not a ClientError) when ClientTimeout expires.
NETWORK_ERRORS = (aiohttp.ClientError, TimeoutError)


def to_bool(value, default: bool = False) -> bool:
    """Convert a YAML option (bool, int or string) to a boolean, safely."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "y"):
        return True
    if text in ("0", "false", "no", "off", "n", ""):
        return False
    _LOGGER.warning("Invalid boolean value %r, using %s", value, default)
    return default


class TransferResult(Enum):
    """Result of an async_transfer call."""
    SUCCESS = "success"          # Command sent and confirmed
    SENT = "sent"                # Command sent, no confirmation (wait=True + 500)
    SERVER_ERROR = "server_error" # Server responded with error (command may have been sent)
    NETWORK_ERROR = "network_error" # Addon unreachable (timeout, connection refused)


class Device:
    """Representation of a Device."""

    def __init__(
        self,
        name: str,
        options,
        serviceurl: str,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Initialize a device.

        ``session`` should be Home Assistant's shared client session
        (``async_get_clientsession(hass)``); without it a temporary session
        is created for each request.
        """
        self._name = name
        self._serviceurl = serviceurl
        self._session = session
        self._entity_discriminator = None
        self._uid = None
        self._rtype = None
        self._apikey = None
        self._spurl = None
        self._wait = False
        self._channel = {}
        self._note = None
        self._bind = None
        self._refresh = 5 * 60
        try:
            self._uid = options["id"]
        except KeyError:
            pass
        try:
            self._rtype = options["type"]
        except KeyError:
            pass
        if self._rtype == 0:
            self._channel = {"id": 1}
        try:
            self._apikey = options["apiKey"]
        except KeyError:
            pass
        try:
            self._spurl = options["spurl"]
        except KeyError:
            pass
        self._wait = to_bool(options.get("wait"), False)
        try:
            self._channel = options["channel"]
        except KeyError:
            pass
        try:
            self._note = options["note"]
        except KeyError:
            pass
        try:
            self._bind = int(options["bind"])
        except KeyError:
            pass
        try:
            self._refresh = int(options["refresh"])
        except KeyError:
            pass
        self._invert = to_bool(options.get("invert"), False)

        # MAC and local IP — only for AirSend Box (type 0)
        # MAC derived from EUI-64 link-local IPv6 in spurl: sp://TOKEN@[fe80::xxxx]?gw=0&rhost=192.168.x.x
        self._mac = None
        self._local_ip = None
        if self._rtype == 0 and self._spurl:
            try:
                ipv6_match = re.search(r'@\[([^\]]+)\]', self._spurl)
                if ipv6_match:
                    self._mac = self._ipv6_link_local_to_mac(ipv6_match.group(1))
                rhost_match = re.search(r'rhost=([^&]+)', self._spurl)
                if rhost_match:
                    self._local_ip = rhost_match.group(1)
            except Exception:
                pass

    @staticmethod
    def _ipv6_link_local_to_mac(ipv6_str: str) -> str:
        """Convert a fe80:: EUI-64 link-local IPv6 address to a MAC address."""
        ipv6_str = ipv6_str.lower().strip()
        if '::' in ipv6_str:
            left, right = ipv6_str.split('::')
            left_groups = left.split(':') if left else []
            right_groups = right.split(':') if right else []
            missing = 8 - len(left_groups) - len(right_groups)
            groups = left_groups + ['0'] * missing + right_groups
        else:
            groups = ipv6_str.split(':')
        groups = [g.zfill(4) for g in groups]
        eui64 = ''.join(groups[4:])
        b = [int(eui64[i:i+2], 16) for i in range(0, 16, 2)]
        mac_bytes = [b[0] ^ 0x02, b[1], b[2], b[5], b[6], b[7]]
        return ':'.join(f'{x:02X}' for x in mac_bytes)

    @property
    def name(self) -> str:
        """Return the name."""
        return self._name

    @property
    def unique_channel_name(self) -> str:
        if self._uid:
            return str(self._uid)
        if self._channel:
            result = str(self._channel['id'])
            if result:
                uniquefield = ['source', 'mac', 'seed']
                for field in uniquefield:
                    if field in self._channel:
                        result += "_"
                        result += str(self._channel[field])
            return result
        return self._name

    @property
    def entity_group_key(self) -> tuple:
        """Key shared by the entries that would derive the same unique_id."""
        return (self.unique_channel_name, PLATFORM_KEYS.get(self._rtype, self._rtype))

    @property
    def note_digest(self) -> str:
        """Return a short digest of the note block, if any."""
        return _digest(json.dumps(self._note, sort_keys=True, separators=(",", ":")))

    def set_entity_discriminator(self, value: str) -> None:
        """Set the suffix telling this entry apart from others on its channel."""
        self._entity_discriminator = value

    @property
    def unique_entity_name(self) -> str:
        """Channel name, suffixed when several entries share that channel."""
        if self._entity_discriminator:
            return self.unique_channel_name + "_" + self._entity_discriminator
        return self.unique_channel_name

    @property
    def device_info(self):
        """Return device info for Home Assistant device registry.

        Built with keyword arguments, which works both with the TypedDict
        DeviceInfo (HA < 2026.11) and the dataclass DeviceInfo (HA >= 2026.11,
        where plain mappings are going to be deprecated). Unset fields are
        left out rather than passed as None.
        """
        from homeassistant.helpers import device_registry as dr

        fields = {
            "identifiers": {(DOMAIN, self.unique_channel_name)},
            "name": self._name,
            "manufacturer": "AirSend",
            "model": RTYPE_LABELS.get(self._rtype, "AirSend"),
            "configuration_url": "https://app.airsend.cloud/",
        }
        if self._mac:
            fields["connections"] = {(dr.CONNECTION_NETWORK_MAC, self._mac)}
        if self._local_ip:
            fields["hw_version"] = f"IP: {self._local_ip}"
        return dr.DeviceInfo(**fields)

    @property
    def extra_state_attributes(self):
        if self._channel:
            return {"channel": self._channel}
        return None

    @property
    def is_async(self) -> bool:
        """Return if asynchronous state."""
        return self._wait == False

    @property
    def is_airsend(self) -> bool:
        return self._rtype == 0

    @property
    def is_sensor(self) -> bool:
        return self._rtype == 1

    @property
    def is_button(self) -> bool:
        return self._rtype == 4096

    @property
    def is_cover(self) -> bool:
        return self._rtype in (4098, 4099)

    @property
    def is_cover_with_position(self) -> bool:
        return self._rtype == 4099

    @property
    def is_switch(self) -> bool:
        return self._rtype == 4097

    @property
    def is_light(self) -> bool:
        return self._rtype == 4100

    @property
    def is_inverted(self) -> bool:
        """Return True if open/close logic is inverted."""
        return self._invert

    @property
    def refresh_value(self) -> int:
        """Return refresh value in seconds."""
        if isinstance(self._refresh, int) and self._refresh > 0:
            return self._refresh
        return 5 * 60

    @contextlib.asynccontextmanager
    async def _client(self):
        """Yield the shared HA session, or a temporary one if none was given."""
        if self._session is not None and not self._session.closed:
            yield self._session
        else:
            async with aiohttp.ClientSession() as session:
                yield session

    async def async_bind(self) -> bool:
        """Bind a channel to listen (async)."""
        if not (self._serviceurl and self._spurl and isinstance(self._bind, int) and self._bind > 0):
            return False
        payload = json.dumps({
            "channel": {"id": self._bind},
            "duration": 0,
            "callback": "http://127.0.0.1/"
        })
        headers = {
            "Authorization": "Bearer " + self._spurl,
            "content-type": "application/json",
            "User-Agent": "hass_airsend",
        }
        try:
            async with self._client() as session:
                async with session.post(
                    self._serviceurl + "airsend/bind",
                    headers=headers,
                    data=payload,
                    timeout=aiohttp.ClientTimeout(total=6),
                ) as response:
                    return response.status == 200
        except NETWORK_ERRORS as err:
            _LOGGER.debug("Bind error '%s': %s", self._name, err)
            return False

    async def async_query(self, note, entity_id: str) -> dict | None:
        """Send a synchronous local query and return the JSON response.

        Sensor queries (TEMPERATURE, ILLUMINANCE...) only return their value
        when the transfer waits for the answer, whatever the device ``wait``
        option is. Raises one of NETWORK_ERRORS if the addon is unreachable,
        returns None if it answered without a usable value.
        """
        if not (self._serviceurl and self._spurl):
            return None
        uid = hashlib.sha256(entity_id.encode("utf-8")).hexdigest()[:12]
        payload = json.dumps({
            "wait": True,
            "channel": self._channel,
            "thingnotes": {"uid": "0x" + uid, "notes": [note]},
        })
        headers = {
            "Authorization": "Bearer " + self._spurl,
            "content-type": "application/json",
            "User-Agent": "hass_airsend",
        }
        async with self._client() as session:
            async with session.post(
                self._serviceurl + "airsend/transfer",
                headers=headers,
                data=payload,
                timeout=aiohttp.ClientTimeout(total=6),
            ) as response:
                if response.status != 200:
                    _LOGGER.debug(
                        "Query '%s' %s: status %s",
                        self._name, note.get("type"), response.status,
                    )
                    return None
                try:
                    data = await response.json(content_type=None)
                except ValueError:
                    return None
        try:
            if isinstance(data, dict) and int(data.get("type", 0x100)) < 0x100:
                return data
        except (TypeError, ValueError):
            pass
        return None

    @staticmethod
    def extract_note_value(response, expected_type: int) -> float | None:
        """Extract a numeric note value of the given type from a response."""
        if not isinstance(response, dict):
            return None
        thingnotes = response.get("thingnotes")
        if not isinstance(thingnotes, dict):
            return None
        notes = thingnotes.get("notes")
        if not isinstance(notes, list):
            return None
        for note in notes:
            if not isinstance(note, dict) or "value" not in note:
                continue
            try:
                if int(note.get("type")) == expected_type:
                    return float(note["value"])
            except (TypeError, ValueError):
                continue
        return None

    async def async_probe(self) -> bool:
        """Check a command path is reachable (sends no command, no RF)."""
        if self._serviceurl and await self._probe_url(self._serviceurl):
            return True
        if self._apikey:
            # async_transfer falls back to the cloud API: probe it the same way
            return await self._probe_url("https://airsend.cloud/")
        if not self._serviceurl:
            # Nothing configured to probe: don't report unavailable
            return True
        return False

    async def _probe_url(self, url: str) -> bool:
        try:
            async with self._client() as session:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=6),
                ) as response:
                    # Any answer means the path is reachable
                    return response.status < 500
        except NETWORK_ERRORS as err:
            _LOGGER.debug("Probe error '%s': %s", self._name, repr(err))
            return False

    async def async_transfer(self, note, entity_id=None) -> "TransferResult":
        """Send a command (async)."""
        started = time.monotonic()
        status_code = 404
        ret = False
        is_command = isinstance(note, dict) and note.get("method") == 1
        wait ='false, "callback":"http://127.0.0.1/"'
        if self._wait:
            wait = 'true'

        if self._serviceurl and self._spurl and entity_id is not None:
            uid = hashlib.sha256(entity_id.encode('utf-8')).hexdigest()[:12]
            jnote = json.dumps(note)
            if (
                self._note is not None
                and all(k in self._note for k in ("method", "type", "value"))
                and all(k in note for k in ("method", "type", "value"))
                and note["method"] == 1 and note["type"] == 0
                and note["value"] in ("TOGGLE", 6)
            ):
                jnote = json.dumps(self._note)

            payload = (
                '{"wait": ' + wait + ', "channel":'
                + json.dumps(self._channel)
                + ', "thingnotes":{"uid":"0x' + uid + '", "notes":['
                + jnote
                + "]}}"
            )
            headers = {
                "Authorization": "Bearer " + self._spurl,
                "content-type": "application/json",
                "User-Agent": "hass_airsend",
            }
            try:
                async with self._client() as session:
                    async with session.post(
                        self._serviceurl + "airsend/transfer",
                        headers=headers,
                        data=payload,
                        timeout=aiohttp.ClientTimeout(total=6),
                    ) as response:
                        if self._wait:
                            ret = True
                            status_code = 500
                            try:
                                jdata = await response.json(content_type=None)
                                if jdata.get("type", 0x100) < 0x100:
                                    status_code = response.status
                            except Exception:
                                pass
                        else:
                            ret = None
                            status_code = response.status
                _LOGGER.debug(
                    "Transfer '%s' %s: local HTTP %s in %.0f ms (wait=%s)",
                    self._name, note.get("value", note.get("type")),
                    status_code, (time.monotonic() - started) * 1000, self._wait,
                )
            except NETWORK_ERRORS as err:
                _LOGGER.debug(
                    "Transfer local error '%s' after %.0f ms: %s",
                    self._name, (time.monotonic() - started) * 1000, repr(err),
                )
                if not (self._apikey and is_command):
                    return TransferResult.NETWORK_ERROR

        # Fallback to cloud API if local failed (or is unreachable).
        # Only for real commands: the cloud API has no equivalent for QUERY
        # notes and would turn them into a TOGGLE (value 6).
        if status_code != 200 and self._apikey and is_command:
            # Visible without debug logging: going through the cloud adds a
            # round trip over the internet to every command.
            _LOGGER.info(
                "Transfer '%s': local addon answered %s, falling back to the "
                "AirSend cloud API",
                self._name, status_code if self._serviceurl else "(not configured)",
            )
            cloud_started = time.monotonic()
            action = "command"
            value = 6
            if all(k in note for k in ("method", "type", "value")):
                if note["method"] == 1 and note["type"] == 0:
                    value = {
                        "OFF": 0, "ON": 1, "STOP": 3, "DOWN": 4, "UP": 5
                    }.get(note["value"], 6)
                elif note["method"] == 1 and note["type"] == 9:
                    action = "level"
                    value = int(note["value"])

            cloud_url = (
                "https://airsend.cloud/device/"
                + str(self._uid) + "/" + action + "/" + str(value) + "/"
            )
            headers = {
                "Authorization": "Bearer " + self._apikey,
                "content-type": "application/json",
                "User-Agent": "hass_airsend",
            }
            try:
                async with self._client() as session:
                    async with session.get(
                        cloud_url,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as response:
                        status_code = response.status
                        ret = True
                _LOGGER.debug(
                    "Transfer '%s': cloud HTTP %s in %.0f ms (total %.0f ms)",
                    self._name, status_code,
                    (time.monotonic() - cloud_started) * 1000,
                    (time.monotonic() - started) * 1000,
                )
            except NETWORK_ERRORS as err:
                _LOGGER.warning(
                    "Transfer cloud error '%s': %s — both local and cloud failed",
                    self._name, repr(err),
                )
                return TransferResult.NETWORK_ERROR

        if status_code == 200:
            return TransferResult.SUCCESS
        if status_code == 401:
            _LOGGER.error("Transfer '%s' : invalid locator (401) — check spurl in airsend.yaml", self.name)
            return TransferResult.SERVER_ERROR
        if status_code == 405:
            _LOGGER.error("Transfer '%s' : invalid input (405) — check channel configuration", self.name)
            return TransferResult.SERVER_ERROR
        if status_code == 500:
            if self._wait:
                _LOGGER.warning("Transfer '%s' : no RF confirmation (500), command may have been sent", self.name)
                return TransferResult.SENT
            _LOGGER.warning("Transfer '%s' : server error (500), command may have been sent", self.name)
            return TransferResult.SERVER_ERROR
        _LOGGER.warning("Transfer '%s' : unexpected status %s", self.name, status_code)
        return TransferResult.SERVER_ERROR


def build_devices(
    devices_config: dict,
    serviceurl: str,
    session: aiohttp.ClientSession | None = None,
) -> list["Device"]:
    """Build the devices of a config entry, disambiguating shared channels.

    Several entries may legitimately target the same RF channel with a
    different ``note``, e.g. the positions of a multi-button remote. They only
    differ by that note, so they would all derive the same unique_id and Home
    Assistant would keep the first one and drop the others. Give every member
    of such a group a discriminator; lone entries are left untouched so their
    existing unique_id is preserved.
    """
    devices = [
        Device(name, options, serviceurl, session)
        for name, options in devices_config.items()
    ]

    groups: dict[tuple, list[Device]] = {}
    for device in devices:
        groups.setdefault(device.entity_group_key, []).append(device)

    for group in groups.values():
        if len(group) < 2:
            continue
        digests = [device.note_digest for device in group]
        for device, digest in zip(group, digests):
            if digests.count(digest) > 1:
                # Same channel and same note: only the YAML key tells them
                # apart, and it is unique by construction.
                digest = _digest(device.name)
            device.set_entity_discriminator(digest)

    return devices
