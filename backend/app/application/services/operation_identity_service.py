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

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.operation_identity import (
    ClaveDeOperacion,
    LineaEfectiva,
    SinClave,
    clave_de_operacion,
    claves_equivalentes,
    forma_k1,
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
    #: I6-7: la huella del documento de cada fila EN ESTE ARCHIVO. Viaja con la
    #: fila que va a «Otros» por conflicto, para que resolverla reclame la
    #: identidad con el contenido que un reimport del mismo archivo reconoce.
    contenido: dict[int, str] = field(default_factory=dict)
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
    # I6-3/I6-4: las otras formas guardables de cada documento (``k1`` ↔ ``k2``).
    # Se leen en la MISMA query que las reclamadas: no cuestan un statement.
    equivalentes = {clave: claves_equivalentes(clave, entity) for clave in documentos}

    ganadas, existentes = await _reclamar(
        session,
        tenant_id,
        file_id,
        contenidos=contenidos,
        entity=entity,
        leer_ademas=sorted({e for eqs in equivalentes.values() for e in eqs}),
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
            plan.contenido[indice] = contenidos[clave]
            plan.decision[indice] = decision
            if decision == DECISION_CONFLICTO:
                plan.motivo[indice] = (
                    "Ya hay una operación cargada con esta misma identidad "
                    "(mismo comprobante o mismo ID de origen) pero con datos "
                    "distintos. Puede ser una corrección o un error de carga: "
                    "revisala antes de aplicarla."
                )

    _decidir_por_equivalentes(
        plan,
        entity=entity,
        documentos=documentos,
        contenidos=contenidos,
        equivalentes=equivalentes,
        existentes=existentes,
        ganadas=ganadas,
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


#: Lo que ve el usuario en «Otros» cuando lo que choca es el mismo comprobante
#: guardado en la otra forma de clave (con o sin la letra, o de antes de que la
#: clave distinguiera venta de gasto): no se puede saber si es el mismo
#: documento corregido o el de la otra letra.
MOTIVO_CONFLICTO_EQUIVALENTE = (
    "Ya hay una operación cargada con el mismo comprobante o ID de origen (en un "
    "archivo que escribía la letra del comprobante distinto, o de antes de que "
    "Véktor la distinguiera) y con datos distintos. Puede ser otro comprobante "
    "o una corrección: revisala antes de aplicarla."
)


def _decidir_por_equivalentes(
    plan: PlanDeIdentidad,
    *,
    entity: str,
    documentos: dict[str, _Documento],
    contenidos: dict[str, str],
    equivalentes: dict[str, list[str]],
    existentes: dict[str, tuple[uuid.UUID, str, str]],
    ganadas: set[str],
) -> None:
    """I6-3/I6-4: lo que la clave da por NUEVO se vuelve a mirar contra el mismo
    documento guardado en su otra forma (``claves_equivalentes``).

    Sin esto, reimportar un archivo ya importado con la clave vieja lo aplicaría
    entero de nuevo (la clave nueva no está guardada en ningún lado), y dos
    exportaciones del mismo comprobante —una con «Factura A», otra con «FC»—
    entrarían las dos. Nada se migra; se busca. La decisión es conservadora:

    * La guardada tiene que ser de la MISMA entidad. Es justo lo que la clave
      vieja del ID externo no distinguía (I6-3): si es de la otra entidad, no
      dice nada de ésta, y los gastos que la regla vieja omitió entran ahora.
    * Mismo contenido → ``ya_aplicada``. Contra una ``k1`` se compara además con
      el grupo que armaba la regla vieja: la A y la B del mismo número en un
      archivo eran UN documento, con una huella.
    * Contenido distinto → ``conflicto``, nunca ``nueva``: entre una clave con
      letra y una sin letra, «la otra letra» y «la misma corregida» son
      indistinguibles, y aplicarla podría duplicar. Va a «Otros» con la clave
      GUARDADA, que es la que está tomada: así el candado de «Otros» (409) la ve.

    Las reclamaciones nuevas de esas filas no producen efecto y se devuelven al
    cierre, como cualquier otra.
    """
    # El grupo de la regla vieja: todo lo de este plan con la misma forma k1.
    grupos_k1: dict[str, list[LineaEfectiva]] = defaultdict(list)
    for clave, doc in documentos.items():
        grupos_k1[forma_k1(clave, entity)].extend(doc.lineas)

    for clave, doc in documentos.items():
        if plan.decision.get(doc.filas[0]) != DECISION_NUEVA:
            continue
        guardadas = [
            (eq, existentes[eq])
            for eq in equivalentes.get(clave, ())
            # Una equivalente que reclamó ESTE plan no es «lo guardado»: es otra
            # fila del mismo archivo («Factura A 0001-123» y «FC 0001-123»
            # juntos). Entre hermanos del mismo archivo nadie bloquea a nadie:
            # entran los dos (cada uno con su clave), como en main, donde eran
            # dos renglones del mismo documento y se aplicaban los dos.
            if eq in existentes and existentes[eq][2] == entity and eq not in ganadas
        ]
        if not guardadas:
            continue
        coincide: tuple[str, uuid.UUID] | None = None
        for eq, (ident, contenido, _) in guardadas:
            grupo = grupos_k1.get(eq) if eq == forma_k1(clave, entity) else None
            if contenido == contenidos[clave] or (
                grupo is not None and contenido == huella_de_contenido(grupo)
            ):
                coincide = (eq, ident)
                break
        eq, ident = coincide or (guardadas[0][0], guardadas[0][1][0])
        plan.identity_id[eq] = ident
        for indice in doc.filas:
            plan.clave[indice] = eq
            if coincide is not None:
                plan.decision[indice] = DECISION_YA_APLICADA
                plan.motivo.pop(indice, None)
            else:
                plan.decision[indice] = DECISION_CONFLICTO
                plan.motivo[indice] = MOTIVO_CONFLICTO_EQUIVALENTE


async def _reclamar(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    file_id: uuid.UUID | None,
    *,
    contenidos: dict[str, str],
    entity: str,
    leer_ademas: list[str] | None = None,
) -> tuple[set[str], dict[str, tuple[uuid.UUID, str, str]]]:
    """Toma el candado de todas las claves del archivo en UNA sentencia.

    Devuelve ``(claves ganadas, {clave: (id, content_hash, entity_type)}``) de
    las ganadas, las perdidas y las de ``leer_ademas`` (las del mismo documento
    en su otra forma de clave, I6-3/I6-4, que sólo se leen y nunca se reclaman).
    Todo eso se lee en UNA query (hasta ``_CLAVES_POR_LECTURA``), después del
    INSERT.
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
        return set(), await _leer_con_entidad(
            session, tenant_id, sorted(set(contenidos) | set(leer_ademas or ()))
        )

    ganadas: set[str] = set((await session.execute(sentencia)).scalars().all())
    # Las ganadas también necesitan su id para colgar vínculos: se leen de una,
    # junto con las perdidas y las equivalentes, en vez de pagar una query por grupo.
    existentes = await _leer_con_entidad(
        session, tenant_id, sorted(set(contenidos) | set(leer_ademas or ()))
    )
    return ganadas, existentes


#: Claves por query. Un comprobante sin letra se busca también con cada letra
#: fiscal (siete claves por documento): sin tope, un archivo grande pasaría el
#: límite de parámetros de asyncpg (32.767). Hasta este tamaño es UNA query.
_CLAVES_POR_LECTURA = 5000


def tandas_de_claves(claves: list[str]) -> list[list[str]]:
    """``claves`` partidas en tandas de ``_CLAVES_POR_LECTURA``. La usa todo el
    que arma un ``IN`` con claves y sus equivalentes (el import y el revert de
    B·5g): un ``IN`` sin tope pasa el límite de parámetros de asyncpg."""
    tope = _CLAVES_POR_LECTURA
    return [claves[desde : desde + tope] for desde in range(0, len(claves), tope)]


async def _leer_con_entidad(
    session: AsyncSession, tenant_id: uuid.UUID, claves: list[str]
) -> dict[str, tuple[uuid.UUID, str, str]]:
    salida: dict[str, tuple[uuid.UUID, str, str]] = {}
    for tanda in tandas_de_claves(claves):
        filas = (
            await session.execute(
                select(
                    OperationIdentity.identity_key,
                    OperationIdentity.id,
                    OperationIdentity.content_hash,
                    OperationIdentity.entity_type,
                ).where(
                    OperationIdentity.tenant_id == tenant_id,
                    OperationIdentity.identity_key.in_(tanda),
                )
            )
        ).all()
        salida.update(
            {clave: (ident, contenido, entidad) for clave, ident, contenido, entidad in filas}
        )
    return salida


async def _leer_existentes(
    session: AsyncSession, tenant_id: uuid.UUID, claves: list[str]
) -> dict[str, tuple[uuid.UUID, str]]:
    return {
        clave: (ident, contenido)
        for clave, (ident, contenido, _) in (
            await _leer_con_entidad(session, tenant_id, claves)
        ).items()
    }


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
    if vivos & afectadas:
        await _resellar_sin_respaldo(session, tenant_id, sorted(vivos & afectadas))
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


#: I6-7: el ``content_hash`` de una identidad que sobrevive sin ningún efecto del
#: archivo que la describió. No coincide con ninguna huella (``c1:…``).
CONTENIDO_SIN_RESPALDO = "sin_respaldo"


async def _resellar_sin_respaldo(
    session: AsyncSession, tenant_id: uuid.UUID, ids: list[uuid.UUID]
) -> None:
    """I6-7: la identidad que sigue tomada pero ya no tiene ningún efecto vivo del
    archivo que la reclamó (``first_seen_upload_id``) deja de afirmar ese
    contenido.

    El caso: F1 carga el comprobante; F2 lo trae con otro importe y el dueño lo
    aplica «pese al conflicto» (dos ventas para él). Borrar F1 deja viva sólo la
    de F2, y la identidad conservaba la huella de F1: re-subir F1 la daba por
    «ya aplicada» y la venta se perdía en silencio. Re-sellada, el reimport la ve
    en conflicto y va a «Otros», donde el dueño decide. Si queda un efecto del
    archivo que la reclamó (la relectura que conserva un renglón editado, el
    hermano de una venta sacada por B·5e), la huella sigue siendo cierta y no se
    toca.
    """
    con_archivo = (
        await session.execute(
            select(OperationIdentity.id, OperationIdentity.first_seen_upload_id).where(
                OperationIdentity.tenant_id == tenant_id,
                OperationIdentity.id.in_(ids),
                OperationIdentity.first_seen_upload_id.is_not(None),
            )
        )
    ).all()
    if not con_archivo:
        return
    origen_de: dict[uuid.UUID, uuid.UUID | None] = {fila[0]: fila[1] for fila in con_archivo}
    respaldadas = {
        ident
        for ident, origen in (
            await session.execute(
                select(
                    OperationIdentityLink.identity_id, OperationIdentityLink.source_upload_id
                ).where(OperationIdentityLink.identity_id.in_([i for i, _ in con_archivo]))
            )
        ).all()
        if origen == origen_de[ident]
    }
    sin_respaldo = sorted(i for i, _ in con_archivo if i not in respaldadas)
    if sin_respaldo:
        await session.execute(
            update(OperationIdentity)
            .where(
                OperationIdentity.tenant_id == tenant_id,
                OperationIdentity.id.in_(sin_respaldo),
            )
            .values(content_hash=CONTENIDO_SIN_RESPALDO)
        )


async def vinculos_de(
    session: AsyncSession, tenant_id: uuid.UUID, efectos: list[tuple[str, uuid.UUID]]
) -> dict[tuple[str, uuid.UUID], list[dict[str, str]]]:
    """La foto de las identidades de estos efectos, para poder volver a colgarlas.

    La toma la relectura ANTES de soltarlas (``liberar_identidades_de``) y la
    guarda en el snapshot de cada anulado: el undo la necesita porque, al soltarse,
    una identidad huérfana se borra y con ella su clave y su ``content_hash``.
    """
    if not efectos:
        return {}
    pedidos = set(efectos)
    filas = (
        await session.execute(
            select(
                OperationIdentityLink.entity_type,
                OperationIdentityLink.entity_id,
                OperationIdentity.identity_key,
                OperationIdentity.content_hash,
                OperationIdentity.entity_type,
            )
            .join(OperationIdentity, OperationIdentity.id == OperationIdentityLink.identity_id)
            .where(
                OperationIdentityLink.tenant_id == tenant_id,
                OperationIdentityLink.entity_id.in_(sorted({eid for _, eid in efectos})),
            )
        )
    ).all()
    foto: dict[tuple[str, uuid.UUID], list[dict[str, str]]] = defaultdict(list)
    for tipo, eid, clave, contenido, entidad in filas:
        if (tipo, eid) in pedidos:
            foto[(tipo, eid)].append(
                {"identity_key": clave, "content_hash": contenido, "entity_type": entidad}
            )
    return dict(foto)


async def restaurar_vinculos(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    file_id: uuid.UUID | None,
    vinculos: list[tuple[str, uuid.UUID, dict[str, str]]],
) -> None:
    """Vuelve a colgar de su identidad los efectos que el undo de una relectura
    des-anula, con la foto de ``vinculos_de``.

    Reclama con el mismo candado que el import: si la clave sigue tomada (la
    comparte un renglón editado que la relectura conservó) se cuelga de la que
    está; si se había soltado, vuelve con su ``content_hash`` original.
    """
    por_entidad: dict[str, dict[str, str]] = defaultdict(dict)
    for _, _, ident in vinculos:
        por_entidad[ident["entity_type"]][ident["identity_key"]] = ident["content_hash"]
    ids: dict[str, uuid.UUID] = {}
    for entidad, contenidos in por_entidad.items():
        _, existentes = await _reclamar(
            session, tenant_id, file_id, contenidos=contenidos, entity=entidad
        )
        ids.update({clave: ident for clave, (ident, _, _) in existentes.items()})
    nuevos = [
        (ids[ident["identity_key"]], tipo, eid)
        for tipo, eid, ident in vinculos
        if ident["identity_key"] in ids
    ]
    if nuevos:
        await _persistir_vinculos(session, tenant_id, file_id, nuevos)


async def identidad_tomada(
    session: AsyncSession, tenant_id: uuid.UUID, clave: str
) -> tuple[uuid.UUID, str] | None:
    """¿Esta clave ya está aplicada? ``(id, content_hash)`` o ``None``.

    La usa la resolución desde "Otros": una fila que llegó ahí por conflicto no
    puede importarse sin volver a mirar el candado, porque si no la revisión
    humana sería la forma de saltearse la protección.
    """
    return (await _leer_existentes(session, tenant_id, [clave])).get(clave)


async def aplicada_desde_otro_origen(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    clave: str,
    entidad: str,
    file_id: uuid.UUID | None,
) -> bool:
    """I6-7: ¿este comprobante ya tiene un efecto vivo que NO vino de ``file_id``?

    La pregunta de una fila de «Otros» que lleva la identidad que su import soltó
    (una venta sin compra previa, B·5e). Que la clave esté tomada no alcanza para
    frenarla: la puede tener el renglón hermano del mismo comprobante, que sí se
    cargó — y ése no es un duplicado. Lo es si el efecto vino de OTRO archivo (un
    reimport, una exportación solapada): resolver la fila cargaría dos veces lo
    mismo. Se mira la clave y sus equivalentes de la misma entidad, igual que el
    planificador. Sin archivo (chat) no hay hermano que distinguir: cualquier
    efecto vivo frena.
    """
    existentes = await _leer_con_entidad(
        session, tenant_id, [clave, *claves_equivalentes(clave, entidad)]
    )
    ids = sorted(
        ident
        for k, (ident, _, ent) in existentes.items()
        if k == clave or ent == entidad
    )
    if not ids:
        return False
    origenes = (
        (
            await session.execute(
                select(OperationIdentityLink.source_upload_id).where(
                    OperationIdentityLink.tenant_id == tenant_id,
                    OperationIdentityLink.identity_id.in_(ids),
                )
            )
        )
        .scalars()
        .all()
    )
    return any(file_id is None or origen != file_id for origen in origenes)


#: Reclamos por sentencia al resolver desde «Otros» en lote: seis parámetros por
#: fila, lejos del tope de asyncpg.
_RECLAMOS_POR_SENTENCIA = 1000


async def reclamar_para_efectos(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    file_id: uuid.UUID | None,
    *,
    entidad: str,
    pedidos: list[tuple[str, str, str, uuid.UUID]],
) -> None:
    """I6-7: el efecto que nace de resolver una fila de «Otros» toma la identidad
    de su comprobante, con el MISMO candado que el import (``_reclamar``: ``ON
    CONFLICT DO NOTHING``) y el mismo vínculo (``_persistir_vinculos``).

    ``pedidos`` es ``[(clave, content_hash, tipo del efecto, id del efecto)]``.
    Si la clave está libre se reclama con ese contenido; si ya la tiene otra
    operación viva (el renglón hermano, o la que el usuario decidió cargar igual
    pese al conflicto), el efecto se cuelga de ESA identidad. En los dos casos un
    reimport del documento lo ve aplicado o en conflicto, nunca nuevo, y borrar
    el archivo del otro efecto ya no suelta la clave (``liberar_identidades_de``
    sólo suelta la que se queda sin vínculos).
    """
    if not pedidos:
        return
    contenidos: dict[str, str] = {}
    for clave, contenido, _, _ in pedidos:
        contenidos.setdefault(clave, contenido)
    ids: dict[str, uuid.UUID] = {}
    claves = list(contenidos)
    for desde in range(0, len(claves), _RECLAMOS_POR_SENTENCIA):
        tanda = {c: contenidos[c] for c in claves[desde : desde + _RECLAMOS_POR_SENTENCIA]}
        _, existentes = await _reclamar(
            session, tenant_id, file_id, contenidos=tanda, entity=entidad
        )
        ids.update({c: ident for c, (ident, _, _) in existentes.items() if c in tanda})
    vinculos = [(ids[c], tipo, eid) for c, _, tipo, eid in pedidos if c in ids]
    for desde in range(0, len(vinculos), _RECLAMOS_POR_SENTENCIA):
        await _persistir_vinculos(
            session, tenant_id, file_id, vinculos[desde : desde + _RECLAMOS_POR_SENTENCIA]
        )


def contar(planes: list[PlanDeIdentidad]) -> dict[str, int]:
    """Conteos por decisión, para el resumen del confirm."""
    total: dict[str, int] = defaultdict(int)
    for plan in planes:
        for decision in plan.decision.values():
            total[decision] += 1
    return dict(total)
