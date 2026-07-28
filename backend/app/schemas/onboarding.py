"""Pydantic schemas for onboarding endpoints."""

from __future__ import annotations

from decimal import Decimal
from typing import Final, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.fiscal_condition import FiscalCondition

#: Preocupación principal declarada por el dueño del negocio. Definida acá una
#: sola vez porque la preguntan DOS formularios distintos: el onboarding
#: post-login y el screening de la solicitud de acceso
#: (`app/schemas/access_request.py`). Escrita a mano en cada uno se desincroniza.
MAIN_CONCERN_PATTERN: Final[str] = r"^(MARGIN|STOCK|CASH)$"


class BusinessSurveyMixin(BaseModel):
    """Los números del negocio que se piden UNA vez para arrancar.

    Extraído de `OnboardingSubmitRequest` para que la regla "todo o nada" de los
    horarios laborales tenga una sola definición y no se duplique en el próximo
    formulario que también los pida.

    Ojo con el alcance: la solicitud de acceso pública NO hereda de acá — el
    formulario público no pregunta plata (ver `app/schemas/access_request.py`).
    Estos números se siguen pidiendo recién en el primer login.
    """

    weekly_sales_estimate_ars: Decimal = Field(gt=0)
    # Los tres montos son OPCIONALES y `None` significa "no contestó", que no es
    # lo mismo que contestar cero.
    #
    # Cuando eran obligatorios, el frontend no tenía forma de expresar la
    # ausencia y mandaba `parseFloat(campo) || 0`: un campo en blanco entraba
    # como un cero afirmado. "No tengo gastos fijos" y "no sé cuánto pago de
    # gastos fijos" terminaban siendo el mismo dato, y el segundo se persistía
    # como si el dueño lo hubiera dicho. Eso es exactamente lo que prohíbe la
    # regla de no-invención: si falta un dato, baja la confianza; no se
    # completa con un default neutral.
    #
    # `product_count_estimate` y `supplier_count_estimate` NO se tocan: el
    # formulario los valida como obligatorios, así que hacerlos nullable
    # ampliaría el contrato sin resolver ningún caso real.
    monthly_inventory_cost_ars: Decimal | None = Field(default=None, ge=0)
    monthly_fixed_expenses_ars: Decimal | None = Field(default=None, ge=0)
    cash_on_hand_ars: Decimal | None = Field(default=None, ge=0)
    product_count_estimate: int = Field(ge=0)
    supplier_count_estimate: int = Field(ge=0)
    # Días y horarios laborales (Sprint 20) — opcionales; si faltan, se usan defaults.
    # Regla "todo o nada": o se envían los 3 (válidos) o ninguno. Evita que entre
    # una configuración parcial/inválida al crear la cuenta (misma validación que
    # WorkScheduleRequest en settings).
    work_days: list[int] | None = Field(default=None)
    work_open_hour: int | None = Field(default=None, ge=0, le=23)
    work_close_hour: int | None = Field(default=None, ge=0, le=23)

    @model_validator(mode="after")
    def _validate_work_schedule(self) -> Self:
        provided = [
            self.work_days is not None,
            self.work_open_hour is not None,
            self.work_close_hour is not None,
        ]
        if not any(provided):
            return self  # ninguno → se usan defaults en el service
        if not all(provided):
            raise ValueError(
                "Enviá work_days, work_open_hour y work_close_hour juntos, o ninguno."
            )
        assert self.work_days is not None  # narrowing para mypy
        if not self.work_days:
            raise ValueError("work_days debe tener al menos un día.")
        if any(d < 0 or d > 6 for d in self.work_days):
            raise ValueError("work_days deben estar en 0-6 (0=lunes … 6=domingo).")
        if len(set(self.work_days)) != len(self.work_days):
            raise ValueError("work_days no puede tener días repetidos.")
        if (
            self.work_close_hour is not None
            and self.work_open_hour is not None
            and self.work_close_hour <= self.work_open_hour
        ):
            raise ValueError("work_close_hour debe ser mayor que work_open_hour.")
        return self


class OnboardingSubmitRequest(BusinessSurveyMixin):
    """Números financieros del primer login — el rubro NO se pide acá.

    El vertical ya lo fijó el dueño al aprobar la solicitud de acceso
    (`business_profiles.vertical_code`); el usuario recién aprobado no puede
    reescribirlo. `extra="forbid"` convierte un bundle viejo del frontend que
    todavía mande `vertical_code` en un 422 ruidoso en vez de un valor
    ignorado en silencio — exactamente el punto de este cambio.
    """

    model_config = ConfigDict(extra="forbid")

    # Preocupación principal: ahora se pregunta en el formulario público de
    # solicitud de acceso (`CreateAccessRequestRequest.main_concern`), no acá.
    # Opcional: si no viene, el service la busca en
    # `business_profiles.custom_fields["main_concern"]` (la copió la
    # aprobación); si tampoco está ahí, se omite del snapshot — no se inventa.
    main_concern: str | None = Field(default=None, pattern=MAIN_CONCERN_PATTERN)
    # Condición fiscal — opcional y solo informativa. Si viene, se guarda en el
    # profile; si falta, queda NULL (no configurado) y NO bloquea el onboarding.
    fiscal_condition: FiscalCondition | None = Field(default=None)


class OnboardingSubmitResponse(BaseModel):
    snapshot_id: UUID
    data_completeness_score: int
    confidence_level: str
    message: str


class OnboardingStatusResponse(BaseModel):
    completed: bool
    vertical_code: str
    data_completeness_score: int | None
