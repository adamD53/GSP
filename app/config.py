from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    kafka_bootstrap_servers: str = "redpanda:9092"
    default_topic: str = "raw-logs"
    startup_retries: int = 10
    startup_retry_delay: float = 1.5


settings = Settings()
