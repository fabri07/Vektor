"""Paso 0b de B·nuevo: qué hace HOY ``main`` cuando las filas de un archivo se corren
bajo una edición a mano (``docs/plans/bnuevo-reemplazo-seguro-pr86.md``).

No construye nada: reproduce. El PR #86 (cerrado sin merge, ``200ff590``) intentó
reubicar la edición por contenido y la revisión le encontró 9 hallazgos. Antes de
decidir si se automatiza algo (A/B/C), hace falta saber qué resuelve ya el freno
del #81 y qué no. Cada test acá es un escenario, y el criterio es UNO solo:

    resultado correcto  — cada operación una sola vez, la edición conservada una
                          vez y ninguna fila salteada;
    o freno con rollback — ``RereadWouldLoseDataError`` y el estado idéntico al
                          previo (nada escrito).

No se exige que todo frene. Si un escenario pierde o duplica en silencio, es un
defecto de ``main``: se marca ``xfail(strict=True)`` con el motivo y NO se arregla
en este paso (va primero en la lista de defectos).

Las aserciones miran la foto entera del archivo —ids, importes, descripción,
``source_row_ref``, huellas, stock, movimientos y vínculos de identidad—, no
conteos: una relectura que anula una operación y crea otra deja el mismo total.

Fechas: relativas a ``today_ar()`` (una fecha futura se descarta en el import).
"""

from __future__ import annotations

