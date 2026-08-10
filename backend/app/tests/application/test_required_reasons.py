"""F-C: ningún campo que el importador exige puede quedarse sin explicación.

La queja que originó la fase fue leer «Campos requeridos sin mapear:
transaction_date». Eso no explica nada: nombra el campo interno y suena a que la
columna de la planilla es obligatoria, cuando lo que pasa es al revés —Véktor
necesita que ALGUNA columna le diga la fecha—. `REQUIRED_REASONS` existe para que
la pantalla pueda decir la consecuencia en vez del nombre del campo.

Estos tests son un **ratchet**, no una descripción: agregar un requerido nuevo
—o una alternativa nueva— sin escribirle el motivo pone el archivo en rojo. Sin
la compuerta, el próximo requerido entra mudo y la pantalla vuelve a listar
nombres internos, que es exactamente el estado del que se salió.
"""

from __future__ import annotations

import pytest

from app.application.services.column_mapping_service import (
    CANONICAL_FIELDS,
    REQUIRED_ALTERNATIVES,
    REQUIRED_FIELDS,
    REQUIRED_REASONS,
    required_reason,
)


def _campos_que_exigen_motivo() -> list[tuple[str, str]]:
    """``(entidad, campo)`` de todo lo que el importador necesita para importar.

    Incluye las ALTERNATIVAS (`unit_price` + `quantity` cubriendo al monto, F-H4):
    para el usuario son tan "lo que hay que mapear" como el requerido mismo — de
    hecho son la salida cuando su planilla no trae la columna del total—, así que
    dejarlas sin motivo reproduce el problema en la mitad de los casos.
    """
    pares: set[tuple[str, str]] = set()
    for entidad, campos in REQUIRED_FIELDS.items():
        pares.update((entidad, campo) for campo in campos)
    for entidad, alternativas in REQUIRED_ALTERNATIVES.items():
        for sustitutos in alternativas.values():
            pares.update((entidad, campo) for campo in sustitutos)
    return sorted(pares)


class TestTodoRequeridoTieneMotivo:
    @pytest.mark.parametrize(("entidad", "campo"), _campos_que_exigen_motivo())
    def test_el_campo_tiene_motivo_no_vacio(self, entidad: str, campo: str) -> None:
        motivo = required_reason(entidad, campo)
        assert motivo.strip(), (
            f"«{entidad}.{campo}» lo exige el importador y no tiene motivo escrito: "
            "la pantalla sólo puede mostrar el nombre interno del campo."
        )

    @pytest.mark.parametrize(("entidad", "campo"), _campos_que_exigen_motivo())
    def test_el_motivo_no_es_el_nombre_del_campo_disfrazado(
        self, entidad: str, campo: str
    ) -> None:
        """Un motivo tiene que ser una frase, no la etiqueta repetida.

        Sin este piso, `"Monto de venta"` pasaría la compuerta de arriba y no
        explicaría absolutamente nada más que el label que ya está al lado.
        """
        motivo = required_reason(entidad, campo)
        assert motivo.strip() != CANONICAL_FIELDS[entidad][campo]
        assert len(motivo.split()) >= 10, (
            f"El motivo de «{entidad}.{campo}» no alcanza a explicar una consecuencia."
        )


class TestNingunMotivoHuerfano:
    """Un motivo sobre un campo que no existe es peor que ninguno: nadie lo ve.

    Renombrar un campo canónico y olvidar el motivo deja la explicación colgada,
    invisible en la UI y viva en el diff — y el test de arriba sigue verde porque
    mira los requeridos, no los motivos.
    """

    def test_la_entidad_del_motivo_existe(self) -> None:
        assert set(REQUIRED_REASONS) <= set(CANONICAL_FIELDS)

    def test_el_campo_del_motivo_existe_en_su_entidad(self) -> None:
        huerfanos = [
            f"{entidad}.{campo}"
            for entidad, motivos in REQUIRED_REASONS.items()
            for campo in motivos
            if campo not in CANONICAL_FIELDS.get(entidad, {})
        ]
        assert not huerfanos, f"Motivos sobre campos inexistentes: {huerfanos}"


class TestElMotivoDiceLoQueElImportadorHace:
    """El copy no puede prometer un destino que el importador no le da a la fila.

    Los tres destinos son distintos y verificados contra
    `ingestion_import_service`: la venta sin monto y todo lo que no tiene fecha
    van a «Otros» (`_capture_unclassified`, rescatable); el gasto sin monto y el
    producto sin nombre se DESCARTAN (`return False`, sin rastro); el maestro sin
    nombre se cuenta como inválido en el resumen. Decirle a alguien que busque en
    «Otros» una fila que se descartó lo manda a buscar algo que no está.
    """

    def test_la_venta_sin_monto_promete_otros(self) -> None:
        assert "«Otros»" in required_reason("sale", "amount")

    def test_el_gasto_sin_monto_avisa_que_se_descarta(self) -> None:
        motivo = required_reason("expense", "amount")
        assert "descarta" in motivo
        assert "tampoco queda en «Otros»" in motivo

    @pytest.mark.parametrize(
        ("entidad", "campo"), [("sale", "transaction_date"), ("expense", "expense_date")]
    )
    def test_la_fila_sin_fecha_promete_otros_y_niega_el_hoy(
        self, entidad: str, campo: str
    ) -> None:
        """Invariante 2d: sin fecha reconocible NUNCA se inventa "hoy"."""
        motivo = required_reason(entidad, campo)
        assert "«Otros»" in motivo
        assert "hoy" in motivo

    def test_el_producto_sin_nombre_avisa_que_se_descarta(self) -> None:
        assert "descarta" in required_reason("product", "name")

    @pytest.mark.parametrize("entidad", ["customer", "supplier"])
    def test_el_maestro_sin_nombre_avisa_que_se_cuenta_como_invalido(
        self, entidad: str
    ) -> None:
        assert "inválida" in required_reason(entidad, "name")


class TestElHelperNoRompeConLoDesconocido:
    """El catálogo pide el motivo de TODOS los campos, no sólo de los requeridos."""

    @pytest.mark.parametrize(
        ("entidad", "campo"),
        [("sale", "notes"), ("sale", "no_existe"), ("no_existe", "amount")],
    )
    def test_sin_motivo_devuelve_cadena_vacia(self, entidad: str, campo: str) -> None:
        assert required_reason(entidad, campo) == ""
