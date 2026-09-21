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
   Mosquitto Broker  (included in this repo via Docker Compose)
        │  $share/ingestor-group/meshcore/#
        ▼
   Ingestor (this project)
   ├── status   → node info + telemetry
   ├── packets  → decoded with meshcoredecoder
   │   ├── Advert     → nodes + positions
   │   ├── GroupText  → messages (public channels only)
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
- A MeshCore repeater running VBart firmware with MQTT configured to point at
  this broker
- A running potato-mesh instance with an API token

---

## Setup

### 1. Clone and configure

```bash
git clone <this-repo>
cd meshcore-observer-potato-mesh-ingestor
cp .env.example .env
```

Edit `.env`:

```env
MQTT_HOST=localhost
MQTT_PORT=1883
MQTT_USERNAME=ingestor
MQTT_PASSWORD=your-strong-password

POTATO_HOST=https://your-potato-mesh.example.com
POTATO_API_TOKEN=your-api-token
INGESTOR_NAME=my-mqtt-ingestor

# Optional: comma-separated channel indices to forward (empty = all public)
ALLOWED_CHANNEL_INDICES=
```

### 2. Create Mosquitto password file

```bash
docker run --rm eclipse-mosquitto:2 \
  mosquitto_passwd -b /dev/stdout ingestor your-strong-password \
  > mosquitto/config/passwd
```

> The password in the command must match `MQTT_PASSWORD` in `.env`.
> The `passwd` file is gitignored — never commit it.

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

## Scaling

Run multiple ingestor replicas to process the message queue faster:

```bash
docker compose up -d --scale ingestor=3
```

All replicas join the same shared subscription group — each MQTT message is
delivered to exactly one replica, no duplicates.

Or set it permanently in `.env`:

```env
INGESTOR_REPLICAS=3
```

---

## Message Filtering

Only **public channel messages** (`to_id = "^all"`) are forwarded to
potato-mesh. The following are always dropped:

- Direct messages (DMs) — `to_id` is a specific node ID
- Encrypted / closed channel packets

To restrict forwarding to specific channels, set their indices in `.env`:

```env
# Forward only channels 0 and 1
ALLOWED_CHANNEL_INDICES=0,1

# Forward all public channels (default)
ALLOWED_CHANNEL_INDICES=
```

Channel indices correspond to the channel slots configured on the MeshCore
device. Channel names (if announced via Advert packets) are resolved at the
potato-mesh side.

---

## MQTT Data Cleanup

The ingestor subscribes to the top-level `meshcore/#` wildcard and processes
everything. After each message is handled:

- **Retained messages** — ingestor publishes an empty payload to the same topic,
  which instructs Mosquitto to remove the retained copy.
- **Non-retained messages** — consumed and discarded automatically.

Mosquitto is also configured with a 24-hour retained message expiry as a safety
net (`retained_expire_interval 86400`).

---

## Project Structure

```
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
├── .env.example
├── mosquitto/
│   └── config/
│       ├── mosquitto.conf
│       └── passwd.example
└── src/ingestor/
    ├── config.py          — settings (pydantic-settings, reads .env)
    ├── main.py            — async event loop, MQTT reconnect, heartbeat
    ├── processor.py       — message routing + retain cleanup
    ├── handlers.py        — data mapping to potato-mesh API calls
    └── potato_client.py   — async HTTP client for potato-mesh
```

---

## Development

```bash
# Install dependencies with uv
uv pip install -e .

# Run locally (requires Mosquitto running and .env set)
python -m ingestor.main
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

The firmware will publish to `meshcore/{iata}/{device_id}/{subtopic}` — no
additional configuration needed on the ingestor side.
