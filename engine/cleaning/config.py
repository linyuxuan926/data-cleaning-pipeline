from dataclasses import dataclass
import os
from urllib.parse import urlparse


@dataclass(frozen=True)
class Settings:
    minio_endpoint: str
    minio_access_key: str
    minio_secret_key: str
    minio_bucket: str
    internal_token: str
    java_api_url: str
    rabbitmq_host: str
    rabbitmq_port: int
    rabbitmq_user: str
    rabbitmq_password: str
    worker_queue: str
    execution_timeout_seconds: float = 1800
    cancel_grace_seconds: float = 10
    scratch_root: str = "/tmp/cleaning-attempts"
    scratch_retention_seconds: int = 3600

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.environ.get("INTERNAL_API_TOKEN", "")
        if len(token) < 32:
            raise RuntimeError("INTERNAL_API_TOKEN must contain at least 32 characters")
        endpoint = os.environ.get("MINIO_ENDPOINT", "http://minio:9000")
        parsed = urlparse(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.path not in {"", "/"}
        ):
            raise RuntimeError("MINIO_ENDPOINT must be an http(s) origin")
        return cls(
            minio_endpoint=endpoint,
            minio_access_key=os.environ["MINIO_ACCESS_KEY"],
            minio_secret_key=os.environ["MINIO_SECRET_KEY"],
            minio_bucket=os.environ.get("MINIO_BUCKET", "cleaning"),
            internal_token=token,
            java_api_url=os.environ.get("JAVA_API_URL", "http://backend:8080").rstrip(
                "/"
            ),
            rabbitmq_host=os.environ.get("RABBITMQ_HOST", "rabbitmq"),
            rabbitmq_port=int(os.environ.get("RABBITMQ_PORT", "5672")),
            rabbitmq_user=os.environ["RABBITMQ_USER"],
            rabbitmq_password=os.environ["RABBITMQ_PASSWORD"],
            worker_queue=os.environ.get("WORKER_QUEUE", "cleaning.tasks"),
            execution_timeout_seconds=_bounded("EXECUTION_TIMEOUT_SECONDS", 1800, 1, 86400),
            cancel_grace_seconds=_bounded("CANCEL_GRACE_SECONDS", 10, 1, 60),
            scratch_root=os.environ.get("SCRATCH_ROOT", "/tmp/cleaning-attempts"),
            scratch_retention_seconds=int(_bounded("SCRATCH_RETENTION_SECONDS", 3600, 60, 604800)),
        )


def _bounded(name: str, default: float, minimum: float, maximum: float) -> float:
    value = float(os.environ.get(name, str(default)))
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value
