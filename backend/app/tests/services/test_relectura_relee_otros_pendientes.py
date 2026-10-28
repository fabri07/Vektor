"""I4-6 + I1-4 — la relectura vuelve a leer lo que el import mandó a «Otros».

**I4-6.** Una fila que el import mandó a «Otros» (fecha ilegible, sin monto,
fila de total, conflicto, venta sin compra) registra su huella en el MISMO ancla
de la fila: es output persistido. La relectura sólo liberaba la huella de las
ventas y gastos no editados y la de las filas de «Otros» ya CLASIFICADAS (F-O.2),
así que una fila PENDIENTE nunca se volvía a leer: el usuario corregía el mapeo o
el archivo, releía, y la venta no entraba —el preview la contaba «sin cambios»—.
Una carga limpia del mismo archivo sí la importaba.

**I1-4.** Las filas que F8 manda a «Otros» se BORRAN del bucket antes del
import, así que las que siguen corren un lugar: la captura guardaba como
``__row_ref__`` el ancla de su índice ORIGINAL, que es la de la fila siguiente
en la numeración del import. La relectura tomaba esa ref para reconocer la fila
clasificada a mano (F-O.2) y, como la fila siguiente siempre vuelve a entrar,
anulaba la clasificada como «reemplazada» sin que su fila hubiera entrado. En
la tabla suelta pasaba lo contrario: la captura usa el contexto ``table`` y el
import el contexto vacío, nunca coincidían, y una fila corregida quedaba
duplicada con la clasificada.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import reread_service
from app.application.services.column_risk import apply_column_risk_decisions
from app.application.services.file_parsing import parse_uploaded_content
from app.application.services.ingestion_import_service import (
    RISK_REF_KEY,
    ROW_REF_KEY,
    _capture_column_risk_rows,
    default_confirmed_fields,
    huellas_presentes,
    insert_confirmed_data,
)
from app.application.services.inventory_replay_service import run_inventory_replay
from app.domain.inventory_effect import HISTORICAL_REPLAY
from app.integrations.s3 import S3Client
from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.models.unclassified_record import (
    UNCLASSIFIED_STATUS_DISMISSED,
    UNCLASSIFIED_STATUS_IMPORTED,
    UNCLASSIFIED_STATUS_PENDING,
    UnclassifiedRecord,
    unclassified_row_ref,
)
from app.schemas.ingestion import ColumnRiskDecision
from app.tests._respaldo import producto_con_compra
from app.tests.conftest import add_business_profile

#: La segunda fila tiene la fecha ilegible: va a «Otros» y queda PENDIENTE. La
#: primera es el control (entra siempre).
_CSV = (
    b"fecha,producto,cantidad,monto,cliente\n"
    b"2026-01-05,Vela aromatica,3,1500,Ana\n"
    b"cuando pueda,Vela aromatica,2,900,Luis\n"
)
#: El mismo archivo, corregido: la fila 2 ahora tiene fecha.
_CSV_CORREGIDO = (
    b"fecha,producto,cantidad,monto,cliente\n"
    b"2026-01-05,Vela aromatica,3,1500,Ana\n"
    b"2026-01-06,Vela aromatica,2,900,Luis\n"
)
_PRODUCTO = "Vela aromatica"


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker, cada apply paga los reintentos de kombu."""


@pytest_asyncio.fixture
async def tenant(db_session: AsyncSession) -> Tenant:
    t = Tenant(
        tenant_id=uuid.uuid4(),
        legal_name="Kiosco Test",
        display_name="Kiosco Test",
        currency="ARS",
        pricing_reference_mode="MEP",
        status="ACTIVE",
    )
    db_session.add(t)
    await db_session.flush()
    await add_business_profile(db_session, t.tenant_id)
    await db_session.commit()
    return t


@pytest_asyncio.fixture
async def producto(db_session: AsyncSession, tenant: Tenant) -> Product:
    # B·5e: con compra previa, si no la fila 1 también iría a «Otros».
    return await producto_con_compra(
        db_session,
        tenant.tenant_id,
        nombre=_PRODUCTO,
        stock=10,
        costo=Decimal("300"),
        precio=Decimal("500"),
    )


