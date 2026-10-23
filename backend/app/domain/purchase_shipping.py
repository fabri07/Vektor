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

**El usuario tiene dos salidas, y son explícitas.** Que Véktor no pueda deducirlo
no significa que el dato se pierda: quien armó la planilla sabe si ese flete es
uno solo o varios, y puede declararlo por hoja (`SIN_COMPROBANTE_UNA_POR_HOJA` /
`SIN_COMPROBANTE_UNA_POR_FILA`). Lo que nunca pasa es que Véktor elija por él —
por eso no hay default: sin decisión, no se cobra.

Sin sesión ni ORM: recibe las filas ya leídas y devuelve qué cobrar. Quién lo
persiste —y como qué categoría— es del importador.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

#: Clave de un comprobante: proveedor + número, ya normalizados por el caller.
#: El proveedor entra porque dos proveedores pueden emitir el mismo número.
ComprobanteKey = tuple[str, str]

#: Qué hacer con las filas que traen envío y NO tienen comprobante. Sin decisión
#: del usuario no se cobra nada: ver el docstring del módulo.
#:
#: - `una_por_hoja`: la hoja ES el comprobante. Cada cifra distinta se cobra una
#:   vez —misma regla que dentro de un comprobante—, así que un archivo con dos
#:   fletes legítimos no queda sin salida y una repetición no se suma.
#: - `una_por_fila`: cada fila trae su propio flete. Diez filas de 2.000 son
#:   $20.000, que es exactamente lo que el usuario está declarando.
SIN_COMPROBANTE_UNA_POR_HOJA = "una_por_hoja"
SIN_COMPROBANTE_UNA_POR_FILA = "una_por_fila"
SHIPPING_FALLBACKS: frozenset[str] = frozenset(
    {SIN_COMPROBANTE_UNA_POR_HOJA, SIN_COMPROBANTE_UNA_POR_FILA}
)

#: Marca de un cargo que NO salió de un comprobante, sino de la decisión del
#: usuario. El importador la usa para redactar la descripción del gasto: decir
#: «comprobante ''» sería peor que decir que no lo tiene.
SIN_COMPROBANTE = ""


def identidad_de_comprobante(supplier: str, invoice: str) -> ComprobanteKey | None:
    """La clave del comprobante, o ``None`` si el archivo no permite formarla.

    **Hacen falta los dos.** El número solo no alcanza: dos proveedores emiten
    «Factura 0001» el mismo mes, y agruparlos cobraría un flete y perdería el
    otro. El proveedor solo tampoco: sin número, dos compras distintas al mismo
    proveedor son indistinguibles.

    Existe como función y no como un ``if`` adentro del planificador porque hay
    tres lugares que tienen que responder LO MISMO: acá se agrupa, la validación
    previa al lease decide si ofrecer el reparto, y el preview dibuja los grupos.
    Si divergen, la pantalla ofrece repartir un costo entre líneas que el
    importador después no va a agrupar — y el usuario ve un reparto que no ocurrió.
    """
    if not supplier or not invoice:
        return None
    return (supplier, invoice)


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


