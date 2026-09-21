# meshcore-potato-ingestor

Reads MeshCore node data from an MQTT broker and forwards it to a
[potato-mesh](https://github.com/l5yth/potato-mesh) map instance.

**Why:** The existing potato-mesh MeshCore ingestor requires a direct companion
node connection. This ingestor works with the
[VBart/MeshCoreTel-firmware](https://github.com/VBart/MeshCoreTel-firmware)
fork, which publishes all received packets to MQTT — no companion node needed.

---

## How It Works

```
MeshCore Repeater (VBart firmware)
        │  MQTT publish  meshcore/{iata}/{device_id}/{status|packets|raw}
        ▼
   Mosquitto Broker  (included via Docker Compose)
        │  $share/ingestor-group/meshcore/#
        ▼
   Ingestor (this project)
   ├── status   → node info + telemetry
   ├── packets  → decoded with meshcoredecoder
   │   ├── Advert     → nodes + positions
   │   ├── GroupText  → messages (public channels, decrypted)
   │   └── Path/Trace → neighbors
   └── raw / unknown → discarded, retain cleared
        │  POST JSON + Bearer token
        ▼
   potato-mesh API
```

Multiple ingestor replicas can run in parallel — MQTT shared subscriptions
ensure each message is processed by exactly one replica.

---

## Requirements

- Docker & Docker Compose v2
- A MeshCore repeater running VBart/MeshCoreTel-firmware with MQTT pointing at
  this broker
- A running [potato-mesh](https://github.com/l5yth/potato-mesh) instance with
  an API token

---

## Setup

### 1. Clone and configure

```bash
git clone <this-repo>
cd meshcore-observer-potato-mesh-ingestor
cp .env.example .env   # then edit .env
```

Key variables in `.env`:

| Variable | Description |
|---|---|
| `MQTT_USERNAME` / `MQTT_PASSWORD` | Broker credentials (must match passwd file) |
| `POTATO_HOST` | potato-mesh base URL, e.g. `https://map.example.com` |
| `POTATO_API_TOKEN` | potato-mesh API bearer token |
| `INGESTOR_NAME` | Name shown in potato-mesh ingestors list |
| `CHANNELS_RAW` | Comma-separated channel names to forward, e.g. `test,news`. Empty = all public channels |
| `REPEATER_NODE_ID` | Optional: repeater node id (e.g. `!3c15e67e`) for heartbeat before first status arrives |

### 2. Create Mosquitto password file

```bash
docker run --rm eclipse-mosquitto:2 \
  mosquitto_passwd -b /dev/stdout meshcore your-strong-password \
  > mosquitto/config/passwd
```

The username and password must match `MQTT_USERNAME` / `MQTT_PASSWORD` in `.env`.
The `passwd` file is gitignored — never commit it.

### 3. Start

```bash
docker compose up -d
```

Check logs:

```bash
docker compose logs -f ingestor
docker compose logs -f mosquitto
```

---

## Channel Configuration

GroupText messages are encrypted per-channel. Keys are **derived automatically**
from channel names — no manual key entry.

```env
# Forward only these channels (keys derived from names automatically)
CHANNELS_RAW=test,news

# Forward all public channels, decrypt where possible (default)
CHANNELS_RAW=
```

The `Public` channel key is always registered at startup. Only broadcast
messages (`to_id = "^all"`) are forwarded; DMs are always dropped.

---

## Scaling

Run multiple replicas to handle higher message volume:

```bash
docker compose up -d --scale ingestor=3
```

Or set permanently in `.env`:

```env
INGESTOR_REPLICAS=3
```

Each MQTT message is delivered to exactly one replica — no duplicate processing.

---

## MQTT Retain Cleanup

- After processing each retained message, the ingestor publishes an empty
  payload to the same topic → Mosquitto removes the retained copy immediately.
- A background task runs every hour and clears any retained messages whose
  `timestamp` is older than **6 hours** (safety net for messages missed during
  downtime).

---

## Project Structure

```
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
├── mosquitto/
│   └── config/
│       ├── mosquitto.conf
│       └── passwd.example
└── src/ingestor/
    ├── config.py          — settings (pydantic-settings, reads .env)
    ├── main.py            — event loop, MQTT reconnect, heartbeat, retain cleanup
    ├── processor.py       — message routing, GroupText decryption, retain clear
    ├── handlers.py        — data mapping to potato-mesh API calls
    └── potato_client.py   — async HTTP client for potato-mesh
```

---

## Firmware Configuration

On the MeshCore repeater (VBart/MeshCoreTel-firmware), configure the MQTT
broker to point at the machine running this stack:

```
MQTT Host: <your-server-ip>
MQTT Port: 1883
MQTT Username: <MQTT_USERNAME from .env>
MQTT Password: <MQTT_PASSWORD from .env>
```

---

## Development

```bash
uv pip install -e .
python -m ingestor.main
```
