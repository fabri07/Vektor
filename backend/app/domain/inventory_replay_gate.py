"""F-H3.d.3 — qué ventas de un archivo NO tienen stock que las respalde.

Decide, sin sesión ni ORM, cuáles filas de venta se quedan sin unidades cuando se
reproduce la historia que declara el archivo. Sólo corre para las hojas que el
usuario marcó ``historical_replay``: con el default (``informational``) el archivo
entra entero y esto no se ejecuta.

**Por qué existir, en vez de simplemente dejar el stock en negativo.** Las dos
alternativas obvias están mal por el mismo motivo: el descuento y el movimiento
que lo registra dejarían de decir lo mismo. Clampeando a cero se descuenta menos
de lo que dice el movimiento, y la reversa por borrado —que revierte el
movimiento entero— infla el stock. Dejándolo ir a negativo el número queda
exacto, pero es un inventario que nadie tiene. La salida es no importar la venta
que no se puede respaldar: va a "Otros", el usuario carga el inventario que falta
y la registra desde ahí.

**El orden es por FECHA, no por solapa.** Si se recorriera en el orden del Excel,
cuál venta se rechaza dependería de en qué hoja la puso quien armó el archivo:
dos libros con las mismas ventas y las solapas al revés darían resultados
distintos. Con 10 unidades y ventas de 6 el 03/03 y 6 el 10/03, la que se queda
afuera es la del 10/03 en los dos casos.

No decide identidad: cada fila llega con su producto ya resuelto (F-H1/F-H2). El
saldo de apertura tampoco se calcula acá — lo trae el caller, que es el único que
sabe leer la DB.

**Dónde NO se puede gatear todavía, y por qué se rechaza en vez de seguir.** En un
archivo de UNA sola tabla donde las mismas filas dan la venta *y* dan de alta el
producto, no hay saldo contra el cual evaluar: el stock que respaldaría a esas
ventas lo está cargando el propio archivo, en la misma pasada. El camino
multi-hoja no tiene el problema —recorre catálogos → compras → ventas y calcula el
gate al llegar a la primera hoja de ventas—, así que esto es un límite del archivo
plano, no del dominio. Es **transitorio**: se levanta el día que el import prepare
identidades y saldos provisionales en memoria antes de construir los movimientos
(ver `docs/plans/ingestion-mapping-overhaul.md`). Hasta entonces el confirm lo
rechaza antes de tomar el lease: elegir ``historical_replay`` es pedir que Véktor
valide cada venta contra el stock, y degradar eso en silencio a "importé todo sin
validar nada" es peor que no dejar confirmar.
"""

from __future__ import annotations

from collections.abc import Hashable
from dataclasses import dataclass
from datetime import date
from uuid import UUID

#: Motivo del rechazo en la traza (`STAGE_REJECT`) y en el contador del respaldo.
#: Uno solo para los dos lugares: si el confirm y el importador nombraran distinto
#: la misma situación, buscarla en `pipeline_events` daría la mitad de los casos.
MOTIVO_REPLAY_NO_GATEABLE = "replay_no_gateable"

#: Lo que se le dice al usuario. Explica QUÉ pasa con su archivo y ofrece las dos
#: salidas reales, en vez de nombrar el modo técnico que eligió.
#:
#: La primera salida es la que resuelve el caso en dos pasos y por eso va primero:
#: el replay del panel **se recalcula contra el stock del momento**, así que ahí sí
#: hay saldo para validar. Lo único que se pierde respecto de gatear al confirmar
#: es dónde queda la venta que no se puede respaldar — por el panel entra a los
#: libros y su descuento queda pendiente, en vez de irse a "Otros". Decirlo es
#: parte del mensaje: sin eso, mandar a reestructurar el archivo suena a que no
#: hay otro camino, y sí lo hay.
MENSAJE_REPLAY_NO_GATEABLE = (
    "En la hoja «{hoja}»: el archivo da de alta productos y además trae "
    "movimientos históricos en las mismas filas. Véktor puede analizarlo y "
    "mostrarte el impacto, pero todavía no puede validar cada venta contra el "
    "stock en una sola confirmación: ese stock lo está cargando este mismo "
    "archivo. Importalo sin que las ventas modifiquen el inventario y después "
    "aplicá el histórico desde el panel de impacto —ahí el cálculo corre contra "
    "el stock ya cargado—, teniendo en cuenta que una venta sin respaldo va a "
    "entrar igual y su descuento va a quedar pendiente. Si preferís que esas "
    "ventas ni entren, separá el saldo inicial de los movimientos en hojas "
    "distintas."
)


