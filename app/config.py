from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    kafka_bootstrap_servers: str = "redpanda:9092"
    default_topic: str = "raw-logs"
    parsed_topic: str = "parsed-logs"
    dead_letter_topic: str = "dead-letter-logs"
    consumer_group_id: str = "ollama-log-parser"
    ollama_base_url: str = "http://ollama:11434"
    ollama_model: str = "qwen2.5:1.5b"
    ollama_timeout_seconds: float = 60.0
    startup_retries: int = 10
    startup_retry_delay: float = 1.5
    rules_file: str = "rules.json"


settings = Settings()
