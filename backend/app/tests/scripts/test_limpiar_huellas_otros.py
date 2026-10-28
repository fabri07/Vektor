"""``scripts/limpiar_huellas_otros.py`` (V-16): las huellas del lote de «Otros»
que el bug viejo dejó puestas en filas que nunca se importaron.

Las protecciones:
- el análisis (default) no borra nada;
- una huella con una venta o un gasto VIVO nacido de la fila nunca se borra;
- sólo se miran filas PENDING del negocio pedido;
- la corrida queda en ``data_repair_runs`` y se puede revertir.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from limpiar_huellas_otros import correr, parser  # noqa: E402

from app.application.services.ingestion_import_service import (  # noqa: E402
    ancla_de_fila_de_otros,
)
from app.application.services.reparacion_huellas_otros_service import (  # noqa: E402
    REPAIR_TYPE,
)
from app.persistence.models.memory import OperationFingerprint  # noqa: E402
from app.persistence.models.repair import DataRepairRun  # noqa: E402
from app.persistence.models.tenant import Tenant  # noqa: E402
from app.persistence.models.transaction import ExpenseEntry, SaleEntry  # noqa: E402
from app.persistence.models.unclassified_record import (  # noqa: E402
    UnclassifiedRecord,
    unclassified_row_ref,
)

pytestmark = pytest.mark.asyncio

_GASTO = {"fecha": "2024-05-01", "detalle": "Pago luz", "monto": "15000", "categoria": "Luz"}


@pytest.fixture(autouse=True)
def _sin_celery(mock_score_trigger: Any) -> None:
    pass


def _args(*argv: str) -> argparse.Namespace:
    return parser().parse_args(list(argv))


async def _fila_con_huella(
    db: AsyncSession, tenant: Tenant, *, estado: str = "PENDING", entidad: str = "expense"
) -> UnclassifiedRecord:
    rec = UnclassifiedRecord(
        tenant_id=tenant.tenant_id,
        source="ingestion",
        context_label="Fecha ilegible",
        suggested_entity=entidad,
        status=estado,
        row_data=dict(_GASTO),
    )
    db.add(rec)
    await db.flush()
    # La huella como la registraba el lote viejo, antes de las compuertas.
    db.add(
        OperationFingerprint(
            tenant_id=tenant.tenant_id,
            fingerprint=hashlib.sha256(
                ancla_de_fila_de_otros(tenant.tenant_id, rec.id).encode()
            ).hexdigest(),
            action_type="IMPORT_ROW",
        )
    )
    await db.commit()
    return rec


async def _huella_de(db: AsyncSession, tenant_id: uuid.UUID, record_id: uuid.UUID) -> bool:
    fp = hashlib.sha256(ancla_de_fila_de_otros(tenant_id, record_id).encode()).hexdigest()
    db.expunge_all()
    return (
        await db.execute(
            select(OperationFingerprint.id).where(OperationFingerprint.fingerprint == fp)
        )
    ).first() is not None


async def _venta_de_la_fila(
    db: AsyncSession, tenant: Tenant, rec: UnclassifiedRecord, *, anulada: bool = False
) -> None:
    db.add(
        SaleEntry(
            tenant_id=tenant.tenant_id,
            amount=Decimal("100"),
            quantity=1,
            transaction_date=datetime(2024, 5, 1, 12),
            payment_method="cash",
            source_row_ref=unclassified_row_ref(rec.id),
            voided_at=datetime.now(UTC) if anulada else None,
            void_reason="USER_CANCELLED" if anulada else None,
        )
    )
    await db.commit()


async def test_el_analisis_no_borra_nada(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    rec = await _fila_con_huella(db_session, sample_tenant)
    rec_id, tid = rec.id, sample_tenant.tenant_id

    [resumen] = await correr(db_session, _args("--tenant-id", str(sample_tenant.tenant_id)))

    assert [h.record_id for h in resumen.huerfanas] == [rec_id]
    assert await _huella_de(db_session, tid, rec_id)
    assert (await db_session.execute(select(DataRepairRun))).first() is None


async def test_apply_borra_solo_las_huerfanas_y_el_lote_vuelve_a_juzgarla(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    second_tenant: Tenant,
) -> None:
    huerfana = await _fila_con_huella(db_session, sample_tenant)
    con_venta = await _fila_con_huella(db_session, sample_tenant, entidad="sale")
    await _venta_de_la_fila(db_session, sample_tenant, con_venta)
    importada = await _fila_con_huella(db_session, sample_tenant, estado="IMPORTED")
    de_otro_negocio = await _fila_con_huella(db_session, second_tenant)

    # Antes de limpiar, el lote la saltea («ya importada»).
    r = await client.post("/api/v1/others/bulk-import", json={}, headers=auth_headers)
    assert r.json()["skipped"] == 2 and r.json()["imported_expenses"] == 0, r.json()

    [resumen] = await correr(
        db_session, _args("--tenant-id", str(sample_tenant.tenant_id), "--apply")
    )

    assert [h.record_id for h in resumen.huerfanas] == [huerfana.id]
    assert [h.record_id for h in resumen.con_respaldo] == [con_venta.id]
    assert not await _huella_de(db_session, sample_tenant.tenant_id, huerfana.id)
    assert await _huella_de(db_session, sample_tenant.tenant_id, con_venta.id)
    assert await _huella_de(db_session, sample_tenant.tenant_id, importada.id)
    assert await _huella_de(db_session, second_tenant.tenant_id, de_otro_negocio.id)
    run = await db_session.get(DataRepairRun, resumen.run_id)
    assert run is not None and run.repair_type == REPAIR_TYPE and run.status == "APPLIED"
    assert run.details_json is not None
    assert [h["record_id"] for h in run.details_json["huellas"]] == [str(huerfana.id)]

    # Liberada, el lote la importa con las compuertas de hoy.
    r = await client.post("/api/v1/others/bulk-import", json={}, headers=auth_headers)
    assert r.json()["imported_expenses"] == 1, r.json()
    db_session.expunge_all()
    gasto = (
        await db_session.execute(
            select(ExpenseEntry).where(ExpenseEntry.tenant_id == sample_tenant.tenant_id)
        )
    ).scalar_one()
    assert gasto.amount == Decimal("15000")
    fila = await db_session.get(UnclassifiedRecord, huerfana.id)
    assert fila is not None and fila.status == "IMPORTED"


async def test_una_venta_anulada_no_respalda_la_huella(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    rec = await _fila_con_huella(db_session, sample_tenant, entidad="sale")
    await _venta_de_la_fila(db_session, sample_tenant, rec, anulada=True)

    [resumen] = await correr(
        db_session, _args("--tenant-id", str(sample_tenant.tenant_id), "--apply")
    )

    assert [h.record_id for h in resumen.huerfanas] == [rec.id]
    assert not await _huella_de(db_session, sample_tenant.tenant_id, rec.id)


async def test_todos_recorre_cada_negocio_y_revert_repone(
    db_session: AsyncSession, sample_tenant: Tenant, second_tenant: Tenant
) -> None:
    a = await _fila_con_huella(db_session, sample_tenant)
    b = await _fila_con_huella(db_session, second_tenant)

    resumenes = await correr(db_session, _args("--todos", "--apply"))

    assert {r.tenant_id for r in resumenes} == {sample_tenant.tenant_id, second_tenant.tenant_id}
    assert not await _huella_de(db_session, sample_tenant.tenant_id, a.id)
    assert not await _huella_de(db_session, second_tenant.tenant_id, b.id)

    run_a = next(r.run_id for r in resumenes if r.tenant_id == sample_tenant.tenant_id)
    assert run_a is not None
    await correr(db_session, _args("--revert", str(run_a)))

    assert await _huella_de(db_session, sample_tenant.tenant_id, a.id)
    assert not await _huella_de(db_session, second_tenant.tenant_id, b.id)
    run = await db_session.get(DataRepairRun, run_a)
    assert run is not None and run.status == "REVERTED"
    with pytest.raises(ValueError):
        await correr(db_session, _args("--revert", str(run_a)))


async def test_hay_que_decir_a_quien() -> None:
    with pytest.raises(SystemExit):
        parser().parse_args([])
    with pytest.raises(SystemExit):
        parser().parse_args(["--apply"])
    with pytest.raises(SystemExit):
        parser().parse_args(["--todos", "--tenant-id", str(uuid.uuid4())])
