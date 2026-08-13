"""F-M — el diff de comportamiento del reconocedor, fila por fila.

`test_header_recognition_battery` fija lo que responde el motor VIEJO
(`_heuristic_match`). Este archivo corre el motor NUEVO sobre la misma batería y
declara, explícitamente, cuáles de esas 90 lecturas cambian y en qué se
convierten.

Sin esto el rediseño es un efecto lateral: un reconocedor nuevo que mueve
columnas de campo sin que nadie pueda revisar cuáles. Con esto, cambiar el
comportamiento de un encabezado obliga a tocar la tabla de abajo — que es
exactamente la revisión que un cambio así merece.

La lectura de esta compuerta, medida:

- **72 filas resuelven igual que hoy** — y son, una por una, las 72 que la
  batería marcó ``OK``. Cero regresiones.
- **18 cambian** — las 7 ``MAL``, las 3 ``AMBIGUO``, las 3 ``FALTA`` y las 5
  ``SIN_CAMPO``. Ninguna fila que ya andaba bien se movió.
"""

from __future__ import annotations

import pytest

from app.application.services.column_mapping_service import _normalize_col, read_header
from app.tests.unit.test_header_recognition_battery import BATERIA, OK

#: ``(entidad, encabezado) → (outcome, target | opciones ordenadas)``.
#:
#: Toda fila de la batería que NO esté acá tiene que resolver a `unico` con el
#: mismo target que hoy. Las que están, cambian — y este es el cambio.
LECTURA_NUEVA: dict[tuple[str, str], tuple[str, object]] = {
    # ── Empates que resolvía el orden de declaración del dict ────────────────
    ("sale", "Fecha de venta"): ("unico", "transaction_date"),
    ("expense", "Fecha del gasto"): ("unico", "expense_date"),
    ("product", "Descripción"): ("unico", "description"),
    # ── El modificador ya no se lleva el campo ───────────────────────────────
    # F-M.7: los dos reconocían el concepto y no tenían dónde ponerlo. Ahora
    # `discount` existe, así que el calificador de entidad sigue sin llevarse el
    # campo y además el descuento llega a destino.
    ("expense", "Bonificación proveedor"): ("unico", "discount"),
    ("expense", "Descuento por producto"): ("unico", "discount"),
    ("expense", "Envío unitario"): ("sin_evidencia", None),
    ("expense", "Costo final por producto"): ("ambiguo", ("amount", "unit_price")),
    ("expense", "Total factura sin impuestos"): ("sin_evidencia", None),
    # ── Los acentos ya NO son un diff ────────────────────────────────────────
    # `Artículo`, `Categoría` (venta y producto), `Envío` y `Código de barras`
    # estaban declarados acá: F-M los leía bien y la capa heurística no, porque
    # su clave de matching no plegaba acentos. Ahora sí los pliega, así que las
    # dos capas coinciden y estas filas dejaron de ser un cambio entre motores
    # — no porque F-M dejara de resolverlas, sino porque el viejo se puso a la
    # par. Declararlas seguiría afirmando una diferencia que ya no existe.
    # ── `con`/`sin` describen la inclusión, no el concepto ───────────────────
    ("expense", "Precio con IVA"): ("ambiguo", ("amount", "unit_price")),
    ("expense", "Precio sin IVA"): ("ambiguo", ("amount", "unit_price")),
    ("expense", "Neto sin IVA"): ("unico", "amount"),
    # ── Dos lecturas razonables: se ofrecen, no se elige ─────────────────────
    ("sale", "Precio de venta"): ("ambiguo", ("amount", "unit_price")),
    # ── F-M.7: conceptos que se reconocían y recién ahora tienen campo ───────
    ("expense", "Descuento"): ("unico", "discount"),
    ("expense", "Bonificación"): ("unico", "discount"),
    ("expense", "IVA"): ("unico", "taxes"),
    ("expense", "Impuestos"): ("unico", "taxes"),
    # Semántica OPUESTA a `shipping_cost`: éste se suma, aquél se cobra una vez.
    ("expense", "Flete por línea"): ("unico", "shipping_cost_line"),
    # ── Reconocido, pero sin campo donde ponerlo ─────────────────────────────
    ("product", "Marca"): ("sin_evidencia", None),
}


