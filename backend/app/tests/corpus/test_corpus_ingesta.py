"""El corpus corre entero contra el pipeline real y se compara con su baseline.

Cada caso sube el archivo, lo parsea el parser de producción y lo confirma por
HTTP; lo que se afirma son las filas persistidas contra la expectativa escrita a
mano en ``casos.py``. Es la compuerta que el resto de los tests no da: los tests
por defecto prueban un defecto concreto, y éste prueba que el conjunto de
decisiones sigue siendo el mismo cuando se toca cualquiera de ellas.

Un caso que falla no se "actualiza": se decide si la política cambió a propósito
—y entonces se sube ``revisado_en`` y se explica en el commit— o si se rompió.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.file_parsing import parse_uploaded_content
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord
from app.tests.corpus.casos import CASOS, CasoDeCorpus, ProductoEsperado, VentaEsperada


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker, cada confirm paga ~5s de reintentos de kombu (fail-safe)."""


async def _subir(
    db_session: AsyncSession, tenant: Tenant, caso: CasoDeCorpus
) -> UploadedFile:
    record = UploadedFile(
        tenant_id=tenant.tenant_id,
        uploaded_by=None,
        original_filename=caso.archivo,
        s3_key=f"uploads/corpus/{uuid.uuid4()}/{caso.archivo}",
        content_type=caso.mime,
        size_bytes=len(caso.contenido),
        purpose="ingestion",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=parse_uploaded_content(
            caso.contenido, caso.mime, caso.archivo
        ),
    )
    db_session.add(record)
    await db_session.commit()
    return record


async def _ventas_reales(
    db_session: AsyncSession, tenant: Tenant
) -> list[VentaEsperada]:
    filas = (
        (
            await db_session.execute(
                select(SaleEntry).where(
                    SaleEntry.tenant_id == tenant.tenant_id,
                    SaleEntry.voided_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return sorted(
        (
            VentaEsperada(
                fecha=f.transaction_date.date(),
                monto=Decimal(str(f.amount)),
                cantidad=int(f.quantity),
            )
            for f in filas
        ),
        key=lambda v: (v.fecha, v.monto),
    )


async def _productos_reales(
    db_session: AsyncSession, tenant: Tenant
) -> list[ProductoEsperado]:
    filas = (
        (
            await db_session.execute(
                select(Product).where(Product.tenant_id == tenant.tenant_id)
            )
        )
        .scalars()
        .all()
    )
    return sorted(
        (
            ProductoEsperado(
                nombre=p.name,
                stock=int(p.stock_units or 0),
                costo=Decimal(str(p.unit_cost_ars)) if p.unit_cost_ars is not None else None,
                precio=(
                    Decimal(str(p.sale_price_ars)) if p.sale_price_ars is not None else None
                ),
            )
            for p in filas
        ),
        key=lambda p: p.nombre,
    )


@pytest.mark.parametrize("caso", CASOS, ids=lambda c: c.nombre)
async def test_el_corpus_sigue_dando_el_mismo_resultado(
    caso: CasoDeCorpus,
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    record = await _subir(db_session, sample_tenant, caso)

    resp = await client.post(
        f"/api/v1/ingestion/files/{record.id}/confirm",
        json={"column_mappings": caso.mapeo, "confirmed_fields": caso.confirmado},
        headers=auth_headers,
    )
    assert resp.status_code == 200, f"{caso.nombre}: {resp.text}"

    assert await _ventas_reales(db_session, sample_tenant) == sorted(
        caso.ventas, key=lambda v: (v.fecha, v.monto)
    ), f"{caso.nombre} — {caso.porque}"

    if caso.productos:
        assert await _productos_reales(db_session, sample_tenant) == sorted(
            caso.productos, key=lambda p: p.nombre
        ), f"{caso.nombre} — {caso.porque}"

    otros = (
        (
            await db_session.execute(
                select(UnclassifiedRecord).where(
                    UnclassifiedRecord.tenant_id == sample_tenant.tenant_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(otros) == caso.otros, (
        f"{caso.nombre}: se esperaban {caso.otros} filas en Otros y hay "
        f"{len(otros)} — {[o.context_label for o in otros]}"
    )


def test_cada_caso_declara_por_que_existe_y_cuando_se_reviso() -> None:
    """La disciplina que hace útil al corpus: una expectativa sin justificación
    escrita se "actualiza" sola la primera vez que falla, y ahí deja de medir
    nada. Este test es barato y evita exactamente eso."""
    assert CASOS, "un corpus vacío pasa siempre"
    for caso in CASOS:
        assert len(caso.porque) > 40, f"{caso.nombre} no dice qué decisión fija"
        assert caso.revisado_en, f"{caso.nombre} no dice cuándo se revisó"
    assert len({c.nombre for c in CASOS}) == len(CASOS), "hay nombres repetidos"
