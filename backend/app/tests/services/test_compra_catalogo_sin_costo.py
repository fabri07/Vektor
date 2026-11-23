"""``compra_catalogo_sin_costo``: stock de catálogo cargado como COMPRA sin costo ni gasto.

Casos reales: Camisa lino beige M (8 u.) y Pantalón cargo verde T42 (15 u.), hoja
«Producto / Cantidad / Costo total» con el stock tratado como «La compré ahora».

El corazón es el round-trip: el estado «legado» NO se fabrica a mano, lo deja el
importador real al leer un xlsx real, y la mejora tiene que ubicar EXACTAMENTE esas
filas (mismo archivo, hoja e índice → mismo ``source_row_ref``) releyendo el mismo xlsx
desde un S3 falso.
"""

from __future__ import annotations

import io
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import openpyxl
import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.application.services.file_parsing import parse_uploaded_content
from app.application.services.mejoras_de_datos import registro, servicio
from app.application.services.mejoras_de_datos.compra_catalogo_sin_costo import (
    CompraCatalogoSinCosto,
)
from app.domain.business_time import today_ar
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.business import BusinessProfile
from app.persistence.models.cash_close import CashClose
from app.persistence.models.file import UploadedFile
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.pipeline_event import PipelineEvent
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.tests.services.test_costeo_fuente_tardia_m2d import _a_las

pytestmark = pytest.mark.usefixtures("mock_score_trigger")

MEJORA = "compra_catalogo_sin_costo"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAPEO = {"Producto": "name", "Cantidad": "stock_units", "Costo total": "purchase_base_cost"}
HOJA_1 = "Camperas"
HOJA_2 = "Otra"
CTX_1 = f"sheet:{HOJA_1}"
CTX_2 = f"sheet:{HOJA_2}"

CAMISA = "Camisa lino beige M"
PANTALON = "Pantalón cargo verde T42"
REMERA = "Remera X"

#: Dos hojas, a propósito: el índice de fila REINICIA en cada hoja, así que ubicar la fila
#: sin el contexto daría otro ``source_row_ref``.
HOJAS: dict[str, list[list[Any]]] = {
    HOJA_1: [
        ["Producto", "Cantidad", "Costo total", "Forma de pago"],
        [CAMISA, 8, 48000, "Efectivo"],
        [PANTALON, 15, 90000, None],
    ],
    HOJA_2: [
        ["Producto", "Cantidad", "Costo total"],
        [REMERA, 2, 1000],
    ],
}


class S3Falso:
    """Lo único que usa la mejora: ``download``."""

    def __init__(self, contenido: bytes | None, *, falla: bool = False) -> None:
        self.contenido = contenido
        self.falla = falla

    async def download(self, key: str) -> bytes:
        if self.falla or self.contenido is None:
            raise FileNotFoundError(key)
        return self.contenido


def _xlsx(hojas: dict[str, list[list[Any]]]) -> bytes:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for nombre, filas in hojas.items():
        ws = wb.create_sheet(nombre)
        for fila in filas:
            ws.append(fila)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _sin_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.jobs import recalcular_costo_promedio as job  # noqa: PLC0415
    from app.persistence import costo_promedio_pendiente as pend  # noqa: PLC0415

    monkeypatch.setattr(pend, "ENCOLAR_EN_HILO", False)
    monkeypatch.setattr(job.recalcular_costo_promedio, "apply_async", lambda *_, **__: None)


def _s3(monkeypatch: pytest.MonkeyPatch, s3: S3Falso) -> None:
    monkeypatch.setattr(servicio, "crear_s3", lambda: s3)


