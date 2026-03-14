"""Repository for UploadedFile queries."""

from uuid import UUID

from sqlalchemy import select

from app.persistence.models.file import UploadedFile
from app.persistence.repositories.base import BaseRepository


class FileRepository(BaseRepository[UploadedFile]):
    model = UploadedFile

    async def list_by_tenant_filtered(
        self,
        tenant_id: UUID,
        processing_status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[UploadedFile]:
        """List files for a tenant, optionally filtered by processing_status."""
        q = select(UploadedFile).where(UploadedFile.tenant_id == tenant_id)
        if processing_status is not None:
            q = q.where(UploadedFile.processing_status == processing_status)
        q = q.order_by(UploadedFile.created_at.desc()).limit(limit).offset(offset)
        result = await self._session.execute(q)
        return list(result.scalars().all())
