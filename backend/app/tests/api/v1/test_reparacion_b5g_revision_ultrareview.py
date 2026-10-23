"""B·5g — hallazgos de la revisión profunda (ultrareview, 2026-10-23).

1. El ``--revert`` no volvía a tomar la identidad de operación de la venta: si
   el archivo se re-subía después, la venta entraba dos veces. Ahora la vuelve a
   colgar, y si un import posterior ya la cargó, la anulada NO se restaura. La
   identidad es por COMPROBANTE: un renglón hermano (de esta corrida o vivo al
   aplicar) no cuenta como re-import (revisión adversarial del arreglo).
2. El análisis no tenía la excepción de la importación: una venta respaldada por
   el stock del catálogo de su MISMO archivo (fecha por confirmar) se anulaba.
   Ahora la regla es una sola (``respaldo_service.motivo_sin_respaldo``).
3. El cliente quedaba en una clave interna que nadie leía: una venta fiada
   resuelta desde «Otros» volvía sin cliente y la deuda se perdía. Ahora
   ``_registrar_venta_desde_otros`` lo toma de la fila y aplica las reglas de
   POST /sales (un fiado sin cliente real es 400). «Importar los sugeridos» deja
   las fiadas para resolverlas de a una (en lote se creaban sin cliente).
4. El cobro de fiado se filtraba con mayúsculas exactas (la importación no las
   distingue): un ``Inflow`` viejo se trataba como venta sin producto.
"""

from __future__ import annotations

import unittest.mock
import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import stock_service
from app.application.services.ingestion_import_service import (
    CUSTOMER_REF_KEY,
    bulk_import_unclassified,
)
from app.application.services.reparacion_ventas_sin_compra_service import (
    analizar,
    reparar,
    revertir,
)
from app.domain.business_time import today_ar
from app.domain.contrato_de_costo import ContratoDeCosto
from app.domain.ingestion_version import INGESTION_VERSION
from app.persistence.models.customer import Customer
from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.operation_identity import OperationIdentity, OperationIdentityLink
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord

pytestmark = pytest.mark.asyncio

_DIA = today_ar() - timedelta(days=10)
_CLAVE = "v1|sale|factura|0001-00000042"
_HASH = "hash-de-la-venta"


@pytest.fixture(autouse=True)
def _sin_eventos(mock_score_trigger: Any) -> Any:
    with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
        yield


async def _archivo(db: AsyncSession, tid: uuid.UUID) -> UploadedFile:
    f = UploadedFile(
        tenant_id=tid,
        uploaded_by=None,
        original_filename="historial.xlsx",
        s3_key=f"uploads/test/{uuid.uuid4()}/historial.xlsx",
        content_type="text/csv",
        size_bytes=10,
        purpose="ventas",
        status="uploaded",
        processing_status=PROCESSING_STATUS_DONE,
        parsed_summary_json={},
        ingestion_version=INGESTION_VERSION,
    )
    db.add(f)
    await db.flush()
    return f


async def _producto(db: AsyncSession, tid: uuid.UUID, nombre: str = "Vela") -> Product:
    p = Product(tenant_id=tid, name=nombre, sale_price_ars=Decimal("1000"), stock_units=10)
    db.add(p)
    await db.commit()
    return p


async def _venta(
    db: AsyncSession,
    tid: uuid.UUID,
    pid: uuid.UUID | None,
    *,
    monto: str = "2000",
    medio: str = "cash",
    cliente: uuid.UUID | None = None,
    archivo: uuid.UUID | None = None,
) -> SaleEntry:
    v = SaleEntry(
        tenant_id=tid, amount=Decimal(monto), quantity=2, product_id=pid,
        transaction_date=datetime.combine(_DIA, time(12)), payment_method=medio,
        customer_id=cliente, source_upload_id=archivo, provenance="REAL",
    )
    db.add(v)
    await db.flush()
    if pid is not None:
        await stock_service.decrement_for_sale(v, db)
    await db.commit()
    return v


