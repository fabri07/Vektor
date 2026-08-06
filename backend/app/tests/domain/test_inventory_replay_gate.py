"""F-H3.d.3 — cuáles ventas de un archivo se quedan sin stock que las respalde."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

from app.domain.inventory_replay_gate import ReplayRow, rows_without_stock_backing

_VELA = uuid4()
_TAZA = uuid4()


def _fila(
    ctx: str, idx: int, day: date, qty: int, product_id=_VELA, sheet_rank: int = 0
) -> ReplayRow:
    return ReplayRow(
        key=(ctx, idx), product_id=product_id, day=day, qty=qty, sheet_rank=sheet_rank
    )


def test_alcanza_para_todas_no_rechaza_ninguna() -> None:
    filas = [
        _fila("v", 0, date(2024, 3, 3), 6),
        _fila("v", 1, date(2024, 3, 10), 4),
    ]
    assert rows_without_stock_backing(filas, {_VELA: 10}) == []


def test_la_que_sobra_es_la_mas_nueva_no_la_ultima_del_archivo() -> None:
    """Con 10 unidades y dos ventas de 6, la que se queda afuera es la del 10/03.

    Está escrita con la fila más nueva PRIMERO en la lista justamente para que el
    orden del archivo no pueda ser lo que decide.
    """
    filas = [
        _fila("v", 0, date(2024, 3, 10), 6),
        _fila("v", 1, date(2024, 3, 3), 6),
    ]

    sin_respaldo = rows_without_stock_backing(filas, {_VELA: 10})

    assert [r.key for r in sin_respaldo] == [("v", 0)]
    assert sin_respaldo[0].disponible == 4


def test_el_orden_de_las_hojas_no_cambia_el_resultado() -> None:
    """Las mismas ventas repartidas en dos hojas, con las solapas al revés.

    Es el control del anterior: si el gate recorriera por hoja, acá se rechazaría
    una fila distinta según cuál solapa viniera primero.
    """
    ventas_primero = [
        _fila("hoja_a", 0, date(2024, 3, 10), 6, sheet_rank=0),
        _fila("hoja_b", 0, date(2024, 3, 3), 6, sheet_rank=1),
    ]
    ventas_al_reves = [
        _fila("hoja_b", 0, date(2024, 3, 3), 6, sheet_rank=0),
        _fila("hoja_a", 0, date(2024, 3, 10), 6, sheet_rank=1),
    ]

    rechazo_1 = rows_without_stock_backing(ventas_primero, {_VELA: 10})
    rechazo_2 = rows_without_stock_backing(ventas_al_reves, {_VELA: 10})

    assert [r.key for r in rechazo_1] == [("hoja_a", 0)]
    assert [r.key for r in rechazo_2] == [("hoja_a", 0)]


def test_una_fila_rechazada_no_se_come_el_stock_de_las_siguientes() -> None:
    """Una venta imposible de cubrir no puede arrastrar a las que sí entraban.

    Descontar igual lo que no se importó dejaría el saldo en -10 y todas las
    ventas posteriores del producto quedarían rechazadas por una sola fila mala.
    """
    filas = [
        _fila("v", 0, date(2024, 3, 3), 20),
        _fila("v", 1, date(2024, 3, 10), 5),
    ]

    sin_respaldo = rows_without_stock_backing(filas, {_VELA: 10})

    assert [r.key for r in sin_respaldo] == [("v", 0)]


def test_cada_producto_lleva_su_propia_cuenta() -> None:
    filas = [
        _fila("v", 0, date(2024, 3, 3), 8, product_id=_VELA),
        _fila("v", 1, date(2024, 3, 3), 8, product_id=_TAZA),
    ]

    sin_respaldo = rows_without_stock_backing(filas, {_VELA: 10, _TAZA: 2})

    assert [r.key for r in sin_respaldo] == [("v", 1)]


def test_un_producto_sin_saldo_conocido_cuenta_como_cero() -> None:
    """Ausente del dict no es "no sé": es que no hay unidades que respalden nada."""
    filas = [_fila("v", 0, date(2024, 3, 3), 1)]

    sin_respaldo = rows_without_stock_backing(filas, {})

    assert [r.key for r in sin_respaldo] == [("v", 0)]
    assert sin_respaldo[0].disponible == 0


def test_cantidad_no_positiva_se_ignora() -> None:
    """Ni rechaza ni consume: una fila sin cantidad no habla de unidades."""
    filas = [
        _fila("v", 0, date(2024, 3, 3), 0),
        _fila("v", 1, date(2024, 3, 4), 10),
    ]

    assert rows_without_stock_backing(filas, {_VELA: 10}) == []


def test_a_igual_fecha_desempata_la_hoja_y_despues_el_orden_de_llegada() -> None:
    """Empate total de fecha: el resultado tiene que ser estable, no del dict.

    No se desempata por la clave: es opaca (una tupla al confirmar, un UUID al
    aplicar) y ordenar por ella haría que la decisión dependa de un id aleatorio.
    """
    filas = [
        _fila("b", 1, date(2024, 3, 3), 4, sheet_rank=1),
        _fila("a", 0, date(2024, 3, 3), 4, sheet_rank=0),
        _fila("a", 1, date(2024, 3, 3), 4, sheet_rank=0),
    ]

    sin_respaldo = rows_without_stock_backing(filas, {_VELA: 8})

    assert [r.key for r in sin_respaldo] == [("b", 1)]
