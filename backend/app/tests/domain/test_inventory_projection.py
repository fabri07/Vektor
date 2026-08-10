"""F-H3.b: qué le PASARÍA al stock si se aplicara la historia de un archivo.

La cuenta que acá se prueba es la misma que va a aplicar el replay (F-H3.d) y la
misma que va a mostrar el preview (F-H3.c). Tenerla en un solo lugar es lo que
evita que lo que se muestra y lo que se aplica se separen; tenerla probada acá,
sin sesión ni ORM, es lo que permite probar los bordes sin montar un import.
"""

from __future__ import annotations

import uuid
from datetime import date

from app.domain.inventory_projection import (
    ProductImpact,
    ProductProjection,
    project_import_impact,
)


def _proyeccion(
    *,
    saldo_previo: int = 0,
    saldo_declarado: int | None = None,
    nombre: str = "Vela aromática 200g",
) -> ProductProjection:
    return ProductProjection(
        product_id=uuid.uuid4(),
        product_name=nombre,
        saldo_previo=saldo_previo,
        saldo_declarado=saldo_declarado,
    )


def _impacto_de(proyeccion: ProductProjection) -> ProductImpact:
    return project_import_impact({proyeccion.product_id: proyeccion}).productos[0]


class TestApertura:
    def test_el_catalogo_declara_un_absoluto_y_pisa_el_saldo_previo(self) -> None:
        """Un catálogo es una foto, no un movimiento.

        Sumarlo al saldo previo leería "tengo 10 en góndola" como "entraron 10",
        que sobre un producto que ya existía inventa una compra.
        """
        p = _proyeccion(saldo_previo=7, saldo_declarado=10)
        p.agregar_venta(date(2024, 3, 10), 2)
        impacto = _impacto_de(p)
        assert impacto.saldo_inicial == 10
        assert impacto.saldo_final == 8

    def test_sin_catalogo_arranca_del_saldo_previo(self) -> None:
        p = _proyeccion(saldo_previo=7)
        p.agregar_venta(date(2024, 3, 10), 2)
        assert _impacto_de(p).saldo_inicial == 7


class TestReplayPorFecha:
    def test_apertura_mas_compra_menos_venta(self) -> None:
        """El caso de la tabla del plan: 10 + 5 − 4 = 11."""
        p = _proyeccion(saldo_declarado=10)
        p.agregar_compra(date(2024, 3, 5), 5)
        p.agregar_venta(date(2024, 3, 10), 4)
        impacto = _impacto_de(p)
        assert impacto.saldo_final == 11
        assert impacto.compradas == 5
        assert impacto.vendidas == 4

    def test_el_orden_de_carga_no_cambia_el_resultado(self) -> None:
        """Se ordena por FECHA, no por el orden en que se agregaron las filas."""
        temprano = _proyeccion(saldo_declarado=10)
        temprano.agregar_compra(date(2024, 3, 5), 5)
        temprano.agregar_venta(date(2024, 3, 10), 4)

        tarde = _proyeccion(saldo_declarado=10)
        tarde.agregar_venta(date(2024, 3, 10), 4)
        tarde.agregar_compra(date(2024, 3, 5), 5)

        assert _impacto_de(temprano).saldo_final == _impacto_de(tarde).saldo_final

    def test_a_igual_fecha_la_compra_entra_antes_que_la_venta(self) -> None:
        """Sin esto, comprar y vender el mismo día daría negativo intermedio falso."""
        p = _proyeccion(saldo_declarado=0)
        p.agregar_venta(date(2024, 3, 5), 4)
        p.agregar_compra(date(2024, 3, 5), 5)
        impacto = _impacto_de(p)
        assert impacto.saldo_final == 1
        assert impacto.primer_negativo_en is None


class TestNegativos:
    def test_una_venta_sin_compra_previa_marca_la_fecha_del_pozo(self) -> None:
        p = _proyeccion(saldo_declarado=0)
        p.agregar_venta(date(2024, 3, 10), 4)
        p.agregar_compra(date(2024, 3, 20), 10)
        impacto = _impacto_de(p)
        # Termina bien, pero pasó por abajo de cero: falta la compra vieja.
        assert impacto.saldo_final == 6
        assert impacto.toca_negativo
        assert impacto.primer_negativo_en == date(2024, 3, 10)
        assert impacto.minimo == -4

    def test_terminar_negativo_y_tocar_negativo_no_son_lo_mismo(self) -> None:
        """Un final sano con un pozo en el medio no es un inventario mal cargado."""
        p = _proyeccion(saldo_declarado=0)
        p.agregar_venta(date(2024, 3, 10), 4)
        p.agregar_compra(date(2024, 3, 20), 10)
        impacto = _impacto_de(p)
        assert impacto.toca_negativo
        assert not impacto.queda_negativo


class TestResumenDelArchivo:
    def test_los_productos_sin_movimiento_no_ensucian_el_preview(self) -> None:
        """El archivo los nombró pero no declara nada sobre ellos."""
        quieto = _proyeccion(saldo_previo=5, nombre="Sahumerio")
        movido = _proyeccion(saldo_declarado=3, nombre="Vela")
        movido.agregar_venta(date(2024, 3, 10), 1)

        impacto = project_import_impact(
            {quieto.product_id: quieto, movido.product_id: movido}
        )
        assert [p.product_name for p in impacto.productos] == ["Vela"]

    def test_los_negativos_van_primero_y_el_orden_es_estable(self) -> None:
        """Un orden por dict mostraría el mismo archivo distinto entre corridas."""
        sano = _proyeccion(saldo_declarado=100, nombre="Alfajor")
        sano.agregar_venta(date(2024, 3, 10), 1)
        roto = _proyeccion(saldo_declarado=0, nombre="Zeta")
        roto.agregar_venta(date(2024, 3, 10), 5)

        impacto = project_import_impact({sano.product_id: sano, roto.product_id: roto})
        assert [p.product_name for p in impacto.productos] == ["Zeta", "Alfajor"]
        assert len(impacto.negativos) == 1

    def test_totales_del_archivo(self) -> None:
        uno = _proyeccion(saldo_declarado=10, nombre="A")
        uno.agregar_compra(date(2024, 3, 1), 4)
        uno.agregar_venta(date(2024, 3, 2), 1)
        dos = _proyeccion(saldo_declarado=10, nombre="B")
        dos.agregar_venta(date(2024, 3, 2), 2)

        impacto = project_import_impact({uno.product_id: uno, dos.product_id: dos})
        assert impacto.unidades_compradas == 4
        assert impacto.unidades_vendidas == 3


class TestCantidadesInvalidas:
    def test_cantidad_cero_o_negativa_no_genera_evento(self) -> None:
        """Una cantidad que no se puede interpretar no inventa un movimiento."""
        p = _proyeccion(saldo_declarado=10)
        p.agregar_venta(date(2024, 3, 10), 0)
        p.agregar_compra(date(2024, 3, 10), -5)
        assert project_import_impact({p.product_id: p}).productos == []
