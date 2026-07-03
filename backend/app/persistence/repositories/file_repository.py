"""Repository for UploadedFile queries."""

from uuid import UUID

from sqlalchemy import select

from app.persistence.models.file import UploadedFile
from app.persistence.repositories.base import BaseRepository


class FileRepository(BaseRepository[UploadedFile]):
    model = UploadedFile

    async def get_by_id(
        self,
        id: UUID,
        tenant_id: UUID,
        *,
        include_deleted: bool = False,
    ) -> UploadedFile | None:
        """Get a file by id. Soft-deleted files are hidden unless include_deleted."""
        q = select(UploadedFile).where(
            UploadedFile.id == id,
            UploadedFile.tenant_id == tenant_id,
        )
        if not include_deleted:
            q = q.where(UploadedFile.deleted_at.is_(None))
        result = await self._session.execute(q)
        return result.scalar_one_or_none()

    async def find_imported_by_content_hash(
        self,
        tenant_id: UUID,
        content_hash: str,
        exclude_id: UUID | None = None,
    ) -> UploadedFile | None:
        """Busca un archivo YA importado (DONE) con el mismo contenido (dedup de re-upload).

        Solo considera archivos no soft-deleted. Sirve para avisar al usuario que el
        archivo ya fue importado antes y evitar duplicar datos sin querer.

        EXCLUYE los uploads de chat (``purpose == "chat"``): esos se parsean de forma
        síncrona y llegan a DONE sin importarse a ventas/gastos, así que NO cuentan como
        "ya importado" y no deben bloquear una primera ingestión legítima del mismo archivo.
        """
        from app.persistence.models.file import PROCESSING_STATUS_DONE  # noqa: PLC0415

        q = select(UploadedFile).where(
            UploadedFile.tenant_id == tenant_id,
            UploadedFile.content_hash == content_hash,
            UploadedFile.processing_status == PROCESSING_STATUS_DONE,
            UploadedFile.purpose != "chat",
            UploadedFile.deleted_at.is_(None),
        )
        if exclude_id is not None:
            q = q.where(UploadedFile.id != exclude_id)
        q = q.order_by(UploadedFile.created_at.desc()).limit(1)
        result = await self._session.execute(q)
        return result.scalar_one_or_none()

    async def find_imported_by_filename(
        self,
        tenant_id: UUID,
        original_filename: str,
        exclude_content_hash: str | None = None,
    ) -> UploadedFile | None:
        """Busca un archivo YA importado (DONE) con el MISMO nombre pero contenido distinto.

        Sirve para avisar (NO bloquear) cuando el usuario resube una versión actualizada de
        un archivo ya importado: como el contenido difiere, el dedup por hash no lo frena, y
        reimportar todo duplica las filas repetidas. Excluye chat y, si se pasa
        ``exclude_content_hash``, el contenido idéntico (ese caso ya lo maneja el 409).
        """
        from app.persistence.models.file import PROCESSING_STATUS_DONE  # noqa: PLC0415

        q = select(UploadedFile).where(
            UploadedFile.tenant_id == tenant_id,
            UploadedFile.original_filename == original_filename,
            UploadedFile.processing_status == PROCESSING_STATUS_DONE,
            UploadedFile.purpose != "chat",
            UploadedFile.deleted_at.is_(None),
        )
        if exclude_content_hash is not None:
            q = q.where(UploadedFile.content_hash != exclude_content_hash)
        q = q.order_by(UploadedFile.created_at.desc()).limit(1)
        result = await self._session.execute(q)
        return result.scalar_one_or_none()

    async def list_by_tenant_filtered(
        self,
        tenant_id: UUID,
        processing_status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[UploadedFile]:
        """List files for a tenant, optionally filtered by processing_status.

        Soft-deleted files (deleted_at IS NOT NULL) are always excluded — el
        crudo se preserva en R2 pero el archivo no aparece en la UI.
        """
        q = select(UploadedFile).where(
            UploadedFile.tenant_id == tenant_id,
            UploadedFile.deleted_at.is_(None),
        )
        if processing_status is not None:
            q = q.where(UploadedFile.processing_status == processing_status)
        q = q.order_by(UploadedFile.created_at.desc()).limit(limit).offset(offset)
        result = await self._session.execute(q)
        return list(result.scalars().all())
