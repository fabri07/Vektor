"""El catálogo de campos tiene UNA sola fuente.

El frontend mantenía una copia manual de ``CANONICAL_FIELDS`` comentada "mantener
en sync" y divergió: a ``expense`` le faltaban ``payment_method`` e
``is_recurring``. Como el ``<select>`` del panel solo renderiza opciones de esa
copia, cuando el backend sugería ``payment_method`` ninguna ``<option>``
matcheaba, el DOM caía a la primera y la pantalla mostraba "Sin mapear" mientras
el estado enviaba ``payment_method`` (incidente ASTERIA, captura 16:50:50).

Este endpoint es lo que el frontend consume ahora, en vez de tener su propia lista.
"""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient

from app.application.services.column_mapping_service import (
    CANONICAL_FIELDS,
    REQUIRED_FIELDS,
    SINGLE_VALUE_FIELDS,
)


class TestFieldCatalog:
    async def test_devuelve_todas_las_entidades_con_requeridos_y_campos(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        response = await client.get("/api/v1/ingestion/field-catalog", headers=auth_headers)
        assert response.status_code == 200
        catalog = response.json()

        assert set(catalog) == set(CANONICAL_FIELDS)
        for entity, entry in catalog.items():
            assert entry["required"] == REQUIRED_FIELDS.get(entity, [])
            valores = {f["value"] for f in entry["fields"]}
            assert valores == set(CANONICAL_FIELDS[entity])
            # Todo campo trae etiqueta en castellano para mostrar en el select.
            assert all(f["label"] for f in entry["fields"])

    async def test_expense_incluye_los_campos_que_el_frontend_habia_perdido(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """La divergencia concreta que rompió el mapeo de `forma_pago`."""
        response = await client.get("/api/v1/ingestion/field-catalog", headers=auth_headers)
        valores = {f["value"] for f in response.json()["expense"]["fields"]}
        assert "payment_method" in valores
        assert "is_recurring" in valores

    async def test_los_tres_precios_de_producto_estan_disponibles(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        response = await client.get("/api/v1/ingestion/field-catalog", headers=auth_headers)
        fields = {f["value"]: f for f in response.json()["product"]["fields"]}
        assert fields["unit_cost_ars"]["label"] == "Costo unitario"
        assert fields["list_price_ars"]["label"] == "Precio de lista (sugerido)"
        assert fields["sale_price_ars"]["label"] == "Precio de venta"

    async def test_venta_puede_declarar_sku_y_barcode_del_producto(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """F-S.0: `sale` no tenía sku/barcode — el motor de resolución
        (`_venta_producto_id`) ya sabía leerlos, faltaba el target."""
        response = await client.get("/api/v1/ingestion/field-catalog", headers=auth_headers)
        valores = {f["value"] for f in response.json()["sale"]["fields"]}
        assert "sku" in valores
        assert "barcode" in valores

    async def test_marca_los_campos_de_valor_unico(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """`single_value` es lo que le permite a la UI bloquear la colisión con la
        MISMA regla que aplica el confirm, en vez de tener su propia lista."""
        response = await client.get("/api/v1/ingestion/field-catalog", headers=auth_headers)
        catalog = response.json()

        for entity, entry in catalog.items():
            escalares = {f["value"] for f in entry["fields"] if f["single_value"]}
            assert escalares == set(SINGLE_VALUE_FIELDS.get(entity, frozenset()))

        producto = {f["value"]: f["single_value"] for f in catalog["product"]["fields"]}
        assert producto["sale_price_ars"] is True
        assert producto["list_price_ars"] is True
        assert producto["unit_cost_ars"] is True
        # Varias columnas pueden alimentar una descripción: no bloquea.
        assert producto["description"] is False

    async def test_publica_la_alternativa_del_monto(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """F-H4: `amount OR (unit_price AND quantity)` viaja en el catálogo.

        Si la UI tuviera su propia copia de esta regla, bloquearía un archivo sin
        columna de monto que el confirm acepta — la misma clase de divergencia que
        obligó a crear este endpoint.
        """
        response = await client.get("/api/v1/ingestion/field-catalog", headers=auth_headers)
        catalog = response.json()

        # F-H6.a: compras también. Hasta entonces `expense` no tenía `unit_price`
        # ni `quantity` en su catálogo, así que derivar habría sido adivinar desde
        # columnas que nadie declaró.
        for entidad in ("sale", "expense"):
            assert catalog[entidad]["required_alternatives"] == {
                "amount": ["quantity", "unit_price"]
            }, entidad
        # Los dos campos de la alternativa existen de verdad en la entidad: una
        # alternativa que nombre un campo inexistente no se puede mapear nunca.
        for entidad in ("sale", "expense"):
            valores = {f["value"] for f in catalog[entidad]["fields"]}
            assert {"quantity", "unit_price"} <= valores, entidad

        # Los maestros y el catálogo de productos no tienen alternativa: su
        # requerido es el nombre, y no hay dos campos que lo calculen.
        for entity, entry in catalog.items():
            if entity not in ("sale", "expense"):
                assert entry["required_alternatives"] == {}, entity

    async def test_todo_requerido_llega_con_su_motivo(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """F-C.c2 — el catálogo no dice sólo QUÉ falta, dice por qué hace falta.

        Un asterisco rojo comunica "esto es obligatorio" y nada más; la persona
        que no mapeó el monto no sabe si su planilla no entra o si entra distinta.
        El motivo es lo que le permite decidir, así que ningún requerido puede
        salir sin él.
        """
        response = await client.get("/api/v1/ingestion/field-catalog", headers=auth_headers)
        catalog = response.json()

        for entity, entry in catalog.items():
            campos = {f["value"]: f for f in entry["fields"]}
            for requerido in entry["required"]:
                motivo = campos[requerido]["required_reason"]
                assert motivo, f"{entity}.{requerido} es obligatorio y no explica por qué"
                # En castellano y sobre el negocio: nunca el nombre técnico del
                # campo, que es justo lo que el mensaje viene a reemplazar.
                assert requerido not in motivo, f"{entity}.{requerido}"

    async def test_un_campo_sin_motivo_devuelve_cadena_vacia(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """Control del test de arriba: sin esto, servir el mismo texto para todos
        los campos lo daría por bueno igual.

        Y vacío en vez de `null`: la UI renderiza nada sin distinguir ausencias.
        """
        response = await client.get("/api/v1/ingestion/field-catalog", headers=auth_headers)
        gasto = {f["value"]: f for f in response.json()["expense"]["fields"]}

        assert "payment_method" not in response.json()["expense"]["required"]
        assert gasto["payment_method"]["required_reason"] == ""

    async def test_la_alternativa_del_monto_tambien_explica(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """`unit_price`/`quantity` no están en `required`, pero cubren al monto:
        quien no mapeó el total tiene que poder leer por qué estos dos alcanzan."""
        response = await client.get("/api/v1/ingestion/field-catalog", headers=auth_headers)
        catalog = response.json()

        for entidad in ("sale", "expense"):
            campos = {f["value"]: f for f in catalog[entidad]["fields"]}
            for campo in ("unit_price", "quantity"):
                assert campos[campo]["required_reason"], f"{entidad}.{campo}"

    async def test_la_regla_contextual_viaja_en_el_catalogo(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """F-C.c3b — `required: bool` contesta una pregunta sola para todos los
        archivos, y por eso contesta mal en los dos sentidos.

        `required_when` es lo que le permite a la pantalla decir "el monto sólo
        hace falta si no mapeaste precio × cantidad" y "el producto sólo hace
        falta si la hoja mueve unidades", sin que ninguna de las dos cosas se
        vuelva bloqueante.
        """
        response = await client.get("/api/v1/ingestion/field-catalog", headers=auth_headers)
        catalog = response.json()

        for entidad in ("sale", "expense"):
            campos = {f["value"]: f for f in catalog[entidad]["fields"]}

            monto = campos["amount"]["required_when"]
            assert monto is not None, entidad
            assert monto["condition"] == "covered_by_alternative"
            assert monto["explanation"]
            # La alternativa concreta NO se duplica acá: vive en
            # `required_alternatives` y una segunda copia podría divergir.
            assert monto["signals"] == []

            for campo in ("product_name", "quantity"):
                regla = campos[campo]["required_when"]
                assert regla is not None, f"{entidad}.{campo}"
                assert regla["condition"] == "sheet_moves_units"
                assert regla["explanation"]
                # Las señales nombran las columnas que gobiernan la condición, y
                # llegan ordenadas para que el mismo catálogo se lea siempre igual.
                assert regla["signals"]
                assert all(g == sorted(g) for g in regla["signals"]), f"{entidad}.{campo}"

    async def test_un_campo_sin_regla_contextual_no_inventa_una(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """Control: la mayoría de los campos no tienen regla, y ahí `required`
        alcanza. Sin esto, devolver una condición para todos pasaría igual."""
        response = await client.get("/api/v1/ingestion/field-catalog", headers=auth_headers)
        catalog = response.json()

        venta = {f["value"]: f for f in catalog["sale"]["fields"]}
        assert venta["transaction_date"]["required_when"] is None
        producto = {f["value"]: f for f in catalog["product"]["fields"]}
        assert producto["name"]["required_when"] is None

    async def test_la_regla_contextual_no_vuelve_obligatorio_nada(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """El catálogo DESCRIBE la regla; no la impone.

        `required` tiene que seguir siendo exactamente `REQUIRED_FIELDS`: si
        `product_name` se colara ahí, el confirm rechazaría con 422 toda planilla
        de servicios u honorarios que hoy entra bien.
        """
        response = await client.get("/api/v1/ingestion/field-catalog", headers=auth_headers)
        for entity, entry in response.json().items():
            assert entry["required"] == REQUIRED_FIELDS.get(entity, []), entity
            assert "product_name" not in entry["required"], entity

    async def test_requiere_autenticacion(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/ingestion/field-catalog")
        assert response.status_code in (401, 403)

    async def test_los_costos_de_compra_llegan_al_catalogo_como_escalares(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """F-M.7 — `discount`, `taxes` y `shipping_cost_line`.

        La UI arma el `<select>` desde acá, así que si el campo no sale servido no
        se puede elegir aunque el reconocedor lo proponga. Y los tres son
        escalares: dos columnas de descuento sobre la misma línea no se suman
        solas ni se elige una — el criterio que ya rechaza con 422.

        `shipping_cost_line` es un campo APARTE de `shipping_cost` a propósito: el
        del comprobante se cobra una vez y éste se suma, porque el reparto ya lo
        hizo quien armó la planilla.
        """
        response = await client.get("/api/v1/ingestion/field-catalog", headers=auth_headers)
        gasto = {f["value"]: f for f in response.json()["expense"]["fields"]}

        for campo in ("discount", "taxes", "shipping_cost_line"):
            assert campo in gasto, f"{campo} no llega al select del frontend"
            assert gasto[campo]["single_value"] is True, campo

        # Y los dos fletes conviven, cada uno con su etiqueta.
        assert gasto["shipping_cost"]["label"] != gasto["shipping_cost_line"]["label"]
