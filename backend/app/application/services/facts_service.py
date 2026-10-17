"""FactsService — Única fuente de verdad para las métricas de negocio de Véktor.

REGLA DE ORO (invariante del repo, ahora cumplida en TODAS las superficies):
    Si el dashboard muestra un número, el chat debe obtener ESE MISMO número
    desde acá. Toda aritmética financiera vive en este servicio. Nadie más
    computa métricas: ni el frontend, ni business_state_service, ni los
    endpoints de insights. Todos delegan acá.

DECISIONES CANÓNICAS RATIFICADAS (Fabrizio):
    - margen_bruto = (ventas − costo vendido) / ventas, con el costo CONGELADO por
      venta (snapshot M·2a/M·2c) y la definición única de domain/margen.py:
      cobertura sobre TODO lo vendido, piso del 50%, confirmado/estimado/ausente.
      Nunca el costo actual del producto ni las compras del período (M·2b).
    - margen_neto  = (ingreso − costo vendido − gastos operativos) / ingreso, por MES
      CERRADO (M·3-1, dueño 2026-10-16): gastos operativos = todo gasto vivo que no
      sea mercadería. Es el indicador de salud: tarjeta, chat, alertas y score.
      Antes era ventas − TODOS los gastos (restaba la mercadería COMPRADA).
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
from decimal import Decimal
from enum import Enum
from typing import Literal, Protocol

import pandas as pd
from pydantic import BaseModel

from app.domain.business_time import AR_TZ
from app.domain.expense_categories import normalize_expense_category
from app.domain.margen import (
    ESTADO_PROMEDIO_MEDIDO,
    ComparacionDeMargenes,
    LineaDeComparacion,
    LineaDeMargen,
    MedicionDeMargen,
    MedicionDeMargenNeto,
    comparar_promedio_y_reposicion,
    medir_margen,
    medir_margen_neto,
)
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

def es_categoria_de_mercaderia(categoria: object) -> bool:
    """¿La categoría de un gasto es compra de mercadería (aunque diga OPEX)?

    La migración 20260710_0001 dejó compras viejas como OPEX, y hay filas con el
    texto libre de antes del catálogo («mercaderia», «compras», «compra
    proveedor», «reposicion»…). Se normaliza con el MISMO catálogo de alias
    (``domain/expense_categories``): si no, el neto restaría como gasto una
    compra que ya está en el costo vendido.
    """
    if not isinstance(categoria, str):
        return False
    return normalize_expense_category(categoria)[0] == "INVENTORY"

# Muestra mínima para reportar una métrica como confiable sin advertencia.
MIN_CONFIDENCE_SAMPLE = 50  # < 50 ventas → REAL con confidence reducido, NUNCA blend

# Redondeo (mata la divergencia "cómo redondeás"):
ARS_DECIMALS = 2   # pesos con 2 decimales
PCT_DECIMALS = 1   # porcentajes con 1 decimal


# Umbrales INDEPENDIENTES del vertical: valen igual para kiosco, limpieza y
# decoración, así que sí tienen default (y se usan también cuando el servicio se
# construye sin `SeverityThresholds`, que es el caso "no sé el vertical").
VENTAS_DROP_WARNING = -20.0
VENTAS_DROP_CRITICAL = -35.0
SOBRESTOCK_DIAS_WARNING = 90


def _factor_de(prod: pd.DataFrame) -> pd.Series | int:
    """El factor de unidad de venta de cada producto (1 si el frame no lo trae).

    Un frame armado a mano —los tests de reconciliación— puede no tener la
    columna: ahí todo es por unidad y dividir por 1 no cambia nada.
    """
    if "base_units_per_sale_unit" in prod.columns:
        return prod["base_units_per_sale_unit"].fillna(1).clip(lower=1)
    return 1


@dataclass(frozen=True)
class SeverityThresholds:
    """Umbrales por vertical.

    Los umbrales de margen NO tienen default: replicarlos acá era regalar los
    del benchmark de kiosco a cualquiera que construyera `SeverityThresholds()`.
    Se construye con `facts_provider.thresholds_for_vertical(vertical)` — misma
    fuente JSON que el health engine, para que el severity del fact y el health
    score NUNCA se contradigan.
    """

    # Piso de margen NETO (en %, del benchmark del vertical):
    margen_neto_floor: float
    margen_neto_critical: float
    # Caída de ventas vs período anterior (variación %):
    ventas_drop_warning: float = VENTAS_DROP_WARNING
    ventas_drop_critical: float = VENTAS_DROP_CRITICAL
    # Días de stock sin rotación → sobrestock:
    sobrestock_dias_warning: int = SOBRESTOCK_DIAS_WARNING


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
    # Metadata estructurada de la métrica (p. ej. el margen: estado, coberturas,
    # supuestos). Los consumidores la leen en vez de parsear `source`.
    detail: dict[str, object] | None = None


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

    @classmethod
    def mes_cerrado(cls, *, today: date | None = None) -> Period:
        """El último mes calendario COMPLETO antes de ``today`` (M·3-1).

        El margen neto se mide por mes cerrado: en una ventana parcial el
        alquiler o los sueldos del mes todavía no están o caen enteros.
        """
        today = today or datetime.now(AR_TZ).date()
        fin = today.replace(day=1) - timedelta(days=1)
        return cls(start=fin.replace(day=1), end=fin, label=f"mes_{fin:%Y_%m}")

    @classmethod
    def mes_en_curso(cls, *, today: date | None = None) -> Period:
        """Del 1.º del mes de ``today`` a ``today`` (el bruto mientras corre el mes)."""
        today = today or datetime.now(AR_TZ).date()
        return cls(start=today.replace(day=1), end=today,
                   label=f"mes_en_curso_{today:%Y_%m}")

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
        cost_ars:        float|NaN # costo CONGELADO de la venta (snapshot M·2a/M·2c);
                                   #   NaN = sin costo (sin producto, sin costear, sin_costo)
        cost_confirmed:  bool      # el snapshot es confirmado (opcional: sin columna = False)
        cost_avg_ars:    float|NaN # costo PROMEDIO de la venta (M·P3), un dato al lado del
                                   #   principal (opcional: sin columna = sin promedio)
        cost_avg_estado: str|None  # medido|incompleto|sin_medicion (opcional)
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

    mixed_tenders(tenant_id, start, end) -> DataFrame:   (B9, pago mixto de caja)
        sale_date:       date/datetime   # operation_date de la cabecera
        amount_ars:      float           # importe de ESE medio
        payment_method:  str             # método del tender (nunca "mixed")
        provenance:      str             # la de las líneas de la operación
        Una fila por tender de cada operación de caja mixta viva. Las ventas de
        esas operaciones vienen en `sales` con payment_method = "mixed": su
        importe cuenta como venta, y cómo se pagó sale de acá. Sin mixtos → vacío.
    """

    def sales(self, tenant_id: str, start: date, end: date) -> pd.DataFrame: ...
    def expenses(self, tenant_id: str, start: date, end: date) -> pd.DataFrame: ...
    def products(self, tenant_id: str) -> pd.DataFrame: ...
    def mixed_tenders(self, tenant_id: str, start: date, end: date) -> pd.DataFrame: ...
    # Opcional (M·3-1): ``primer_movimiento(tenant_id) -> date | None``, la fecha de
    # la primera venta o gasto vivos del negocio. Sin el método, el neto no puede
    # saber si el mes cerrado está completo y lo da como completo.


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

