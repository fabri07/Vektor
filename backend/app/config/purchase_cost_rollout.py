"""F-H6.c/d — compuerta de rollout por tenant del motor de costos de compra.

Este repo no tiene entorno de staging. El rollout por tenant **es** el staging:
el motor de costos de compra se despliega con la lista vacía (nadie habilitado,
comportamiento idéntico al de antes) y se enciende un tenant por vez, mirando
qué pasa con sus números antes de pasar al siguiente.

Qué gobierna la compuerta
-------------------------
Sólo el **motor de costos**: el reparto de las cifras compartidas del
comprobante y los ajustes de descuento / IVA / flete sobre el costo de cada
línea (``app.domain.purchase_cost``). Eso cambia plata sin que el usuario opte:
alcanza con mapear una columna de descuento para que el costo de un producto
—y por lo tanto su margen— quede distinto.

Qué NO gobierna, y sale global: los targets nuevos del mapeo (inertes hasta que
alguien los mapee) y el cobro de envío, que ya exige una decisión explícita del
usuario por hoja. Gatear algo que ya es opt-in sólo agrega una segunda llave que
hay que acordarse de girar.

Por qué una función y no ``get_settings().FLAG`` inline
-------------------------------------------------------
El precedente de ``SUPPLIER_REFERENCE_CREATION_MODE`` está leído inline y
**duplicado** en los dos caminos del importador, con un comentario que remite al
otro bloque. Dos lecturas de la misma compuerta pueden divergir, y divergir acá
significa que un tenant tiene el motor prendido en un camino y apagado en el
otro. Una sola función, un solo lugar. ``get_settings()`` es ``lru_cache``, así
que leerla por invocación no cuesta nada.

Sin sesión, sin ORM: recibe un ``tenant_id`` y contesta sí o no.
"""

from __future__ import annotations

import json
import logging
import uuid

#: Nombre de la variable de entorno, para nombrarla en los mensajes de error sin
#: que el texto pueda quedar desfasado del campo real de ``Settings``.
ENV_VAR = "PURCHASE_COST_ROLLOUT_TENANT_IDS"


def normalizar_tenant_id(value: object) -> str | None:
    """UUID en su forma canónica (minúsculas, con guiones), o ``None`` si no lo es.

    La usan las DOS puntas de la comparación —lo que se configuró y lo que se
    pregunta— justamente para que no puedan interpretar distinto el mismo UUID:
    mayúsculas, espacios al costado o la forma sin guiones tienen que dar el
    mismo tenant, o la compuerta habilitaría a medias según cómo se haya escrito
    la variable.
    """
    if isinstance(value, uuid.UUID):
        return str(value)
    if not isinstance(value, str):
        return None
    candidato = value.strip()
    if not candidato:
        return None
    try:
        return str(uuid.UUID(candidato))
    except ValueError:
        return None


def _elementos_crudos(value: object) -> list[str]:
    """Parte el valor crudo en elementos, sin juzgar si son UUIDs.

    Acepta csv, JSON array o una lista ya materializada — el mismo molde que
    ``CORS_ORIGINS``, que es el único precedente de csv→lista del repo y no hay
    motivo para que esta variable se escriba distinto.
    """
    if isinstance(value, str):
        crudo = value.strip()
        if not crudo:
            return []
        if crudo.startswith("["):
            try:
                parseado = json.loads(crudo)
            except ValueError:
                parseado = None
            if isinstance(parseado, list):
                return [str(item) for item in parseado]
            # No era un JSON array válido: se trata como csv y los pedazos que no
            # sean UUID caen por el camino de descarte de abajo. Un corchete mal
            # cerrado no puede ser el motivo de que la API no arranque.
        return crudo.split(",")
    if isinstance(value, list | tuple | set):
        return [str(item) for item in value]
    return [str(value)]


def parse_rollout_tenant_ids(value: object) -> list[str]:
    """Normaliza el valor crudo de la variable a una lista de UUIDs canónicos.

    **Nunca levanta.** Un elemento que no es un UUID se descarta con un
    ``logger.error`` que lo nombra, y la API arranca igual — el mismo criterio
    que ``ACCESS_REQUEST_AUTOVERIFY``, pero acá importa más que de costumbre: no
    hay staging donde ensayar, la variable se va a setear en producción desde el
    minuto cero y un typo en un UUID no puede ser la razón por la que nadie
    puede operar. Descartar deja a ese tenant en el comportamiento viejo, que es
    el seguro; abortar el arranque no deja nada en pie.

    Los duplicados se colapsan preservando el orden: la lista es una llave, no
    un contador, y que el mismo tenant aparezca dos veces no significa nada.
    """
    habilitados: list[str] = []
    descartados: list[str] = []

    for crudo in _elementos_crudos(value):
        normalizado = normalizar_tenant_id(crudo)
        if normalizado is None:
            if crudo.strip():
                descartados.append(crudo.strip())
            continue
        if normalizado not in habilitados:
            habilitados.append(normalizado)

    if descartados:
        logging.getLogger(__name__).error(
            "config.purchase_cost_rollout.tenant_id_invalido: "
            f"{len(descartados)} entrada(s) de {ENV_VAR} no son UUID y se "
            f"descartaron: {', '.join(repr(d) for d in descartados)}. "
            "Esos tenants NO quedan habilitados (siguen con el costo de compra "
            f"anterior); los {len(habilitados)} UUID válidos sí se aplican."
        )

    return habilitados


def purchase_cost_enabled_for(tenant_id: uuid.UUID | str) -> bool:
    """¿Este tenant tiene habilitado el motor de costos de compra?

    Lista vacía (el default) ⇒ ``False`` para todos. Un ``tenant_id`` que no es
    un UUID también da ``False``: no habilitar es el lado seguro de la duda.
    """
    from app.config.settings import get_settings  # noqa: PLC0415

    normalizado = normalizar_tenant_id(tenant_id)
    if normalizado is None:
        return False

    configurados = get_settings().PURCHASE_COST_ROLLOUT_TENANT_IDS
    return any(normalizar_tenant_id(entrada) == normalizado for entrada in configurados)
