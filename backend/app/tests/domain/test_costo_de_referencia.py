"""M·1a — regla pura del costo de referencia (``app/domain/costo_de_referencia.py``)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from app.domain.business_time import today_ar
from app.domain.costo_de_referencia import (
    MOTIVO_COSTO_CARGADO_CERO,
    MOTIVO_SIN_COMPRA_NI_COSTO,
    CompraCandidata,
    costo_de_referencia,
    elegir_ultima_compra,
)

HOY = today_ar()


def _dt(d: date, hora: int = 12) -> datetime:
    return datetime.combine(d, time(hora), tzinfo=UTC)


def _c(
    fecha: date,
    costo: str | None,
    *,
    cargada: datetime | None = None,
    final: str | None = None,
    inicial: bool = False,
    **extra: object,
) -> CompraCandidata:
    return CompraCandidata(
        movement_id=uuid.uuid4(),
        fecha_negocio=_dt(fecha),
        cargada_en=cargada or _dt(HOY),
        costo_facturado=Decimal(costo) if costo is not None else None,
        costo_final=Decimal(final) if final is not None else None,
        es_stock_inicial=inicial,
        **extra,  # type: ignore[arg-type]
    )


def test_manda_la_fecha_de_negocio_no_la_de_carga() -> None:
    # Un comprobante viejo cargado hoy NO pisa a la compra más nueva cargada antes.
    nueva = _c(HOY - timedelta(days=2), "120", cargada=_dt(HOY - timedelta(days=2)))
    atrasada = _c(HOY - timedelta(days=30), "90", cargada=_dt(HOY))
    assert elegir_ultima_compra([atrasada, nueva], HOY) == nueva


def test_una_compra_posterior_al_corte_no_es_elegible() -> None:
    antes = _c(HOY - timedelta(days=10), "100")
    despues = _c(HOY - timedelta(days=1), "150")
    assert elegir_ultima_compra([antes, despues], HOY - timedelta(days=5)) == antes
    # El mismo día del corte sí entra.
    assert elegir_ultima_compra([antes, despues], HOY - timedelta(days=1)) == despues


def test_empate_de_fecha_desempata_por_carga_y_es_estable() -> None:
    dia = HOY - timedelta(days=3)
    primera = _c(dia, "100", cargada=_dt(HOY, 9))
    segunda = _c(dia, "110", cargada=_dt(HOY, 10))
    assert elegir_ultima_compra([primera, segunda], HOY) == segunda
    assert elegir_ultima_compra([segunda, primera], HOY) == segunda


def test_una_compra_sin_costo_no_es_fuente() -> None:
    con_costo = _c(HOY - timedelta(days=10), "100")
    sin_costo = _c(HOY - timedelta(days=1), None)
    en_cero = _c(HOY - timedelta(days=2), "0")
    assert elegir_ultima_compra([con_costo, sin_costo, en_cero], HOY) == con_costo


def test_sin_candidatas_no_hay_compra() -> None:
    assert elegir_ultima_compra([], HOY) is None


def test_compra_real_le_gana_al_stock_inicial_aunque_sea_mas_vieja() -> None:
    compra = _c(HOY - timedelta(days=20), "100")
    inicial = _c(HOY - timedelta(days=1), "80", inicial=True)
    r = costo_de_referencia([inicial, compra], HOY, Decimal("70"))
    assert (r.origen, r.costo_unitario) == ("compra", Decimal("100"))
    assert r.movement_id == compra.movement_id


def test_sin_compra_real_usa_el_stock_inicial() -> None:
    inicial = _c(HOY - timedelta(days=1), "80", inicial=True)
    r = costo_de_referencia([inicial], HOY, Decimal("70"))
    assert (r.origen, r.costo_unitario) == ("stock_inicial", Decimal("80"))
    assert not r.es_estimacion


def test_sin_compras_usa_el_costo_cargado_como_estimacion() -> None:
    r = costo_de_referencia([], HOY, Decimal("70"))
    assert (r.origen, r.costo_unitario, r.fecha, r.movement_id) == (
        "costo_cargado", Decimal("70"), None, None)
    assert r.es_estimacion
    assert not r.contrato_confirmado


def test_costo_cargado_en_cero_queda_pendiente() -> None:
    r = costo_de_referencia([], HOY, Decimal("0"))
    assert (r.origen, r.costo_unitario, r.motivo) == ("sin_costo", None, MOTIVO_COSTO_CARGADO_CERO)


def test_sin_nada_queda_sin_costo_con_motivo() -> None:
    r = costo_de_referencia([], HOY, None)
    assert (r.origen, r.costo_unitario, r.motivo) == ("sin_costo", None, MOTIVO_SIN_COMPRA_NI_COSTO)


def test_movimiento_viejo_usa_lo_facturado_y_no_queda_confirmado() -> None:
    r = costo_de_referencia([_c(HOY, "100")], HOY, None)
    assert r.costo_unitario == Decimal("100")
    assert (r.envio, r.impuesto) == ("desconocido", "desconocido")
    assert not r.contrato_confirmado


def test_con_contrato_usa_el_costo_final_y_sus_tratamientos() -> None:
    c = _c(
        HOY, "100", final="131",
        shipping_treatment="adicional", tax_treatment="adicional",
        tax_rate=Decimal("0.21"), tax_amount=Decimal("21"), tax_recoverable=False,
        document_ref="A-0001-00000042", contract_version=1,
    )
    r = costo_de_referencia([c], HOY, Decimal("90"))
    assert r.costo_unitario == Decimal("131")
    assert (r.envio, r.impuesto, r.tax_recoverable) == ("adicional", "adicional", False)
    assert r.document_ref == "A-0001-00000042"
    assert r.contrato_confirmado


def test_un_tratamiento_desconocido_no_se_cuela() -> None:
    c = _c(HOY, "100", shipping_treatment="raro", tax_treatment=None, contract_version=1)
    r = costo_de_referencia([c], HOY, None)
    assert (r.envio, r.impuesto) == ("desconocido", "desconocido")
