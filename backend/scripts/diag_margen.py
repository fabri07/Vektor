"""Read-only: por qué el margen de un negocio da lo que da (M·0, fase 2).

Paso M·0 de ``docs/plans/cierre-ingesta-capital-margen.md``. Hoy el margen se
calcula en varios lugares con fórmulas distintas, y el «margen en 0» de ASTERIA no
se puede explicar sin ver los datos. Este script NO cambia ningún cálculo: mide,
por tenant,

- el catálogo activo: precio y costo, costo nulo, costo 0 (que
  ``ProductResponse.margin_pct`` muestra como margen 100%), costo mayor que el
  precio, venta por peso/volumen e indicios de unidad cruzada;
- de dónde salió el costo: la última compra viva por fecha de NEGOCIO
  (``COALESCE(occurred_at, created_at)``), si coincide con el costo actual, si
  difiere de la última compra CARGADA y si hay empates de fecha con costos
  distintos;
- marcas de envío (``_vektor_costo_base``, ``shipping_percentage``), impuesto y
  régimen fiscal;
- el mismo negocio medido por cada cuenta que existe hoy: catálogo (promedio
  simple de ``margin_pct``), dashboard (``buildMarginBreakdown``: promedio simple
  por categoría), FactsService (``_cogs`` con el costo ACTUAL del producto),
  ponderado sólo sobre ventas con costo (la definición de M·3), FactsService
  (desde M·2b: el costo congelado por venta y la definición única),
  ``deterministic_finance.calcular_margen_bruto`` (ventas − TODOS los gastos) y el
  subscore de margen del health score.

**Ausencia de dato ≠ cero.** Lo que no se puede calcular sale como
``sin medición (<motivo>)``, nunca como 0.

SOLO SELECT, dentro de una transacción READ ONLY. NUNCA imprime la connection URL
ni credenciales: sólo ``Base leída: host=… base=…``. No imprime emails.

Usage (desde ``backend/``):
    DATABASE_URL='postgresql://...' .venv/bin/python scripts/diag_margen.py
    ... scripts/diag_margen.py --tenant-id <uuid>
    ... scripts/diag_margen.py --email dueño@negocio.com --dias 90
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPTS)
sys.path.insert(0, os.path.dirname(_SCRIPTS))

import asyncpg  # noqa: E402
from _db import normalize_dsn  # noqa: E402

from app.domain.business_time import today_ar  # noqa: E402
from app.domain.margen import LineaDeMargen, MedicionDeMargen, medir_margen  # noqa: E402

CIEN = Decimal("100")

#: Una razón precio/costo (o costo/precio) a partir de la cual el costo parece
#: estar en OTRA unidad que el precio (por gramo contra kilo, por bulto contra
#: unidad). Es un indicio para mirar a mano, no una prueba.
UMBRAL_UNIDAD_CRUZADA = Decimal("50")

SIN_IMPUESTO = (
    "sin medición (ninguna compra guarda IVA, tasa ni si es recuperable: "
    "_vektor_impuestos está declarado en domain/purchase_cost.py pero nadie lo escribe)"
)


# ═══════════════════════════════════════════════════════════════════════════
# Datos de entrada (los arma main() desde la base; los tests, a mano)
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Producto:
    nombre: str
    categoria: str | None
    precio: Decimal | None
    costo: Decimal | None
    factor: int = 1
    requiere_completar: bool = False
    #: ``custom_fields._vektor_costo_base`` del producto: con_flete | sin_flete | None.
    costo_base: str | None = None
    #: ``custom_fields.shipping_percentage`` presente.
    envio_pct: bool = False
    #: Procedencia de la última compra viva por fecha de negocio (None = no hay).
    fuente_ultima_compra: str | None = None
    costo_ultima_compra: Decimal | None = None
    #: La última por fecha de negocio NO es la última cargada.
    ultima_difiere_de_cargada: bool = False
    #: En la fecha de la última compra hay más de un costo distinto.
    empate_de_fecha: bool = False


@dataclass(frozen=True)
class LineaVenta:
    """Una ``sales_entries`` viva y REAL, sin cobros de fiado, en la ventana."""

    importe: Decimal
    #: El snapshot congelado de la venta (``sales_entries.cost_ars``, M·2a/M·2c),
    #: como el provider de FactsService. None = sin producto o sin costo.
    costo: Decimal | None
    confirmado: bool = False


@dataclass(frozen=True)
class Salud:
    fecha: date
    score_margin: int | None
    ventas_est: Decimal | None
    inventario_est: Decimal | None
    fijos_est: Decimal | None


@dataclass
class DatosTenant:
    tenant_id: str
    rubro: str | None = None
    regimen: str | None = None
    tiene_perfil: bool = False
    productos: list[Producto] = field(default_factory=list)
    lineas: list[LineaVenta] = field(default_factory=list)
    #: Mismo criterio que el BSL (COGS o categoría INVENTORY/mercaderia).
    inventario_bsl: Decimal = Decimal("0")
    #: De lo anterior, lo que es «Stock inicial: …» (catálogo leído como compra).
    inventario_stock_inicial: Decimal = Decimal("0")
    #: Ventas y gastos de ``calcular_flujo_neto_30d`` (sin filtrar procedencia).
    ventas_30d_det: Decimal = Decimal("0")
    gastos_30d_det: Decimal = Decimal("0")
    ultima_venta: date | None = None
    salud: Salud | None = None


# ═══════════════════════════════════════════════════════════════════════════
# Cálculo (puro)
# ═══════════════════════════════════════════════════════════════════════════


def clasificar_costo(precio: Decimal | None, costo: Decimal | None) -> str:
    """Una sola clase por producto, en orden de gravedad.

    Un costo 0 no es un costo: es el placeholder de un producto incompleto
    (``debe_pisar_costo_de_referencia`` ya lo trata así). Se separa del nulo
    porque la API lo convierte en un margen de 100%.
    """
    if precio is None or precio <= 0:
        return "sin_precio"
    if costo is None:
        return "costo_nulo"
    if costo <= 0:
        return "costo_cero"
    if costo > precio:
        return "costo_mayor_que_precio"
    return "con_costo"


def margen_api(precio: Decimal | None, costo: Decimal | None) -> Decimal | None:
    """Réplica EXACTA de ``ProductResponse.margin_pct`` (schemas/product.py).

    Con costo 0 da 100: así lo ve hoy el frontend.
    """
    if costo is None or precio is None or precio == 0:
        return None
    return (precio - costo) / precio * CIEN


def margen_producto(precio: Decimal | None, costo: Decimal | None) -> Decimal | None:
    """El margen que se puede afirmar: sin costo positivo no hay margen."""
    if precio is None or precio <= 0 or costo is None or costo <= 0:
        return None
    return (precio - costo) / precio * CIEN


def promedio_simple(valores: Iterable[Decimal]) -> Decimal | None:
    lista = list(valores)
    if not lista:
        return None
    return sum(lista, Decimal("0")) / len(lista)


def margen_ponderado(
    pares: Iterable[tuple[Decimal, Decimal | None]],
) -> tuple[Decimal | None, Decimal | None]:
    """``(margen, cobertura)`` = ``100 × Σ(ingreso − costo) / Σ ingreso`` sobre lo
    que tiene costo positivo, y la fracción del ingreso que entra en la cuenta.

    Es la definición de M·3. Un costo nulo o 0 queda AFUERA (no suma costo 0).
    """
    total = Decimal("0")
    ingreso = Decimal("0")
    costo_total = Decimal("0")
    for importe, costo in pares:
        total += importe
        if costo is not None and costo > 0:
            ingreso += importe
            costo_total += costo
    cobertura = ingreso / total if total > 0 else None
    if ingreso <= 0:
        return None, cobertura
    return (ingreso - costo_total) / ingreso * CIEN, cobertura


def margen_dashboard(productos: Sequence[Producto]) -> list[tuple[str, Decimal]]:
    """Réplica de ``buildMarginBreakdown`` (frontend/.../dashboardData.ts).

    Promedio SIMPLE de ``margin_pct`` por categoría, ordenado de mayor a menor. Un
    producto de $10 pesa lo mismo que uno de $10.000, y un costo 0 entra al 100%.
    """
    grupos: dict[str, list[Decimal]] = defaultdict(list)
    for p in productos:
        m = margen_api(p.precio, p.costo)
        if m is None:
            continue
        grupos[(p.categoria or "").strip() or "Sin categoría"].append(m)
    filas = [(cat, sum(v, Decimal("0")) / len(v)) for cat, v in grupos.items()]
    return sorted(filas, key=lambda f: f[1], reverse=True)


def margen_facts(lineas: Sequence[LineaVenta], regimen: str | None) -> MedicionDeMargen:
    """``FactsService.margen_bruto``: la definición única (``domain/margen.py``, M·2b)."""
    return medir_margen(
        (LineaDeMargen(ln.importe, ln.costo, ln.confirmado) for ln in lineas), regimen
    )


def margen_neto_det(ventas: Decimal, gastos: Decimal) -> Decimal | None:
    """Réplica de ``deterministic_finance.calcular_margen_bruto``: resta TODOS los
    gastos (fijos, mercadería y cualquier otro), así que es un margen neto de caja."""
    if ventas == 0:
        return None
    return (ventas - gastos) / ventas * CIEN


def margen_salud(s: Salud) -> Decimal | None:
    """El margen que usa ``health_engine._score_margin``:
    ``(ventas − inventario − fijos) / ventas`` con los estimados del snapshot."""
    if s.ventas_est is None or s.ventas_est <= 0:
        return None
    return (
        (s.ventas_est - (s.inventario_est or Decimal("0")) - (s.fijos_est or Decimal("0")))
        / s.ventas_est
        * CIEN
    )


def indicio_unidad_cruzada(precio: Decimal | None, costo: Decimal | None) -> bool:
    if precio is None or costo is None or precio <= 0 or costo <= 0:
        return False
    return precio / costo >= UMBRAL_UNIDAD_CRUZADA or costo / precio >= UMBRAL_UNIDAD_CRUZADA


# ═══════════════════════════════════════════════════════════════════════════
# Informe (puro)
# ═══════════════════════════════════════════════════════════════════════════


def pct(valor: Decimal | None, motivo: str) -> str:
    """Un porcentaje, o ``sin medición (<motivo>)``. Nunca 0 por ausencia."""
    if valor is None:
        return f"sin medición ({motivo})"
    return f"{valor.quantize(Decimal('0.1'))}%"


def ars(valor: Decimal) -> str:
    return f"${valor.quantize(Decimal('0.01')):,}"


def explicar_margen_en_cero(d: DatosTenant) -> list[str]:
    """Lo que los datos dicen de un margen que se ve en 0. Sólo afirma lo medido."""
    out: list[str] = []
    s = d.salud
    if s is not None and s.score_margin == 0:
        m = margen_salud(s)
        out.append(
            "- El subscore «Margen» del health score es 0 "
            f"(snapshot {s.fecha.isoformat()}): (ventas − inventario − fijos) / ventas = "
            f"{pct(m, 'ventas estimadas en 0')}, por debajo del piso crítico del rubro."
        )
        if d.inventario_stock_inicial > 0:
            out.append(
                f"  En la ventana, {ars(d.inventario_stock_inicial)} de gastos de mercadería "
                "son «Stock inicial: …»: el catálogo leído como compra cuenta como "
                "inventario del mes."
            )
    if not d.lineas:
        ultima = d.ultima_venta.isoformat() if d.ultima_venta else "nunca"
        out.append(
            f"- No hay ventas REALES en la ventana (última venta: {ultima}): Facts no "
            "tiene margen de ventas que mostrar."
        )
    clases = Counter(clasificar_costo(p.precio, p.costo) for p in d.productos)
    if d.productos and clases["con_costo"] + clases["costo_mayor_que_precio"] == 0:
        out.append(
            "- Ningún producto activo tiene costo positivo: el margen de catálogo no se "
            "puede calcular."
        )
    if clases["costo_mayor_que_precio"]:
        out.append(
            f"- {clases['costo_mayor_que_precio']} productos con costo mayor que el precio "
            "(margen negativo en catálogo)."
        )
    if not out:
        out.append("- Los datos de este tenant no muestran un margen en 0.")
    return out


def informe_tenant(d: DatosTenant, desde: date, hasta: date, ejemplos: int = 3) -> list[str]:
    out = [f"\n{'=' * 78}\n  tenant {d.tenant_id}\n{'=' * 78}"]

    def linea(etiqueta: str, valor: Any) -> None:
        out.append(f"  {etiqueta:<58}: {valor}")

    if d.tiene_perfil:
        linea("rubro", d.rubro or "sin medición (perfil sin rubro)")
        linea(
            "régimen fiscal (business_profiles.fiscal_condition)",
            d.regimen or "sin medición (no configurado)",
        )
    else:
        linea("rubro / régimen", "sin medición (el tenant no tiene business_profile)")

    # ── Catálogo ──────────────────────────────────────────────────────────
    out.append("\n  Catálogo (productos activos)")
    prods = d.productos
    clases = Counter(clasificar_costo(p.precio, p.costo) for p in prods)
    linea("productos activos", len(prods))
    linea("  con precio y costo (costo ≤ precio)", clases["con_costo"])
    linea("  costo mayor que el precio", clases["costo_mayor_que_precio"])
    linea("  costo 0 (la API los muestra con margen 100%)", clases["costo_cero"])
    linea("  costo nulo", clases["costo_nulo"])
    linea("  sin precio (≤ 0)", clases["sin_precio"])
    linea("  marcados requires_completion", sum(p.requiere_completar for p in prods))
    linea("  venta por peso/volumen (factor > 1)", sum(p.factor > 1 for p in prods))
    linea(
        f"  indicio de unidad cruzada (razón ≥ {UMBRAL_UNIDAD_CRUZADA})",
        sum(indicio_unidad_cruzada(p.precio, p.costo) for p in prods),
    )
    for clase, titulo in (
        ("costo_mayor_que_precio", "costo > precio"),
        ("costo_cero", "costo 0"),
    ):
        muestra = [p for p in prods if clasificar_costo(p.precio, p.costo) == clase][:ejemplos]
        for p in muestra:
            out.append(f"      ej. {titulo}: {p.nombre[:40]!r} precio={p.precio} costo={p.costo}")

    # ── Fuente del costo ─────────────────────────────────────────────────
    out.append("\n  Fuente del costo (productos con costo > 0)")
    con_costo = [p for p in prods if p.costo is not None and p.costo > 0]
    fuentes = Counter(p.fuente_ultima_compra or "sin compra viva" for p in con_costo)
    for fuente, n in sorted(fuentes.items()):
        linea(f"  última compra: {fuente}", n)
    linea(
        "  costo actual ≠ costo de la última compra (por fecha)",
        sum(
            p.costo_ultima_compra is not None and p.costo_ultima_compra != p.costo
            for p in con_costo
        ),
    )
    linea(
        "  última por fecha de negocio ≠ última cargada",
        sum(p.ultima_difiere_de_cargada for p in con_costo),
    )
    linea("  empate de fecha con costos distintos", sum(p.empate_de_fecha for p in con_costo))

    # ── Envío e impuesto ─────────────────────────────────────────────────
    out.append("\n  Envío e impuesto")
    bases = Counter(p.costo_base or "sin marca (no se sabe)" for p in con_costo)
    for base, n in sorted(bases.items()):
        linea(f"  _vektor_costo_base = {base}", n)
    linea("  con shipping_percentage (informativo)", sum(p.envio_pct for p in prods))
    linea("  impuesto de la compra", SIN_IMPUESTO)

    # ── Margen: el mismo negocio, cada cuenta ────────────────────────────
    out.append(f"\n  Margen — ventana {desde.isoformat()} a {hasta.isoformat()}")
    api = [m for p in prods if (m := margen_api(p.precio, p.costo)) is not None]
    linea(
        "catálogo: promedio simple de margin_pct (API)",
        pct(promedio_simple(api), "ningún producto con costo"),
    )
    linea(
        "catálogo: promedio simple sin costos 0",
        pct(
            promedio_simple(
                m for p in prods if (m := margen_producto(p.precio, p.costo)) is not None
            ),
            "ningún producto con costo positivo",
        ),
    )
    dash = margen_dashboard(prods)
    if not dash:
        linea("dashboard (buildMarginBreakdown)", "sin medición (ningún producto con costo)")
    for cat, m in dash[:8]:
        linea(f"  dashboard «{cat[:30]}»", pct(m, ""))
    if len(dash) > 8:
        linea("  dashboard: categorías no listadas", len(dash) - 8)

    ventas = sum((ln.importe for ln in d.lineas), Decimal("0"))
    linea("ventas REALES en la ventana", f"{len(d.lineas)} líneas, {ars(ventas)}")
    m_f = margen_facts(d.lineas, d.regimen)
    if not d.lineas:
        valor_f = "sin medición (sin ventas en la ventana)"
    else:
        valor_f = (
            f"{pct(m_f.margen_pct, m_f.motivo or 'sin medición')} estado={m_f.estado} "
            f"cobertura confirmada={pct(m_f.cobertura_confirmada * CIEN, '')} "
            f"con estimaciones={pct(m_f.cobertura_total * CIEN, '')}"
        )
    linea("FactsService.margen_bruto (costo congelado por venta)", valor_f)
    m_p, cob = margen_ponderado((ln.importe, ln.costo) for ln in d.lineas)
    linea(
        "ponderado sólo ventas con costo > 0 (definición M·3)",
        f"{pct(m_p, 'ninguna venta con costo positivo')} cobertura="
        f"{pct(cob * CIEN if cob is not None else None, 'sin ventas')}",
    )
    linea(
        "deterministic_finance (ventas − TODOS los gastos, 30 d)",
        pct(margen_neto_det(d.ventas_30d_det, d.gastos_30d_det), "sin ventas en 30 días"),
    )
    if d.salud is None:
        linea("health score: subscore de margen", "sin medición (no hay snapshot)")
    else:
        s = d.salud
        linea(
            f"health score {s.fecha.isoformat()}: score_margin",
            s.score_margin if s.score_margin is not None else "sin medición (columna nula)",
        )
        linea("  (ventas − inventario − fijos) / ventas", pct(margen_salud(s), "ventas est. 0"))
    linea("gastos de mercadería en la ventana (criterio BSL)", ars(d.inventario_bsl))
    linea("  de ellos «Stock inicial: …» (catálogo como compra)", ars(d.inventario_stock_inicial))

    out.append("\n  Lectura")
    out.extend(f"  {ln}" for ln in explicar_margen_en_cero(d))
    return out


def informe(
    base: str,
    desde: date,
    hasta: date,
    tenants: Sequence[DatosTenant],
    ejemplos: int = 3,
) -> list[str]:
    out = [f"Base leída: {base}"]
    if not tenants:
        out.append("\nNingún tenant con productos activos ni ventas.")
    for d in tenants:
        out.extend(informe_tenant(d, desde, hasta, ejemplos))
    out.append("")
    out.append("Un 0 es una cuenta hecha; 'sin medición' es un dato que la base no tiene.")
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Lectura (SELECT-only)
# ═══════════════════════════════════════════════════════════════════════════

_FILTRO = "($1::uuid IS NULL OR {col} = $1::uuid)"

_PRODUCTOS = f"""
SELECT tenant_id::text AS tenant_id, id::text AS product_id, name, category,
       sale_price_ars, unit_cost_ars, base_units_per_sale_unit, requires_completion,
       custom_fields->>'_vektor_costo_base' AS costo_base,
       coalesce(custom_fields ? 'shipping_percentage', false) AS envio_pct
  FROM products
 WHERE is_active AND {_FILTRO.format(col="tenant_id")}
