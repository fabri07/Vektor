"""Fase 2 del incidente ASTERIA (2026-10-05): clasificar hojas por contenido.

Medido sobre el archivo real (``docs/plans/ingesta-archivos-generados-y-derivados.md``):
un catálogo con columna de proveedor caía como maestro de proveedores, un libro
mixto como stock y una hoja de resumen como ventas. Cada sección cubre una de
esas causas, más los casos que NO tienen que cambiar.
"""

from __future__ import annotations

import pytest

from app.application.services.file_parsing import analyze_headers


def _tipo(headers: list[str]) -> str:
    return str(analyze_headers(headers)["inferred_type"])


# ── F2a: un catálogo que nombra al proveedor sigue siendo un catálogo ─────────


def test_catalogo_con_columna_de_proveedor_es_stock() -> None:
    """La hoja real: "Proveedores" disparaba la regla de maestro, y lo único que
    la frenaba era un sku/código/artículo. Un maestro no lista productos."""
    assert (
        _tipo(
            [
                "Proveedores",
                "Productos ",
                "Especificaciones",
                "Stock",
                "Precio de compra",
                "Envio",
                "compra+envio",
                "Precio de lista 80% ",
                "Precio de lista 40% ",
                "Precio de venta final",
            ]
        )
        == "stock"
    )


@pytest.mark.parametrize(
    "headers",
    [
        ["Proveedor", "CUIL", "Teléfono", "Email"],
        ["Nombre", "Proveedor", "Contacto", "Localidad"],
        ["Proveedores", "CUIL", "Dirección"],
    ],
)
def test_un_maestro_de_proveedores_sigue_siendolo(headers: list[str]) -> None:
    assert _tipo(headers) == "proveedores"


def test_un_catalogo_con_producto_stock_y_proveedor_es_stock() -> None:
    assert _tipo(["Proveedor", "Producto", "Stock", "Precio"]) == "stock"


# ── F2b: una hoja de totales por período junto a sus movimientos es derivada ──

import io  # noqa: E402
from datetime import datetime  # noqa: E402
from typing import Any  # noqa: E402

from openpyxl import Workbook  # noqa: E402

from app.application.services.file_parsing import parse_uploaded_content  # noqa: E402

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _libro(hojas: dict[str, list[list[Any]]]) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    for nombre, filas in hojas.items():
        ws = wb.create_sheet(nombre)
        for fila in filas:
            ws.append(fila)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


_MOVIMIENTOS: list[list[Any]] = [
    ["Fecha", "Concepto", "Cliente", "Venta cobrada $"],
    [datetime(2026, 6, 3), "vaso", "Ana", 1200],
    [datetime(2026, 6, 9), "bandeja", "Luis", 3400],
    [datetime(2026, 7, 2), "vela", "Ana", 800],
]
_RESUMEN: list[list[Any]] = [
    ["Período", "Ventas $", "Margen bruto $", "Ticket promedio $", "Saldo de caja acumulado $"],
    ["2026-06", 4600, 2100, 2300, 4600],
    ["2026-07", 800, 300, 800, 5400],
]

_NOTAS: list[list[Any]] = [
    ["Fecha", "Nota"],
    [datetime(2026, 6, 1), "x"],
    [datetime(2026, 6, 2), "y"],
]


def _contexto(summary: dict[str, Any], hoja: str) -> dict[str, Any]:
    return next(c for c in summary["mapping_contexts"] if c["label"] == hoja)


def test_resumen_numerado_junto_a_movimientos_es_derivado() -> None:
    """El caso real: "3. Resumen mensual". El prefijo de número escondía el
    nombre, y el contenido —totales por mes— no se miraba."""
    summary = parse_uploaded_content(
        _libro({"2. Movimientos": _MOVIMIENTOS, "3. Resumen mensual": _RESUMEN}), _XLSX, "x.xlsx"
    )
    ctx = _contexto(summary, "3. Resumen mensual")
    assert ctx["is_summary_or_derived"] is True
    assert ctx["entity_type"] is None
    assert not any(
        f.get("__context__") == ctx["context_id"] for f in summary.get("ventas_detectadas", [])
    ), "las filas del resumen no pueden quedar en el bucket de ventas"


def test_totales_por_periodo_con_otro_nombre_tambien_son_derivados() -> None:
    summary = parse_uploaded_content(
        _libro({"Movimientos": _MOVIMIENTOS, "Tablero": _RESUMEN}), _XLSX, "x.xlsx"
    )
    assert _contexto(summary, "Tablero")["is_summary_or_derived"] is True