import io
import os
import uuid
from collections.abc import AsyncGenerator
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.application.services import ingestion_import_service, reread_service
from app.application.services.file_parsing import parse_uploaded_content
from app.application.services.ingestion_import_service import (
    default_confirmed_fields,
    insert_confirmed_data,
)
from app.domain.business_time import today_ar
from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.memory import OperationFingerprint
from app.persistence.models.operation_identity import OperationIdentity, OperationIdentityLink
from app.persistence.models.product import Product
from app.persistence.models.repair import DataRepairRun
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.tests._respaldo import productos_con_compra
from app.tests.conftest import add_business_profile

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
TEST_PG_DSN = os.environ.get("TEST_PG_DSN")
_pg = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere TEST_PG_DSN (Postgres real)"),
]


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker: cada recálculo de score pagaría reintentos de kombu."""


def _dia(n: int) -> str:
    """Un día del pasado, fijo dentro del test: hoy menos ``30 - n``."""
    return (today_ar() - timedelta(days=30 - n)).isoformat()


# ── Archivos ────────────────────────────────────────────────────────────────────

#: (día, detalle, monto). El detalle termina en ``description`` del gasto.
Fila = tuple[int, str, str]
A, B, C, D = (1, "Coca Cola", "1500"), (2, "Pan Lactal", "800"), (3, "Yerba", "2200"), (
    4,
    "Fideos",
    "950",
)
N = (5, "Azúcar", "333")  # la fila que aparece en la relectura


def _csv(filas: list[Fila]) -> bytes:
    cuerpo = "".join(f"{_dia(d)},{p},{m},Proveedor Sur\n" for d, p, m in filas)
    return ("fecha,producto,monto,proveedor\n" + cuerpo).encode()


def _xlsx(hojas: dict[str, list[list[Any]]]) -> bytes:
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for nombre, filas in hojas.items():
        ws = wb.create_sheet(nombre)
        for fila in filas:
            ws.append(fila)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _hoja(filas: list[Fila]) -> list[list[Any]]:
    return [["fecha", "producto", "monto", "proveedor"]] + [
        [_dia(d), p, m, "Proveedor Sur"] for d, p, m in filas
    ]


def _leer(content: bytes, nombre: str) -> dict[str, Any]:
    mime = _XLSX_MIME if nombre.endswith(".xlsx") else "text/csv"
    return parse_uploaded_content(content, mime, nombre)


# ── Tenant, import y edición ────────────────────────────────────────────────────


async def _tenant(session: AsyncSession) -> uuid.UUID:
    tid = uuid.uuid4()
    session.add(
        Tenant(
            tenant_id=tid,
            legal_name="Relectura",
            display_name="Relectura",
            currency="ARS",
            pricing_reference_mode="MEP",
            status="ACTIVE",
        )
    )
    await session.flush()
    await add_business_profile(session, tid)
    await session.commit()
    return tid


async def _importar(
    session: AsyncSession,
    tid: uuid.UUID,
    content: bytes,
    nombre: str,
    *,
    confirmed: dict[str, bool] | None = None,
    column_mappings: dict[str, str] | None = None,
    summary: dict[str, Any] | None = None,
    context_mappings: dict[str, dict[str, str]] | None = None,
) -> uuid.UUID:
    summary = deepcopy(summary) if summary is not None else _leer(content, nombre)
    confirmed = confirmed or default_confirmed_fields(summary)
    f = UploadedFile(
        id=uuid.uuid4(),
        tenant_id=tid,
        uploaded_by=None,
        original_filename=nombre,
        s3_key=f"tenants/{tid}/{nombre}",
        content_type=_XLSX_MIME if nombre.endswith(".xlsx") else "text/csv",
        size_bytes=len(content),
        purpose="ingestion",
        processing_status=PROCESSING_STATUS_DONE,
        parsed_summary_json={
            "inferred_type": summary.get("inferred_type"),
            "confirmed_fields": confirmed,
        },
    )
    session.add(f)
    await session.commit()
    await insert_confirmed_data(
        session,
        tid,
        summary,
        confirmed,
        source="ingestion",
        uploaded_file_id=f.id,
        column_mappings=column_mappings,
        context_mappings=context_mappings,
    )
    await session.commit()
    return f.id


async def _registro(
    session: AsyncSession,
    fid: uuid.UUID,
    monto: str,
    tipo: type[SaleEntry] | type[ExpenseEntry] = ExpenseEntry,
) -> Any:
    return (
        await session.execute(
            select(tipo).where(
                tipo.source_upload_id == fid,
                tipo.amount == Decimal(monto),
                tipo.voided_at.is_(None),
            )
        )
    ).scalar_one()


async def _editar(
    session: AsyncSession,
    fid: uuid.UUID,
    monto: str,
    tipo: type[SaleEntry] | type[ExpenseEntry] = ExpenseEntry,
    **campos: Any,
) -> uuid.UUID:
    """Una edición a mano: el flag que hace que la relectura la preserve, más el
    cambio. Por defecto cambia sólo el texto (los otros dos campos quedan)."""
    rec = await _registro(session, fid, monto, tipo)
    rec.has_user_edits = True
    if not campos:
        campos = {"description" if tipo is ExpenseEntry else "notes": "corregido a mano"}
    for k, v in campos.items():
        setattr(rec, k, v)
    await session.commit()
    return rec.id


def _antes_de_b5e(m: pytest.MonkeyPatch) -> None:
    """Importa como antes de B·5e: sin la pasada que manda a «Otros» las ventas sin
    producto o sin compra previa."""

    async def _sin_pasada(*_a: Any, **_k: Any) -> None:
        return None

    m.setattr(ingestion_import_service, "_ventas_sin_compra_a_otros", _sin_pasada)


# ── La foto ─────────────────────────────────────────────────────────────────────


async def _foto(session: AsyncSession, tid: uuid.UUID, fid: uuid.UUID) -> dict[str, Any]:
    """Todo lo que una relectura de ``fid`` puede tocar, leído de la base."""
    gastos = (
        (await session.execute(select(ExpenseEntry).where(ExpenseEntry.source_upload_id == fid)))
        .scalars()
        .all()
    )
    ventas = (
        (await session.execute(select(SaleEntry).where(SaleEntry.source_upload_id == fid)))
        .scalars()
        .all()
    )
    gastos_t: list[tuple[Any, ...]] = [
        (
            "expense",
            str(g.id),
            str(g.amount),
            g.transaction_date.date().isoformat() if g.transaction_date else None,
            g.description,
            g.voided_at is not None,
            bool(g.has_user_edits),
            g.source_row_ref,
        )
        for g in gastos
    ]
    ventas_t: list[tuple[Any, ...]] = [
        (
            "sale",
            str(v.id),
            str(v.amount),
            v.transaction_date.date().isoformat() if v.transaction_date else None,
            v.notes,
            v.voided_at is not None,
            bool(v.has_user_edits),
            v.source_row_ref,
        )
        for v in ventas
    ]
    registros = sorted(gastos_t + ventas_t)
    movimientos = sorted(
        (str(m.id), str(m.movement_type), int(m.qty), m.voided_at is not None)
        for m in (
            await session.execute(
                select(InventoryMovement).where(InventoryMovement.source_upload_id == fid)
            )
        ).scalars()
    )
    productos = sorted(
        (p.name, int(p.stock_units or 0))
        for p in (await session.execute(select(Product).where(Product.tenant_id == tid))).scalars()
    )
    huellas = sorted(
        (
            await session.execute(
                select(OperationFingerprint.fingerprint).where(
                    OperationFingerprint.tenant_id == tid
                )
            )
        ).scalars()
    )
    vinculos = sorted(
        (str(k), t, str(e))
        for k, t, e in (
            await session.execute(
                select(
                    OperationIdentity.identity_key,
                    OperationIdentityLink.entity_type,
                    OperationIdentityLink.entity_id,
                )
                .join(OperationIdentity, OperationIdentity.id == OperationIdentityLink.identity_id)
                .where(OperationIdentityLink.tenant_id == tid)
            )
        ).all()
    )
    return {
        "registros": registros,
        "movimientos": movimientos,
        "productos": productos,
        "huellas": huellas,
        "vinculos": vinculos,
    }


def _vivos(foto: dict[str, Any]) -> list[tuple[str, str, str | None, bool]]:
    """(importe, detalle, día, editado) de lo vivo, ordenado."""
    return sorted((r[2], r[4] or "", r[3], r[6]) for r in foto["registros"] if not r[5])


@dataclass
class Salida:
    frenada: bool
    perdida: reread_service.PerdidaDeRelectura | None = None
    resultado: reread_service.RereadApplyResult | None = None


async def _releer(
    session: AsyncSession,
    tid: uuid.UUID,
    fid: uuid.UUID,
    content: bytes,
    nombre: str,
    *,
    run: DataRepairRun | None = None,
    accept_data_loss: bool = False,
    summary: dict[str, Any] | None = None,
) -> Salida:
    """Aplica la relectura REAL. Si frena, hace el rollback que hace el worker y
    exige que la base quede idéntica a la de antes — la mitad del criterio."""
    antes = await _foto(session, tid, fid)
    try:
        resultado = await reread_service.apply_reread(
            session,
            fid,
            tid,
            run=run,
            fresh_override=deepcopy(summary) if summary is not None else _leer(content, nombre),
            accept_data_loss=accept_data_loss,
        )
    except reread_service.RereadWouldLoseDataError as frenado:
        await session.rollback()
        assert await _foto(session, tid, fid) == antes, "frenó pero dejó algo escrito"
        return Salida(frenada=True, perdida=frenado.perdida)
    await session.commit()
    return Salida(frenada=False, resultado=resultado)


def _una_vez(foto: dict[str, Any], esperado: list[tuple[str, str]]) -> None:
    """Resultado correcto: lo vivo es EXACTAMENTE ``esperado`` (importe, detalle),
    cada operación una vez."""
    vivos = sorted((r[0], r[1]) for r in _vivos(foto))
    assert vivos == sorted(esperado), vivos


def _monto(m: str) -> str:
    return f"{Decimal(m):.2f}"


@pytest_asyncio.fixture
async def tid(db_session: AsyncSession) -> uuid.UUID:
    return await _tenant(db_session)


# ════════════════════════════════════════════════════════════════════════════════
# Los 9 escenarios del #86, contra main
# ════════════════════════════════════════════════════════════════════════════════


async def test_1_fila_insertada_antes_de_la_edicion_frena(
    db_session: AsyncSession, tid: uuid.UUID
) -> None:
    fid = await _importar(db_session, tid, _csv([A, B, C]), "g.csv")
    editado = await _editar(db_session, fid, C[2])

    salida = await _releer(db_session, tid, fid, _csv([N, A, B, C]), "g.csv")

    assert salida.frenada and salida.perdida is not None
    assert salida.perdida.edited_at_risk == 1
    riesgo = next(s for s in salida.perdida.samples if s["action"] == "edited_at_risk")
    assert riesgo["before"]["id"] == str(editado)
    # 0c: el CSV no trae comprobante ni ID de operación.
    assert salida.perdida.motivo == "sin_identificador_de_operacion"
    assert "no borra tu edición" in str(reread_service.texto_de_perdida(salida.perdida))


async def test_2_fila_eliminada_antes_de_la_edicion_frena(
    db_session: AsyncSession, tid: uuid.UUID
) -> None:
    fid = await _importar(db_session, tid, _csv([A, B, C]), "g.csv")
    await _editar(db_session, fid, C[2])

    salida = await _releer(db_session, tid, fid, _csv([B, C]), "g.csv")

    assert salida.frenada and salida.perdida is not None
    perdidos = [s["before"]["amount"] for s in salida.perdida.samples if s["action"] == "lose"]
    assert perdidos == [_monto(A[2])]


async def test_3_filas_reordenadas_sin_mover_la_edicion_es_correcto(
    db_session: AsyncSession, tid: uuid.UUID
) -> None:
    fid = await _importar(db_session, tid, _csv([A, B, C]), "g.csv")
    editado = await _editar(db_session, fid, C[2])
    ref = (await _foto(db_session, tid, fid))["registros"]

    salida = await _releer(db_session, tid, fid, _csv([B, A, C]), "g.csv")

    assert not salida.frenada
    foto = await _foto(db_session, tid, fid)
    _una_vez(
        foto,
        [(_monto(A[2]), A[1]), (_monto(B[2]), B[1]), (_monto(C[2]), "corregido a mano")],
    )
    conservado = next(r for r in foto["registros"] if r[1] == str(editado))
    assert not conservado[5] and conservado[7] == next(r[7] for r in ref if r[1] == str(editado))


async def test_4_filas_reordenadas_moviendo_la_edicion_frena(
    db_session: AsyncSession, tid: uuid.UUID
) -> None:
    fid = await _importar(db_session, tid, _csv([A, B, C]), "g.csv")
    await _editar(db_session, fid, B[2])

    salida = await _releer(db_session, tid, fid, _csv([B, A, C]), "g.csv")

    assert salida.frenada and salida.perdida is not None
    assert salida.perdida.edited_at_risk == 1


async def test_5_gemelas_con_una_editada_y_una_fila_nueva_antes(
    db_session: AsyncSession, tid: uuid.UUID
) -> None:
    """Hallazgo 1 del #86: la edición «saltaba» a la gemela. En main no hay
    reubicación: la huella de la editada cae sobre la otra gemela, que es igual."""
    gemela = (3, "Yerba", "2200")
    fid = await _importar(db_session, tid, _csv([A, gemela, gemela]), "g.csv")
    gastos = (
        (
            await db_session.execute(
                select(ExpenseEntry)
                .where(ExpenseEntry.source_upload_id == fid, ExpenseEntry.amount == Decimal("2200"))
                .order_by(ExpenseEntry.created_at, ExpenseEntry.id)
            )
        )
        .scalars()
        .all()
    )
    gastos[1].has_user_edits = True
    gastos[1].description = "corregido a mano"
    await db_session.commit()

    salida = await _releer(db_session, tid, fid, _csv([N, A, gemela, gemela]), "g.csv")

    assert not salida.frenada
    _una_vez(
        await _foto(db_session, tid, fid),
        [
            (_monto(N[2]), N[1]),
            (_monto(A[2]), A[1]),
            ("2200.00", "Yerba"),
            ("2200.00", "corregido a mano"),
        ],
    )


async def test_6_fila_con_venta_y_gasto_y_la_venta_editada(
    db_session: AsyncSession, tid: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hallazgo 2 del #86: la fila con venta y gasto. Las dos operaciones nacen de
    la misma fila y comparten ``source_row_ref``.

    B·5e: la planilla no tiene producto, así que HOY sus ventas van a «Otros» en
    el import y no hay venta que editar. El caso sigue existiendo para un archivo
    importado ANTES de B·5e (datos que ya están en producción): el import original
    se hace sin la pasada de B·5e; la relectura, con la regla vigente (que no
    vuelve a juzgar una fila que ya era venta de este archivo)."""
    contenido = (
        "fecha,detalle,venta,gasto\n"
        f"{_dia(1)},A,1500,900\n{_dia(2)},B,800,500\n{_dia(3)},C,2200,1000\n"
    ).encode()
    with monkeypatch.context() as m:
        _antes_de_b5e(m)
        fid = await _importar(
            db_session, tid, contenido, "vg.csv", confirmed={"ventas": True, "gastos": True}
        )
    editada = await _editar(db_session, fid, "800", SaleEntry)

    salida = await _releer(db_session, tid, fid, contenido, "vg.csv")

    # Medido en main: frena aunque el archivo NO cambió. El gasto de la misma
    # fila no está editado, su huella (la misma ref) se borra y la fila entera
    # vuelve a entrar: la venta, repetida. Freno con rollback: cumple, pero ese
    # archivo no se puede releer sin aceptar.
    assert salida.frenada and salida.perdida is not None
    assert (salida.perdida.to_lose, salida.perdida.edited_at_risk) == (0, 1)
    riesgo = next(x for x in salida.perdida.samples if x["action"] == "edited_at_risk")
    assert riesgo["before"]["id"] == str(editada)
    assert salida.perdida.motivo == "fila_con_venta_y_gasto"


