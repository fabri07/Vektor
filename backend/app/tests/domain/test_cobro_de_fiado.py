"""«Cobro saldo fiado» es un cobro de cuenta corriente, no una venta; un «cobro» a secas
o una venta fiada no lo son."""

from __future__ import annotations

import pytest

from app.domain.cobro_de_fiado import es_texto_de_cobro_de_fiado


@pytest.mark.parametrize(
    "texto",
    [
        "Cobro saldo fiado",
        "cobro SALDO FIADO",
        "Cobro de saldo",
        "Pago de deuda",
        "Cancelación de deuda",
        "Cobro cuenta corriente",
        "Abono cta cte",
    ],
)
def test_es_un_cobro_de_cuenta_corriente(texto: str) -> None:
    assert es_texto_de_cobro_de_fiado(texto)


@pytest.mark.parametrize(
    "texto",
    [
        "Cobro",
        "Cobro mostrador",
        "Venta fiada",
        "Fiado remera",
        "Pago con tarjeta",
        "Ventas del día",
        "Jean + buzo",
        "",
        None,
    ],
)
def test_lo_demas_sigue_siendo_una_venta(texto: str | None) -> None:
    assert not es_texto_de_cobro_de_fiado(texto)