async def _importar(
    db: AsyncSession,
    tenant: Tenant,
    *,
    hojas: dict[str, list[list[Any]]] | None = None,
    tratamiento: str | dict[str, str] | None = None,
    mapeo: dict[str, str] | None = None,
    con_confirm: bool = True,
) -> tuple[uuid.UUID, bytes]:
    """Importa el xlsx por el camino REAL y deja el confirm del archivo registrado."""
    await db.execute(
        update(BusinessProfile)
        .where(BusinessProfile.tenant_id == tenant.tenant_id)
        .values(fiscal_condition="monotributo")
    )
    contenido = _xlsx(hojas or HOJAS)
    resumen = parse_uploaded_content(contenido, XLSX, "stock.xlsx")
    ctxs = [c["context_id"] for c in resumen["mapping_contexts"]]
    tratamiento = tratamiento or dict.fromkeys(ctxs, "purchase")
    mapeos = {cid: dict(mapeo or MAPEO) for cid in ctxs}
    archivo = UploadedFile(
        id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        original_filename="stock.xlsx",
        s3_key=f"k/{uuid.uuid4()}",
        content_type=XLSX,
        size_bytes=len(contenido),
        purpose="stock",
        processing_status="DONE",
    )
    db.add(archivo)
    await db.flush()
    await importer.insert_confirmed_data(
        db,
        tenant.tenant_id,
        resumen,
        {"productos": True},
        context_mappings=mapeos,
        context_confirmed=dict.fromkeys(ctxs, True),
        stock_treatment=tratamiento,
        uploaded_file_id=archivo.id,
    )
    if con_confirm:
        db.add(
            PipelineEvent(
                trace_id=uuid.uuid4(),
                tenant_id=tenant.tenant_id,
                file_id=archivo.id,
                stage="confirm",
                detail={
                    "mappings": {"flat": {}, "context": mapeos},
                    "stock_treatment": tratamiento,
                },
            )
        )
    await db.commit()
    return archivo.id, contenido


async def _por_nombre(
    db: AsyncSession, tenant: Tenant
) -> dict[str, tuple[Product, InventoryMovement]]:
    out: dict[str, tuple[Product, InventoryMovement]] = {}
    for p in (
        await db.execute(select(Product).where(Product.tenant_id == tenant.tenant_id))
    ).scalars():
        m = (
            await db.execute(
                select(InventoryMovement).where(InventoryMovement.product_id == p.id)
            )
        ).scalar_one()
        out[p.name] = (p, m)
    return out


async def _releer(db: AsyncSession, modelo: Any, pk: uuid.UUID) -> Any:
    fila = await db.get(modelo, pk, populate_existing=True)
    assert fila is not None
    return fila


async def _gastos(db: AsyncSession, tenant: Tenant, *, vivos: bool = True) -> list[ExpenseEntry]:
    q = select(ExpenseEntry).where(ExpenseEntry.tenant_id == tenant.tenant_id)
    if vivos:
        q = q.where(ExpenseEntry.voided_at.is_(None))
    return list((await db.execute(q.order_by(ExpenseEntry.amount))).scalars())


def test_registrada_de_fabrica_y_cambia_valores() -> None:
    m = registro.obtener(MEJORA)
    assert isinstance(m, CompraCatalogoSinCosto)
    assert m.clase == "cambia_valores" and m.necesita_archivo