@dataclass(frozen=True)
class Liquidez:
    """Reparto de las ventas de un período entre medios (ver ``_split_liquidez``)."""

    ventas: float    # V
    cobros: float    # I — cobros de fiado ("inflow")
    fiado: float     # F
    diferido: float  # D — tarjeta de crédito
    liquida: float   # L = V − F − D + I


class FactsService:
    def __init__(self, provider: FactsDataProvider,
                 thresholds: SeverityThresholds | None = None, *,
                 fiscal_condition_crudo: str | None = None) -> None:
        self.provider = provider
        # Régimen CRUDO del tenant: el margen de un RI se compara sin IVA.
        self.fiscal_condition_crudo = fiscal_condition_crudo
        # `None` = el caller no sabe el vertical. Los umbrales independientes del
        # vertical siguen aplicando (constantes de módulo); los de margen NO se
        # inventan: sin benchmark del rubro, margen_neto sale sin severity.
        self.t = thresholds

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

    def _mixed_tenders(self, tenant_id: str, p: Period, include_demo: bool) -> pd.DataFrame:
        # Mismo período y MISMO filtro de procedencia que las ventas: un tender
        # REAL no puede sumarse a una liquidez calculada sobre ventas DEMO.
        return self._filter(
            self.provider.mixed_tenders(tenant_id, p.start, p.end), include_demo
        )

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

        drop_critical = self.t.ventas_drop_critical if self.t else VENTAS_DROP_CRITICAL
        drop_warning = self.t.ventas_drop_warning if self.t else VENTAS_DROP_WARNING
        severity: Literal["info", "warning", "critical"] | None = None
        if var is not None:
            if var <= drop_critical:
                severity = "critical"
            elif var <= drop_warning:
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
    def medir_margen(self, tenant_id: str, p: Period, *,
                     include_demo: bool = False) -> MedicionDeMargen:
        """El margen bruto del período con la definición única (domain/margen.py).

        Universo: ventas reales (sin cobros de fiado, sin anuladas, sin demo salvo
        ``include_demo``), CON las ventas sin producto: su importe es ingreso sin
        costo y baja la cobertura. El costo es el snapshot congelado por venta.
        """
        sales = self._ventas_reales(tenant_id, p, include_demo)
        if sales.empty:
            return medir_margen([], self.fiscal_condition_crudo)
        costos = sales["cost_ars"] if "cost_ars" in sales.columns else None
        confirmadas = (
            sales["cost_confirmed"].astype("boolean").fillna(False).astype(bool)
            if "cost_confirmed" in sales.columns
            else None
        )
        lineas = []
        for i, importe in enumerate(sales["amount_ars"]):
            costo = None if costos is None else costos.iloc[i]
            con_costo = costo is not None and not pd.isna(costo)
            lineas.append(LineaDeMargen(
                importe=Decimal(str(importe)),
                costo=Decimal(str(costo)) if con_costo else None,
                confirmado=bool(con_costo and confirmadas is not None
                                and confirmadas.iloc[i]),
            ))
        return medir_margen(lineas, self.fiscal_condition_crudo)

    def margen_bruto(self, tenant_id: str, p: Period, *,
                     include_demo: bool = False) -> BusinessFact:
        """Margen bruto de ventas (M·2b). Ausente → EMPTY con el motivo de cobertura.

        ``confidence`` es la cobertura de las líneas de las que sale el número
        (``MedicionDeMargen.cobertura_del_estado``). Con margen ausente es la
        cobertura con estimaciones que no alcanzó el piso (antes: 0.0); el hecho es
        EMPTY igual. El estado, las coberturas y los supuestos van en ``detail``.
        """
        sales = self._ventas_reales(tenant_id, p, include_demo)
        m = self.medir_margen(tenant_id, p, include_demo=include_demo)
        detail: dict[str, object] = {
            "estado": m.estado,
            "motivo": m.motivo,
            "cobertura_confirmada_pct": _pct(float(m.cobertura_confirmada * 100)),
            "cobertura_total_pct": _pct(float(m.cobertura_total * 100)),
            "ingreso_ars": _ars(float(m.ingreso_total)),
            "costo_vendido_ars": (
                _ars(float(m.costo_vendido)) if m.costo_vendido is not None else None
            ),
            "supuestos": list(m.supuestos),
        }
        if m.margen_pct is None:
            return BusinessFact(
                fact_id=f"margen_bruto_{p.label}", domain="rentabilidad",
                metric="margen_bruto", value=None, unit="%", period=p.label,
                provenance=Provenance.EMPTY,
                confidence=round(float(m.cobertura_total), 2),
                sample_size=len(sales), source="sales[snapshot_costo]",
                explanation_key="margen_bruto", detail=detail,
            )
        return BusinessFact(
            fact_id=f"margen_bruto_{p.label}", domain="rentabilidad", metric="margen_bruto",
            value=_pct(float(m.margen_pct)), unit="%", period=p.label,
            confidence=round(min(1.0, float(m.cobertura_del_estado)), 2),
            sample_size=len(sales), source=f"sales[snapshot_costo,{m.estado}]",
            explanation_key="margen_bruto", detail=detail,
        )

    def comparar_promedio_y_reposicion(self, tenant_id: str, p: Period, *,
                                       include_demo: bool = False) -> ComparacionDeMargenes:
        """M·P4-2: margen con costo promedio y con reposición, sobre la intersección.

        Mismo universo que ``medir_margen``. No lo toca: es un dato al lado.
        """
        return self._comparar(self._ventas_reales(tenant_id, p, include_demo))

    def _comparar(self, sales: pd.DataFrame) -> ComparacionDeMargenes:
        # Vectorizado: corre en cada turno del chat y en cada recálculo del score
        # (``leer_margenes``). Las sumas del dominio son lineales, así que pasarle
        # la intersección y el resto como DOS líneas agregadas da lo mismo que
        # pasarle cada venta, sin un loop por fila.
        if sales.empty:
            return comparar_promedio_y_reposicion([], self.fiscal_condition_crudo)
        cols = sales.columns
        if not {"cost_ars", "cost_avg_ars", "cost_avg_estado"} <= set(cols):
            comun = pd.Series(False, index=sales.index)
        else:
            comun = (
                sales["cost_ars"].notna()
                & sales["cost_avg_ars"].notna()
                & (sales["cost_avg_estado"] == ESTADO_PROMEDIO_MEDIDO)
            )

        def total(serie: pd.Series) -> Decimal:
            return Decimal(str(float(serie.sum())))

        lineas = [LineaDeComparacion(
            importe=total(sales.loc[~comun, "amount_ars"]),
            costo_reposicion=None, costo_promedio=None, estado_promedio=None,
        )]
        if comun.any():
            lineas.append(LineaDeComparacion(
                importe=total(sales.loc[comun, "amount_ars"]),
                costo_reposicion=total(sales.loc[comun, "cost_ars"]),
                costo_promedio=total(sales.loc[comun, "cost_avg_ars"]),
                estado_promedio=ESTADO_PROMEDIO_MEDIDO,
            ))
        return comparar_promedio_y_reposicion(lineas, self.fiscal_condition_crudo)

    def margen_promedio_vs_reposicion(self, tenant_id: str, p: Period, *,
                                      include_demo: bool = False) -> BusinessFact:
        """El margen con costo promedio al lado del de reposición (M·P4-2).

        ``value`` es el margen con costo PROMEDIO sobre la intersección; el de
        reposición común, la brecha (efecto inflación sobre el stock) y la
        cobertura conjunta van en ``detail``. Nunca reemplaza ni se suma a
        ``margen_bruto``. Ausente → EMPTY con ``detail["motivo"]``.
        """
        sales = self._ventas_reales(tenant_id, p, include_demo)
        c = self._comparar(sales)

        def pct(x: Decimal | None) -> float | None:
            return None if x is None else _pct(float(x))

        detail: dict[str, object] = {
            "motivo": c.motivo_ausente,
            "cobertura_conjunta_pct": _pct(float(c.cobertura_conjunta * 100)),
            "ingreso_comun_ars": _ars(float(c.ingreso_comun)),
            "margen_reposicion_comun_pct": pct(c.margen_reposicion_pct),
            "margen_promedio_comun_pct": pct(c.margen_promedio_pct),
            "brecha_pp": pct(c.brecha_pp),
            "brecha_ars": None if c.brecha_ars is None else _ars(float(c.brecha_ars)),
        }
        return BusinessFact(
            fact_id=f"margen_promedio_vs_reposicion_{p.label}", domain="rentabilidad",
            metric="margen_promedio_vs_reposicion",
            value=None if c.ausente else pct(c.margen_promedio_pct),
            unit="%", period=p.label,
            provenance=Provenance.EMPTY if c.ausente else Provenance.REAL,
            confidence=round(min(1.0, float(c.cobertura_conjunta)), 2),
            sample_size=len(sales), source="sales[snapshot_costo,costo_promedio]",
            explanation_key="margen_promedio_vs_reposicion", detail=detail,
        )

    def _gastos_operativos(self, tenant_id: str, p: Period,
                           include_demo: bool) -> pd.DataFrame:
        """Los gastos que resta el margen neto: todo lo vivo que NO es mercadería.

        La mercadería ya está en el costo de lo vendido (con el costo congelado
        por venta); restar además la compra del período la contaba dos veces y un
        mes de reposición fuerte parecía una pérdida. Mismo criterio que
        ``business_state_service`` (``expense_type`` COGS + las categorías
        ``INVENTORY``/``mercaderia`` que esa columna no alcanza). El flete
        capitalizado ya lo saca el provider (``gasto_de_resultado``).
        """
        exp = self._expenses(tenant_id, p, include_demo)
        if exp.empty:
            return exp
        mercaderia = (exp["expense_type"] == "COGS") | exp["category"].map(
            es_categoria_de_mercaderia
        ).astype(bool)
        return exp[~mercaderia]

    def medir_margen_neto(self, tenant_id: str, p: Period, *,
                          include_demo: bool = False) -> MedicionDeMargenNeto:
        """El margen neto de ``p`` (un mes cerrado, para la salud) — M·3-1."""
        gastos = self._gastos_operativos(tenant_id, p, include_demo)
        total = Decimal(str(float(gastos["amount_ars"].sum()))) if len(gastos) else Decimal(0)
        primer = getattr(self.provider, "primer_movimiento", None)
        return medir_margen_neto(
            self.medir_margen(tenant_id, p, include_demo=include_demo),
            total,
            gastos_observados=not gastos.empty,
            datos_desde=primer(tenant_id) if callable(primer) else None,
            periodo_desde=p.start,
        )

    def margen_neto(self, tenant_id: str, p: Period, *, include_demo: bool = False,
                    benchmark_floor: float | None = None) -> BusinessFact:
        """LA métrica de salud (M·3-1): (ingreso − costo vendido − gastos operativos) / ingreso.

        ``p`` es un mes cerrado (``Period.mes_cerrado``) en todos los lectores de
        salud. Ausente → EMPTY con ``detail["motivo"]``; la severidad del
        benchmark sólo con número. Antes era «ventas − TODOS los gastos», que
        restaba la mercadería comprada en vez de la vendida.
        """
        sales = self._ventas_reales(tenant_id, p, include_demo)
        m = self.medir_margen_neto(tenant_id, p, include_demo=include_demo)
        gastos = self._gastos_operativos(tenant_id, p, include_demo)
        por_categoria = (
            {str(k): _ars(float(v)) for k, v in
             gastos.groupby("category")["amount_ars"].sum().items()}
            if len(gastos) else {}
        )
        bruto = self.medir_margen(tenant_id, p, include_demo=include_demo)
        detail: dict[str, object] = {
            "estado": m.estado,
            "motivo": m.motivo,
            "ingreso_ars": _ars(float(m.ingreso_total)),
            "costo_vendido_ars": (
                _ars(float(m.costo_vendido)) if m.costo_vendido is not None else None
            ),
            "gastos_operativos_ars": _ars(float(m.gastos_operativos)),
            "gastos_por_categoria": por_categoria,
            "resultado_ars": _ars(float(m.resultado)) if m.resultado is not None else None,
            "cobertura_confirmada_pct": _pct(float(bruto.cobertura_confirmada * 100)),
            "cobertura_total_pct": _pct(float(bruto.cobertura_total * 100)),
            "supuestos": list(m.supuestos),
            "periodo_desde": p.start.isoformat(),
            "periodo_hasta": p.end.isoformat(),
        }
        if m.margen_pct is None:
            return BusinessFact(
                fact_id=f"margen_neto_{p.label}", domain="rentabilidad",
                metric="margen_neto", value=None, unit="%", period=p.label,
                provenance=Provenance.EMPTY,
                confidence=round(min(1.0, float(bruto.cobertura_total)), 2),
                sample_size=len(sales), source="sales[snapshot_costo],expenses[operativos]",
                explanation_key="margen_neto", detail=detail,
            )
        margen = float(m.margen_pct)

        # Sin umbrales del vertical no hay con qué comparar el margen: el hecho
        # sale sin severity antes que con la severidad de otro rubro.
        severity: Literal["info", "warning", "critical"] | None = None
        if self.t is not None:
            floor = benchmark_floor if benchmark_floor is not None else self.t.margen_neto_floor
            severity = (
                "critical" if margen < self.t.margen_neto_critical
                else "warning" if margen < floor
                else "info"
            )

        return BusinessFact(
            fact_id=f"margen_neto_{p.label}", domain="rentabilidad", metric="margen_neto",
            value=_pct(margen), unit="%", period=p.label, severity=severity,
            confidence=round(min(1.0, float(bruto.cobertura_del_estado)), 2),
            sample_size=len(sales),
            source=f"sales[snapshot_costo,{m.estado}],expenses[operativos]",
            explanation_key="margen_neto", detail=detail,
        )

    def _split_liquidez(self, sales_full: pd.DataFrame, mixtos: pd.DataFrame) -> Liquidez:
        """Reparte las ventas del período entre medios, con el pago mixto de caja.

        V = ventas (sin cobros) · I = cobros de fiado ("inflow")
        F = fiado de las ventas con su método + fiado de los tenders mixtos
        D = crédito diferido, ídem · L = V − F − D + I
        Invariante: L − I + F + D == V.

        Las ventas con payment_method = "mixed" (B9) cuentan enteras en V, pero
        no se clasifican por su método: el reparto de su importe entre medios lo
        dan los tenders de ``mixtos``, cuya suma es, por construcción, la de esas
        ventas. Un invariante que cierra solo no alcanza para probar esto: si el
        mixto se leyera mal faltaría de F y de L a la vez y seguiría cerrando.
        """
        if sales_full.empty:
            return Liquidez(0.0, 0.0, 0.0, 0.0, 0.0)
        ventas, inflows = self._split_inflows(sales_full)
        pm = ventas["payment_method"].str.lower()
        total = float(ventas["amount_ars"].sum())
        fiado = float(ventas.loc[pm.isin(FIADO_METHODS), "amount_ars"].sum())
        delayed = float(ventas.loc[pm.isin(DELAYED_METHODS), "amount_ars"].sum())
        if not mixtos.empty:
            pm_t = mixtos["payment_method"].str.lower()
            fiado += float(mixtos.loc[pm_t.isin(FIADO_METHODS), "amount_ars"].sum())
            delayed += float(mixtos.loc[pm_t.isin(DELAYED_METHODS), "amount_ars"].sum())
        cobros = float(inflows["amount_ars"].sum()) if len(inflows) else 0.0
        # ratificado: fiado NO es caja; tarjeta tampoco (delay ~30d); cobros SÍ entran
        liquida = total - fiado - delayed + cobros
        return Liquidez(total, cobros, fiado, delayed, liquida)

    def liquidez(self, tenant_id: str, p: Period, *, include_demo: bool = False) -> Liquidez:
        """Los cinco números del reparto (V, I, F, D, L), para reconciliar."""
        return self._split_liquidez(
            self._sales(tenant_id, p, include_demo),
            self._mixed_tenders(tenant_id, p, include_demo),
        )

    def caja_liquida(self, tenant_id: str, p: Period, *,
                     include_demo: bool = False) -> BusinessFact:
        sales = self._sales(tenant_id, p, include_demo)
        liquida = self._split_liquidez(
            sales, self._mixed_tenders(tenant_id, p, include_demo)
        ).liquida
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
        fiado = self._split_liquidez(
            sales, self._mixed_tenders(tenant_id, p, include_demo)
        ).fiado
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
        # Costo por unidad de venta, stock en unidades base: se divide por el factor.
        factor = _factor_de(prod)
        valor = float((prod["stock_units"] * prod["unit_cost_ars"] / factor).sum())
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
        dias_warning = self.t.sobrestock_dias_warning if self.t else SOBRESTOCK_DIAS_WARNING
        frozen = prod[dias > dias_warning]
        out: list[BusinessFact] = []
        for _, r in frozen.iterrows():
            capital = float(
                r["stock_units"] * r["unit_cost_ars"] / r.get("base_units_per_sale_unit", 1)
            )
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
            # El neto es del último mes CERRADO (M·3-1), no de ``p``: el
            # proveedor carga ese mes (``build_facts_service``).
            "margen_neto": self.margen_neto(
                tenant_id, Period.mes_cerrado(today=p.end), include_demo=include_demo
            ),
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


