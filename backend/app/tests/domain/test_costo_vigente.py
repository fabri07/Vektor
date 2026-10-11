"""M·1c — cuál fuente del costo manda hoy (puro)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from app.domain.costo_de_referencia import CostoDeReferencia
from app.domain.costo_vigente import (
    CATALOGO_FIELD,
    FECHA_FIELD,
    MANUAL_FIELD,
    ORIGEN_FIELD,
    CostoFechado,
    aplicar_costo_de_catalogo,
    conservar_claves_del_costo,
    custom_fields_tras_editar_costo,
    decidir_costo_vigente,
    leer_costo_fechado,
    registrar_edicion,
    sellar,
)
from app.domain.purchase_cost import COSTO_BASE_FIELD

_SEP_10 = datetime(2026, 9, 10, 12, 0)


def _compra(costo: str, fecha: datetime, envio: str = "desconocido") -> CostoDeReferencia:
    return CostoDeReferencia(
        origen="compra",
        costo_unitario=Decimal(costo),
        fecha=fecha.replace(tzinfo=UTC),  # rótulo UTC, hora AR (M·1b)
        movement_id=uuid.uuid4(),
        envio=envio,  # type: ignore[arg-type]
    )


_SIN_COMPRA = CostoDeReferencia(origen="costo_cargado", costo_unitario=Decimal("1"))


class TestDecision2ManualFrenteACompra:
    def test_compra_posterior_a_la_edicion_gana(self) -> None:
        manual = CostoFechado(Decimal("90"), datetime(2026, 9, 5, 15, 0))
        v = decidir_costo_vigente(_compra("100", _SEP_10), manual, None)
        assert v is not None and (v.origen, v.costo) == ("compra", Decimal("100"))

    def test_compra_atrasada_no_pisa_una_edicion_mas_nueva(self) -> None:
        manual = CostoFechado(Decimal("90"), datetime(2026, 9, 20, 15, 0))
        v = decidir_costo_vigente(_compra("100", _SEP_10), manual, None)
        assert v is not None and (v.origen, v.costo) == ("manual", Decimal("90"))

    def test_empate_exacto_gana_lo_manual(self) -> None:
        manual = CostoFechado(Decimal("90"), _SEP_10)
        v = decidir_costo_vigente(_compra("100", _SEP_10), manual, None)
        assert v is not None and v.origen == "manual"


class TestDecision3Catalogo:
    def test_compra_posterior_reemplaza_al_catalogo(self) -> None:
        catalogo = CostoFechado(Decimal("70"), datetime(2026, 9, 1))
        v = decidir_costo_vigente(_compra("100", _SEP_10), None, catalogo)
        assert v is not None and v.origen == "compra"

    def test_catalogo_mas_nuevo_que_la_compra_es_la_estimacion_vigente(self) -> None:
        catalogo = CostoFechado(Decimal("70"), datetime(2026, 9, 30))
        v = decidir_costo_vigente(_compra("100", _SEP_10), None, catalogo)
        assert v is not None and (v.origen, v.costo) == ("catalogo", Decimal("70"))


class TestSinFuentes:
    def test_sin_compra_ni_ediciones_no_toca_nada(self) -> None:
        assert decidir_costo_vigente(_SIN_COMPRA, None, None) is None

    def test_sin_compra_vale_la_edicion(self) -> None:
        manual = CostoFechado(Decimal("90"), _SEP_10)
        v = decidir_costo_vigente(_SIN_COMPRA, manual, None)
        assert v is not None and v.origen == "manual"


class TestRotuloDelEnvio:
    """La guarda con flete / sin flete se reemplazó por un rótulo (2026-10-11)."""

    def test_compra_mas_nueva_sin_flete_gana_y_queda_rotulada(self) -> None:
        v = decidir_costo_vigente(_compra("100", _SEP_10, "adicional"), None, None)
        assert v is not None
        cf = sellar({COSTO_BASE_FIELD: "con_flete"}, v)
        assert cf[COSTO_BASE_FIELD] == "sin_flete"
        assert (cf[ORIGEN_FIELD], cf[FECHA_FIELD]) == ("compra", _SEP_10.isoformat())

    def test_envio_incluido_es_con_flete(self) -> None:
        v = decidir_costo_vigente(_compra("110", _SEP_10, "incluido"), None, None)
        assert v is not None and v.base == "con_flete"

    def test_envio_desconocido_borra_el_rotulo(self) -> None:
        v = decidir_costo_vigente(_compra("100", _SEP_10), None, None)
        assert v is not None
        assert COSTO_BASE_FIELD not in sellar({COSTO_BASE_FIELD: "con_flete"}, v)


class TestEdiciones:
    def test_registrar_y_leer(self) -> None:
        cf = registrar_edicion({"x": 1}, MANUAL_FIELD, Decimal("90"), _SEP_10)
        assert cf["x"] == 1
        assert leer_costo_fechado(cf, MANUAL_FIELD) == CostoFechado(Decimal("90"), _SEP_10)
        assert cf[ORIGEN_FIELD] == "manual"

    def test_lo_ilegible_es_ausencia(self) -> None:
        for malo in ("90", {"costo": "x", "fecha": "2026"}, {"costo": "0", "fecha": "2026-09-10"}):
            assert leer_costo_fechado({MANUAL_FIELD: malo}, MANUAL_FIELD) is None

    def test_reenviar_el_mismo_costo_no_es_una_edicion(self) -> None:
        assert custom_fields_tras_editar_costo({}, Decimal("90"), Decimal("90.00"), _SEP_10) is None

    def test_borrar_el_costo_quita_la_edicion(self) -> None:
        cf = registrar_edicion({}, MANUAL_FIELD, Decimal("90"), _SEP_10)
        nuevo = custom_fields_tras_editar_costo(cf, Decimal("90"), None, _SEP_10)
        assert nuevo is not None
        assert MANUAL_FIELD not in nuevo and ORIGEN_FIELD not in nuevo

    def test_el_cliente_no_puede_escribir_las_claves_del_costo(self) -> None:
        """Una edición «manual» falsificada con fecha futura le ganaría a toda
        compra: las claves las escribe sólo el servidor."""
        anteriores = registrar_edicion({}, CATALOGO_FIELD, Decimal("70"), _SEP_10)
        forjado = {
            "propio": "ok",
            MANUAL_FIELD: {"costo": "1", "fecha": "2099-01-01T00:00:00"},
            CATALOGO_FIELD: None,
        }
        r = conservar_claves_del_costo(anteriores, forjado)
        assert r["propio"] == "ok"
        assert MANUAL_FIELD not in r
        assert r[CATALOGO_FIELD] == anteriores[CATALOGO_FIELD]


class TestRevisionMismoDia:
    """Revisión M·1c-1 (P2): una compra «de hoy» trae sólo la fecha (00:00)."""

    def _compra_de_hoy(self, cargada_utc: datetime) -> CostoDeReferencia:
        return CostoDeReferencia(
            origen="compra",
            costo_unitario=Decimal("100"),
            fecha=datetime(2026, 9, 10, tzinfo=UTC),
            movement_id=uuid.uuid4(),
            cargada_en=cargada_utc,
        )

    def test_compra_cargada_despues_de_la_edicion_del_mismo_dia_gana(self) -> None:
        manual = CostoFechado(Decimal("90"), datetime(2026, 9, 10, 10, 0))
        # 18:00 UTC = 15:00 AR, después de la edición de las 10:00.
        ref = self._compra_de_hoy(datetime(2026, 9, 10, 18, 0, tzinfo=UTC))
        v = decidir_costo_vigente(ref, manual, None)
        assert v is not None and v.origen == "compra"

    def test_compra_cargada_antes_de_la_edicion_del_mismo_dia_pierde(self) -> None:
        manual = CostoFechado(Decimal("90"), datetime(2026, 9, 10, 10, 0))
        ref = self._compra_de_hoy(datetime(2026, 9, 10, 11, 0, tzinfo=UTC))  # 08:00 AR
        v = decidir_costo_vigente(ref, manual, None)
        assert v is not None and v.origen == "manual"

    def test_compra_de_un_dia_anterior_cargada_hoy_no_le_gana_a_la_edicion_de_hoy(
        self,
    ) -> None:
        manual = CostoFechado(Decimal("90"), datetime(2026, 9, 11, 10, 0))
        ref = self._compra_de_hoy(datetime(2026, 9, 11, 20, 0, tzinfo=UTC))
        v = decidir_costo_vigente(ref, manual, None)
        assert v is not None and v.origen == "manual"


class TestRevisionSinProcedencia:
    """Revisión M·1c-1 (P1): costo anterior a M·1c, sin fecha conocida."""

    def test_solo_la_compra_recien_registrada_y_mas_reciente_lo_reemplaza(self) -> None:
        ref = _compra("100", _SEP_10)
        assert ref.movement_id is not None
        assert (
            decidir_costo_vigente(
                ref, None, None, sin_procedencia=True, movimientos_nuevos={ref.movement_id}
            )
            is not None
        )

    def test_una_compra_vieja_no_lo_pisa(self) -> None:
        """Comprobante atrasado o alta de stock sin costo: la referencia es otra
        compra, no la recién registrada."""
        ref = _compra("30", datetime(2026, 6, 1))
        assert (
            decidir_costo_vigente(
                ref, None, None, sin_procedencia=True, movimientos_nuevos={uuid.uuid4()}
            )
            is None
        )
        assert decidir_costo_vigente(ref, None, None, sin_procedencia=True) is None


class TestRevisionCatalogo:
    """Revisión M·1c-1 (P2): el catálogo se fecha con la CARGA del archivo."""

    def test_releer_un_catalogo_viejo_no_le_gana_a_una_compra_posterior(self) -> None:
        cf = {ORIGEN_FIELD: "compra", FECHA_FIELD: "2026-09-10T00:00:00"}
        costo, nuevo = aplicar_costo_de_catalogo(
            cf, Decimal("150"), Decimal("100"), datetime(2026, 1, 5, 9, 0)
        )
        assert costo == Decimal("150")
        assert nuevo[ORIGEN_FIELD] == "compra"
        # La carga queda guardada igual (si se anula la compra, cuenta).
        assert leer_costo_fechado(nuevo, CATALOGO_FIELD) == CostoFechado(
            Decimal("100"), datetime(2026, 1, 5, 9, 0)
        )

    def test_catalogo_mas_nuevo_manda(self) -> None:
        cf = {ORIGEN_FIELD: "compra", FECHA_FIELD: "2026-09-10T00:00:00"}
        costo, nuevo = aplicar_costo_de_catalogo(
            cf, Decimal("150"), Decimal("100"), datetime(2026, 9, 20, 9, 0)
        )
        assert costo == Decimal("100") and nuevo[ORIGEN_FIELD] == "catalogo"

    def test_producto_sin_procedencia_toma_el_catalogo_como_antes(self) -> None:
        costo, nuevo = aplicar_costo_de_catalogo(
            {}, Decimal("150"), Decimal("100"), datetime(2026, 1, 5)
        )
        assert costo == Decimal("100") and nuevo[ORIGEN_FIELD] == "catalogo"

    def test_catalogo_y_compra_del_mismo_dia_deciden_igual_que_el_recalculo(self) -> None:
        """Revisión M·1c-1 (2ª vuelta): catálogo cargado a las 14:00 y compra con
        fecha del mismo día REGISTRADA a las 16:00. El recálculo elige la compra;
        el catálogo releído no puede elegir otra cosa."""
        ref = CostoDeReferencia(
            origen="compra",
            costo_unitario=Decimal("150"),
            fecha=datetime(2026, 1, 10, tzinfo=UTC),
            movement_id=uuid.uuid4(),
            cargada_en=datetime(2026, 1, 10, 19, 0, tzinfo=UTC),  # 16:00 AR
        )
        catalogo = CostoFechado(Decimal("100"), datetime(2026, 1, 10, 14, 0))
        elegido = decidir_costo_vigente(ref, None, catalogo)
        assert elegido is not None and elegido.origen == "compra"

        cf = sellar({}, elegido)
        costo, _ = aplicar_costo_de_catalogo(cf, Decimal("150"), Decimal("100"), catalogo.fecha)
        assert costo == Decimal("150")
