"""F-H4 en el importador: el total sale de precio × cantidad, por los dos caminos.

El módulo puro ya está probado fila por fila (`app/tests/domain/test_line_amount.py`).
Acá se prueba lo que sólo se ve cableado: que la hoja ENTRE (sin columna de monto,
la compuerta `wants_ventas` la salteaba antes de llegar a la primera fila), que la
evidencia quede en la fila, que los contadores alimenten los avisos, que una fila
que no se puede resolver no desaparezca — y que el camino plano y el multi-hoja
contesten lo mismo sobre el mismo archivo, que es la asimetría que ya se pagó dos
veces en este importador.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.ingestion_import_service import insert_confirmed_data
from app.domain.line_amount import AMOUNT_ORIGINAL_FIELD, AMOUNT_SOURCE_FIELD
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord

_CTX = "sheet:Ventas"
_HEADERS = ["fecha", "producto", "precio_unit", "cant", "valor_declarado"]


def _fila(
    *,
    fecha: str = "2024-03-10",
    producto: str = "Vela aromatica",
    precio: str = "150.50",
    cantidad: str = "3",
    declarado: str = "",
) -> dict[str, Any]:
    return {
        "fecha": fecha,
        "producto": producto,
        "precio_unit": precio,
        "cant": cantidad,
        "valor_declarado": declarado,
    }


def _summary_plano(filas: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "multi_sheet": False,
        "has_venta": True,
        "row_count": len(filas),
        "headers": _HEADERS,
        "ventas_detectadas": filas,
        "preview_rows": filas,
    }


def _summary_multihoja(filas: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {
                "context_id": _CTX,
                "entity_type": "sale",
                "source_kind": "sheet",
                "headers": _HEADERS,
                "fields": None,
                "preview_rows": [],
                "row_count": len(filas),
            }
        ],
        "ventas_detectadas": [{**f, "__context__": _CTX} for f in filas],
        "gastos_detectados": [],
        "stock_detectado": [],
    }


#: Mapeo SIN columna de monto: el total es la cuenta. Los nombres de las columnas
#: no son keywords de ninguna heurística del importador a propósito — si el monto
#: apareciera igual, sería porque se autodetectó, y el test no probaría nada.
_MAPEO_SIN_MONTO = {
    "fecha": "transaction_date",
    "producto": "product_name",
    "precio_unit": "unit_price",
    "cant": "quantity",
}


async def _importar_plano(
    db_session: AsyncSession,
    tenant: Tenant,
    filas: list[dict[str, Any]],
    mapeo: dict[str, str],
) -> dict[str, Any]:
    counts = await insert_confirmed_data(
        db_session,
        tenant.tenant_id,
        _summary_plano(filas),
        {"ventas": True},
        column_mappings=mapeo,
    )
    await db_session.flush()
    return counts


async def _importar_multihoja(
    db_session: AsyncSession,
    tenant: Tenant,
    filas: list[dict[str, Any]],
    mapeo: dict[str, str],
) -> dict[str, Any]:
    counts = await insert_confirmed_data(
        db_session,
        tenant.tenant_id,
        _summary_multihoja(filas),
        {"ventas": True},
        context_mappings={_CTX: mapeo},
        context_confirmed={_CTX: True},
    )
    await db_session.flush()
    return counts


async def _ventas(db_session: AsyncSession, tenant: Tenant) -> list[SaleEntry]:
    result = await db_session.execute(
        select(SaleEntry).where(SaleEntry.tenant_id == tenant.tenant_id)
    )
    return list(result.scalars().all())


async def _otros(db_session: AsyncSession, tenant: Tenant) -> list[UnclassifiedRecord]:
    result = await db_session.execute(
        select(UnclassifiedRecord).where(
            UnclassifiedRecord.tenant_id == tenant.tenant_id
        )
    )
    return list(result.scalars().all())


@pytest.mark.asyncio
class TestElMontoSeCalcula:
    async def test_multihoja_sin_columna_de_monto(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        counts = await _importar_multihoja(
            db_session, sample_tenant, [_fila()], _MAPEO_SIN_MONTO
        )
        assert counts["ventas"] == 1
        assert counts["montos_calculados"] == 1
        assert counts["montos_discrepantes"] == 0

        venta = (await _ventas(db_session, sample_tenant))[0]
        assert venta.amount == Decimal("451.50")  # 150.50 × 3
        assert venta.quantity == 3
        assert venta.unit_price == Decimal("150.50")
        assert (venta.custom_fields or {})[AMOUNT_SOURCE_FIELD] == "calculated"
        assert AMOUNT_ORIGINAL_FIELD not in (venta.custom_fields or {})

    async def test_plano_sin_columna_de_monto(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """La compuerta `wants_ventas` exigía columna de monto: sin esto la hoja
        entera se salteaba y la derivación quedaba escrita pero inalcanzable."""
        counts = await _importar_plano(
            db_session, sample_tenant, [_fila()], _MAPEO_SIN_MONTO
        )
        assert counts["ventas"] == 1
        assert counts["montos_calculados"] == 1

        venta = (await _ventas(db_session, sample_tenant))[0]
        assert venta.amount == Decimal("451.50")
        assert venta.unit_price == Decimal("150.50")
        assert (venta.custom_fields or {})[AMOUNT_SOURCE_FIELD] == "calculated"

    async def test_los_dos_caminos_dan_lo_mismo(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Paridad: mismo archivo como hoja suelta y como solapa → misma venta."""
        filas = [_fila(), _fila(precio="99.99", cantidad="7", declarado="700")]

        await _importar_plano(db_session, sample_tenant, filas, _MAPEO_SIN_MONTO)
        plano = [
            (v.amount, v.quantity, v.unit_price, (v.custom_fields or {}).get(AMOUNT_SOURCE_FIELD))
            for v in sorted(await _ventas(db_session, sample_tenant), key=lambda v: v.amount)
        ]
        for venta in await _ventas(db_session, sample_tenant):
            await db_session.delete(venta)
        await db_session.flush()

        await _importar_multihoja(db_session, sample_tenant, filas, _MAPEO_SIN_MONTO)
        multi = [
            (v.amount, v.quantity, v.unit_price, (v.custom_fields or {}).get(AMOUNT_SOURCE_FIELD))
            for v in sorted(await _ventas(db_session, sample_tenant), key=lambda v: v.amount)
        ]
        assert plano == multi
        # Y no es un empate vacío: las dos corridas importaron las dos filas.
        assert len(multi) == 2