async def test_7_compra_con_stock_editada_y_fila_nueva_antes_frena_sin_mover_stock(
    db_session: AsyncSession, tid: uuid.UUID
) -> None:
    def compras(filas: list[tuple[int, str, str, str]]) -> bytes:
        cuerpo = "".join(f"{_dia(d)},{p},{q},{t},Proveedor Sur\n" for d, p, q, t in filas)
        return ("fecha,articulo,cantidad,total,proveedor\n" + cuerpo).encode()

    filas = [(1, "Vela", "5", "6000"), (2, "Difusor", "3", "3600"), (3, "Mantel", "2", "8000")]
    fid = await _importar(db_session, tid, compras(filas), "c.csv")
    await _editar(db_session, fid, "8000")
    stock = (await _foto(db_session, tid, fid))["productos"]
    assert stock, "precondición: la compra crea productos con stock"

    salida = await _releer(
        db_session, tid, fid, compras([(0, "Florero", "4", "1200"), *filas]), "c.csv"
    )

    assert salida.frenada
    assert (await _foto(db_session, tid, fid))["productos"] == stock


async def test_8_multi_hoja_la_fila_nueva_en_otra_hoja_no_toca_la_edicion(
    db_session: AsyncSession, tid: uuid.UUID
) -> None:
    hojas = {"Enero": _hoja([A, B]), "Febrero": _hoja([C, D])}
    fid = await _importar(db_session, tid, _xlsx(hojas), "m.xlsx")
    await _editar(db_session, fid, D[2])

    nuevo = {"Enero": _hoja([N, A, B]), "Febrero": _hoja([C, D])}
    salida = await _releer(db_session, tid, fid, _xlsx(nuevo), "m.xlsx")

    assert not salida.frenada
    _una_vez(
        await _foto(db_session, tid, fid),
        [
            (_monto(N[2]), N[1]),
            (_monto(A[2]), A[1]),
            (_monto(B[2]), B[1]),
            (_monto(C[2]), C[1]),
            (_monto(D[2]), "corregido a mano"),
        ],
    )