"""

#: Compras vivas con costo; la última por fecha de NEGOCIO (desempate: carga, id),
#: la última CARGADA, y si en la fecha ganadora hay costos distintos.
_COMPRAS = f"""
WITH c AS (
    SELECT product_id, id, unit_cost, source_type, source_event_id, created_at,
           coalesce(occurred_at, created_at) AS f_negocio
      FROM inventory_movements
     WHERE movement_type = 'purchase' AND voided_at IS NULL AND unit_cost IS NOT NULL
       AND {_FILTRO.format(col="tenant_id")}
),
por_fecha AS (
    SELECT DISTINCT ON (product_id) * FROM c
     ORDER BY product_id, f_negocio DESC, created_at DESC, id DESC
),
por_carga AS (
    SELECT DISTINCT ON (product_id) product_id, id FROM c
     ORDER BY product_id, created_at DESC, id DESC
)
SELECT f.product_id::text AS product_id, f.unit_cost,
       coalesce(f.source_type,
                CASE WHEN f.source_event_id LIKE 'manual_purchase:%' THEN 'compra manual'
                     ELSE 'sin source_type' END) AS fuente,
       (f.id <> pc.id) AS difiere_de_cargada,
       (SELECT count(DISTINCT c2.unit_cost) FROM c c2
         WHERE c2.product_id = f.product_id AND c2.f_negocio = f.f_negocio) > 1 AS empate
  FROM por_fecha f JOIN por_carga pc USING (product_id)