#: Las MISMAS columnas, pero nombradas como las nombra una planilla argentina de
#: verdad. "precio_unitario" es un keyword de `_VENTA_AMOUNT_COLS`: la heurística
#: del monto se la lleva puesta si nadie le avisa que el usuario ya la declaró.
_HEADERS_TRAMPA = ["fecha", "producto", "precio_unitario", "cantidad"]
_MAPEO_TRAMPA = {
    "fecha": "transaction_date",
    "producto": "product_name",
    "precio_unitario": "unit_price",
    "cantidad": "quantity",
}


def _fila_trampa() -> dict[str, Any]:
    return {
        "fecha": "2024-03-10",
        "producto": "Vela aromatica",
        "precio_unitario": "150.50",
        "cantidad": "3",
    }


@pytest.mark.asyncio
class TestLaHeuristicaNoSePisaConElMapeo:
    """Una columna declarada a mano no puede releerse como otra cosa.

    `_VENTA_AMOUNT_COLS` contiene "precio_unitario" (legacy: en un archivo sin
    total, el precio unitario ERA el monto). Con F-H4 esa lectura pasó a ser
    activamente dañina: el archivo trae precio y cantidad, el monto se calcula
    bien, pero el "monto del archivo" que se compara contra el cálculo sale de la
    MISMA columna del precio → toda fila con cantidad > 1 se reporta como
    discrepancia, se le estampa `_vektor_amount_original` con un total que nadie
    escribió, y el confirm avisa "N filas tenían un monto distinto… suele ser un
    descuento o un impuesto" sobre un archivo sin una sola discrepancia.
    """

    async def test_multihoja_no_confunde_el_precio_con_el_monto(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        counts = await insert_confirmed_data(
            db_session,
            sample_tenant.tenant_id,
            {
                "file_type": "spreadsheet",
                "inferred_type": "mixed",
                "multi_sheet": True,
                "mapping_contexts": [
                    {
                        "context_id": _CTX,
                        "entity_type": "sale",
                        "source_kind": "sheet",
                        "headers": _HEADERS_TRAMPA,
                        "fields": None,
                        "preview_rows": [],
                        "row_count": 1,
                    }
                ],
                "ventas_detectadas": [{**_fila_trampa(), "__context__": _CTX}],
                "gastos_detectados": [],
                "stock_detectado": [],
            },
            {"ventas": True},
            context_mappings={_CTX: _MAPEO_TRAMPA},
            context_confirmed={_CTX: True},
        )
        await db_session.flush()

        assert counts["montos_calculados"] == 1
        assert counts["montos_discrepantes"] == 0

        venta = (await _ventas(db_session, sample_tenant))[0]
        assert venta.amount == Decimal("451.50")
        cf = venta.custom_fields or {}
        assert cf[AMOUNT_SOURCE_FIELD] == "calculated"
        assert AMOUNT_ORIGINAL_FIELD not in cf

    async def test_el_camino_plano_tampoco(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Acá el pisón entra por otra puerta: `venta_col` se resuelve con
        `_find_col` sobre los headers, antes de mirar el mapeo."""
        filas = [_fila_trampa()]
        counts = await insert_confirmed_data(
            db_session,
            sample_tenant.tenant_id,
            {
                "file_type": "spreadsheet",
                "inferred_type": "ventas",
                "multi_sheet": False,
                "has_venta": True,
                "row_count": 1,
                "headers": _HEADERS_TRAMPA,
                "ventas_detectadas": filas,
                "preview_rows": filas,
            },
            {"ventas": True},
            column_mappings=_MAPEO_TRAMPA,
        )
        await db_session.flush()

        assert counts["ventas"] == 1
        assert counts["montos_calculados"] == 1
        assert counts["montos_discrepantes"] == 0

        venta = (await _ventas(db_session, sample_tenant))[0]
        assert venta.amount == Decimal("451.50")
        assert AMOUNT_ORIGINAL_FIELD not in (venta.custom_fields or {})

    async def test_una_columna_de_total_sin_mapear_sigue_siendo_el_monto(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Control: la heurística NO se apaga entera. Una columna de total que
        nadie declaró sigue entrando como monto — y ahí sí, si no cuadra con
        precio × cantidad, la discrepancia es real y hay que reportarla."""
        fila = {**_fila_trampa(), "total": "400"}
        counts = await insert_confirmed_data(
            db_session,
            sample_tenant.tenant_id,
            {
                "file_type": "spreadsheet",
                "inferred_type": "ventas",
                "multi_sheet": False,
                "has_venta": True,
                "row_count": 1,
                "headers": [*_HEADERS_TRAMPA, "total"],
                "ventas_detectadas": [fila],
                "preview_rows": [fila],
            },
            {"ventas": True},
            column_mappings=_MAPEO_TRAMPA,
        )
        await db_session.flush()

        assert counts["montos_discrepantes"] == 1
        venta = (await _ventas(db_session, sample_tenant))[0]
        assert venta.amount == Decimal("451.50")
        assert (venta.custom_fields or {})[AMOUNT_ORIGINAL_FIELD] == "400"


@pytest.mark.asyncio
class TestDiscrepancia:
    async def test_el_monto_del_archivo_no_cuadra_gana_el_calculo(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        counts = await _importar_multihoja(
            db_session,
            sample_tenant,
            [_fila(declarado="400")],
            {**_MAPEO_SIN_MONTO, "valor_declarado": "amount"},
        )
        assert counts["montos_discrepantes"] == 1
        # No se cuenta como "calculado": el archivo SÍ traía un monto.
        assert counts["montos_calculados"] == 0

        venta = (await _ventas(db_session, sample_tenant))[0]
        assert venta.amount == Decimal("451.50")
        cf = venta.custom_fields or {}
        assert cf[AMOUNT_SOURCE_FIELD] == "recalculated"
        assert cf[AMOUNT_ORIGINAL_FIELD] == "400"

    async def test_dentro_del_centavo_se_guarda_el_del_archivo(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """451.51 vs 451.50: la diferencia de redondeo de una planilla no es una
        discrepancia de negocio, y el monto del archivo no se pisa."""
        counts = await _importar_multihoja(
            db_session,
            sample_tenant,
            [_fila(declarado="451.51")],
            {**_MAPEO_SIN_MONTO, "valor_declarado": "amount"},
        )
        assert counts["montos_discrepantes"] == 0
        assert counts["montos_calculados"] == 0

        venta = (await _ventas(db_session, sample_tenant))[0]
        assert venta.amount == Decimal("451.51")
        assert AMOUNT_SOURCE_FIELD not in (venta.custom_fields or {})


@pytest.mark.asyncio
class TestLoQueNoHabilitaElCalculo:
    async def test_solo_precio_unitario_no_inventa_una_venta(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Sin cantidad no hay cuenta que hacer. Y la fila no desaparece."""
        mapeo = {k: v for k, v in _MAPEO_SIN_MONTO.items() if k != "cant"}
        counts = await _importar_multihoja(db_session, sample_tenant, [_fila()], mapeo)

        assert counts["ventas"] == 0
        assert counts["filas_sin_monto"] == 1
        assert counts["otros"] == 1
        assert await _ventas(db_session, sample_tenant) == []

    async def test_solo_cantidad_no_inventa_una_venta(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        mapeo = {k: v for k, v in _MAPEO_SIN_MONTO.items() if k != "precio_unit"}
        counts = await _importar_multihoja(
            db_session, sample_tenant, [_fila(precio="")], mapeo
        )

        assert counts["ventas"] == 0
        assert counts["filas_sin_monto"] == 1
        assert await _ventas(db_session, sample_tenant) == []

    async def test_la_cantidad_vacia_no_vale_una_unidad(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """El piso en 1 de `_venta_cantidad` existe para el gate y la inserción;
        usarlo para derivar le pondría `precio × 1` a cada fila con la celda de
        cantidad en blanco — un monto que el archivo nunca dijo."""
        counts = await _importar_multihoja(
            db_session, sample_tenant, [_fila(cantidad="")], _MAPEO_SIN_MONTO
        )
        assert counts["ventas"] == 0
        assert counts["filas_sin_monto"] == 1

    async def test_un_campo_propio_llamado_amount_no_es_el_monto(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """`custom_field:amount` guarda el dato; no cubre ni alimenta el canónico.

        Sin esta distinción, la columna entraría como monto por la puerta de
        atrás y el import se comportaría distinto según cómo se llame el campo
        propio que eligió el usuario.
        """
        counts = await _importar_multihoja(
            db_session,
            sample_tenant,
            [_fila(declarado="400")],
            {**_MAPEO_SIN_MONTO, "valor_declarado": "custom_field:amount"},
        )
        assert counts["ventas"] == 1

        venta = (await _ventas(db_session, sample_tenant))[0]
        # El monto es el CALCULADO, no el 400 del campo propio.
        assert venta.amount == Decimal("451.50")
        cf = venta.custom_fields or {}
        assert cf[AMOUNT_SOURCE_FIELD] == "calculated"
        assert cf["amount"] == "400"  # el dato se guarda igual, como campo propio


@pytest.mark.asyncio
class TestLaFilaQueNoSeResuelve:
    async def test_una_fila_incompleta_no_arrastra_a_las_demas(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Dos filas resolubles y una sin precio: entran dos, la otra queda a la
        vista con el motivo. Antes desaparecía sin dejar rastro."""
        filas = [
            _fila(producto="Vela"),
            _fila(producto="Sahumerio", precio="", cantidad="2"),
            _fila(producto="Difusor", precio="80", cantidad="5"),
        ]
        counts = await _importar_multihoja(
            db_session, sample_tenant, filas, _MAPEO_SIN_MONTO
        )

        assert counts["ventas"] == 2
        assert counts["filas_sin_monto"] == 1
        assert counts["otros"] == 1

        montos = sorted(v.amount for v in await _ventas(db_session, sample_tenant))
        assert montos == [Decimal("400.00"), Decimal("451.50")]

        capturada = (await _otros(db_session, sample_tenant))[0]
        assert capturada.suggested_entity == "sale"
        assert "sin monto" in (capturada.context_label or "").lower()
        assert capturada.row_data["producto"] == "Sahumerio"

    async def test_una_fila_de_relleno_no_ensucia_otros(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Las planillas traen filas vacías al final de la hoja. Mandarlas a la
        bandeja la llenaría de ruido que nadie puede clasificar."""
        vacia = {h: "" for h in _HEADERS}
        counts = await _importar_multihoja(
            db_session, sample_tenant, [_fila(), vacia], _MAPEO_SIN_MONTO
        )

        assert counts["ventas"] == 1
        assert counts["filas_sin_monto"] == 0
        assert counts["otros"] == 0

    async def test_una_venta_derivada_sin_fecha_no_llega_a_la_base(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Sin fecha reconocible la fila va a "Otros" (F6-A2) — también si su monto
        es una cuenta.

        El guard de F6-A2 decide si rutear mirando si la fila "iba a registrar una
        operación fechada", y eso lo resolvía leyendo la columna de monto. En una
        hoja de precio × cantidad esa columna no existe: la fila no se ruteaba, y
        el bloque de ventas de más abajo SÍ calculaba el monto → `SaleEntry` con
        `transaction_date=None` y el import entero muerto con un NOT NULL. El
        invariante que el propio bloque declara ("tx_date=None nunca llega a un
        registro") dejó de valer cuando el monto pasó a poder derivarse.
        """
        counts = await _importar_plano(
            db_session, sample_tenant, [_fila(fecha="no es una fecha")], _MAPEO_SIN_MONTO
        )
        assert counts["ventas"] == 0
        assert counts["otros"] == 1
        assert await _ventas(db_session, sample_tenant) == []

        capturada = (await _otros(db_session, sample_tenant))[0]
        assert "fecha" in (capturada.context_label or "").lower()
        # Y la sugerencia dice venta: la hoja es de ventas.
        assert capturada.suggested_entity == "sale"

    async def test_el_camino_plano_captura_igual(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Paridad de la captura, no sólo del cálculo."""
        counts = await _importar_plano(
            db_session, sample_tenant, [_fila(precio="")], _MAPEO_SIN_MONTO
        )
        assert counts["ventas"] == 0
        assert counts["filas_sin_monto"] == 1
        assert counts["otros"] == 1
        assert "sin monto" in (
            (await _otros(db_session, sample_tenant))[0].context_label or ""
        ).lower()


@pytest.mark.asyncio
class TestF10Intacto:
    async def test_el_precio_unitario_nunca_sale_del_monto(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Monto + cantidad mapeados y ningún precio: `unit_price` queda NULL.

        En una fila histórica no se sabe si el monto es unitario o total —
        adivinarlo fue el incidente ASTERIA. La derivación va en un solo sentido.
        """
        mapeo = {
            k: v for k, v in _MAPEO_SIN_MONTO.items() if k != "precio_unit"
        } | {"valor_declarado": "amount"}
        counts = await _importar_multihoja(
            db_session,
            sample_tenant,
            [_fila(precio="", declarado="900")],
            mapeo,
        )
        assert counts["ventas"] == 1

        venta = (await _ventas(db_session, sample_tenant))[0]
        assert venta.amount == Decimal("900")
        assert venta.quantity == 3
        assert venta.unit_price is None
        assert AMOUNT_SOURCE_FIELD not in (venta.custom_fields or {})
