"""F-H6.d — qué líneas componen una compra, y cuánto costo compartido tienen.

Repartir el flete de un comprobante entre sus líneas exige una pregunta previa que
hasta acá nadie hacía en un solo lugar: **cuáles son sus líneas**. `purchase_shipping`
la respondía de paso, mientras decidía qué cobrar; `build_line_costs` recibía las
líneas ya elegidas y confiaba en el caller. El resultado es que el cobro del flete y
el reparto del costo podían agrupar distinto sobre el mismo archivo.

El docstring de `identidad_de_comprobante` ya nombra a los tres que tienen que
responder lo mismo —la agrupación, la validación previa al lease y el preview— y
avisa qué pasa si divergen: «la pantalla ofrece repartir un costo entre líneas que
el importador después no va a agrupar, y el usuario ve un reparto que no ocurrió».
Este módulo es ese único lugar. Por eso **importa** `identidad_de_comprobante` y
`plan_shipping_charges` en vez de reimplementar el criterio: si el colapso de la
cifra repetida se calculara acá por segunda vez, las dos copias podrían divergir y
volveríamos al problema que el módulo vino a resolver.

**Un grupo distribuible es más que un grupo identificable.** Alcanza con que el
archivo permita formar la clave para *agrupar*, pero para *repartir* hace falta
además una cifra compartida inequívoca. Dos cifras distintas en el mismo
comprobante pueden ser un flete y un seguro, o el total en una fila y el prorrateo
en las otras; repartir la suma como si fuera una sola cifra sería elegir por el
usuario, que es justo lo que la fase entera evita.

Sin sesión, sin ORM y sin leer el archivo: recibe valores ya parseados.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.domain.purchase_shipping import (
    SIN_COMPROBANTE,
    SIN_COMPROBANTE_UNA_POR_HOJA,
    ComprobanteKey,
    ShippingLine,
    identidad_de_comprobante,
    plan_shipping_charges,
)

#: Por qué un grupo no admite reparto. Son motivos de NEGOCIO, no fallas: cada uno
#: describe un dato que el archivo no trae, y el import los reporta tal cual.
MOTIVO_SIN_IDENTIDAD = "sin_identidad_de_comprobante"
MOTIVO_CIFRAS_DISTINTAS = "cifras_distintas_de_envio"
MOTIVO_SIN_ENVIO_COMPARTIDO = "sin_envio_compartido"


@dataclass(frozen=True)
class GroupLine:
    """Lo que una fila de compra declara sobre a qué operación pertenece.

    ``subtotal`` es el peso de la línea en la compra: es la base contra la cual se
    prorratea, y sale del monto ya resuelto por F-H4. ``shipping`` es la cifra de
    envío del COMPROBANTE que esta fila declara —la que suele venir repetida en
    todas las líneas del mismo remito—, no el envío asignado a la línea, que es
    otro target y no se reparte porque ya viene repartido.
    """

    row_index: int
    supplier: str
    invoice: str
    subtotal: Decimal
    shipping: Decimal = Decimal("0")


@dataclass
class PurchaseGroup:
    """Las líneas de una compra y el costo compartido que les corresponde."""

    #: ``None`` cuando el archivo no permite formar la clave. Esas filas siguen
    #: siendo un grupo —hay que poder contarlas y mostrarlas— pero no reparten.
    key: ComprobanteKey | None
    row_indexes: list[int] = field(default_factory=list)
    subtotal: Decimal = Decimal("0")
    #: Cifra compartida YA COLAPSADA: repetida en diez filas, llega acá una vez.
    shared_shipping: Decimal = Decimal("0")
    distribuible: bool = False
    motivo_no_distribuible: str | None = None


@dataclass
class PurchaseGroupPlan:
    """Los grupos de una hoja, en el orden en que aparecen en el archivo."""

    groups: list[PurchaseGroup] = field(default_factory=list)

    def by_row(self) -> dict[int, PurchaseGroup]:
        """Índice por fila, que es como lo consume el importador."""
        return {row: group for group in self.groups for row in group.row_indexes}

    @property
    def distribuibles(self) -> list[PurchaseGroup]:
        return [g for g in self.groups if g.distribuible]


def build_purchase_groups(
    lines: list[GroupLine],
    *,
    sin_comprobante: str | None = None,
) -> PurchaseGroupPlan:
    """Agrupa las líneas por comprobante y resuelve su costo compartido.

    ``sin_comprobante`` es la MISMA decisión del usuario que gobierna el cobro del
    flete (`plan_shipping_charges`), y se reenvía sin interpretarla de nuevo. Sólo
    ``una_por_hoja`` habilita reparto sobre filas sin comprobante: ahí el usuario
    declaró que la hoja entera es una operación, que es la confirmación explícita
    que el reparto necesita. ``una_por_fila`` declara lo contrario —cada fila trae
    su propio flete—, así que no hay nada compartido que repartir.

    El orden de los grupos sigue el de aparición: dos corridas sobre el mismo
    archivo tienen que dar el mismo reparto, y el resto de la cadena (el centavo
    de redondeo, la traza) depende de que este orden sea estable.
    """
    por_hoja = sin_comprobante == SIN_COMPROBANTE_UNA_POR_HOJA
    pseudo: ComprobanteKey = (SIN_COMPROBANTE, SIN_COMPROBANTE)

    # El colapso de la cifra repetida lo resuelve `plan_shipping_charges`, que es
    # quien también decide qué se cobra. Calcularlo acá de nuevo es exactamente la
    # divergencia que este módulo existe para impedir.
    charges = plan_shipping_charges(
        [
            ShippingLine(
                row_index=line.row_index,
                supplier=line.supplier,
                invoice=line.invoice,
                amount=line.shipping,
            )
            for line in lines
        ],
        sin_comprobante=sin_comprobante,
    )
    envios: dict[ComprobanteKey, list[Decimal]] = {}
    for charge in charges.charges:
        clave: ComprobanteKey = (
            (charge.supplier, charge.invoice)
            if identidad_de_comprobante(charge.supplier, charge.invoice) is not None
            else pseudo
        )
        envios.setdefault(clave, []).append(charge.amount)

    grupos: dict[ComprobanteKey, PurchaseGroup] = {}
    orden: list[ComprobanteKey] = []
    for line in lines:
        identidad = identidad_de_comprobante(line.supplier, line.invoice)
        clave = identidad if identidad is not None else pseudo
        grupo = grupos.get(clave)
        if grupo is None:
            grupo = PurchaseGroup(key=identidad if identidad is not None else None)
            grupos[clave] = grupo
            orden.append(clave)
        grupo.row_indexes.append(line.row_index)
        grupo.subtotal += line.subtotal

    for clave in orden:
        grupo = grupos[clave]
        cifras = envios.get(clave, [])
        identificable = grupo.key is not None or por_hoja
        if not identificable:
            # Un 2.000 repetido diez veces es indistinguible de diez envíos de
            # 2.000. Sin identidad no se reparte, igual que no se cobra.
            grupo.motivo_no_distribuible = MOTIVO_SIN_IDENTIDAD
        elif len(cifras) > 1:
            grupo.motivo_no_distribuible = MOTIVO_CIFRAS_DISTINTAS
        elif not cifras:
            grupo.motivo_no_distribuible = MOTIVO_SIN_ENVIO_COMPARTIDO
        else:
            grupo.shared_shipping = cifras[0]
            grupo.distribuible = True

    return PurchaseGroupPlan(groups=[grupos[clave] for clave in orden])