async def test_9_archivo_anterior_a_las_huellas_con_una_edicion(
    db_session: AsyncSession, tid: uuid.UUID
) -> None:
    """Legacy: registros sin ``source_row_ref`` (importados antes de las huellas)."""
    fid = await _importar(db_session, tid, _csv([A, B, C]), "g.csv")
    await _editar(db_session, fid, C[2])
    await db_session.execute(
        update(ExpenseEntry).where(ExpenseEntry.source_upload_id == fid).values(source_row_ref=None)
    )
    await db_session.commit()

    salida = await _releer(db_session, tid, fid, _csv([N, A, B, C]), "g.csv")

    assert salida.frenada and salida.perdida is not None
    assert salida.perdida.edited_at_risk == 1
    assert salida.perdida.motivo == "archivo_anterior_a_huellas"


# ════════════════════════════════════════════════════════════════════════════════
# Los límites declarados del freno y lo que pidió la revisión
# ════════════════════════════════════════════════════════════════════════════════


async def test_contexto_todo_editado_el_apply_frena_aunque_el_preview_no_lo_vea(
    db_session: AsyncSession, tid: uuid.UUID
) -> None:
    fid = await _importar(db_session, tid, _csv([A, B, C]), "g.csv")
    for f in (A, B, C):
        await _editar(db_session, fid, f[2])

    preview = await reread_service.preview_reread(
        db_session, fid, tid, fresh_override=_leer(_csv([N, A, B, C]), "g.csv")
    )
    await db_session.rollback()
    # Límite declarado de `_ediciones_en_riesgo`: sin registros no editados no hay
    # con qué ver el corrimiento.
    assert preview.edited_at_risk == 0

    salida = await _releer(db_session, tid, fid, _csv([N, A, B, C]), "g.csv")
    assert salida.frenada


async def test_contexto_todo_editado_con_dos_campos_cambiados(
    db_session: AsyncSession, tid: uuid.UUID
) -> None:
    """Defecto (a) de B·nuevo, resuelto frenando. La edición cambió importe Y
    texto: la fila original que vuelve a entrar ya no coincide «dos de tres» con
    su edición, y no hay registro no editado que delate el corrimiento. Antes: la
    fila nueva se perdía bajo la huella de la primera edición y la de la última
    volvía a entrar repetida. Ahora el contexto cambió (esta relectura inserta en
    él) sin testigo: freno con rollback (``_releer`` exige el estado idéntico)."""
    fid = await _importar(db_session, tid, _csv([A, B, C]), "g.csv")
    for f in (A, B, C):
        await _editar(
            db_session, fid, f[2], amount=Decimal(f[2]) + 1, description=f"{f[1]} (a mano)"
        )

    salida = await _releer(db_session, tid, fid, _csv([N, A, B, C]), "g.csv")

    assert salida.frenada and salida.perdida is not None
    # Lo que hace aceptar (ver el test de abajo): las filas del archivo en la
    # posición de cada edición (N, A, B) no entran y a lo sumo una edición se
    # repite (el contexto tuvo una sola alta, C). Sin el contenido original no se
    # sabe que A y B eran las de las ediciones: cota, nunca menos de lo que pasa.
    assert (salida.perdida.to_lose, salida.perdida.edited_at_risk) == (3, 1)
    assert salida.perdida.motivo == "sin_identificador_de_operacion"
    perdidas = {s["before"]["producto"] for s in salida.perdida.samples if s["action"] == "lose"}
    assert N[1] in perdidas
    repetidas = [s["before"] for s in salida.perdida.samples if s["action"] == "edited_at_risk"]
    assert [r["description"] for r in repetidas] == [C[1]], repetidas


async def test_contexto_todo_editado_aceptar_aplica_lo_medido(
    db_session: AsyncSession, tid: uuid.UUID
) -> None:
    """Aceptar el freno del defecto (a): N no entra (la tapa la huella de la
    edición de A) y la fila original de C vuelve repetida. La medición aceptada
    —la que queda en ``data_loss`` del run— lo cubre: N está entre las pérdidas y
    la repetida es C."""
    fid = await _importar(db_session, tid, _csv([A, B, C]), "g.csv")
    for f in (A, B, C):
        await _editar(
            db_session, fid, f[2], amount=Decimal(f[2]) + 1, description=f"{f[1]} (a mano)"
        )

    salida = await _releer(
        db_session, tid, fid, _csv([N, A, B, C]), "g.csv", accept_data_loss=True
    )

    assert not salida.frenada and salida.resultado is not None
    vivos = sorted((r[0], r[1]) for r in _vivos(await _foto(db_session, tid, fid)))
    assert vivos == sorted(
        [
            *[(f"{Decimal(f[2]) + 1:.2f}", f"{f[1]} (a mano)") for f in (A, B, C)],
            (_monto(C[2]), C[1]),
        ]
    ), vivos
    run = await db_session.get(DataRepairRun, salida.resultado.run_id)
    assert run is not None
    data_loss = (run.details_json or {})["data_loss"]
    assert (data_loss["to_lose"], data_loss["edited_at_risk"]) == (3, 1)
    perdidas = {s["before"]["producto"] for s in data_loss["samples"] if s["action"] == "lose"}
    assert N[1] in perdidas