def test_totales_por_periodo_sin_movimientos_al_lado_se_importan() -> None:
    """Sin la hoja de la que salen, los totales mensuales son el dato del usuario."""
    summary = parse_uploaded_content(
        _libro({"Ventas mensuales": _RESUMEN, "Notas": _NOTAS}),
        _XLSX,
        "x.xlsx",
    )
    assert not _contexto(summary, "Ventas mensuales").get("is_summary_or_derived")


def test_la_hoja_de_movimientos_no_es_derivada() -> None:
    summary = parse_uploaded_content(
        _libro({"2. Movimientos": _MOVIMIENTOS, "3. Resumen mensual": _RESUMEN}), _XLSX, "x.xlsx"
    )
    assert not _contexto(summary, "2. Movimientos").get("is_summary_or_derived")


# ── F2c: libro mixto — la columna de tipo decide fila por fila ────────────────

from decimal import Decimal  # noqa: E402

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.application.services.file_parsing import separar_libro_mixto  # noqa: E402
from app.application.services.ingestion_import_service import (  # noqa: E402
    insert_confirmed_data,
)
from app.domain.tipo_de_movimiento import clasificar  # noqa: E402
from app.persistence.models.tenant import Tenant  # noqa: E402
from app.persistence.models.transaction import ExpenseEntry, SaleEntry  # noqa: E402
from app.persistence.models.unclassified_record import UnclassifiedRecord  # noqa: E402


def _mov(
    dia: int,
    tipo: str,
    concepto: str,
    *,
    cliente: str | None = None,
    venta: float | None = None,
    costo: float | None = None,
    salida: float | None = None,
    ingreso: float | None = None,
    saldo: float = 0,
    origen: str = "registro original",
) -> list[Any]:
    fecha = datetime(2026, 6, dia)
    return [fecha, tipo, concepto, cliente, venta, costo, salida, ingreso, saldo, origen]


_LIBRO_MIXTO: list[list[Any]] = [
    ["NEGOCIO — Movimientos consolidados", None, None, None, None, None, None, None],
    [
        "Fecha",
        "Tipo de movimiento",
        "Concepto",
        "Cliente",
        "Venta cobrada $",
        "Costo de lo vendido $",
        "Salida de caja $",
        "Otro ingreso de caja $",
        "Saldo de caja $",
        "Origen del dato",
    ],
    _mov(1, "Saldo inicial", "caja anterior", ingreso=50000, saldo=50000),
    _mov(2, "Venta", "vaso", cliente="Ana", venta=12000.5, costo=6000, saldo=62000.5),
    _mov(3, "Venta (costo sin registrar)", "bandeja", venta=8000, saldo=70000.5),
    _mov(4, "Compra de mercadería", "compra mayorista", salida=30000, saldo=40000.5),
    _mov(5, "Flete / envío", "envio courier", salida=2500, saldo=38000.5),
    _mov(6, "Aporte de socio", "entrada de un socio", ingreso=10000, saldo=48000.5),
    _mov(7, "Mercadería recibida", "cuadernos", saldo=48000.5),
    _mov(8, "Retiro de socios", "retiro ganancia", salida=5000, saldo=-3000),
    _mov(9, "Trueque", "cambio con vecino", salida=100, saldo=-3100),
    _mov(10, "Venta", "vela", cliente="Luis", venta=99999, costo=1, origen="simulado"),
]


@pytest.mark.parametrize(
    ("tipo", "destino"),
    [
        ("Venta", "venta"),
        ("Venta (costo sin registrar)", "venta"),
        ("Gasto operativo", "gasto"),
        ("Flete / envío", "gasto"),
        ("Packaging / insumos", "gasto"),
        ("Publicidad", "gasto"),
        ("Compra de mercadería", "compra"),
        ("Mercadería recibida", "otros"),
        ("Devolución de proveedor", "otros"),
        ("Aporte de socio", "otros"),
        ("Retiro de socios", "otros"),
        ("Préstamo a socio", "otros"),
        ("Ajuste", "otros"),
        ("Saldo inicial", "otros"),
        ("Trueque", "otros"),
        (None, "otros"),
    ],
)
def test_vocabulario_de_tipos(tipo: str | None, destino: str) -> None:
    """Los 14 tipos reales de ASTERIA más un desconocido: nunca se adivina."""
    assert clasificar(tipo).destino == destino


def test_una_hoja_de_solo_ventas_con_columna_tipo_no_es_mixta() -> None:
    filas = [{"Tipo": "Venta", "Monto": "100"}, {"Tipo": "Venta", "Monto": "200"}]
    assert separar_libro_mixto(["Tipo", "Monto"], filas) is None


