import time

import httpx
import structlog

from .config import settings

_start_time = int(time.time())

log = structlog.get_logger()

HEADERS = {
    "Authorization": f"Bearer {settings.potato_api_token}",
    "Content-Type": "application/json",
}


class PotatoClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.potato_host,
            headers=HEADERS,
            timeout=10.0,
        )
        self._lora_freq: float | None = None
        self._modem_preset: str | None = None

    async def post(self, endpoint: str, payload: dict | list) -> None:
        try:
            r = await self._client.post(endpoint, json=payload)
            r.raise_for_status()
            log.debug("potato_api_ok", endpoint=endpoint, status=r.status_code)
        except httpx.HTTPStatusError as e:
            log.warning("potato_api_error", endpoint=endpoint, status=e.response.status_code,
                        body=e.response.text[:200])
        except httpx.RequestError as e:
            log.error("potato_request_failed", endpoint=endpoint, error=str(e))

    async def send_node(self, node_id: str, data: dict) -> None:
        await self.post("/api/nodes", {node_id: {**data, "protocol": "meshcore", "ingestor": settings.ingestor_name}})

    async def send_message(self, data: dict) -> None:
        await self.post("/api/messages", [{**data, "protocol": "meshcore", "ingestor": settings.ingestor_name}])

    async def send_position(self, data: dict) -> None:
        await self.post("/api/positions", {**data, "protocol": "meshcore", "ingestor": settings.ingestor_name})

    async def send_telemetry(self, data: dict) -> None:
        await self.post("/api/telemetry", {**data, "protocol": "meshcore", "ingestor": settings.ingestor_name})

    async def send_neighbors(self, data: dict) -> None:
        await self.post("/api/neighbors", {**data, "ingestor": settings.ingestor_name})

    async def set_repeater_node_id(self, node_id: str) -> None:
        self._repeater_node_id = node_id

    async def set_repeater_lora_info(self, lora_freq: float | None, modem_preset: str | None) -> None:
        self._lora_freq = lora_freq
        self._modem_preset = modem_preset

    @property
    def lora_freq(self) -> float | None:
        return getattr(self, "_lora_freq", None)

    @property
    def modem_preset(self) -> str | None:
        return getattr(self, "_modem_preset", None)

    async def heartbeat(self) -> None:
        node_id = getattr(self, "_repeater_node_id", None) or settings.repeater_node_id or None
        if not node_id:
            log.debug("heartbeat_skipped_no_node_id")
            return
        await self.post("/api/ingestors", {
            "node_id": node_id,
            "version": "1.0.0",
            "start_time": _start_time,
            "last_seen_time": int(time.time()),
            "protocol": "meshcore",
        })

    async def aclose(self) -> None:
        await self._client.aclose()
