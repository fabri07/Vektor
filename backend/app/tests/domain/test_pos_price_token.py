"""La firma del precio: qué acepta y qué no.

Lo que se protege acá: que un cliente no pueda declarar un precio que el
servidor no emitió. Es lo único que hace seguro que la caja mande su propio
precio, y sin eso una cola offline editable podría cobrar de menos.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.pos_price_token import (
    VIGENCIA_SEGUNDOS,
    ResultadoToken,
    emitir,
    verificar,
)

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
PRODUCTO = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _emitido(**cambios):
    datos = {
        "tenant_id": TENANT,
        "product_id": PRODUCTO,
        "sale_price_ars": Decimal("1234.56"),
        "product_name": "Yerba Playadito 1 kg",
        "sale_unit": "unit",
        "base_units_per_sale_unit": 1,
    }
    datos.update(cambios)
    return datos


class TestFirma:
    def test_lo_que_el_servidor_emitio_se_verifica(self) -> None:
        datos = _emitido()
        token = emitir(**datos)
        assert verificar(token, **datos) is ResultadoToken.VALIDO

    @pytest.mark.parametrize(
        "cambio",
        [
            {"sale_price_ars": Decimal("1.00")},
            {"base_units_per_sale_unit": 1000},
            {"sale_unit": "gram"},
            {"product_name": "Yerba Playadito 500 g"},
            {"product_id": uuid.uuid4()},
            {"tenant_id": uuid.uuid4()},
        ],
        ids=["precio", "factor", "unidad", "nombre", "otro producto", "otro negocio"],
    )
    def test_cambiar_cualquier_dato_invalida(self, cambio: dict) -> None:
        """La firma cubre los seis: cambiar uno solo tiene que romperla.

        El factor importa tanto como el precio: el importe es
        `precio × cantidad / factor`, así que firmar sólo el precio dejaba
        abierto que el importe cambiara igual.
        """
        token = emitir(**_emitido())
        assert verificar(token, **_emitido(**cambio)) is ResultadoToken.INVALIDO

    @pytest.mark.parametrize(
        "token", ["", None, "no-tiene-punto", "abc.def", ".", "9999999999."]
    )
    def test_token_malformado_es_invalido_sin_explotar(self, token: str | None) -> None:
        assert verificar(token, **_emitido()) is ResultadoToken.INVALIDO

    def test_el_precio_se_canoniza_a_dos_decimales(self) -> None:
        """`100.0` y `100.00` son el mismo precio escrito distinto.

        El cliente manda un string y Pydantic lo parsea: sin canonizar, un
        reintento legítimo de una venta ya cobrada quedaría rechazado por una
        diferencia de tipeo.
        """
        token = emitir(**_emitido(sale_price_ars=Decimal("100.00")))
        assert (
            verificar(token, **_emitido(sale_price_ars=Decimal("100.0")))
            is ResultadoToken.VALIDO
        )
        assert (
            verificar(token, **_emitido(sale_price_ars=Decimal("100")))
            is ResultadoToken.VALIDO
        )


class TestVigencia:
    def test_dentro_de_la_ventana_vale(self) -> None:
        ahora = datetime.now(UTC)
        token = emitir(**_emitido(), ahora=ahora - timedelta(days=29))
        assert verificar(token, **_emitido(), ahora=ahora) is ResultadoToken.VALIDO

    def test_vencido_se_distingue_de_invalido(self) -> None:
        """Son dos problemas distintos y se arreglan distinto.

        Un token vencido es una venta vieja que hay que revisar; uno inválido es
        una petición que este servidor no emitió.
        """
        ahora = datetime.now(UTC)
        token = emitir(**_emitido(), ahora=ahora - timedelta(seconds=VIGENCIA_SEGUNDOS + 60))
        assert verificar(token, **_emitido(), ahora=ahora) is ResultadoToken.VENCIDO

    def test_la_fecha_de_emision_no_se_mira_sin_firma_valida(self) -> None:
        """Primero la firma, después el vencimiento.

        Al revés, un token con la firma cambiada y una emisión vieja saldría
        como VENCIDO, y el mensaje le diría al dueño que revise una venta vieja
        en vez de que alguien mandó algo que el servidor no emitió.
        """
        ahora = datetime.now(UTC)
        viejo = int((ahora - timedelta(days=400)).timestamp())
        assert (
            verificar(f"{viejo}.{'0' * 32}", **_emitido(), ahora=ahora)
            is ResultadoToken.INVALIDO
        )


class TestNombreConSeparador:
    """Un nombre con `|` no puede correr la frontera entre dos campos.

    Sin largo prefijado, «Coca 2|5 L» con unidad «unit» y «Coca 2» con unidad
    «5 L|unit» producen el MISMO mensaje: un token emitido para uno serviría
    para el otro, y bastaría con editar la cola para cobrar un producto con la
    firma de otro. Es el motivo de `_campo`.
    """

    def test_dos_productos_distintos_no_comparten_firma(self) -> None:
        uno = emitir(**_emitido(product_name="Coca 2|5 L", sale_unit="unit"))
        otro = emitir(**_emitido(product_name="Coca 2", sale_unit="5 L|unit"))
        assert uno.split(".")[1] != otro.split(".")[1]

    def test_el_token_de_uno_no_sirve_para_el_otro(self) -> None:
        token = emitir(**_emitido(product_name="Coca 2|5 L", sale_unit="unit"))
        cruzado = verificar(
            token, **_emitido(product_name="Coca 2", sale_unit="5 L|unit")
        )
        assert cruzado is ResultadoToken.INVALIDO

    def test_un_nombre_con_separador_se_verifica_contra_si_mismo(self) -> None:
        datos = _emitido(product_name="Fernet 750|1 L")
        assert verificar(emitir(**datos), **datos) is ResultadoToken.VALIDO