async def test_contexto_todo_editado_con_el_archivo_identico_no_frena(
    db_session: AsyncSession, tid: uuid.UUID
) -> None:
    """La otra mitad del defecto (a): sin cambios en el contexto, no hay freno."""
    fid = await _importar(db_session, tid, _csv([A, B, C]), "g.csv")
    for f in (A, B, C):
        await _editar(
            db_session, fid, f[2], amount=Decimal(f[2]) + 1, description=f"{f[1]} (a mano)"
        )
    antes = await _foto(db_session, tid, fid)

    salida = await _releer(db_session, tid, fid, _csv([A, B, C]), "g.csv")

    assert not salida.frenada
    assert await _foto(db_session, tid, fid) == antes


_CTX_PXC = "sheet:Ventas"
_MAPEO_PXC = {
    "fecha": "transaction_date",
    "producto": "product_name",
    "precio_unit": "unit_price",
    "cant": "quantity",
}


def _pxc(filas: list[tuple[str, str, str]]) -> dict[str, Any]:
    """Ventas SIN total, todas del mismo día: el import calcula precio × cantidad
    con el mapeo confirmado; el preview, que no lo ve, lee el precio unitario."""
    rows = [
        {"fecha": _dia(1), "producto": p, "precio_unit": pu, "cant": q, "__context__": _CTX_PXC}
        for p, pu, q in filas
    ]
    return {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {
                "context_id": _CTX_PXC,
                "entity_type": "sale",
                "source_kind": "sheet",
                "headers": list(_MAPEO_PXC),
                "fields": None,
                "preview_rows": [],
                "row_count": len(rows),
            }
        ],
        "ventas_detectadas": rows,
        "gastos_detectados": [],
        "stock_detectado": [],
    }


async def _run_con_borrador(
    session: AsyncSession, tid: uuid.UUID, fid: uuid.UUID, ctx: str, mapeo: dict[str, str]
) -> DataRepairRun:
    """El run de una sesión de relectura con el mapeo confirmado en el borrador,
    como en producción (sin él, la relectura vuelve a la heurística)."""
    run = DataRepairRun(
        tenant_id=tid,
        repair_type=reread_service.REPAIR_TYPE_REREAD,
        status="APPLYING",
        dry_run=False,
        details_json={
            "file_id": str(fid),
            "draft": {
                "column_mappings": [
                    {"context_id": ctx, "source_column": c, "target_field": t}
                    for c, t in mapeo.items()
                ]
            },
        },
    )
    session.add(run)
    await session.commit()
    return run


async def test_corrimiento_en_el_mismo_dia_con_importe_no_confiable(
    db_session: AsyncSession, tid: uuid.UUID
) -> None:
    """Límite declarado de `_ediciones_en_riesgo`: con el importe no confiable el
    preview compara por día, y acá todo es del mismo día — el preview no ve el
    corrimiento. La autoridad es el apply."""
    filas = [("Vela", "150", "3"), ("Difusor", "900", "2"), ("Mantel", "4200", "1")]
    # B·5e: con compra previa; si no, las ventas irían a «Otros» desde el import.
    await productos_con_compra(db_session, tid, ["Vela", "Difusor", "Mantel", "Florero"])
    fid = await _importar(
        db_session,
        tid,
        b"pxc",
        "p.xlsx",
        confirmed={"ventas": True},
        summary=_pxc(filas),
        context_mappings={_CTX_PXC: _MAPEO_PXC},
    )
    vivos = _vivos(await _foto(db_session, tid, fid))
    assert "1800.00" in {v[0] for v in vivos}, f"precondición: precio × cantidad: {vivos}"
    editado = await _editar(db_session, fid, "4200", SaleEntry)

    nuevo = _pxc([("Florero", "1300", "4"), *filas])
    preview = await reread_service.preview_reread(
        db_session, fid, tid, fresh_override=deepcopy(nuevo)
    )
    await db_session.rollback()
    assert preview.edited_at_risk == 0, "el preview no lo ve (límite declarado)"

    run = await _run_con_borrador(db_session, tid, fid, _CTX_PXC, _MAPEO_PXC)
    salida = await _releer(db_session, tid, fid, b"", "p.xlsx", run=run, summary=nuevo)

    assert salida.frenada and salida.perdida is not None
    assert salida.perdida.edited_at_risk == 1
    riesgo = next(s for s in salida.perdida.samples if s["action"] == "edited_at_risk")
    assert riesgo["before"]["id"] == str(editado)


async def test_renombrar_la_hoja_de_la_edicion(db_session: AsyncSession, tid: uuid.UUID) -> None:
    fid = await _importar(
        db_session, tid, _xlsx({"Compras": _hoja([A, B, C]), "Otra": _hoja([D])}), "r.xlsx"
    )
    await _editar(db_session, fid, B[2])

    renombrada = _xlsx({"Compras marzo": _hoja([A, B, C]), "Otra": _hoja([D])})
    salida = await _releer(db_session, tid, fid, renombrada, "r.xlsx")

    # El contexto es la hoja: renombrarla cambia todas sus huellas. Los no
    # editados vuelven, y la fila de la edición también: repetida → frena.
    assert salida.frenada and salida.perdida is not None
    assert (salida.perdida.to_lose, salida.perdida.edited_at_risk) == (0, 1)


