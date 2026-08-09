"""F-M — el reconocedor contra TODO lo que el motor viejo ya sabía leer.

Esta compuerta existe porque la anterior no alcanzó, y el hueco costó un revert.

`test_header_reading_vs_battery` compara contra una batería escrita a mano: 90
encabezados, y sólo de `sale`, `expense` y `product`. **`customer` y `supplier`
no estaban**, así que cuando el reconocedor dejó de mapear `Cliente` y
`Proveedor` —el encabezado más canónico de cada import de maestros, y `name` es
requerido— el gate siguió verde y la rama viajó rota hasta que un code-review la
encontró.

La lección no es «faltaban dos filas»: es que una batería elegida a mano mide lo
que a alguien se le ocurrió, no lo que el sistema ya sabía hacer. Acá el corpus
no se elige: son **los 299 keywords de `_HEURISTICS`**, en las 5 entidades. Todo
lo que el motor viejo resolvía tiene que seguir resolviéndose, salvo lo que esté
declarado abajo con su motivo.

Se compara CADENA contra CADENA, no motor contra motor: el reconocedor no corre
solo, y medirlo aislado exagera el daño (lo que no reconoce y no explica sigue
cayendo a fuzzy, como siempre).
"""

from __future__ import annotations

import pytest

from app.application.services.column_mapping_service import (
    _HEURISTICS,
    _fuzzy_match,
    _heuristic_match,
    _normalize_col,
    read_header,
)

#: Los que dejan de auto-mapear **a propósito**: el encabezado admite más de una
#: lectura y la fase entera existe para preguntarlo en vez de elegir a dedo.
#: Todos tienen que dar `ambiguo` — si alguno cayera a `sin_evidencia`, sería un
#: hueco de la tabla disfrazado de decisión.
AMBIGUOS_A_PROPOSITO: dict[tuple[str, str], str] = {
    ("sale", "pago"): "¿cuánto se pagó, o con qué? El encabezado no lo dice.",
    ("sale", "precio_venta"): "¿el precio de cada unidad, o el total de la línea?",
    ("sale", "precio_vendido"): "ídem: unitario o total de la línea.",
    ("product", "precio"): "los tres precios de un producto conviven (F10).",
    ("product", "price"): (
        "El mismo caso en inglés: tampoco dice cuál de los tres precios es."
    ),
}

#: Los que cambian de campo, con el motivo. Un cambio sin motivo escrito es un
#: bug esperando a que alguien lo lea como intencional.
CAMBIAN_DE_CAMPO: dict[tuple[str, str], tuple[str, str]] = {
    ("product", "descripcion"): (
        "description",
        "La batería ya marcaba `Descripción`→`name` como equivocado: una "
        "descripción no es el nombre, y el catálogo tiene campo propio.",
    ),
    ("product", "descripción"): (
        "description",
        "La misma columna escrita con tilde: resuelve igual porque los acentos se "
        "normalizan en el tokenizador, que es lo que se arregló en esta fase.",
    ),
    ("expense", "p_costo"): (
        "amount",
        "Degradación conocida y menor: `costo` a secas en una compra ya resolvía "
        "a `amount`, y la `p` abreviada no alcanza para afirmar que es unitario.",
    ),
}


def _cadena_vieja(normalizado: str, entidad: str) -> str | None:
    heur = _heuristic_match(normalizado, entidad)
    return heur if heur is not None else _fuzzy_match(normalizado, entidad)[0]


def _cadena_nueva(normalizado: str, entidad: str) -> str | None:
    """El reconocedor con el corte de cadena que se va a cablear.

    Vive acá y no en el servicio porque el cableado todavía no está: esta
    compuerta es justamente la condición para volver a hacerlo.
    """
    r = read_header(normalizado, entidad)
    if r.outcome == "unico":
        return r.target
    if r.outcome == "ambiguo" or r.duda is not None:
        return None
    return _fuzzy_match(normalizado, entidad)[0]


def _corpus() -> list[tuple[str, str]]:
    return [
        (entidad, kw)
        for entidad, campos in _HEURISTICS.items()
        for kw in sorted({k for kws in campos.values() for k in kws})
    ]


