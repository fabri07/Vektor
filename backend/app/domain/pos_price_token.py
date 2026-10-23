"""El precio que la caja cobró: emitido por el servidor y verificable al volver.

Una venta de caja se cobra en el mostrador y puede tardar horas o días en
llegar al servidor (corte de internet, la PC apagada, la pestaña cerrada). Hasta
B7 el servidor resolvía el precio **al recibir** la operación, contra el
catálogo de ese momento: si el dueño lo cambió en el medio, los pagos dejaban de
sumar el total y la venta —ya cobrada— se rechazaba.

La salida obvia sería dejar que el cliente declare el precio. No se puede: entre
el navegador y el handler hay una cola editable a mano, y un carrito que trae
sus propios importes es un carrito que puede cobrar de menos.

Así que el precio lo declara el cliente pero **el servidor lo firma**. El
catálogo emite, junto a cada producto, un token con HMAC-SHA256 sobre
``(tenant, producto, precio, unidad, factor, emisión)``. Al sincronizar, el
servidor recalcula la firma **con los valores declarados**: si coincide, esos
valores son exactamente los que él mismo emitió y los usa; si no, rechaza. No
hace falta guardar historia de precios ni confiar en el cliente.

**Se firma todo lo que el cliente declara**, no sólo el precio:

- el **factor**, porque ``sale_price_ars`` es por unidad de VENTA y la cantidad
  va en unidades base: el importe depende de los dos, y congelar sólo el precio
  dejaba abierto que un producto pasara de unidad a gramos entre la venta y la
  sincronización y el importe cambiara igual;
- la **unidad** y el **nombre**, porque son lo que el ticket dice que se vendió.
  Verificarlos contra el catálogo del momento de la sincronización —como se
  hacía hasta acá— tiene dos consecuencias malas: un cambio de unidad invalida
  el token de una venta legítima ya cobrada, y un producto renombrado entre el
  cobro y la subida imprime un nombre en el comprobante provisional y otro en el
  definitivo. Firmados, el cliente los declara y el servidor los acredita.

Límites declarados:

- **Vence a los 30 días.** Un token guardado a propósito sirve hasta ahí; cada
  uso que difiera del catálogo queda auditado.
- **Rotar ``JWT_SECRET_KEY`` invalida los tokens en vuelo.** Esas ventas quedan
  en revisión, que es lo correcto: el servidor ya no puede afirmar que emitió
  ese precio.
- Esto NO autoriza a cambiar un precio desde la caja. Eso es otra cosa, con su
  permiso y su auditoría, y no está en B7.

Puro: sin sesión, sin ORM.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from uuid import UUID

from app.config.settings import get_settings

#: Cuánto vale un token. Un corte de varios días es plausible en un local; un
#: mes es holgado sin volver eterno un precio viejo.
VIGENCIA_SEGUNDOS = 30 * 24 * 60 * 60

#: Largo del hexadecimal que se guarda del HMAC. 32 caracteres = 128 bits: de
#: sobra para que no se pueda adivinar, y corto para no inflar el catálogo.
_LARGO_FIRMA = 32


class ResultadoToken(StrEnum):
    VALIDO = "VALIDO"
    #: Falta, está mal formado, o la firma no corresponde a esos valores.
    INVALIDO = "INVALIDO"
    VENCIDO = "VENCIDO"


@lru_cache(maxsize=1)
def _clave() -> bytes:
    """Clave derivada de la del JWT: una sola cosa que configurar y que rotar.

    Se deriva en vez de reusarla tal cual para que firmar precios y firmar
    sesiones no compartan la misma clave efectiva.
    """
    base = get_settings().JWT_SECRET_KEY.encode("utf-8")
    return hmac.new(base, b"pos-price-token", hashlib.sha256).digest()


def _canonico(monto: Decimal) -> str:
    """El precio siempre con dos decimales.

    Sin esto ``100.0`` y ``100.00`` —el mismo precio, escrito distinto por el
    cliente— darían firmas distintas y un reintento legítimo sería rechazado.
    """
    return f"{monto.quantize(Decimal('0.01')):.2f}"


def _campo(valor: str) -> str:
    """Cada campo va con su largo adelante, para que no haya dos lecturas.

    Con un separador a secas, un nombre de producto que contenga ``|`` movería
    la frontera entre dos campos: «Coca 2|5 L» con factor 1 y «Coca 2» con
    factor «5 L» darían el MISMO mensaje, y por lo tanto la misma firma. Un
    token emitido para uno serviría para el otro. Prefijar el largo lo hace
    imposible sin prohibir ningún carácter en el nombre.
    """
    return f"{len(valor)}:{valor}"


def _firma(
    tenant_id: UUID,
    product_id: UUID,
    sale_price_ars: Decimal,
    product_name: str,
    sale_unit: str,
    base_units_per_sale_unit: int,
    emitido: int,
) -> str:
    mensaje = "|".join(
        _campo(parte)
        for parte in (
            str(tenant_id),
            str(product_id),
            _canonico(sale_price_ars),
            product_name,
            sale_unit,
            str(base_units_per_sale_unit),
            str(emitido),
        )
    ).encode("utf-8")
    return hmac.new(_clave(), mensaje, hashlib.sha256).hexdigest()[:_LARGO_FIRMA]


def emitir(
    tenant_id: UUID,
    product_id: UUID,
    sale_price_ars: Decimal,
    product_name: str,
    sale_unit: str,
    base_units_per_sale_unit: int,
    *,
    ahora: datetime | None = None,
) -> str:
    """El token que viaja con el producto en el catálogo de caja."""
    emitido = int((ahora or datetime.now(UTC)).timestamp())
    firma = _firma(
        tenant_id,
        product_id,
        sale_price_ars,
        product_name,
        sale_unit,
        base_units_per_sale_unit,
        emitido,
    )
    return f"{emitido}.{firma}"


def verificar(
    token: str | None,
    tenant_id: UUID,
    product_id: UUID,
    sale_price_ars: Decimal,
    product_name: str,
    sale_unit: str,
    base_units_per_sale_unit: int,
    *,
    ahora: datetime | None = None,
) -> ResultadoToken:
    """¿El servidor emitió ESTOS valores para ESTE producto, y siguen vigentes?

    Nunca levanta: un token basura es ``INVALIDO``, igual que uno bien formado
    con la firma cambiada.
    """
    if not token:
        return ResultadoToken.INVALIDO
    emitido_txt, _, firma = token.partition(".")
    if not firma:
        return ResultadoToken.INVALIDO
    try:
        emitido = int(emitido_txt)
    except ValueError:
        return ResultadoToken.INVALIDO
    esperada = _firma(
        tenant_id,
        product_id,
        sale_price_ars,
        product_name,
        sale_unit,
        base_units_per_sale_unit,
        emitido,
    )
    # `compare_digest` y no `==`: la comparación no debe filtrar en cuánto
    # tarda cuántos caracteres coincidieron.
    if not hmac.compare_digest(esperada, firma):
        return ResultadoToken.INVALIDO
    # El vencimiento se mira DESPUÉS de la firma: sin firma válida, la fecha de
    # emisión la puso el cliente y no significa nada.
    segundos = int((ahora or datetime.now(UTC)).timestamp()) - emitido
    if segundos > VIGENCIA_SEGUNDOS:
        return ResultadoToken.VENCIDO
    return ResultadoToken.VALIDO
