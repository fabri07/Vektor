"""E6a — el mismo archivo tiene que dar el mismo resultado entre formatos.

Por qué esta compuerta y no una prueba del envío a secas
--------------------------------------------------------
El cobro del envío vivía en un **closure anidado dentro del camino multi-hoja**,
así que desde el camino de tabla suelta era estructuralmente inalcanzable: una
planilla de una sola tabla con la columna de flete mapeada importaba las compras
y **no cobraba el envío**, dejando el costo más bajo que el real y el margen
inflado. Medido antes de arreglarlo, con el mismo contenido: 30 envíos por el
camino multi-hoja y **0** por el plano.

El confirm lo tapaba rechazando el archivo con un 422. Sacar ese rechazo sin una
prueba de igualdad sería cambiar un error visible por uno silencioso — que es
exactamente el defecto que este programa viene a cerrar. Por eso lo que se afirma
acá no es "el plano ahora cobra envío" sino **"los dos caminos producen los
mismos efectos persistidos"**: gastos, caja, costos, stock y el envío cobrado una
sola vez.

Se compara lo PERSISTIDO y no los contadores del import: un contador es lo que el
importador dice que hizo, y la pregunta es qué quedó en los libros.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.ingestion_import_service import insert_confirmed_data
from app.domain.purchase_cost_decision import PurchaseCostDecision
from app.persistence.models.business import BusinessProfile
from app.persistence.models.file import UploadedFile
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry

_CTX = "table"
_COLS = {
    "fecha": "expense_date",
    "nro": "invoice_number",
    "proveedor": "supplier_name",
    "articulo": "product_name",
    "cantidad": "quantity",
    "total": "amount",
    "envio": "shipping_cost",
}

#: Dos comprobantes de dos líneas cada uno, con el flete REPETIDO en cada línea —
#: que es como lo escribe una planilla real. Si el agrupamiento por comprobante no
#: funciona, se cobran cuatro fletes en vez de dos.
_FILAS: list[dict[str, Any]] = [
    {"fecha": "2024-03-05", "nro": "0001-00000123", "proveedor": "Sur",
     "articulo": "Vela", "cantidad": "2", "total": "6000", "envio": "2000"},
    {"fecha": "2024-03-05", "nro": "0001-00000123", "proveedor": "Sur",
     "articulo": "Difusor", "cantidad": "3", "total": "3600", "envio": "2000"},
    {"fecha": "2024-03-06", "nro": "0001-00000124", "proveedor": "Norte",
     "articulo": "Sahumerio", "cantidad": "5", "total": "5000", "envio": "1500"},
    {"fecha": "2024-03-06", "nro": "0001-00000124", "proveedor": "Norte",
     "articulo": "Portavela", "cantidad": "1", "total": "900", "envio": "1500"},
]


def _summary(filas: list[dict[str, Any]], *, multi: bool) -> dict[str, Any]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "multi_sheet": multi,
        "row_count": len(filas),
        "mapping_contexts": [
            {
                "context_id": _CTX,
                "entity_type": "expense",
                "source_kind": "sheet",
                "headers": list(_COLS),
                "row_count": len(filas),
            }
        ],
        "gastos_detectados": [{**f, "__context__": _CTX} for f in filas],
    }


async def _importar(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    filas: list[dict[str, Any]],
    *,
    multi: bool,
    upload_id: uuid.UUID | None = None,
    shipping_decision: str | None = "una_por_hoja",
) -> dict[str, Any]:
    # La PK de BusinessProfile es `profile_id`, no `tenant_id`: un `get` por
    # tenant siempre devuelve None y duplicaría el perfil que la fixture ya creó.
    _perfil = (
        await db.execute(
            select(BusinessProfile).where(BusinessProfile.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if _perfil is None:
        db.add(
            BusinessProfile(
                profile_id=uuid.uuid4(),
                tenant_id=tenant_id,
                vertical_code="kiosco_almacen",
                data_mode="M0",
                data_confidence="LOW",
                onboarding_completed=True,
            )
        )
    if upload_id is None:
        upload_id = uuid.uuid4()
        db.add(
            UploadedFile(
                id=upload_id,
                tenant_id=tenant_id,
                original_filename="compras.xlsx",
                s3_key=f"tests/{upload_id}.xlsx",
                content_type="text/csv",
                size_bytes=1,
                purpose="ingestion",
                status="uploaded",
                processing_status="DONE",
            )
        )
    await db.flush()
    counts = await insert_confirmed_data(
        db,
        tenant_id,
        _summary(filas, multi=multi),
        {},
        context_mappings={_CTX: dict(_COLS)},
        context_entity={_CTX: "expense"},
        context_confirmed={_CTX: True},
        source="ingestion",
        uploaded_file_id=upload_id,
        shipping_decisions={_CTX: shipping_decision} if shipping_decision else None,
        purchase_cost_decisions={_CTX: PurchaseCostDecision(context_id=_CTX)},
    )
    await db.commit()
    counts["_upload_id"] = upload_id
    return counts


async def _efectos(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, Any]:
    """Lo que quedó en los libros, en una forma comparable entre tenants.

    Sin ids ni timestamps: dos imports del mismo contenido en tenants distintos
    generan uuids distintos, y compararlos diría que todo difiere. Lo que tiene
    que coincidir es el HECHO ECONÓMICO.
    """
    gastos = (
        (
            await db.execute(
                select(ExpenseEntry).where(
                    ExpenseEntry.tenant_id == tenant_id, ExpenseEntry.voided_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    productos = (
        (await db.execute(select(Product).where(Product.tenant_id == tenant_id)))
        .scalars()
        .all()
    )
    movs = (
        (
            await db.execute(
                select(InventoryMovement).where(InventoryMovement.tenant_id == tenant_id)
            )
        )
        .scalars()
        .all()
    )
    return {
        "gastos": sorted(
            (g.category, g.expense_type, str(g.amount), g.transaction_date.date().isoformat(),
             g.supplier_name or "", g.payment_method)
            for g in gastos
        ),
        # La caja: todo gasto es una salida. Se suma aparte porque un envío que se
        # capitaliza al costo sigue siendo plata que salió, y ése es justo el
        # punto donde los dos caminos podrían diferir sin que los totales de
        # resultado lo muestren.
        "caja": str(sum((g.amount for g in gastos), Decimal("0"))),
        "productos": sorted(
            (p.name, str(p.stock_units), str(p.unit_cost_ars))
            for p in productos
        ),
        "movimientos": sorted(
            (m.movement_type, str(m.qty), str(m.unit_cost or ""), m.source_type or "")
            for m in movs
        ),
    }


async def test_el_mismo_archivo_da_lo_mismo_como_tabla_suelta_que_como_hoja(
    db_session: AsyncSession, sample_tenant: Tenant, second_tenant: Tenant
) -> None:
    """La compuerta. Dos tenants, el mismo contenido, los dos formatos."""
    plano = await _importar(db_session, sample_tenant.tenant_id, _FILAS, multi=False)
    hoja = await _importar(db_session, second_tenant.tenant_id, _FILAS, multi=True)

    efectos_plano = await _efectos(db_session, sample_tenant.tenant_id)
    efectos_hoja = await _efectos(db_session, second_tenant.tenant_id)

    assert efectos_plano == efectos_hoja, (
        "el mismo archivo produce efectos distintos según el formato:\n"
        f"  tabla suelta: {efectos_plano}\n"
        f"  por hoja    : {efectos_hoja}"
    )
    # Y que no sea una igualdad vacía: los dos tienen que haber cobrado el envío.
    assert plano.get("envios") == hoja.get("envios") == 2


async def test_el_envio_se_cobra_una_sola_vez_por_comprobante_en_los_dos_caminos(
    db_session: AsyncSession, sample_tenant: Tenant, second_tenant: Tenant
) -> None:
    """El flete viene repetido en las 4 filas y son 2 comprobantes: 2 fletes.

    Cobrarlo por fila multiplica el costo de logística por la cantidad de
    artículos del remito, que es el defecto original de F-H6.b.
    """
    await _importar(db_session, sample_tenant.tenant_id, _FILAS, multi=False)
    await _importar(db_session, second_tenant.tenant_id, _FILAS, multi=True)

    for tenant_id in (sample_tenant.tenant_id, second_tenant.tenant_id):
        logistica = list(
            (
                await db_session.execute(
                    select(ExpenseEntry).where(
                        ExpenseEntry.tenant_id == tenant_id,
                        ExpenseEntry.category == "LOGISTICS",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(logistica) == 2, (
            f"{len(logistica)} fletes para 2 comprobantes en {tenant_id}: "
            "el envío repetido en cada línea del remito se cobró más de una vez"
        )
        assert sorted(str(g.amount) for g in logistica) == ["1500.00", "2000.00"]


async def test_reordenar_las_filas_no_cambia_los_efectos(
    db_session: AsyncSession, sample_tenant: Tenant, second_tenant: Tenant
) -> None:
    """Un exportador puede escribir las líneas en otro orden. Si el agrupamiento
    dependiera de la posición, el mismo remito se cobraría distinto."""
    await _importar(db_session, sample_tenant.tenant_id, _FILAS, multi=False)
    await _importar(
        db_session, second_tenant.tenant_id, list(reversed(_FILAS)), multi=False
    )
    assert await _efectos(db_session, sample_tenant.tenant_id) == await _efectos(
        db_session, second_tenant.tenant_id
    )


async def test_reintentar_el_mismo_archivo_no_cobra_el_envio_dos_veces(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Idempotencia: re-confirmar el MISMO archivo (mismo `uploaded_file_id`) no
    puede duplicar el flete. La huella del cargo se ancla en el comprobante + la
    cifra, no en una fila arbitraria del grupo."""
    primero = await _importar(db_session, sample_tenant.tenant_id, _FILAS, multi=False)
    antes = await _efectos(db_session, sample_tenant.tenant_id)

    await _importar(
        db_session,
        sample_tenant.tenant_id,
        _FILAS,
        multi=False,
        upload_id=primero["_upload_id"],
    )
    despues = await _efectos(db_session, sample_tenant.tenant_id)
    assert antes["gastos"] == despues["gastos"], "el reintento duplicó gastos"
    assert antes["caja"] == despues["caja"]


