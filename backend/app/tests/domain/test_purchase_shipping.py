"""F-H6.b — el envío se cobra una vez por comprobante, o no se cobra.

El caso que motiva la fase: un remito de diez artículos con «Envío 2.000» repetido
en las diez filas. Importarlo fila por fila da $20.000 de logística sobre un flete
de $2.000.
"""

from __future__ import annotations

from decimal import Decimal

from app.domain.purchase_shipping import (
    SIN_COMPROBANTE_UNA_POR_FILA,
    SIN_COMPROBANTE_UNA_POR_HOJA,
    ShippingLine,
    plan_shipping_charges,
)

_PROV = "distribuidora sur"
_COMP = "a-0001-00012345"


def _linea(
    row_index: int,
    amount: str,
    *,
    supplier: str = _PROV,
    invoice: str = _COMP,
) -> ShippingLine:
    return ShippingLine(
        row_index=row_index,
        supplier=supplier,
        invoice=invoice,
        amount=Decimal(amount),
    )


class TestUnEnvioPorComprobante:
    def test_la_misma_cifra_en_diez_filas_se_cobra_una_vez(self) -> None:
        plan = plan_shipping_charges([_linea(i, "2000") for i in range(10)])

        assert len(plan.charges) == 1
        assert plan.total == Decimal("2000")
        # Se conserva en cuántas filas figuraba: es lo que hace explicable el
        # aviso ("venía repetido en 10 líneas del mismo comprobante").
        assert plan.charges[0].repetido_en == 10

    def test_dos_comprobantes_son_dos_envios(self) -> None:
        plan = plan_shipping_charges(
            [
                _linea(0, "2000", invoice="a-0001-00012345"),
                _linea(1, "2000", invoice="a-0001-00012345"),
                _linea(2, "3500", invoice="a-0001-00099999"),
            ]
        )

        assert plan.total == Decimal("5500")
        assert [c.invoice for c in plan.charges] == [
            "a-0001-00012345",
            "a-0001-00099999",
        ]

    def test_el_mismo_numero_de_dos_proveedores_no_se_mezcla(self) -> None:
        """Dos proveedores pueden emitir el mismo número de comprobante."""
        plan = plan_shipping_charges(
            [
                _linea(0, "2000", supplier="distribuidora sur"),
                _linea(1, "2000", supplier="mayorista norte"),
            ]
        )

        assert len(plan.charges) == 2
        assert plan.total == Decimal("4000")

    def test_cifras_distintas_en_el_mismo_comprobante_se_cobran_y_se_avisan(self) -> None:
        """Puede ser flete + seguro, o una planilla con el total y el prorrateo
        mezclados. Véktor cobra las dos y lo dice; elegir sería inventar."""
        plan = plan_shipping_charges([_linea(0, "2000"), _linea(1, "500")])

        assert plan.total == Decimal("2500")
        assert plan.cifras_distintas == [(_PROV, _COMP)]

    def test_una_sola_cifra_no_se_reporta_como_rara(self) -> None:
        plan = plan_shipping_charges([_linea(0, "2000"), _linea(1, "2000")])
        assert plan.cifras_distintas == []


class TestSinIdentidadNoSeCobra:
    """Regla no-invention: sin comprobante, un 2.000 repetido diez veces es
    indistinguible de diez envíos de 2.000."""

    def test_sin_numero_de_comprobante_no_genera_gasto(self) -> None:
        plan = plan_shipping_charges([_linea(i, "2000", invoice="") for i in range(10)])

        assert plan.charges == []
        assert plan.total == Decimal("0")
        assert plan.sin_identidad == list(range(10))

    def test_sin_proveedor_tampoco(self) -> None:
        plan = plan_shipping_charges([_linea(0, "2000", supplier="")])

        assert plan.charges == []
        assert plan.sin_identidad == [0]

    def test_lo_agrupable_entra_aunque_otra_fila_no_lo_sea(self) -> None:
        """Una fila sin identidad no puede anular las que sí la tienen."""
        plan = plan_shipping_charges(
            [_linea(0, "2000"), _linea(1, "2000"), _linea(2, "900", invoice="")]
        )

        assert plan.total == Decimal("2000")
        assert plan.sin_identidad == [2]


class TestLoQueNoEsUnEnvio:
    def test_cero_y_negativo_se_ignoran_sin_reportar(self) -> None:
        """La mayoría de las filas de un libro no traen flete: una celda vacía no
        es un problema que haya que contarle al usuario."""
        plan = plan_shipping_charges(
            [_linea(0, "0"), _linea(1, "-500"), _linea(2, "2000")]
        )

        assert plan.total == Decimal("2000")
        assert plan.sin_identidad == []

    def test_sin_filas_no_hay_nada_que_cobrar(self) -> None:
        plan = plan_shipping_charges([])
        assert plan.charges == []
        assert plan.total == Decimal("0")