async def _identidad(db: AsyncSession, tid: uuid.UUID, *ventas: SaleEntry) -> None:
    """Un comprobante: UNA identidad colgada de todos sus renglones."""
    ident = OperationIdentity(
        tenant_id=tid, identity_key=_CLAVE, content_hash=_HASH, entity_type="sale"
    )
    db.add(ident)
    await db.flush()
    for venta in ventas:
        db.add(
            OperationIdentityLink(
                identity_id=ident.id, tenant_id=tid, entity_type="sale", entity_id=venta.id
            )
        )
    await db.commit()


async def _vivas(db: AsyncSession, tid: uuid.UUID) -> set[uuid.UUID]:
    return set(
        (
            await db.execute(
                select(SaleEntry.id).where(
                    SaleEntry.tenant_id == tid, SaleEntry.voided_at.is_(None)
                )
            )
        ).scalars()
    )


async def _vinculos(db: AsyncSession, venta_id: uuid.UUID) -> list[str]:
    return list(
        (
            await db.execute(
                select(OperationIdentity.identity_key)
                .join(
                    OperationIdentityLink,
                    OperationIdentityLink.identity_id == OperationIdentity.id,
                )
                .where(OperationIdentityLink.entity_id == venta_id)
            )
        ).scalars()
    )


async def _stock_inicial(
    db: AsyncSession, tid: uuid.UUID, pid: uuid.UUID, dia: Any, archivo: uuid.UUID | None = None
) -> None:
    await stock_service.registrar_apertura_manual(
        pid, tid, 10, ContratoDeCosto(unit_cost_final=Decimal("400")), db,
        fecha=datetime.combine(dia, time(9)), confirmado_por=None,
    )
    if archivo is not None:
        mov = (
            await db.execute(
                select(InventoryMovement).where(
                    InventoryMovement.product_id == pid, InventoryMovement.qty > 0
                )
            )
        ).scalar_one()
        mov.source_upload_id = archivo
    await db.commit()


# ── 1. El revert y la identidad de operación ─────────────────────────────────


