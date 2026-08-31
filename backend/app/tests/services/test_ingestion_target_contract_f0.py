"""F-0: el contrato de un ``target_field``, en un solo lugar.

Hoy la pregunta "¿qué es este target?" se contesta con un ``startswith`` suelto
en seis archivos distintos, y cada uno contesta un poco distinto: el importador
distingue ``custom_field:`` para rutearlo a otro bucket, el confirm para
excluirlo de los requeridos, ``column_risk`` para decidir si es un campo real.
Seis copias de la misma regla son seis oportunidades de que una se quede vieja.

Estos tests fijan la gramática ANTES de que F-D agregue una tercera forma de
target (``{entidad}:{campo}``), que es exactamente cuando esas seis copias
empezarían a divergir.
"""

from __future__ import annotations

import pytest

from app.application.services.column_mapping_service import (
    CANONICAL_FIELDS,
    CROSS_ENTITY_FORBIDDEN_FIELDS,
    CROSS_ENTITY_PREFIXES,
    CROSS_ENTITY_TARGETS,
    parse_target,
)
from app.application.services.ingestion_import_service import _resolve_target_cols


class TestGramaticaDelTarget:
    """``parse_target`` es la ÚNICA fuente de verdad sobre qué es un target."""

    @pytest.mark.parametrize("vacio", [None, "", "   "])
    def test_target_vacio_no_es_un_destino(self, vacio: str | None) -> None:
        assert parse_target(vacio).kind == "none"

    def test_ignorar_es_una_decision_explicita_no_un_vacio(self) -> None:
        """``ignore`` y "sin mapear" NO son lo mismo.

        Uno es el usuario diciendo "esta columna no va"; el otro es una columna
        que todavía nadie miró. Colapsarlos deja que una columna sin revisar se
        descarte como si alguien lo hubiera decidido.
        """
        assert parse_target("ignore").kind == "ignore"

    def test_campo_canonico(self) -> None:
        parsed = parse_target("amount")
        assert parsed.kind == "canonical"
        assert parsed.entity is None
        assert parsed.field == "amount"

    def test_campo_propio_devuelve_la_clave_sin_prefijo(self) -> None:
        parsed = parse_target("custom_field:hora_de_venta")
        assert parsed.kind == "custom"
        assert parsed.entity is None
        assert parsed.field == "hora_de_venta"

    @pytest.mark.parametrize(
        "variante", ["custom_field:obs", "custom_field: obs", "custom_field:obs "]
    )
    def test_la_clave_propia_se_normaliza_como_el_resto(self, variante: str) -> None:
        """Sin esto, el espacio decidía si dos columnas colisionaban o no.

        ``"custom_field:obs "`` ya colapsaba a ``obs`` porque el strip de afuera
        lo alcanzaba, pero ``"custom_field: obs"`` quedaba como clave distinta:
        dos columnas compartían campo y la colisión no se detectaba.
        """
        assert parse_target(variante).field == "obs"

    def test_target_cruzado_separa_entidad_y_campo(self) -> None:
        parsed = parse_target("customer:dni")
        assert parsed.kind == "cross"
        assert parsed.entity == "customer"
        assert parsed.field == "dni"

    def test_custom_field_no_se_confunde_con_una_entidad(self) -> None:
        """``custom_field`` usa el mismo separador que un cruzado.

        Si el parser partiera por el primer ``:`` sin mirar qué hay a la
        izquierda, todos los campos propios pasarían a ser targets cruzados
        contra una entidad inexistente llamada "custom_field".
        """
        assert parse_target("custom_field:marca").kind == "custom"

    def test_prefijo_desconocido_no_inventa_una_entidad(self) -> None:
        """``foo:bar`` no es un cruzado: ``foo`` no es una entidad.

        Tratarlo como cruzado lo haría pasar por la validación de allowlist con
        una entidad que no existe. Queda como canónico desconocido, que es lo
        que el confirm ya sabe rechazar.
        """
        parsed = parse_target("foo:bar")
        assert parsed.kind == "canonical"
        assert parsed.entity is None

    def test_la_gramatica_admite_cruzado_mas_campo_propio(self) -> None:
        """``product:custom_field:marca`` parsea, aunque la allowlist no lo habilite.

        Se fija ahora para que la extensión futura no obligue a cambiar la
        gramática (y con ella los seis consumidores).
        """
        parsed = parse_target("product:custom_field:marca")
        assert parsed.kind == "cross"
        assert parsed.entity == "product"
        assert parsed.field == "custom_field:marca"


