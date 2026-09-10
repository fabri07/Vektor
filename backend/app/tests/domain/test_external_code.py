"""El código externo: qué fusiona y, sobre todo, qué no.

Fusionar dos identidades que no eran la misma es el error más caro que puede
cometer este módulo — deja un maestro con los datos de otro y no salta en ningún
total. Por eso la mitad de estos tests afirman que dos cosas parecidas siguen
siendo distintas.
"""

from __future__ import annotations

from app.domain.external_code import (
    clave_de_codigo_externo,
    mismo_codigo,
    normalizar_codigo,
    normalizar_sistema,
)


def test_el_mismo_codigo_escrito_distinto_es_el_mismo() -> None:
    """Mayúsculas, espacios y tildes son variantes de tipeo, no de identidad."""
    assert mismo_codigo("A-001", None, " a-001 ", None)
    assert mismo_codigo("Artículo", None, "articulo", None)


def test_los_ceros_a_la_izquierda_nunca_se_recortan() -> None:
    """Un código externo es una CADENA, no un número. ``007`` y ``7`` pueden ser
    dos artículos distintos del mismo catálogo, y fusionarlos deja un maestro con
    los datos de otro sin que salte en ningún total.

    Es la diferencia deliberada con `document_series` en `operation_identity`,
    donde el punto de venta SÍ se recorta porque ahí es un entero.
    """
    assert not mismo_codigo("007", None, "7", None)
    assert normalizar_codigo("007") == "007"


def test_dos_sistemas_distintos_con_el_mismo_codigo_no_se_confunden() -> None:
    """El "1024" de un sistema no es el "1024" de otro."""
    assert not mismo_codigo("1024", "tango", "1024", "bejerman")
    assert mismo_codigo("1024", "Tango", "1024", " tango ")


def test_el_codigo_propio_es_un_espacio_de_nombres_mas_no_un_comodin() -> None:
    """Sin sistema declarado el código es del negocio. Dos códigos propios
    iguales son la misma entidad; uno propio y uno de un sistema, no."""
    assert mismo_codigo("1024", None, "1024", "")
    assert not mismo_codigo("1024", None, "1024", "tango")


def test_dos_ausencias_no_son_iguales() -> None:
    """Dos maestros sin código no son el mismo maestro."""
    assert not mismo_codigo(None, None, None, None)
    assert not mismo_codigo("", None, "", None)
    assert clave_de_codigo_externo(None) is None
    assert clave_de_codigo_externo("   ") is None


def test_el_separador_no_se_puede_falsificar_desde_el_dato() -> None:
    """Con un separador que aparece en códigos reales (`-`, `:`), el par
    (sistema="a", código="b") colisionaría con (sistema="", código="a-b")."""
    assert clave_de_codigo_externo("b", "a") != clave_de_codigo_externo("a-b", None)
    assert clave_de_codigo_externo("b", "a") != clave_de_codigo_externo("a:b", None)


def test_la_clave_lleva_su_version_adelante() -> None:
    clave = clave_de_codigo_externo("A-001")
    assert clave is not None and clave.startswith("e1")


def test_un_codigo_larguisimo_no_empuja_al_sistema_fuera_de_la_clave() -> None:
    """Si los componentes se cortaran DESPUÉS de unirlos, un código de 200
    caracteres borraría el sistema y dos sistemas distintos colisionarían."""
    largo = "x" * 400
    assert clave_de_codigo_externo(largo, "tango") != clave_de_codigo_externo(
        largo, "bejerman"
    )


def test_normalizar_sistema_vacio_es_el_negocio() -> None:
    assert normalizar_sistema(None) == ""
    assert normalizar_sistema("  ") == ""