def replay_no_gateable(
    *,
    hoja_unica: bool,
    pide_replay: bool,
    da_de_alta_productos: bool,
    trae_ventas: bool,
) -> bool:
    """¿Este confirm pide un replay que no se puede validar?

    Los cuatro datos los arma cada caller con lo que tiene a mano, y ahí está la
    única diferencia entre los dos: el confirm los deriva del mapeo declarado y de
    las señales del parseo —antes del lease, que es donde un rechazo no deja nada a
    medias—, y el importador de las columnas ya resueltas, que incluyen las
    autodetectadas sin mapeo explícito. Por eso el importador conserva su propio
    respaldo: puede ver una alta de productos que el confirm no llegó a ver.
    """
    return hoja_unica and pide_replay and da_de_alta_productos and trae_ventas


@dataclass(frozen=True)
class ReplayRow:
    """Una fila de venta candidata a descontar stock, ya identificada."""

    #: Con qué la identifica el caller. Al confirmar es ``(context_id, índice DENTRO
    #: de su hoja)`` —la misma clave que el ancla de idempotencia, así reordenar para
    #: evaluar no puede cambiarla—; al aplicar el replay es el id de la venta, que
    #: para entonces ya existe. Genérica porque los dos momentos identifican la misma
    #: fila con lo que tienen a mano, y no vale la pena una segunda implementación
    #: del gate para eso.
    key: Hashable
    product_id: UUID
    day: date
    qty: int
    #: Posición de la hoja en el archivo. Desempata sólo entre filas de la MISMA
    #: fecha, para que el resultado no dependa del orden en que se recorran las hojas.
    #: Sin hojas que desempatar (el apply trabaja sobre ventas ya persistidas) queda
    #: en 0 y el desempate lo hace el orden de llegada.
    sheet_rank: int = 0


@dataclass(frozen=True)
class UnbackedRow:
    """Una venta que se queda sin unidades, con el número que lo explica."""

    key: Hashable
    product_id: UUID
    day: date
    qty: int
    #: Unidades que quedaban cuando llegó su turno. Siempre < ``qty``.
    disponible: int


def rows_without_stock_backing(
    rows: list[ReplayRow],
    saldo_por_producto: dict[UUID, int],
) -> list[UnbackedRow]:
    """Las filas que no se pueden respaldar, en orden cronológico.

    ``saldo_por_producto`` es lo que hay ANTES de aplicar estas ventas — o sea, el
    stock ya con los catálogos y las compras del archivo adentro (que sí se
    aplican al confirmar, **V16**). Un producto ausente del dict cuenta como 0.

    Una fila rechazada **no consume** stock: el saldo que ve la siguiente es el
    mismo. Si no, una venta grande imposible de cubrir se llevaría puestas a todas
    las chicas que venían después y sí entraban.
    """
    disponible = dict(saldo_por_producto)
    sin_respaldo: list[UnbackedRow] = []
    # `sorted` es estable: a igual fecha y hoja, el desempate final es el orden en
    # que el caller las entregó, que es el orden de fila del archivo. No se ordena
    # por la clave — es opaca (una tupla al confirmar, un UUID al aplicar) y
    # ordenar por ella haría que la decisión dependa de un id aleatorio.
    for row in sorted(rows, key=lambda r: (r.day, r.sheet_rank)):
        if row.qty <= 0:
            continue
        saldo = disponible.get(row.product_id, 0)
        if row.qty > saldo:
            sin_respaldo.append(
                UnbackedRow(
                    key=row.key,
                    product_id=row.product_id,
                    day=row.day,
                    qty=row.qty,
                    disponible=saldo,
                )
            )
            continue
        disponible[row.product_id] = saldo - row.qty
    return sin_respaldo
