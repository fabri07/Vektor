"""El código interno de producto: determinístico, estable y legible."""

from __future__ import annotations

import uuid

from app.domain.internal_sku import (
    INTERNAL_SKU_PREFIX,
    generate_internal_sku,
    is_internal_sku,
)

#: Caracteres que Crockford base32 excluye porque se confunden al copiar un
#: código a mano o leerlo de una etiqueta.
_AMBIGUOS = set("ILOU")


def test_el_mismo_producto_da_siempre_el_mismo_codigo() -> None:
    """Determinístico: es lo que hace imposible que una relectura lo cambie."""
    pid = uuid.uuid4()
    assert generate_internal_sku(pid) == generate_internal_sku(pid)


def test_productos_distintos_dan_codigos_distintos() -> None:
    codigos = {generate_internal_sku(uuid.uuid4()) for _ in range(5_000)}
    assert len(codigos) == 5_000


def test_el_formato_es_el_declarado() -> None:
    code = generate_internal_sku(uuid.uuid4())
    assert code.startswith(INTERNAL_SKU_PREFIX)
    cuerpo = code[len(INTERNAL_SKU_PREFIX) :]
    assert len(cuerpo) == 12
    assert not (set(cuerpo) & _AMBIGUOS), f"{code} trae un carácter ambiguo"


def test_el_codigo_sale_de_los_60_bits_bajos_del_uuid() -> None:
    """Dicho como es, no como suena mejor: el código usa los 60 bits menos
    significativos, así que dos ids que difieren SÓLO arriba de ese corte dan el
    mismo código.

    Es deliberado y es seguro: en un UUID v4 los bits altos incluyen versión y
    variante, que son constantes, y los 60 bajos son aleatorios. Tomar los altos
    gastaría caracteres del código en información que no distingue nada. El
    índice único es el que convierte una colisión real en un error ruidoso.
    """
    assert generate_internal_sku(uuid.UUID(int=1)) != generate_internal_sku(
        uuid.UUID(int=2)
    )
    solo_bits_altos = uuid.UUID(int=(1 << 120))
    assert generate_internal_sku(uuid.UUID(int=0)) == generate_internal_sku(
        solo_bits_altos
    )


def test_reconoce_un_codigo_propio() -> None:
    assert is_internal_sku(generate_internal_sku(uuid.uuid4()))


def test_no_confunde_un_codigo_del_proveedor_con_uno_propio() -> None:
    """El prefijo solo no alcanza: un SKU del proveedor podría empezar igual."""
    assert not is_internal_sku("VKT-123")  # largo incorrecto
    assert not is_internal_sku("VKT-ABCDEFGHIJKL")  # trae I y L, fuera del alfabeto
    assert not is_internal_sku("ABC-123456789012")
    assert not is_internal_sku(None)
    assert not is_internal_sku("")
