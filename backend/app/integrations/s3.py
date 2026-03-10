"""S3-compatible storage client (AWS S3 / MinIO / Cloudflare R2)."""

import uuid

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.config.settings import get_settings
from app.observability.logger import get_logger

logger = get_logger(__name__)


class S3Client:
    def __init__(self) -> None:
        s = get_settings()
        self._bucket = s.S3_BUCKET_NAME
        self._client = boto3.client(  # type: ignore[call-overload]
            "s3",
            region_name=s.S3_REGION,
            aws_access_key_id=s.S3_ACCESS_KEY_ID,
            aws_secret_access_key=s.S3_SECRET_ACCESS_KEY,
            **({"endpoint_url": s.S3_ENDPOINT_URL} if s.S3_ENDPOINT_URL else {}),
        )

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

        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=content,
                ContentType=content_type,
            )
        except (BotoCoreError, ClientError) as exc:
            logger.error("s3.upload_failed", key=key, error=str(exc))
            raise

        logger.info("s3.uploaded", key=key, size=len(content))
        return key

    async def generate_presigned_url(self, key: str, expires_in: int = 3600) -> str:
        """Generate a pre-signed GET URL for a given S3 key."""
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
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
            logger.info("s3.deleted", key=key)
        except (BotoCoreError, ClientError) as exc:
            logger.error("s3.delete_failed", key=key, error=str(exc))
            raise
