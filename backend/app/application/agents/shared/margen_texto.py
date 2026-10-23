"""Cómo se le dice el margen al LLM (M·2b-2). Sólo formatea: no calcula.

El número sale de FactsService (``facts_provider.margenes_del_tenant``). Acá se
asegura que un margen ESTIMADO llegue dicho como estimado y uno AUSENTE como
«sin medir» con su motivo, nunca como 0.
"""

from __future__ import annotations

from typing import Any

from app.application.services.facts_service import BusinessFact


def describir_margen_bruto(f: BusinessFact) -> str:
    """«32.5% (confirmado; …)», «32.5% ESTIMADO (…)» o «sin medir (…)»."""
    d = f.detail or {}
    if f.value is None:
        return f"sin medir ({d.get('motivo') or 'sin ventas en el período'})"
    cobertura = (
        f"cobertura de costo confirmada {d.get('cobertura_confirmada_pct')}%, "
        f"con estimaciones {d.get('cobertura_total_pct')}%"
    )
    if d.get("estado") == "estimado":
        return f"{f.value}% ESTIMADO ({d.get('motivo')}; {cobertura})"
    return f"{f.value}% ({cobertura})"


def datos_margen_bruto(f: BusinessFact) -> dict[str, Any]:
    """Los campos del margen bruto para un ``structured_data`` JSON-serializable."""
    d = f.detail or {}
    return {
        "margen_bruto_pct": f.value,
        "margen_bruto_estado": d.get("estado"),
        "margen_bruto_motivo": d.get("motivo"),
        "cobertura_costo_confirmada_pct": d.get("cobertura_confirmada_pct"),
        "cobertura_costo_total_pct": d.get("cobertura_total_pct"),
    }


_MESES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
    "septiembre", "octubre", "noviembre", "diciembre",
)


def nombre_del_mes(f: BusinessFact) -> str:
    """«septiembre 2026» desde ``detail["periodo_desde"]`` o el label ``mes_AAAA_MM``."""
    d = f.detail or {}
    desde = d.get("periodo_desde")
    texto = str(desde) if desde else ""
    if not texto and f.period:
        partes = f.period.rsplit("_", 2)
        texto = f"{partes[-2]}-{partes[-1]}-01" if len(partes) == 3 else ""
    try:
        anio, mes = int(texto[:4]), int(texto[5:7])
    except ValueError:
        return f.period or "el período"
    return f"{_MESES[mes - 1]} {anio}"


def describir_margen_neto(f: BusinessFact) -> str:
    """El neto del mes cerrado (M·3-1): «12.4% (…)», «… ESTIMADO» o «sin medir (…)»."""
    d = f.detail or {}
    if f.value is None:
        return f"sin medir ({d.get('motivo') or 'sin ventas en el período'})"
    detalle = (
        f"resultado ${d.get('resultado_ars')}; gastos operativos "
        f"${d.get('gastos_operativos_ars')}; cobertura de costo "
        f"{d.get('cobertura_total_pct')}%"
    )
    # M·3-3: los impuestos sin tipo restan igual; el IVA de un RI no resta.
    if d.get("impuestos_sin_tipo"):
        detalle += (
            f"; {d.get('impuestos_sin_tipo')} gastos de impuestos sin tipo "
            f"(${d.get('impuestos_sin_tipo_ars')}) se restaron igual"
        )
    if d.get("iva_excluido_ars"):
        detalle += f"; IVA pagado ${d.get('iva_excluido_ars')} no resta (ventas sin IVA)"
    if d.get("estado") == "estimado":
        return f"{f.value}% ESTIMADO ({d.get('motivo')}; {detalle})"
    return f"{f.value}% ({detalle})"


def describir_promedio_vs_reposicion(f: BusinessFact | None) -> str | None:
    """El efecto inflación sobre el stock (M·P4-2), o ``None``.

    ``None`` si la cuenta no está habilitada o el dato está ausente: no se dice
    nada (el margen principal ya se dijo). Arranca por la brecha y no repite el
    margen de reposición de la intersección (otro universo que el bruto): son
    datos al lado del margen, nunca un segundo margen.
    """
    if f is None or f.value is None:
        return None
    d = f.detail or {}
    brecha_pp = float(d.get("brecha_pp") or 0)  # type: ignore[arg-type]
    brecha_ars = float(d.get("brecha_ars") or 0)  # type: ignore[arg-type]
    efecto = (
        "ganancia por tenencia" if brecha_ars >= 0 else "pérdida por tenencia"
    )
    return (
        f"efecto inflación sobre el stock ({efecto}): {abs(brecha_pp):.1f} puntos, "
        f"${abs(brecha_ars):.2f}, en las ventas con costo de reposición y costo "
        f"promedio (cobertura {d.get('cobertura_conjunta_pct')}%); con costo promedio "
        f"esas ventas darían {f.value}%. NO es otro margen ni se suma al margen bruto"
    )
