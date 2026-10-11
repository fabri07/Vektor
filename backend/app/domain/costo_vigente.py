"""Costo vigente del producto: cuál de las fuentes manda hoy (M·1c).

Puro: sin sesión ni ORM. ``products.unit_cost_ars`` deja de ser «el último que
escribió» y pasa a ser la fuente MÁS RECIENTE POR FECHA entre tres:

- **la última compra** (o el stock inicial valorizado), elegida por M·1a por
  fecha de NEGOCIO: una compra atrasada cargada hoy no le gana a una más nueva;
- **la última edición manual** (PATCH, chat, vincular desde «Otros»), con la
  hora en que se hizo;
- **la última carga de catálogo** (hoja de precios), con la fecha de la carga.

Decisiones del dueño (2026-10-10):

- **2.** La edición manual gana hasta que llega una compra con fecha de negocio
  **posterior** a la edición. En un empate exacto gana lo manual: es lo único que
  el usuario dijo explícitamente.
- **3.** El costo de catálogo es una **estimación**: una compra posterior lo
  reemplaza, y no cuenta como compra confirmada.
- **Guarda con flete / sin flete (2026-10-11):** se reemplaza por un rótulo. El
  costo es el de la última compra aunque no incluya el envío, y
  ``_vektor_costo_base`` dice si lo incluye (``con_flete``/``sin_flete``) o lo
  borra si no se sabe. El margen (M·1d/M·2) lo trata como base no comparable en
  vez de esconderlo.

La edición manual y la de catálogo se GUARDAN aparte en ``custom_fields`` (claves
reservadas ``_vektor_``, que el usuario no puede mapear: salen de headers y nunca
empiezan con guión bajo). Así, si se anula la compra que había ganado, vuelve a
valer la edición: no se pierde lo que el usuario dijo.

Todas las fechas son hora argentina SIN zona (como ``now_ar_naive``). La de una
compra llega rotulada UTC desde ``occurred_at`` y se le saca el rótulo: ya es
hora argentina (M·1b).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal

from app.domain.costo_de_referencia import CostoDeReferencia
from app.domain.purchase_cost import CON_FLETE, COSTO_BASE_FIELD, SIN_FLETE

OrigenVigente = Literal["compra", "stock_inicial", "manual", "catalogo"]

#: Origen y fecha del costo que quedó en ``unit_cost_ars`` (para mostrarlo).
ORIGEN_FIELD: Final = "_vektor_costo_origen"
FECHA_FIELD: Final = "_vektor_costo_fecha"
#: Última edición manual y última carga de catálogo: ``{"costo", "fecha"}``.
MANUAL_FIELD: Final = "_vektor_costo_manual"
CATALOGO_FIELD: Final = "_vektor_costo_catalogo"

#: Claves que viajan con el costo: quien restaure o reemplace ``custom_fields``
#: (PATCH, undo) las tiene que conservar juntas.
CLAVES_DEL_COSTO: Final[tuple[str, ...]] = (
    COSTO_BASE_FIELD,
    ORIGEN_FIELD,
    FECHA_FIELD,
    MANUAL_FIELD,
    CATALOGO_FIELD,
)


@dataclass(frozen=True)
class CostoFechado:
    costo: Decimal
    fecha: datetime


@dataclass(frozen=True)
class CostoVigente:
    costo: Decimal
    origen: OrigenVigente
    fecha: datetime
    #: ``con_flete`` / ``sin_flete``, o ``None`` = no se sabe (se borra la clave).
    base: str | None


def _sin_zona(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None)


def leer_costo_fechado(custom_fields: Mapping[str, Any] | None, clave: str) -> CostoFechado | None:
    """Lee ``{"costo", "fecha"}`` de una clave reservada; lo ilegible es ausencia."""
    valor = (custom_fields or {}).get(clave)
    if not isinstance(valor, Mapping):
        return None
    try:
        costo = Decimal(str(valor["costo"]))
        fecha = _sin_zona(datetime.fromisoformat(str(valor["fecha"])))
    except (KeyError, ValueError, InvalidOperation):
        return None
    if not costo.is_finite() or costo <= 0:
        return None
    return CostoFechado(costo=costo, fecha=fecha)


def _base_de_la_compra(envio: str) -> str | None:
    if envio == "incluido":
        return CON_FLETE
    if envio in ("adicional", "no_aplica"):
        return SIN_FLETE
    return None


def decidir_costo_vigente(
    referencia: CostoDeReferencia,
    manual: CostoFechado | None,
    catalogo: CostoFechado | None,
) -> CostoVigente | None:
    """La fuente más reciente por fecha, o ``None`` si no hay ninguna fechada.

    ``None`` significa «no tocar»: un producto sin compra, sin edición y sin
    catálogo registrados conserva el costo que tenga (dato previo a M·1c).
    Empates: manual > catálogo > compra.
    """
    candidatos: list[tuple[datetime, int, CostoVigente]] = []
    origen_compra: OrigenVigente | None = (
        "compra"
        if referencia.origen == "compra"
        else "stock_inicial"
        if referencia.origen == "stock_inicial"
        else None
    )
    if (
        origen_compra is not None
        and referencia.costo_unitario is not None
        and referencia.fecha is not None
    ):
        fecha = _sin_zona(referencia.fecha)
        candidatos.append(
            (
                fecha,
                0,
                CostoVigente(
                    costo=referencia.costo_unitario,
                    origen=origen_compra,
                    fecha=fecha,
                    base=_base_de_la_compra(referencia.envio),
                ),
            )
        )
    if catalogo is not None:
        candidatos.append(
            (catalogo.fecha, 1, CostoVigente(catalogo.costo, "catalogo", catalogo.fecha, None))
        )
    if manual is not None:
        candidatos.append(
            (manual.fecha, 2, CostoVigente(manual.costo, "manual", manual.fecha, None))
        )
    if not candidatos:
        return None
    return max(candidatos, key=lambda c: (c[0], c[1]))[2]


def sellar(custom_fields: Mapping[str, Any] | None, vigente: CostoVigente) -> dict[str, Any]:
    """``custom_fields`` con el origen, la fecha y la base del costo vigente."""
    nuevos = {**(custom_fields or {})}
    nuevos[ORIGEN_FIELD] = vigente.origen
    nuevos[FECHA_FIELD] = vigente.fecha.isoformat()
    if vigente.base is None:
        nuevos.pop(COSTO_BASE_FIELD, None)
    else:
        nuevos[COSTO_BASE_FIELD] = vigente.base
    return nuevos


def registrar_edicion(
    custom_fields: Mapping[str, Any] | None,
    clave: str,
    costo: Decimal,
    fecha: datetime,
) -> dict[str, Any]:
    """Guarda una edición manual o de catálogo y la deja como costo vigente.

    Es «ahora»: siempre es la fuente más reciente, así que también sella el
    origen. El envío de un costo tipeado no se sabe: se borra la base.
    """
    if clave not in (MANUAL_FIELD, CATALOGO_FIELD):
        raise ValueError(f"clave de edición desconocida: {clave!r}")
    origen: OrigenVigente = "manual" if clave == MANUAL_FIELD else "catalogo"
    fecha = _sin_zona(fecha)
    nuevos = {**(custom_fields or {})}
    nuevos[clave] = {"costo": str(costo), "fecha": fecha.isoformat()}
    return sellar(nuevos, CostoVigente(costo, origen, fecha, None))


def conservar_claves_del_costo(
    anteriores: Mapping[str, Any] | None, nuevos: Mapping[str, Any] | None
) -> dict[str, Any]:
    """``nuevos`` con las claves del costo tomadas SIEMPRE de ``anteriores``.

    Un PATCH que manda ``custom_fields`` completo no puede borrar la procedencia
    del costo, ni escribirla: una edición «manual» con fecha futura falsificada
    le ganaría a toda compra. Esas claves sólo las escribe el servidor.
    """
    resultado = {k: v for k, v in (nuevos or {}).items() if k not in CLAVES_DEL_COSTO}
    for clave in CLAVES_DEL_COSTO:
        if anteriores and clave in anteriores:
            resultado[clave] = anteriores[clave]
    return resultado


def snapshot_claves_del_costo(custom_fields: Mapping[str, Any] | None) -> dict[str, Any]:
    """Las claves del costo para el ``before`` de un ledger (``None`` = ausente).

    El undo (``_ledger_restore``) las devuelve JUNTO con ``unit_cost_ars``: una
    carga de catálogo borrada no puede dejar su edición guardada, porque el
    próximo recálculo la volvería a elegir.
    """
    cf = custom_fields or {}
    return {clave: cf.get(clave) for clave in CLAVES_DEL_COSTO}


def quitar_edicion_manual(custom_fields: Mapping[str, Any] | None) -> dict[str, Any]:
    """El usuario borró el costo (o lo puso en 0): no hay costo vigente que sellar.

    Sin edición guardada, el próximo recálculo vuelve a la compra si la hay.
    """
    nuevos = {**(custom_fields or {})}
    for clave in (MANUAL_FIELD, ORIGEN_FIELD, FECHA_FIELD, COSTO_BASE_FIELD):
        nuevos.pop(clave, None)
    return nuevos


def custom_fields_tras_editar_costo(
    custom_fields: Mapping[str, Any] | None,
    costo_anterior: Decimal | None,
    costo_nuevo: Decimal | None,
    ahora: datetime,
) -> dict[str, Any] | None:
    """Lo que una edición MANUAL del costo deja en ``custom_fields``.

    ``None`` = no cambió el costo: no se sella nada. Un formulario que reenvía el
    mismo costo al editar el nombre no es una edición del costo, y sellarla
    «ahora» le ganaría a cualquier compra anterior.
    """
    if costo_nuevo == costo_anterior:
        return None
    if costo_nuevo is None or costo_nuevo <= 0:
        return quitar_edicion_manual(custom_fields)
    return registrar_edicion(custom_fields, MANUAL_FIELD, costo_nuevo, ahora)
