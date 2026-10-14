"""M·R: el stock inicial importado como COMPRA deja de ser gasto.

Se importa un catálogo de verdad con «La compré ahora» (``insert_confirmed_data``)
y se repara: el gasto «Stock inicial» queda anulado; el movimiento, el stock y el
costo, intactos; la elección guardada vuelve a «apertura».
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.application.services import reparacion_stock_inicial_service as mr
from app.application.services.deterministic_finance import calcular_flujo_neto_30d
from app.application.services.facts_provider import build_facts_service
from app.application.services.facts_service import Period
from app.application.services.reread_service import REPAIR_TYPE_REREAD
from app.application.services.stock_service import void_movement
from app.domain.business_time import today_ar
from app.domain.verticals import Vertical
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.file import UploadedFile
from app.persistence.models.ingestion_schema_decision import IngestionSchemaDecision
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.repair import DataRepairItem, DataRepairRun
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry
from app.persistence.models.user import User

_MAPEO = {"producto": "name", "cantidad": "stock_units", "costo": "unit_cost_ars"}
_CTX = "sheet:catalogo"


def _summary(stock_treatment: Any = None) -> dict[str, Any]:
    filas = [
        {"producto": "Vela aromática", "cantidad": "10", "costo": "1200", "__context__": _CTX},
        {"producto": "Sahumerio lavanda", "cantidad": "5", "costo": "800", "__context__": _CTX},
    ]
    summary: dict[str, Any] = {
        "file_type": "spreadsheet",
        "inferred_type": "stock",
        "multi_sheet": True,
        "has_producto": True,
        "stock_detectado": filas,
        "mapping_contexts": [
            {
                "context_id": _CTX,
                "label": "catalogo",
                "source_kind": "sheet",
                "entity_type": "product",
                "headers": ["producto", "cantidad", "costo"],
                "fields": None,
                "preview_rows": filas[:1],
                "row_count": 2,
            }
        ],
    }
    if stock_treatment is not None:
        summary["stock_treatment"] = stock_treatment
    return summary


def _compacto(summary: dict[str, Any]) -> dict[str, Any]:
    """Lo que el confirm GUARDA (``api/v1/ingestion.py``): sin filas ni
    ``mapping_contexts``. Un test que guarde el summary entero prueba un mundo que
    en producción no existe."""
    fuera = {"stock_detectado", "preview_rows", "mapping_contexts"}
    return {k: v for k, v in summary.items() if k not in fuera}


async def _importar_como_compra(
    db: AsyncSession,
    tenant: Tenant,
    user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> UploadedFile:
    """Un catálogo confirmado con «La compré ahora», como el de ASTERIA."""
    monkeypatch.setattr(importer, "ingestion_schema_decisions_enabled_for", lambda _t: True)
    eleccion = {_CTX: "purchase"}
    archivo = UploadedFile(
        tenant_id=tenant.tenant_id,
        uploaded_by=user.user_id,
        original_filename="catalogo.xlsx",
        s3_key="uploads/test/catalogo.xlsx",
        content_type="application/vnd.ms-excel",
        size_bytes=100,
        purpose="ingestion",
        status="uploaded",
        processing_status="done",
        parsed_summary_json=_compacto(_summary(eleccion)),
    )
    db.add(archivo)
    await db.flush()
    await importer.insert_confirmed_data(
        db,
        tenant.tenant_id,
        _summary(eleccion),
        {"productos": True},
        context_mappings={_CTX: _MAPEO},
        context_confirmed={_CTX: True},
        stock_treatment=eleccion,
        uploaded_file_id=archivo.id,
    )
    await db.flush()
    return archivo


async def _gastos_vivos(db: AsyncSession, tenant_id: uuid.UUID) -> list[ExpenseEntry]:
    return list(
        (
            await db.execute(
                select(ExpenseEntry).where(
                    ExpenseEntry.tenant_id == tenant_id, ExpenseEntry.voided_at.is_(None)
                )
            )
        ).scalars()
    )


async def _foto_inventario(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, Any]:
    movimientos = sorted(
        (m.id, m.movement_type, m.qty, m.unit_cost, m.occurred_at, m.voided_at)
        for m in (
            await db.execute(
                select(InventoryMovement).where(InventoryMovement.tenant_id == tenant_id)
            )
        ).scalars()
    )
    productos = sorted(
        (p.id, p.stock_units, p.unit_cost_ars)
        for p in (
            await db.execute(select(Product).where(Product.tenant_id == tenant_id))
        ).scalars()
    )
    return {"movimientos": movimientos, "productos": productos}


def test_los_literales_son_los_del_import() -> None:
    assert mr.COMPRA == importer.STOCK_TREATMENT_PURCHASE
    assert mr.APERTURA == importer.STOCK_TREATMENT_OPENING_BALANCE


async def test_anula_el_gasto_y_deja_intacto_el_inventario(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = sample_tenant.tenant_id
    archivo = await _importar_como_compra(db_session, sample_tenant, sample_user, monkeypatch)
    gastos = await _gastos_vivos(db_session, tid)
    assert {g.amount for g in gastos} == {Decimal("12000.00"), Decimal("4000.00")}
    inventario_antes = await _foto_inventario(db_session, tid)
    assert {m[1] for m in inventario_antes["movimientos"]} == {"purchase"}
    flujo_antes = (await calcular_flujo_neto_30d(tid, db_session))["flujo_neto"]

    r = await mr.reparar(db_session, tid, aplicar=True)

    assert r.anulados == 2
    assert r.monto == Decimal("16000.00")
    assert not r.excluidos
    assert await _gastos_vivos(db_session, tid) == []
    for g in gastos:
        await db_session.refresh(g)
        assert g.void_reason == "REPAIR_MISCLASSIFIED_IMPORT"
        assert g.voided_by_repair_run_id == r.run_id
    # El movimiento sigue siendo compra: Proveedores, costo de referencia,
    # integridad y deduplicación no se enteran.
    assert await _foto_inventario(db_session, tid) == inventario_antes
    # La plata que nunca salió vuelve al flujo.
    assert r.flujo_antes == flujo_antes
    assert r.flujo_despues == flujo_antes + Decimal("16000.00")

    run = await db_session.get(DataRepairRun, r.run_id)
    assert run is not None and run.status == "APPLIED" and run.repair_type == mr.REPAIR_TYPE
    items = (
        await db_session.execute(select(DataRepairItem).where(DataRepairItem.run_id == run.id))
    ).scalars().all()
    assert len(items) == 2
    assert {(it.action, it.source_file_id) for it in items} == {
        ("RECLASSIFY_EXPENSE", archivo.id)
    }
    auditoria = await db_session.scalar(
        select(func.count()).where(
            DecisionAuditLog.tenant_id == tid,
            DecisionAuditLog.decision_type == "REPAIR_OPENING_STOCK_EXPENSES",
        )
    )
    assert auditoria == 1


async def test_facts_deja_de_contar_la_mercaderia(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = sample_tenant.tenant_id
    await _importar_como_compra(db_session, sample_tenant, sample_user, monkeypatch)
    periodo = Period.last_n_days(30, today=today_ar())

    async def mercaderia() -> Any:
        svc = await build_facts_service(
            db_session, tid, periodo, vertical=Vertical.DECORACION_HOGAR
        )
        hechos = svc.gastos_por_categoria(str(tid), periodo)
        return {h.fact_id: h.value for h in hechos}

    assert (await mercaderia()).get(f"gastos_inventory_{periodo.label}") == 16000.0
    await mr.reparar(db_session, tid, aplicar=True)
    assert f"gastos_inventory_{periodo.label}" not in await mercaderia()


async def test_reparar_marca_el_costo_promedio_pendiente(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M·P2b: el catálogo reparado pasa a ser apertura en el costo promedio, así
    que el historial de esos productos queda entero pendiente."""
    from datetime import date as _date  # noqa: PLC0415

    from app.jobs import recalcular_costo_promedio as job  # noqa: PLC0415
    from app.persistence import costo_promedio_pendiente as pend  # noqa: PLC0415
    from app.persistence.models.product_cost import ProductCostState  # noqa: PLC0415

    monkeypatch.setattr(pend, "ENCOLAR_EN_HILO", False)
    monkeypatch.setattr(job.recalcular_costo_promedio, "apply_async", lambda *_, **__: None)
    tid = sample_tenant.tenant_id
    await _importar_como_compra(db_session, sample_tenant, sample_user, monkeypatch)
    await db_session.commit()

    r = await mr.reparar(db_session, tid, aplicar=True)
    await db_session.commit()

    productos = {c.product_id for c in r.candidatos}
    estados = (
        await db_session.execute(
            select(ProductCostState)
            .where(ProductCostState.product_id.in_(productos))
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    assert len(estados) == 2 and {e.pendiente_desde for e in estados} == {_date.min}


async def test_repetir_no_hace_nada(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = sample_tenant.tenant_id
    await _importar_como_compra(db_session, sample_tenant, sample_user, monkeypatch)
    await mr.reparar(db_session, tid, aplicar=True)
    segunda = await mr.reparar(db_session, tid, aplicar=True)
    assert segunda.anulados == 0 and segunda.run_id is None


async def _releer(db: AsyncSession, tenant_id: uuid.UUID, archivo: UploadedFile) -> int:
    """Lo que hace «Volver a leer» sin borrador: anula los movimientos del archivo
    (devolviendo el stock) y reimporta el archivo re-parseado con la elección del
    summary guardado. Devuelve cuántos gastos vivos quedan."""
    movimientos = (
        await db.execute(
            select(InventoryMovement).where(
                InventoryMovement.source_upload_id == archivo.id,
                InventoryMovement.voided_at.is_(None),
            )
        )
    ).scalars().all()
    for m in movimientos:
        await void_movement(m, db)
    await db.refresh(archivo)
    fresco = _summary(archivo.parsed_summary_json.get("stock_treatment"))
    await importer.insert_confirmed_data(
        db,
        tenant_id,
        fresco,
        {"productos": True},
        context_mappings={_CTX: _MAPEO},
        context_confirmed={_CTX: True},
        uploaded_file_id=archivo.id,
    )
    await db.flush()
    return len(await _gastos_vivos(db, tenant_id))


async def test_sin_reparar_la_relectura_recrea_los_gastos(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control del test de abajo: la emulación de la relectura SÍ crea gastos
    cuando la elección guardada sigue en «compra»."""
    archivo = await _importar_como_compra(db_session, sample_tenant, sample_user, monkeypatch)
    assert await _releer(db_session, sample_tenant.tenant_id, archivo) == 4


async def test_la_eleccion_guardada_vuelve_a_apertura_y_no_recrea_gastos(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = sample_tenant.tenant_id
    archivo = await _importar_como_compra(db_session, sample_tenant, sample_user, monkeypatch)
    # El summary guardado no trae ``mapping_contexts``: la decisión recordada se
    # encuentra por tenant, no reconstruyendo la huella.
    assert "mapping_contexts" not in archivo.parsed_summary_json
    decision = (
        await db_session.execute(
            select(IngestionSchemaDecision).where(
                IngestionSchemaDecision.tenant_id == tid,
                IngestionSchemaDecision.decision_type == "stock_treatment",
            )
        )
    ).scalar_one()
    assert decision.payload == {"treatment": "purchase"}

    r = await mr.reparar(db_session, tid, aplicar=True)
    assert r.decisiones_compra == 1

    await db_session.refresh(archivo)
    await db_session.refresh(decision)
    assert archivo.parsed_summary_json["stock_treatment"] == {_CTX: "opening_balance"}
    assert decision.payload == {"treatment": "opening_balance"}
    assert await _releer(db_session, tid, archivo) == 0


async def test_revert_restaura_todo(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = sample_tenant.tenant_id
    archivo = await _importar_como_compra(db_session, sample_tenant, sample_user, monkeypatch)
    r = await mr.reparar(db_session, tid, aplicar=True)
    assert r.run_id is not None

    res = await mr.revertir(db_session, r.run_id)

    assert (res.revividos, res.salteados, res.elecciones, res.decisiones) == (2, 0, 1, 1)
    assert len(await _gastos_vivos(db_session, tid)) == 2
    await db_session.refresh(archivo)
    assert archivo.parsed_summary_json["stock_treatment"] == {_CTX: "purchase"}
    run = await db_session.get(DataRepairRun, r.run_id)
    assert run is not None and run.status == "REVERTED"
    # Revertir dos veces no hace nada.
    assert (await mr.revertir(db_session, r.run_id)).revividos == 0


async def test_revert_no_pisa_lo_que_cambio_despues(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = sample_tenant.tenant_id
    archivo = await _importar_como_compra(db_session, sample_tenant, sample_user, monkeypatch)
    r = await mr.reparar(db_session, tid, aplicar=True)
    assert r.run_id is not None
    # Después de la reparación, un gasto se anula por otra razón y el usuario
    # vuelve a elegir la hoja.
    gasto = (
        await db_session.execute(
            select(ExpenseEntry).where(ExpenseEntry.voided_by_repair_run_id == r.run_id)
        )
    ).scalars().first()
    assert gasto is not None
    gasto.void_reason = "USER_CANCELLED"
    gasto.voided_by_repair_run_id = None
    await db_session.refresh(archivo)
    archivo.parsed_summary_json = {**archivo.parsed_summary_json, "stock_treatment": "x"}
    await db_session.flush()

    res = await mr.revertir(db_session, r.run_id)

    assert (res.revividos, res.salteados, res.elecciones) == (1, 1, 0)
    await db_session.refresh(archivo)
    assert archivo.parsed_summary_json["stock_treatment"] == "x"


async def test_revert_saltea_el_gasto_de_un_movimiento_reemplazado(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Una relectura posterior anuló el movimiento: revivir su gasto dejaría un
    gasto sin par que nada vuelve a anular. La elección del archivo tampoco vuelve."""
    tid = sample_tenant.tenant_id
    archivo = await _importar_como_compra(db_session, sample_tenant, sample_user, monkeypatch)
    r = await mr.reparar(db_session, tid, aplicar=True)
    assert r.run_id is not None
    assert await _releer(db_session, tid, archivo) == 0

    res = await mr.revertir(db_session, r.run_id)

    assert (res.revividos, res.salteados, res.elecciones) == (0, 2, 0)
    assert await _gastos_vivos(db_session, tid) == []
    await db_session.refresh(archivo)
    assert archivo.parsed_summary_json["stock_treatment"] == {_CTX: "opening_balance"}


@pytest.mark.parametrize(
    ("monto", "costo_guardado", "qty", "coincide"),
    [
        # Costo 123,456 del archivo → gasto 1234,56; el movimiento guarda 123,46.
        (Decimal("1234.56"), Decimal("123.46"), 10, True),
        (Decimal("1240.00"), Decimal("123.46"), 10, False),
        (Decimal("16000.00"), Decimal("1200.00"), 10, False),
    ],
)
def test_importe_tolera_el_redondeo_del_costo(
    monto: Decimal, costo_guardado: Decimal, qty: int, coincide: bool
) -> None:
    m = InventoryMovement(unit_cost=costo_guardado, qty=qty)
    assert mr._importe_coincide(monto, m, 1) is coincide


async def test_lo_dudoso_no_se_toca(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = sample_tenant.tenant_id
    await _importar_como_compra(db_session, sample_tenant, sample_user, monkeypatch)
    editado, cambiado = sorted(await _gastos_vivos(db_session, tid), key=lambda g: g.amount)
    editado.has_user_edits = True
    cambiado.amount = Decimal("99.00")
    # Uno sin archivo, cargado por otro camino.
    db_session.add(
        ExpenseEntry(
            tenant_id=tid,
            amount=Decimal("500.00"),
            category="INVENTORY",
            expense_type="COGS",
            transaction_date=datetime.now(UTC).replace(tzinfo=None),
            description="Stock inicial: suelto",
            payment_method="transfer",
            provenance="REAL",
            product_id=editado.product_id,
        )
    )
    await db_session.flush()

    r = await mr.reparar(db_session, tid, aplicar=True)

    assert r.anulados == 0
    assert r.excluidos == {
        mr.MOTIVO_EDITADO: 1,
        mr.MOTIVO_IMPORTE: 1,
        mr.MOTIVO_SIN_ARCHIVO: 1,
    }
    assert len(await _gastos_vivos(db_session, tid)) == 3


async def test_otro_tenant_no_se_toca(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _importar_como_compra(db_session, sample_tenant, sample_user, monkeypatch)
    r = await mr.reparar(db_session, uuid.uuid4(), aplicar=True)
    assert r.anulados == 0
    assert len(await _gastos_vivos(db_session, sample_tenant.tenant_id)) == 2


@pytest.mark.parametrize(
    ("estado", "motivo"),
    [("APPLIED", mr.MOTIVO_RELECTURA), ("NEEDS_REVIEW", mr.MOTIVO_SESION)],
)
async def test_archivo_con_relectura_queda_bloqueado(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
    monkeypatch: pytest.MonkeyPatch,
    estado: str,
    motivo: str,
) -> None:
    tid = sample_tenant.tenant_id
    archivo = await _importar_como_compra(db_session, sample_tenant, sample_user, monkeypatch)
    db_session.add(
        DataRepairRun(
            tenant_id=tid,
            repair_type=REPAIR_TYPE_REREAD,
            status=estado,
            dry_run=False,
            details_json={"file_id": str(archivo.id)},
        )
    )
    await db_session.flush()

    r = await mr.reparar(db_session, tid, aplicar=True)

    assert r.anulados == 0
    assert r.excluidos == {motivo: 2}
    assert r.archivos[archivo.id].bloqueo == motivo
    await db_session.refresh(archivo)
    assert archivo.parsed_summary_json["stock_treatment"] == {_CTX: "purchase"}


async def test_filtro_por_archivo(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = sample_tenant.tenant_id
    await _importar_como_compra(db_session, sample_tenant, sample_user, monkeypatch)
    r = await mr.reparar(db_session, tid, aplicar=True, file_ids={uuid.uuid4()})
    assert r.anulados == 0
    assert len(await _gastos_vivos(db_session, tid)) == 2
