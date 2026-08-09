"""F-M — qué dice un encabezado: su concepto, y la semántica que lo modifica.

Un encabezado real no es una palabra, es un sustantivo núcleo con modificadores:
«Envío unitario», «Bonificación proveedor», «Total factura sin impuestos». El
reconocedor anterior elegía *la keyword ganadora* —el substring más largo— y por
eso el modificador podía llevarse el campo: un flete entraba como precio de
compra y un descuento como nombre de proveedor.

**Un calificador nunca puede ser la respuesta.** `envío + unitario` sigue siendo
envío; `descuento + proveedor` sigue siendo descuento; `precio + IVA` sigue
siendo precio. Lo que el calificador cambia es la *granularidad* o el *rol*, no
la identidad.

Y cuando quedan dos núcleos, no se elige: `Fecha del gasto` no puede resolverse
tirando una moneda entre la fecha y el monto — que es literalmente lo que hacía
el desempate por orden de declaración del dict.

Este módulo hace SÓLO el análisis. A qué campo va cada `(entidad, concepto,
calificadores)` lo decide la tabla de resolución en ``column_mapping_service``:
acá no se nombra ningún ``target_field``, para que el vocabulario del idioma no
quede atado al catálogo de campos de la base.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.header_keys import STOPWORDS
from app.domain.text_norm import normalize_text

# ── Conceptos: el sustantivo núcleo ──────────────────────────────────────────
# Token → concepto. Estos SIEMPRE ganan sobre un calificador.
CONCEPTOS: dict[str, str] = {
    "fecha": "fecha",
    "date": "fecha",
    "dia": "fecha",
    "mes": "fecha",
    "periodo": "fecha",
    "vencimiento": "vencimiento",
    "caducidad": "vencimiento",
    "cumpleanos": "cumpleanos",
    "nacimiento": "cumpleanos",
    "precio": "precio",
    "price": "precio",
    "envio": "envio",
    "flete": "envio",
    "shipping": "envio",
    "iva": "impuesto",
    "impuesto": "impuesto",
    "impuestos": "impuesto",
    "percepciones": "impuesto",
    "descuento": "descuento",
    "descuentos": "descuento",
    "bonificacion": "descuento",
    "dto": "descuento",
    "cantidad": "cantidad",
    "qty": "cantidad",
    "unidades": "cantidad",
    "cant": "cantidad",
    "stock": "stock",
    "existencias": "stock",
    "nombre": "nombre",
    "apellido": "apellido",
    "descripcion": "descripcion",
    "observaciones": "nota",
    "notas": "nota",
    "nota": "nota",
    "sku": "sku",
    "codigo": "codigo",
    "cod": "codigo",
    "barras": "barcode",
    "ean": "barcode",
    "upc": "barcode",
    "barcode": "barcode",
    "categoria": "categoria",
    "rubro": "categoria",
    "clasificacion": "categoria",
    "email": "email",
    "correo": "email",
    "mail": "email",
    "telefono": "telefono",
    "celular": "telefono",
    "phone": "telefono",
    "dni": "dni",
    "cuit": "cuit",
    "cuil": "cuil",
    "direccion": "direccion",
    "domicilio": "direccion",
    "localidad": "localidad",
    "ciudad": "localidad",
    "provincia": "provincia",
    "recurrente": "recurrencia",
    "recurring": "recurrencia",
    "frecuencia": "recurrencia",
    "marca": "marca",
}

#: Bigramas que son UN concepto y no la suma de sus partes. Se buscan antes que
#: los tokens sueltos: «forma pago» es el método de pago, no una forma y un pago.
CONCEPTOS_BIGRAMA: dict[tuple[str, str], str] = {
    ("forma", "pago"): "metodo_pago",
    ("metodo", "pago"): "metodo_pago",
    ("medio", "pago"): "metodo_pago",
    ("tipo", "pago"): "metodo_pago",
    ("codigo", "postal"): "codigo_postal",
    ("razon", "social"): "nombre",
    ("codigo", "producto"): "sku",
    ("cod", "producto"): "sku",
    ("codigo", "interno"): "sku",
}

#: Concepto → el concepto MÁS GENERAL del que es una especie.
#:
#: Cuando los dos aparecen en el mismo header no hay rivalidad: el específico
#: manda. «Código de barras» es un código, pero uno en particular; «Fecha de
#: vencimiento» es una fecha, pero una en particular. Sin esta relación los dos
#: quedaban como núcleos rivales y el header se volvía ambiguo — y en el motor
#: viejo, directamente empataban en longitud y ganaba el orden del dict (el
#: código de barras entraba al campo SKU, que es identidad de producto).
ESPECIALIZA: dict[str, str] = {
    "barcode": "codigo",
    "codigo_postal": "codigo",
    "vencimiento": "fecha",
    "cumpleanos": "fecha",
}

#: El mismo token nombra cosas distintas según el tipo de hoja. «Costo» en un
#: catálogo es un precio de referencia; en un libro de compras es el monto de la
#: línea. Se resuelve acá y no en la tabla de targets para que el análisis ya
#: salga con el concepto correcto.
CONCEPTOS_POR_ENTIDAD: dict[str, dict[str, str]] = {
    "product": {"costo": "precio", "cost": "precio"},
}

# ── Débiles: concepto si están solos, calificador si hay otro núcleo ─────────
# Token → (concepto cuando es el único núcleo, calificador cuando acompaña).
#
# Son las palabras que nombran la OPERACIÓN o la ENTIDAD. «Gasto» solo es el
# monto del gasto; en «Fecha del gasto» sólo dice de qué gasto es la fecha. Sin
# esta distinción, `gasto`(5) y `fecha`(5) empataban y ganaba el orden del dict:
# la columna de fecha de una planilla de gastos entraba como monto.
DEBILES: dict[str, tuple[str, str]] = {
    "monto": ("monto", "del_monto"),
    "importe": ("monto", "del_monto"),
    "total": ("monto", "por_comprobante"),
    "subtotal": ("monto", "del_monto"),
    "valor": ("monto", "del_monto"),
    "neto": ("monto", "neto"),
    "gasto": ("monto", "de_gasto"),
    "gastos": ("monto", "de_gasto"),
    "egreso": ("monto", "de_gasto"),
    "compra": ("monto", "de_compra"),
    "venta": ("monto", "de_venta"),
    "pago": ("monto", "de_pago"),
    "costo": ("monto", "de_costo"),
    "cost": ("monto", "de_costo"),
    # Entidades: nunca identifican el concepto de la columna, sólo lo ubican.
    "producto": ("producto", "de_producto"),
    "articulo": ("producto", "de_producto"),
    "mercaderia": ("producto", "de_producto"),
    "item": ("producto", "de_producto"),
    "proveedor": ("proveedor", "de_proveedor"),
    "cliente": ("cliente", "de_cliente"),
    "comprobante": ("comprobante", "por_comprobante"),
    "factura": ("comprobante", "por_comprobante"),
    "remito": ("comprobante", "por_comprobante"),
    "orden": ("comprobante", "por_comprobante"),
}

#: Conceptos que nombran una ENTIDAD, no una propiedad de la columna. Ceden ante
#: cualquier otro candidato: «Total factura» es un monto del comprobante, no un
#: comprobante.
CONCEPTOS_DE_ENTIDAD: frozenset[str] = frozenset(
    {"producto", "proveedor", "cliente", "comprobante"}
)

# ── Calificadores puros: nunca son la respuesta ──────────────────────────────
CALIFICADORES: dict[str, str] = {
    "unitario": "unitario",
    "unit": "unitario",
    "unidad": "unitario",
    "linea": "por_linea",
    "renglon": "por_linea",
    "lista": "de_lista",
    "sugerido": "de_lista",
    "pvp": "de_lista",
    "final": "final",
    "vigente": "final",
    "prorrateado": "por_linea",
    "asignado": "por_linea",
    "nro": "identificador",
    "numero": "identificador",
    "n": "identificador",
}

#: Preposiciones que declaran INCLUSIÓN. El concepto que las sigue deja de ser
#: núcleo: «Precio sin IVA» es un precio, no un impuesto.
INCLUSION: dict[str, str] = {"con": "con_", "sin": "sin_"}


@dataclass(frozen=True)
class HeaderAnalysis:
    """Qué se pudo leer de un encabezado, antes de mirar el catálogo de campos."""

    #: El sustantivo núcleo. ``None`` = no se reconoció ninguno.
    concept: str | None
    qualifiers: frozenset[str]
    #: Más de un núcleo posible. Cuando no está vacío, ``concept`` es ``None``:
    #: elegir uno sería el desempate por orden de dict que esto vino a eliminar.
    rivals: tuple[str, ...]
    #: Tokens ya normalizados, para poder explicar la lectura.
    tokens: tuple[str, ...]


def tokenize(normalized: str) -> tuple[str, ...]:
    """Tokens de un header ya normalizado (lowercase + guiones bajos).

    Saca acentos acá y no en los vocabularios: están todos escritos sin tilde, así
    que sin esto «Envío», «Artículo», «Categoría» y «Bonificación» no matchean
    NADA — no es que ganara el target equivocado, es que no había sugerencia.
    Se delega en ``normalize_text``, la cadena canónica NFKD, que pide
    explícitamente que no se la copie.

    Las preposiciones vacías se descartan salvo ``con``/``sin``, que sí significan
    algo: son las que distinguen un precio de un impuesto.
    """
    partes = normalize_text(normalized).replace("-", "_").replace(".", "_").split("_")
    return tuple(p for p in partes if p and (p not in STOPWORDS or p in INCLUSION))


def analyze_header(normalized: str, entity_type: str = "") -> HeaderAnalysis:
    """Separa el concepto de sus calificadores.

    Reglas, en orden:

    1. **Bigramas primero** — «forma pago» es un concepto, no dos.
    2. **Inclusión** — un concepto precedido por ``con``/``sin`` pasa a ser
       calificador fiscal. «Precio con IVA» habla de un precio.
    3. **Débiles** — las palabras de operación y de entidad son núcleo sólo si
       son el único candidato; si hay otro, califican.
    4. **Dos núcleos = ambigüedad** — no se elige, se devuelve la rivalidad para
       que la capa de arriba pregunte.
    """
    tokens = tokenize(normalized)
    por_entidad = CONCEPTOS_POR_ENTIDAD.get(entity_type, {})

    fuertes: list[str] = []
    debiles: list[str] = []
    calificadores: set[str] = set()

    i = 0
    while i < len(tokens):
        tok = tokens[i]

        if tok in INCLUSION:
            # R2: lo que sigue describe la inclusión, no el concepto de la columna.
            siguiente = tokens[i + 1] if i + 1 < len(tokens) else None
            if siguiente is not None:
                concepto_seguido = (
                    por_entidad.get(siguiente)
                    or CONCEPTOS.get(siguiente)
                    or (DEBILES[siguiente][0] if siguiente in DEBILES else None)
                )
                if concepto_seguido is not None:
                    calificadores.add(f"{INCLUSION[tok]}{concepto_seguido}")
                    i += 2
                    continue
            i += 1
            continue

        # R1: bigrama antes que token suelto.
        if i + 1 < len(tokens) and (tok, tokens[i + 1]) in CONCEPTOS_BIGRAMA:
            fuertes.append(CONCEPTOS_BIGRAMA[(tok, tokens[i + 1])])
            i += 2
            continue

        if tok in por_entidad:
            fuertes.append(por_entidad[tok])
        elif tok in CONCEPTOS:
            fuertes.append(CONCEPTOS[tok])
        elif tok in DEBILES:
            debiles.append(tok)
        elif tok in CALIFICADORES:
            calificadores.add(CALIFICADORES[tok])
        i += 1

    if fuertes:
        # R3: con un núcleo fuerte presente, los débiles pasan a calificar.
        calificadores.update(DEBILES[t][1] for t in debiles)
        candidatos = list(dict.fromkeys(fuertes))
        # R3-bis: entre un concepto y su género no hay rivalidad — «Código de
        # barras» es un código, pero uno en particular.
        generos = {ESPECIALIZA[c] for c in candidatos if c in ESPECIALIZA}
        if generos:
            candidatos = [c for c in candidatos if c not in generos] or candidatos
    else:
        # Sin fuertes, el primer débil NO-entidad es el núcleo y el resto califica.
        candidatos = list(dict.fromkeys(DEBILES[t][0] for t in debiles))
        if len(candidatos) > 1:
            # Una entidad nunca es el núcleo si hay otro candidato: «Total factura»
            # es un monto del comprobante, no un comprobante.
            no_entidad = [t for t in debiles if DEBILES[t][0] not in CONCEPTOS_DE_ENTIDAD]
            if no_entidad:
                calificadores.update(
                    DEBILES[t][1] for t in debiles if t not in no_entidad
                )
                candidatos = list(dict.fromkeys(DEBILES[t][0] for t in no_entidad))

    if len(candidatos) == 1:
        return HeaderAnalysis(candidatos[0], frozenset(calificadores), (), tokens)
    if len(candidatos) > 1:
        # R4: dos núcleos. No se elige.
        return HeaderAnalysis(None, frozenset(calificadores), tuple(candidatos), tokens)
    return HeaderAnalysis(None, frozenset(calificadores), (), tokens)