class TestRevertEIdentidad:
    async def test_el_revert_vuelve_a_tomar_la_identidad(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        tid = sample_tenant.tenant_id
        vela = await _producto(db_session, tid)
        venta = await _venta(db_session, tid, vela.id)
        await _identidad(db_session, tid, venta)

        r = await reparar(db_session, tid)
        await db_session.commit()
        assert await _vinculos(db_session, venta.id) == []  # soltada: se puede re-subir

        assert r.run_id is not None
        rev = await revertir(db_session, r.run_id)
        await db_session.commit()
        assert rev.restauradas == [str(venta.id)]
        # Vuelve a estar tomada: re-subir el archivo la omite como «ya aplicada».
        assert await _vinculos(db_session, venta.id) == [_CLAVE]

    async def test_si_un_import_posterior_la_volvio_a_cargar_no_se_restaura(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        tid = sample_tenant.tenant_id
        vela = await _producto(db_session, tid)
        venta = await _venta(db_session, tid, vela.id)
        await _identidad(db_session, tid, venta)
        r = await reparar(db_session, tid)
        await db_session.commit()
        await db_session.refresh(vela)
        stock_tras_apply = vela.stock_units

        # El dueño re-sube el archivo con la compra: la venta entra de nuevo y
        # toma la MISMA identidad.
        nueva = await _venta(db_session, tid, vela.id)
        await _identidad(db_session, tid, nueva)

        assert r.run_id is not None
        rev = await revertir(db_session, r.run_id)
        await db_session.commit()
        assert rev.reimportadas == [str(venta.id)]
        assert rev.restauradas == []
        await db_session.refresh(venta)
        assert venta.voided_at is not None  # una sola venta viva, la del re-import
        vivas = (
            await db_session.execute(
                select(SaleEntry).where(SaleEntry.tenant_id == tid, SaleEntry.voided_at.is_(None))
            )
        ).scalars().all()
        assert [v.id for v in vivas] == [nueva.id]
        await db_session.refresh(vela)
        assert vela.stock_units == stock_tras_apply - 2  # sólo descuenta la nueva
        assert (await db_session.execute(select(UnclassifiedRecord))).first() is None

    async def test_dos_renglones_del_mismo_comprobante_vuelven_los_dos(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        tid = sample_tenant.tenant_id
        a = await _producto(db_session, tid, "A")
        b = await _producto(db_session, tid, "B")
        va = await _venta(db_session, tid, a.id)
        vb = await _venta(db_session, tid, b.id)
        await _identidad(db_session, tid, va, vb)
        r = await reparar(db_session, tid)
        await db_session.commit()
        assert r.run_id is not None
        rev = await revertir(db_session, r.run_id)
        await db_session.commit()
        assert sorted(rev.restauradas) == sorted([str(va.id), str(vb.id)])
        assert rev.reimportadas == []
        assert await _vivas(db_session, tid) == {va.id, vb.id}
        assert sorted(await _vinculos(db_session, va.id) + await _vinculos(db_session, vb.id)) == [
            _CLAVE, _CLAVE
        ]

    async def test_un_renglon_hermano_vivo_no_es_un_re_import(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """A está respaldado y B no: la identidad nunca se suelta (A sigue vivo),
        y el revert igual tiene que devolver B."""
        tid = sample_tenant.tenant_id
        a = await _producto(db_session, tid, "A")
        b = await _producto(db_session, tid, "B")
        await _stock_inicial(db_session, tid, a.id, _DIA - timedelta(days=5))
        va = await _venta(db_session, tid, a.id)
        vb = await _venta(db_session, tid, b.id)
        await _identidad(db_session, tid, va, vb)
        r = await reparar(db_session, tid)
        await db_session.commit()
        assert [c.sale_id for c in r.candidatas] == [vb.id]
        assert r.run_id is not None
        rev = await revertir(db_session, r.run_id)
        await db_session.commit()
        assert rev.restauradas == [str(vb.id)]
        assert await _vivas(db_session, tid) == {va.id, vb.id}

    async def test_re_import_con_un_hermano_vivo_igual_se_detecta(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        tid = sample_tenant.tenant_id
        a = await _producto(db_session, tid, "A")
        b = await _producto(db_session, tid, "B")
        await _stock_inicial(db_session, tid, a.id, _DIA - timedelta(days=5))
        va = await _venta(db_session, tid, a.id)
        vb = await _venta(db_session, tid, b.id)
        await _identidad(db_session, tid, va, vb)
        r = await reparar(db_session, tid)
        await db_session.commit()
        # El re-import vuelve a colgar el renglón B (venta nueva) del comprobante.
        nueva = await _venta(db_session, tid, b.id)
        ident = (await db_session.execute(select(OperationIdentity))).scalar_one()
        db_session.add(
            OperationIdentityLink(
                identity_id=ident.id, tenant_id=tid, entity_type="sale", entity_id=nueva.id
            )
        )
        await db_session.commit()
        assert r.run_id is not None
        rev = await revertir(db_session, r.run_id)
        await db_session.commit()
        assert rev.reimportadas == [str(vb.id)]
        assert await _vivas(db_session, tid) == {va.id, nueva.id}


# ── 2. La excepción del catálogo del mismo archivo ───────────────────────────


async def test_la_venta_respaldada_por_el_catalogo_de_su_archivo_no_se_toca(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Catálogo + historial en un archivo, con la fecha del stock inicial todavía
    sin confirmar (queda DESPUÉS de las ventas): la importación las aceptó; la
    reparación no puede anularlas. Una venta del mismo producto que vino de OTRO
    lado sí es candidata."""
    tid = sample_tenant.tenant_id
    archivo = await _archivo(db_session, tid)
    vela = await _producto(db_session, tid)
    await _stock_inicial(db_session, tid, vela.id, today_ar(), archivo=archivo.id)
    del_archivo = await _venta(db_session, tid, vela.id, archivo=archivo.id)
    de_otro_lado = await _venta(db_session, tid, vela.id)

    r = await analizar(db_session, tid)
    assert [c.sale_id for c in r.candidatas] == [de_otro_lado.id]
    assert del_archivo.id not in {c.sale_id for c in r.candidatas}


# ── 3. El cliente de la venta fiada ──────────────────────────────────────────


async def test_la_venta_fiada_vuelve_con_su_cliente(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    juan = Customer(tenant_id=tid, name="Juan")
    db_session.add(juan)
    vela = await _producto(db_session, tid)
    venta = await _venta(db_session, tid, vela.id, medio="account", cliente=juan.id)
    r = await reparar(db_session, tid)
    await db_session.commit()
    assert r.fiado == Decimal("2000")
    fila = (await db_session.execute(select(UnclassifiedRecord))).scalar_one()
    assert fila.row_data[CUSTOMER_REF_KEY] == str(juan.id)

    # Carga el stock inicial y resuelve la fila: la venta vuelve fiada A JUAN.
    await _stock_inicial(db_session, tid, vela.id, _DIA - timedelta(days=30))
    resp = await client.post(
        f"/api/v1/others/{fila.id}/link-product",
        json={"product_id": str(vela.id)},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    db_session.expunge_all()
    vuelta = (
        await db_session.execute(
            select(SaleEntry).where(
                SaleEntry.tenant_id == tid,
                SaleEntry.voided_at.is_(None),
                SaleEntry.id != venta.id,
            )
        )
    ).scalar_one()
    assert vuelta.payment_method == "account"
    assert vuelta.customer_id == juan.id


async def test_una_fila_fiada_sin_cliente_no_crea_un_fiado_huerfano(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Las mismas reglas que POST /sales: fiado sin cliente real → 400."""
    tid = sample_tenant.tenant_id
    vela = await _producto(db_session, tid)
    await _stock_inicial(db_session, tid, vela.id, _DIA - timedelta(days=30))
    fila = UnclassifiedRecord(
        tenant_id=tid, source="ingestion", context_label="Venta sin producto",
        row_data={"fecha": _DIA.isoformat(), "monto": "500", "cantidad": "1",
                  "medio_de_pago": "account", "producto": "Vela"},
        suggested_entity="sale", status="PENDING",
    )
    db_session.add(fila)
    await db_session.commit()
    resp = await client.post(
        f"/api/v1/others/{fila.id}/link-product",
        json={"product_id": str(vela.id)},
        headers=auth_headers,
    )
    assert resp.status_code == 400, resp.text
    db_session.expunge_all()
    assert (
        await db_session.execute(select(SaleEntry).where(SaleEntry.tenant_id == tid))
    ).first() is None


async def test_importar_los_sugeridos_deja_las_fiadas_para_resolver_de_a_una(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    juan = Customer(tenant_id=tid, name="Juan")
    db_session.add(juan)
    vela = await _producto(db_session, tid)
    await _stock_inicial(db_session, tid, vela.id, _DIA - timedelta(days=30))
    base = {"fecha": _DIA.isoformat(), "monto": "500", "cantidad": "1", "producto": "Vela"}
    filas = [
        UnclassifiedRecord(
            tenant_id=tid, source="ingestion", context_label="Venta sin compra previa",
            row_data={**base, **extra}, suggested_entity="sale", status="PENDING",
        )
        for extra in (
            {"medio_de_pago": "cash", CUSTOMER_REF_KEY: str(juan.id)},
            {"medio_de_pago": "cuenta corriente"},
        )
    ]
    db_session.add_all(filas)
    await db_session.commit()
    counts = await bulk_import_unclassified(db_session, tid, "sale")
    assert counts["needs_manual"] == 2
    assert counts["imported_sales"] == 0
    assert await _vivas(db_session, tid) == set()


# ── 4. Cobros de fiado ───────────────────────────────────────────────────────


async def test_inflow_sin_distinguir_mayusculas(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    cobro = await _venta(db_session, tid, None, medio="Inflow")
    venta = await _venta(db_session, tid, None, medio="cash")
    ids = {c.sale_id for c in (await analizar(db_session, tid)).candidatas}
    assert ids == {venta.id}
    assert cobro.id not in ids
