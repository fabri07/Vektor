"""F-H3.d.3 — cuáles ventas de un archivo se quedan sin stock que las respalde."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

from app.domain.inventory_replay_gate import (
    CreditEvent,
    ReplayRow,
    productos_con_saldo_conocido,
    rows_without_stock_backing,
)

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


# ── F-F: las compras del archivo entran como créditos DATADOS ─────────────────


def _compra(day: date, qty: int, product_id=_VELA, sheet_rank: int = 0) -> CreditEvent:
    return CreditEvent(product_id=product_id, day=day, qty=qty, sheet_rank=sheet_rank)


def test_una_compra_del_archivo_respalda_la_venta_posterior() -> None:
    """El caso que antes obligaba a rechazar el archivo entero.

    Sin stock previo, un libro con la compra del 01/03 y la venta del 10/03 no se
    podía confirmar en modo replay: el saldo contra el cual validar lo cargaba el
    propio archivo. Como crédito con fecha, se evalúa como cualquier otro.
    """
    filas = [_fila("v", 0, date(2024, 3, 10), 6)]

    assert rows_without_stock_backing(filas, {}, [_compra(date(2024, 3, 1), 10)]) == []


def test_una_compra_posterior_no_respalda_la_venta_anterior() -> None:
    """Lo que pidió el usuario: «lo que se compró primero y lo que se vendió después».

    Es el control del test anterior. Antes toda compra del archivo estaba metida en
    el saldo inicial SIN fecha, así que una compra del 20/03 respaldaba una venta
    del 10/03 — y el inventario decía que había unidades que todavía no existían.
    """
    filas = [_fila("v", 0, date(2024, 3, 10), 6)]

    sin_respaldo = rows_without_stock_backing(filas, {}, [_compra(date(2024, 3, 20), 10)])

    assert [r.key for r in sin_respaldo] == [("v", 0)]
    assert sin_respaldo[0].disponible == 0


def test_a_igual_fecha_la_compra_entra_antes_que_la_venta() -> None:
    """Mismo desempate que `replay_timeline`: crédito antes que débito.

    Sin esto, una compra y una venta del mismo día —el caso más común en un libro
    diario— daría un falso negativo que manda la venta a «Otros».
    """
    filas = [_fila("v", 0, date(2024, 3, 3), 6)]

    assert rows_without_stock_backing(filas, {}, [_compra(date(2024, 3, 3), 6)]) == []


def test_el_credito_se_suma_al_saldo_previo_no_lo_reemplaza() -> None:
    filas = [_fila("v", 0, date(2024, 3, 10), 7)]

    assert rows_without_stock_backing(filas, {_VELA: 4}, [_compra(date(2024, 3, 5), 3)]) == []


def test_el_credito_de_otro_producto_no_respalda_nada() -> None:
    filas = [_fila("v", 0, date(2024, 3, 10), 6, product_id=_VELA)]

    sin_respaldo = rows_without_stock_backing(
        filas, {}, [_compra(date(2024, 3, 1), 10, product_id=_TAZA)]
    )

    assert [r.key for r in sin_respaldo] == [("v", 0)]


def test_un_credito_sin_cantidad_no_suma() -> None:
    filas = [_fila("v", 0, date(2024, 3, 10), 1)]

    sin_respaldo = rows_without_stock_backing(filas, {}, [_compra(date(2024, 3, 1), 0)])

    assert [r.key for r in sin_respaldo] == [("v", 0)]


def test_sin_creditos_el_resultado_es_el_de_siempre() -> None:
    """El parámetro es opcional y su ausencia no cambia nada: los callers que no
    tienen créditos que declarar siguen viendo el gate de antes."""
    filas = [
        _fila("v", 0, date(2024, 3, 10), 6),
        _fila("v", 1, date(2024, 3, 3), 6),
    ]

    assert rows_without_stock_backing(filas, {_VELA: 10}) == rows_without_stock_backing(
        filas, {_VELA: 10}, []
    )


# ── F-F.2: «sé que no hay» no es lo mismo que «no sé» ─────────────────────────


def test_un_producto_con_unidades_tiene_saldo_conocido() -> None:
    conocidos = productos_con_saldo_conocido(
        [_VELA], saldo_previo={_VELA: 3}, declarados_por_el_archivo=set(), con_historial=set()
    )

    assert conocidos == frozenset({_VELA})


def test_un_cero_sin_procedencia_no_es_saldo_conocido() -> None:
    """El producto está en cero, nadie lo compró nunca y el archivo no lo declara.

    Ese cero no afirma que no hubiera stock: afirma que nunca se cargó inventario.
    Tratarlo como "no hay" para sacar una venta de los libros sería inventar el
    dato que falta, con el signo cambiado.
    """
    conocidos = productos_con_saldo_conocido(
        [_VELA], saldo_previo={}, declarados_por_el_archivo=set(), con_historial=set()
    )

    assert conocidos == frozenset()


def test_un_cero_con_historial_en_el_ledger_si_es_saldo_conocido() -> None:
    """Se compró y se vendió todo: el cero es el resultado de una historia."""
    conocidos = productos_con_saldo_conocido(
        [_VELA], saldo_previo={_VELA: 0}, declarados_por_el_archivo=set(), con_historial={_VELA}
    )

    assert conocidos == frozenset({_VELA})


def test_un_cero_que_el_archivo_declara_es_saldo_conocido() -> None:
    """Un catálogo que dice «0 unidades» o una compra del archivo son afirmaciones."""
    conocidos = productos_con_saldo_conocido(
        [_VELA], saldo_previo={_VELA: 0}, declarados_por_el_archivo={_VELA}, con_historial=set()
    )

    assert conocidos == frozenset({_VELA})


def test_cada_producto_se_juzga_por_su_cuenta() -> None:
    conocidos = productos_con_saldo_conocido(
        [_VELA, _TAZA],
        saldo_previo={_VELA: 5},
        declarados_por_el_archivo=set(),
        con_historial=set(),
    )

    assert conocidos == frozenset({_VELA})
