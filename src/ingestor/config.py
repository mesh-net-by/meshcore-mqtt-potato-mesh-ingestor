from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # MQTT
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_username: str
    mqtt_password: str
    mqtt_topic_prefix: str = "meshcore"
    # Shared subscription group — all replicas share load, each message processed once
    mqtt_consumer_group: str = "ingestor-group"

    # potato-mesh
    potato_host: str
    potato_api_token: str
    ingestor_name: str = "mqtt-ingestor"

    # Comma-separated public channel names (without #), e.g. "test,news,general".
    # Keys are derived automatically from names.
    # Empty = forward all public channels (decrypt where possible).
    channels_raw: str = ""

    # Optional: pre-configure the repeater's canonical node_id (e.g. !3c15e67e)
    # Used for heartbeat before the first status message arrives.
    # Learned automatically from status messages at runtime.
    repeater_node_id: str = ""

    log_level: str = "INFO"

    @property
    def channels(self) -> list[str]:
        raw = self.channels_raw.strip()
        return [n.strip() for n in raw.split(",") if n.strip()] if raw else []

    @property
    def mqtt_shared_topic(self) -> str:
        return f"$share/{self.mqtt_consumer_group}/{self.mqtt_topic_prefix}/#"


settings = Settings()
