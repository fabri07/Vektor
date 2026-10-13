"""M·P1: las reglas del costo promedio ponderado móvil (dominio puro)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.application.services import inventory_movement_origin as origen
from app.domain import costo_promedio as cp
from app.domain.costo_promedio import Evento, costo_a, proyectar
from app.domain.sale_unit import importe

_BASE = datetime(2026, 3, 1, tzinfo=UTC)
_SEQ = iter(range(1, 1_000_000))


def ev(
    dia: int,
    clase: cp.Clase,
    unidades: int,
    costo: str | None = None,
    *,
    hora: int = 12,
    creado: datetime | None = None,
) -> Evento:
    fecha = _BASE + timedelta(days=dia - 1, hours=hora)
    return Evento(
        fecha=fecha,
        clase=clase,
        unidades=unidades,
        costo=None if costo is None else Decimal(costo),
        creado=creado or fecha,
        id=uuid.UUID(int=next(_SEQ)),
    )


def d(dia: int) -> date:
    return (_BASE + timedelta(days=dia - 1)).date()


def _q(v: Decimal | None) -> Decimal | None:
    return None if v is None else v.quantize(Decimal("0.01"))


# ── aceptación ──────────────────────────────────────────────────────────────


def test_aceptacion_166_67() -> None:
    p = proyectar([ev(1, "compra", 10, "100"), ev(2, "salida", 5), ev(3, "compra", 10, "200")])
    e = costo_a(p, d(3))
    assert _q(e.promedio) == Decimal("166.67")
    assert e.estado == "medido"
    assert e.u_conocidas == 15


def test_anular_la_venta_previa_cambia_el_promedio_posterior() -> None:
    """Sin la venta del día 2 el stock previo es 10, no 5: $150, no $166,67."""
    p = proyectar([ev(1, "compra", 10, "100"), ev(3, "compra", 10, "200")])
    assert _q(costo_a(p, d(3)).promedio) == Decimal("150.00")


def test_una_venta_no_mueve_el_promedio() -> None:
    p = proyectar([ev(1, "compra", 10, "100"), ev(2, "compra", 10, "200"), ev(3, "salida", 7)])
    assert costo_a(p, d(2)).promedio == costo_a(p, d(3)).promedio == Decimal("150")


# ── costo desconocido ───────────────────────────────────────────────────────


def test_compra_sin_costo_no_entra_en_el_promedio() -> None:
    """10 a $100 + 10 sin costo + 10 a $200: no $133,33 (sería valorizar las
    unidades sin costo al promedio anterior) sino $150 sobre 20 conocidas."""
    p = proyectar([
        ev(1, "compra", 10, "100"),
        ev(2, "compra", 10),
        ev(3, "compra", 10, "200"),
    ])
    e = costo_a(p, d(3))
    assert _q(e.promedio) == Decimal("150.00")
    assert e.estado == "incompleto"
    assert e.motivo == cp.MOTIVO_SIN_COSTO
    assert e.u_conocidas == 20 and e.u_sin_costo == 10
    assert _q(e.fraccion_sin_costo) == Decimal("0.33")


def test_las_salidas_consumen_en_proporcion() -> None:
    p = proyectar([ev(1, "compra", 10, "100"), ev(2, "compra", 10), ev(3, "salida", 10)])
    e = costo_a(p, d(3))
    assert (e.u_conocidas, e.u_sin_costo) == (Decimal(5), Decimal(5))
    assert e.estado == "incompleto"


def test_incompleto_se_recupera_al_agotarse_el_stock() -> None:
    p = proyectar([
        ev(1, "compra", 10, "100"),
        ev(2, "compra", 10),
        ev(3, "salida", 20),
        ev(4, "compra", 10, "300"),
    ])
    agotado = costo_a(p, d(3))
    assert agotado.estado == "medido"
    assert agotado.motivo is None, "el motivo de unidades sin costo no sobrevive al agotarse"
    e = costo_a(p, d(4))
    assert (e.promedio, e.estado, e.motivo) == (Decimal("300"), "medido", cp.MOTIVO_REINICIO)


def test_ajuste_positivo_entra_sin_costo() -> None:
    """Un ajuste que agrega unidades no trae precio: no se valoriza en silencio."""
    p = proyectar([ev(1, "compra", 10, "100"), ev(2, "entrada", 5, "999")])
    e = costo_a(p, d(2))
    assert e.promedio == Decimal("100")
    assert e.estado == "incompleto" and e.u_sin_costo == 5


def test_todo_sin_costo_no_muestra_un_promedio_viejo() -> None:
    p = proyectar([ev(1, "compra", 10, "100"), ev(2, "salida", 10), ev(3, "compra", 5)])
    e = costo_a(p, d(3))
    assert e.promedio is None and e.estado == "sin_medicion"


# ── reinicio contra no reconstruible ────────────────────────────────────────


def test_stock_negativo_reinicia_el_promedio_sin_perdonar_unidades() -> None:
    """Se vendieron 5 de más: la compra de 10 a $200 las cubre y quedan 5, no 10.
    La compra siguiente promedia sobre esas 5: (5×200 + 10×100) / 15."""
    p = proyectar([
        ev(1, "compra", 10, "100"), ev(2, "salida", 15),
        ev(3, "compra", 10, "200"), ev(4, "compra", 10, "100"),
    ])
    assert costo_a(p, d(2)).stock == -5
    e = costo_a(p, d(3))
    assert (e.promedio, e.u_conocidas, e.motivo) == (
        Decimal("200"), Decimal(5), cp.MOTIVO_REINICIO
    )
    assert _q(costo_a(p, d(4)).promedio) == Decimal("133.33")


def test_una_compra_que_no_alcanza_a_cubrir_el_faltante() -> None:
    p = proyectar([ev(1, "compra", 10, "100"), ev(2, "salida", 30), ev(3, "compra", 5, "200")])
    e = costo_a(p, d(3))
    assert e.stock == -15 and e.promedio == Decimal("200")


def test_costo_cero_no_es_un_costo() -> None:
    p = proyectar([ev(1, "compra", 10, "100"), ev(2, "compra", 10, "0")])
    e = costo_a(p, d(2))
    assert e.promedio == Decimal("100")
    assert e.estado == "incompleto" and e.u_sin_costo == 10


def test_el_consumo_proporcional_no_deja_residuos() -> None:
    """1 + 5 sin costo, salidas de 2 y 4: el stock queda en 0 exacto, no en
    1E-28, así que vuelve a «medido» y la compra siguiente reinicia."""
    p = proyectar([
        ev(1, "compra", 1, "100"), ev(2, "compra", 5),
        ev(3, "salida", 2), ev(4, "salida", 4), ev(5, "compra", 10, "300"),
    ])
    agotado = costo_a(p, d(4))
    assert (agotado.u_conocidas, agotado.u_sin_costo) == (Decimal(0), Decimal(0))
    assert agotado.estado == "medido" and agotado.motivo is None
    e = costo_a(p, d(5))
    assert (e.promedio, e.estado, e.motivo) == (Decimal("300"), "medido", cp.MOTIVO_REINICIO)


def test_salida_desde_incompleto_con_exceso() -> None:
    p = proyectar([ev(1, "compra", 4, "100"), ev(2, "compra", 4), ev(3, "salida", 10)])
    e = costo_a(p, d(3))
    assert (e.u_conocidas, e.u_sin_costo, e.estado) == (Decimal(-2), Decimal(0), "medido")


def test_entrada_con_stock_cero() -> None:
    """Una reconciliación que repone unidades sin stock previo: todo sin costo."""
    p = proyectar([ev(1, "compra", 5, "100"), ev(2, "salida", 5), ev(3, "entrada", 4)])
    e = costo_a(p, d(3))
    assert (e.promedio, e.estado, e.motivo) == (None, "sin_medicion", cp.MOTIVO_SIN_COSTO)


def test_salida_sobre_stock_todo_sin_costo_conserva_el_motivo() -> None:
    p = proyectar([ev(1, "compra", 6), ev(2, "salida", 2)])
    assert costo_a(p, d(2)).motivo == cp.MOTIVO_SIN_COSTO


def test_ledger_complejo_no_se_recupera_con_una_compra() -> None:
    p = proyectar([
        ev(1, "compra", 10, "100"),
        ev(2, "complejo", 3),
        ev(3, "compra", 10, "200"),
        ev(4, "ancla", 12, "180"),
        ev(5, "compra", 12, "220"),
    ])
    for dia in (2, 3):
        e = costo_a(p, d(dia))
        assert (e.promedio, e.estado, e.motivo) == (
            None, "sin_medicion", cp.MOTIVO_NO_RECONSTRUIBLE
        )
    assert costo_a(p, d(4)).promedio == Decimal("180")
    assert costo_a(p, d(5)).promedio == Decimal("200")


def test_stock_fuera_del_ledger_espera_un_ancla() -> None:
    eventos = [ev(1, "compra", 10, "100"), ev(2, "ancla", 4, "90"), ev(3, "compra", 4, "110")]
    p = proyectar(eventos, stock_fuera_del_ledger=True)
    assert costo_a(p, d(1)).motivo == cp.MOTIVO_FUERA_DEL_LEDGER
    assert costo_a(p, d(1)).promedio is None
    assert costo_a(p, d(3)).promedio == Decimal("100")


# ── apertura y catálogo reparado ────────────────────────────────────────────


def test_la_apertura_cuenta_en_su_fecha() -> None:
    p = proyectar([
        ev(1, "compra", 10, "100"),
        ev(2, "salida", 10),
        ev(5, "apertura", 10, "80"),
    ])
    assert costo_a(p, d(4)).promedio == Decimal("100")
    assert costo_a(p, d(5)).promedio == Decimal("80")


def test_apertura_y_compra_del_mismo_dia_se_promedian() -> None:
    p = proyectar([ev(1, "compra", 10, "200", hora=9), ev(1, "apertura", 10, "100", hora=18)])
    # La apertura va primero aunque se haya registrado más tarde en el día.
    assert [e.clase for e, _ in p] == ["apertura", "compra"]
    assert costo_a(p, d(1)).promedio == Decimal("150")


@pytest.mark.parametrize(
    ("movement_type", "source_type", "qty", "reparado", "clase", "evento"),
    [
        ("purchase", "catalog_initial_stock", 10, True, "apertura", None),
        ("purchase", "catalog_initial_stock", -3, True, "salida", None),
        ("purchase", "catalog_initial_stock", 10, False, "compra", None),
        ("purchase", "purchase_import", 10, False, "compra", None),
        ("purchase", None, 10, False, "compra", None),
        ("adjustment", "catalog_initial_stock", 10, False, "apertura", None),
        ("adjustment", "catalog_initial_stock", -4, False, "salida", None),
        ("adjustment", "reconciliation", 3, False, "entrada", None),
        ("adjustment", "manual_adjustment", -3, False, "salida", None),
        ("adjustment", None, 3, False, "complejo", None),
        ("return", None, 2, False, "complejo", None),
        ("loss", None, -2, False, "salida", None),
        # El stock sale del ledger: toda venta que movió stock es una salida.
        ("sale", None, -2, False, "salida", "sale:6f1c0d1e-0000-0000-0000-000000000001"),
        ("sale", "historical_replay", -2, False, "salida", "sale:6f1c0d1e-0000-0000-0000-2"),
        ("sale", None, -2, False, "salida", "0b6f8a52-1c1e-4f5a-9d0a-1f1f1f1f1f1f"),
        ("sale", None, 2, False, "complejo", None),
        ("purchase", None, 0, False, None, None),
    ],
)
def test_clasificar(
    movement_type: str,
    source_type: str | None,
    qty: int,
    reparado: bool,
    clase: str | None,
    evento: str | None,
) -> None:
    del evento  # documenta el origen del movimiento; no cambia la clase
    assert cp.clasificar(movement_type, source_type, qty, catalogo_reparado=reparado) == clase


def test_los_literales_son_los_del_ledger() -> None:
    assert cp._CATALOGO == origen.SOURCE_CATALOG_INITIAL_STOCK
    assert cp._AJUSTES_CON_PROCEDENCIA == origen.TAGGED_ADJUSTMENT_SOURCE_TYPES


def test_catalogo_reparado_proyectado_es_apertura() -> None:
    """El catálogo que reparó M·R entra como apertura en su fecha: se promedia
    con la compra del mismo día en vez de contar como una compra más."""
    clase = cp.clasificar("purchase", "catalog_initial_stock", 10, catalogo_reparado=True)
    assert clase is not None
    p = proyectar([ev(3, "compra", 10, "200", hora=8), ev(3, clase, 10, "100", hora=20)])
    assert [e.clase for e, _ in p] == ["apertura", "compra"]
    assert costo_a(p, d(3)).promedio == Decimal("150")


def test_aproximado_viaja_en_cada_estado() -> None:
    p = proyectar([ev(1, "compra", 10, "100"), ev(2, "salida", 3)], aproximado=True)
    assert all(e.aproximado for _, e in p)
    assert not any(e.aproximado for _, e in proyectar([ev(1, "compra", 10, "100")]))


def test_fechas_con_y_sin_zona_se_ordenan_juntas() -> None:
    con = ev(1, "compra", 10, "100")
    sin = Evento(con.fecha.replace(tzinfo=None), "compra", 10, Decimal("200"),
                 con.creado.replace(tzinfo=None), uuid.UUID(int=10**9))
    assert len(cp.ordenar([con, sin])) == 2


# ── orden, unidad, checkpoint ───────────────────────────────────────────────


def test_dentro_del_dia_la_compra_cubre_la_venta() -> None:
    """La venta de la mañana y la compra de la tarde: el crédito va primero, así
    que no hay stock negativo ni reinicio."""
    p = proyectar([ev(1, "compra", 10, "100"), ev(2, "salida", 10, hora=9),
                   ev(2, "compra", 10, "200", hora=15), ev(2, "salida", 5, hora=10)])
    e = costo_a(p, d(2))
    assert e.promedio == Decimal("150") and e.motivo is None


def test_desempate_estable_por_creado_e_id() -> None:
    mismo = _BASE
    a = Evento(mismo, "compra", 10, Decimal("100"), mismo, uuid.UUID(int=2))
    b = Evento(mismo, "compra", 10, Decimal("300"), mismo, uuid.UUID(int=1))
    assert [e.id for e in cp.ordenar([a, b])] == [b.id, a.id]


def test_producto_fraccionado_promedia_en_unidades_base() -> None:
    """Por kilo (factor 1000): 1500 g a $800/kg y 500 g a $1200/kg → $900/kg."""
    p = proyectar([ev(1, "compra", 1500, "800"), ev(2, "compra", 500, "1200")])
    prom = costo_a(p, d(2)).promedio
    assert prom == Decimal("900")
    assert importe(prom, 750, 1000) == Decimal("675.00")


def test_una_compra_con_fecha_vieja_cambia_solo_lo_posterior() -> None:
    base = [ev(1, "compra", 10, "100"), ev(10, "compra", 10, "200")]
    sin = proyectar(base)
    con = proyectar([*base, ev(5, "compra", 10, "160")])
    assert costo_a(sin, d(4)) == costo_a(con, d(4))
    assert costo_a(sin, d(10)).promedio != costo_a(con, d(10)).promedio
    assert _q(costo_a(con, d(10)).promedio) == Decimal("153.33")


def test_reconstruir_desde_un_checkpoint_da_lo_mismo() -> None:
    eventos = [
        ev(1, "compra", 10, "100"), ev(2, "salida", 4), ev(3, "compra", 10, "200"),
        ev(4, "compra", 5), ev(5, "salida", 9), ev(6, "compra", 10, "250"),
    ]
    completo = proyectar(eventos)
    checkpoint = cp.registros(completo)[1]  # la compra del día 3
    resto = [e for e in eventos if e.clave() > checkpoint.evento.clave()]
    parcial = proyectar(resto, desde=checkpoint.estado)
    assert parcial[-1][1] == completo[-1][1]


def test_registros_de_compras_y_de_lo_que_cambia_el_estado() -> None:
    """Toda compra, apertura o ancla deja registro; una salida que sólo resta no.
    Una entrada sin costo sí, porque pasa el estado a «incompleto»."""
    p = proyectar([ev(1, "apertura", 5, "90"), ev(2, "compra", 5, "110"),
                   ev(3, "salida", 2), ev(4, "entrada", 1), ev(5, "ancla", 6, "100")])
    assert [r.evento.clase for r in cp.registros(p)] == ["apertura", "compra", "entrada", "ancla"]


def test_un_complejo_despues_de_la_ultima_compra_queda_registrado() -> None:
    """Sin este registro, leer el historial después de la devolución seguiría
    mostrando el promedio «medido» de la compra anterior."""
    p = proyectar([ev(1, "compra", 10, "100"), ev(2, "complejo", 2), ev(3, "salida", 1)])
    regs = cp.registros(p)
    assert [r.evento.clase for r in regs] == ["compra", "complejo"]
    assert (regs[-1].estado.estado, regs[-1].estado.stock_conocido) == ("sin_medicion", False)


def test_la_salida_que_vuelve_a_medido_queda_registrada() -> None:
    p = proyectar([ev(1, "compra", 5, "100"), ev(2, "compra", 5), ev(3, "salida", 10)])
    regs = cp.registros(p)
    assert [r.evento.clase for r in regs] == ["compra", "compra", "salida"]
    assert regs[-1].estado.estado == "medido"


# ── la brecha con inflación ─────────────────────────────────────────────────


def _brecha_con_inflacion(meses_de_stock: Decimal) -> Decimal:
    """5% de inflación mensual: cada mes se compra y se vende lo mismo, con un
    stock que alcanza para ``meses_de_stock``. Brecha = 1 − promedio / última."""
    demanda = 100
    stock = int(meses_de_stock * demanda)
    eventos = [ev(1, "apertura", stock, "100")] if stock else []
    precio = Decimal("100")
    for mes in range(36):
        dia = 1 + mes * 30
        precio = Decimal("100") * Decimal("1.05") ** mes
        eventos.append(ev(dia, "compra", demanda, str(precio.quantize(Decimal("0.01")))))
        eventos.append(ev(dia + 15, "salida", demanda))
    final = proyectar(eventos)[-1][1]
    assert final.promedio is not None
    return 1 - final.promedio / precio.quantize(Decimal("0.01"))


def test_con_inflacion_la_brecha_crece_con_los_meses_de_stock() -> None:
    alta_rotacion = _brecha_con_inflacion(Decimal("0.5"))
    baja_rotacion = _brecha_con_inflacion(Decimal("12"))
    assert Decimal("0") < alta_rotacion < Decimal("0.03")
    assert baja_rotacion > Decimal("0.15")