"""

#: Réplica del provider de FactsService: vivas, REAL, sin cobros de fiado.
_VENTAS = f"""
SELECT s.tenant_id::text AS tenant_id, s.amount, s.cost_ars, s.cost_confirmed
  FROM sales_entries s
 WHERE s.voided_at IS NULL AND s.provenance = 'REAL'
   AND lower(s.payment_method) <> 'inflow'
   AND date(s.transaction_date) BETWEEN $2 AND $3
   AND {_FILTRO.format(col="s.tenant_id")}
"""

_GASTOS = f"""
SELECT tenant_id::text AS tenant_id,
       coalesce(sum(amount) FILTER (
           WHERE expense_type = 'COGS' OR category IN ('INVENTORY', 'mercaderia')), 0)
           AS inventario_bsl,
       coalesce(sum(amount) FILTER (
           WHERE (expense_type = 'COGS' OR category IN ('INVENTORY', 'mercaderia'))
             AND description LIKE 'Stock inicial:%'), 0) AS inventario_stock_inicial
  FROM expense_entries
 WHERE voided_at IS NULL AND date(transaction_date) BETWEEN $2 AND $3
   AND {_FILTRO.format(col="tenant_id")}
 GROUP BY tenant_id
"""

#: ``calcular_flujo_neto_30d``: todo lo vivo desde hoy − 30, sin otro filtro.
_DET = f"""
SELECT t.tenant_id::text AS tenant_id, sum(t.v) AS ventas, sum(t.g) AS gastos FROM (
    SELECT tenant_id, amount AS v, 0 AS g FROM sales_entries
     WHERE voided_at IS NULL AND transaction_date >= $2
    UNION ALL
    SELECT tenant_id, 0, amount FROM expense_entries
     WHERE voided_at IS NULL AND transaction_date >= $2
) t
 WHERE {_FILTRO.format(col="t.tenant_id")}
 GROUP BY t.tenant_id
