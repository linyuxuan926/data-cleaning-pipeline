from pathlib import Path
from urllib.parse import urlparse

from minio import Minio
import urllib3

from .config import Settings
from .models import InputError

MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_ARTIFACT_BYTES = 256 * 1024 * 1024


class ObjectStorage:
    def __init__(self, settings: Settings):
        parsed = urlparse(settings.minio_endpoint)
        self.bucket = settings.minio_bucket
        self.client = Minio(
            parsed.netloc,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=parsed.scheme == "https",
            http_client=urllib3.PoolManager(
                timeout=urllib3.Timeout(connect=5, read=60),
                retries=urllib3.Retry(total=2, backoff_factor=0.5),
            ),
        )
        self.cleanup_client = Minio(
            parsed.netloc, access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key, secure=parsed.scheme == "https",
            http_client=urllib3.PoolManager(timeout=urllib3.Timeout(connect=2, read=3), retries=False),
        )

    def download_input(self, key: str, target: Path, max_bytes: int = MAX_FILE_BYTES) -> None:
        if not key.startswith("raw/") or ".." in key or "\\" in key:
            raise InputError("Invalid input object reference")
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_FILE_BYTES:
            raise InputError("Input download byte limit must be within 1 byte and 100 MiB")
        size = self.client.stat_object(self.bucket, key).size
        if size <= 0 or size > max_bytes:
            raise InputError("Input exceeds configured download byte limit (at most 100 MiB)")
        # Bound bytes independently of stat to also catch object replacement races.
        response = self.client.get_object(self.bucket, key)
        try:
            total = 0
            with target.open("wb") as output:
                for block in response.stream(1024 * 1024):
                    total += len(block)
                    if total > max_bytes:
                        raise InputError("Input exceeds configured download byte limit (at most 100 MiB)")
                    output.write(block)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        finally:
            response.close()
            response.release_conn()

    def download_artifact(self, key: str, target: Path) -> None:
        if (
            not key.startswith(("results/", "dirty/"))
            or ".." in key
            or "\\" in key
            or ":" in key
        ):
            raise InputError("Invalid task artifact reference")
        size = self.client.stat_object(self.bucket, key).size
        if size < 0 or size > MAX_ARTIFACT_BYTES:
            raise InputError("Task artifact exceeds the 256 MiB preview limit")
        response = self.client.get_object(self.bucket, key)
        total = 0
        try:
            with target.open("wb") as output:
                for block in response.stream(1024 * 1024):
                    total += len(block)
                    if total > MAX_ARTIFACT_BYTES:
                        raise InputError("Task artifact exceeds the 256 MiB preview limit")
                    output.write(block)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        finally:
            response.close()
            response.release_conn()

    def upload(self, key: str, path: Path, content_type: str) -> None:
        self.client.fput_object(self.bucket, key, str(path), content_type=content_type)

    def remove(self, key: str) -> None:
        if not key.startswith(("results/", "dirty/", "reports/")) or ".." in key or "\\" in key:
            raise InputError("Invalid partial artifact reference")
        self.cleanup_client.remove_object(self.bucket, key)
