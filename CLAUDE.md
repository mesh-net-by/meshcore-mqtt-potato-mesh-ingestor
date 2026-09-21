# MeshCore → potato-mesh MQTT Ingestor

## What This Is

An ingestor that reads MeshCore node data from an MQTT broker (published by
MeshCore repeater firmware) and forwards it to the potato-mesh map API.

**Why:** The existing potato-mesh MeshCore ingestor requires a direct companion
node connection. The VBart/MeshCoreTel-firmware fork publishes all received
packets to MQTT, enabling remote/headless repeaters to feed the map without a
companion node.

**No TCP/companion node.** All data comes from MQTT only.

---

## Upstream Projects

| Project | URL | Role |
|---|---|---|
| potato-mesh | https://github.com/l5yth/potato-mesh | Map UI + API we POST to |
| VBart firmware | https://github.com/VBart/MeshCoreTel-firmware | Firmware publishing to MQTT |
| meshcoredecoder | https://pypi.org/project/meshcoredecoder/ | Packet structure parser (NOT its crypto — see below) |
| meshcore-packet-capture | https://github.com/agessaman/meshcore-packet-capture | Reference implementation |
| mc-webui | https://github.com/MarekWo/mc-webui | Reference implementation |
| meshcoretomqtt | https://github.com/Cisien/meshcoretomqtt | Reference implementation |

---

## MQTT Topic Format

```
meshcore/{iata}/{device_id}/{subtopic}
```

- `iata` — 8-char location code configured per device
- `device_id` — node identifier (public key or similar)
- `subtopic` — one of: `status`, `packets`, `raw`

### Payload Shapes

**`status`** — the publishing repeater's own state:
```json
{
  "status": "...", "timestamp": "...", "origin": "...", "origin_id": "...",
  "model": "...", "firmware_version": "...", "client_version": "...",
  "radio": "freq,bw,sf,cr",
  "stats": { "battery_mv": 0, "uptime_secs": 0, "noise_floor": 0, "air_time": 0, "queue_length": 0 }
}
```

**`packets`** — all packets the repeater heard over the air:
```json
{
  "type": "PACKET", "direction": "...", "timestamp": "...",
  "packet_type": "...", "payload_len": 0,
  "route": "...", "raw": "<hex>", "SNR": 0, "RSSI": 0,
  "score": 0, "duration": 0, "hash": "...", "path": "..."
}
```

**`raw`** — raw hex only (discarded, duplicates `packets` without RF metadata):
```json
{ "type": "RAW", "origin": "...", "origin_id": "...", "timestamp": "...", "data": "<hex>" }
```

---

## MeshCore GroupText Crypto — IMPORTANT