async def test_el_import_real_deja_el_estado_legado(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Precondición del round-trip: sin costo y sin gasto, como los casos reales."""
    await _importar(db_session, sample_tenant)

    movs = await _por_nombre(db_session, sample_tenant)
    assert set(movs) == {CAMISA, PANTALON, REMERA}
    for _, m in movs.values():
        assert (m.movement_type, m.source_type) == ("purchase", "catalog_initial_stock")
        assert m.unit_cost is None and m.unit_cost_final is None
    assert await _gastos(db_session, sample_tenant, vivos=False) == []


async def test_la_vista_previa_ubica_las_filas_del_import_real(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, contenido = await _importar(db_session, sample_tenant)
    _s3(monkeypatch, S3Falso(contenido))

    filas, a_mano = await servicio.vista_previa(db_session, sample_tenant.tenant_id, MEJORA)

    assert a_mano == {}
    por = {f["etiqueta"]: f for f in filas}
    assert set(por) == {CAMISA, PANTALON, REMERA}
    assert (por[CAMISA]["total"], por[CAMISA]["costo_unitario"]) == ("48000", "6000.00")
    assert por[CAMISA]["qty"] == 8
    assert (por[PANTALON]["total"], por[PANTALON]["costo_unitario"]) == ("90000", "6000.00")
    # La segunda hoja reinicia el índice de fila: sólo se ubica con el contexto.
    assert (por[REMERA]["total"], por[REMERA]["costo_unitario"]) == ("1000", "500.00")
    assert por[CAMISA]["medio_de_pago"] == "cash" and not por[CAMISA]["medio_por_defecto"]
    assert por[PANTALON]["medio_de_pago"] == "transfer" and por[PANTALON]["medio_por_defecto"]
    assert not any(f["arqueo_del_dia"] for f in filas)


async def test_aplica_la_cadena_entera(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, contenido = await _importar(db_session, sample_tenant)
    _s3(monkeypatch, S3Falso(contenido))
    antes = await _por_nombre(db_session, sample_tenant)

    r = await servicio.aplicar(db_session, sample_tenant.tenant_id, MEJORA)
    await db_session.commit()

    assert r.aplicadas == 3 and r.restantes == 0
    gastos = await _gastos(db_session, sample_tenant)
    assert [g.amount for g in gastos] == [
        Decimal("1000.00"),
        Decimal("48000.00"),
        Decimal("90000.00"),
    ]
    por_producto = {g.product_id: g for g in gastos}
    for nombre, (p, m) in antes.items():
        mov = await _releer(db_session, InventoryMovement, m.id)
        g = por_producto[p.id]
        # Costo «estimado» en el movimiento y en el producto.
        assert mov.unit_cost is not None and mov.unit_cost == mov.unit_cost_final
        assert (mov.shipping_treatment, mov.tax_treatment) == ("desconocido", "desconocido")
        assert (await _releer(db_session, Product, p.id)).unit_cost_ars == mov.unit_cost
        # El gasto es COGS, de la misma fila y el mismo archivo, con la fecha del movimiento.
        assert (g.category, g.expense_type, g.provenance) == ("INVENTORY", "COGS", "REAL")
        assert g.description == f"Stock inicial: {nombre}"
        assert (g.source_upload_id, g.source_row_ref) == (mov.source_upload_id, mov.source_row_ref)
        assert g.transaction_date == m.occurred_at
    assert por_producto[antes[CAMISA][0].id].payment_method == "cash"
    assert por_producto[antes[PANTALON][0].id].payment_method == "transfer"
    # Idempotente: ya no es candidata (su fila tiene gasto vivo).
    det = await CompraCatalogoSinCosto().detectar(db_session, sample_tenant.tenant_id)
    assert det.candidatos == []


async def test_recostea_ventas_y_deshacer_devuelve_todo(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, contenido = await _importar(db_session, sample_tenant)
    _s3(monkeypatch, S3Falso(contenido))
    camisa, mov = (await _por_nombre(db_session, sample_tenant))[CAMISA]
    venta = SaleEntry(
        tenant_id=sample_tenant.tenant_id,
        product_id=camisa.id,
        amount=Decimal("20000"),
        quantity=2,
        transaction_date=_a_las(today_ar()),
        payment_method="cash",
    )
    db_session.add(venta)
    await db_session.commit()
    v0 = await _releer(db_session, SaleEntry, venta.id)
    assert v0.cost_origin == "sin_costo"
    foto = (v0.cost_origin, v0.cost_ars, v0.cost_confirmed)

    r = await servicio.aplicar(db_session, sample_tenant.tenant_id, MEJORA, {mov.id})
    await db_session.commit()
    assert r.run_id is not None and r.aplicadas == 1
    v1 = await _releer(db_session, SaleEntry, venta.id)
    assert (v1.cost_origin, v1.cost_ars) == ("compra", Decimal("12000.00"))  # 2 × 6000

    rev = await servicio.revertir(db_session, sample_tenant.tenant_id, r.run_id)
    await db_session.commit()

    assert rev.completa and len(rev.revertidos) == 2  # la compra y la venta
    m = await _releer(db_session, InventoryMovement, mov.id)
    assert (m.unit_cost, m.unit_cost_final) == (None, None)
    v2 = await _releer(db_session, SaleEntry, venta.id)
    assert (v2.cost_origin, v2.cost_ars, v2.cost_confirmed) == foto
    anulado = (await _gastos(db_session, sample_tenant, vivos=False))[0]
    assert anulado.voided_at is not None and anulado.void_reason == "MANUAL_ADMIN_VOID"
    assert await _gastos(db_session, sample_tenant) == []
    # Idempotente: un segundo Deshacer no encuentra nada que hacer.
    otra = await servicio.revertir(db_session, sample_tenant.tenant_id, r.run_id)
    assert otra.completa and otra.revertidos == []
    # Con la lápida, nada lo reaplica solo; a mano sí se puede forzar y crea UN gasto vivo.
    forzada = await servicio.aplicar(db_session, sample_tenant.tenant_id, MEJORA, {mov.id})
    await db_session.commit()
    assert forzada.aplicadas == 1
    assert len(await _gastos(db_session, sample_tenant)) == 1


async def test_gasto_editado_despues_frena_el_deshacer(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, contenido = await _importar(db_session, sample_tenant)
    _s3(monkeypatch, S3Falso(contenido))
    _, mov = (await _por_nombre(db_session, sample_tenant))[CAMISA]
    r = await servicio.aplicar(db_session, sample_tenant.tenant_id, MEJORA, {mov.id})
    await db_session.commit()
    assert r.run_id is not None
    await db_session.execute(
        update(ExpenseEntry)
        .where(ExpenseEntry.tenant_id == sample_tenant.tenant_id)
        .values(has_user_edits=True)
    )
    await db_session.commit()

    rev = await servicio.revertir(db_session, sample_tenant.tenant_id, r.run_id)

    assert not rev.completa and str(mov.id) in rev.pendientes
    # Ni el gasto ni el costo se tocaron: no se deshace a medias.
    assert (await _releer(db_session, InventoryMovement, mov.id)).unit_cost is not None
    assert len(await _gastos(db_session, sample_tenant)) == 1


@pytest.mark.parametrize(
    ("variante", "motivo"),
    [
        ("producto_distinto", "es otro"),
        ("cantidad_distinta", "cantidad"),
        ("sin_total", "costo total válido"),
        ("pago_cuenta_corriente", "medio de pago"),
        ("pago_otro", "medio de pago"),
    ],
)
async def test_lo_que_no_calza_va_a_mano_con_su_motivo(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
    variante: str,
    motivo: str,
) -> None:
    _, _original = await _importar(db_session, sample_tenant)
    hojas = {k: [list(f) for f in v] for k, v in HOJAS.items()}
    fila = hojas[HOJA_1][1]  # la Camisa
    if variante == "producto_distinto":
        fila[0] = "Otra camisa"
    elif variante == "cantidad_distinta":
        fila[1] = 9
    elif variante == "sin_total":
        fila[2] = None
    elif variante == "pago_cuenta_corriente":
        fila[3] = "Cuenta corriente"
    elif variante == "pago_otro":
        fila[3] = "Cheque"
    _s3(monkeypatch, S3Falso(_xlsx(hojas)))

    filas, a_mano = await servicio.vista_previa(db_session, sample_tenant.tenant_id, MEJORA)

    assert {f["etiqueta"] for f in filas} == {PANTALON, REMERA}
    (clave, texto), = a_mano.items()
    assert clave.startswith(CAMISA) and motivo in texto
    # Lo que va a mano no se toca al aplicar.
    r = await servicio.aplicar(db_session, sample_tenant.tenant_id, MEJORA)
    await db_session.commit()
    assert r.aplicadas == 2 and len(a_mano) == 1
    camisa, mov = (await _por_nombre(db_session, sample_tenant))[CAMISA]
    assert (await _releer(db_session, InventoryMovement, mov.id)).unit_cost is None
    assert all(g.product_id != camisa.id for g in await _gastos(db_session, sample_tenant))


@pytest.mark.parametrize("falla", ["ausente", "caido"])
async def test_archivo_que_no_se_puede_leer_va_a_mano(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
    falla: str,
) -> None:
    await _importar(db_session, sample_tenant)
    _s3(monkeypatch, S3Falso(None, falla=falla == "caido"))

    filas, a_mano = await servicio.vista_previa(db_session, sample_tenant.tenant_id, MEJORA)
    assert filas == [] and len(a_mano) == 3
    assert all("archivo original" in m for m in a_mano.values())
    r = await servicio.aplicar(db_session, sample_tenant.tenant_id, MEJORA)
    assert r.aplicadas == 0 and r.run_id is None and r.restantes == 0
    assert await _gastos(db_session, sample_tenant, vivos=False) == []


async def test_archivo_borrado_va_a_mano(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    fid, contenido = await _importar(db_session, sample_tenant)
    await db_session.execute(
        update(UploadedFile).where(UploadedFile.id == fid).values(deleted_at=_a_las(today_ar()))
    )
    await db_session.commit()
    _s3(monkeypatch, S3Falso(contenido))

    filas, a_mano = await servicio.vista_previa(db_session, sample_tenant.tenant_id, MEJORA)
    assert filas == [] and all("ya no está" in m for m in a_mano.values())


@pytest.mark.parametrize(
    "caso", ["apertura", "sin_confirm", "sin_columna_total", "encabezado_que_no_es_total"]
)
async def test_no_son_candidatas(
    db_session: AsyncSession, sample_tenant: Tenant, caso: str
) -> None:
    if caso == "apertura":
        apertura = {CTX_1: "opening_balance", CTX_2: "opening_balance"}
        await _importar(db_session, sample_tenant, tratamiento=apertura)
    elif caso == "sin_confirm":
        await _importar(db_session, sample_tenant, con_confirm=False)
    elif caso == "sin_columna_total":
        sin_total = {"Producto": "name", "Cantidad": "stock_units"}
        await _importar(db_session, sample_tenant, mapeo=sin_total)
    else:
        await _importar(
            db_session,
            sample_tenant,
            hojas={
                HOJA_1: [["Producto", "Cantidad", "Costo"], [CAMISA, 8, 6000]],
            },
            mapeo={"Producto": "name", "Cantidad": "stock_units", "Costo": "purchase_base_cost"},
        )

    det = await CompraCatalogoSinCosto().detectar(db_session, sample_tenant.tenant_id)

    assert det.candidatos == []


async def test_una_fila_con_gasto_vivo_no_es_candidata(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, contenido = await _importar(db_session, sample_tenant)
    camisa, mov = (await _por_nombre(db_session, sample_tenant))[CAMISA]
    db_session.add(
        ExpenseEntry(
            tenant_id=sample_tenant.tenant_id,
            amount=Decimal("48000"),
            category="INVENTORY",
            expense_type="COGS",
            transaction_date=mov.occurred_at,
            description="ya cargado",
            product_id=camisa.id,
            source_upload_id=mov.source_upload_id,
            source_row_ref=mov.source_row_ref,
        )
    )
    await db_session.commit()
    _s3(monkeypatch, S3Falso(contenido))

    det = await CompraCatalogoSinCosto().detectar(db_session, sample_tenant.tenant_id)

    assert {c.etiqueta for c in det.candidatos} == {PANTALON, REMERA}
    assert any("gasto vivo" in m for m in det.a_mano.values())


async def test_otro_tenant_no_ve_las_compras(
    db_session: AsyncSession, sample_tenant: Tenant, second_tenant: Tenant
) -> None:
    await _importar(db_session, sample_tenant)
    det = await CompraCatalogoSinCosto().detectar(db_session, second_tenant.tenant_id)
    assert det.candidatos == [] and det.a_mano == {}


async def test_efectivo_con_arqueo_del_dia_se_avisa_y_se_audita(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, contenido = await _importar(db_session, sample_tenant)
    _, mov = (await _por_nombre(db_session, sample_tenant))[CAMISA]
    assert mov.occurred_at is not None
    dia: date = mov.occurred_at.date()
    db_session.add(
        CashClose(
            tenant_id=sample_tenant.tenant_id,
            close_date=dia,
            expected_total_ars=Decimal("0"),
            counted_total_ars=Decimal("0"),
            difference_ars=Decimal("0"),
            breakdown_by_method={},
        )
    )
    await db_session.commit()
    _s3(monkeypatch, S3Falso(contenido))

    filas, _ = await servicio.vista_previa(db_session, sample_tenant.tenant_id, MEJORA)
    por = {f["etiqueta"]: f for f in filas}
    assert por[CAMISA]["arqueo_del_dia"] is True  # efectivo + cierre ese día
    assert por[PANTALON]["arqueo_del_dia"] is False  # transferencia: no toca el efectivo

    await servicio.aplicar(db_session, sample_tenant.tenant_id, MEJORA)
    await db_session.commit()

    auditoria = {
        a.decision_data["despues"]["medio_de_pago"]: a.decision_data["despues"]
        for a in (
            await db_session.execute(
                select(DecisionAuditLog).where(
                    DecisionAuditLog.decision_type == f"MEJORA_DE_DATOS:{MEJORA}"
                )
            )
        ).scalars()
    }
    assert auditoria["cash"]["autorizo_fecha_con_arqueo"] is True
    assert auditoria["transfer"]["autorizo_fecha_con_arqueo"] is False
    # El cierre guardado no se reescribe.
    cierre = (await db_session.execute(select(CashClose))).scalar_one()
    assert cierre.counted_total_ars == Decimal("0")


async def test_endpoint_de_vista_previa(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, contenido = await _importar(db_session, sample_tenant)
    _s3(monkeypatch, S3Falso(contenido))

    r = await client.get(f"/api/v1/data-improvements/{MEJORA}/preview", headers=auth_headers)

    assert r.status_code == 200
    cuerpo = r.json()
    assert {f["etiqueta"] for f in cuerpo["filas"]} == {CAMISA, PANTALON, REMERA}
    assert cuerpo["a_mano"] == {}
    # Sólo lee: no escribió nada.
    assert await _gastos(db_session, sample_tenant, vivos=False) == []
    r404 = await client.get("/api/v1/data-improvements/no_existe/preview", headers=auth_headers)
    assert r404.status_code == 404
