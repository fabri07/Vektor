"""S3-compatible storage client (AWS S3 / MinIO / Cloudflare R2)."""

from pathlib import Path
import uuid

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.config.settings import get_settings
from app.observability.logger import get_logger

logger = get_logger(__name__)


class S3Client:
    def __init__(self) -> None:
        s = get_settings()
        self._settings = s
        self._bucket = s.S3_BUCKET_NAME
        self._local_root = Path(__file__).resolve().parents[2] / ".local_uploads"
        self._client = boto3.client(  # type: ignore[call-overload]
            "s3",
            region_name=s.S3_REGION,
            aws_access_key_id=s.S3_ACCESS_KEY_ID,
            aws_secret_access_key=s.S3_SECRET_ACCESS_KEY,
            **({"endpoint_url": s.S3_ENDPOINT_URL} if s.S3_ENDPOINT_URL else {}),
        )

    @staticmethod
    def _is_local_key(key: str) -> bool:
        return key.startswith("local/")

    def _local_key(self, key: str) -> str:
        return key if self._is_local_key(key) else f"local/{key}"

    def _local_path(self, key: str) -> Path:
        relative = key.removeprefix("local/")
        return self._local_root / relative

    async def _store_local(self, content: bytes, key: str) -> str:
        local_key = self._local_key(key)
        local_path = self._local_path(local_key)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(content)
        logger.warning("s3.fallback_to_local_storage", key=local_key, path=str(local_path))
        return local_key

    def _can_fallback_to_local(self) -> bool:
        return self._settings.is_development or self._settings.DEBUG

    async def _put_object(self, content: bytes, key: str, content_type: str) -> str:
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=content,
                ContentType=content_type,
            )
        except (BotoCoreError, ClientError) as exc:
            logger.error("s3.upload_failed", key=key, error=str(exc))
            if self._can_fallback_to_local():
                return await self._store_local(content, key)
            raise

        logger.info("s3.uploaded", key=key, size=len(content))
        return key

    async def upload(
        self,
        content: bytes,
        filename: str,
        content_type: str,
        tenant_id: str,
    ) -> str:
        """Upload bytes to S3 and return the S3 key."""
        ext = filename.rsplit(".", 1)[-1] if "." in filename else "bin"
        key = f"tenants/{tenant_id}/{uuid.uuid4()}.{ext}"
        return await self._put_object(content, key, content_type)

    async def upload_to_key(
        self,
        content: bytes,
        key: str,
        content_type: str,
    ) -> str:
        """Upload bytes to S3 using a caller-specified key. Returns the key."""
        return await self._put_object(content, key, content_type)

    async def download(self, key: str) -> bytes:
        """Download and return object bytes from S3."""
        if self._is_local_key(key):
            local_path = self._local_path(key)
            data = local_path.read_bytes()
            logger.info("s3.downloaded_local", key=key, size=len(data))
            return data

        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            data: bytes = response["Body"].read()
            logger.info("s3.downloaded", key=key, size=len(data))
            return data
        except (BotoCoreError, ClientError) as exc:
            logger.error("s3.download_failed", key=key, error=str(exc))
            raise

    async def generate_presigned_url(self, key: str, expires_in: int = 3600) -> str:
        """Generate a pre-signed GET URL for a given S3 key."""
        if self._is_local_key(key):
            return f"local://{key.removeprefix('local/')}"

        try:
            url: str = self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=expires_in,
            )
            return url
        except (BotoCoreError, ClientError) as exc:
            logger.error("s3.presign_failed", key=key, error=str(exc))
            raise

    async def delete(self, key: str) -> None:
        """Delete an object from S3."""
        if self._is_local_key(key):
            local_path = self._local_path(key)
            if local_path.exists():
                local_path.unlink()
            logger.info("s3.deleted_local", key=key)
            return

        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
            logger.info("s3.deleted", key=key)
        except (BotoCoreError, ClientError) as exc:
            logger.error("s3.delete_failed", key=key, error=str(exc))
            raise
