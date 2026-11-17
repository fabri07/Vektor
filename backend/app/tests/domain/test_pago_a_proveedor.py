"""El texto de una fila que dice «le pagué a un proveedor»: una acción Y «proveedor»."""

import pytest

from app.domain.pago_a_proveedor import es_texto_de_pago_a_proveedor


@pytest.mark.parametrize(
    "texto",
    ["Pago a proveedor", "pago proveedor Textil Sur", "Cancelación proveedor", "PAGO A PROVEEDORES",
     "Abono prov. 22/09"],
)
def test_reconoce_el_pago(texto: str) -> None:
    assert es_texto_de_pago_a_proveedor(texto)


@pytest.mark.parametrize(
    "texto", ["Pago de alquiler", "Proveedor de packaging", "Compra a proveedor", "", None, "Luz"]
)
def test_lo_ambiguo_sigue_siendo_gasto(texto: str | None) -> None:
    assert not es_texto_de_pago_a_proveedor(texto)


def test_alcanza_con_que_lo_diga_un_texto_de_la_fila() -> None:
    assert es_texto_de_pago_a_proveedor("Gasto", None, "Pago a proveedor")