@pytest.mark.parametrize("multi", [False, True])
async def test_borrar_el_archivo_revierte_tambien_el_envio(
    db_session: AsyncSession, sample_tenant: Tenant, multi: bool
) -> None:
    """La reversión tiene que alcanzar al flete en los DOS caminos.

    Un envío que sobrevive al borrado del archivo que lo trajo es un gasto sin
    respaldo: no aparece en ninguna compra y nadie lo va a salir a buscar.
    """
    from app.application.services.file_deletion_service import revert_file_data

    counts = await _importar(db_session, sample_tenant.tenant_id, _FILAS, multi=multi)
    logistica_antes = len(
        (
            await db_session.execute(
                select(ExpenseEntry).where(
                    ExpenseEntry.tenant_id == sample_tenant.tenant_id,
                    ExpenseEntry.category == "LOGISTICS",
                    ExpenseEntry.voided_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert logistica_antes == 2

    await revert_file_data(db_session, counts["_upload_id"], sample_tenant.tenant_id)
    await db_session.commit()
    db_session.expunge_all()

    vivos = (
        (
            await db_session.execute(
                select(ExpenseEntry).where(
                    ExpenseEntry.tenant_id == sample_tenant.tenant_id,
                    ExpenseEntry.voided_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert not vivos, f"quedaron {len(vivos)} gastos vivos tras borrar el archivo"


# ── Filas SIN comprobante: acá la decisión del usuario es lo único que decide ──
#: Mismo flete repetido en dos filas, sin número de comprobante. Sin la decisión
#: del usuario no se cobra nada (un 2.000 repetido es indistinguible de dos fletes
#: de 2.000); con `una_por_hoja` se cobra UNA vez.
_SIN_COMPROBANTE: list[dict[str, Any]] = [
    {"fecha": "2024-03-05", "nro": "", "proveedor": "Sur",
     "articulo": "Vela", "cantidad": "2", "total": "6000", "envio": "2000"},
    {"fecha": "2024-03-05", "nro": "", "proveedor": "Sur",
     "articulo": "Difusor", "cantidad": "3", "total": "3600", "envio": "2000"},
]


@pytest.mark.parametrize("multi", [False, True])
async def test_la_decision_sobre_el_envio_sin_comprobante_se_honra_en_los_dos_caminos(
    db_session: AsyncSession, sample_tenant: Tenant, multi: bool
) -> None:
    """Esta es la prueba que detecta la clave de contexto rota.

    La decisión del usuario viaja indexada por `context_id`. El camino plano
    descartaba esa clave y buscaba la decisión bajo `""`, así que **nunca la
    encontraba**: la API la validaba, el usuario la veía aceptada, y el import se
    comportaba como si no existiera — el envío no se cobraba y el archivo no
    dejaba ningún rastro de que una decisión suya se hubiera perdido.

    El escenario tiene filas SIN número de comprobante a propósito: es el único
    caso donde la decisión cambia el resultado. Con comprobante, la agrupación
    sale de los identificadores de la fila y la decisión no se consulta — por eso
    el test de paridad no alcanzaba para detectar esto.
    """
    counts = await _importar(
        db_session, sample_tenant.tenant_id, _SIN_COMPROBANTE, multi=multi
    )
    logistica = (
        (
            await db_session.execute(
                select(ExpenseEntry).where(
                    ExpenseEntry.tenant_id == sample_tenant.tenant_id,
                    ExpenseEntry.category == "LOGISTICS",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(logistica) == 1, (
        f"la decisión «una por hoja» tenía que cobrar UN flete y cobró "
        f"{len(logistica)}. counts={counts.get('envios')} "
        f"sin_comprobante={counts.get('envios_sin_comprobante')}"
    )
    assert str(logistica[0].amount) == "2000.00"


@pytest.mark.parametrize("multi", [False, True])
async def test_sin_decision_el_envio_sin_comprobante_no_se_cobra_y_se_reporta(
    db_session: AsyncSession, sample_tenant: Tenant, multi: bool
) -> None:
    """La otra mitad: sin identidad Y sin decisión, no se inventa.

    Un 2.000 repetido en dos filas es indistinguible de dos fletes de 2.000.
    Elegir uno de los dos sería inventar un dato contable — se reporta y el resto
    del archivo entra igual, en los dos caminos.
    """
    counts = await _importar(
        db_session,
        sample_tenant.tenant_id,
        _SIN_COMPROBANTE,
        multi=multi,
        shipping_decision=None,
    )
    logistica = (
        (
            await db_session.execute(
                select(ExpenseEntry).where(
                    ExpenseEntry.tenant_id == sample_tenant.tenant_id,
                    ExpenseEntry.category == "LOGISTICS",
                )
            )
        )
        .scalars()
        .all()
    )
    assert not logistica, "sin identidad ni decisión no se puede cobrar nada"
    assert counts.get("envios_sin_comprobante"), (
        "y tiene que quedar reportado: un envío que no se cobró en silencio es "
        "un costo más bajo que el real que nadie va a salir a buscar"
    )
    # Las compras entran igual: el envío sin resolver no bloquea la hoja.
    assert counts["gastos"] == 2