def test_el_libro_mixto_se_reparte_por_fila() -> None:
    summary = parse_uploaded_content(
        _libro({"2. Movimientos": _LIBRO_MIXTO, "Notas": _NOTAS}), _XLSX, "x.xlsx"
    )
    por_etiqueta = {c["label"]: c for c in summary["mapping_contexts"]}
    ventas = por_etiqueta["2. Movimientos — Ventas"]
    gastos = por_etiqueta["2. Movimientos — Gastos y compras"]
    assert ventas["entity_type"] == "sale" and ventas["row_count"] == 2
    assert gastos["entity_type"] == "expense" and gastos["row_count"] == 2
    # El importe es la columna de FLUJO de cada tipo, ya interpretado; nunca el
    # costo ni el saldo.
    assert sorted(Decimal(f["monto"]) for f in summary["ventas_detectadas"]) == [
        Decimal("8000"),
        Decimal("12000.5"),
    ]
    assert sorted(Decimal(f["monto"]) for f in summary["gastos_detectados"]) == [
        Decimal("2500"),
        Decimal("30000"),
    ]
    pendientes = {
        c["label"].split(" — ", 1)[1]: c["row_count"]
        for c in summary["mapping_contexts"]
        if c["label"].startswith("2. Movimientos — ") and c["entity_type"] is None
    }
    # Cada movimiento que no es venta ni gasto queda con un motivo propio.
    assert sum(pendientes.values()) == 6
    assert any(m.startswith("Aporte de socio") for m in pendientes)
    assert any(m.startswith("Ingreso de mercadería sin pago") for m in pendientes)
    assert any("Trueque" in m for m in pendientes)
    assert any("no reales" in m for m in pendientes), "la fila simulada se aparta"


