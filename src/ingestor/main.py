import asyncio
import json
import logging
import time
from datetime import datetime, timezone

import structlog
from aiomqtt import Client as MqttClient, MqttError, ProtocolVersion

from .config import settings
from .handlers import _modem_preset as parse_modem_preset
from .potato_client import PotatoClient
from .processor import process_message

structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(
        logging.getLevelName(settings.log_level.upper())
    ),
)

log = structlog.get_logger()

HEARTBEAT_INTERVAL = 60       # seconds
HEARTBEAT_INITIAL_DELAY = 15  # wait for first status before first heartbeat
RECONNECT_DELAY = 5           # seconds on MQTT disconnect
RETAIN_CLEANUP_INTERVAL = 3600  # run cleanup every hour
RETAIN_MAX_AGE = 6 * 3600       # clear retained messages older than 6 hours


async def heartbeat_loop(potato: PotatoClient) -> None:
    await asyncio.sleep(HEARTBEAT_INITIAL_DELAY)
    while True:
        try:
            await potato.heartbeat()
        except Exception:
            log.exception("heartbeat_failed")
        await asyncio.sleep(HEARTBEAT_INTERVAL)


async def _prefetch_lora_info(potato: PotatoClient) -> None:
    """Subscribe briefly to status topics to get radio params before processing packets."""
    status_topic = f"{settings.mqtt_topic_prefix}/+/+/status"
    try:
        async with MqttClient(
            hostname=settings.mqtt_host,
            port=settings.mqtt_port,
            username=settings.mqtt_username,
            password=settings.mqtt_password,
            protocol=ProtocolVersion.V5,
        ) as mqtt:
            await mqtt.subscribe(status_topic, qos=1)
            try:
                async with asyncio.timeout(5.0):
                    async for msg in mqtt.messages:
                        try:
                            payload = json.loads(msg.payload)
                            radio_raw = payload.get("radio", "")
                            if isinstance(radio_raw, str) and radio_raw:
                                lora_freq = float(radio_raw.split(",")[0])
                                modem_preset = parse_modem_preset(radio_raw)
                                await potato.set_repeater_lora_info(lora_freq, modem_preset)
                                log.info("lora_prefetched", lora_freq=lora_freq, modem_preset=modem_preset)
                        except Exception:
                            pass
                        break  # one status message is enough
            except TimeoutError:
                log.debug("lora_prefetch_timeout")
    except Exception as e:
        log.debug("lora_prefetch_failed", error=str(e))


async def _retain_cleanup_loop() -> None:
    """Periodically scan all retained messages and clear those older than RETAIN_MAX_AGE."""
    scan_topic = f"{settings.mqtt_topic_prefix}/#"
    while True:
        await asyncio.sleep(RETAIN_CLEANUP_INTERVAL)
        now = time.time()
        cleared = 0
        try:
            async with MqttClient(
                hostname=settings.mqtt_host,
                port=settings.mqtt_port,
                username=settings.mqtt_username,
                password=settings.mqtt_password,
                protocol=ProtocolVersion.V5,
            ) as mqtt:
                await mqtt.subscribe(scan_topic, qos=1)
                try:
                    async with asyncio.timeout(10.0):
                        async for msg in mqtt.messages:
                            if not msg.retain:
                                continue
                            topic = str(msg.topic)
                            msg_ts = None
                            try:
                                payload = json.loads(msg.payload)
                                ts_raw = payload.get("timestamp")
                                if isinstance(ts_raw, str):
                                    msg_ts = datetime.fromisoformat(ts_raw).replace(tzinfo=timezone.utc).timestamp()
                                elif isinstance(ts_raw, (int, float)):
                                    msg_ts = float(ts_raw)
                            except Exception:
                                pass
                            if msg_ts and (now - msg_ts) > RETAIN_MAX_AGE:
                                await mqtt.publish(topic, payload=b"", retain=True)
                                log.info("retain_expired", topic=topic, age_h=round((now - msg_ts) / 3600, 1))
                                cleared += 1
                except TimeoutError:
                    pass
        except Exception as e:
            log.warning("retain_cleanup_failed", error=str(e))
        if cleared:
            log.info("retain_cleanup_done", cleared=cleared)
        else:
            log.debug("retain_cleanup_done", cleared=0)


async def run() -> None:
    potato = PotatoClient()
    log.info("starting", ingestor=settings.ingestor_name, topic=settings.mqtt_shared_topic)

    await _prefetch_lora_info(potato)

    asyncio.create_task(heartbeat_loop(potato))
    asyncio.create_task(_retain_cleanup_loop())

    while True:
        try:
            async with MqttClient(
                hostname=settings.mqtt_host,
                port=settings.mqtt_port,
                username=settings.mqtt_username,
                password=settings.mqtt_password,
                # MQTT 5.0 for shared subscription support
                protocol=ProtocolVersion.V5,
            ) as mqtt:
                await mqtt.subscribe(settings.mqtt_shared_topic, qos=1)
                log.info("subscribed", topic=settings.mqtt_shared_topic)

                async for msg in mqtt.messages:
                    asyncio.create_task(process_message(msg, potato, mqtt))

        except MqttError as e:
            log.warning("mqtt_disconnected", error=str(e), reconnect_in=RECONNECT_DELAY)
            await asyncio.sleep(RECONNECT_DELAY)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
