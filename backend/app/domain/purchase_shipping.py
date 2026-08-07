"""F-H6.b — el envío de una compra se cobra una vez, aunque figure en cada línea.

Un libro de compras trae una fila por artículo y, muy seguido, una columna «Envío»
con la MISMA cifra repetida en todas las líneas del mismo remito: el flete es uno
solo y la planilla lo arrastra. Importar esa columna fila por fila convierte un
envío de $2.000 en $20.000 de logística.

**La agrupación es la condición, no un detalle.** Véktor sólo puede afirmar que
dos filas comparten un envío si el archivo dice que pertenecen al mismo
comprobante: mismo proveedor y mismo número. Sin eso, un 2.000 repetido diez veces
es indistinguible de diez envíos de 2.000 — y elegir uno de los dos sería inventar
un dato contable. Por eso, sin identidad de comprobante, la columna no genera
ningún gasto y el import lo dice (regla no-invention: ver `CLAUDE.md`).

Sin sesión ni ORM: recibe las filas ya leídas y devuelve qué cobrar. Quién lo
persiste —y como qué categoría— es del importador.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

#: Clave de un comprobante: proveedor + número, ya normalizados por el caller.
#: El proveedor entra porque dos proveedores pueden emitir el mismo número.
ComprobanteKey = tuple[str, str]


@dataclass(frozen=True)
class ShippingLine:
    """El costo de envío que declara UNA fila del archivo."""

    #: Índice de la fila dentro de su hoja. Sólo para poder señalarla.
    row_index: int
    #: Proveedor y número de comprobante normalizados. Cualquiera vacío = la fila
    #: no se puede agrupar.
    supplier: str
    invoice: str
    amount: Decimal


@dataclass
class ShippingCharge:
    """Un envío a cobrar UNA vez, con las filas que lo declararon."""

    supplier: str
    invoice: str
    amount: Decimal
    row_indexes: list[int] = field(default_factory=list)

    @property
    def repetido_en(self) -> int:
        """En cuántas filas figuraba la misma cifra. 1 = no estaba repetida."""
        return len(self.row_indexes)


@dataclass
class ShippingPlan:
    """Qué se cobra, qué no se puede cobrar y por qué."""

    charges: list[ShippingCharge] = field(default_factory=list)
    #: Filas con envío que NO se pueden agrupar (sin proveedor o sin comprobante).
    #: No generan gasto: no hay forma de saber si son un envío o varios.
    sin_identidad: list[int] = field(default_factory=list)
    #: Filas del mismo comprobante con cifras DISTINTAS de envío. Cada cifra se
    #: cobra una vez —son cargos distintos, no una repetición—, y se avisa porque
    #: en un comprobante real es raro y suele ser un error de la planilla.
    cifras_distintas: list[ComprobanteKey] = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return sum((c.amount for c in self.charges), Decimal("0"))


def plan_shipping_charges(lines: list[ShippingLine]) -> ShippingPlan:
    """Agrupa los envíos declarados y devuelve qué cobrar una sola vez.

    Regla, en una línea: **una cifra por comprobante se cobra una vez**.

    - Misma cifra repetida en N filas del mismo comprobante → UN cargo.
    - Cifras distintas en el mismo comprobante → un cargo por cifra, y se avisa:
      puede ser un flete y un seguro, o puede ser que la planilla tenga el total
      en una fila y el prorrateo en las otras. Véktor no elige por el usuario.
    - Fila sin proveedor o sin número de comprobante → no se cobra nada y se
      reporta. Ver el docstring del módulo.

    El orden de `charges` sigue el de aparición en el archivo: dos corridas sobre
    el mismo archivo tienen que producir los mismos gastos, en el mismo orden.
    """
    plan = ShippingPlan()
    por_comprobante: dict[ComprobanteKey, dict[Decimal, ShippingCharge]] = {}
    orden: list[tuple[ComprobanteKey, Decimal]] = []

    for line in lines:
        if line.amount <= 0:
            # Una celda vacía o en cero no es un envío. No se reporta como
            # problema: la mayoría de las filas de un libro no traen flete.
            continue
        if not line.supplier or not line.invoice:
            plan.sin_identidad.append(line.row_index)
            continue
        key = (line.supplier, line.invoice)
        cifras = por_comprobante.setdefault(key, {})
        cargo = cifras.get(line.amount)
        if cargo is None:
            cargo = ShippingCharge(
                supplier=line.supplier, invoice=line.invoice, amount=line.amount
            )
            cifras[line.amount] = cargo
            orden.append((key, line.amount))
        cargo.row_indexes.append(line.row_index)

    for key, monto in orden:
        plan.charges.append(por_comprobante[key][monto])
    plan.cifras_distintas = [k for k, cifras in por_comprobante.items() if len(cifras) > 1]
    return plan
