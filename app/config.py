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
    worker_batch_size: int = 100
    worker_concurrency: int = 16
    enable_fast_parser: bool = True
    enable_schema_learning: bool = True
    startup_retries: int = 10
    startup_retry_delay: float = 1.5
    rules_file: str = "rules.json"
    runbooks_file: str = "runbooks.json"
    alerts_db_path: str = "data/alerts.db"
    default_dedup_window_seconds: int = 300

    # Notification routing & working-hours defaults
    working_hours_start: str = "09:00"
    working_hours_end: str = "17:00"
    working_days: str = "0,1,2,3,4"  # 0=Mon .. 6=Sun
    working_hours_timezone: str = "UTC"
    default_alert_email: str = "oncall@example.com"
    default_alert_sms: str = "+15550001234"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_from: str = "alerts@gsp.local"
    sms_webhook_url: str | None = None


settings = Settings()
