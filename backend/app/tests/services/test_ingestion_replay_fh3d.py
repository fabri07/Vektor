"""F-H3.d — la venta importada tiene que saber de qué hoja vino.

Sin esto el replay sólo podría aplicarse al archivo entero. Un libro con una hoja
de ventas viejas marcada `informational` y otra de ventas del mes marcada
`historical_replay` terminaría descontando las dos, que es exactamente lo que el
eje por hoja vino a evitar. `source_row_ref` no sirve para reconstruirlo: es el
sha256 del ancla y no se puede volver atrás (V18).

Los DOS caminos de inserción tienen que estamparlo. El de una sola tabla no
recorre contextos —no tiene por qué, hay uno solo—, y por eso venía perdiendo el
contexto entero: ni estampaba la hoja en la venta ni le pasaba el efecto
declarado a la proyección, así que un `.xlsx` plano quedaba clavado en el default
aunque el usuario hubiera elegido otra cosa.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.domain.inventory_effect import IMPORT_CONTEXT_FIELD
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord

_VENTAS = "sheet:ventas"
_TABLA = "table:0"
_PRODUCTO = "Vela aromática 200g"

_MAPPING_VENTAS = {
    "fecha": "transaction_date",
    "producto": "product_name",
    "cantidad": "quantity",
    "monto": "amount",
}


def _fila_venta(context_id: str | None = None) -> dict[str, Any]:
    fila = {
        "fecha": "2024-03-10",
        "producto": _PRODUCTO,
        "cantidad": "2",
        "monto": "2100",
    }
    if context_id is not None:
        fila["__context__"] = context_id
    return fila


def _ctx(context_id: str, label: str) -> dict[str, Any]:
    return {
        "context_id": context_id,
        "label": label,
        "source_kind": "sheet",
        "entity_type": "sale",
        "headers": ["fecha", "producto", "cantidad", "monto"],
        "fields": None,
        "preview_rows": [],
        "row_count": 1,
    }


async def _crear_producto(db: AsyncSession, tenant: Tenant, stock: int = 10) -> Product:
    """El producto tiene que existir: una venta sólo entra a la proyección si resuelve."""
    producto = Product(
        id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        name=_PRODUCTO,
        sale_price_ars=Decimal("1050"),
        unit_cost_ars=Decimal("600"),
        stock_units=stock,
    )
    db.add(producto)
    await db.flush()
    return producto


async def _venta_unica(db: AsyncSession) -> SaleEntry:
    ventas = (await db.execute(select(SaleEntry))).scalars().all()
    assert len(ventas) == 1, f"se esperaba una venta, hay {len(ventas)}"
    return ventas[0]


class TestLaVentaGuardaSuHoja:
    async def test_multi_hoja_estampa_el_contexto(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        await _crear_producto(db_session, sample_tenant)
        summary = {
            "file_type": "spreadsheet",
            "inferred_type": "mixed",
            "multi_sheet": True,
            "has_venta": True,
            "row_count": 1,
            "ventas_detectadas": [_fila_venta(_VENTAS)],
            "mapping_contexts": [_ctx(_VENTAS, "Ventas")],
        }

        await importer.insert_confirmed_data(
            db_session,
            sample_tenant.tenant_id,
            summary,
            {"ventas": True},
            context_mappings={_VENTAS: _MAPPING_VENTAS},
            context_confirmed={_VENTAS: True},
            inventory_effect={_VENTAS: "informational"},
        )
        await db_session.flush()

        venta = await _venta_unica(db_session)
        assert venta.custom_fields.get(IMPORT_CONTEXT_FIELD) == _VENTAS

    async def test_una_sola_tabla_tambien_estampa_su_hoja(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """El archivo plano tiene UNA hoja, y la venta tiene que decir cuál es."""
        await _crear_producto(db_session, sample_tenant)
        summary = {
            "file_type": "spreadsheet",
            "inferred_type": "ventas",
            "has_venta": True,
            "row_count": 1,
            "ventas_detectadas": [_fila_venta()],
            "mapping_contexts": [_ctx(_TABLA, "Hoja 1")],
        }

        await importer.insert_confirmed_data(
            db_session,
            sample_tenant.tenant_id,
            summary,
            {"ventas": True},
            column_mappings=_MAPPING_VENTAS,
            inventory_effect={_TABLA: "informational"},
        )
        await db_session.flush()

        venta = await _venta_unica(db_session)
        assert venta.custom_fields.get(IMPORT_CONTEXT_FIELD) == _TABLA

    async def test_una_venta_manual_no_gana_la_clave(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Control: la clave marca procedencia de import, no es un campo de toda venta."""
        venta = SaleEntry(
            tenant_id=sample_tenant.tenant_id,
            amount=Decimal("2100"),
            quantity=2,
            transaction_date=datetime(2024, 3, 10),
        )
        db_session.add(venta)
        await db_session.flush()

        assert IMPORT_CONTEXT_FIELD not in (venta.custom_fields or {})


