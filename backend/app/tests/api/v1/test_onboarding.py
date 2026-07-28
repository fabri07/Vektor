"""
Tests for /api/v1/onboarding endpoints.

Required tests:
  - test_onboarding_submit_kiosco
  - test_onboarding_calculates_completeness_correctly
  - test_onboarding_cannot_submit_twice
  - test_onboarding_status_before_and_after
"""

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.domain.verticals import Vertical
from app.persistence.models.business import BusinessProfile, BusinessSnapshot


@pytest.fixture(autouse=True)
def _registro_abierto(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prende `ENABLE_OPEN_REGISTRATION` para todo este módulo.

    Lo necesita `_register_and_token`, que consigue el JWT entrando por
    `POST /auth/register` — el único endpoint que el apagado alcanza.
    **`POST /onboarding/submit` NO está gateado** (está detrás de JWT, lo usa un
    usuario ya aprobado y no crea cuentas); eso lo fija
    `test_onboarding_submit_no_esta_gateado_por_el_registro` en
    `app/tests/api/v1/test_access_requests.py`.
    """
    monkeypatch.setattr(get_settings(), "ENABLE_OPEN_REGISTRATION", True)

# ── Helpers ────────────────────────────────────────────────────────────────────

_REGISTER_PAYLOAD = {
    "email": "owner@kiosco.example.com",
    "password": "Secure123",
    "full_name": "Juan Pérez",
    "business_name": "Kiosco El Rápido",
    "vertical_code": Vertical.KIOSCO_ALMACEN.value,
}

_ONBOARDING_PAYLOAD = {
    "weekly_sales_estimate_ars": 50000,
    "monthly_inventory_cost_ars": 80000,
    "monthly_fixed_expenses_ars": 30000,
    "cash_on_hand_ars": 20000,
    "product_count_estimate": 10,
    "supplier_count_estimate": 3,
    "main_concern": "MARGIN",
}


async def _register_and_token(client: AsyncClient) -> str:
    await client.post("/api/v1/auth/register", json=_REGISTER_PAYLOAD)
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": _REGISTER_PAYLOAD["email"], "password": _REGISTER_PAYLOAD["password"]},
    )
    return resp.json()["access_token"]  # test double / fixture


# ── Tests ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestOnboarding:
    async def test_onboarding_submit_kiosco(self, client: AsyncClient) -> None:
        """POST /onboarding/submit with valid kiosco data returns snapshot_id and message."""
        token = await _register_and_token(client)
        headers = {"Authorization": f"Bearer {token}"}

        response = await client.post(
            "/api/v1/onboarding/submit",
            json=_ONBOARDING_PAYLOAD,
            headers=headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert "snapshot_id" in data
        assert "data_completeness_score" in data
        assert "confidence_level" in data
        assert data["message"] == "Procesando tu score..."

    async def test_onboarding_calculates_completeness_correctly(self, client: AsyncClient) -> None:
        """Full payload (all fields > 0, products >= 5, suppliers >= 1) scores 100 HIGH."""
        token = await _register_and_token(client)
        headers = {"Authorization": f"Bearer {token}"}

        # ventas(25) + mercaderia(20) + fijos(15) + caja(20) + productos>=5(10) +
        # proveedores>=1(10) = 100
        response = await client.post(
            "/api/v1/onboarding/submit",
            json=_ONBOARDING_PAYLOAD,
            headers=headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["data_completeness_score"] == 100
        assert data["confidence_level"] == "HIGH"

    async def test_onboarding_cannot_submit_twice(self, client: AsyncClient) -> None:
        """Second submit with same tenant must return 409."""
        token = await _register_and_token(client)
        headers = {"Authorization": f"Bearer {token}"}

        await client.post(
            "/api/v1/onboarding/submit",
            json=_ONBOARDING_PAYLOAD,
            headers=headers,
        )
        response = await client.post(
            "/api/v1/onboarding/submit",
            json=_ONBOARDING_PAYLOAD,
            headers=headers,
        )

        assert response.status_code == 409

    async def test_onboarding_status_before_and_after(self, client: AsyncClient) -> None:
        """GET /onboarding/status reflects completed=False before and True after submit."""
        token = await _register_and_token(client)
        headers = {"Authorization": f"Bearer {token}"}

        # Before onboarding
        resp_before = await client.get("/api/v1/onboarding/status", headers=headers)
        assert resp_before.status_code == 200
        assert resp_before.json()["completed"] is False

        # Submit onboarding
        await client.post(
            "/api/v1/onboarding/submit",
            json=_ONBOARDING_PAYLOAD,
            headers=headers,
        )

        # After onboarding
        resp_after = await client.get("/api/v1/onboarding/status", headers=headers)
        assert resp_after.status_code == 200
        after_data = resp_after.json()
        assert after_data["completed"] is True
        assert after_data["vertical_code"] == Vertical.KIOSCO_ALMACEN.value
        assert after_data["data_completeness_score"] == 100

    async def test_onboarding_vertical_code_en_el_body_es_422(
        self, client: AsyncClient
    ) -> None:
        """Mandar `vertical_code` en /onboarding/submit es 422 (extra="forbid").

        El vertical ya lo fijó el dueño al aprobar la solicitud de acceso; el
        usuario no puede reescribirlo. Un bundle viejo del frontend que
        todavía mande `vertical_code` tiene que fallar ruidoso, no ser
        ignorado en silencio.
        """
        token = await _register_and_token(client)
        headers = {"Authorization": f"Bearer {token}"}

        response = await client.post(
            "/api/v1/onboarding/submit",
            json={**_ONBOARDING_PAYLOAD, "vertical_code": Vertical.LIMPIEZA.value},
            headers=headers,
        )

        assert response.status_code == 422

    async def test_onboarding_no_reescribe_el_vertical_del_perfil(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        """Aunque no se pueda mandar `vertical_code`, confirmamos que el
        vertical persistido después del submit sigue siendo el que ya tenía
        el `BusinessProfile` (el que fijó la aprobación), no uno inventado."""
        token = await _register_and_token(client)
        headers = {"Authorization": f"Bearer {token}"}

        await client.post(
            "/api/v1/onboarding/submit",
            json=_ONBOARDING_PAYLOAD,
            headers=headers,
        )

        bp = (
            await db_session.execute(
                select(BusinessProfile).where(
                    BusinessProfile.vertical_code == Vertical.KIOSCO_ALMACEN.value
                )
            )
        ).scalar_one()
        assert bp.vertical_code == Vertical.KIOSCO_ALMACEN.value

    async def test_onboarding_main_concern_sale_de_custom_fields_si_no_viene(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        """Si el body no manda `main_concern`, se lee de
        `business_profiles.custom_fields["main_concern"]` (lo escribió la
        aprobación de la solicitud de acceso). Simulamos esa escritura previa
        a mano porque el registro abierto de este test no pasa por ese flujo."""
        token = await _register_and_token(client)
        headers = {"Authorization": f"Bearer {token}"}

        bp = (
            await db_session.execute(
                select(BusinessProfile).where(
                    BusinessProfile.vertical_code == Vertical.KIOSCO_ALMACEN.value
                )
            )
        ).scalar_one()
        bp.custom_fields = {**bp.custom_fields, "main_concern": "STOCK"}
        await db_session.commit()

        payload_sin_main_concern = {
            k: v for k, v in _ONBOARDING_PAYLOAD.items() if k != "main_concern"
        }
        response = await client.post(
            "/api/v1/onboarding/submit",
            json=payload_sin_main_concern,
            headers=headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["data_completeness_score"] == 100

    async def test_onboarding_sin_main_concern_en_ningun_lado_no_lo_inventa(
        self, client: AsyncClient
    ) -> None:
        """Sin `main_concern` en el body ni en `custom_fields`, el submit igual
        tiene que completarse (200) — no se inventa un valor por default."""
        token = await _register_and_token(client)
        headers = {"Authorization": f"Bearer {token}"}

        payload_sin_main_concern = {
            k: v for k, v in _ONBOARDING_PAYLOAD.items() if k != "main_concern"
        }
        response = await client.post(
            "/api/v1/onboarding/submit",
            json=payload_sin_main_concern,
            headers=headers,
        )

        assert response.status_code == 200


@pytest.mark.asyncio
class TestOnboardingMontosAusentes:
    """Dejar un monto en blanco NO es lo mismo que contestar cero.

    El formulario mandaba `parseFloat(campo) || 0` porque el schema exigía los
    tres montos: un campo sin contestar entraba como un cero afirmado, se
    persistía como estimación del dueño y el score lo usaba para calcular. Peor:
    el completeness sumaba 20 puntos por caja de forma incondicional, así que un
    `cash_on_hand_ars` que nunca se tipeó contaba como dato presente.

    Los dos tests de esta clase son un par: uno prueba la ausencia, el otro el
    cero explícito. Solos no distinguen el fix del bug.
    """

    async def test_montos_ausentes_no_se_guardan_como_cero(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        token = await _register_and_token(client)
        headers = {"Authorization": f"Bearer {token}"}

        payload_sin_montos = {
            k: v
            for k, v in _ONBOARDING_PAYLOAD.items()
            if k
            not in (
                "monthly_inventory_cost_ars",
                "monthly_fixed_expenses_ars",
                "cash_on_hand_ars",
            )
        }
        response = await client.post(
            "/api/v1/onboarding/submit",
            json=payload_sin_montos,
            headers=headers,
        )

        assert response.status_code == 200
        data = response.json()
        # 100 − 20 (mercadería) − 15 (fijos) − 20 (caja) = 45 → LOW.
        assert data["data_completeness_score"] == 45
        assert data["confidence_level"] == "LOW"

        bp = (
            await db_session.execute(
                select(BusinessProfile).where(
                    BusinessProfile.vertical_code == Vertical.KIOSCO_ALMACEN.value
                )
            )
        ).scalar_one()
        # NULL en la base, no `Decimal("0")`: el negocio no dijo que no gasta.
        assert bp.monthly_inventory_spend_estimate_ars is None
        assert bp.monthly_fixed_expenses_estimate_ars is None
        assert bp.cash_on_hand_estimate_ars is None

        snapshot = (
            await db_session.execute(
                select(BusinessSnapshot).where(BusinessSnapshot.tenant_id == bp.tenant_id)
            )
        ).scalar_one()
        # Y `None` en el snapshot, no el string "None" — es la materia prima
        # del score y de cualquier auditoría posterior.
        crudos = snapshot.raw_inputs_json
        assert crudos is not None
        assert crudos["monthly_inventory_cost_ars"] is None
        assert crudos["monthly_fixed_expenses_ars"] is None
        assert crudos["cash_on_hand_ars"] is None

    async def test_cero_explicito_si_cuenta_como_dato(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        """El espejo del test anterior: contestar 0 es contestar.

        Un negocio sin gastos fijos dio un dato tan bueno como el que paga cien
        mil. Antes puntuaba igual que no contestar (`> 0`), que es la misma
        confusión al revés.
        """
        token = await _register_and_token(client)
        headers = {"Authorization": f"Bearer {token}"}

        response = await client.post(
            "/api/v1/onboarding/submit",
            json={
                **_ONBOARDING_PAYLOAD,
                "monthly_inventory_cost_ars": 0,
                "monthly_fixed_expenses_ars": 0,
                "cash_on_hand_ars": 0,
            },
            headers=headers,
        )

        assert response.status_code == 200
        assert response.json()["data_completeness_score"] == 100

        bp = (
            await db_session.execute(
                select(BusinessProfile).where(
                    BusinessProfile.vertical_code == Vertical.KIOSCO_ALMACEN.value
                )
            )
        ).scalar_one()
        assert bp.monthly_fixed_expenses_estimate_ars == Decimal("0")
        assert bp.cash_on_hand_estimate_ars == Decimal("0")

    async def test_monto_negativo_sigue_siendo_422(self, client: AsyncClient) -> None:
        """Opcional no es "cualquier cosa": el `ge=0` sigue vigente."""
        token = await _register_and_token(client)
        headers = {"Authorization": f"Bearer {token}"}

        response = await client.post(
            "/api/v1/onboarding/submit",
            json={**_ONBOARDING_PAYLOAD, "cash_on_hand_ars": -1},
            headers=headers,
        )

        assert response.status_code == 422


class TestOnboardingWorkScheduleValidation:
    """Validación de los campos de horario laboral en el onboarding (Sprint 20)."""

    def test_no_schedule_fields_is_valid(self) -> None:
        from app.schemas.onboarding import OnboardingSubmitRequest  # noqa: PLC0415

        body = OnboardingSubmitRequest(**_ONBOARDING_PAYLOAD)
        assert body.work_days is None

    def test_all_three_valid(self) -> None:
        from app.schemas.onboarding import OnboardingSubmitRequest  # noqa: PLC0415

        body = OnboardingSubmitRequest(
            **_ONBOARDING_PAYLOAD,
            work_days=[0, 1, 2, 3, 4],
            work_open_hour=9,
            work_close_hour=18,
        )
        assert body.work_close_hour == 18

    def test_partial_schedule_rejected(self) -> None:
        from pydantic import ValidationError  # noqa: PLC0415

        from app.schemas.onboarding import OnboardingSubmitRequest  # noqa: PLC0415

        with pytest.raises(ValidationError):
            OnboardingSubmitRequest(**_ONBOARDING_PAYLOAD, work_open_hour=9)

    def test_out_of_range_day_rejected(self) -> None:
        from pydantic import ValidationError  # noqa: PLC0415

        from app.schemas.onboarding import OnboardingSubmitRequest  # noqa: PLC0415

        with pytest.raises(ValidationError):
            OnboardingSubmitRequest(
                **_ONBOARDING_PAYLOAD,
                work_days=[0, 7],
                work_open_hour=9,
                work_close_hour=18,
            )

    def test_duplicate_days_rejected(self) -> None:
        from pydantic import ValidationError  # noqa: PLC0415

        from app.schemas.onboarding import OnboardingSubmitRequest  # noqa: PLC0415

        with pytest.raises(ValidationError):
            OnboardingSubmitRequest(
                **_ONBOARDING_PAYLOAD,
                work_days=[0, 0, 1],
                work_open_hour=9,
                work_close_hour=18,
            )

    def test_close_not_after_open_rejected(self) -> None:
        from pydantic import ValidationError  # noqa: PLC0415

        from app.schemas.onboarding import OnboardingSubmitRequest  # noqa: PLC0415

        with pytest.raises(ValidationError):
            OnboardingSubmitRequest(
                **_ONBOARDING_PAYLOAD,
                work_days=[0, 1],
                work_open_hour=18,
                work_close_hour=9,
            )