"""

_ULTIMA_VENTA = f"""
SELECT tenant_id::text AS tenant_id, max(transaction_date)::date AS ultima
  FROM sales_entries
 WHERE voided_at IS NULL AND provenance = 'REAL' AND {_FILTRO.format(col="tenant_id")}
 GROUP BY tenant_id
"""

_PERFIL = f"""
SELECT tenant_id::text AS tenant_id, vertical_code, fiscal_condition
  FROM business_profiles WHERE {_FILTRO.format(col="tenant_id")}
"""

_SALUD = f"""
SELECT DISTINCT ON (tenant_id) tenant_id::text AS tenant_id, snapshot_date, score_margin,
       score_inputs_json->>'monthly_sales_est' AS ventas,
       score_inputs_json->>'monthly_inventory_cost_est' AS inventario,
       score_inputs_json->>'monthly_fixed_expenses_est' AS fijos
  FROM health_score_snapshots
 WHERE {_FILTRO.format(col="tenant_id")}
 ORDER BY tenant_id, snapshot_date DESC
"""


def _dec(v: Any) -> Decimal | None:
    return None if v is None else Decimal(str(v))


async def leer(conn: Any, tenant: str | None, desde: date, hasta: date) -> list[DatosTenant]:
    datos: dict[str, DatosTenant] = {}

    def de(tid: str) -> DatosTenant:
        return datos.setdefault(tid, DatosTenant(tenant_id=tid))

    compras = {r["product_id"]: r for r in await conn.fetch(_COMPRAS, tenant)}
    for r in await conn.fetch(_PRODUCTOS, tenant):
        c = compras.get(r["product_id"])
        de(r["tenant_id"]).productos.append(
            Producto(
                nombre=r["name"],
                categoria=r["category"],
                precio=_dec(r["sale_price_ars"]),
                costo=_dec(r["unit_cost_ars"]),
                factor=r["base_units_per_sale_unit"] or 1,
                requiere_completar=bool(r["requires_completion"]),
                costo_base=r["costo_base"],
                envio_pct=bool(r["envio_pct"]),
                fuente_ultima_compra=c["fuente"] if c else None,
                costo_ultima_compra=_dec(c["unit_cost"]) if c else None,
                ultima_difiere_de_cargada=bool(c["difiere_de_cargada"]) if c else False,
                empate_de_fecha=bool(c["empate"]) if c else False,
            )
        )
    for r in await conn.fetch(_VENTAS, tenant, desde, hasta):
        de(r["tenant_id"]).lineas.append(
            LineaVenta(
                importe=Decimal(str(r["amount"])),
                costo=_dec(r["cost_ars"]),
                confirmado=r["cost_confirmed"] is True,
            )
        )
    for r in await conn.fetch(_GASTOS, tenant, desde, hasta):
        d = de(r["tenant_id"])
        d.inventario_bsl = Decimal(str(r["inventario_bsl"]))
        d.inventario_stock_inicial = Decimal(str(r["inventario_stock_inicial"]))
    for r in await conn.fetch(_DET, tenant, hasta - timedelta(days=30)):
        d = de(r["tenant_id"])
        d.ventas_30d_det = Decimal(str(r["ventas"]))
        d.gastos_30d_det = Decimal(str(r["gastos"]))
    for r in await conn.fetch(_ULTIMA_VENTA, tenant):
        de(r["tenant_id"]).ultima_venta = r["ultima"]
    perfiles = {r["tenant_id"]: r for r in await conn.fetch(_PERFIL, tenant)}
    salud = {r["tenant_id"]: r for r in await conn.fetch(_SALUD, tenant)}
    if tenant:
        de(tenant)
    for tid, d in datos.items():
        if (p := perfiles.get(tid)) is not None:
            d.tiene_perfil = True
            d.rubro = p["vertical_code"]
            d.regimen = p["fiscal_condition"]
        if (s := salud.get(tid)) is not None:
            d.salud = Salud(
                fecha=s["snapshot_date"].date(),
                score_margin=s["score_margin"],
                ventas_est=_dec(s["ventas"]),
                inventario_est=_dec(s["inventario"]),
                fijos_est=_dec(s["fijos"]),
            )
    return [datos[k] for k in sorted(datos)]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", help="un solo tenant (sin esto: todos)")
    parser.add_argument("--email", help="resuelve el tenant por el email de un usuario")
    parser.add_argument("--dias", type=int, default=30, help="ventana de ventas (default 30)")
    parser.add_argument("--ejemplos", type=int, default=3, help="ejemplos por anomalía")
    args = parser.parse_args()

    raw = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_SYNC")
    if not raw:
        print("ERROR: exportá DATABASE_URL antes de correr.")
        return 2
    dsn, sslarg = normalize_dsn(raw)
    hasta = today_ar()
    desde = hasta - timedelta(days=max(1, args.dias) - 1)
    conn = await asyncpg.connect(dsn, ssl=sslarg)
    try:
        async with conn.transaction(readonly=True):
            # Host y nombre de base, nunca usuario ni contraseña.
            nombre = await conn.fetchval("SELECT current_database()")
            base = f"host={urlparse(dsn).hostname} base={nombre}"
            tenant = args.tenant_id
            if args.email and not tenant:
                tenant = await conn.fetchval(
                    "SELECT tenant_id::text FROM users WHERE lower(email) = lower($1) LIMIT 1",
                    args.email,
                )
                if tenant is None:
                    print(f"Base leída: {base}\nNingún usuario con ese email.")
                    return 1
            tenants = await leer(conn, tenant, desde, hasta)
    finally:
        await conn.close()

    print("\n".join(informe(base, desde, hasta, tenants, args.ejemplos)))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