def plan_shipping_charges(
    lines: list[ShippingLine],
    *,
    sin_comprobante: str | None = None,
) -> ShippingPlan:
    """Agrupa los envíos declarados y devuelve qué cobrar una sola vez.

    Regla, en una línea: **una cifra por comprobante se cobra una vez**.

    - Misma cifra repetida en N filas del mismo comprobante → UN cargo.
    - Cifras distintas en el mismo comprobante → un cargo por cifra, y se avisa:
      puede ser un flete y un seguro, o puede ser que la planilla tenga el total
      en una fila y el prorrateo en las otras. Véktor no elige por el usuario.
    - Fila sin proveedor o sin número de comprobante → depende de
      ``sin_comprobante``, que es una decisión del USUARIO por hoja:

      ``None`` (default)      no se cobra y se reporta. Véktor no puede saber si
                              es un flete repetido o varios distintos.
      ``una_por_hoja``        la hoja es el comprobante: una cifra, un cargo.
                              La agrupación es sólo por importe — el proveedor
                              puede faltar y el usuario ya declaró que estas
                              filas comparten operación.
      ``una_por_fila``        cada fila es su propio flete.

    El orden de `charges` sigue el de aparición en el archivo: dos corridas sobre
    el mismo archivo tienen que producir los mismos gastos, en el mismo orden.
    """
    if sin_comprobante is not None and sin_comprobante not in SHIPPING_FALLBACKS:
        # Sin esto un valor desconocido caía al `else` y se comportaba como "no
        # cobrar": un typo en la decisión del usuario borraba el flete en silencio
        # y el import lo reportaba como si el archivo no tuviera comprobante.
        raise ValueError(f"sin_comprobante desconocido: {sin_comprobante!r}")

    plan = ShippingPlan()
    por_comprobante: dict[ComprobanteKey, dict[Decimal, ShippingCharge]] = {}
    orden: list[tuple[ComprobanteKey, Decimal]] = []

    for line in lines:
        if line.amount <= 0:
            # Una celda vacía o en cero no es un envío. No se reporta como
            # problema: la mayoría de las filas de un libro no traen flete.
            continue
        if identidad_de_comprobante(line.supplier, line.invoice) is None:
            if sin_comprobante == SIN_COMPROBANTE_UNA_POR_FILA:
                # Cada fila, su cargo. La clave lleva el índice para que dos filas
                # con la misma cifra NO colapsen: es justo lo que el usuario dijo.
                plan.charges.append(
                    ShippingCharge(
                        supplier=line.supplier,
                        invoice=SIN_COMPROBANTE,
                        amount=line.amount,
                        row_indexes=[line.row_index],
                    )
                )
                continue
            if sin_comprobante == SIN_COMPROBANTE_UNA_POR_HOJA:
                # La hoja es el comprobante: se agrupa SÓLO por importe. El
                # proveedor puede estar vacío y el usuario ya declaró que estas
                # filas comparten la operación.
                key = (SIN_COMPROBANTE, SIN_COMPROBANTE)
                cifras = por_comprobante.setdefault(key, {})
                de_la_hoja = cifras.get(line.amount)
                if de_la_hoja is None:
                    de_la_hoja = ShippingCharge(
                        supplier=line.supplier,
                        invoice=SIN_COMPROBANTE,
                        amount=line.amount,
                    )
                    cifras[line.amount] = de_la_hoja
                    orden.append((key, line.amount))
                de_la_hoja.row_indexes.append(line.row_index)
                continue
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
    # El pseudo-comprobante de `una_por_hoja` queda afuera: ahí dos cifras
    # distintas son lo esperado (dos fletes), no la anomalía que este aviso señala.
    plan.cifras_distintas = [
        k
        for k, cifras in por_comprobante.items()
        if len(cifras) > 1 and k != (SIN_COMPROBANTE, SIN_COMPROBANTE)
    ]
    return plan


def plan_line_shipping(lines: list[ShippingLine]) -> ShippingPlan:
    """Agrupa los envíos que el archivo YA asignó a cada línea. Regla: se SUMAN.

    Es la hermana de ``plan_shipping_charges`` y la diferencia entre las dos es
    exactamente la razón de que sean dos targets distintos en el catálogo:

    - «Envío del comprobante» trae la MISMA cifra repetida porque la planilla
      arrastra un flete único. Repetición → **se colapsa**.
    - «Envío asignado a esta línea» trae el pedazo que le toca a cada artículo.
      Dos líneas de $200 son $400 de flete. → **se suma**.

    Por eso este planificador **no necesita decisión del usuario ni comprobante**:
    sumar valores que cada fila declara como propios nunca es ambiguo, que es lo
    único que hacía falta preguntar en el otro caso. Las filas sin comprobante se
    agrupan en un cargo de la hoja en vez de quedar sin cobrar — no hay nada que
    deducir, sólo un total.

    ``repetido_en`` acá significa **cuántas líneas aportaron**, no cuántas veces
    figuraba repetida una cifra: el aviso de «se colapsó una repetición» no
    corresponde a este camino y el importador no lo emite.
    """
    plan = ShippingPlan()
    por_comprobante: dict[ComprobanteKey, ShippingCharge] = {}
    orden: list[ComprobanteKey] = []

    for line in lines:
        if line.amount <= 0:
            continue
        key = identidad_de_comprobante(line.supplier, line.invoice) or (
            SIN_COMPROBANTE,
            SIN_COMPROBANTE,
        )
        cargo = por_comprobante.get(key)
        if cargo is None:
            cargo = ShippingCharge(
                supplier=key[0], invoice=key[1], amount=Decimal("0")
            )
            por_comprobante[key] = cargo
            orden.append(key)
        cargo.amount += line.amount
        cargo.row_indexes.append(line.row_index)

    plan.charges = [por_comprobante[key] for key in orden]
    return plan