class TestDeterminismo:
    def test_el_orden_sigue_al_archivo(self) -> None:
        """Dos corridas sobre el mismo archivo tienen que crear los mismos gastos
        en el mismo orden: si no, un re-import genera duplicados distintos."""
        lineas = [
            _linea(0, "500", invoice="c"),
            _linea(1, "2000", invoice="a"),
            _linea(2, "500", invoice="c"),
            _linea(3, "700", invoice="b"),
        ]

        primero = plan_shipping_charges(lineas)
        segundo = plan_shipping_charges(lineas)

        assert [c.invoice for c in primero.charges] == ["c", "a", "b"]
        assert [c.invoice for c in segundo.charges] == [c.invoice for c in primero.charges]


class TestLasDosSalidasSinComprobante:
    """El usuario declara por hoja qué es ese envío sin comprobante.

    Véktor no puede deducirlo, pero quien armó la planilla sí sabe. Lo que nunca
    pasa es que se elija por él: sin decisión no se cobra (los tests de arriba).
    """

    def test_una_por_hoja_colapsa_la_repeticion(self) -> None:
        plan = plan_shipping_charges(
            [_linea(i, "2000", invoice="") for i in range(10)],
            sin_comprobante=SIN_COMPROBANTE_UNA_POR_HOJA,
        )

        assert len(plan.charges) == 1
        assert plan.total == Decimal("2000")
        assert plan.charges[0].repetido_en == 10
        assert plan.sin_identidad == []

    def test_una_por_hoja_con_dos_cifras_son_dos_cargos(self) -> None:
        """Decisión del usuario: una cifra, un cargo. Sumar convertiría una
        repetición en un total inflado; exigir una sola cifra dejaría sin salida
        a un archivo con dos fletes legítimos."""
        lineas = [_linea(i, "2000", invoice="") for i in range(5)]
        lineas += [_linea(5 + i, "3500", invoice="") for i in range(3)]

        plan = plan_shipping_charges(lineas, sin_comprobante=SIN_COMPROBANTE_UNA_POR_HOJA)

        assert sorted(c.amount for c in plan.charges) == [Decimal("2000"), Decimal("3500")]
        assert plan.total == Decimal("5500")

    def test_una_por_hoja_no_reporta_las_cifras_como_anomalia(self) -> None:
        """Dentro de un comprobante dos cifras son raras y se avisan; en la hoja
        entera son lo esperado."""
        lineas = [_linea(0, "2000", invoice=""), _linea(1, "3500", invoice="")]
        plan = plan_shipping_charges(lineas, sin_comprobante=SIN_COMPROBANTE_UNA_POR_HOJA)

        assert plan.cifras_distintas == []

    def test_una_por_fila_no_colapsa_nada(self) -> None:
        """Es exactamente lo que el usuario declaró: diez fletes de 2.000."""
        plan = plan_shipping_charges(
            [_linea(i, "2000", invoice="") for i in range(10)],
            sin_comprobante=SIN_COMPROBANTE_UNA_POR_FILA,
        )

        assert len(plan.charges) == 10
        assert plan.total == Decimal("20000")
        assert all(c.repetido_en == 1 for c in plan.charges)

    def test_la_decision_no_toca_las_filas_que_si_tienen_comprobante(self) -> None:
        """Una hoja puede traer las dos cosas: lo identificable se agrupa por su
        comprobante y la decisión sólo alcanza al resto."""
        lineas = [
            _linea(0, "2000"),
            _linea(1, "2000"),
            _linea(2, "900", invoice=""),
            _linea(3, "900", invoice=""),
        ]

        plan = plan_shipping_charges(lineas, sin_comprobante=SIN_COMPROBANTE_UNA_POR_FILA)

        # El comprobante colapsa; las sueltas no, porque así se declaró.
        assert plan.total == Decimal("2000") + Decimal("1800")
        assert len(plan.charges) == 3

    def test_sin_proveedor_tambien_entra_por_hoja(self) -> None:
        """La agrupación de `una_por_hoja` es sólo por importe: el proveedor puede
        faltar y el usuario ya declaró que esas filas comparten la operación."""
        plan = plan_shipping_charges(
            [
                _linea(0, "2000", supplier="", invoice=""),
                _linea(1, "2000", supplier="", invoice=""),
            ],
            sin_comprobante=SIN_COMPROBANTE_UNA_POR_HOJA,
        )

        assert len(plan.charges) == 1
        assert plan.total == Decimal("2000")
