"""Consola de solicitudes de acceso — la herramienta de operación del dueño.

Véktor cerró el registro abierto: el visitante manda una **solicitud de acceso**
y el dueño la revisa a mano. Como no hay panel de administración, este script y
los mails del flujo son la herramienta real con la que se opera el producto:
acá se lee la cola, se decide a quién dejar entrar y se acuñan las cuentas.

Subcomandos
-----------
::

    list [--status ...] [--plan free|premium|all] [--limit 50] [--email-failed]
    show <request_id|email>
    approve <request_id> --vertical kiosco_almacen [--notes "..."] [--apply]
    reject  <request_id> --reason "..." [--no-email] [--apply]
    waitlist <request_id> [--notes "..."] [--apply]
    otros
    resend-invite <request_id> [--apply]
    expire-stale [--days 14] [--apply]

Reglas que este archivo sostiene
--------------------------------
1. **``--apply`` es el switch de escritura** (convención de los 16 scripts
   operativos del repo). Sin él, todo subcomando mutante es un dry-run que
   imprime exactamente qué haría y hace ``rollback``. El default es seguro.
2. **Nunca imprime el ``DATABASE_URL``** (lo maneja ``_db.async_engine_config``)
   **ni el ``ip_hash``** de una solicitud.
3. **Toda decisión pasa por ``AccessRequestService``, nunca por SQL crudo.**
   ``approve`` llama al MISMO ``approve(..., via="script")`` que la API. Si el
   script tuviera su propio camino de aprobación, las dos rutas divergirían y
   una se saltearía las guardias — incluida la del doble opt-in. Lo único que el
   script resuelve por su cuenta son LECTURAS que el servicio no expone
   (``--email-failed`` y ``otros``), que no transicionan nada.
4. **``--vertical`` sale de ``Vertical``**, así que ``otros`` es *indecible* en
   la CLI: es una opción del formulario público, nunca un vertical operativo.
5. No importa ``app.main`` (arrastraría el limiter y la app FastAPI entera):
   importa el servicio directo.

Usage::

    DATABASE_URL='postgresql://...' .venv/bin/python scripts/access_requests.py list
    ... scripts/access_requests.py approve <uuid> --vertical kiosco_almacen        # dry-run
    ... scripts/access_requests.py approve <uuid> --vertical kiosco_almacen --apply

Todas las fechas se imprimen en **UTC**. Correr desde ``backend/``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _db import async_engine_config  # noqa: E402
from sqlalchemy import func, or_, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.application.services.access_request_service import (  # noqa: E402
    ESTADOS_APROBABLES,
    ESTADOS_DE_LA_COLA,
    AccessRequestError,
    AccessRequestService,
    ResendKind,
)
from app.domain.access_request import (  # noqa: E402
    AccessRequestStatus,
    CanShareFiles,
    HistoryDepth,
    RecordsFormat,
    RequestedPlan,
    RevenueBand,
    StaffSize,
    YearsOperating,
)
from app.domain.contact_lead import EmailNotificationStatus, normalize_email  # noqa: E402
from app.domain.verticals import RequestedVertical, Vertical, parse_vertical  # noqa: E402
from app.persistence.models.access_request import AccessRequest  # noqa: E402
from app.persistence.repositories.user_repository import UserRepository  # noqa: E402

#: ``reviewed_via`` de todo lo que decide este script (la API usa ``"api"``).
VIA_SCRIPT = "script"

#: Valor de ``--status``/``--plan`` que significa "sin filtro".
TODOS = "all"

#: Días por defecto de ``expire-stale``.
DIAS_RANCIAS_DEFAULT = 14

#: Ancho mínimo de las dos columnas elásticas de ``list`` (negocio y email).
_ANCHO_MINIMO_ELASTICO = 14

#: Los tres mails del flujo: ``(atributo, columna ORM, etiqueta)``. Una sola
#: definición para el filtro SQL de ``--email-failed`` y para la lectura por
#: instancia — escritas aparte se desincronizan.
COLUMNAS_DE_EMAIL: tuple[tuple[str, Any, str], ...] = (
    ("verification_email_status", AccessRequest.verification_email_status, "verificación"),
    ("owner_notification_status", AccessRequest.owner_notification_status, "aviso al dueño"),
    ("decision_email_status", AccessRequest.decision_email_status, "decisión"),
)

#: Glosa corta de los códigos del screening. Es copy de operación, no lógica: si
#: mañana aparece una banda nueva, el código crudo se imprime igual (sin glosa) —
#: nunca se inventa una etiqueta para un valor desconocido.
GLOSA: dict[str, str] = {
    YearsOperating.LT_6M: "menos de 6 meses",
    YearsOperating.M6_2Y: "entre 6 meses y 2 años",
    YearsOperating.Y2_5Y: "entre 2 y 5 años",
    YearsOperating.GT_5Y: "más de 5 años",
    StaffSize.SOLO: "trabaja solo",
    StaffSize.S2_5: "entre 2 y 5 personas",
    StaffSize.S6_15: "entre 6 y 15 personas",
    StaffSize.GT_15: "más de 15 personas",
    RevenueBand.LT_3M: "menos de $3M por mes",
    RevenueBand.M3_10M: "entre $3M y $10M por mes",
    RevenueBand.M10_30M: "entre $10M y $30M por mes",
    RevenueBand.GT_30M: "más de $30M por mes",
    RevenueBand.NO_CONTESTA: "prefirió no contestar",
    RecordsFormat.PAPEL: "anota en papel",
    RecordsFormat.PLANILLA: "usa planillas",
    RecordsFormat.SISTEMA: "ya usa un sistema",
    RecordsFormat.MIXTO: "mezcla papel y digital",
    RecordsFormat.NINGUNO: "no registra nada",
    HistoryDepth.LT_6M: "menos de 6 meses de historial",
    HistoryDepth.M6_1Y: "entre 6 meses y 1 año de historial",
    HistoryDepth.Y1_3Y: "entre 1 y 3 años de historial",
    HistoryDepth.GT_3Y: "más de 3 años de historial",
    HistoryDepth.NINGUNO: "sin historial",
    CanShareFiles.SI_ORDENADOS: "sí, ordenados",
    CanShareFiles.SI_DESPROLIJOS: "sí, pero desprolijos",
    CanShareFiles.NO: "no puede subir archivos",
}


# ── Formato ──────────────────────────────────────────────────────────────────


def ancho_terminal() -> int:
    """Columnas de la terminal, con un piso razonable para no romper el layout."""
    return max(80, shutil.get_terminal_size(fallback=(120, 24)).columns)


def recortar(texto: str, ancho: int) -> str:
    """Recorta a ``ancho`` agregando ``…`` cuando sobra. Nunca devuelve ``None``."""
    if ancho <= 1 or len(texto) <= ancho:
        return texto
    return texto[: ancho - 1] + "…"


def fecha(valor: datetime | None) -> str:
    """``YYYY-MM-DD HH:MM`` en UTC, o ``—`` si no hay dato (nunca un default)."""
    if valor is None:
        return "—"
    return valor.strftime("%Y-%m-%d %H:%M")


def opcional(valor: str | None) -> str:
    """Texto libre para pantalla; ``—`` si está vacío. No inventa contenido."""
    if valor is None or not valor.strip():
        return "—"
    return valor.strip()


def con_glosa(codigo: str) -> str:
    """``codigo (glosa)`` si hay glosa conocida; el código pelado si no."""
    glosa = GLOSA.get(codigo)
    return f"{codigo} ({glosa})" if glosa else codigo


def prioridad(solicitud: AccessRequest) -> str:
    """``ALTA``/``NORMAL`` — DERIVADO de ``requested_plan``, no hay columna propia."""
    return (
        "ALTA"
        if solicitud.requested_plan == RequestedPlan.PREMIUM.value
        else "NORMAL"
    )


def rubro(solicitud: AccessRequest) -> str:
    """Vertical asignado si ya se aprobó; si no, el declarado por el solicitante."""
    if solicitud.assigned_vertical_code is None:
        return solicitud.requested_vertical
    return solicitud.assigned_vertical_code


def emails_fallidos(solicitud: AccessRequest) -> list[str]:
    """Etiquetas de los mails de esta solicitud que quedaron en ``failed``."""
    fallado = EmailNotificationStatus.FAILED.value
    return [
        etiqueta
        for atributo, _, etiqueta in COLUMNAS_DE_EMAIL
        if getattr(solicitud, atributo) == fallado
    ]


# ── list ─────────────────────────────────────────────────────────────────────


def imprimir_tabla(solicitudes: list[AccessRequest], total: int) -> None:
    """Tabla de la cola: una línea de datos + una línea con el id por solicitud.

    El id va en su propia línea a propósito: es un UUID de 36 caracteres y
    meterlo como columna dejaría al negocio y al email recortados a nada, que
    son justamente los dos campos con los que el dueño reconoce una solicitud.
    En esa segunda línea viaja también el detalle del rubro ``otros``, recortado
    al ancho de la terminal.
    """
    if not solicitudes:
        print("  (sin solicitudes para ese filtro)")
        return

    ancho = ancho_terminal()
    fijas = 9 + 7 + 10 + 16 + 16 + (6 * 2)  # + separadores de 2 espacios
    elastico = max(_ANCHO_MINIMO_ELASTICO * 2, ancho - fijas)
    ancho_negocio = elastico // 2
    ancho_email = elastico - ancho_negocio

    encabezado = (
        f"{'PRIORIDAD':<9}  {'PLAN':<7}  {'ESTADO':<10}  {'VERIFICADA':<16}  "
        f"{'NEGOCIO':<{ancho_negocio}}  {'EMAIL':<{ancho_email}}  {'RUBRO':<16}"
    )
    print(encabezado)
    print("─" * min(len(encabezado), ancho))

    for solicitud in solicitudes:
        print(
            f"{prioridad(solicitud):<9}  "
            f"{solicitud.requested_plan.upper():<7}  "
            f"{solicitud.status:<10}  "
            f"{fecha(solicitud.email_verified_at):<16}  "
            f"{recortar(solicitud.business_name, ancho_negocio):<{ancho_negocio}}  "
            f"{recortar(solicitud.email, ancho_email):<{ancho_email}}  "
            f"{recortar(rubro(solicitud), 16):<16}"
        )
        detalle = f"           id: {solicitud.id}"
        if solicitud.requested_vertical == RequestedVertical.OTROS.value:
            detalle += f'  · "otros": {opcional(solicitud.vertical_other_text)}'
        fallidos = emails_fallidos(solicitud)
        if fallidos:
            detalle += f"  · MAIL FALLIDO: {', '.join(fallidos)}"
        print(recortar(detalle, ancho))

    print(f"\n  mostrando {len(solicitudes)} de {total} solicitud/es.")


async def listar_por_estado(
    servicio: AccessRequestService,
    *,
    estado: AccessRequestStatus | None,
    plan: RequestedPlan | None,
    limit: int,
) -> tuple[list[AccessRequest], int]:
    """``list_requests`` tal cual: sin estado devuelve LA COLA (pending+waitlist)."""
    return await servicio.list_requests(
        status=estado, requested_plan=plan, limit=limit, offset=0
    )


async def listar_todos_los_estados(
    session: AsyncSession, *, plan: RequestedPlan | None, limit: int
) -> tuple[list[AccessRequest], int]:
    """``--status all``: el archivo histórico completo, las más nuevas primero.

    Consulta propia (read-only, sin transiciones) porque ``list_requests`` no
    tiene un modo "sin filtro de estado": sin estado devuelve LA COLA. Lo que
    NO se reimplementa acá es el orden de prioridad —Premium primero, después la
    verificada más antigua—: ese vive en el servicio y solo tiene sentido sobre
    la cola de revisión. El histórico se ordena por fecha, en SQL.
    """
    condiciones: list[Any] = []
    if plan is not None:
        condiciones.append(AccessRequest.requested_plan == plan.value)

    filas = (
        (
            await session.execute(
                select(AccessRequest)
                .where(*condiciones)
                .order_by(AccessRequest.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    total = (
        await session.execute(
            select(func.count()).select_from(AccessRequest).where(*condiciones)
        )
    ).scalar_one()
    return list(filas), int(total)


async def listar_con_email_fallido(
    session: AsyncSession,
    *,
    estado: AccessRequestStatus | None,
    plan: RequestedPlan | None,
    limit: int,
) -> tuple[list[AccessRequest], int]:
    """Solicitudes con AL MENOS uno de los tres mails en ``failed``.

    Consulta propia (read-only, sin transiciones) porque el servicio no expone
    este filtro. Sin panel de administración es la ÚNICA forma de enterarse de
    que alguien quedó esperando sin recibir nada: el worker agota sus reintentos,
    marca la columna en ``failed`` y no avisa por ningún otro lado.

    Barre todos los estados salvo que se pase ``--status`` explícito.
    """
    fallado = EmailNotificationStatus.FAILED.value
    condiciones: list[Any] = [
        or_(*[columna == fallado for _, columna, _ in COLUMNAS_DE_EMAIL])
    ]
    if estado is not None:
        condiciones.append(AccessRequest.status == estado.value)
    if plan is not None:
        condiciones.append(AccessRequest.requested_plan == plan.value)

    filas = (
        (
            await session.execute(
                select(AccessRequest)
                .where(*condiciones)
                .order_by(AccessRequest.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    total = (
        await session.execute(
            select(func.count()).select_from(AccessRequest).where(*condiciones)
        )
    ).scalar_one()
    return list(filas), int(total)


async def cmd_list(session: AsyncSession, args: argparse.Namespace) -> int:
    servicio = AccessRequestService(session)
    estado = (
        None
        if args.status in (None, TODOS)
        else AccessRequestStatus(args.status)
    )
    plan = None if args.plan in (None, TODOS) else RequestedPlan(args.plan)

    if args.email_failed:
        # El estado se anexa al título igual que el plan: sin eso, un
        # `--email-failed --status pending` que devuelve poco se lee como si no
        # hubiera mails fallidos, cuando en realidad hay un filtro más puesto.
        titulo = "Solicitudes con algún mail FALLIDO"
        if estado is not None:
            titulo += f" · estado: {estado.value}"
        filas, total = await listar_con_email_fallido(
            session, estado=estado, plan=plan, limit=args.limit
        )
    elif args.status == TODOS:
        titulo = "Todas las solicitudes (histórico, las más nuevas primero)"
        filas, total = await listar_todos_los_estados(
            session, plan=plan, limit=args.limit
        )
    elif estado is None:
        titulo = (
            f"Cola de revisión ({' + '.join(ESTADOS_DE_LA_COLA)}) "
            "— Premium primero, después la verificada más antigua"
        )
        filas, total = await listar_por_estado(
            servicio, estado=None, plan=plan, limit=args.limit
        )
    else:
        titulo = f"Solicitudes en estado '{estado.value}'"
        filas, total = await listar_por_estado(
            servicio, estado=estado, plan=plan, limit=args.limit
        )

    if plan is not None:
        titulo += f" · plan solicitado: {plan.value}"
    print(f"\n=== {titulo} ===\n")
    imprimir_tabla(filas, total)
    print(
        "\n  Ver el detalle:  scripts/access_requests.py show <id|email>\n"
        "  Fechas en UTC. La prioridad se deriva del plan solicitado; "
        "una cuenta Premium NO recibe una suscripción Premium."
    )
    return 0


# ── show ─────────────────────────────────────────────────────────────────────


def imprimir_ficha(solicitud: AccessRequest) -> None:
    """Ficha completa de una solicitud. **Nunca imprime ``ip_hash``.**

    El ``ip_hash`` es trazabilidad anti-abuso, no información de decisión: no
    ayuda a decidir una admisión y mostrarlo en pantalla solo agrega un dato
    personal a la terminal (y al scrollback) sin ninguna contrapartida.
    """
    ancho = ancho_terminal()
    print(f"\n=== Solicitud {solicitud.id} ===\n")

    print(f"  {'Estado:':<22}{solicitud.status}")
    # Etiqueta corta a propósito: "Prioridad de revisión:" mide exactamente 22 y
    # se comía el espacio de separación con el valor.
    print(f"  {'Prioridad:':<22}{prioridad(solicitud)}")
    # Las dos líneas van SEPARADAS a propósito: es lo que evita que el operador
    # crea que está concediendo Premium. La intención Premium no es una
    # suscripción Premium — no hay plan pago operativo, ni cobro, ni límites.
    print(f"  {'Plan solicitado:':<22}{solicitud.requested_plan.upper()}")
    print(f"  {'Suscripción a crear:':<22}FREE")

    print("\n  --- Contacto ---")
    print(f"  {'Negocio:':<22}{solicitud.business_name}")
    print(f"  {'Contacto:':<22}{solicitud.full_name}")
    print(f"  {'Email:':<22}{solicitud.email}")
    print(f"  {'Email confirmado:':<22}{fecha(solicitud.email_verified_at)}")
    print(f"  {'Teléfono:':<22}{opcional(solicitud.phone)}")

    print("\n  --- Rubro ---")
    print(f"  {'Declarado:':<22}{solicitud.requested_vertical}")
    if solicitud.requested_vertical == RequestedVertical.OTROS.value:
        print(
            recortar(
                f"  {'Detalle:':<22}{opcional(solicitud.vertical_other_text)}", ancho
            )
        )
    asignado = solicitud.assigned_vertical_code
    print(
        f"  {'Asignado:':<22}"
        f"{'— (lo decide el dueño al aprobar)' if asignado is None else asignado}"
    )

    print("\n  --- Screening del negocio ---")
    print(f"  {'Antigüedad:':<22}{con_glosa(solicitud.years_operating)}")
    print(f"  {'Equipo:':<22}{con_glosa(solicitud.staff_size)}")
    print(f"  {'Facturación:':<22}{con_glosa(solicitud.monthly_revenue_band)}")
    print(f"  {'Preocupación:':<22}{solicitud.main_concern}")
    print(f"  {'Registros:':<22}{con_glosa(solicitud.records_format)}")
    print(f"  {'Historial:':<22}{con_glosa(solicitud.history_depth)}")
    print(f"  {'Puede subir datos:':<22}{con_glosa(solicitud.can_share_files)}")
    print(recortar(f"  {'Cómo lo lleva:':<22}{opcional(solicitud.records_notes)}", ancho))
    print(recortar(f"  {'Algo más:':<22}{opcional(solicitud.applicant_notes)}", ancho))

    print("\n  --- Emails del flujo ---")
    print(f"  {'Verificación:':<22}{solicitud.verification_email_status}")
    print(f"  {'Aviso al dueño:':<22}{solicitud.owner_notification_status}")
    print(f"  {'Decisión:':<22}{solicitud.decision_email_status}")
    fallidos = emails_fallidos(solicitud)
    if fallidos:
        print(
            f"  ⚠  Quedaron en failed: {', '.join(fallidos)}. "
            "Reintentá con: resend-invite <id> --apply"
        )

    print("\n  --- Revisión ---")
    print(f"  {'Revisada:':<22}{fecha(solicitud.reviewed_at)}")
    print(f"  {'Vía:':<22}{opcional(solicitud.reviewed_via)}")
    print(recortar(f"  {'Notas:':<22}{opcional(solicitud.review_notes)}", ancho))
    print(
        recortar(f"  {'Motivo de rechazo:':<22}{opcional(solicitud.rejection_reason)}", ancho)
    )
    if solicitud.approved_tenant_id is not None:
        print(f"  {'Tenant acuñado:':<22}{solicitud.approved_tenant_id}")
    if solicitud.approved_user_id is not None:
        print(f"  {'Usuario acuñado:':<22}{solicitud.approved_user_id}")

    print("\n  --- Trazabilidad ---")
    print(f"  {'Creada:':<22}{fecha(solicitud.created_at)}")
    print(f"  {'Origen (CTA):':<22}{opcional(solicitud.cta_source)}")
    print(
        f"  {'Vino por Google:':<22}"
        f"{'sí' if solicitud.google_subject else 'no'}"
    )
    print(
        f"  {'Consentimiento:':<22}{solicitud.consent_version} "
        f"({fecha(solicitud.consent_accepted_at)})"
    )

    print("\n  --- Próximo paso ---")
    if solicitud.status in ESTADOS_APROBABLES and solicitud.email_verified_at:
        verticales = " | ".join(v.value for v in Vertical)
        print(
            f"  approve {solicitud.id} --vertical <{verticales}> --apply\n"
            f"  reject  {solicitud.id} --reason \"...\" --apply\n"
            f"  waitlist {solicitud.id} --apply"
        )
    elif solicitud.status == AccessRequestStatus.UNVERIFIED.value:
        print(
            "  Nadie confirmó el email todavía: no se puede aprobar (doble opt-in).\n"
            f"  Reenviar el link:  resend-invite {solicitud.id} --apply"
        )
    elif solicitud.status == AccessRequestStatus.APPROVED.value:
        print(
            "  Ya aprobada. Si nunca pudo entrar (mail fallido o link vencido):\n"
            f"  resend-invite {solicitud.id} --apply"
        )
    else:
        print(f"  Estado '{solicitud.status}': no hay acción pendiente.")


async def buscar_por_email(
    session: AsyncSession, texto: str
) -> list[AccessRequest]:
    """Solicitudes de ese email, la más reciente primero (puede haber varias)."""
    return list(
        (
            await session.execute(
                select(AccessRequest)
                .where(AccessRequest.email == normalize_email(texto))
                .order_by(AccessRequest.created_at.desc())
            )
        )
        .scalars()
        .all()
    )


async def cmd_show(session: AsyncSession, args: argparse.Namespace) -> int:
    referencia: str = args.referencia
    try:
        request_id = uuid.UUID(referencia)
    except ValueError:
        solicitudes = await buscar_por_email(session, referencia)
        if not solicitudes:
            print(f"No hay ninguna solicitud con id ni email {referencia!r}.")
            return 1
        imprimir_ficha(solicitudes[0])
        if len(solicitudes) > 1:
            print(
                f"\n  Ese email tiene {len(solicitudes)} solicitudes; arriba está la "
                "más reciente. Las otras:"
            )
            for otra in solicitudes[1:]:
                print(f"    {otra.id}  {otra.status:<10}  creada {fecha(otra.created_at)}")
        return 0

    solicitud = await AccessRequestService(session).get(request_id)
    if solicitud is None:
        print(f"No existe la solicitud {request_id}.")
        return 1
    imprimir_ficha(solicitud)
    return 0


# ── approve ──────────────────────────────────────────────────────────────────


async def bloqueos_para_aprobar(
    session: AsyncSession, solicitud: AccessRequest
) -> list[str]:
    """Motivos por los que ``approve()`` rechazaría esta solicitud. **Solo preview.**

    Reproduce en modo lectura las cuatro guardias de
    ``AccessRequestService.approve`` para que el dry-run no prometa una cuenta
    que después no se va a acuñar. La verificación AUTORITATIVA sigue siendo la
    del servicio, dentro de la transacción con ``FOR UPDATE``: esto es
    diagnóstico, no control de acceso.

    La cuarta (identidad de Google ya vinculada) pregunta por el MISMO método
    público que usa la guardia del servicio, ``google_identity_taken``: con una
    consulta propia acá, el preview y el ``--apply`` podrían volver a divergir.
    """
    motivos: list[str] = []
    if solicitud.status == AccessRequestStatus.APPROVED.value:
        motivos.append(
            "ya está aprobada — volver a aprobarla es idempotente y NO acuña un "
            "segundo tenant"
        )
        return motivos
    if solicitud.status not in ESTADOS_APROBABLES:
        motivos.append(
            f"estado '{solicitud.status}' (aprobables: {', '.join(ESTADOS_APROBABLES)})"
        )
    if solicitud.email_verified_at is None:
        motivos.append("nadie confirmó el email (doble opt-in pendiente)")
    if await UserRepository(session).get_by_email_any_tenant(solicitud.email):
        motivos.append(f"ya existe una cuenta con {solicitud.email}")
    if solicitud.google_subject is not None and await AccessRequestService(
        session
    ).google_identity_taken(solicitud.google_subject):
        motivos.append(
            "la identidad de Google de la solicitud ya está vinculada a otra cuenta"
        )
    return motivos


def imprimir_plan_de_aprobacion(
    solicitud: AccessRequest, vertical: Vertical, notas: str | None
) -> None:
    """Qué se crearía exactamente al aprobar: las 5 filas, con sus valores reales."""
    print(f"  {'Negocio:':<22}{solicitud.business_name}")
    print(f"  {'Contacto:':<22}{solicitud.full_name}")
    print(f"  {'Email:':<22}{solicitud.email}")
    declarado = solicitud.requested_vertical
    if solicitud.requested_vertical == RequestedVertical.OTROS.value:
        declarado += f' — "{opcional(solicitud.vertical_other_text)}"'
    print(recortar(f"  {'Rubro declarado:':<22}{declarado}", ancho_terminal()))
    print(f"  {'Vertical a asignar:':<22}{vertical.value}")
    if solicitud.requested_vertical != vertical.value:
        print(
            f"  {'':<22}(corrige el rubro declarado — es la decisión del dueño, "
            "no un error)"
        )
    # Las dos líneas separadas, otra vez: es el punto donde más caro sale
    # confundir la intención con la suscripción.
    print(f"  {'Plan solicitado:':<22}{solicitud.requested_plan.upper()}")
    print(f"  {'Suscripción a crear:':<22}FREE")
    print(recortar(f"  {'Notas de revisión:':<22}{opcional(notas)}", ancho_terminal()))

    print("\n  Se crearían estas 5 filas (vía provision_tenant):")
    print(
        f"    1. Tenant           display_name={solicitud.business_name!r}, "
        "currency='ARS', status='ACTIVE'"
    )
    print(
        f"    2. User             email={solicitud.email!r}, role_code='OWNER', "
        "is_active=True (sin contraseña: entra por el link de invitación)"
    )
    print(
        "    3. Subscription     plan_code='FREE'  ← NO "
        f"{solicitud.requested_plan.upper()!r}: no hay plan pago operativo"
    )
    print(
        f"    4. BusinessProfile  vertical_code={vertical.value!r}, "
        "onboarding_completed=False (los números se piden en el primer login)"
    )
    print("    5. MomentumProfile  vacío")
    print(
        "\n  Además: un token de invitación (TTL 72 h) y el mail "
        '"tu acceso a Véktor está listo".'
    )


async def cmd_approve(session: AsyncSession, args: argparse.Namespace) -> int:
    request_id = exigir_uuid(args.request_id)
    # `choices` de argparse ya restringe el valor; `parse_vertical` es la
    # validación canónica y falla ruidoso si alguien la evade.
    vertical = parse_vertical(args.vertical)

    servicio = AccessRequestService(session)
    solicitud = await servicio.get(request_id)
    if solicitud is None:
        print(f"No existe la solicitud {request_id}.")
        return 1

    print(f"\n[{'APPLY' if args.apply else 'DRY-RUN'}] Aprobar solicitud {request_id}\n")
    imprimir_plan_de_aprobacion(solicitud, vertical, args.notes)

    bloqueos = await bloqueos_para_aprobar(session, solicitud)
    if bloqueos:
        print("\n  ⚠  El servicio va a rechazar esta aprobación:")
        for motivo in bloqueos:
            print(f"     - {motivo}")

    if not args.apply:
        print("\nDry-run: no se escribió nada. Repetí con --apply para aprobar.")
        return 0

    try:
        resultado = await servicio.approve(
            request_id,
            vertical=vertical,
            reviewer_user_id=None,
            via=VIA_SCRIPT,
            notes=args.notes,
        )
    except AccessRequestError as exc:
        # Cubre las cuatro del flujo (NotFound / NotApprovable / EmailTaken /
        # GoogleIdentityTaken): todas heredan de AccessRequestError y el mensaje
        # ya explica cuál fue.
        print(f"\nERROR: no se aprobó. {exc}")
        return 1

    if resultado.already_approved:
        print(
            f"\nSIN CAMBIOS: ya estaba aprobada (tenant {resultado.tenant_id}). "
            "No se acuñó un segundo tenant."
        )
        return 0
    print(
        f"\nCOMMIT: cuenta acuñada.\n"
        f"  tenant_id: {resultado.tenant_id}\n"
        f"  user_id:   {resultado.user_id}\n"
        f"  suscripción: FREE (el plan solicitado fue "
        f"{solicitud.requested_plan.upper()})\n"
        "  Se encoló el mail con el link para definir la contraseña."
    )
    return 0


# ── reject / waitlist ────────────────────────────────────────────────────────


#: Largo mínimo del motivo del rechazo. Espeja la validación de
#: ``AccessRequestService.reject()`` para poder avisarlo en el dry-run: es
#: PREVIEW, no control de acceso — el que valida de verdad es el servicio.
_MINIMO_MOTIVO_RECHAZO = 3


async def cmd_reject(session: AsyncSession, args: argparse.Namespace) -> int:
    request_id = exigir_uuid(args.request_id)
    servicio = AccessRequestService(session)
    solicitud = await servicio.get(request_id)
    if solicitud is None:
        print(f"No existe la solicitud {request_id}.")
        return 1

    notifica = not args.no_email
    print(f"\n[{'APPLY' if args.apply else 'DRY-RUN'}] Rechazar solicitud {request_id}\n")
    print(f"  {'Negocio:':<22}{solicitud.business_name}")
    print(f"  {'Email:':<22}{solicitud.email}")
    print(f"  {'Estado actual:':<22}{solicitud.status}")
    print(f"  {'Estado nuevo:':<22}{AccessRequestStatus.REJECTED.value}")
    print(recortar(f"  {'Motivo (interno):':<22}{args.reason}", ancho_terminal()))
    print(
        f"  {'Le avisa por mail:':<22}"
        f"{'sí' if notifica else 'no (--no-email)'}"
    )
    print(
        f"  {'':<22}El mail NUNCA transcribe el motivo: es una nota interna."
    )
    if solicitud.status == AccessRequestStatus.APPROVED.value:
        print("\n  ⚠  Ya está aprobada: el servicio no la va a rechazar.")
    elif solicitud.status == AccessRequestStatus.REJECTED.value:
        print("\n  ⚠  Ya estaba rechazada: es idempotente y no re-notifica.")
    if len(args.reason.strip()) < _MINIMO_MOTIVO_RECHAZO:
        # Que el dry-run no prometa un rechazo que el --apply va a rebotar.
        print(
            "\n  ⚠  El servicio va a rechazar esta operación:\n"
            f"     - el motivo es obligatorio (mínimo {_MINIMO_MOTIVO_RECHAZO} "
            "caracteres)."
        )

    if not args.apply:
        print("\nDry-run: no se escribió nada. Repetí con --apply para rechazar.")
        return 0

    try:
        solicitud = await servicio.reject(
            request_id,
            reviewer_user_id=None,
            via=VIA_SCRIPT,
            reason=args.reason,
            notify=notifica,
        )
    except (AccessRequestError, ValueError) as exc:
        print(f"\nERROR: no se rechazó. {exc}")
        return 1
    print(f"\nCOMMIT: solicitud en estado '{solicitud.status}'.")
    return 0


async def cmd_waitlist(session: AsyncSession, args: argparse.Namespace) -> int:
    request_id = exigir_uuid(args.request_id)
    servicio = AccessRequestService(session)
    solicitud = await servicio.get(request_id)
    if solicitud is None:
        print(f"No existe la solicitud {request_id}.")
        return 1

    print(
        f"\n[{'APPLY' if args.apply else 'DRY-RUN'}] Lista de espera para {request_id}\n"
    )
    print(f"  {'Negocio:':<22}{solicitud.business_name}")
    print(f"  {'Email:':<22}{solicitud.email}")
    print(f"  {'Estado actual:':<22}{solicitud.status}")
    print(f"  {'Estado nuevo:':<22}{AccessRequestStatus.WAITLIST.value}")
    print(recortar(f"  {'Notas de revisión:':<22}{opcional(args.notes)}", ancho_terminal()))
    print(
        f"  {'':<22}No es terminal: después se puede aprobar o rechazar.\n"
        f"  {'':<22}Se le avisa por mail que quedó en lista de espera."
    )
    if solicitud.status == AccessRequestStatus.APPROVED.value:
        print("\n  ⚠  Ya está aprobada: el servicio no la va a postergar.")
    if solicitud.email_verified_at is None:
        # Postergar manda mail y deja el trámite abierto: sin doble opt-in sería
        # escribirle a una casilla sin consentimiento y trabarle el reintento.
        print(
            "\n  ⚠  Nadie confirmó el email: el servicio no la va a postergar.\n"
            "     Para descartarla sin escribirle: reject --no-email."
        )

    if not args.apply:
        print("\nDry-run: no se escribió nada. Repetí con --apply para postergarla.")
        return 0

    try:
        solicitud = await servicio.waitlist(
            request_id, reviewer_user_id=None, via=VIA_SCRIPT, notes=args.notes
        )
    except AccessRequestError as exc:
        print(f"\nERROR: no se postergó. {exc}")
        return 1
    print(f"\nCOMMIT: solicitud en estado '{solicitud.status}'.")
    return 0


# ── otros ────────────────────────────────────────────────────────────────────


def clave_de_rubro(texto: str | None) -> str:
    """Normaliza el texto libre para agrupar ("Panadería " ≡ "panaderia")."""
    if texto is None or not texto.strip():
        return "(sin detalle)"
    return " ".join(texto.strip().lower().split())


async def cmd_otros(session: AsyncSession, args: argparse.Namespace) -> int:
    """Agrupa por rubro declarado las solicitudes 'otros' que están en la cola.

    Es la señal de producto del formulario: cuando una misma descripción se
    repite, ese es el vertical que conviene construir. Consulta propia porque el
    servicio no expone un agrupamiento; es read-only y no transiciona nada.
    """
    filas = (
        (
            await session.execute(
                select(AccessRequest)
                .where(
                    AccessRequest.requested_vertical == RequestedVertical.OTROS.value,
                    AccessRequest.status.in_(ESTADOS_DE_LA_COLA),
                )
                .order_by(AccessRequest.created_at.asc())
            )
        )
        .scalars()
        .all()
    )

    print(
        f"\n=== Rubros \"otros\" en la cola ({' + '.join(ESTADOS_DE_LA_COLA)}) ===\n"
    )
    if not filas:
        print("  (ninguna solicitud de rubro 'otros' esperando revisión)")
        return 0

    grupos: dict[str, list[AccessRequest]] = defaultdict(list)
    for solicitud in filas:
        grupos[clave_de_rubro(solicitud.vertical_other_text)].append(solicitud)

    ancho = ancho_terminal()
    ordenados = sorted(grupos.items(), key=lambda par: (-len(par[1]), par[0]))
    for clave, solicitudes in ordenados:
        print(recortar(f"  {len(solicitudes):>3}  {clave}", ancho))
        for solicitud in solicitudes:
            linea = (
                f"         · {solicitud.business_name} — {solicitud.email} "
                f"({solicitud.status}, {solicitud.requested_plan})"
            )
            print(recortar(linea, ancho))
        print()

    print(
        f"  Total: {len(filas)} solicitud/es de rubro 'otros' en {len(grupos)} "
        "descripción/es distinta/s.\n"
        "  'otros' NO es un vertical operativo: al aprobar hay que asignar uno de "
        f"los reales ({', '.join(v.value for v in Vertical)})."
    )
    return 0


# ── resend-invite ────────────────────────────────────────────────────────────


async def cmd_resend_invite(session: AsyncSession, args: argparse.Namespace) -> int:
    """Reencola el mail que el solicitante está esperando (verificación o invitación).

    Lo que se imprime antes del ``--apply`` es **preview, no control de acceso**
    —mismo criterio que ``bloqueos_para_aprobar``—: avisa qué va a pasar, pero la
    autoridad sobre qué estado es reencolable es ``AccessRequestService``, que
    decide adentro de la transacción con ``FOR UPDATE``. Por eso el preview de un
    estado sin mail pendiente NO corta: con ``--apply`` se llama al servicio
    igual y su ``AccessRequestNotResendable`` es la que devuelve el código 1. Si
    el script cortara por su cuenta, mañana el servicio cambia la lista de
    estados reencolables y la CLI se queda con la regla vieja en silencio.
    """
    request_id = exigir_uuid(args.request_id)
    servicio = AccessRequestService(session)
    solicitud = await servicio.get(request_id)
    if solicitud is None:
        print(f"No existe la solicitud {request_id}.")
        return 1

    print(
        f"\n[{'APPLY' if args.apply else 'DRY-RUN'}] Reenviar el mail de {request_id}\n"
    )
    print(f"  {'Negocio:':<22}{solicitud.business_name}")
    print(f"  {'Email:':<22}{solicitud.email}")
    print(f"  {'Estado:':<22}{solicitud.status}")
    fallidos = emails_fallidos(solicitud)
    print(f"  {'Mails en failed:':<22}{', '.join(fallidos) if fallidos else '—'}")

    if solicitud.status == AccessRequestStatus.UNVERIFIED.value:
        print(
            f"  {'Se reenviaría:':<22}el link de verificación de email "
            "(doble opt-in).\n"
            f"  {'':<22}Si el cooldown de tokens todavía corre, no se reemite."
        )
    elif solicitud.status == AccessRequestStatus.APPROVED.value:
        print(
            f"  {'Se reenviaría:':<22}una invitación NUEVA para definir la "
            "contraseña (TTL 72 h).\n"
            f"  {'':<22}Invalida los links de reset vigentes del usuario.\n"
            f"  {'':<22}No cambia el estado ni acuña un segundo tenant."
        )
        if solicitud.approved_user_id is None:
            # FK ON DELETE SET NULL: el usuario acuñado ya no existe. El servicio
            # no elige otro destinatario (sería inventar), así que se avisa acá
            # en vez de prometer un mail que el --apply después rechaza.
            print(
                "\n  ⚠  El servicio va a rechazar este reenvío:\n"
                "     - la solicitud aprobada ya no apunta a ningún usuario "
                "(se borró la cuenta): no hay a quién invitar."
            )
    else:
        print(
            f"\n  ⚠  En estado '{solicitud.status}' no hay ningún mail que el "
            "solicitante esté esperando;\n"
            "     el servicio va a rechazar el reenvío. Solo 'unverified' "
            "(verificación) y\n"
            "     'approved' (invitación) tienen algo para reencolar."
        )

    if not args.apply:
        print("\nDry-run: no se escribió nada. Repetí con --apply para reenviarlo.")
        return 0

    try:
        resultado = await servicio.resend_invite(request_id, via=VIA_SCRIPT)
    except AccessRequestError as exc:
        print(f"\nERROR: no se reenvió. {exc}")
        return 1

    if not resultado.enqueued:
        print(
            "\nSIN CAMBIOS: el cooldown de tokens todavía corre "
            "(protección contra un mail por click). Volvé a intentar más tarde."
        )
        return 0
    que = (
        "link de verificación"
        if resultado.kind is ResendKind.VERIFICATION
        else "invitación para definir la contraseña"
    )
    print(f"\nCOMMIT: se encoló el {que} a {solicitud.email}.")
    return 0


# ── expire-stale ─────────────────────────────────────────────────────────────


async def cmd_expire_stale(session: AsyncSession, args: argparse.Namespace) -> int:
    """Higiene de la cola: expira las ``unverified`` que nadie confirmó hace mucho.

    **No destraba a nadie.** Si el solicitante vuelve a mandar el formulario,
    ``create()`` le reemite el token igual (vencido el cooldown), esté o no
    expirada la vieja. Lo único que resuelve es que esas filas dejen de
    acumularse para siempre ensuciando ``list --status unverified`` y ocupando el
    índice único parcial sin nadie del otro lado.
    """
    servicio = AccessRequestService(session)
    candidatas = await servicio.find_stale(older_than_days=args.days)

    print(
        f"\n[{'APPLY' if args.apply else 'DRY-RUN'}] Expirar solicitudes "
        f"'unverified' con más de {args.days} día/s\n"
    )
    print(f"  Solicitudes que se tocarían: {len(candidatas)}")
    if not candidatas:
        print("\n  Nada que expirar.")
        return 0

    # Se lista SIEMPRE el detalle, no solo el conteo: con --days mal puesto, ver
    # "expiraría 47" sin saber cuáles es justo el momento en que uno aprieta
    # --apply sin mirar.
    ancho = ancho_terminal()
    print()
    for solicitud in candidatas:
        linea = (
            f"    {solicitud.id}  creada {fecha(solicitud.created_at)}  "
            f"{solicitud.business_name} — {solicitud.email} "
            f"({solicitud.requested_plan})"
        )
        print(recortar(linea, ancho))

    print(
        f"\n  Pasarían de '{AccessRequestStatus.UNVERIFIED.value}' a "
        f"'{AccessRequestStatus.EXPIRED.value}'. No se manda ningún mail "
        "(nadie confirmó esas casillas)."
    )
    if not args.apply:
        print("\nDry-run: no se escribió nada. Repetí con --apply para expirarlas.")
        return 0

    expiradas = await servicio.expire_stale(older_than_days=args.days, via=VIA_SCRIPT)
    print(f"\nCOMMIT: {len(expiradas)} solicitud/es expirada/s (auditadas).")
    return 0


# ── CLI ──────────────────────────────────────────────────────────────────────


def exigir_uuid(valor: str) -> uuid.UUID:
    try:
        return uuid.UUID(valor)
    except ValueError:
        print(f"ERROR: {valor!r} no es un request_id (uuid) válido.")
        sys.exit(2)


def agregar_apply(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--apply",
        action="store_true",
        help="escribe de verdad (sin esta flag es un dry-run que hace rollback)",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Consola de solicitudes de acceso. Sin --apply, todo comando mutante "
            "es un dry-run que imprime qué haría y hace rollback."
        )
    )
    sub = parser.add_subparsers(dest="comando", required=True)

    # --- list ---
    p_list = sub.add_parser("list", help="listar la cola de revisión")
    p_list.add_argument(
        "--status",
        # Se DERIVA del enum (más `all`) en vez de escribir la lista a mano: así
        # `expired` —que crea este mismo script con expire-stale— es listable, y
        # un estado nuevo no queda invisible en silencio.
        choices=[s.value for s in AccessRequestStatus] + [TODOS],
        default=None,
        help="sin filtro lista LA COLA (pending + waitlist); 'all' es el histórico",
    )
    p_list.add_argument(
        "--plan",
        choices=[p.value for p in RequestedPlan] + [TODOS],
        default=TODOS,
        help="filtrar por plan SOLICITADO (es intención, no suscripción)",
    )
    p_list.add_argument("--limit", type=int, default=50, help="máximo de filas")
    p_list.add_argument(
        "--email-failed",
        action="store_true",
        help=(
            "solo las solicitudes con algún mail en 'failed' tras agotar los "
            "reintentos (alguien quedó esperando sin recibir nada)"
        ),
    )
    p_list.set_defaults(handler=cmd_list)

    # --- show ---
    p_show = sub.add_parser("show", help="ficha completa de una solicitud")
    p_show.add_argument("referencia", help="request_id (uuid) o email")
    p_show.set_defaults(handler=cmd_show)

    # --- approve ---
    p_approve = sub.add_parser("approve", help="aprobar y acuñar la cuenta")
    p_approve.add_argument("request_id")
    p_approve.add_argument(
        "--vertical",
        required=True,
        # `Vertical`, NO `RequestedVertical`: 'otros' es INDECIBLE acá a
        # propósito. Es una opción del formulario público, nunca un vertical
        # operativo — no tiene heurísticas, categorías ni benchmarks.
        choices=[v.value for v in Vertical],
        help="rubro que asigna el DUEÑO (puede corregir el declarado)",
    )
    p_approve.add_argument("--notes", default=None, help="nota interna de revisión")
    agregar_apply(p_approve)
    p_approve.set_defaults(handler=cmd_approve)

    # --- reject ---
    p_reject = sub.add_parser("reject", help="rechazar una solicitud")
    p_reject.add_argument("request_id")
    p_reject.add_argument(
        "--reason",
        required=True,
        help="motivo INTERNO (mínimo 3 caracteres); el mail nunca lo transcribe",
    )
    p_reject.add_argument(
        "--no-email",
        action="store_true",
        help="no avisarle por mail (para descartar spam sin escribirle)",
    )
    agregar_apply(p_reject)
    p_reject.set_defaults(handler=cmd_reject)

    # --- waitlist ---
    p_wait = sub.add_parser("waitlist", help="postergar (no es terminal)")
    p_wait.add_argument("request_id")
    p_wait.add_argument("--notes", default=None, help="nota interna de revisión")
    agregar_apply(p_wait)
    p_wait.set_defaults(handler=cmd_waitlist)

    # --- otros ---
    p_otros = sub.add_parser(
        "otros", help="agrupar por descripción las solicitudes de rubro 'otros'"
    )
    p_otros.set_defaults(handler=cmd_otros)

    # --- resend-invite ---
    p_resend = sub.add_parser(
        "resend-invite", help="reenviar el mail que el solicitante está esperando"
    )
    p_resend.add_argument("request_id")
    agregar_apply(p_resend)
    p_resend.set_defaults(handler=cmd_resend_invite)

    # --- expire-stale ---
    p_expire = sub.add_parser(
        "expire-stale",
        help="higiene de la cola: expirar 'unverified' viejas (no destraba a nadie)",
    )
    p_expire.add_argument(
        "--days",
        type=int,
        default=DIAS_RANCIAS_DEFAULT,
        help=f"antigüedad mínima en días (default {DIAS_RANCIAS_DEFAULT})",
    )
    agregar_apply(p_expire)
    p_expire.set_defaults(handler=cmd_expire_stale)

    return parser.parse_args(argv)


async def main() -> None:
    args = parse_args()
    if getattr(args, "days", 1) < 1:
        print("ERROR: --days tiene que ser al menos 1.")
        sys.exit(2)
    if getattr(args, "limit", 1) < 1:
        print("ERROR: --limit tiene que ser al menos 1.")
        sys.exit(2)

    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    codigo = 0
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            codigo = await args.handler(session, args)
            if not getattr(args, "apply", False):
                # Red de seguridad del dry-run: aunque ningún camino sin --apply
                # escribe, cualquier cosa pendiente en la sesión muere acá.
                await session.rollback()
    finally:
        await engine.dispose()
    if codigo:
        sys.exit(codigo)


if __name__ == "__main__":
    asyncio.run(main())