**Context:** Any MeshCore user can join a hashtag channel (#test, #news, Public)
just by typing its name — no manual key sharing. Keys are derived automatically
from the channel name.

### Key Derivation (from MeshCore `TransportKeyStore.cpp`)

```python
import hashlib

def channel_secret(name: str) -> bytes:
    """Full 32-byte channel secret."""
    tag = f"#{name}" if not name.startswith("#") else name
    return hashlib.sha256(tag.lower().encode("utf-8")).digest()  # 32 bytes

def channel_hash_byte(secret: bytes) -> int:
    """1-byte wire identifier: SHA256(secret[:16])[0]"""
    return hashlib.sha256(secret[:16]).digest()[0]
```

- `#test`   → SHA256("#test")   → hash byte `0xD9`
- `#news`   → SHA256("#news")   → hash byte `0x03`
- `#Public` → derived similarly (historically also distributed as fixed 16-byte key)

### Encryption Algorithm (from MeshCore `Utils.cpp`)

```
AES-128-ECB(key=channel_key[:16], plaintext)                      ← cipher
HMAC-SHA256(key=channel_key[:16] + b"\x00"*16, data=ciphertext)  → first 2 bytes = MAC on wire
```

Plaintext layout: `timestamp(4 LE) | flags(1) | "Sender: message\x00"`

**HMAC key is `key16 + 16 zero bytes` (32 bytes total).** The firmware's `TransportKey`
struct holds only 16 key bytes; passing it with `PUB_KEY_SIZE=32` reads key + zero-padded memory.
Verified empirically: `key16+zeros` produces correct MAC, raw `SHA256("#name")` (32 bytes) does not.

### meshcoredecoder Library — Crypto NOT Used

The library's HMAC key (`key16 + zeros`) is actually correct. However, we still
bypass its decryption because we need the full 32-byte SHA256 for key derivation
and the library expects a pre-computed 16-byte hex key as input. We call
`_decrypt_group_text()` in `processor.py` directly.

**We still use `meshcoredecoder` for packet structure parsing** (it decodes the
wire format into typed Python objects — `AdvertPayload`, `GroupTextPayload`, etc.).
We only bypass its decryption and call our own `_decrypt_group_text()` in
`processor.py`.

We use `pycryptodome` directly for AES and Python's stdlib `hmac` for HMAC.

---

## Channel Configuration

Single env variable: `CHANNELS_RAW`

- **Empty** (`CHANNELS_RAW=`): attempt to decrypt all GroupText packets; forward
  everything (decrypted or not).
- **List** (`CHANNELS_RAW=test,news`): derive keys automatically for those channels;
  only forward messages we can successfully decrypt on those channels.

**Never add keys manually.** Channel names → keys are derived automatically.
The Public channel key is registered at startup unconditionally.

---

## potato-mesh API

Base: `https://{POTATO_HOST}`
Auth: `Authorization: Bearer {API_TOKEN}`

### Endpoints

**`POST /api/nodes`** — keyed dict `{ node_id: {...}, "ingestor": "...", "protocol": "..." }`:
- `user`: `role`, `longName`, `shortName`, `publicKey`
- `position`: `latitude`, `longitude`, `altitude`
- `deviceMetrics`: `batteryLevel`, `voltage`, `uptimeSeconds`
- top-level per node: `lastHeard`, `lora_freq`, `modemPreset`, `snr`, `rssi`

**`POST /api/messages`** — accepts array or single object. Fields are **FLAT** (no nested `decoded`):
- **`id`** (integer, **REQUIRED** — without it message is silently dropped even with 201 response)
- `from_id` (required), `to_id` (`"^all"` = public broadcast)
- `rx_time` (unix int), `text` (string), `portnum` (`"TEXT_MESSAGE_APP"`)
- `channel` (int — UI groups messages into tabs by this; 0 = default tab)
- `channel_name` (string — used for stable content dedup across ingestors, prefer this over `channel`)
- `snr`, `rssi`, `hops`, `path`, `ingestor`, `protocol`
- Text format for MeshCore: `"SenderName: body"` — potato-mesh parses sender from this prefix via `parse_meshcore_sender_name()`
- Chat tabs appear for each unique `channel_name` seen.
- `from_id` must be `"!" + SHA256(name.strip())[0:8 hex]` — matches `meshcore_synthetic_node_id()` in potato-mesh

**`POST /api/positions`** — GPS updates:
- `from_id`, `rx_time`, `latitude`, `longitude`, `altitude`, `snr`, `rssi`

**`POST /api/telemetry`** — battery / environment:
- `from_id`, `rx_time`, `decoded.telemetry.deviceMetrics`

**`POST /api/neighbors`** — mesh topology:
- `node_id`, `rx_time`, `neighbors[]`: `{ neighbor_id, snr, rx_time }`

**`POST /api/ingestors`** — heartbeat (sent every 60 s).

### Sender node_id in GroupText

GroupText packets do NOT include the full sender public key — only the display name
in the decrypted plaintext (e.g. `"ALEX KORO: Potato"`). We resolve `from_id`:
1. Check in-memory cache `_name_to_node_id` populated by Advert packets
2. Fallback: `"!" + SHA256(name.strip())[0:8 hex]` — matches potato-mesh `meshcore_synthetic_node_id()`

potato-mesh creates a placeholder "synthetic" node for the sender automatically when it
processes the message. That node reconciles with the real node when an Advert arrives.

### Message `id` Derivation

```python
import hashlib, struct

def _derive_msg_id(channel_hash: str, from_id: str, text: str) -> int:
    h = hashlib.sha256(f"{channel_hash}|{from_id}|{text}".encode()).digest()
    return struct.unpack(">I", h[:4])[0]
```

Same physical message from multiple repeaters gets the same id → primary key dedup in DB.

### Internal potato-mesh Source Files (folder deleted — captured here for reference)

Key files read from `potato-mesh/` folder (now deleted):

- **`data_processing/meshcore_chat.rb`**: `meshcore_synthetic_node_id(name)` = `"!" + SHA256(name.strip)[0:8]`;
  `parse_meshcore_sender_name(text)` extracts sender from `"Name: body"` prefix;
  `process_meshcore_chat_nodes()` creates synthetic node records for sender and `@[mentions]`;
  only runs for `to_id == "^all"` messages.

- **`data_processing/messages.rb`** (line 94): `return unless msg_id` — NO `id` = silent drop
  despite 201 response. Accepts both `id` and `packet_id`; `id` preferred.

- **`queries/chat_queries.rb`**: messages returned in `rx_time DESC` order; default window = 7 days
  (28 days for per-node lookups); filters out opt-out nodes; deduplicates by `id` PK.

- **`routes/ingest.rb`**: `/api/messages` accepts array or single object; max 1000 items per batch;
  all endpoints require `Authorization: Bearer` token; all respond 201 on success.

- **`routes/ingest.rb`** `/api/nodes`: takes dict `{node_id: {...}, "ingestor": "...", "protocol": "..."}`;
  top-level `"ingestor"` and `"protocol"` keys are special (not treated as node_ids).

### Node Roles

| MeshCore ADV type | potato-mesh role |
|---|---|
| 1 | COMPANION |
| 2 | REPEATER |
| 3 | ROOM_SERVER |
| 4 | SENSOR |

---

## Message Filtering Rules

- **Send:** GroupText (channel broadcast, `to_id=^all`) only
- **Never send:** DMs (TextMessage packet type) or private messages
- **Never send:** messages we can't associate with a known public channel

---

## Architecture

```
MeshCore Repeater Firmware (VBart fork)
        │
        │  MQTT publish  meshcore/{iata}/{pubkey}/{subtopic}
        ▼
   Mosquitto Broker  (Docker Compose, local, auth required)
        │
        │  $share/ingestor-group/meshcore/#   ← shared subscription
        ▼
  ┌─────────────────────────────────────────────────┐
  │  Consumer Instance (1..N)                       │
  │                                                 │
  │  subtopic == "status"                           │
  │    → handle_status() → /api/nodes + /api/telemetry │
  │                                                 │
  │  subtopic == "packets"                          │
  │    → meshcoredecoder.decode(raw_hex)            │
  │    → route by PayloadType:                      │
  │        Advert    → /api/nodes + /api/positions  │
  │        GroupText → _decrypt_group_text()        │
  │                    → /api/messages              │
  │        Path/Trace→ /api/neighbors               │
  │        TextMessage → DROP (DM)                  │
  │                                                 │
  │  subtopic == "raw" → DROP                       │
  │  After processing: clear MQTT retain            │
  └─────────────────────────────────────────────────┘
        │
        │  POST JSON + Bearer token
        ▼
   potato-mesh API
```

### Multiple Consumers (Competing Consumers Pattern)

- All instances subscribe to `$share/ingestor-group/meshcore/#`
- Mosquitto 2.x delivers each message to exactly ONE consumer — no duplicates
- Scale: `docker compose up --scale ingestor=3`

### MQTT Retained Message Cleanup

After processing any retained message, publish `b""` to the same topic with
`retain=True` → broker discards the retained copy.

---

## Tech Stack

| Layer | Choice |
|---|---|
| Language | Python 3.11+ |
| Package manager | `uv` |
| MQTT client | `aiomqtt` (async, MQTT 5.0) |
| Packet structure parser | `meshcoredecoder` (PyPI) — structure only, NOT its crypto |
| AES crypto | `pycryptodome` — AES-128-ECB |
| HMAC | Python stdlib `hmac` + `hashlib` |
| HTTP client | `httpx` (async) |
| Broker | Mosquitto 2.x in Docker Compose |
| Config | `.env` + `pydantic-settings` |

---

## Key Files

| File | Purpose |
|---|---|
| `src/ingestor/config.py` | Settings via pydantic-settings; `channels` property parses `CHANNELS_RAW` |
| `src/ingestor/processor.py` | MQTT message router; `_decrypt_group_text()` custom crypto |
| `src/ingestor/handlers.py` | Maps decoded objects to potato-mesh API calls |
| `src/ingestor/potato_client.py` | HTTP client wrapping potato-mesh endpoints |
| `src/ingestor/main.py` | Entry point: MQTT connection loop + heartbeat task |

---

## Open Questions

- [ ] Does `packets` MQTT topic include packets received FROM OTHER NODES over the
      air (neighbor Adverts, messages passing through), or only the repeater's own
      outbound packets? **Critical** — determines whether we can build a full node
      map from a single repeater feed.
- [ ] Retry / buffer strategy when potato-mesh API is unreachable — in-memory queue
      only, or persist to SQLite?
