"""Productos parecidos: agrupa para que una persona decida; no fusiona nada."""

from __future__ import annotations

import uuid

from app.application.services.product_similarity import (
    ProductoParaComparar,
    agrupar_parecidos,
    clave_de_parecido,
)


def _p(name: str) -> ProductoParaComparar:
    return ProductoParaComparar(uuid.uuid4(), name)


def test_el_caso_del_dueno_se_agrupa_aunque_cambie_plural_orden_y_talle() -> None:
    productos = [
        _p("Jean azul clásico"),
        _p("Jean azul T40"),
        _p("Jeans azules T40"),
        _p("Jean azul 40"),
        _p("Remera básica"),
    ]
    # «clásico» es una palabra del artículo: queda fuera del grupo de los jeans.
    grupos = agrupar_parecidos(productos)
    assert len(grupos) == 1
    nombres = sorted(m.name for m in grupos[0].miembros)
    assert nombres == ["Jean azul 40", "Jean azul T40", "Jeans azules T40"]
    # Mismo talle (40) en los tres: es una coincidencia fuerte.
    assert grupos[0].mismos_numeros is True


def test_talles_distintos_se_marcan_como_duda_no_como_coincidencia() -> None:
    grupos = agrupar_parecidos([_p("Jean azul T40"), _p("Jean azul T42")])
    assert len(grupos) == 1
    assert grupos[0].mismos_numeros is False  # «T40» y «T42» son productos distintos


def test_el_orden_de_las_palabras_no_cuenta() -> None:
    assert clave_de_parecido("Azul jean") == clave_de_parecido("Jean azul")


def test_un_nombre_sin_palabras_identificatorias_no_forma_grupo() -> None:
    assert agrupar_parecidos([_p("500 ml"), _p("500ml")]) == []


def test_nombres_realmente_distintos_no_se_agrupan() -> None:
    assert agrupar_parecidos([_p("Yerba mate"), _p("Azúcar")]) == []