# ─────────────────────────────────────────────────────────────────────────────
# 7. CAPITAL DE LOS SOCIOS (D·4) — fuera del flujo operativo
# ─────────────────────────────────────────────────────────────────────────────
# Aportes, retiros, préstamos de socio y devoluciones NO son ventas ni gastos:
# nada de esto toca ingresos, egresos, resultado ni margen (ver
# owner_capital.py). Acá sólo se agrupan, para la vista de capital del Balance.
# No va en `dashboard_snapshot` a propósito: el snapshot se calcula en cada
# request del dashboard y el capital lo pide una sola pantalla.
#
# Frame de entrada (facts_provider.load_capital_frame): movimientos VIVOS del
# tenant con occurred_on <= fin, historia completa (el préstamo pendiente es
# acumulado). Columnas: occurred_on, kind, form, amount_ars, partner_name.

_CAPITAL_KINDS_ENTRADA = ("aporte", "prestamo_socio")
_CAPITAL_KINDS_SALIDA = ("retiro", "devolucion_prestamo")


@dataclass(frozen=True)
class MontoPorForma:
    """Un monto del período partido en dinero y especie."""

    dinero: float
    especie: float


@dataclass(frozen=True)
class CapitalDeSocio:
    """Lo de un socio. ``socio`` None = movimientos sin socio identificado."""

    socio: str | None
    aportes: MontoPorForma
    retiros: MontoPorForma
    prestamos_recibidos: MontoPorForma
    devoluciones: MontoPorForma
    # Préstamos − devoluciones de TODA la historia hasta el fin del período.
    # Negativo = se devolvió más de lo que figura prestado: no se recorta a 0
    # (sería esconder un error de carga), se devuelve y se marca.
    prestamo_pendiente: float
    prestamo_pendiente_inconsistente: bool


