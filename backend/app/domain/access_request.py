"""Dominio de las solicitudes de acceso (registro cerrado con aprobación manual).

El visitante ya no crea una cuenta: manda una **solicitud** que el dueño revisa a
mano y aprueba, rechaza o deja en lista de espera. Este módulo define el
vocabulario cerrado de ese flujo — estados, plan solicitado y las bandas del
screening del negocio — más el TTL del token de verificación de email.

Dos decisiones de producto quedan clavadas acá y en los CHECK de la tabla:

* **`RequestedPlan` es una INTENCIÓN, no una suscripción.** Al aprobar se crea
  siempre una suscripción ``FREE``: no existe plan pago operativo, ni cobro, ni
  límites configurados. Por eso la columna se llama ``requested_plan`` y nunca
  ``plan_code``.
* **`otros` nunca es un vertical operativo.** El solicitante puede declarar
  ``RequestedVertical.OTROS`` con un texto libre, pero el vertical asignado al
  aprobar sale de `app.domain.verticals.Vertical` (los 3 reales).

**No se duplica nada de `app.domain.contact_lead`**: el estado de los envíos de
email (`EmailNotificationStatus`), los umbrales anti-bot
(`MIN_SUBMIT_ELAPSED_MS`, `DEDUP_WINDOW_SECONDS`) y `normalize_email` se importan
de ahí — es el otro formulario público anónimo del sistema y la semántica es la
misma.

Sin dependencias de infraestructura (ni SQLAlchemy, ni settings, ni repos).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class AccessRequestStatus(StrEnum):
    """Ciclo de vida de una solicitud de acceso.

    ``UNVERIFIED`` → el visitante todavía no confirmó su email (doble opt-in).
    ``PENDING``    → verificada, esperando revisión del dueño.
    ``WAITLIST``   → revisada y postergada; NO es terminal (se puede aprobar o
                     rechazar después).
    ``APPROVED`` / ``REJECTED`` / ``EXPIRED`` → terminales.
    """

    UNVERIFIED = "unverified"
    PENDING = "pending"
    WAITLIST = "waitlist"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class RequestedPlan(StrEnum):
    """Con qué cuenta le gustaría empezar al solicitante.

    Es intención declarada, no una suscripción: sirve para priorizar la cola de
    revisión y para medir demanda. La suscripción creada al aprobar es siempre
    ``FREE``.
    """

    FREE = "free"
    PREMIUM = "premium"


class YearsOperating(StrEnum):
    """¿Hace cuánto opera el negocio?"""

    LT_6M = "lt_6m"
    M6_2Y = "6m_2y"
    Y2_5Y = "2y_5y"
    GT_5Y = "gt_5y"


class StaffSize(StrEnum):
    """¿Cuánta gente trabaja en el negocio?"""

    SOLO = "solo"
    S2_5 = "2_5"
    S6_15 = "6_15"
    GT_15 = "gt_15"


class RevenueBand(StrEnum):
    """Banda de facturación mensual aproximada.

    ``NO_CONTESTA`` es una respuesta legítima, no un dato faltante: la pregunta
    es opcional y responderla es voluntario.
    """

    LT_3M = "lt_3m"
    M3_10M = "3m_10m"
    M10_30M = "10m_30m"
    GT_30M = "gt_30m"
    NO_CONTESTA = "no_contesta"


class RecordsFormat(StrEnum):
    """¿Cómo guarda hoy el registro de ventas y gastos?"""

    PAPEL = "papel"
    PLANILLA = "planilla"
    SISTEMA = "sistema"
    MIXTO = "mixto"
    NINGUNO = "ninguno"


class HistoryDepth(StrEnum):
    """¿Desde cuándo tiene esos registros?"""

    LT_6M = "lt_6m"
    M6_1Y = "6m_1y"
    Y1_3Y = "1y_3y"
    GT_3Y = "gt_3y"
    NINGUNO = "ninguno"


class CanShareFiles(StrEnum):
    """¿Podría subir esos archivos para arrancar?"""

    SI_ORDENADOS = "si_ordenados"
    SI_DESPROLIJOS = "si_desprolijos"
    NO = "no"


#: Estados en los que una solicitud sigue "abierta": ocupa el lugar del email en
#: la cola y bloquea una segunda solicitud del mismo correo. Es el predicado del
#: índice único parcial ``uq_access_requests_open_email``.
OPEN_ACCESS_REQUEST_STATUSES: Final[frozenset[str]] = frozenset(
    {
        AccessRequestStatus.UNVERIFIED.value,
        AccessRequestStatus.PENDING.value,
        AccessRequestStatus.WAITLIST.value,
    }
)

#: Vigencia (horas) del token de verificación de email de una solicitud. Más
#: largo que el de reset de contraseña: acá el visitante puede tardar en abrir el
#: correo y no hay nada sensible detrás del link (solo confirma que el mail es
#: suyo).
ACCESS_REQUEST_TOKEN_TTL_HOURS: Final[int] = 48

#: Versión del texto de consentimiento que acepta el solicitante al enviar el
#: formulario. Se persiste con la solicitud; subirla cuando cambie el copy legal.
#:
#: Tiene que coincidir con `frontend/src/lib/privacyNotices.ts::CONSENT_NOTICE_VERSION`.
#: Si el cliente manda otra, el servicio lo loguea como `consent_version_mismatch`
#: y persiste SIEMPRE esta — el valor del cliente es declarativo, no autoritativo.
#:
#: `v2`: reescritura del cuerpo del aviso y separación de la nota de facturación,
#: que antes se mostraba también en el onboarding, donde es falsa.
CONSENT_VERSION: Final[str] = "v2"
