"""F-H6.d — cuáles son las líneas de una compra y cuánto costo compartido tienen.

Es la pregunta previa al reparto. Repartir mal el flete de un comprobante entre
líneas de otro le carga a una compra un costo que no pagó, y eso no se ve en
ningún total: los dos cierran igual, cambia el costo por producto.
"""

from __future__ import annotations

from decimal import Decimal

from app.domain.purchase_group import (
    MOTIVO_CIFRAS_DISTINTAS,
    MOTIVO_SIN_ENVIO_COMPARTIDO,
    MOTIVO_SIN_IDENTIDAD,
    GroupLine,
    build_purchase_groups,
)
from app.domain.purchase_shipping import (
    SIN_COMPROBANTE_UNA_POR_FILA,
    SIN_COMPROBANTE_UNA_POR_HOJA,
)

_PROV = "distribuidora sur"
_COMP = "a-0001-00012345"


def _linea(
    row_index: int,
    subtotal: str,
    *,
    shipping: str = "0",
    supplier: str = _PROV,
    invoice: str = _COMP,
) -> GroupLine:
    return GroupLine(
        row_index=row_index,
        supplier=supplier,
        invoice=invoice,
        subtotal=Decimal(subtotal),
        shipping=Decimal(shipping),
    )


class TestLasLineasDeUnaCompra:
    def test_las_filas_del_mismo_comprobante_son_un_grupo(self) -> None:
        plan = build_purchase_groups(
            [_linea(0, "100"), _linea(1, "200"), _linea(2, "300")]
        )

        assert len(plan.groups) == 1
        assert plan.groups[0].row_indexes == [0, 1, 2]
        assert plan.groups[0].subtotal == Decimal("600")

    def test_dos_proveedores_con_la_misma_factura_no_se_mezclan(self) -> None:
        # El número solo no alcanza: dos proveedores emiten «Factura 0001» el
        # mismo mes. Agruparlos repartiría el flete de uno sobre las líneas del
        # otro. Es la mutación que este test protege.
        plan = build_purchase_groups(
            [
                _linea(0, "100", shipping="50", supplier="sur"),
                _linea(1, "100", shipping="80", supplier="norte"),
            ]
        )

        assert len(plan.groups) == 2
        assert [g.shared_shipping for g in plan.groups] == [
            Decimal("50"),
            Decimal("80"),
        ]

    def test_una_fila_sin_envio_igual_pertenece_al_grupo(self) -> None:
        # El flete del comprobante se reparte entre TODAS sus líneas, no sólo
        # entre las que repitieron la cifra. Si el grupo se armara desde la
        # columna de envío, las filas sin flete quedarían afuera del reparto y
        # el costo se concentraría en las que sí lo traían.
        plan = build_purchase_groups(
            [_linea(0, "100", shipping="30"), _linea(1, "100")]
        )

        assert plan.groups[0].row_indexes == [0, 1]
        assert plan.groups[0].shared_shipping == Decimal("30")

    def test_el_orden_sigue_al_archivo(self) -> None:
        # De este orden dependen el centavo de redondeo del reparto y la traza.
        plan = build_purchase_groups(
            [
                _linea(0, "100", invoice="b-0002"),
                _linea(1, "100", invoice="a-0001"),
                _linea(2, "100", invoice="b-0002"),
            ]
        )

        assert [g.key for g in plan.groups] == [
            (_PROV, "b-0002"),
            (_PROV, "a-0001"),
        ]


class TestCuandoSePuedeRepartir:
    def test_la_cifra_repetida_se_colapsa_y_el_grupo_reparte(self) -> None:
        plan = build_purchase_groups(
            [_linea(i, "100", shipping="2000") for i in range(10)]
        )

        assert plan.groups[0].distribuible is True
        # Diez filas de 2.000 son un flete de 2.000, no de 20.000.
        assert plan.groups[0].shared_shipping == Decimal("2000")
        assert plan.groups[0].motivo_no_distribuible is None

    def test_sin_comprobante_no_reparte_y_lo_dice(self) -> None:
        plan = build_purchase_groups(
            [_linea(0, "100", shipping="500", invoice="")]
        )

        assert plan.groups[0].key is None
        assert plan.groups[0].distribuible is False
        assert plan.groups[0].motivo_no_distribuible == MOTIVO_SIN_IDENTIDAD
        # Y no se arrastra una cifra que no se va a usar.
        assert plan.groups[0].shared_shipping == Decimal("0")

    def test_sin_proveedor_tampoco(self) -> None:
        plan = build_purchase_groups(
            [_linea(0, "100", shipping="500", supplier="")]
        )

        assert plan.groups[0].motivo_no_distribuible == MOTIVO_SIN_IDENTIDAD

    def test_dos_cifras_distintas_en_el_mismo_comprobante_no_reparten(self) -> None:
        # Puede ser un flete y un seguro, o el total en una fila y el prorrateo
        # en las otras. Sumarlas y repartir la suma sería elegir por el usuario.
        plan = build_purchase_groups(
            [_linea(0, "100", shipping="500"), _linea(1, "100", shipping="300")]
        )

        assert plan.groups[0].distribuible is False
        assert plan.groups[0].motivo_no_distribuible == MOTIVO_CIFRAS_DISTINTAS
        assert plan.groups[0].shared_shipping == Decimal("0")

    def test_un_comprobante_sin_columna_de_envio_no_tiene_nada_que_repartir(
        self,
    ) -> None:
        # Control: sin costo compartido el grupo existe (hay que poder mostrarlo)
        # pero no es distribuible, y el motivo lo distingue de una falla.
        plan = build_purchase_groups([_linea(0, "100"), _linea(1, "200")])

        assert plan.groups[0].distribuible is False
        assert plan.groups[0].motivo_no_distribuible == MOTIVO_SIN_ENVIO_COMPARTIDO


class TestLaDecisionDelUsuarioSobreFilasSinComprobante:
    def test_una_por_hoja_habilita_el_reparto(self) -> None:
        # El usuario declaró que la hoja entera es una operación: ésa es la
        # confirmación explícita que el reparto necesita.
        plan = build_purchase_groups(
            [_linea(i, "100", shipping="900", invoice="") for i in range(3)],
            sin_comprobante=SIN_COMPROBANTE_UNA_POR_HOJA,
        )

        assert len(plan.groups) == 1
        assert plan.groups[0].distribuible is True
        assert plan.groups[0].shared_shipping == Decimal("900")

    def test_una_por_fila_no_es_un_costo_compartido(self) -> None:
        # «Cada fila trae su propio flete» es lo contrario de «hay algo que
        # repartir»: no queda nada compartido.
        plan = build_purchase_groups(
            [_linea(i, "100", shipping="900", invoice="") for i in range(3)],
            sin_comprobante=SIN_COMPROBANTE_UNA_POR_FILA,
        )

        assert plan.groups[0].distribuible is False
        assert plan.groups[0].motivo_no_distribuible == MOTIVO_SIN_IDENTIDAD


class TestIndicePorFila:
    def test_by_row_apunta_cada_fila_a_su_grupo(self) -> None:
        plan = build_purchase_groups(
            [
                _linea(0, "100", invoice="a-0001"),
                _linea(1, "100", invoice="b-0002"),
                _linea(2, "100", invoice="a-0001"),
            ]
        )
        por_fila = plan.by_row()

        assert por_fila[0] is por_fila[2]
        assert por_fila[1] is not por_fila[0]
