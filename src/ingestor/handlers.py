"""
Map decoded MeshCore payload objects to potato-mesh API calls.
"""

import hashlib
import struct
import time

import structlog

from .config import settings
from .potato_client import PotatoClient

log = structlog.get_logger()

# In-memory cache: lowercased display name → canonical node_id (!deadbeef)
# Populated from Advert packets; used to resolve GroupText senders.
_name_to_node_id: dict[str, str] = {}


def _meshcore_synthetic_node_id(name: str) -> str:
    """Deterministic synthetic node id: '!' + SHA256(trimmed_name)[0:8 hex chars].
    Matches potato-mesh meshcore_synthetic_node_id() in meshcore_chat.rb."""
    return "!" + hashlib.sha256(name.strip().encode()).hexdigest()[:8]


def _derive_msg_id(channel_hash: str, from_id: str, text: str) -> int:
    """Deterministic 32-bit int message id from content hash.
    Same physical message from multiple repeaters gets the same id → PK dedup."""
    h = hashlib.sha256(f"{channel_hash}|{from_id}|{text}".encode()).digest()
    return struct.unpack(">I", h[:4])[0]


def _node_id(public_key: str) -> str:
    """First 4 bytes (8 hex chars) of public key, lowercased, prefixed with !"""
    return f"!{public_key[:8].lower()}"


def _modem_preset(radio_str: str) -> str | None:
    """Parse 'freq,bw,sf,cr' string into 'SFx/BWy/CRz' preset label."""
    if not radio_str:
        return None
    parts = radio_str.split(",")
    if len(parts) < 4:
        return None
    try:
        bw = float(parts[1])
        sf = int(parts[2])
        cr = int(parts[3])
        bw_str = str(int(bw)) if bw == int(bw) else str(bw)
        return f"SF{sf}/BW{bw_str}/CR{cr}"
    except (ValueError, IndexError):
        return None


# meshcoredecoder DeviceRole → potato-mesh role
DEVICE_ROLE_MAP = {
    1: "COMPANION",   # ChatNode
    2: "REPEATER",
    3: "ROOM_SERVER",
    4: "SENSOR",
}


async def handle_status(payload: dict, client: PotatoClient) -> None:  # noqa: C901
    """Repeater's own status → node + telemetry."""
    origin_id = payload.get("origin_id", "")
    if not origin_id:
        return

    node_id = _node_id(origin_id)
    stats = payload.get("stats", {})
    now = int(time.time())

    radio_raw = payload.get("radio", "")
    lora_freq = None
    modem_preset = None
    if isinstance(radio_raw, str) and radio_raw:
        try:
            lora_freq = float(radio_raw.split(",")[0])
        except (ValueError, IndexError):
            pass
        modem_preset = _modem_preset(radio_raw)
    elif isinstance(radio_raw, dict):
        lora_freq = radio_raw.get("frequency")

    battery_mv = stats.get("battery_mv")
    voltage = (battery_mv / 1000.0) if battery_mv is not None else stats.get("battery_voltage")
    uptime = stats.get("uptime_secs") or stats.get("uptime")

    await client.set_repeater_node_id(node_id)
    await client.set_repeater_lora_info(lora_freq, modem_preset)

    long_name = payload.get("origin", "")
    short_name = long_name[:4] if long_name else ""

    await client.send_node(node_id, {
        "user": {"role": "REPEATER", "longName": long_name, "shortName": short_name},
        "deviceMetrics": {"voltage": voltage, "uptimeSeconds": uptime},
        "lastHeard": now,
        "lora_freq": lora_freq,
        "modem_preset": modem_preset,
    })

    if voltage is not None:
        await client.send_telemetry({
            "from_id": node_id,
            "rx_time": now,
            "decoded": {
                "portnum": "TELEMETRY_APP",
                "telemetry": {
                    "time": now,
                    "deviceMetrics": {"voltage": voltage, "uptimeSeconds": uptime},
                },
            },
        })


async def handle_advert(advert, rx_time: int, snr: float, rssi: int, client: PotatoClient) -> None:
    """AdvertPayload → node info + position."""
    if advert is None or not advert.is_valid:
        return

    public_key = advert.public_key or ""
    app_data = advert.app_data or {}

    role_int = getattr(app_data.get("device_role"), "value", 1)
    role = DEVICE_ROLE_MAP.get(role_int, "COMPANION")
    name = app_data.get("name", "")
    node_id = _node_id(public_key) if public_key else "!unknown"

    log.info("advert", node_id=node_id, name=name, role=role, snr=snr, rssi=rssi,
             pubkey_prefix=public_key[:16] if public_key else None)

    short_name = app_data.get("short_name", "") or (name[:4] if name else "")

    # Cache name → node_id so GroupText sender lookup works
    if name and node_id != "!unknown":
        _name_to_node_id[name.lower()] = node_id

    node_data: dict = {
        "user": {"role": role, "longName": name, "shortName": short_name, "publicKey": public_key},
        "lastHeard": rx_time,
        "snr": snr,
        "rssi": rssi,
        "lora_freq": client.lora_freq,
        "modem_preset": client.modem_preset,
    }

    if app_data.get("has_location"):
        loc = app_data.get("location", {})
        lat = loc.get("latitude")
        lon = loc.get("longitude")
        if lat is not None and lon is not None:
            node_data["position"] = {"latitude": lat, "longitude": lon}
            await client.send_position({
                "from_id": node_id,
                "rx_time": rx_time,
                "latitude": lat,
                "longitude": lon,
                "snr": snr,
                "rssi": rssi,
            })

    await client.send_node(node_id, node_data)


async def handle_group_text(
    msg, decrypted: dict | None, rx_time: int, snr: float, rssi: int, path: str,
    channel_name: str, client: PotatoClient
) -> None:
    """GroupTextPayload → message (public channels only).

    decrypted is our own decryption result (dict with sender/text/timestamp keys),
    None if we couldn't decrypt (unknown channel or MAC failure).
    """
    if msg is None:
        return

    channel_hash = getattr(msg, "channel_hash", "")
    decrypted = decrypted or {}

    text = decrypted.get("text", "")
    sender = decrypted.get("sender", "")
    channel = decrypted.get("channel", 0)

    decrypted_ok = bool(text or sender)

    if not decrypted_ok:
        log.info("group_text_encrypted", channel_hash=channel_hash)
        if settings.channels:
            log.debug("group_text_no_key_skipped", channel_hash=channel_hash)
            return

    # from_id: prefer real node_id from Advert cache; fallback to synthetic id
    # derived the same way potato-mesh does: "!" + SHA256(name)[0:8]
    if sender:
        from_id = _name_to_node_id.get(sender.lower()) or _meshcore_synthetic_node_id(sender)
    else:
        from_id = ""

    # potato-mesh parses sender name from "SenderName: body" text format
    full_text = f"{sender}: {text}" if sender and text else text

    msg_id = _derive_msg_id(channel_hash, from_id, full_text)
    if decrypted_ok:
        log.info("group_text", sender=sender, from_id=from_id, channel_name=channel_name, text=text[:60])

    await client.send_message({
        "id": msg_id,
        "rx_time": rx_time,
        "from_id": from_id,
        "to_id": "^all",
        "channel": 0,
        "channel_name": channel_name,
        "text": full_text,
        "portnum": "TEXT_MESSAGE_APP",
        "snr": snr,
        "rssi": rssi,
        "lora_freq": client.lora_freq,
        "modem_preset": client.modem_preset,
    })