async def test_aceptar_la_perdida_conserva_la_edicion(
    db_session: AsyncSession, tid: uuid.UUID
) -> None:
    """Lo que hace REALMENTE ``accept_data_loss``: destraba la compuerta, nada más.
    La edición NO se descarta (``_split_records`` la preserva) y la fila original
    vuelve a entrar, así que queda la edición Y su fila: repetida. Y la fila que
    quedó bajo la huella de la edición (B, que ahora ocupa la posición de C) se
    saltea: se pierde. El texto del freno no puede prometer «aceptar descarta la
    edición»."""
    fid = await _importar(db_session, tid, _csv([A, B, C]), "g.csv")
    editado = await _editar(db_session, fid, C[2])

    salida = await _releer(
        db_session, tid, fid, _csv([N, A, B, C]), "g.csv", accept_data_loss=True
    )

    assert not salida.frenada
    foto = await _foto(db_session, tid, fid)
    conservado = next(r for r in foto["registros"] if r[1] == str(editado))
    assert not conservado[5] and conservado[4] == "corregido a mano"
    vivos = sorted((r[0], r[1]) for r in _vivos(foto))
    assert vivos == sorted(
        [
            (_monto(N[2]), N[1]),
            (_monto(A[2]), A[1]),
            (_monto(C[2]), C[1]),  # la fila original, otra vez
            (_monto(C[2]), "corregido a mano"),  # la edición, conservada
        ]
    ), vivos
    assert (_monto(B[2]), B[1]) not in vivos, "B quedó bajo la huella de la edición"
    run = await db_session.get(DataRepairRun, salida.resultado.run_id) if salida.resultado else None
    assert run is not None and (run.details_json or {})["data_loss"]["edited_at_risk"] == 1


# ── El loop de apply (hallazgo 7 del #86) ───────────────────────────────────────


@pytest.fixture
def fake_redis() -> Any:
    """El FakeRedis del conftest raíz (el de integración no tiene ``exists``, que
    usa la compuerta de PIN del apply)."""
    from app.tests.conftest import FakeRedis

    return FakeRedis()


@pytest_asyncio.fixture
async def cliente(db_session: AsyncSession, fake_redis: Any) -> AsyncGenerator[Any, None]:
    """El cliente HTTP del conftest raíz: el de integración no inyecta Redis, y sin
    él la ventana de PIN de ``auth_headers`` no existe (428 PIN_REQUIRED)."""
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app, limiter
    from app.persistence.db.redis_client import get_redis
    from app.persistence.db.session import get_db_session

    limiter._storage.reset()
    app = create_app()

    async def _sesion() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    async def _redis() -> AsyncGenerator[Any, None]:
        yield fake_redis

    app.dependency_overrides[get_db_session] = _sesion
    app.dependency_overrides[get_redis] = _redis
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