class TestAllowlistCruzada:
    """La matriz de rutas permitidas es explícita, no un producto cartesiano."""

    def test_ninguna_hoja_puede_escribir_un_campo_prohibido(self) -> None:
        """El invariante que ya costó un incidente de inventario.

        ``stock_units`` es la proyección de un ledger de movimientos: se toca
        por ``+= qty`` con su movimiento, nunca por ``setattr`` desde una
        columna. Una venta que lo escriba rompe la conciliación stock↔movimientos
        y encima no tiene un ``stock_treatment`` que le dé sentido contable.

        Se itera la CONSTANTE y no el literal: así el guard de campos prohibidos
        es de verdad un segundo cinturón —agregarle un campo lo cubre acá
        automáticamente— y no una lista decorativa que nadie lee.
        """
        assert CROSS_ENTITY_FORBIDDEN_FIELDS, "la lista de prohibidos no puede estar vacía"
        for origen, destinos in CROSS_ENTITY_TARGETS.items():
            for destino, campos in destinos.items():
                for prohibido in CROSS_ENTITY_FORBIDDEN_FIELDS:
                    assert prohibido not in campos, (
                        f"{origen} → {destino} habilita {prohibido}"
                    )

    def test_ningun_cruzado_duplica_una_referencia_canonica(self) -> None:
        """La REGLA, no una instancia: si la hoja ya puede expresar el campo, fuera.

        Los campos de referencia siguen la convención ``{entidad}_{campo}``
        (``customer_dni``, ``supplier_name``, ``product_name``). Cuando existen,
        la columna ya tiene un camino canónico que pasa por el resolvedor de
        referencias, cuya creación de maestros gobierna
        ``*_REFERENCE_CREATION_MODE``. Ofrecer ADEMÁS una ruta cruzada que
        escriba el maestro directo deja dos caminos para la misma columna con
        semánticas distintas y sin nadie que arbitre cuál gana.

        Este test es el que hace que la exclusión de ``sale → product:name`` sea
        un principio y no un caso especial: antes estaba escrita a mano para
        productos mientras clientes y proveedores la violaban.
        """
        for origen, destinos in CROSS_ENTITY_TARGETS.items():
            canonicos_origen = CANONICAL_FIELDS.get(origen, {})
            for destino, campos in destinos.items():
                for campo in campos:
                    referencia = f"{destino}_{campo}"
                    assert referencia not in canonicos_origen, (
                        f"{origen} → {destino}:{campo} duplica el campo canónico "
                        f"«{referencia}» que la hoja de {origen} ya tiene"
                    )

    def test_todo_campo_permitido_existe_en_su_entidad_destino(self) -> None:
        """Una allowlist que nombra un campo inexistente es una promesa vacía.

        El usuario lo elegiría en el select y el importador no tendría dónde
        escribirlo.
        """
        for origen, destinos in CROSS_ENTITY_TARGETS.items():
            for destino, campos in destinos.items():
                conocidos = CANONICAL_FIELDS.get(destino, {})
                for campo in campos:
                    assert campo in conocidos, (
                        f"{origen} → {destino}:{campo} no existe en CANONICAL_FIELDS"
                    )

    def test_una_venta_no_renombra_un_producto(self) -> None:
        """Nombre, SKU y código de barras de una venta son IDENTIDAD, no escritura.

        Ya existen como campos canónicos de venta (``product_name``, ``sku``,
        ``barcode``) y son lo que ``_resolve_product`` usa para vincular. Si
        además fueran ruta de escritura cruzada, una fila de venta podría
        renombrar un producto del catálogo — modificar un maestro desde una
        transacción.
        """
        desde_venta = CROSS_ENTITY_TARGETS.get("sale", {}).get("product", frozenset())
        for identidad in ("name", "sku", "barcode"):
            assert identidad not in desde_venta

    def test_un_catalogo_de_productos_puede_declarar_proveedor_solo_si_el_usuario_lo_confirma(
        self,
    ) -> None:
        """Bloque 2 (Tienda → proveedor): la ruta existe, pero nunca es automática.

        En un catálogo, la columna "Tienda"/"Proveedor" sigue siendo MARCA por
        DEFECTO (`Product.custom_fields["marca"]`) — habilitar la ruta
        ``product → supplier:name`` en el allowlist no cambia ese default: solo
        permite que el USUARIO mapee esa columna a ``supplier:name`` a propósito.
        Aun mapeada, `ingestion_import_service._add_product` la aplica solo si
        el tenant está en `PRODUCT_SUPPLIER_LINKS_ROLLOUT_TENANT_IDS` (compuerta
        de rollout, ver `test_product_supplier_links_bloque2.py`), y el vínculo
        se guarda en `product_supplier_links` — NUNCA pisa `custom_fields["marca"]`
        ni recrea las filas marca-como-proveedor que hubo que limpiar con
        `deactivate_brand_suppliers.py`.
        """
        assert CROSS_ENTITY_TARGETS.get("product", {}).get("supplier") == frozenset({"name"})

    def test_custom_field_no_puede_ser_una_entidad(self) -> None:
        """Si alguien agregara una entidad llamada ``custom_field``, que falle acá.

        La gramática la resuelve por orden (el prefijo de campo propio se chequea
        antes que el de entidad), así que no habría ambigüedad — pero la entidad
        quedaría permanentemente inalcanzable como prefijo cruzado, en silencio.
        """
        assert "custom_field" not in CROSS_ENTITY_PREFIXES