def _leer(entidad: str, header: str):  # noqa: ANN202 — el tipo real es HeaderReading
    return read_header(_normalize_col(header), entidad)


def _lectura(r) -> tuple[str, object]:  # noqa: ANN001
    if r.outcome == "unico":
        return ("unico", r.target)
    return (r.outcome, tuple(sorted(r.options)) if r.options else None)


class TestElDiffEsExactamenteElDeclarado:
    def test_las_filas_que_cambian_son_las_que_estan_en_la_tabla(self) -> None:
        """Ni una columna se mueve de campo sin quedar escrita acá."""
        medidas = {
            (entidad, header)
            for entidad, header, viejo, _v, _p in BATERIA
            if _lectura(_leer(entidad, header)) != ("unico", viejo)
        }
        assert medidas == set(LECTURA_NUEVA), (
            f"sin declarar: {sorted(medidas - set(LECTURA_NUEVA))} · "
            f"declaradas y ya no cambian: {sorted(set(LECTURA_NUEVA) - medidas)}"
        )

    @pytest.mark.parametrize(("clave", "esperado"), sorted(LECTURA_NUEVA.items()))
    def test_cada_cambio_da_la_lectura_declarada(
        self, clave: tuple[str, str], esperado: tuple[str, object]
    ) -> None:
        assert _lectura(_leer(*clave)) == esperado


class TestCeroRegresiones:
    def test_toda_fila_que_no_cambia_es_una_que_ya_estaba_bien(self) -> None:
        """El motor nuevo no puede conservar una respuesta que la batería marcó
        como equivocada: dejarla quieta sería no haber arreglado nada."""
        for entidad, header, viejo, veredicto, por_que in BATERIA:
            if (entidad, header) in LECTURA_NUEVA:
                continue
            assert veredicto == OK, f"{entidad}/{header} sigue igual y estaba mal: {por_que}"
            assert _lectura(_leer(entidad, header)) == ("unico", viejo)

    def test_las_72_lecturas_correctas_siguen_intactas(self) -> None:
        intactas = [
            (e, h) for e, h, _viejo, v, _p in BATERIA if v == OK and (e, h) not in LECTURA_NUEVA
        ]
        # Eran 67 hasta que el plegado de acentos en la clave de matching
        # sumó 5 filas al lado OK (ver la nota de LECTURA_NUEVA).
        assert len(intactas) == 72


class TestLoQueNoResuelveConservaLaColumna:
    def test_ningun_ambiguo_ni_sin_evidencia_propone_un_target(self) -> None:
        """La regla rectora de la fase: sin demostración no hay mapeo."""
        for entidad, header in LECTURA_NUEVA:
            r = _leer(entidad, header)
            if r.outcome != "unico":
                assert r.target is None, f"{entidad}/{header} propone {r.target}"

    def test_reconocer_el_concepto_y_no_tener_campo_siempre_se_explica(self) -> None:
        """«No entiendo esto» y «entiendo qué es pero no tengo dónde ponerlo» no
        son el mismo mensaje. El segundo sin `duda` deja al usuario ante un hueco
        mudo — y es el caso de `Marca`, `Envío unitario` y `Total factura sin
        impuestos`. (`Descuento` e `IVA` estaban acá hasta F-M.7, que les dio
        campo propio: la lista se achica a medida que el catálogo crece.)"""
        for entidad, header in LECTURA_NUEVA:
            r = _leer(entidad, header)
            if r.outcome != "unico" and r.concept is not None:
                assert r.duda, f"{entidad}/{header} reconoce «{r.concept}» y no explica nada"

    def test_ninguna_fila_mal_conserva_el_target_equivocado(self) -> None:
        """La compuerta de la fase, dicha al revés: ningún target que la batería
        midió como equivocado sobrevive.

        Sólo las filas que HOY proponen algo: en las que no proponían nada
        (`falta`/`sin_campo`) el target viejo es ``None``, y seguir sin proponer
        es el resultado correcto, no el error que este test busca.
        """
        revisadas = 0
        for entidad, header, viejo, veredicto, _p in BATERIA:
            if veredicto == OK or viejo is None:
                continue
            revisadas += 1
            assert _leer(entidad, header).target != viejo, f"{entidad}/{header}"
        assert revisadas == 10, "7 `mal` + 3 `ambiguo`: si baja, la batería perdió casos"