async def test_pedidos_de_apply_sobre_una_relectura_frenada(
    cliente: Any,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cuántas tareas encola pedir «Aplicar» N veces sobre una relectura frenada.

    Medido en main: una por pedido, y nunca dos a la vez. Mientras una está en
    cola el run no está READY_TO_APPLY y el segundo pedido recibe 409; cuando el
    worker frena, la sesión vuelve a READY_TO_APPLY con la medición, y el pedido
    siguiente SIN aceptar encola otro apply completo, que vuelve a frenar con
    rollback. No hay loop automático: cada apply lo pidió el usuario. Es costo
    (un apply entero por clic), no pérdida."""
    import unittest.mock

    from app.integrations.s3 import S3Client
    from app.jobs import reread_worker

    tid = sample_tenant.tenant_id
    fid = await _importar(db_session, tid, _csv([A, B, C]), "g.csv")
    await _editar(db_session, fid, C[2])
    contenido = _csv([N, A, B, C])

    async def _bajar(self: S3Client, key: str) -> bytes:  # noqa: ARG001
        return contenido

    async def _cabecera(self: S3Client, key: str) -> dict[str, Any]:  # noqa: ARG001
        return {"etag": '"x"', "size": len(contenido), "last_modified": "2026-01-01T00:00:00Z"}

    monkeypatch.setattr(S3Client, "download", _bajar)
    monkeypatch.setattr(S3Client, "head", _cabecera)
    client = cliente
    pre = await client.post(f"/api/v1/ingestion/files/{fid}/reread/preview", headers=auth_headers)
    assert pre.status_code == 200, pre.text
    pedido = {"run_id": pre.json()["run_id"], "draft_version": pre.json()["draft_version"]}
    run_id = uuid.UUID(pedido["run_id"])
    sin_worker = unittest.mock.patch(
        "app.application.services.reread_diagnostics_service.check_ingestion_workers_available",
        return_value=None,
    )
    antes = await _foto(db_session, tid, fid)
    url = f"/api/v1/ingestion/files/{fid}/reread/apply"

    with sin_worker, unittest.mock.patch.object(reread_worker.reread_apply, "delay") as delay:
        for _ in range(3):
            r = await client.post(url, headers=auth_headers, json=pedido)
            assert r.status_code == 202, r.text
            # Un segundo pedido mientras está en cola no encola otro.
            doble = await client.post(url, headers=auth_headers, json=pedido)
            assert doble.status_code == 409, doble.text
            # Lo que deja el worker al frenar (probado contra una base aislada en
            # test_reread_compuerta_perdidas.py).
            run = await db_session.get(DataRepairRun, run_id)
            assert run is not None
            await db_session.refresh(run)
            reread_service.volver_a_confirmar(
                run,
                reread_service.PerdidaDeRelectura(
                    edited_at_risk=1, motivo="sin_identificador_de_operacion"
                ),
            )
            await db_session.commit()
        assert delay.call_count == 3

    assert await _foto(db_session, tid, fid) == antes
    # 0c: el estado del run frenado lleva el motivo, aditivo a la forma de siempre.
    estado = await client.get(
        f"/api/v1/ingestion/files/{fid}/reread/runs/{run_id}", headers=auth_headers
    )
    e = estado.json()
    assert (e["status"], e["error_code"]) == ("FAILED", "REREAD_WOULD_LOSE_DATA")
    assert e["data_loss"]["motivo"] == "sin_identificador_de_operacion"
    assert e["data_loss"]["motivo_texto"] == reread_service.MOTIVOS_DEL_FRENO[
        "sin_identificador_de_operacion"
    ]
    assert "no borra tu edición" in e["error"]


# ── El undo de la relectura ─────────────────────────────────────────────────────

_CUIT = "30-71234567-8"
_MAPEO_ID = {
    "fecha": "expense_date",
    "tipo": "document_type",
    "pto": "document_series",
    "nro": "invoice_number",
    "cuit": "supplier_cuit",
    "proveedor": "supplier_name",
    "detalle": "notes",
    "monto": "amount",
}


def _con_comprobante(filas: list[tuple[int, str, str, str]]) -> bytes:
    """Compras con identidad fuerte de DOCUMENTO: tipo, punto de venta, número y
    CUIT del emisor (mapeados a mano, como exige E6b)."""
    cuerpo = "".join(
        f"{_dia(d)},Factura A,0001,{nro},{_CUIT},Proveedor Sur,{det},{m}\n"
        for d, nro, det, m in filas
    )
    return ("fecha,tipo,pto,nro,cuit,proveedor,detalle,monto\n" + cuerpo).encode()


_FACTURAS = [
    (1, "00000101", "Coca Cola", "1500"),
    (2, "00000102", "Pan", "800"),
    (3, "00000103", "Yerba", "2200"),
]


async def _undo_de_una_relectura_con_identidad(
    session: AsyncSession, tid: uuid.UUID
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Importa con identidad, edita una, relee con un importe corregido y deshace.
    Devuelve la foto de antes de releer y la de después del undo."""
    fid = await _importar(
        session, tid, _con_comprobante(_FACTURAS), "f.csv", column_mappings=_MAPEO_ID
    )
    antes = await _foto(session, tid, fid)
    assert len(antes["vinculos"]) == 3, "precondición: tres identidades de documento"
    await _editar(session, fid, "2200")
    antes = await _foto(session, tid, fid)

    corregido = [_FACTURAS[0], (2, "00000102", "Pan", "850"), _FACTURAS[2]]
    run = await _run_con_borrador(session, tid, fid, "table", _MAPEO_ID)
    salida = await _releer(session, tid, fid, _con_comprobante(corregido), "f.csv", run=run)
    assert not salida.frenada and salida.resultado is not None
    medio = await _foto(session, tid, fid)
    assert ("850.00", "Pan") in [(r[0], r[1]) for r in _vivos(medio)]

    await reread_service.undo_reread(session, salida.resultado.run_id, tid)
    await session.commit()
    return antes, await _foto(session, tid, fid)


async def test_undo_de_la_relectura_restaura_registros_y_huellas(
    db_session: AsyncSession, tid: uuid.UUID
) -> None:
    antes, despues = await _undo_de_una_relectura_con_identidad(db_session, tid)
    vivos = [r for r in despues["registros"] if r[1] in {x[1] for x in antes["registros"]}]
    assert vivos == antes["registros"], "los mismos ids, importes, edición y source_row_ref"
    assert [r for r in despues["registros"] if r not in antes["registros"]] == []
    assert despues["huellas"] == antes["huellas"]


async def test_undo_de_la_relectura_restaura_los_vinculos_de_identidad(
    db_session: AsyncSession, tid: uuid.UUID
) -> None:
    """Defecto (c), resuelto: la relectura soltó la identidad de los anulados (y se
    borró al quedar huérfana) y la reclamó para los reinsertados. El undo suelta la
    de los borrados y vuelve a colgar la de los originales desde su snapshot."""
    antes, despues = await _undo_de_una_relectura_con_identidad(db_session, tid)
    assert despues["vinculos"] == antes["vinculos"]
    vivos = {r[1] for r in despues["registros"]}
    assert {e for _, _, e in despues["vinculos"]} <= vivos, "ningún vínculo colgando"


# ════════════════════════════════════════════════════════════════════════════════
# Contra Postgres real: rollback y unicidad (huellas, identidades) de verdad
# ════════════════════════════════════════════════════════════════════════════════


@pytest_asyncio.fixture
async def pg() -> AsyncGenerator[tuple[async_sessionmaker[AsyncSession], uuid.UUID], None]:
    """Factory sobre Postgres real + un tenant. Cada test abre su propia sesión:
    una sesión creada en la fixture quedaría atada a otro event loop."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    assert TEST_PG_DSN is not None
    engine: AsyncEngine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        tenant = await _tenant(s)
    try:
        yield factory, tenant
    finally:
        async with factory() as s:
            for modelo in (OperationIdentityLink, OperationIdentity, InventoryMovement):
                await s.execute(delete(modelo).where(modelo.tenant_id == tenant))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tenant))
            await s.commit()
        await engine.dispose()


@pytest.mark.parametrize("_", [pytest.param(None, marks=_pg)])
async def test_pg_freno_con_rollback_real(
    pg: tuple[async_sessionmaker[AsyncSession], uuid.UUID], _: None
) -> None:
    factory, tenant = pg
    async with factory() as session:
        fid = await _importar(
            session, tenant, _con_comprobante(_FACTURAS), "f.csv", column_mappings=_MAPEO_ID
        )
        await _editar(session, fid, "2200")
        run = await _run_con_borrador(session, tenant, fid, "table", _MAPEO_ID)
        nuevo = [(0, "00000100", "Azúcar", "333"), *_FACTURAS]

        salida = await _releer(session, tenant, fid, _con_comprobante(nuevo), "f.csv", run=run)

    assert salida.frenada and salida.perdida is not None  # foto idéntica: `_releer`
    # Con identidad, la fila original de la edición NO vuelve a entrar (su
    # identidad sigue tomada por la edición: «ya aplicada»), así que no hay edición
    # repetida. La fila que quedó bajo la huella de la edición (Pan) se saltea y se
    # mide como pérdida UNA vez: era un registro anulado, no una operación nueva.
    assert (salida.perdida.to_lose, salida.perdida.edited_at_risk) == (1, 0)
    assert [x["before"]["description"] for x in salida.perdida.samples] == ["Pan"]
    assert salida.perdida.motivo is None


@pytest.mark.parametrize("_", [pytest.param(None, marks=_pg)])
async def test_pg_undo_restaura_registros_y_huellas(
    pg: tuple[async_sessionmaker[AsyncSession], uuid.UUID], _: None
) -> None:
    factory, tenant = pg
    async with factory() as session:
        antes, despues = await _undo_de_una_relectura_con_identidad(session, tenant)
    assert [r for r in despues["registros"] if r not in antes["registros"]] == []
    assert despues["huellas"] == antes["huellas"]


@pytest.mark.parametrize(
    "_",
    [
        pytest.param(None, marks=_pg)
    ],
)
async def test_pg_undo_restaura_los_vinculos_de_identidad(
    pg: tuple[async_sessionmaker[AsyncSession], uuid.UUID], _: None
) -> None:
    factory, tenant = pg
    async with factory() as session:
        antes, despues = await _undo_de_una_relectura_con_identidad(session, tenant)
    assert despues["vinculos"] == antes["vinculos"]


async def test_releer_despues_del_undo_da_el_resultado_correcto(
    db_session: AsyncSession, tid: uuid.UUID
) -> None:
    """Antes del arreglo del defecto (c), la identidad de los restaurados seguía
    tomada por el vínculo del reinsertado que el undo borró: releer el MISMO
    archivo mandaba Coca Cola y Pan a «Otros» y frenaba para siempre. Con los
    vínculos restaurados, la relectura entra y deja cada operación una vez."""
    await _undo_de_una_relectura_con_identidad(db_session, tid)
    fid = (
        await db_session.execute(select(UploadedFile.id).where(UploadedFile.tenant_id == tid))
    ).scalar_one()
    run = await _run_con_borrador(db_session, tid, fid, "table", _MAPEO_ID)

    salida = await _releer(db_session, tid, fid, _con_comprobante(_FACTURAS), "f.csv", run=run)

    assert not salida.frenada
    foto = await _foto(db_session, tid, fid)
    _una_vez(foto, [("1500.00", "Coca Cola"), ("800.00", "Pan"), ("2200.00", "corregido a mano")])
    vivos = {r[1] for r in foto["registros"] if not r[5]}
    assert {e for _, _, e in foto["vinculos"]} <= vivos, "ningún vínculo colgando"
    assert len(foto["vinculos"]) == 3


async def test_identidad_fila_nueva_en_la_posicion_de_la_edicion(
    db_session: AsyncSession, tid: uuid.UUID
) -> None:
    fid = await _importar(
        db_session, tid, _con_comprobante(_FACTURAS), "f.csv", column_mappings=_MAPEO_ID
    )
    await _editar(db_session, fid, "2200")
    run = await _run_con_borrador(db_session, tid, fid, "table", _MAPEO_ID)
    nueva = (3, "00000104", "Azúcar", "333")

    salida = await _releer(
        db_session,
        tid,
        fid,
        _con_comprobante([_FACTURAS[0], _FACTURAS[1], nueva, _FACTURAS[2]]),
        "f.csv",
        run=run,
    )

    # Defecto (b), resuelto frenando (decisión A: no se traslada la edición). La
    # fila de Azúcar cae bajo la huella de la edición con una identidad que nunca
    # entró: se saltea. Lo que se pierde es Azúcar —no hay edición repetida, la
    # original no vuelve—, y eso es lo que se mide y se muestra.
    assert salida.frenada and salida.perdida is not None
    assert (salida.perdida.to_lose, salida.perdida.edited_at_risk) == (1, 0)
    assert salida.perdida.motivo is None
    assert [x["before"]["detalle"] for x in salida.perdida.samples] == ["Azúcar"]
    assert "edición" not in reread_service.texto_de_perdida(salida.perdida)

    # Aceptar: entra lo anunciado y nada más. Azúcar no entra, la edición no se
    # repite, y lo aceptado registra la pérdida que de verdad ocurrió.
    await db_session.refresh(run)  # el rollback del freno lo dejó expirado
    aceptada = await _releer(
        db_session,
        tid,
        fid,
        _con_comprobante([_FACTURAS[0], _FACTURAS[1], nueva, _FACTURAS[2]]),
        "f.csv",
        run=run,
        accept_data_loss=True,
    )
    assert not aceptada.frenada and aceptada.resultado is not None
    _una_vez(
        await _foto(db_session, tid, fid),
        [("1500.00", "Coca Cola"), ("800.00", "Pan"), ("2200.00", "corregido a mano")],
    )
    aplicado = await db_session.get(DataRepairRun, aceptada.resultado.run_id)
    assert aplicado is not None
    registrada = (aplicado.details_json or {})["data_loss"]
    assert (registrada["to_lose"], registrada["edited_at_risk"]) == (1, 0)
    assert [x["before"]["detalle"] for x in registrada["samples"]] == ["Azúcar"]


@pytest.mark.parametrize(
    ("editado", "items", "con_identidad", "motivo"),
    [
        (
            {"kind": "expense", "id": "e", "source_row_ref": None},
            [],
            set(),
            "archivo_anterior_a_huellas",
        ),
        (
            {"kind": "sale", "id": "v", "source_row_ref": "r"},
            [{"action": "REREAD_VOID", "before": {"kind": "expense", "source_row_ref": "r"}}],
            {"v"},
            "fila_con_venta_y_gasto",
        ),
        (
            {"kind": "expense", "id": "e", "source_row_ref": "r"},
            [],
            set(),
            "sin_identificador_de_operacion",
        ),
        ({"kind": "expense", "id": "e", "source_row_ref": "r"}, [], {"e"}, "filas_corridas"),
    ],
)
def test_motivo_del_freno(
    editado: dict[str, Any], items: list[dict[str, Any]], con_identidad: set[str], motivo: str
) -> None:
    """0c, unidad: el motivo más específico, en orden. Un movimiento de stock con la
    misma ref no cuenta como «la otra mitad» de la fila."""
    movimiento = {"action": "REREAD_VOID", "before": {"kind": "movement", "source_row_ref": "r"}}
    assert reread_service.motivo_del_freno([editado], [*items, movimiento], con_identidad) == motivo
    assert motivo in reread_service.MOTIVOS_DEL_FRENO
