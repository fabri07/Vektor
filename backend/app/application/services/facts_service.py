"""FactsService — Única fuente de verdad para las métricas de negocio de Véktor.

REGLA DE ORO (invariante del repo, ahora cumplida en TODAS las superficies):
    Si el dashboard muestra un número, el chat debe obtener ESE MISMO número
    desde acá. Toda aritmética financiera vive en este servicio. Nadie más
    computa métricas: ni el frontend, ni business_state_service, ni los
    endpoints de insights. Todos delegan acá.

DECISIONES CANÓNICAS RATIFICADAS (Fabrizio):
    - margen_bruto = (ventas − COGS) / ventas
    - margen_neto  = (ventas − TODOS los gastos) / ventas   ← la heurística corre sobre neto
    - caja_liquida excluye fiado (fiado = deuda a cobrar, no efectivo)
    - caja_liquida excluye tarjeta de crédito (se acredita ~30 días → DELAYED_METHODS)
    - ingresos_totales incluye fiado (es venta reconocida)
    - cobros de fiado (payment_method="inflow" en sales_entries): NO son ventas —
      se excluyen de ventas/ticket/margen y se SUMAN a caja_liquida
    - ventas del período: ventana [hoy-N+1, hoy], borde superior DURO (nunca futuro),
      SIN blend de onboarding. Con pocas ventas → confidence bajo o EMPTY, nunca
      estimación silenciosa.
    - frontera COGS/OPEX: la columna canónica `expense_type` de expense_entries
      (no se matchean nombres de categoría — eso sería re-crear la divergencia).

El seam con el ORM vive en facts_provider.py (FactsDataProvider). Este módulo
NO conoce SQLAlchemy: recibe DataFrames normalizados.

Motor: pandas. El LLM NUNCA entra acá. Esto es Python determinista puro.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Literal, Protocol

import pandas as pd
from pydantic import BaseModel

from app.domain.business_time import AR_TZ
from app.domain.transaction import PaymentMethod

# ─────────────────────────────────────────────────────────────────────────────
# 1. CONFIG CANÓNICA — las reglas de negocio como constantes, no como magia dispersa
# ─────────────────────────────────────────────────────────────────────────────
# La zona horaria del día de negocio viene de app/domain/business_time.py (AR_TZ),
# la MISMA que usan los paths de escritura y el cierre de caja. La divergencia
# "cómo agrupás por día" muere ahí, no acá.

# Métodos de pago: referencian el enum canónico (app/domain/transaction.py) para
# que un cambio del enum no deje estos sets stale. Los alias legacy quedan como
# defensa (normalize_payment_method los convierte antes de escribir).
# fiado / account = plata que te deben → NO es caja (ratificado).
FIADO_METHODS = frozenset({PaymentMethod.ACCOUNT.value, "fiado", "cuenta_corriente"})

# RATIFICADO: tarjeta de crédito settlea ~30 días → NO es caja inmediata.
# La venta cuenta como ingreso reconocido, pero la plata todavía no está.
DELAYED_METHODS = frozenset({PaymentMethod.CREDIT_CARD.value, "tarjeta_credito"})

# Cobros de fiado: filas en sales_entries con payment_method="inflow" (fuera del
# enum canónico). NO son ventas (la venta fiada original ya se contó) — son plata
# que ENTRA a caja. Se excluyen de ventas/ticket/margen y se suman a caja_liquida.
INFLOW_METHOD = "inflow"

# Muestra mínima para reportar una métrica como confiable sin advertencia.
MIN_CONFIDENCE_SAMPLE = 50  # < 50 ventas → REAL con confidence reducido, NUNCA blend

# Redondeo (mata la divergencia "cómo redondeás"):
ARS_DECIMALS = 2   # pesos con 2 decimales
PCT_DECIMALS = 1   # porcentajes con 1 decimal


@dataclass(frozen=True)
class SeverityThresholds:
    """Umbrales por vertical.

    Los defaults replican el benchmark canónico de kiosco_almacen.json
    (warning_below=0.18, critical_below=0.10) para que el severity del fact y
    el health score NUNCA se contradigan. Para otros verticales, construir con
    `facts_provider.thresholds_for_vertical(vertical_code)` — misma fuente JSON.
    """

    # Caída de ventas vs período anterior (variación %):
    ventas_drop_warning: float = -20.0
    ventas_drop_critical: float = -35.0
    # Piso de margen NETO (= benchmark kiosco_almacen.json, en %):
    margen_neto_floor: float = 18.0
    margen_neto_critical: float = 10.0
    # Días de stock sin rotación → sobrestock:
    sobrestock_dias_warning: int = 90


# ─────────────────────────────────────────────────────────────────────────────
# 2. MODELO DE HECHO — estructurado, no texto bonito
# ─────────────────────────────────────────────────────────────────────────────

class Provenance(str, Enum):
    REAL = "REAL"    # datos reales del tenant
    DEMO = "DEMO"    # datos de demostración
    EMPTY = "EMPTY"  # no hay datos suficientes para computar


class BusinessFact(BaseModel):
    """Un hecho computado. El dashboard lo pinta; el chat lo explica y aconseja sobre él.

    NUNCA lleva prosa: solo el número y su metadata. La prosa la pone Sonnet, después.
    """

    fact_id: str                      # clave canónica: "margen_neto_30d"
    domain: str                       # ventas | rentabilidad | caja | inventario | gastos
    metric: str                       # "margen_neto"
    value: float | int | None
    unit: str | None               # "ARS" | "%" | "unidades" | "ratio" | "dias"
    period: str | None = None      # "últimos_30_días"; None = snapshot actual
    comparison_value: float | int | None = None   # mismo metric, período anterior
    variation_pct: float | None = None
    severity: Literal["info", "warning", "critical"] | None = None
    provenance: Provenance = Provenance.REAL
    confidence: float = 1.0
    sample_size: int | None = None  # nº de registros base (drivea EMPTY/low-conf)
    source: str = ""                   # tablas de origen, p/ auditoría
    explanation_key: str | None = None  # clave hacia AgentHelper (docs versionadas)


# ─────────────────────────────────────────────────────────────────────────────
# 3. PERÍODO — una sola semántica de ventana, con borde superior DURO
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Period:
    start: date   # inclusive
    end: date     # inclusive — GARANTIZADO nunca > hoy
    label: str

    @classmethod
    def last_n_days(cls, n: int, *, today: date | None = None) -> Period:
        today = today or datetime.now(AR_TZ).date()
        return cls(start=today - timedelta(days=n - 1), end=today, label=f"últimos_{n}_días")

    def previous(self) -> Period:
        """Ventana inmediatamente anterior, mismo largo. Para variación %."""
        span = (self.end - self.start).days
        prev_end = self.start - timedelta(days=1)
        return Period(start=prev_end - timedelta(days=span), end=prev_end,
                      label=f"{self.label}_anterior")

    def contains(self, s: pd.Series) -> pd.Series:
        """Máscara [start, end] inclusive sobre una serie de fechas (date-normalizada)."""
        d = pd.to_datetime(s).dt.date
        return (d >= self.start) & (d <= self.end)


# ─────────────────────────────────────────────────────────────────────────────
# 4. SEAM DE DATOS — el único punto que toca tu ORM/repos reales
# ─────────────────────────────────────────────────────────────────────────────

class FactsDataProvider(Protocol):
    """Implementado contra los repos reales en facts_provider.py.

    FactsService NO conoce el ORM: recibe DataFrames normalizados con el schema
    de abajo. El provider es el ÚNICO lugar donde se mapean los nombres de campo
    reales a los canónicos.

    Contrato de columnas (respetalo o todo lo de arriba se rompe):

    sales(tenant_id, start, end) -> DataFrame:
        sale_date:       date/datetime
        amount_ars:      float     # total de la venta
        cost_ars:        float|NaN # COGS de esa venta (unit_cost_ars × quantity) si existe
        payment_method:  str       # cash|debit_card|credit_card|transfer|qr|account|other
                                   #   (+ "inflow" = cobro de fiado, NO es venta)
        provenance:      str       # REAL|DEMO

    expenses(tenant_id, start, end) -> DataFrame:
        expense_date:    date/datetime
        amount_ars:      float
        category:        str       # códigos canónicos (RENT..INVENTORY..OTHER)
        expense_type:    str       # COGS|OPEX — frontera canónica bruto/neto
        provenance:      str

    products(tenant_id) -> DataFrame:
        product_id:      str
        stock_units:     float
        unit_cost_ars:   float|NaN
        last_sold_date:  date/datetime|NaT   # para rotación/sobrestock
        first_seen_date: date/datetime|NaT   # alta del producto (acquired_at/created_at);
                                             #   ancla el sobrestock de nunca-vendidos
        provenance:      str
    """

    def sales(self, tenant_id: str, start: date, end: date) -> pd.DataFrame: ...
    def expenses(self, tenant_id: str, start: date, end: date) -> pd.DataFrame: ...
    def products(self, tenant_id: str) -> pd.DataFrame: ...


# ─────────────────────────────────────────────────────────────────────────────
# 5. HELPERS DE DINERO — redondeo explícito, null-safe
# ─────────────────────────────────────────────────────────────────────────────

def _ars(x: float | None) -> float | None:
    return None if x is None or pd.isna(x) else round(float(x), ARS_DECIMALS)


def _pct(x: float | None) -> float | None:
    return None if x is None or pd.isna(x) else round(float(x), PCT_DECIMALS)


def _variation(current: float | None, previous: float | None) -> float | None:
    """Variación % contra período anterior. None si no hay base para comparar."""
    if current is None or previous in (None, 0) or pd.isna(previous):
        return None
    assert previous is not None
    return _pct((current - previous) / abs(previous) * 100.0)


# ─────────────────────────────────────────────────────────────────────────────
# 6. EL SERVICIO
# ─────────────────────────────────────────────────────────────────────────────

class FactsService:
    def __init__(self, provider: FactsDataProvider,
                 thresholds: SeverityThresholds | None = None) -> None:
        self.provider = provider
        self.t = thresholds or SeverityThresholds()

    # ---- filtrado de provenance (respeta REAL/DEMO/EMPTY) ------------------
    @staticmethod
    def _filter(df: pd.DataFrame, include_demo: bool) -> pd.DataFrame:
        if df.empty or "provenance" not in df.columns:
            return df
        allowed = {"REAL", "DEMO"} if include_demo else {"REAL"}
        return df[df["provenance"].isin(allowed)]

    # ---- carga base (una sola vez por request si querés cachear) -----------
    def _sales(self, tenant_id: str, p: Period, include_demo: bool) -> pd.DataFrame:
        return self._filter(self.provider.sales(tenant_id, p.start, p.end), include_demo)

    def _expenses(self, tenant_id: str, p: Period, include_demo: bool) -> pd.DataFrame:
        return self._filter(self.provider.expenses(tenant_id, p.start, p.end), include_demo)

    # ---- split ventas reales vs cobros de fiado ("inflow") ------------------
    @staticmethod
    def _split_inflows(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
        """(ventas_reales, inflows). Los inflows NO son ventas: son cobros de fiado."""
        if df.empty or "payment_method" not in df.columns:
            return df, df.iloc[0:0]
        is_inflow = df["payment_method"].str.lower() == INFLOW_METHOD
        return df[~is_inflow], df[is_inflow]

    def _ventas_reales(self, tenant_id: str, p: Period, include_demo: bool) -> pd.DataFrame:
        ventas, _ = self._split_inflows(self._sales(tenant_id, p, include_demo))
        return ventas

    # ═══════════════════════════════════════════════════════════════════════
    # MÉTRICA 1 — VENTAS DEL PERÍODO (+ variación vs anterior)
    # ═══════════════════════════════════════════════════════════════════════
    def ventas_periodo(self, tenant_id: str, p: Period, *,
                       include_demo: bool = False) -> BusinessFact:
        cur = self._ventas_reales(tenant_id, p, include_demo)
        n = len(cur)

        if n == 0:
            return BusinessFact(
                fact_id=f"ventas_{p.label}", domain="ventas", metric="ventas_totales",
                value=None, unit="ARS", period=p.label, provenance=Provenance.EMPTY,
                confidence=0.0, sample_size=0, source="sales",
                explanation_key="ventas_sin_datos",
            )

        total = float(cur["amount_ars"].sum())
        prev = self._ventas_reales(tenant_id, p.previous(), include_demo)
        prev_total = float(prev["amount_ars"].sum()) if len(prev) else None
        var = _variation(total, prev_total)

        # Sin blend de onboarding: si hay pocas ventas, bajamos confidence y avisamos,
        # NUNCA reemplazamos por una estimación silenciosa.
        confidence = (1.0 if n >= MIN_CONFIDENCE_SAMPLE
                      else round(0.5 + 0.5 * n / MIN_CONFIDENCE_SAMPLE, 2))

        severity: Literal["info", "warning", "critical"] | None = None
        if var is not None:
            if var <= self.t.ventas_drop_critical:
                severity = "critical"
            elif var <= self.t.ventas_drop_warning:
                severity = "warning"

        return BusinessFact(
            fact_id=f"ventas_{p.label}", domain="ventas", metric="ventas_totales",
            value=_ars(total), unit="ARS", period=p.label,
            comparison_value=_ars(prev_total), variation_pct=var, severity=severity,
            provenance=Provenance.REAL, confidence=confidence, sample_size=n,
            source="sales", explanation_key="ventas_periodo",
        )

    def ticket_promedio(self, tenant_id: str, p: Period, *,
                        include_demo: bool = False) -> BusinessFact:
        cur = self._ventas_reales(tenant_id, p, include_demo)
        n = len(cur)
        if n == 0:
            return BusinessFact(fact_id=f"ticket_prom_{p.label}", domain="ventas",
                                metric="ticket_promedio", value=None, unit="ARS",
                                period=p.label, provenance=Provenance.EMPTY,
                                confidence=0.0, sample_size=0, source="sales")
        cur_avg = float(cur["amount_ars"].mean())
        prev = self._ventas_reales(tenant_id, p.previous(), include_demo)
        prev_avg = float(prev["amount_ars"].mean()) if len(prev) else None
        return BusinessFact(
            fact_id=f"ticket_prom_{p.label}", domain="ventas", metric="ticket_promedio",
            value=_ars(cur_avg), unit="ARS", period=p.label,
            comparison_value=_ars(prev_avg), variation_pct=_variation(cur_avg, prev_avg),
            sample_size=n, source="sales", explanation_key="ticket_promedio",
        )

    # ═══════════════════════════════════════════════════════════════════════
    # MÉTRICA 2 — MARGEN (bruto Y neto, definiciones separadas y explícitas)
    # ═══════════════════════════════════════════════════════════════════════
    def _cogs(self, tenant_id: str, p: Period, sales: pd.DataFrame,
              include_demo: bool) -> tuple[float, str, float]:
        """Devuelve (cogs_ars, fuente, confidence).

        Preferencia: COGS por line-items (cost_ars por venta) = matchea lo VENDIDO.
        Fallback: gastos con expense_type=COGS del período = aproximación por caja
        (lo COMPRADO). La diferencia importa cuando hay swing de inventario.
        Decisión abierta: default a line-items si están; si no, proxy con
        confidence menor.

        HONESTIDAD DE COBERTURA (regla "agregados no engañosos"): la confidence
        de line-items es la fracción del MONTO vendido que tiene costo conocido.
        Las ventas sin producto/costo (NaN) suman COGS $0, así que con cobertura
        parcial el margen se sobreestima — la confidence lo declara en vez de
        esconderlo. NOTA: cost_ars usa el costo ACTUAL del producto (no el
        histórico al momento de la venta); la fuente histórica correcta es
        InventoryMovement.unit_cost — mejora pendiente, decisión del dueño.
        """
        if "cost_ars" in sales.columns and bool(sales["cost_ars"].notna().any()):
            covered = sales["cost_ars"].notna()
            total_amount = float(sales["amount_ars"].sum())
            covered_amount = float(sales.loc[covered, "amount_ars"].sum())
            coverage = covered_amount / total_amount if total_amount > 0 else 0.0
            conf = round(min(1.0, coverage), 2)
            fuente = f"line_items[cobertura={coverage:.0%}]"
            return float(sales["cost_ars"].sum(skipna=True)), fuente, conf
        exp = self._expenses(tenant_id, p, include_demo)
        if not exp.empty and "expense_type" in exp.columns:
            # Frontera canónica COGS/OPEX: la columna expense_type de expense_entries
            # (nunca matchear nombres de categoría — sería re-crear la divergencia).
            mask = exp["expense_type"].str.upper() == "COGS"
            if bool(mask.any()):
                return float(exp.loc[mask, "amount_ars"].sum()), "cogs_expense", 0.7
        # Sin costo por venta y sin gastos COGS: el margen bruto que salga de acá
        # es "ventas al 100%" — confidence mínima para que nadie lo tome en serio.
        return 0.0, "sin_cogs", 0.4

    def margen_bruto(self, tenant_id: str, p: Period, *,
                     include_demo: bool = False) -> BusinessFact:
        sales = self._ventas_reales(tenant_id, p, include_demo)
        ventas = float(sales["amount_ars"].sum()) if len(sales) else 0.0
        if ventas <= 0:
            return BusinessFact(fact_id=f"margen_bruto_{p.label}", domain="rentabilidad",
                                metric="margen_bruto", value=None, unit="%", period=p.label,
                                provenance=Provenance.EMPTY, confidence=0.0,
                                sample_size=len(sales), source="sales,expenses")
        cogs, fuente, conf = self._cogs(tenant_id, p, sales, include_demo)
        margen = (ventas - cogs) / ventas * 100.0
        return BusinessFact(
            fact_id=f"margen_bruto_{p.label}", domain="rentabilidad", metric="margen_bruto",
            value=_pct(margen), unit="%", period=p.label, confidence=conf,
            sample_size=len(sales), source=f"sales,expenses[cogs={fuente}]",
            explanation_key="margen_bruto",
        )

    def margen_neto(self, tenant_id: str, p: Period, *, include_demo: bool = False,
                    benchmark_floor: float | None = None) -> BusinessFact:
        """LA métrica del health score. ventas − TODOS los gastos, sobre ventas."""
        sales = self._ventas_reales(tenant_id, p, include_demo)
        ventas = float(sales["amount_ars"].sum()) if len(sales) else 0.0
        if ventas <= 0:
            return BusinessFact(fact_id=f"margen_neto_{p.label}", domain="rentabilidad",
                                metric="margen_neto", value=None, unit="%", period=p.label,
                                provenance=Provenance.EMPTY, confidence=0.0,
                                sample_size=len(sales), source="sales,expenses")
        exp = self._expenses(tenant_id, p, include_demo)
        gastos_totales = float(exp["amount_ars"].sum()) if len(exp) else 0.0
        margen = (ventas - gastos_totales) / ventas * 100.0

        # Cero gastos cargados casi siempre significa "todavía no los cargó", no
        # "no tiene gastos": el 100% resultante sale con confidence mínima
        # (espejo del path sin_cogs de margen_bruto), nunca como dato pleno.
        sin_gastos = exp.empty
        confidence = 0.4 if sin_gastos else 1.0

        floor = benchmark_floor if benchmark_floor is not None else self.t.margen_neto_floor
        severity: Literal["info", "warning", "critical"] = (
            "critical" if margen < self.t.margen_neto_critical
            else "warning" if margen < floor
            else "info"
        )

        return BusinessFact(
            fact_id=f"margen_neto_{p.label}", domain="rentabilidad", metric="margen_neto",
            value=_pct(margen), unit="%", period=p.label, severity=severity,
            confidence=confidence, sample_size=len(sales),
            source="sales,expenses[sin_gastos]" if sin_gastos else "sales,expenses",
            explanation_key="margen_neto",
        )

    # ═══════════════════════════════════════════════════════════════════════
    # MÉTRICA 3 — CAJA (líquida, sin fiado ni tarjeta) + FIADO como deuda separada
    # ═══════════════════════════════════════════════════════════════════════
    def _split_liquidez(self, sales_full: pd.DataFrame) -> tuple[float, float, float]:
        """Devuelve (ventas_totales, caja_liquida, fiado_pendiente).

        caja_liquida = ventas − fiado − delayed + cobros de fiado (inflows).
        Invariante: caja_liquida − inflows + fiado + delayed == ventas_totales.
        """
        if sales_full.empty:
            return 0.0, 0.0, 0.0
        ventas, inflows = self._split_inflows(sales_full)
        pm = ventas["payment_method"].str.lower()
        total = float(ventas["amount_ars"].sum())
        fiado = float(ventas.loc[pm.isin(FIADO_METHODS), "amount_ars"].sum())
        delayed = float(ventas.loc[pm.isin(DELAYED_METHODS), "amount_ars"].sum())
        cobros = float(inflows["amount_ars"].sum()) if len(inflows) else 0.0
        # ratificado: fiado NO es caja; tarjeta tampoco (delay ~30d); cobros SÍ entran
        liquida = total - fiado - delayed + cobros
        return total, liquida, fiado

    def caja_liquida(self, tenant_id: str, p: Period, *,
                     include_demo: bool = False) -> BusinessFact:
        sales = self._sales(tenant_id, p, include_demo)
        total, liquida, _ = self._split_liquidez(sales)
        return BusinessFact(
            fact_id=f"caja_liquida_{p.label}", domain="caja", metric="caja_liquida",
            # EMPTY → None, nunca "$0 en caja" cuando lo que falta son los datos
            value=None if sales.empty else _ars(liquida), unit="ARS", period=p.label,
            provenance=Provenance.EMPTY if sales.empty else Provenance.REAL,
            confidence=0.0 if sales.empty else 1.0, sample_size=len(sales),
            source="sales[excl. fiado/tarjeta, incl. cobros]",
            explanation_key="caja_liquida",
        )

    def fiado_pendiente(self, tenant_id: str, p: Period, *,
                        include_demo: bool = False) -> BusinessFact:
        sales = self._sales(tenant_id, p, include_demo)
        _, _, fiado = self._split_liquidez(sales)
        return BusinessFact(
            fact_id=f"fiado_pendiente_{p.label}", domain="caja", metric="fiado_pendiente",
            # EMPTY → None: "$0 de fiado" solo cuando HAY ventas y ninguna es fiada
            value=None if sales.empty else _ars(fiado), unit="ARS", period=p.label,
            severity="warning" if fiado > 0 else "info",
            provenance=Provenance.EMPTY if sales.empty else Provenance.REAL,
            confidence=0.0 if sales.empty else 1.0,
            sample_size=len(sales), source="sales[fiado]", explanation_key="fiado_deuda",
        )

    # ═══════════════════════════════════════════════════════════════════════
    # MÉTRICA 4 — GASTOS POR CATEGORÍA (ventana parametrizada, una sola función)
    # ═══════════════════════════════════════════════════════════════════════
    def gastos_por_categoria(self, tenant_id: str, p: Period, *,
                             include_demo: bool = False) -> list[BusinessFact]:
        exp = self._expenses(tenant_id, p, include_demo)
        if exp.empty:
            return [BusinessFact(fact_id=f"gastos_cat_{p.label}", domain="gastos",
                                 metric="gastos_por_categoria", value=None, unit="ARS",
                                 period=p.label, provenance=Provenance.EMPTY,
                                 source="expenses")]
        agg = (exp.groupby(exp["category"].str.lower())["amount_ars"]
               .sum().sort_values(ascending=False))
        return [
            BusinessFact(
                fact_id=f"gastos_{cat}_{p.label}", domain="gastos",
                metric="gastos_categoria", value=_ars(float(v)), unit="ARS", period=p.label,
                source="expenses", explanation_key="gastos_categoria",
                severity=None,
            ) for cat, v in agg.items()
        ]

    # ═══════════════════════════════════════════════════════════════════════
    # MÉTRICA 5 — VALOR DE STOCK + detección de sobrestock (rotación)
    # ═══════════════════════════════════════════════════════════════════════
    def valor_stock(self, tenant_id: str, *, include_demo: bool = False) -> BusinessFact:
        prod = self._filter(self.provider.products(tenant_id), include_demo)
        con_costo = int(prod["unit_cost_ars"].notna().sum()) if not prod.empty else 0
        if prod.empty or con_costo == 0:
            # Sin productos, o ninguno con costo cargado: no hay valuación posible.
            # EMPTY con None, nunca un "$0 de stock" falso con provenance REAL.
            return BusinessFact(fact_id="valor_stock", domain="inventario",
                                metric="valor_stock", value=None, unit="ARS",
                                provenance=Provenance.EMPTY, confidence=0.0,
                                sample_size=len(prod), source="products",
                                explanation_key="valor_stock_sin_costos")
        valor = float((prod["stock_units"] * prod["unit_cost_ars"]).sum())
        # Cobertura de costos: los productos sin unit_cost_ars valen $0 en esta
        # suma — la confidence declara cuántos quedaron afuera de la valuación.
        confidence = round(con_costo / len(prod), 2)
        return BusinessFact(
            fact_id="valor_stock", domain="inventario", metric="valor_stock",
            value=_ars(valor), unit="ARS", period="actual",
            confidence=confidence, sample_size=len(prod),
            source=f"products[costeados={con_costo}/{len(prod)}]",
            explanation_key="valor_stock",
        )

    def sobrestock(self, tenant_id: str, *, today: date | None = None,
                   include_demo: bool = False) -> list[BusinessFact]:
        """Productos sin rotación por más de sobrestock_dias_warning → capital inmovilizado.

        Un producto NUNCA vendido se ancla a su fecha de alta (first_seen_date):
        un catálogo cargado ayer no es capital inmovilizado, es stock nuevo.
        Sin fecha de venta NI de alta no se puede afirmar nada → no se flaggea
        (no-invention).
        """
        prod = self._filter(self.provider.products(tenant_id), include_demo)
        if prod.empty or "last_sold_date" not in prod.columns:
            return []
        today = today or datetime.now(AR_TZ).date()
        ref = pd.to_datetime(prod["last_sold_date"])
        if "first_seen_date" in prod.columns:
            ref = ref.fillna(pd.to_datetime(prod["first_seen_date"]))
        dias = ref.apply(lambda d: (today - d.date()).days if pd.notna(d) else -1)
        frozen = prod[dias > self.t.sobrestock_dias_warning]
        out: list[BusinessFact] = []
        for _, r in frozen.iterrows():
            capital = float(r["stock_units"] * r["unit_cost_ars"])
            out.append(BusinessFact(
                fact_id=f"sobrestock_{r['product_id']}", domain="inventario",
                metric="sobrestock", value=_ars(capital), unit="ARS",
                severity="warning", source="products",
                explanation_key="sobrestock", sample_size=int(r["stock_units"]),
            ))
        return out

    # ═══════════════════════════════════════════════════════════════════════
    # AGREGADORES DE ALTO NIVEL — lo que consumen dashboard, chat-explain y chat-advise
    # ═══════════════════════════════════════════════════════════════════════
    def dashboard_snapshot(self, tenant_id: str, p: Period, *,
                           include_demo: bool = False) -> dict[str, BusinessFact]:
        """Lo que el dashboard pinta. El chat lee lo MISMO. Cero divergencia posible."""
        return {
            "ventas": self.ventas_periodo(tenant_id, p, include_demo=include_demo),
            "ticket_promedio": self.ticket_promedio(tenant_id, p, include_demo=include_demo),
            "margen_bruto": self.margen_bruto(tenant_id, p, include_demo=include_demo),
            "margen_neto": self.margen_neto(tenant_id, p, include_demo=include_demo),
            "caja_liquida": self.caja_liquida(tenant_id, p, include_demo=include_demo),
            "fiado_pendiente": self.fiado_pendiente(tenant_id, p, include_demo=include_demo),
            "valor_stock": self.valor_stock(tenant_id, include_demo=include_demo),
        }

    def get_alert_by_id(self, tenant_id: str, alert_id: str, p: Period, *,
                        include_demo: bool = False) -> BusinessFact | None:
        """Para 'explicame el mensaje rojo': el frontend manda el fact_id del alert activo."""
        snap = self.dashboard_snapshot(tenant_id, p, include_demo=include_demo)
        for f in snap.values():
            if f.fact_id == alert_id and f.severity in ("warning", "critical"):
                return f
        # alerts de sobrestock viven aparte
        for f in self.sobrestock(tenant_id, include_demo=include_demo):
            if f.fact_id == alert_id:
                return f
        return None

    # Vocabulario de dominio del chat (mismo que DOMAIN_TO_AGENT en
    # ceo/team_plan_builder.py: ventas/caja/stock/gastos/proveedores/
    # clientes/marketing) → paquete canónico de abajo. ÚNICO lugar donde se
    # traduce domain→hechos — el chat (consulta_libre, pedir_consejo) llama
    # `collect_for_advice(domain)` directo, sin una segunda tabla de mapeo.
    _CHAT_DOMAIN_ALIASES: dict[str, str] = {
        "caja": "flujo_caja",
        "stock": "inventario",
        "gastos": "rentabilidad",
        # clientes: sin pack propio todavía — fiado_pendiente + ventas es lo
        # más cercano (cuentas por cobrar). Ratificar si se agrega un pack
        # dedicado de clientes.
        "clientes": "flujo_caja",
    }

    def collect_for_advice(self, tenant_id: str, domain: str, p: Period, *,
                           include_demo: bool = False) -> list[BusinessFact]:
        """Los HECHOS que Sonnet recibe para aconsejar (NUNCA filas crudas).

        Sonnet razona sobre esto y cita cada dato. Un dominio → su paquete de hechos.
        """
        base = self.dashboard_snapshot(tenant_id, p, include_demo=include_demo)
        packs: dict[str, list[BusinessFact]] = {
            "ventas":       [base["ventas"], base["ticket_promedio"], base["margen_neto"]],
            "precios":      [base["margen_bruto"], base["margen_neto"],
                             base["ticket_promedio"]],
            "inventario":   [base["valor_stock"],
                             *self.sobrestock(tenant_id, include_demo=include_demo)],
            "flujo_caja":   [base["caja_liquida"], base["fiado_pendiente"], base["ventas"]],
            "rentabilidad": [base["margen_bruto"], base["margen_neto"], base["ventas"]],
            "proveedores":  [base["margen_bruto"], base["valor_stock"]],
            # marketing/fidelización: datos débiles en PyME informal → el prompt advisory
            # DEBE avisar que se apoya en best-practice, no en datos del tenant.
            "marketing":    [base["ventas"], base["ticket_promedio"]],
        }
        resolved = self._CHAT_DOMAIN_ALIASES.get(domain, domain)
        return packs.get(resolved, [base["ventas"], base["margen_neto"]])
