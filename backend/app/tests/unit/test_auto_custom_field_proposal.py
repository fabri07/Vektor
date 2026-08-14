"""F-A.2/A.5 — lo que no se reconoce se CONSERVA, no se descarta.

La queja que abrió el programa fue tener que renombrar prácticamente todas las
columnas de un archivo real. Hasta acá, la columna que Véktor no reconocía salía
``unmapped`` con ``target_field=None``: la persona tenía que decidir una por una
o perderla.

Ahora se propone conservarla como campo propio con el NOMBRE ORIGINAL. Lo
delicado no es proponerlo — es qué NO se propone, y que el aviso de requeridos
no desaparezca por el camino (V10).
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.services.column_mapping_service import ColumnMappingService


def _svc() -> ColumnMappingService:
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=result)
    return ColumnMappingService(db)


async def _sugerir(
    entity: str, headers: list[str], fila: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    fila = fila if fila is not None else {h: "x" for h in headers}
    return await _svc().suggest_mappings(
        uuid.uuid4(), entity, headers, [fila], allow_llm=False
    )


def _por_columna(sugerencias: list[dict[str, Any]], col: str) -> dict[str, Any]:
    return next(s for s in sugerencias if s["source_column"] == col)


class TestSeProponeConservar:
    async def test_una_columna_desconocida_se_propone_como_campo_propio(self) -> None:
        s = (await _sugerir("sale", ["Sucursal"]))[0]
        assert s["target_field"] == "custom_field:sucursal"
        assert s["status"] == "mapped"

    async def test_la_etiqueta_es_el_nombre_original_del_archivo(self) -> None:
        """No se reconstruye desde el slug: sería un viaje de ida.

        `custom_field_slug` saca acentos, mayúsculas y puntuación, así que desde
        `ano_fiscal` no hay forma de volver a «Año Fiscal». El label es lo único
        con lo que la persona reconoce su columna en el ERD.
        """
        s = (await _sugerir("sale", ["Año Fiscal"]))[0]
        assert s["target_field"] == "custom_field:ano_fiscal"
        assert s["target_label"] == "Año Fiscal"

    async def test_el_origen_dice_que_lo_propuso_el_nombre_del_archivo(self) -> None:
        # Origen propio: nadie RECONOCIÓ nada. Decir `heuristic` haría que la
        # pantalla mostrara «sugerido por el nombre de la columna», que es
        # exactamente el tipo de afirmación falsa que F-B vino a sacar.
        s = (await _sugerir("sale", ["Sucursal"]))[0]
        assert s["source"] == "auto_custom"

    async def test_un_canonico_reconocido_no_se_toca(self) -> None:
        # Control: si la propuesta pisara lo reconocido, el archivo entero
        # entraría como campos propios y no habría importación de nada.
        s = (await _sugerir("sale", ["Observaciones"]))[0]
        assert s["target_field"] == "notes"
        assert s["source"] == "heuristic"


class TestLoQueNoSePropone:
    async def test_sin_una_sola_muestra_no_se_propone(self) -> None:
        """Una columna sin valores no es un dato a conservar: está vacía.

        Es la primera de las tres capas de columnas vacías; el confirm dropea
        las 100 % vacías del archivo completo.
        """
        s = (await _sugerir("sale", ["Sucursal"], fila={"Sucursal": None}))[0]
        assert s["status"] == "unmapped"
        assert s["target_field"] is None

    async def test_una_columna_con_duda_conserva_su_explicacion(self) -> None:
        """`codigo_interno_xz99` se reconoce como `sku` y esta hoja no tiene dónde.

        Llega `unmapped` pero CON duda. Archivarla como campo propio taparía la
        explicación y rompería el invariante de F-M de que una columna `mapped`
        no arrastra una duda.
        """
        s = (await _sugerir("sale", ["codigo_interno_xz99"]))[0]
        assert s["status"] == "unmapped"
        assert s["duda"]
        assert s["target_field"] is None

    async def test_una_columna_ambigua_sigue_preguntando(self) -> None:
        # «Precio» en un catálogo son tres precios distintos (F10). Elegir uno
        # por default es el incidente ASTERIA; archivarlo como campo propio es
        # perder el precio.
        s = (await _sugerir("product", ["Precio"]))[0]
        assert s["status"] == "ambiguo"
        assert s["target_field"] is None
        assert len(s["options"]) > 1


class TestColisionDeSlug:
    async def test_dos_columnas_con_el_mismo_slug_se_desambiguan_por_orden(self) -> None:
        """«Sucursal» y «Sucursal.» dan el mismo slug.

        Sin sufijo la segunda pisaría a la primera — y encima sería una colisión
        que Véktor se creó solo, no una que el archivo traía. (Se usan estas dos
        y no «Obs.»/«Obs» porque ésas SÍ las reconoce el motor, como `notes`.)
        """
        sugerencias = await _sugerir("sale", ["Sucursal", "Sucursal."])
        assert (
            _por_columna(sugerencias, "Sucursal")["target_field"]
            == "custom_field:sucursal"
        )
        assert (
            _por_columna(sugerencias, "Sucursal.")["target_field"]
            == "custom_field:sucursal_2"
        )

    async def test_las_etiquetas_no_se_desambiguan_junto_con_el_slug(self) -> None:
        # Los dos campos son distintos, pero cada uno se sigue llamando como su
        # columna: el sufijo es del identificador, no del nombre visible.
        sugerencias = await _sugerir("sale", ["Sucursal", "Sucursal."])
        assert _por_columna(sugerencias, "Sucursal")["target_label"] == "Sucursal"
        assert _por_columna(sugerencias, "Sucursal.")["target_label"] == "Sucursal."


class TestV10ElAvisoDeRequeridosNoDesaparece:
    """El riesgo estructural de F-A: si ninguna columna queda `unmapped`, la
    pasada que marca `required_missing` no marca nada **y nadie se entera**.
    """

    #: Hoja de ventas donde el monto NO se reconoce y la columna candidata sí
    #: contiene un keyword del requerido. Medido, no supuesto: casi todo
    #: encabezado con «fecha»/«monto» lo resuelve el reconocedor, así que la
    #: rama sólo se alcanza con un nombre raro alrededor del keyword.
    _HOJA_SIN_MONTO = ["Fecha", "cobro interno xz"]

    async def test_la_candidata_a_un_requerido_no_se_archiva_como_campo_propio(
        self,
    ) -> None:
        """Proponer «guardala como campo propio» sobre la candidata a un
        requerido es ofrecer tirar el monto de la venta a un campo suelto.

        Es el corazón de V10: si esta columna se auto-propusiera, ninguna
        quedaría `unmapped` y la pasada de requeridos no marcaría nada.
        """
        sugerencias = await _sugerir("sale", self._HOJA_SIN_MONTO)
        candidata = _por_columna(sugerencias, "cobro interno xz")
        assert candidata["status"] == "required_missing"
        assert candidata["target_field"] is None

    async def test_dice_cual_requerido_falta(self) -> None:
        sugerencias = await _sugerir("sale", self._HOJA_SIN_MONTO)
        candidata = _por_columna(sugerencias, "cobro interno xz")
        # Sin esto la pantalla ve un punto rojo y tiene que adivinar cuál de los
        # requeridos es.
        assert candidata["missing_field"] == "amount"

    async def test_un_campo_propio_aprendido_no_cubre_el_requerido(self) -> None:
        """Un campo propio homónimo de un requerido no lo cubre.

        Camino real, no fabricado: el historial del tenant es la capa 1 y le
        gana a todo. `save_mappings` hoy no aprende `custom_field:`, pero la
        columna no tiene restricción que lo impida y las filas anteriores a esa
        guarda siguen vivas.

        Lo garantiza la aritmética de conjuntos, no un filtro:
        `"custom_field:amount"` nunca es igual a `"amount"`. Se midió — agregar
        un filtro por target canónico acá no lo mata ninguna mutación, así que
        el filtro se sacó y quedó este test fijando la conducta.
        """
        db = AsyncMock()
        resultado = MagicMock()
        alias = MagicMock()
        alias.source_column = "monto"
        alias.target_field = "custom_field:amount"
        alias.confirmed_count = 5
        resultado.scalars.return_value.all.return_value = [alias]
        db.execute = AsyncMock(return_value=resultado)

        # Hace falta además una columna LIBRE que se parezca al requerido: la
        # marca por columna necesita dónde ponerse. Sin ella el filtro igual
        # corrige la cobertura, pero no queda nada observable que lo pruebe.
        sugerencias = await ColumnMappingService(db).suggest_mappings(
            uuid.uuid4(),
            "sale",
            ["Fecha", "Monto", "cobro interno xz"],
            [{"Fecha": "1/3", "Monto": "10", "cobro interno xz": "9"}],
            allow_llm=False,
        )
        monto = _por_columna(sugerencias, "Monto")
        assert monto["target_field"] == "custom_field:amount"

        # `amount` sigue sin cubrirse, así que la columna libre lo reporta. Sin
        # el filtro canónico, `custom_field:amount` contaría como cubierto y
        # esta marca no existiría.
        libre = _por_columna(sugerencias, "cobro interno xz")
        assert libre["status"] == "required_missing"
        assert libre["missing_field"] == "amount"

    @pytest.mark.parametrize("entity", ["sale", "expense", "product"])
    async def test_ningun_requerido_queda_cubierto_por_un_campo_propio(
        self, entity: str
    ) -> None:
        """Compuerta general: ningún `custom_field:` puede llamarse como un
        requerido y hacerlo pasar por cubierto, en ninguna entidad."""
        from app.application.services.column_mapping_service import (
            REQUIRED_FIELDS,
            missing_required_fields,
        )

        requeridos = list(REQUIRED_FIELDS.get(entity, []))
        if not requeridos:
            pytest.skip(f"{entity} no tiene requeridos")
        cubiertos_falsos = {f"custom_field:{r}" for r in requeridos}
        assert missing_required_fields(entity, cubiertos_falsos) == set(requeridos)