def _servir(monkeypatch: pytest.MonkeyPatch, contenido: bytes) -> None:
    async def _fake_download(self: S3Client, key: str) -> bytes:  # noqa: ARG001
        return contenido

    monkeypatch.setattr(S3Client, "download", _fake_download)


async def _archivo_de_ventas(
    session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> UploadedFile:
    _servir(monkeypatch, _CSV)
    summary = parse_uploaded_content(_CSV, "text/csv", "ventas.csv")
    f = UploadedFile(
        id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        uploaded_by=None,
        original_filename="ventas.csv",
        s3_key=f"tenants/{tenant.tenant_id}/ventas.csv",
        content_type="text/csv",
        size_bytes=len(_CSV),
        purpose="ventas",
        processing_status=PROCESSING_STATUS_DONE,
        parsed_summary_json={
            "inferred_type": summary.get("inferred_type"),
            "confirmed_fields": {"ventas": True},
        },
    )
    session.add(f)
    await session.commit()
    counts = await insert_confirmed_data(
        session,
        tenant.tenant_id,
        summary,
        {"ventas": True},
        source="ingestion",
        uploaded_file_id=f.id,
        inventory_effect={"": HISTORICAL_REPLAY},
    )
    await session.flush()
    await run_inventory_replay(session, tenant.tenant_id, f.id, context_ids=None, apply=True)
    await session.commit()
    assert counts["ventas"] == 1 and counts["otros"] == 1, counts
    return f


async def _ventas_vivas(session: AsyncSession, tenant: Tenant) -> list[SaleEntry]:
    res = await session.execute(
        select(SaleEntry).where(
            SaleEntry.tenant_id == tenant.tenant_id, SaleEntry.voided_at.is_(None)
        )
    )
    return list(res.scalars().all())


async def _otros(
    session: AsyncSession, tenant: Tenant, file_id: uuid.UUID, status: str | None = None
) -> list[UnclassifiedRecord]:
    q = select(UnclassifiedRecord).where(
        UnclassifiedRecord.tenant_id == tenant.tenant_id,
        UnclassifiedRecord.uploaded_file_id == file_id,
    )
    if status is not None:
        q = q.where(UnclassifiedRecord.status == status)
    return list((await session.execute(q)).scalars().all())


# ── I4-6: lo PENDIENTE en «Otros» se vuelve a leer ────────────────────────────


async def test_la_relectura_importa_la_fila_pendiente_que_ahora_sabe_leer(
    db_session: AsyncSession,
    tenant: Tenant,
    producto: Product,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El escenario de I4-6: se corrige el archivo, se relee y la venta entra.

    Antes la huella de la captura tapaba la fila: quedaba una sola venta y la
    fila seguía pendiente en la bandeja, como si el archivo no hubiera cambiado.
    """
    archivo = await _archivo_de_ventas(db_session, tenant, monkeypatch)
    pendiente_id = (await _otros(db_session, tenant, archivo.id))[0].id

    _servir(monkeypatch, _CSV_CORREGIDO)
    await reread_service.apply_reread(db_session, archivo.id, tenant.tenant_id)
    await db_session.commit()

    vivas = await _ventas_vivas(db_session, tenant)
    assert sorted(Decimal(v.amount) for v in vivas) == [Decimal("900"), Decimal("1500")]
    # La fila que entró ya no está pendiente: queda descartada (rastro, no borrado).
    assert await _otros(db_session, tenant, archivo.id, UNCLASSIFIED_STATUS_PENDING) == []
    viejo = await db_session.get(UnclassifiedRecord, pendiente_id)
    assert viejo is not None
    assert viejo.status == UNCLASSIFIED_STATUS_DISMISSED
    assert viejo.resolved_at is not None


async def test_la_fila_que_sigue_sin_leerse_queda_una_sola_vez_en_otros(
    db_session: AsyncSession,
    tenant: Tenant,
    producto: Product,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Liberar la huella no puede duplicar la bandeja: si la fila vuelve a caer,
    queda UN pendiente, y su huella sigue registrada para el próximo import."""
    archivo = await _archivo_de_ventas(db_session, tenant, monkeypatch)

    await reread_service.apply_reread(db_session, archivo.id, tenant.tenant_id)
    await db_session.commit()
    await reread_service.apply_reread(db_session, archivo.id, tenant.tenant_id)
    await db_session.commit()

    pendientes = await _otros(db_session, tenant, archivo.id, UNCLASSIFIED_STATUS_PENDING)
    assert len(pendientes) == 1
    assert len(await _ventas_vivas(db_session, tenant)) == 1
    ref = str(pendientes[0].row_data[ROW_REF_KEY])
    assert await huellas_presentes(db_session, tenant.tenant_id, {ref}) == {ref}


async def test_el_preview_cuenta_como_nueva_la_fila_pendiente(
    db_session: AsyncSession,
    tenant: Tenant,
    producto: Product,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El preview decía «sin cambios» para la fila que el apply sí va a cargar."""
    archivo = await _archivo_de_ventas(db_session, tenant, monkeypatch)

    _servir(monkeypatch, _CSV_CORREGIDO)
    preview = await reread_service.preview_reread(db_session, archivo.id, tenant.tenant_id)

    assert preview.new == 1
    assert preview.unchanged == 0


async def test_deshacer_devuelve_la_fila_a_pendiente(
    db_session: AsyncSession,
    tenant: Tenant,
    producto: Product,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """I4-4 sigue valiendo: deshacer saca la venta que entró y la fila vuelve a
    la bandeja, con su huella (como antes de releer)."""
    archivo = await _archivo_de_ventas(db_session, tenant, monkeypatch)
    pendiente_id = (await _otros(db_session, tenant, archivo.id))[0].id

    _servir(monkeypatch, _CSV_CORREGIDO)
    resultado = await reread_service.apply_reread(db_session, archivo.id, tenant.tenant_id)
    await db_session.commit()
    await reread_service.undo_reread(db_session, resultado.run_id, tenant.tenant_id)
    await db_session.commit()

    vivas = await _ventas_vivas(db_session, tenant)
    assert [Decimal(v.amount) for v in vivas] == [Decimal("1500")]
    pendientes = await _otros(db_session, tenant, archivo.id, UNCLASSIFIED_STATUS_PENDING)
    assert [p.id for p in pendientes] == [pendiente_id]
    ref = str(pendientes[0].row_data[ROW_REF_KEY])
    assert await huellas_presentes(db_session, tenant.tenant_id, {ref}) == {ref}


async def test_el_pendiente_cuya_fila_ya_no_esta_conserva_su_huella(
    db_session: AsyncSession,
    tenant: Tenant,
    producto: Product,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si nada vuelve a tomar la fila (el archivo ya no la trae), el pendiente
    queda como estaba, huella incluida: liberarla para releer no puede dejar la
    fila sin su marca de «ya cargada»."""
    archivo = await _archivo_de_ventas(db_session, tenant, monkeypatch)
    pendiente = (await _otros(db_session, tenant, archivo.id))[0]
    ref = str(pendiente.row_data[ROW_REF_KEY])

    _servir(
        monkeypatch,
        b"fecha,producto,cantidad,monto,cliente\n2026-01-05,Vela aromatica,3,1500,Ana\n",
    )
    await reread_service.apply_reread(db_session, archivo.id, tenant.tenant_id)
    await db_session.commit()

    pendientes = await _otros(db_session, tenant, archivo.id, UNCLASSIFIED_STATUS_PENDING)
    assert [p.id for p in pendientes] == [pendiente.id]
    assert await huellas_presentes(db_session, tenant.tenant_id, {ref}) == {ref}


# ── I1-4: la fila de riesgo de columna ruteada a «Otros» ───────────────────────

_CTX = "sheet:Gastos"
_DECISION = {
    "context_id": _CTX,
    "source_column": "monto",
    "target_field": "amount",
    "action": "route_affected_rows_to_others",
}


def _hoja(montos: list[str], fecha_c: str = "2026-01-03") -> dict[str, Any]:
    """Libro de una hoja de gastos (camino por hoja) con tres filas A, B, C."""
    filas = [
        {
            "fecha": ("2026-01-01", "2026-01-02", fecha_c)[i],
            "concepto": concepto,
            "monto": monto,
            "proveedor": "Edenor",
            "__context__": _CTX,
        }
        for i, (concepto, monto) in enumerate(zip(("Luz", "Gas", "Agua"), montos, strict=True))
    ]
    return {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {
                "context_id": _CTX,
                "label": "Gastos",
                "entity_type": "expense",
                "source_kind": "sheet",
                "headers": ["fecha", "concepto", "monto", "proveedor"],
                "fields": None,
                "preview_rows": [],
                "row_count": 3,
            }
        ],
        "ventas_detectadas": [],
        "gastos_detectados": filas,
        "stock_detectado": [],
    }


def _servir_summary(monkeypatch: pytest.MonkeyPatch, fresh: dict[str, Any]) -> None:
    _servir(monkeypatch, b"")
    monkeypatch.setattr(reread_service, "parse_uploaded_content", lambda *a, **k: fresh)


async def _confirmar_con_riesgo(
    session: AsyncSession,
    tenant: Tenant,
    summary: dict[str, Any],
    confirmed: dict[str, bool],
    decision: dict[str, str] = _DECISION,
) -> UploadedFile:
    """El confirm con la decisión de rutear a «Otros» la fila sin monto: importa
    las que quedan y captura la ruteada (lo mismo que ``POST /confirm``)."""
    f = UploadedFile(
        id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        uploaded_by=None,
        original_filename="gastos.xlsx",
        s3_key=f"tenants/{tenant.tenant_id}/gastos.xlsx",
        content_type="text/csv",
        size_bytes=10,
        purpose="gastos",
        processing_status=PROCESSING_STATUS_DONE,
        parsed_summary_json={
            "inferred_type": summary.get("inferred_type"),
            "confirmed_fields": confirmed,
            "column_risk_decisions": [decision],
        },
    )
    session.add(f)
    await session.commit()
    applied = apply_column_risk_decisions(summary, [ColumnRiskDecision(**decision)], {})
    await insert_confirmed_data(
        session, tenant.tenant_id, applied.summary, confirmed,
        source="ingestion", uploaded_file_id=f.id,
    )
    for cid, filas in applied.routed_rows.items():
        await _capture_column_risk_rows(
            session, tenant.tenant_id, f.id, cid,
            applied.routed_entity.get(cid) or "otros", filas, source="ingestion",
        )
    await session.commit()
    return f


async def _clasificar_gasto(
    session: AsyncSession, tenant: Tenant, file: UploadedFile, monto: str
) -> uuid.UUID:
    """Lo que hace ``others.reclassify_record`` al clasificar como gasto: el
    registro nace con la procedencia de «Otros» y la captura queda IMPORTED."""
    pendiente = (await _otros(session, tenant, file.id, UNCLASSIFIED_STATUS_PENDING))[0]
    gasto = ExpenseEntry(
        tenant_id=tenant.tenant_id,
        amount=Decimal(monto),
        transaction_date=datetime(2026, 1, 2),
        category="SERVICES",
        description="Clasificado desde Otros",
        provenance="REAL",
        source_upload_id=file.id,
        source_row_ref=unclassified_row_ref(pendiente.id),
    )
    session.add(gasto)
    pendiente.status = UNCLASSIFIED_STATUS_IMPORTED
    await session.commit()
    return gasto.id


async def _gastos_vivos(session: AsyncSession, tenant: Tenant) -> list[ExpenseEntry]:
    res = await session.execute(
        select(ExpenseEntry).where(
            ExpenseEntry.tenant_id == tenant.tenant_id, ExpenseEntry.voided_at.is_(None)
        )
    )
    return list(res.scalars().all())


async def test_la_clasificada_de_una_fila_ruteada_sobrevive_si_su_fila_sigue_mal(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El escenario de I1-4: B (sin monto) va a «Otros», el usuario la clasifica
    como gasto de 200 y relee sin corregir nada. La fila C entra en el índice que
    B tenía en el archivo, y la relectura tomaba eso como «B ya entró»: anulaba el
    gasto clasificado y la bandeja no la volvía a mostrar."""
    summary = _hoja(["100", "", "300"])
    archivo = await _confirmar_con_riesgo(db_session, tenant, summary, {"gastos": True})
    assert sorted(Decimal(g.amount) for g in await _gastos_vivos(db_session, tenant)) == [
        Decimal("100"), Decimal("300")
    ]
    clasificado = await _clasificar_gasto(db_session, tenant, archivo, "200")

    _servir_summary(monkeypatch, _hoja(["100", "", "300"]))
    await reread_service.apply_reread(db_session, archivo.id, tenant.tenant_id)
    await db_session.commit()

    vivos = await _gastos_vivos(db_session, tenant)
    assert clasificado in {g.id for g in vivos}, "la relectura anuló el gasto clasificado"
    assert sorted(Decimal(g.amount) for g in vivos) == [
        Decimal("100"), Decimal("200"), Decimal("300")
    ]


async def test_la_fila_ruteada_corregida_reemplaza_a_la_clasificada_por_hoja(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-O.2 por el camino por hoja: B ahora trae monto, entra, y reemplaza a la
    que el usuario clasificó (gana la relectura). Ni dos B ni C perdida."""
    archivo = await _confirmar_con_riesgo(
        db_session, tenant, _hoja(["100", "", "300"]), {"gastos": True}
    )
    clasificado = await _clasificar_gasto(db_session, tenant, archivo, "200")

    _servir_summary(monkeypatch, _hoja(["100", "250", "300"]))
    await reread_service.apply_reread(db_session, archivo.id, tenant.tenant_id)
    await db_session.commit()

    vivos = await _gastos_vivos(db_session, tenant)
    assert clasificado not in {g.id for g in vivos}
    assert sorted(Decimal(g.amount) for g in vivos) == [
        Decimal("100"), Decimal("250"), Decimal("300")
    ]


_CSV_RIESGO = (
    b"fecha,producto,monto,proveedor\n"
    b"2026-01-05,Coca Cola,1500,Distribuidora Sur\n"
    b"2026-01-06,Pan Lactal,,Panaderia Norte\n"
    b"2026-01-07,Yerba,700,Almacen Central\n"
)
_CSV_RIESGO_CORREGIDO = (
    b"fecha,producto,monto,proveedor\n"
    b"2026-01-05,Coca Cola,1500,Distribuidora Sur\n"
    b"2026-01-06,Pan Lactal,800,Panaderia Norte\n"
    b"2026-01-07,Yerba,700,Almacen Central\n"
)


async def test_la_fila_ruteada_corregida_no_se_duplica_en_la_tabla_suelta(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La otra mitad de I1-4: en la tabla suelta la ref de la captura nunca
    coincidía con la del import, así que la fila corregida entraba y la
    clasificada quedaba al lado: el gasto contado dos veces."""
    summary = parse_uploaded_content(_CSV_RIESGO, "text/csv", "gastos.csv")
    archivo = await _confirmar_con_riesgo(
        db_session,
        tenant,
        summary,
        default_confirmed_fields(summary),
        # La tabla suelta vive en el contexto sintético ``table``.
        decision={**_DECISION, "context_id": "table"},
    )
    assert len(await _gastos_vivos(db_session, tenant)) == 2
    clasificado = await _clasificar_gasto(db_session, tenant, archivo, "800")

    _servir(monkeypatch, _CSV_RIESGO_CORREGIDO)
    await reread_service.apply_reread(db_session, archivo.id, tenant.tenant_id)
    await db_session.commit()

    vivos = await _gastos_vivos(db_session, tenant)
    assert clasificado not in {g.id for g in vivos}, "quedaron la clasificada y la del archivo"
    assert sorted(Decimal(g.amount) for g in vivos) == [
        Decimal("700"), Decimal("800"), Decimal("1500")
    ]


async def test_la_captura_de_riesgo_no_se_resuelve_con_la_fila_que_ocupa_su_indice(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B (sin monto) se rutea y C (fecha ilegible) cae a «Otros» en el índice que
    B tenía en el archivo: las dos capturas guardan la misma ``__row_ref__``. Al
    corregir la fecha de C, entra C; la de B tiene que seguir pendiente (su
    huella ``risk:*`` sigue viva y nadie más la volvería a capturar)."""
    archivo = await _confirmar_con_riesgo(
        db_session, tenant, _hoja(["100", "", "300"], fecha_c="cuando pueda"), {"gastos": True}
    )
    pendientes = await _otros(db_session, tenant, archivo.id, UNCLASSIFIED_STATUS_PENDING)
    assert len(pendientes) == 2, [p.context_label for p in pendientes]
    assert len({str(p.row_data.get(ROW_REF_KEY)) for p in pendientes}) == 1

    _servir_summary(monkeypatch, _hoja(["100", "", "300"]))
    await reread_service.apply_reread(db_session, archivo.id, tenant.tenant_id)
    await db_session.commit()

    assert sorted(Decimal(g.amount) for g in await _gastos_vivos(db_session, tenant)) == [
        Decimal("100"), Decimal("300")
    ]
    quedan = await _otros(db_session, tenant, archivo.id, UNCLASSIFIED_STATUS_PENDING)
    assert len(quedan) == 1
    assert quedan[0].row_data.get(RISK_REF_KEY), "se descartó la de riesgo en vez de la de C"


async def test_la_venta_pendiente_no_se_resuelve_porque_entre_el_gasto_de_su_fila(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Una fila con venta y gasto deja las dos partes con la MISMA ref. Si la
    relectura carga por primera vez el gasto, eso no dice nada de la venta sin
    producto que sigue en «Otros»: tiene que seguir pendiente."""
    contenido = b"fecha,detalle,venta,gasto\n2026-01-05,A,1500,900\n"
    _servir(monkeypatch, contenido)
    summary = parse_uploaded_content(contenido, "text/csv", "libro.csv")
    archivo = UploadedFile(
        id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        uploaded_by=None,
        original_filename="libro.csv",
        s3_key=f"tenants/{tenant.tenant_id}/libro.csv",
        content_type="text/csv",
        size_bytes=len(contenido),
        purpose="ventas",
        processing_status=PROCESSING_STATUS_DONE,
        parsed_summary_json={
            "inferred_type": summary.get("inferred_type"),
            "confirmed_fields": {"ventas": True},
        },
    )
    db_session.add(archivo)
    await db_session.commit()
    await insert_confirmed_data(
        db_session, tenant.tenant_id, summary, {"ventas": True},
        source="ingestion", uploaded_file_id=archivo.id,
    )
    await db_session.commit()
    assert await _gastos_vivos(db_session, tenant) == []
    pendiente = (await _otros(db_session, tenant, archivo.id, UNCLASSIFIED_STATUS_PENDING))[0]
    assert pendiente.suggested_entity == "sale"

    # Ahora se confirman también los gastos de la planilla, y se relee.
    archivo.parsed_summary_json = {
        **(archivo.parsed_summary_json or {}),
        "confirmed_fields": {"ventas": True, "gastos": True},
    }
    await db_session.commit()
    await reread_service.apply_reread(db_session, archivo.id, tenant.tenant_id)
    await db_session.commit()

    assert [Decimal(g.amount) for g in await _gastos_vivos(db_session, tenant)] == [
        Decimal("900")
    ]
    pendientes = await _otros(db_session, tenant, archivo.id, UNCLASSIFIED_STATUS_PENDING)
    assert [p.suggested_entity for p in pendientes] == ["sale"]


async def test_clasificar_la_fila_vecina_no_descarta_la_captura_de_riesgo(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mismo choque de ``__row_ref__`` que arriba, del lado de F-O.2: el usuario
    clasifica C, la relectura descarta la copia nueva de C (ya la resolvió) y no
    puede llevarse de la bandeja la de B, que nadie clasificó."""
    hoja = _hoja(["100", "", "300"], fecha_c="cuando pueda")
    archivo = await _confirmar_con_riesgo(db_session, tenant, hoja, {"gastos": True})
    c = next(
        p
        for p in await _otros(db_session, tenant, archivo.id, UNCLASSIFIED_STATUS_PENDING)
        if not p.row_data.get(RISK_REF_KEY)
    )
    db_session.add(
        ExpenseEntry(
            tenant_id=tenant.tenant_id,
            amount=Decimal("300"),
            transaction_date=datetime(2026, 1, 3),
            category="SERVICES",
            description="Clasificado desde Otros",
            provenance="REAL",
            source_upload_id=archivo.id,
            source_row_ref=unclassified_row_ref(c.id),
        )
    )
    c.status = UNCLASSIFIED_STATUS_IMPORTED
    await db_session.commit()

    _servir_summary(monkeypatch, _hoja(["100", "", "300"], fecha_c="cuando pueda"))
    await reread_service.apply_reread(db_session, archivo.id, tenant.tenant_id)
    await db_session.commit()

    quedan = await _otros(db_session, tenant, archivo.id, UNCLASSIFIED_STATUS_PENDING)
    assert len(quedan) == 1
    assert quedan[0].row_data.get(RISK_REF_KEY), "se descartó la captura de riesgo de B"
