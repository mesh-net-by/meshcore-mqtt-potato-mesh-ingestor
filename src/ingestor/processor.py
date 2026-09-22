"""
Core message processor: receives an MQTT message, routes to the right handler,
then clears the retained message from the broker.
"""

import hashlib
import hmac as hmac_mod
import json
import re
import struct
import time
from datetime import datetime, timezone
from typing import Optional

import structlog
from Crypto.Cipher import AES
from aiomqtt import Client as MqttClient, Message
from meshcoredecoder import MeshCoreDecoder
from meshcoredecoder.types import PayloadType

from .config import settings
from .handlers import handle_advert, handle_group_text, handle_path, handle_status
from .potato_client import PotatoClient

log = structlog.get_logger()

decoder = MeshCoreDecoder()

# MeshCore GroupText crypto constants
_CIPHER_MAC_SIZE = 2   # 2-byte HMAC prefix
_CIPHER_KEY_SIZE = 16  # AES-128

# Channel secret: full 32-byte SHA256 of "#name" (lowercase).
# AES key  = first 16 bytes of secret
# HMAC key = full 32 bytes  ← meshcoredecoder library wrongly uses key16+zeros
_CHANNEL_SECRETS: dict[str, bytes] = {}  # channel_hash_hex -> 32-byte secret
_CHANNEL_NAMES: dict[str, str] = {}      # channel_hash_hex -> channel name


def _channel_secret(name: str) -> bytes:
    """Full 32-byte channel secret: SHA256('#name'.lower())."""
    tag = f"#{name}" if not name.startswith("#") else name
    return hashlib.sha256(tag.lower().encode("utf-8")).digest()


def _channel_hash(secret: bytes) -> str:
    """1-byte channel hash used in wire format: SHA256(secret[:16])[0] as 2-hex."""
    return f"{hashlib.sha256(secret[:16]).digest()[0]:02x}"


def _decrypt_group_text(channel_hash_hex: str, cipher_mac_hex: str, ciphertext_hex: str) -> Optional[dict]:
    """
    Decrypt GroupText using the correct MeshCore algorithm:
    - AES-128-ECB with secret[:16]
    - HMAC-SHA256 with full 32-byte secret  (meshcoredecoder wrongly uses key16+zeros)
    """
    secret = _CHANNEL_SECRETS.get(str(channel_hash_hex).lower())
    if not secret:
        return None

    try:
        ciphertext = bytes.fromhex(str(ciphertext_hex))
        cipher_mac = bytes.fromhex(str(cipher_mac_hex))
        if len(ciphertext) < 16 or len(ciphertext) % 16 != 0:
            return None

        # HMAC key: first 16 bytes of channel secret + 16 zero bytes.
        # The firmware stores TransportKey::key as 16 bytes; when passed as 32-byte HMAC key
        # the upper 16 bytes are zero (zero-initialized struct memory).
        hmac_key = secret[:16] + b"\x00" * 16
        calc_mac = hmac_mod.new(hmac_key, ciphertext, hashlib.sha256).digest()
        if not hmac_mod.compare_digest(calc_mac[:2], cipher_mac[:2]):
            return None

        # Decrypt AES-128-ECB with first 16 bytes
        cipher = AES.new(secret[:16], AES.MODE_ECB)
        plaintext = cipher.decrypt(ciphertext)

        if len(plaintext) < 5:
            return None

        # timestamp(4 LE) + flags(1) + UTF-8 null-terminated "sender: message"
        msg_ts = struct.unpack_from("<I", plaintext)[0]
        text = plaintext[5:].decode("utf-8", errors="ignore")
        null = text.find("\x00")
        if null >= 0:
            text = text[:null]

        sender: Optional[str] = None
        content = text
        colon = text.find(": ")
        if 0 < colon < 50 and not re.search(r"[:\[\]]", text[:colon]):
            sender = text[:colon]
            content = text[colon + 2:]

        return {"timestamp": msg_ts, "sender": sender, "text": content}
    except Exception:
        return None


def _register_channel(name: str) -> None:
    secret = _channel_secret(name)
    h = _channel_hash(secret)
    _CHANNEL_SECRETS[h] = secret
    _CHANNEL_NAMES[h] = name.lstrip("#")
    log.info("channel_registered", name=name, hash=h.upper())