class TestNadaQueYaSeLeiaSePierdeEnSilencio:
    @pytest.mark.parametrize(("entidad", "kw"), _corpus())
    def test_cada_keyword_conocido(self, entidad: str, kw: str) -> None:
        n = _normalize_col(kw)
        viejo = _cadena_vieja(n, entidad)
        if viejo is None:
            return  # el motor viejo tampoco lo resolvía: no hay nada que conservar
        nuevo = _cadena_nueva(n, entidad)
        clave = (entidad, kw)

        if clave in AMBIGUOS_A_PROPOSITO:
            assert read_header(n, entidad).outcome == "ambiguo", (
                f"{entidad}/{kw} está declarado como ambiguo a propósito, pero el "
                f"reconocedor no lo ve así: es un hueco de la tabla, no una decisión"
            )
            return
        if clave in CAMBIAN_DE_CAMPO:
            assert nuevo == CAMBIAN_DE_CAMPO[clave][0]
            return
        assert nuevo == viejo, f"{entidad}/{kw}: {viejo} → {nuevo}, sin declarar"


class TestLasDeclaracionesNoSePudren:
    def test_toda_excepcion_declarada_sigue_existiendo(self) -> None:
        """Una excepción que ya no aplica es peor que ninguna: dice que hay una
        pérdida aceptada donde no la hay."""
        corpus = set(_corpus())
        for clave in {**AMBIGUOS_A_PROPOSITO, **CAMBIAN_DE_CAMPO}:
            assert clave in corpus, f"{clave} ya no está en _HEURISTICS"

    def test_toda_excepcion_explica_por_que(self) -> None:
        for clave, motivo in AMBIGUOS_A_PROPOSITO.items():
            assert len(motivo) > 20, clave
        for clave, (_target, motivo) in CAMBIAN_DE_CAMPO.items():
            assert len(motivo) > 20, clave


class TestLosMaestrosQueElGateAnteriorNoMiraba:
    """Las dos entidades que la batería no cubría, con nombre y apellido."""

    @pytest.mark.parametrize(
        ("entidad", "header", "esperado"),
        [
            ("customer", "Cliente", "name"),
            ("customer", "Tipo cliente", "customer_type"),
            ("customer", "Condición IVA", "iva_condition"),
            ("customer", "Situación IVA", "iva_condition"),
            ("supplier", "Proveedor", "name"),
            ("supplier", "Condición de pago", "payment_method"),
        ],
    )
    def test_el_encabezado_canonico_de_un_padron_resuelve(
        self, entidad: str, header: str, esperado: str
    ) -> None:
        assert _cadena_nueva(_normalize_col(header), entidad) == esperado


class TestLoQueLaTablaAFIRMASigueCortando:
    """El corte no se debilitó al arreglar los huecos: lo que una regla declara
    imposible sigue sin llegar a fuzzy, que es lo que reintroducía el bug."""

    @pytest.mark.parametrize(
        ("entidad", "header"),
        [
            ("expense", "Envío unitario"),
            ("expense", "Bonificación proveedor"),
            ("expense", "Total factura sin impuestos"),
            ("expense", "Descuento por producto"),
        ],
    )
    def test_una_decision_de_la_tabla_no_la_revisa_la_capa_de_abajo(
        self, entidad: str, header: str
    ) -> None:
        n = _normalize_col(header)
        assert read_header(n, entidad).duda, f"{header} tiene que explicar por qué no"
        assert _cadena_nueva(n, entidad) is None
        # Y la premisa: fuzzy SÍ le pondría un campo si lo dejaran opinar.
        if header == "Envío unitario":
            assert _fuzzy_match(n, entidad)[0] == "unit_price"

    @pytest.mark.parametrize(
        ("entidad", "header", "basura"),
        [
            ("product", "Comprobante", "unit_cost_ars"),
            ("customer", "Cantidad", "locality"),
            ("customer", "Vencimiento", "birthday"),
        ],
    )
    def test_cortar_evita_la_basura_que_inventaria_fuzzy(
        self, entidad: str, header: str, basura: str
    ) -> None:
        """Por qué el corte alcanza también a «entendí y no tengo dónde ponerlo».

        Dejar caer esos casos a fuzzy no es neutral: un «Comprobante» en un
        catálogo termina en el COSTO del producto, y un «Vencimiento» en un padrón
        de clientes en su CUMPLEAÑOS. La columna sin mapear se completa a mano; la
        columna mapeada mal se descubre cuando el número ya está mal.
        """
        n = _normalize_col(header)
        assert _fuzzy_match(n, entidad)[0] == basura, "premisa: fuzzy inventa esto"
        assert _cadena_nueva(n, entidad) is None