async def test_el_libro_mixto_importa_ventas_y_gastos_y_deja_el_resto_en_otros(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """De punta a punta con el importador real: Σ ventas == Σ de las filas
    reales de venta; la compra entra como COGS; nada de capital se importa."""
    summary = parse_uploaded_content(
        _libro({"2. Movimientos": _LIBRO_MIXTO, "Notas": _NOTAS}), _XLSX, "x.xlsx"
    )
    counts = await insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"ventas": True, "gastos": True}
    )
    assert counts["ventas"] == 2
    assert counts["gastos"] == 2
    ventas = (await db_session.execute(select(SaleEntry))).scalars().all()
    assert sum(v.amount for v in ventas) == Decimal("20000.50")
    gastos = {
        g.description: g for g in (await db_session.execute(select(ExpenseEntry))).scalars()
    }
    assert gastos["compra mayorista"].expense_type == "COGS"
    assert gastos["compra mayorista"].amount == Decimal("30000")
    pendientes = (
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
    # 6 del libro mixto + 2 notas sin clasificar.
    assert len(pendientes) == 8


# ── F2d: lo que el archivo declara no real no entra como real ────────────────

from app.application.services.file_parsing import separar_filas_no_reales  # noqa: E402


@pytest.mark.parametrize("valor", ["simulado", "Dato estimado", "PROYECTADO 2027", "ejemplo"])
def test_filas_marcadas_como_no_reales(valor: str) -> None:
    filas = [
        {"Origen del dato": "registro original", "m": "1"},
        {"Origen del dato": valor, "m": "2"},
    ]
    reales, no_reales, columna = separar_filas_no_reales(["Origen del dato", "m"], filas)
    assert [f["m"] for f in reales] == ["1"]
    assert [f["m"] for f in no_reales] == ["2"]
    assert columna == "Origen del dato"


def test_sin_columna_de_procedencia_no_se_aparta_nada() -> None:
    """"simulado" en una columna cualquiera no es una declaración de procedencia."""
    filas = [{"Detalle": "vela simulada", "m": "1"}]
    reales, no_reales, _ = separar_filas_no_reales(["Detalle", "m"], filas)
    assert reales == filas and no_reales == []


def test_una_tabla_suelta_con_filas_simuladas_avisa() -> None:
    """Límite declarado: en una tabla suelta sólo se avisa."""
    csv = "Fecha,Cliente,Monto,Origen\n2026-06-01,Ana,100,real\n2026-06-02,Luis,200,simulado\n"
    summary = parse_uploaded_content(csv.encode(), "text/csv", "v.csv")
    assert any("simuladas" in w for w in summary["warnings"])


# ── F2e: un monto negativo dice qué parece, nunca se corrige solo ────────────

from app.domain.monto_negativo import motivo_de_negativo  # noqa: E402


def test_negativo_en_fila_de_devolucion() -> None:
    fila = {"Concepto": "Devolución cliente vaso", "Monto": "-1200"}
    assert motivo_de_negativo(Decimal("-1200"), fila) == "parece_devolucion"


def test_negativo_que_coincide_con_cantidad_por_precio() -> None:
    fila = {"Concepto": "vaso", "Monto": "-3600"}
    assert (
        motivo_de_negativo(Decimal("-3600"), fila, cantidad="3", precio_unitario="1200")
        == "signo_probablemente_invertido"
    )


def test_negativo_que_no_cuadra_con_la_fila() -> None:
    fila = {"Concepto": "vaso", "Monto": "-3500"}
    assert (
        motivo_de_negativo(Decimal("-3500"), fila, cantidad="3", precio_unitario="1200")
        == "no_cuadra_con_la_fila"
    )


def test_negativo_sin_contexto() -> None:
    assert motivo_de_negativo(Decimal("-10"), {"Concepto": "vaso"}) == "negativo_sin_contexto"


async def test_una_venta_negativa_va_a_otros_con_su_motivo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Por el importador real: la venta negativa no entra y su pendiente dice
    por qué —antes decía "mapeá la columna del monto" sobre una columna mapeada."""
    csv = (
        "Fecha,Cliente,Detalle,Total\n"
        "2026-06-01,Ana,vaso,1200\n"
        "2026-06-02,Luis,Devolución bandeja,-800\n"
    )
    summary = parse_uploaded_content(csv.encode(), "text/csv", "v.csv")
    counts = await insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"ventas": True}
    )
    assert counts["ventas"] == 1
    pendiente = (
        (await db_session.execute(select(UnclassifiedRecord))).scalars().one()
    )
    assert "devolución" in (pendiente.context_label or "").lower()


# ── Compuerta: un libro con la FORMA del de ASTERIA, de punta a punta ─────────

from app.application.services.validation_gate import ValidationGate  # noqa: E402
from app.domain.texto_nulo import buscar_nulo  # noqa: E402


def _libro_con_forma_de_asteria() -> bytes:
    """Misma estructura que el archivo real, sin un solo dato del cliente:
    títulos arriba del encabezado, catálogo con proveedor y fórmulas sin
    calcular, libro mixto con filas simuladas, resumen por período con flujo
    negativo, y notas fechadas."""
    wb = Workbook()
    precios = wb.active
    precios.title = "1. Precios y stock"
    precios.append([None, None, None, None, None, None, None, 0.8])
    precios.append(
        ["Proveedores", "Productos ", "Stock", "Precio de compra", "Envio", "compra+envio",
         "Precio de lista 80% ", "Precio de venta final"]
    )
    for i in range(3, 9):
        precios.append(
            ["Mayorista Uno", f"Producto {i}", i, 1000 * i, f"=D{i}*0.1", f"=(D{i}+E{i})",
             f"=F{i}+(F{i}*H$1)", 3000 * i]
        )
    movimientos = wb.create_sheet("2. Movimientos")
    movimientos.append(["NEGOCIO — Movimientos consolidados"])
    movimientos.append(["Verde = entra dinero"])
    movimientos.append([])
    for fila in _LIBRO_MIXTO[1:]:
        movimientos.append(fila)
    resumen = wb.create_sheet("3. Resumen mensual")
    resumen.append(["Resumen por período"])
    resumen.append([])
    resumen.append(["Período", "Ventas $", "Margen bruto $", "Operaciones", "Flujo neto de caja $",
                    "Saldo de caja acumulado $"])
    resumen.append(["2026-06", 20000.5, 9000, 2, -19500, -3100])
    notas = wb.create_sheet("4. Notas y pendientes")
    notas.append(["Hoja de origen", "Fecha de referencia", "Nota / pedido"])
    notas.append(["LD", datetime(2026, 6, 1), "pedido mayorista"])
    notas.append(["LD", datetime(2026, 6, 2), "llamar proveedor"])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


async def test_libro_con_forma_de_asteria_de_punta_a_punta(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    summary = parse_uploaded_content(_libro_con_forma_de_asteria(), _XLSX, "asteria.xlsx")

    # Fase 1: guardable en Postgres, y el gate no lo rechaza por el flujo negativo.
    assert buscar_nulo(summary) is None
    assert ValidationGate().validate(summary).passed

    por_hoja = {c["label"]: c for c in summary["mapping_contexts"]}
    assert por_hoja["1. Precios y stock"]["entity_type"] == "product"
    assert por_hoja["3. Resumen mensual"]["is_summary_or_derived"] is True
    assert por_hoja["2. Movimientos — Ventas"]["entity_type"] == "sale"
    assert por_hoja["2. Movimientos — Gastos y compras"]["entity_type"] == "expense"

    counts = await insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"ventas": True, "gastos": True}
    )
    ventas = (await db_session.execute(select(SaleEntry))).scalars().all()
    # Sólo las dos ventas reales: ni la simulada ni el resumen suman otra vez.
    assert len(ventas) == 2
    assert sum(v.amount for v in ventas) == Decimal("20000.50")
    assert counts["gastos"] == 2
