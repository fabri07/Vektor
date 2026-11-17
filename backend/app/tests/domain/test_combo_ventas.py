"""Combos de venta («Jean + buzo» $41.000): la cuenta pura que propone qué productos
suman el importe de la fila. Nunca escribe; la persona confirma."""

from __future__ import annotations

from decimal import Decimal

from app.domain.combo_ventas import (
    Candidato,
    candidatos_de_referencia,
    referencias_de,
    resolver,
)


def c(pid: str, nombre: str, precio: str, factor: int = 1) -> Candidato:
    return Candidato(pid, nombre, Decimal(precio), factor)


JEAN_A = c("j1", "Jean azul T40", "26000")
JEAN_B = c("j2", "Jeans azules T42", "21000")
BUZO = c("b1", "buzo canguro gris L", "20000")
REMERA = c("r1", "Remeras básicas blancas", "7500")


def test_referencias_de_un_combo() -> None:
    assert referencias_de("Jean + buzo") == ["Jean", "buzo"]
    assert referencias_de("Remera + buzo") == ["Remera", "buzo"]
    assert referencias_de("jean y buzo") == ["jean", "buzo"]
    assert referencias_de("2 jean, buzo") == ["jean", "buzo"]


def test_una_sola_referencia_no_es_un_combo() -> None:
    assert referencias_de("Buzo canguro") == []
    assert referencias_de("") == []
    assert referencias_de(None) == []
    assert referencias_de("uno + dos + tres + cuatro + cinco") == []  # más de 4: a mano


def test_candidatos_por_palabras_con_plural_plegado() -> None:
    todos = [JEAN_A, JEAN_B, BUZO, REMERA]
    assert [x.product_id for x in candidatos_de_referencia("jean", todos)] == ["j1", "j2"]
    assert [x.product_id for x in candidatos_de_referencia("Remera", todos)] == ["r1"]
    assert candidatos_de_referencia("campera", todos) == []


def test_una_unica_combinacion_que_suma_el_importe() -> None:
    # 21.000 (jeans T42) + 20.000 (buzo) = 41.000; con el jean T40 sería 46.000.
    sol = resolver(Decimal("41000"), [[JEAN_A, JEAN_B], [BUZO]])
    assert len(sol) == 1
    assert [(ln.product_id, ln.cantidad, ln.importe) for ln in sol[0]] == [
        ("j2", 1, Decimal("21000.00")),
        ("b1", 1, Decimal("20000.00")),
    ]


def test_sin_combinacion_exacta_no_inventa() -> None:
    assert resolver(Decimal("41500"), [[JEAN_A, JEAN_B], [BUZO]]) == []


def test_cantidades_distintas_de_uno() -> None:
    # 2 remeras (15.000) + 1 buzo (20.000) = 35.000
    sol = resolver(Decimal("35000"), [[REMERA], [BUZO]])
    assert [(ln.product_id, ln.cantidad) for ln in sol[0]] == [("r1", 2), ("b1", 1)]


def test_varias_soluciones_se_devuelven_todas_y_en_orden_estable() -> None:
    a = c("x1", "Jean liso", "10000")
    b = c("x2", "Buzo liso", "15000")
    # 55.000 = 1 jean + 3 buzos (10k+45k) = 4 jeans + 1 buzo (40k+15k): dos soluciones.
    sol = resolver(Decimal("55000"), [[a], [b]])
    assert len(sol) == 2
    assert sol == resolver(Decimal("55000"), [[a], [b]])
    assert sum(ln.cantidad for ln in sol[0]) <= sum(ln.cantidad for ln in sol[-1])


def test_un_mismo_producto_no_cubre_dos_referencias() -> None:
    x = c("m1", "Buzo jean", "10000")
    assert resolver(Decimal("20000"), [[x], [x]]) == []


def test_producto_por_peso_usa_el_factor() -> None:
    queso = c("q1", "Queso", "800", factor=1000)  # $800 el kilo
    pan = c("p1", "Pan", "200")
    sol = resolver(Decimal("1000"), [[queso], [pan]])
    assert [(ln.product_id, ln.cantidad) for ln in sol[0]] == [("q1", 1), ("p1", 1)]


def test_el_espacio_de_busqueda_esta_acotado() -> None:
    muchos = [[c(f"{i}-{k}", f"p{i}{k}", str(100 + k)) for k in range(12)] for i in range(4)]
    # No cuelga y devuelve a lo sumo MAX_SOLUCIONES.
    assert len(resolver(Decimal("450"), muchos)) <= 20


def test_una_coma_o_barra_dentro_de_un_numero_no_separa_productos() -> None:
    assert referencias_de("Coca 1,5L") == []
    assert referencias_de("Aceite 1/2 L") == []
    assert referencias_de("Jean, buzo") == ["Jean", "buzo"]
    assert referencias_de("Jean / buzo") == ["Jean", "buzo"]