# Always register the well-known MeshCore Public channel.
_PUBLIC_FIXED_SECRET_32 = bytes.fromhex("8b3387e9c5cdea6ac9e5edbaa115cd72") + b"\x00" * 16
_pub_hash = f"{hashlib.sha256(bytes.fromhex('8b3387e9c5cdea6ac9e5edbaa115cd72')).digest()[0]:02x}"
_CHANNEL_SECRETS[_pub_hash] = _PUBLIC_FIXED_SECRET_32
_CHANNEL_NAMES[_pub_hash] = "Public"

# Register channels from config
for _name in settings.channels:
    _register_channel(_name)


async def process_message(msg: Message, potato: PotatoClient, mqtt: MqttClient) -> None:
    topic = str(msg.topic)
    parts = topic.split("/")

    # Expected: meshcore/{iata}/{device_id}/{subtopic}
    if len(parts) < 4:
        await _clear_retain(mqtt, topic, msg.retain)
        return

    subtopic = parts[3]

    try:
        payload = json.loads(msg.payload)
    except (json.JSONDecodeError, ValueError):
        log.warning("invalid_json", topic=topic)
        await _clear_retain(mqtt, topic, msg.retain)
        return

    try:
        if subtopic == "status":
            log.debug("status_payload", payload=payload)
            await handle_status(payload, potato)

        elif subtopic == "packets":
            potato.count_packet()
            await _process_packet(payload, potato)

        elif subtopic == "raw":
            # raw duplicates packets without RF metadata — retain cleared below
            pass

        else:
            log.debug("discarding_unknown_subtopic", subtopic=subtopic, topic=topic)

    except Exception:
        log.exception("handler_error", topic=topic, subtopic=subtopic)

    finally:
        await _clear_retain(mqtt, topic, msg.retain)


async def _process_packet(payload: dict, potato: PotatoClient) -> None:
    raw_hex = payload.get("raw") or payload.get("data", "")
    if not raw_hex:
        return

    snr: float = float(payload.get("SNR", 0.0))
    rssi: int = int(payload.get("RSSI", 0))
    path: str = payload.get("path", "")
    rx_time: int = _parse_timestamp(payload.get("timestamp"))

    try:
        decoded = decoder.decode(raw_hex)
    except Exception as e:
        log.debug("decode_failed", error=str(e), raw=raw_hex[:32])
        return

    if not decoded or not decoded.is_valid:
        log.debug("invalid_packet", raw=raw_hex[:32])
        return

    ptype = decoded.payload_type
    inner = decoded.payload.get("decoded")

    log.debug("decoded_packet", ptype=ptype.name if ptype else None, inner=str(inner)[:120])

    if ptype == PayloadType.Advert:
        await handle_advert(inner, rx_time, snr, rssi, potato)

    elif ptype == PayloadType.GroupText:
        decrypted = None
        ch = getattr(inner, "channel_hash", "") if inner is not None else ""
        if inner is not None:
            decrypted = _decrypt_group_text(ch, getattr(inner, "cipher_mac", ""), getattr(inner, "ciphertext", ""))
        ch_name = _CHANNEL_NAMES.get(str(ch).lower(), "")
        await handle_group_text(inner, decrypted, rx_time, snr, rssi, path, ch_name, potato)

    elif ptype == PayloadType.TextMessage:
        # Private/direct message — never forward
        log.debug("dropping_private_message")

    elif ptype in (PayloadType.Path, PayloadType.Trace):
        await handle_path(inner, rx_time, snr, rssi, potato)

    else:
        log.debug("unhandled_packet_type", ptype=ptype.name if ptype else "unknown")


def _parse_timestamp(ts: object) -> int:
    if ts is None:
        return int(time.time())
    if isinstance(ts, (int, float)):
        return int(ts)
    try:
        return int(datetime.fromisoformat(str(ts)).replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return int(time.time())


async def _clear_retain(mqtt: MqttClient, topic: str, was_retained: bool) -> None:
    """Remove retained message from broker by publishing empty payload."""
    if was_retained:
        await mqtt.publish(topic, payload=b"", retain=True)