class TestElEfectoDeclaradoLlegaALaProyeccion:
    async def test_una_sola_tabla_honra_no_inventory(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """`no_inventory` en un archivo plano tiene que sacar la fila de la proyección.

        Es el caso que delata el bug: sin pasarle el contexto al registrador, la
        hoja caía al default y el producto aparecía igual en el impacto, con el
        usuario habiendo declarado lo contrario.
        """
        await _crear_producto(db_session, sample_tenant)
        summary = {
            "file_type": "spreadsheet",
            "inferred_type": "ventas",
            "has_venta": True,
            "row_count": 1,
            "ventas_detectadas": [_fila_venta()],
            "mapping_contexts": [_ctx(_TABLA, "Hoja 1")],
        }

        counts = await importer.insert_confirmed_data(
            db_session,
            sample_tenant.tenant_id,
            summary,
            {"ventas": True},
            column_mappings=_MAPPING_VENTAS,
            inventory_effect={_TABLA: "no_inventory"},
        )

        assert counts["ventas"] == 1
        assert counts["impacto_inventario"] == []

    async def test_control_sin_declaracion_la_fila_si_proyecta(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Control del anterior: con el default, la misma fila SÍ entra al impacto.

        Sin esto, "no aparece en el impacto" no probaría nada — podría no aparecer
        porque el producto no resolvió o porque la proyección no corre en este camino.
        """
        await _crear_producto(db_session, sample_tenant)
        summary = {
            "file_type": "spreadsheet",
            "inferred_type": "ventas",
            "has_venta": True,
            "row_count": 1,
            "ventas_detectadas": [_fila_venta()],
            "mapping_contexts": [_ctx(_TABLA, "Hoja 1")],
        }

        counts = await importer.insert_confirmed_data(
            db_session,
            sample_tenant.tenant_id,
            summary,
            {"ventas": True},
            column_mappings=_MAPPING_VENTAS,
            inventory_effect={_TABLA: "informational"},
        )

        assert [p["product_name"] for p in counts["impacto_inventario"]] == [_PRODUCTO]
        assert counts["impacto_inventario"][0]["vendidas"] == 2


# --- F-H3.d.3: la venta sin stock que la respalde no entra ---------------------
#
# Bajo `historical_replay` la hoja pide que sus ventas descuenten. Una venta que
# no tiene unidades detrás no se puede aplicar de ninguna forma honesta: dejar el
# stock negativo inventa un inventario que nadie tiene, y clampear a cero hace que
# el movimiento diga una cosa y el stock otra (y entonces borrar el archivo lo
# infla). La fila va a "Otros" y el usuario la registra después de cargar el stock.

_VENTAS_A = "sheet:ventas-a"
_VENTAS_B = "sheet:ventas-b"


def _summary_dos_ventas(
    *,
    fecha_primera: str,
    fecha_segunda: str,
    cantidad: int = 6,
) -> dict[str, Any]:
    """Dos ventas del mismo producto en UNA hoja."""
    return {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "has_venta": True,
        "row_count": 2,
        "ventas_detectadas": [
            {
                "fecha": fecha_primera,
                "producto": _PRODUCTO,
                "cantidad": str(cantidad),
                "monto": "2100",
                "__context__": _VENTAS,
            },
            {
                "fecha": fecha_segunda,
                "producto": _PRODUCTO,
                "cantidad": str(cantidad),
                "monto": "2100",
                "__context__": _VENTAS,
            },
        ],
        "mapping_contexts": [_ctx(_VENTAS, "Ventas")],
    }


async def _importar_ventas(
    db: AsyncSession,
    tenant: Tenant,
    summary: dict[str, Any],
    efectos: dict[str, str],
) -> dict[str, Any]:
    contextos = [str(c["context_id"]) for c in summary["mapping_contexts"]]
    return await importer.insert_confirmed_data(
        db,
        tenant.tenant_id,
        summary,
        {"ventas": True},
        context_mappings=dict.fromkeys(contextos, _MAPPING_VENTAS),
        context_confirmed=dict.fromkeys(contextos, True),
        inventory_effect=efectos,
    )


async def _otros(db: AsyncSession) -> list[UnclassifiedRecord]:
    return list((await db.execute(select(UnclassifiedRecord))).scalars().all())


class TestVentaSinRespaldoNoEntra:
    async def test_replay_manda_a_otros_la_que_no_se_puede_cubrir(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """10 unidades y dos ventas de 6: entra la primera, la segunda va a Otros."""
        await _crear_producto(db_session, sample_tenant, stock=10)

        counts = await _importar_ventas(
            db_session,
            sample_tenant,
            _summary_dos_ventas(fecha_primera="2024-03-03", fecha_segunda="2024-03-10"),
            {_VENTAS: "historical_replay"},
        )
        await db_session.flush()

        assert counts["ventas"] == 1
        assert counts["ventas_sin_stock"] == 1
        venta = await _venta_unica(db_session)
        assert venta.quantity == 6
        assert venta.transaction_date.strftime("%Y-%m-%d") == "2024-03-03"
        capturas = await _otros(db_session)
        assert len(capturas) == 1
        assert "sin stock que la respalde" in (capturas[0].context_label or "")

    async def test_la_rechazada_es_la_mas_nueva_aunque_venga_primera_en_el_archivo(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Control del anterior: el archivo con las filas dadas vuelta da lo mismo.

        Si el gate recorriera en el orden del Excel, acá entraría la del 10/03 y se
        rechazaría la del 03/03 — el resultado dependería de cómo ordenó las filas
        quien armó la planilla.
        """
        await _crear_producto(db_session, sample_tenant, stock=10)

        await _importar_ventas(
            db_session,
            sample_tenant,
            _summary_dos_ventas(fecha_primera="2024-03-10", fecha_segunda="2024-03-03"),
            {_VENTAS: "historical_replay"},
        )
        await db_session.flush()

        venta = await _venta_unica(db_session)
        assert venta.transaction_date.strftime("%Y-%m-%d") == "2024-03-03"

    async def test_con_el_default_entran_las_dos(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """`informational` no gatea nada: el archivo entra entero y sólo se informa.

        Es el control que impide que el gate se convierta en una regla global. Sin
        esto, "la fila no entró" no distinguiría entre el modo declarado y un bug
        que rechaza ventas siempre.
        """
        await _crear_producto(db_session, sample_tenant, stock=10)

        counts = await _importar_ventas(
            db_session,
            sample_tenant,
            _summary_dos_ventas(fecha_primera="2024-03-03", fecha_segunda="2024-03-10"),
            {_VENTAS: "informational"},
        )
        await db_session.flush()

        assert counts["ventas"] == 2
        assert counts.get("ventas_sin_stock", 0) == 0
        assert await _otros(db_session) == []

    async def test_el_stock_no_se_toca_ni_con_replay_declarado(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Confirmar sigue sin mover stock: el replay es el paso posterior (F-H3.c).

        El gate decide qué se puede importar, no aplica nada.
        """
        producto = await _crear_producto(db_session, sample_tenant, stock=10)

        await _importar_ventas(
            db_session,
            sample_tenant,
            _summary_dos_ventas(fecha_primera="2024-03-03", fecha_segunda="2024-03-10"),
            {_VENTAS: "historical_replay"},
        )
        await db_session.flush()
        await db_session.refresh(producto)

        assert producto.stock_units == 10

    async def test_dos_hojas_de_ventas_comparten_el_mismo_stock(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """El gate corre para el archivo, no por hoja.

        Con 10 unidades y una venta de 6 en cada hoja, evaluar hoja por hoja dejaría
        entrar las dos (6 <= 10 las dos veces) y el archivo consumiría 12.
        """
        await _crear_producto(db_session, sample_tenant, stock=10)
        summary = {
            "file_type": "spreadsheet",
            "inferred_type": "mixed",
            "multi_sheet": True,
            "has_venta": True,
            "row_count": 2,
            "ventas_detectadas": [
                {
                    "fecha": "2024-03-03",
                    "producto": _PRODUCTO,
                    "cantidad": "6",
                    "monto": "2100",
                    "__context__": _VENTAS_A,
                },
                {
                    "fecha": "2024-03-10",
                    "producto": _PRODUCTO,
                    "cantidad": "6",
                    "monto": "2100",
                    "__context__": _VENTAS_B,
                },
            ],
            "mapping_contexts": [_ctx(_VENTAS_A, "Ventas 1"), _ctx(_VENTAS_B, "Ventas 2")],
        }

        counts = await _importar_ventas(
            db_session,
            sample_tenant,
            summary,
            {_VENTAS_A: "historical_replay", _VENTAS_B: "historical_replay"},
        )
        await db_session.flush()

        assert counts["ventas"] == 1
        assert counts["ventas_sin_stock"] == 1

    async def test_reconfirmar_no_duplica_la_captura_en_otros(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """La fila derivada a Otros es output persistido: su huella tiene que quedar.

        Sin registrar el fingerprint, re-confirmar el archivo dejaría dos veces la
        misma venta en la bandeja.
        """
        await _crear_producto(db_session, sample_tenant, stock=10)
        file_id = uuid.uuid4()
        summary = _summary_dos_ventas(fecha_primera="2024-03-03", fecha_segunda="2024-03-10")

        for _ in range(2):
            contextos = [str(c["context_id"]) for c in summary["mapping_contexts"]]
            await importer.insert_confirmed_data(
                db_session,
                sample_tenant.tenant_id,
                summary,
                {"ventas": True},
                context_mappings=dict.fromkeys(contextos, _MAPPING_VENTAS),
                context_confirmed=dict.fromkeys(contextos, True),
                inventory_effect={_VENTAS: "historical_replay"},
                uploaded_file_id=file_id,
            )
            await db_session.flush()

        assert len(await _otros(db_session)) == 1
        assert len((await db_session.execute(select(SaleEntry))).scalars().all()) == 1


class TestElGateTambienCorreEnElArchivoPlano:
    """El camino de una sola tabla es el import más común que existe.

    Gatear sólo los libros multi-hoja dejaría el caso más frecuente —"mis ventas
    del año" en un CSV— sin la protección, y nadie lo notaría hasta ver el stock.
    """

    def _summary(self, cantidad_1: str, cantidad_2: str) -> dict[str, Any]:
        return {
            "file_type": "spreadsheet",
            "inferred_type": "ventas",
            "has_venta": True,
            "row_count": 2,
            "ventas_detectadas": [
                {
                    "fecha": "2024-03-03",
                    "producto": _PRODUCTO,
                    "cantidad": cantidad_1,
                    "monto": "2100",
                },
                {
                    "fecha": "2024-03-10",
                    "producto": _PRODUCTO,
                    "cantidad": cantidad_2,
                    "monto": "2100",
                },
            ],
            "mapping_contexts": [_ctx(_TABLA, "Hoja 1")],
        }

    async def _importar(
        self,
        db: AsyncSession,
        tenant: Tenant,
        summary: dict[str, Any],
        efecto: str,
        confirmados: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        return await importer.insert_confirmed_data(
            db,
            tenant.tenant_id,
            summary,
            confirmados or {"ventas": True},
            column_mappings=_MAPPING_VENTAS,
            inventory_effect={_TABLA: efecto},
        )

    async def test_manda_a_otros_la_que_no_se_puede_cubrir(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        await _crear_producto(db_session, sample_tenant, stock=10)

        counts = await self._importar(
            db_session, sample_tenant, self._summary("6", "6"), "historical_replay"
        )
        await db_session.flush()

        assert counts["ventas"] == 1
        assert counts["ventas_sin_stock"] == 1
        assert (await _venta_unica(db_session)).transaction_date.strftime(
            "%Y-%m-%d"
        ) == "2024-03-03"

    async def test_con_el_default_entran_las_dos(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        await _crear_producto(db_session, sample_tenant, stock=10)

        counts = await self._importar(
            db_session, sample_tenant, self._summary("6", "6"), "informational"
        )
        await db_session.flush()

        assert counts["ventas"] == 2
        assert counts.get("ventas_sin_stock", 0) == 0

    async def test_si_el_archivo_tambien_carga_productos_degrada_y_no_dice_que_aplico(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """F-H3.d.6 — el respaldo del importador, no el camino normal.

        No hay pasadas separadas acá: el stock que el archivo declara todavía no
        existe cuando el gate mira, así que rechazaría ventas contra un saldo que el
        propio archivo está por cargar. Ese archivo el confirm lo rechaza con 422
        antes de tomar el lease (ver `test_ingestion_replay_no_gateable.py`); si
        igual llega hasta acá —el confirm mira el mapeo declarado y el importador
        las columnas ya resueltas— la hoja se degrada a `informational`.

        Antes se abstenía y seguía marcada como `historical_replay`: las ventas sin
        respaldo entraban a los libros igual y el import se reportaba como un replay.
        Lo que se afirma acá es lo segundo — que NO se reporta como replay aplicado.
        """
        await _crear_producto(db_session, sample_tenant, stock=10)
        summary = self._summary("6", "6")
        summary["has_producto"] = True

        counts = await self._importar(
            db_session,
            sample_tenant,
            summary,
            "historical_replay",
            confirmados={"ventas": True, "productos": True},
        )
        await db_session.flush()

        assert counts["replay_degradado"] == 1
        # La degradación tiene que verse en el contador que el confirm traza: si
        # dijera 1, la traza afirmaría un replay que no ocurrió.
        assert counts["hojas_con_replay"] == 0
        assert "replay_sin_gatear" not in counts
        # El import se preserva entero (no se aborta) y ninguna venta se rechaza:
        # sin saldo previo confiable, rechazar sería inventar el criterio.
        assert counts["ventas"] == 2
        assert counts.get("ventas_sin_stock", 0) == 0