@dataclass(frozen=True)
class FlujoDeCapitalEnDinero:
    """Plata de capital que entró y salió en DINERO durante el período."""

    entradas: float  # aportes + préstamos recibidos
    salidas: float   # retiros + devoluciones
    neto: float      # entradas − salidas


@dataclass(frozen=True)
class CapitalDeSocios:
    start: date
    end: date
    # False = ni movimientos en el período ni préstamos pendientes: vacío
    # explícito, sin ceros inventados (``socios`` vacío, totales None).
    has_data: bool
    hay_especie: bool
    socios: list[CapitalDeSocio]
    totales: CapitalDeSocio | None
    flujo_dinero: FlujoDeCapitalEnDinero | None


def _ars0(x: float) -> float:
    """Redondeo de una suma de capital (nunca NaN: la suma de nada es 0)."""
    return round(float(x), ARS_DECIMALS)


def _socio_key(value: object) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip()
    return s or None


def _montos(df: pd.DataFrame, kind: str) -> MontoPorForma:
    sel = df[df["kind"] == kind]
    dinero = sel.loc[sel["form"] == "dinero", "amount_ars"].sum()
    especie = sel.loc[sel["form"] == "especie", "amount_ars"].sum()
    return MontoPorForma(dinero=_ars0(dinero), especie=_ars0(especie))