class TestResolucionDeColumnas:
    """``_resolve_target_cols`` resuelve igual las dos ramas."""

    def test_dos_columnas_al_mismo_campo_propio_gana_la_primera(self) -> None:
        """La rama canónica tiene guard de first-wins; la custom no lo tenía.

        Con los campos propios escritos a mano, de a uno, casi no pasaba. En
        cuanto el mapeo propone un campo propio por cada columna sin reconocer,
        dos columnas que colapsen al mismo slug hacen que la SEGUNDA pise a la
        primera en silencio: el valor guardado pasa a depender del orden de las
        columnas del Excel. Es el incidente ASTERIA en versión campo propio.
        """
        _, custom, _ = _resolve_target_cols(
            {
                "Observaciones": "custom_field:obs",
                "Obs.": "custom_field:obs",
            }
        )
        assert custom["obs"] == "Observaciones"

    def test_la_rama_canonica_sigue_siendo_first_wins(self) -> None:
        canonicos, _, _ = _resolve_target_cols(
            {"Total": "amount", "Importe": "amount"}
        )
        assert canonicos["amount"] == "Total"

    def test_ignorar_no_ocupa_ningun_campo(self) -> None:
        canonicos, custom, _ = _resolve_target_cols({"Notas internas": "ignore"})
        assert canonicos == {}
        assert custom == {}


class TestLosCruzadosDejanRastro:
    """#10 del review — `kind="cross"` caía sin rama y la columna se evaporaba.

    F-D no está entregada, así que un target `{entidad}:{campo}` no se puede
    escribir todavía. Lo que no puede pasar es que el usuario mapee una columna a
    mano, el importador la tire y nadie se entere: es la clase exacta del
    incidente ASTERIA —un valor que desaparece y una heurística que lo reemplaza
    con otra cosa—. No se rechaza (hay imports vivos que dependen del fallback
    heurístico de `unit_cost_ars`), pero se reporta.
    """

    def test_un_target_cruzado_se_descarta_pero_se_reporta(self) -> None:
        canonicos, custom, cruzados = _resolve_target_cols(
            {"Costo": "product:unit_cost_ars", "Total": "amount"}
        )

        assert "product:unit_cost_ars" not in canonicos.values()
        assert cruzados == {"Costo": "product:unit_cost_ars"}
        # …y no contamina las otras dos ramas.
        assert canonicos == {"amount": "Total"}
        assert custom == {}

    def test_sin_cruzados_no_hay_nada_que_reportar(self) -> None:
        """Control: si esto no fuera vacío, el aviso saldría en todo import."""
        _, _, cruzados = _resolve_target_cols({"Total": "amount"})
        assert cruzados == {}
