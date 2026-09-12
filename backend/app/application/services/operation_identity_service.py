"""Reclamar identidades y decidir qué hacer con cada fila. El candado del import.

Qué resuelve
------------
``domain/operation_identity`` dice **cuándo** dos filas son la misma operación.
Este servicio hace las tres cosas que faltan para que eso sirva: agrupa las filas
del archivo en documentos, **reclama** cada identidad de forma que dos cargas
simultáneas no puedan atravesarla, y traduce el resultado a una decisión por fila.

Las tres decisiones, y por qué son esas
---------------------------------------
* ``ya_aplicada`` — la misma identidad y el mismo contenido. **No se aplica de
  nuevo.** Es el único caso donde Véktor se saltea plata, y por eso exige las dos
  condiciones: la identidad sola diría "ya lo tengo" sobre un documento corregido.
* ``conflicto`` — la misma identidad con contenido distinto. **No se decide sola.**
  Puede ser una corrección (el proveedor reemitió la factura) o un error de carga,
  y las dos se parecen. Va a revisión con su motivo: omitirla escondería una
  corrección, aplicarla duplicaría el efecto.
* ``sin_clave`` — el archivo no da identidad suficiente. Camino de siempre: se
  importa y el circuito de candidatos (``import_overlap_service``) avisa si algo se
  parece. Nunca se descarta por parecido.

Por qué la reclamación es masiva y no fila por fila
---------------------------------------------------
Un ``guarded_savepoint`` por fila es el N+1 que este programa ya desarmó dos veces
(fingerprints, balances): cuatro round-trips por fila más el peaje del savepoint.
Acá la reclamación entera del archivo es **una sentencia**:
``INSERT ... ON CONFLICT DO NOTHING RETURNING identity_key``. Lo que vuelve es lo
que ganó el candado; lo que no vuelve, lo perdió — y eso es cierto tanto si lo
perdió contra un import viejo como contra otro corriendo en paralelo en este mismo
instante. La atomicidad la da el UNIQUE ``(tenant_id, identity_key)``, no una
comprobación en memoria, que es lo único que dos confirmaciones simultáneas no
pueden burlar.

Reclamar antes de insertar (y devolver lo que sobra)
----------------------------------------------------
La identidad se reclama ANTES de escribir los efectos, porque si no el candado
llega tarde: entre "miré y no estaba" e "inserté" cabe otra carga entera. La
consecuencia es que se reclama para filas que después pueden no insertar nada (una
fila sin monto, una ruteada a "Otros" por una columna riesgosa). Esas
reclamaciones se **devuelven al cierre del import** —una sentencia, dentro de la
misma transacción— porque una identidad reclamada sin efecto vivo bloquearía el
reintento del archivo corregido.

Todo en la misma transacción que los efectos
--------------------------------------------
Las reclamaciones, los vínculos y las ventas/gastos comparten la transacción del
confirm. Si el import se revierte, las identidades se van con él: no puede quedar
una identidad reclamada por un import que no ocurrió, ni un efecto sin su
identidad.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.operation_identity import (
    ClaveDeOperacion,
    LineaEfectiva,
    SinClave,
    clave_de_operacion,
    hay_columnas_de_identidad,
    huella_de_contenido,
)
from app.observability.logger import get_logger
from app.persistence.models.operation_identity import OperationIdentity, OperationIdentityLink

logger = get_logger(__name__)

DECISION_NUEVA = "nueva"
DECISION_YA_APLICADA = "ya_aplicada"
DECISION_CONFLICTO = "conflicto"
DECISION_SIN_CLAVE = "sin_clave"


@dataclass
class PlanDeIdentidad:
    """Qué hacer con cada fila del contexto, ya con el candado tomado.

    ``decision`` está indexado por ``row_index`` dentro del contexto — el mismo
    índice que usa el ancla de fila del importador, para que las dos capas hablen
    de la misma fila.
    """

    decision: dict[int, str] = field(default_factory=dict)
    #: Sólo para ``conflicto`` y ``sin_clave``: qué falta o qué chocó, en
    #: castellano. Es lo que se le muestra al usuario en "Otros".
    motivo: dict[int, str] = field(default_factory=dict)
    clave: dict[int, str] = field(default_factory=dict)
    #: ``{clave: id de la identidad}`` — para colgarle los vínculos.
    identity_id: dict[str, uuid.UUID] = field(default_factory=dict)
    #: Claves que ESTA corrida reclamó (no las que ya existían). Son las únicas
    #: que se pueden devolver si no terminan produciendo efectos.
    reclamadas: set[str] = field(default_factory=set)
    #: Vínculos acumulados en memoria; se persisten en lote al cierre.
    vinculos: list[tuple[uuid.UUID, str, uuid.UUID]] = field(default_factory=list)

    @property
    def activo(self) -> bool:
        """¿Este plan tiene algo que decir? Un archivo sin columnas de identidad
        devuelve un plan vacío y el importador no paga nada."""
        return bool(self.decision)

    def registrar_efecto(self, row_index: int, entity_type: str, entity_id: uuid.UUID) -> None:
        """La fila produjo un efecto persistido: se lo cuelga a su identidad.

        Sin esto la identidad quedaría reclamada y huérfana, y se devolvería al
        cierre — o sea, el archivo se podría volver a importar entero.
        """
        clave = self.clave.get(row_index)
        if clave is None:
            return
        ident = self.identity_id.get(clave)
        if ident is None:
            return
        self.vinculos.append((ident, entity_type, entity_id))


def plan_vacio() -> PlanDeIdentidad:
    """Ni una decisión, ni una query. Lo devuelven los contextos sin columnas de
    identidad, que son la mayoría de los archivos de hoy.

    Es una función y no una constante compartida a propósito: un plan es mutable
    (acumula vínculos), y una instancia global se contaminaría entre imports.
    """
    return PlanDeIdentidad()


def _parsers() -> tuple[Any, Any, Any]:
    """Los parsers REALES del importador, importados tarde.

    Tarde por la circularidad (el importador importa este módulo), y **los del
    importador** y no una copia por algo más importante: la huella de contenido
    tiene que hablar de los mismos valores que se van a persistir. Dos lecturas
    del mismo número es el defecto que este programa entero viene a cerrar.
    """
    from app.application.services.ingestion_import_service import (  # noqa: PLC0415
        _parse_amount,
        _parse_date,
        _parse_qty,
    )

    return _parse_amount, _parse_date, _parse_qty


def _linea_efectiva(fila: dict[str, Any], cols: dict[str, str], entity: str) -> LineaEfectiva:
    """Los valores de la fila que van a producir efectos, ya interpretados.

    Se resuelve el monto con ``resolve_line_amount`` —la misma función y las
    mismas reglas que usan ``_add_sale``/``_add_expense``—, así que un archivo que
    trae precio × cantidad sin columna de total produce la misma huella que el que
    trae el total escrito. Comparar el texto crudo diría que son documentos
    distintos.
    """
    from app.domain.line_amount import resolve_line_amount  # noqa: PLC0415

    parse_amount, parse_date, parse_qty = _parsers()

    def celda(campo: str) -> Any:
        col = cols.get(campo)
        return fila.get(col) if col else None

    cantidad_cruda = parse_qty(celda("quantity")) if cols.get("quantity") else 0
    precio = parse_amount(celda("unit_price")) if cols.get("unit_price") else None
    linea = resolve_line_amount(
        amount=parse_amount(celda("amount")) if cols.get("amount") else None,
        unit_price=precio,
        quantity=cantidad_cruda or None,
    )
    campo_fecha = "transaction_date" if entity == "sale" else "expense_date"
    cruda = celda(campo_fecha)
    if cruda is None:
        cruda = celda("transaction_date")
    return LineaEfectiva(
        monto=linea.amount,
        fecha=parse_date(cruda) if cruda is not None else None,
        cantidad=cantidad_cruda or None,
        precio_unitario=precio,
        producto=_texto(celda("product_name")) or _texto(celda("sku")),
        descuento=parse_amount(celda("discount")) if cols.get("discount") else None,
        impuestos=parse_amount(celda("taxes")) if cols.get("taxes") else None,
    )


def _texto(valor: Any) -> str | None:
    if valor is None:
        return None
    s = str(valor).strip()
    return s or None


@dataclass
class _Documento:
    clave: str
    granularidad: str = "documento"
    filas: list[int] = field(default_factory=list)
    lineas: list[LineaEfectiva] = field(default_factory=list)


async def planificar_identidades(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    file_id: uuid.UUID | None,
    *,
    rows: list[dict[str, Any]],
    cols: dict[str, str],
    entity: str,
) -> PlanDeIdentidad:
    """Agrupa, reclama y decide. Es todo lo que el importador necesita saber.

    Devuelve ``PLAN_VACIO`` —sin tocar la base— cuando el archivo no mapeó
    ninguna columna que pueda anclar una identidad, que es el caso de la enorme
    mayoría de los imports de hoy. La deduplicación por clave fuerte no puede
    costarle nada al que no la usa.
    """
    if entity not in ("sale", "expense") or not rows:
        return plan_vacio()
    if not hay_columnas_de_identidad(cols):
        return plan_vacio()

    plan = PlanDeIdentidad()
    documentos: dict[str, _Documento] = {}

    for indice, fila in enumerate(rows):
        resultado = clave_de_operacion(fila, cols, entity)
        if isinstance(resultado, SinClave):
            plan.decision[indice] = DECISION_SIN_CLAVE
            plan.motivo[indice] = resultado.texto
            continue
        assert isinstance(resultado, ClaveDeOperacion)
        linea = _linea_efectiva(fila, cols, entity)
        doc = documentos.get(resultado.clave)
        if doc is None:
            documentos[resultado.clave] = _Documento(
                resultado.clave, resultado.granularidad, [indice], [linea]
            )
            plan.clave[indice] = resultado.clave
            continue
        if resultado.granularidad == "linea":
            # La clave identifica UNA fila y ya apareció en este mismo archivo.
            # Acá sí se puede decidir sin ambigüedad: la primera se aplica y la
            # repetida no. Es el único caso decidible dentro del lote — con
            # granularidad de documento, "el mismo remito dos veces" y "un remito
            # con el doble de renglones" son indistinguibles sin identidad de
            # línea, así que ahí no se decide nada y manda el documento entero.
            repetida = huella_de_contenido([linea]) == huella_de_contenido([doc.lineas[0]])
            plan.decision[indice] = DECISION_YA_APLICADA if repetida else DECISION_CONFLICTO
            if not repetida:
                plan.motivo[indice] = (
                    "Este archivo trae dos veces el mismo renglón (mismo ID de "
                    "línea) con datos distintos. Revisá cuál corresponde."
                )
            else:
                plan.motivo[indice] = (
                    "Este archivo trae dos veces el mismo renglón (mismo ID de "
                    "línea): se aplica una sola vez."
                )
            continue
        doc.filas.append(indice)
        doc.lineas.append(linea)
        plan.clave[indice] = resultado.clave

    if not documentos:
        return plan

    contenidos = {clave: huella_de_contenido(doc.lineas) for clave, doc in documentos.items()}

    ganadas, existentes = await _reclamar(
        session, tenant_id, file_id, contenidos=contenidos, entity=entity
    )
    plan.reclamadas = ganadas

    for clave, doc in documentos.items():
        previa = existentes.get(clave)
        if clave in ganadas:
            decision = DECISION_NUEVA
        elif previa is not None and previa[1] == contenidos[clave]:
            decision = DECISION_YA_APLICADA
        else:
            decision = DECISION_CONFLICTO
        if previa is not None:
            plan.identity_id[clave] = previa[0]
        for indice in doc.filas:
            plan.decision[indice] = decision
            if decision == DECISION_CONFLICTO:
                plan.motivo[indice] = (
                    "Ya hay una operación cargada con esta misma identidad "
                    "(mismo comprobante o mismo ID de origen) pero con datos "
                    "distintos. Puede ser una corrección o un error de carga: "
                    "revisala antes de aplicarla."
                )

    logger.info(
        "ingestion.identidad.plan",
        tenant_id=str(tenant_id),
        file_id=str(file_id) if file_id else None,
        documentos=len(documentos),
        nuevas=len(ganadas),
        ya_aplicadas=sum(1 for d in plan.decision.values() if d == DECISION_YA_APLICADA),
        conflictos=sum(1 for d in plan.decision.values() if d == DECISION_CONFLICTO),
        sin_clave=sum(1 for d in plan.decision.values() if d == DECISION_SIN_CLAVE),
    )
    return plan


async def _reclamar(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    file_id: uuid.UUID | None,
    *,
    contenidos: dict[str, str],
    entity: str,
) -> tuple[set[str], dict[str, tuple[uuid.UUID, str]]]:
    """Toma el candado de todas las claves del archivo en UNA sentencia.

    Devuelve ``(claves ganadas, {clave: (id, content_hash)} de las que ya
    estaban)``. La segunda query sólo se paga si algo se perdió.
    """
    filas = [
        {
            "id": uuid.uuid4(),
            "tenant_id": tenant_id,
            "identity_key": clave,
            "content_hash": contenido,
            "entity_type": entity,
            "first_seen_upload_id": file_id,
        }
        for clave, contenido in contenidos.items()
    ]
    bind = session.bind
    dialecto = bind.dialect.name if bind is not None else ""
    # Un alias por rama (y no uno compartido) porque los dos `insert` son tipos
    # distintos: es el mismo patrón que `_persist_import_fingerprints`.
    if dialecto == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as _pg_insert  # noqa: PLC0415

        sentencia = (
            _pg_insert(OperationIdentity)
            .values(filas)
            .on_conflict_do_nothing(index_elements=["tenant_id", "identity_key"])
            .returning(OperationIdentity.identity_key)
        )
    elif dialecto == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as _sqlite_insert  # noqa: PLC0415

        sentencia = (
            _sqlite_insert(OperationIdentity)
            .values(filas)
            .on_conflict_do_nothing(index_elements=["tenant_id", "identity_key"])
            .returning(OperationIdentity.identity_key)
        )
    else:  # pragma: no cover — sin ON CONFLICT no hay candado: se declara nulo
        logger.warning("ingestion.identidad.dialecto_sin_upsert", dialecto=dialecto)
        return set(), await _leer_existentes(session, tenant_id, list(contenidos))

    ganadas = set((await session.execute(sentencia)).scalars().all())
    perdidas = [clave for clave in contenidos if clave not in ganadas]
    existentes = await _leer_existentes(session, tenant_id, perdidas) if perdidas else {}
    # Las ganadas también necesitan su id para colgar vínculos: se leen de una,
    # junto con las perdidas, en vez de pagar una query por grupo.
    if ganadas:
        existentes.update(await _leer_existentes(session, tenant_id, sorted(ganadas)))
    return ganadas, existentes


async def _leer_existentes(
    session: AsyncSession, tenant_id: uuid.UUID, claves: list[str]
) -> dict[str, tuple[uuid.UUID, str]]:
    if not claves:
        return {}
    filas = (
        await session.execute(
            select(
                OperationIdentity.identity_key,
                OperationIdentity.id,
                OperationIdentity.content_hash,
            ).where(
                OperationIdentity.tenant_id == tenant_id,
                OperationIdentity.identity_key.in_(claves),
            )
        )
    ).all()
    return {clave: (ident, contenido) for clave, ident, contenido in filas}


async def cerrar_plan(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    file_id: uuid.UUID | None,
    planes: list[PlanDeIdentidad],
) -> None:
    """Persiste los vínculos y devuelve las reclamaciones que no produjeron nada.

    Se llama UNA vez al final del import con los planes de todas las hojas: son
    dos sentencias para el archivo entero, no dos por hoja.

    La devolución no es housekeeping: una identidad reclamada por una fila que
    terminó en "Otros" —sin monto, con una columna riesgosa ruteada— dejaría el
    archivo corregido sin poder reimportarse, y el usuario vería "ya está
    aplicada" sobre algo que nunca se aplicó.
    """
    vinculos = [(i, t, e) for plan in planes for (i, t, e) in plan.vinculos]
    if vinculos:
        await _persistir_vinculos(session, tenant_id, file_id, vinculos)

    con_efecto = {i for (i, _, _) in vinculos}
    sobrantes: set[uuid.UUID] = set()
    for plan in planes:
        for clave in plan.reclamadas:
            ident = plan.identity_id.get(clave)
            if ident is not None and ident not in con_efecto:
                sobrantes.add(ident)
    if sobrantes:
        await session.execute(
            delete(OperationIdentity).where(
                OperationIdentity.tenant_id == tenant_id,
                OperationIdentity.id.in_(sorted(sobrantes)),
            )
        )
        logger.info(
            "ingestion.identidad.reclamos_devueltos",
            tenant_id=str(tenant_id),
            cantidad=len(sobrantes),
        )


async def _persistir_vinculos(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    file_id: uuid.UUID | None,
    vinculos: list[tuple[uuid.UUID, str, uuid.UUID]],
) -> None:
    filas = [
        {
            "id": uuid.uuid4(),
            "identity_id": ident,
            "tenant_id": tenant_id,
            "entity_type": tipo,
            "entity_id": entidad,
            "source_upload_id": file_id,
        }
        for ident, tipo, entidad in dict.fromkeys(vinculos)
    ]
    bind = session.bind
    dialecto = bind.dialect.name if bind is not None else ""
    if dialecto == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as _pg_insert  # noqa: PLC0415

        await session.execute(
            _pg_insert(OperationIdentityLink)
            .values(filas)
            .on_conflict_do_nothing(index_elements=["identity_id", "entity_type", "entity_id"])
        )
    elif dialecto == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as _sqlite_insert  # noqa: PLC0415

        await session.execute(
            _sqlite_insert(OperationIdentityLink)
            .values(filas)
            .on_conflict_do_nothing(index_elements=["identity_id", "entity_type", "entity_id"])
        )
    else:  # pragma: no cover
        from sqlalchemy import insert as _plain  # noqa: PLC0415

        await session.execute(_plain(OperationIdentityLink).values(filas))


async def liberar_identidades_de(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    efectos: list[tuple[str, uuid.UUID]],
) -> int:
    """Suelta la identidad de los efectos que se revirtieron. Devuelve cuántas.

    Sólo se libera la identidad que se quedó **sin ningún efecto vivo**. Es la
    diferencia entre una reversión total y una parcial: si un archivo trajo un
    remito de tres renglones y la relectura conserva uno porque el usuario lo
    editó, la identidad sigue tomada — soltarla haría que el próximo import
    volviera a aplicar ese renglón encima del que quedó.
    """
    if not efectos:
        return 0
    condiciones = [
        (OperationIdentityLink.entity_type == tipo) & (OperationIdentityLink.entity_id == eid)
        for tipo, eid in efectos
    ]
    from sqlalchemy import or_  # noqa: PLC0415

    afectadas = set(
        (
            await session.execute(
                select(OperationIdentityLink.identity_id).where(
                    OperationIdentityLink.tenant_id == tenant_id, or_(*condiciones)
                )
            )
        )
        .scalars()
        .all()
    )
    if not afectadas:
        return 0
    await session.execute(
        delete(OperationIdentityLink).where(
            OperationIdentityLink.tenant_id == tenant_id, or_(*condiciones)
        )
    )
    # Sólo las que quedaron sin vínculos. El `NOT EXISTS` se resuelve después del
    # DELETE: si se leyera antes, una identidad con dos efectos —uno revertido y
    # otro no— se vería como huérfana y se soltaría de más.
    vivos = set(
        (
            await session.execute(
                select(OperationIdentityLink.identity_id).where(
                    OperationIdentityLink.identity_id.in_(sorted(afectadas))
                )
            )
        )
        .scalars()
        .all()
    )
    huerfanas = afectadas - vivos
    if not huerfanas:
        return 0
    await session.execute(
        delete(OperationIdentity).where(
            OperationIdentity.tenant_id == tenant_id,
            OperationIdentity.id.in_(sorted(huerfanas)),
        )
    )
    logger.info(
        "ingestion.identidad.liberadas",
        tenant_id=str(tenant_id),
        liberadas=len(huerfanas),
        conservadas=len(afectadas - huerfanas),
    )
    return len(huerfanas)


async def identidad_tomada(
    session: AsyncSession, tenant_id: uuid.UUID, clave: str
) -> tuple[uuid.UUID, str] | None:
    """¿Esta clave ya está aplicada? ``(id, content_hash)`` o ``None``.

    La usa la resolución desde "Otros": una fila que llegó ahí por conflicto no
    puede importarse sin volver a mirar el candado, porque si no la revisión
    humana sería la forma de saltearse la protección.
    """
    return (await _leer_existentes(session, tenant_id, [clave])).get(clave)


def contar(planes: list[PlanDeIdentidad]) -> dict[str, int]:
    """Conteos por decisión, para el resumen del confirm."""
    total: dict[str, int] = defaultdict(int)
    for plan in planes:
        for decision in plan.decision.values():
            total[decision] += 1
    return dict(total)