def _capital_de(socio: str | None, periodo: pd.DataFrame,
                historia: pd.DataFrame) -> CapitalDeSocio:
    prestado = historia.loc[historia["kind"] == "prestamo_socio", "amount_ars"].sum()
    devuelto = historia.loc[historia["kind"] == "devolucion_prestamo", "amount_ars"].sum()
    pendiente = _ars0(float(prestado) - float(devuelto))
    return CapitalDeSocio(
        socio=socio,
        aportes=_montos(periodo, "aporte"),
        retiros=_montos(periodo, "retiro"),
        prestamos_recibidos=_montos(periodo, "prestamo_socio"),
        devoluciones=_montos(periodo, "devolucion_prestamo"),
        prestamo_pendiente=pendiente,
        prestamo_pendiente_inconsistente=pendiente < 0,
    )


def capital_de_socios(frame: pd.DataFrame, p: Period) -> CapitalDeSocios:
    """Aportes, retiros y préstamos por socio en ``p``, más el pendiente a ``p.end``.

    Puro: no conoce el ORM ni el tenant (el provider ya filtró tenant y
    anulados). Redondeo a ``ARS_DECIMALS`` como el resto del servicio.
    """
    vacio = CapitalDeSocios(start=p.start, end=p.end, has_data=False, hay_especie=False,
                            socios=[], totales=None, flujo_dinero=None)
    if frame.empty:
        return vacio
    df = frame.copy()
    df["amount_ars"] = df["amount_ars"].astype(float)
    # El socio es texto libre (no hay maestro): «Ana» y «ana » son la misma
    # persona. Se agrupa sin mayúsculas ni espacios de más y se muestra la
    # grafía de su primer movimiento; si no, un préstamo cargado a «Ana» y
    # devuelto a «ana» quedaría como dos socios, uno con pendiente negativo.
    df["_socio"] = [_socio_key(v) for v in df["partner_name"]]
    df["_socio_k"] = [None if s is None else " ".join(s.split()).casefold() for s in df["_socio"]]
    historia = df[pd.to_datetime(df["occurred_on"]).dt.date <= p.end]
    periodo = historia[p.contains(historia["occurred_on"])]
    grafia: dict[str, str] = {}
    en_orden = historia.sort_values("occurred_on", kind="stable")
    for k, nombre in en_orden[["_socio_k", "_socio"]].itertuples(index=False):
        if k is not None:
            grafia.setdefault(k, nombre)

    def del_socio(d: pd.DataFrame, clave: str | None) -> pd.DataFrame:
        return d[d["_socio_k"].isna()] if clave is None else d[d["_socio_k"] == clave]

    socios: list[CapitalDeSocio] = []
    # Con nombre primero (alfabético); «sin socio identificado» al final.
    claves = sorted(set(historia["_socio_k"]), key=lambda k: (k is None, k or ""))
    for clave in claves:
        del_periodo = del_socio(periodo, clave)
        socio = None if clave is None else grafia[clave]
        c = _capital_de(socio, del_periodo, del_socio(historia, clave))
        # Aparece si se movió en el período o si arrastra un préstamo pendiente.
        if not del_periodo.empty or c.prestamo_pendiente != 0:
            socios.append(c)
    if not socios:
        return vacio

    en_dinero = periodo[periodo["form"] == "dinero"]
    entradas = float(en_dinero.loc[en_dinero["kind"].isin(_CAPITAL_KINDS_ENTRADA),
                                   "amount_ars"].sum())
    salidas = float(en_dinero.loc[en_dinero["kind"].isin(_CAPITAL_KINDS_SALIDA),
                                  "amount_ars"].sum())
    return CapitalDeSocios(
        start=p.start,
        end=p.end,
        has_data=True,
        hay_especie=bool((periodo["form"] == "especie").any()),
        socios=socios,
        totales=_capital_de(None, periodo, historia),
        flujo_dinero=FlujoDeCapitalEnDinero(
            entradas=_ars0(entradas),
            salidas=_ars0(salidas),
            neto=_ars0(entradas - salidas),
        ),
    )
