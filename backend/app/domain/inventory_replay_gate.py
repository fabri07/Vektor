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

**F-F — las compras del propio archivo entran como créditos datados.** Antes esto
recibía un saldo ESTÁTICO y por eso un archivo de una sola tabla que declaraba el
stock *y* las ventas no se podía gatear: el stock que respaldaba a esas ventas lo
cargaba el mismo archivo, en la misma pasada, y el confirm lo rechazaba. Con los
créditos como eventos con fecha, ese caso se evalúa igual que cualquier otro —los
"saldos provisionales en memoria" que ese rechazo esperaba no son más que esto— y
además se gana la propiedad que el usuario pidió: **una compra del 10/03 ya no
respalda una venta del 03/03**. Antes sí lo hacía, porque toda compra del archivo
estaba metida en el saldo inicial sin fecha.

**El saldo que se pasa es el PREVIO al archivo.** Si el caller pasara el stock de
hoy —que en el camino multi-hoja ya incluye las compras que el import aplicó— y
además los créditos, cada compra se contaría dos veces y respaldaría el doble de
ventas de las que puede. Es el mismo cuidado que documenta
``domain/inventory_projection``, y por eso los dos leen la misma apertura.

**El ancla del catálogo no es un crédito.** Un catálogo declara un ABSOLUTO sin
fecha de negocio: entra como saldo de apertura, antes de todos los eventos.
Tratarlo como un crédito datado lo pondría después de las ventas anteriores a la
fecha del import y las marcaría como sin respaldo — el falso positivo que
``inventory_temporal_service`` documenta y evita.
"""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from dataclasses import dataclass
from datetime import date
from uuid import UUID


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
class CreditEvent:
    """Unidades que ENTRAN en una fecha: una compra de mercadería del archivo.

    Es la contracara de ``ReplayRow``. No lleva ``key`` porque a nadie le importa
    cuál crédito respaldó cuál venta: lo único que se reporta es la venta que se
    quedó sin unidades.
    """

    product_id: UUID
    day: date
    qty: int
    #: Mismo rol que en ``ReplayRow``: desempata entre créditos de la misma fecha.
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
    credits: Sequence[CreditEvent] = (),
) -> list[UnbackedRow]:
    """Las filas que no se pueden respaldar, evaluadas en orden cronológico.

    ``saldo_por_producto`` es el stock PREVIO al archivo (o el absoluto que declara
    su catálogo, que pisa al previo). Un producto ausente del dict cuenta como 0.
    ``credits`` son las unidades que el archivo hace entrar, con su fecha: las
    compras de mercadería. Ver el docstring del módulo — pasar un saldo que ya las
    incluya y además pasarlas acá las contaría dos veces.

    Una fila rechazada **no consume** stock: el saldo que ve la siguiente es el
    mismo. Si no, una venta grande imposible de cubrir se llevaría puestas a todas
    las chicas que venían después y sí entraban.
    """
    disponible = dict(saldo_por_producto)
    sin_respaldo: list[UnbackedRow] = []
    # Un solo flujo cronológico con créditos y débitos intercalados. La clave de
    # orden es (fecha, crédito antes que débito, hoja, orden de llegada) — el mismo
    # criterio que `inventory_temporal_service.replay_timeline`, para que una compra
    # del mismo día que la venta la respalde en vez de generar un falso negativo.
    #
    # Se ordena una lista de TUPLAS y nunca el evento: la clave de `ReplayRow` es
    # opaca (una tupla al confirmar, un UUID al aplicar) y ordenar por ella haría
    # que la decisión dependa de un id aleatorio. El índice de llegada preserva el
    # orden de fila del archivo dentro del mismo día y la misma hoja.
    eventos: list[tuple[date, int, int, int]] = [
        *((c.day, 0, c.sheet_rank, i) for i, c in enumerate(credits)),
        *((r.day, 1, r.sheet_rank, i) for i, r in enumerate(rows)),
    ]
    eventos.sort()
    for _, es_venta, _, indice in eventos:
        if not es_venta:
            credito = credits[indice]
            if credito.qty <= 0:
                continue
            disponible[credito.product_id] = (
                disponible.get(credito.product_id, 0) + credito.qty
            )
            continue
        row = rows[indice]
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
