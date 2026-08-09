"""Tests for the customers CRUD endpoints + sales linkage.

Cubre:
- Crear / listar / get / patch / soft-delete de clientes.
- Idempotencia: 2º POST con la misma Idempotency-Key → 409, sin duplicar.
- Aislamiento por tenant: un tenant no ve ni accede a clientes de otro.
- Venta vinculada a cliente: POST /sales con customer_id y GET /sales?customer_id.
"""

import uuid
from datetime import date
from typing import Any

import pytest
from httpx import AsyncClient

_TODAY = str(date.today())

# Alta manual de cliente REAL: identidad + documento + celular son obligatorios en
# backend (reforma Clientes). Los payloads de test deben venir completos.
_CUSTOMER_PAYLOAD = {
    "name": "Cliente Uno",
    "customer_type": "person",
    "last_name": "Pérez",
    "dni": "30123456",
    "email": "uno@example.com",
    "phone": "+54 11 1234-5678",
    "telegram_username": "@cliente_uno",
    "notes": "VIP",
}

# Contador para DNIs únicos por test (evita colisiones cuando se crean varios).
_DNI_SEQ = iter(range(10_000_000, 99_000_000))


def _person(name: str, **extra: Any) -> dict[str, Any]:
    """Payload mínimo COMPLETO de persona (identidad + doc + celular)."""
    base: dict[str, Any] = {
        "name": name,
        "customer_type": "person",
        "last_name": "Test",
        "dni": str(next(_DNI_SEQ)),
        "phone": "+54 11 5555-0000",
    }
    base.update(extra)
    return base



@pytest.mark.asyncio
class TestCustomersCRUD:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_create_customer(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        resp = await client.post(
            "/api/v1/customers", json=_CUSTOMER_PAYLOAD, headers=auth_headers
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "Cliente Uno"
        assert body["email"] == "uno@example.com"
        assert body["telegram_username"] == "@cliente_uno"
        assert body["is_active"] is True
        assert body["custom_fields"] == {}
        assert "id" in body and "tenant_id" in body

    async def test_create_requires_name(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        resp = await client.post("/api/v1/customers", json={"name": ""}, headers=auth_headers)
        assert resp.status_code == 422

    async def test_list_customers(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        await client.post(
            "/api/v1/customers", json=_person("A"), headers=auth_headers
        )
        await client.post(
            "/api/v1/customers", json=_person("B"), headers=auth_headers
        )
        resp = await client.get("/api/v1/customers", headers=auth_headers)
        assert resp.status_code == 200
        names = {c["name"] for c in resp.json()}
        assert {"A", "B"} <= names

    async def test_get_customer(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        created = await client.post(
            "/api/v1/customers", json=_CUSTOMER_PAYLOAD, headers=auth_headers
        )
        cid = created.json()["id"]
        resp = await client.get(f"/api/v1/customers/{cid}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["id"] == cid

    async def test_get_customer_404(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        resp = await client.get(f"/api/v1/customers/{uuid.uuid4()}", headers=auth_headers)
        assert resp.status_code == 404

    async def test_patch_customer(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        created = await client.post(
            "/api/v1/customers", json=_CUSTOMER_PAYLOAD, headers=auth_headers
        )
        cid = created.json()["id"]
        resp = await client.patch(
            f"/api/v1/customers/{cid}",
            json={"name": "Cliente Renombrado", "phone": "+54 11 9999-0000"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "Cliente Renombrado"
        assert body["phone"] == "+54 11 9999-0000"
        # Campo no enviado queda intacto.
        assert body["email"] == "uno@example.com"

    async def test_soft_delete_customer(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        created = await client.post(
            "/api/v1/customers", json=_CUSTOMER_PAYLOAD, headers=auth_headers
        )
        cid = created.json()["id"]
        resp = await client.delete(f"/api/v1/customers/{cid}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["message"] == "Customer deactivated."
        # El detalle del inactivo sigue siendo abrible (historial read-only + reactivar).
        detail = await client.get(f"/api/v1/customers/{cid}", headers=auth_headers)
        assert detail.status_code == 200
        assert detail.json()["is_active"] is False
        # La lista por defecto lo excluye; con include_inactive aparece.
        listed = await client.get("/api/v1/customers", headers=auth_headers)
        assert cid not in {c["id"] for c in listed.json()}
        listed_all = await client.get(
            "/api/v1/customers?include_inactive=true", headers=auth_headers
        )
        assert cid in {c["id"] for c in listed_all.json()}


# La idempotencia HTTP (Idempotency-Key) vive parametrizada por endpoint en
# test_idempotency.py — customers incluido. Acá solo lo específico de la entidad.


@pytest.mark.asyncio
class TestCustomersTenantIsolation:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_other_tenant_cannot_see_or_access(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        second_auth_headers: dict[str, Any],
    ) -> None:
        created = await client.post(
            "/api/v1/customers", json=_CUSTOMER_PAYLOAD, headers=auth_headers
        )
        cid = created.json()["id"]

        # El segundo tenant no lo ve en su listado…
        other_list = await client.get("/api/v1/customers", headers=second_auth_headers)
        assert cid not in {c["id"] for c in other_list.json()}

        # …ni lo puede leer por id (404, no 403, para no filtrar existencia).
        other_get = await client.get(
            f"/api/v1/customers/{cid}", headers=second_auth_headers
        )
        assert other_get.status_code == 404


@pytest.mark.asyncio
class TestSaleCustomerLink:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_create_sale_with_customer_and_filter(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        # Cliente
        cust = await client.post(
            "/api/v1/customers", json=_person("Comprador"), headers=auth_headers
        )
        cid = cust.json()["id"]

        # Venta vinculada al cliente
        sale = await client.post(
            "/api/v1/sales",
            json={
                "amount": "2500.00",
                "quantity": 1,
                "transaction_date": _TODAY,
                "payment_method": "cash",
                "customer_id": cid,
            },
            headers=auth_headers,
        )
        assert sale.status_code == 201
        assert sale.json()["customer_id"] == cid

        # Venta sin cliente → se rutea al centinela "Local" (no a este cliente),
        # así que NO aparece al filtrar por cid.
        other_sale = await client.post(
            "/api/v1/sales",
            json={
                "amount": "999.00",
                "quantity": 1,
                "transaction_date": _TODAY,
                "payment_method": "cash",
            },
            headers=auth_headers,
        )
        assert other_sale.status_code == 201
        # La venta huérfana quedó con un customer_id (el de "Local"), distinto de cid.
        local_id = other_sale.json()["customer_id"]
        assert local_id is not None
        assert local_id != cid

        # GET /sales?customer_id= devuelve solo la venta del cliente real.
        listed = await client.get(
            f"/api/v1/sales?customer_id={cid}", headers=auth_headers
        )
        assert listed.status_code == 200
        rows = listed.json()
        assert len(rows) == 1
        assert rows[0]["customer_id"] == cid


# CUIT válido (dígito verificador OK por módulo 11): 20-12345678-6.
_VALID_CUIT = "20-12345678-6"


@pytest.mark.asyncio
class TestCustomerFiscalValidation:
    """Validación fiscal del alta manual: identidad + documento + celular."""

    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_missing_required_returns_422_incomplete(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        # Solo nombre: faltan apellido, documento y celular.
        resp = await client.post(
            "/api/v1/customers", json={"name": "Solo Nombre"}, headers=auth_headers
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["code"] == "CUSTOMER_INCOMPLETE"
        assert set(detail["missing"]) == {"last_name", "dni_or_cuit", "phone"}

    async def test_invalid_dni_returns_422(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        payload = _person("DNI Malo", dni="123")  # menos de 7 dígitos
        resp = await client.post("/api/v1/customers", json=payload, headers=auth_headers)
        assert resp.status_code == 422

    async def test_invalid_cuit_check_digit_returns_422(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        # Formato OK pero dígito verificador incorrecto.
        payload = {
            "name": "CUIT Malo",
            "customer_type": "person",
            "last_name": "Test",
            "cuit": "20-12345678-0",
            "phone": "+54 11 5555-0000",
        }
        resp = await client.post("/api/v1/customers", json=payload, headers=auth_headers)
        assert resp.status_code == 422

    async def test_valid_cuit_person_created(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        payload = {
            "name": "Con CUIT",
            "customer_type": "person",
            "last_name": "Válido",
            "cuit": _VALID_CUIT,
            "iva_condition": "responsable_inscripto",
            "phone": "+54 11 5555-1111",
        }
        resp = await client.post("/api/v1/customers", json=payload, headers=auth_headers)
        assert resp.status_code == 201
        body = resp.json()
        assert body["cuit"] == _VALID_CUIT
        assert body["iva_condition"] == "responsable_inscripto"
        assert body["customer_type"] == "person"

    async def test_company_without_last_name_created(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        # Empresa: razón social en ``name``, sin apellido, con CUIT.
        payload = {
            "name": "Distribuidora SA",
            "customer_type": "company",
            "cuit": _VALID_CUIT,
            "phone": "+54 11 4444-0000",
        }
        resp = await client.post("/api/v1/customers", json=payload, headers=auth_headers)
        assert resp.status_code == 201
        body = resp.json()
        assert body["customer_type"] == "company"
        assert body["last_name"] is None

    async def test_invalid_iva_condition_returns_422(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        payload = _person("IVA Malo", iva_condition="no_existe")
        resp = await client.post("/api/v1/customers", json=payload, headers=auth_headers)
        assert resp.status_code == 422


@pytest.mark.asyncio
class TestLocalSentinel:
    """Centinela "Local": get-or-create único, excluido de métricas, protegido."""

    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def _orphan_sale_local_id(
        self, client: AsyncClient, auth_headers: dict[str, Any], amount: str = "100.00"
    ) -> str:
        sale = await client.post(
            "/api/v1/sales",
            json={
                "amount": amount,
                "quantity": 1,
                "transaction_date": _TODAY,
                "payment_method": "cash",
            },
            headers=auth_headers,
        )
        assert sale.status_code == 201
        local_id = sale.json()["customer_id"]
        assert local_id is not None
        return local_id

    async def test_orphan_sale_routes_to_local_and_is_sentinel(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        local_id = await self._orphan_sale_local_id(client, auth_headers)
        got = await client.get(f"/api/v1/customers/{local_id}", headers=auth_headers)
        assert got.status_code == 200
        body = got.json()
        assert body["is_sentinel"] is True
        assert body["name"] == "Local"

    async def test_single_local_per_tenant(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        first = await self._orphan_sale_local_id(client, auth_headers, "10.00")
        second = await self._orphan_sale_local_id(client, auth_headers, "20.00")
        # Dos ventas huérfanas comparten el MISMO centinela.
        assert first == second

    async def test_local_excluded_from_list(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        local_id = await self._orphan_sale_local_id(client, auth_headers)
        listed = await client.get("/api/v1/customers", headers=auth_headers)
        assert listed.status_code == 200
        assert local_id not in {c["id"] for c in listed.json()}

    async def test_local_included_with_include_sentinel(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        local_id = await self._orphan_sale_local_id(client, auth_headers)
        listed = await client.get(
            "/api/v1/customers?include_sentinel=true", headers=auth_headers
        )
        assert listed.status_code == 200
        assert local_id in {c["id"] for c in listed.json()}

    async def test_include_sentinel_does_not_create_when_absent(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        # Tenant con un cliente real pero SIN ventas huérfanas → no hay centinela.
        # include_sentinel=true solo LISTA si existe; nunca lo crea (R1).
        await client.post(
            "/api/v1/customers", json=_person("Real"), headers=auth_headers
        )
        listed = await client.get(
            "/api/v1/customers?include_sentinel=true", headers=auth_headers
        )
        assert listed.status_code == 200
        body = listed.json()
        assert len(body) == 1
        assert all(c["is_sentinel"] is False for c in body)

    async def test_include_sentinel_returns_both_real_and_local(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        real = await client.post(
            "/api/v1/customers", json=_person("Real"), headers=auth_headers
        )
        real_id = real.json()["id"]
        local_id = await self._orphan_sale_local_id(client, auth_headers)

        with_sentinel = await client.get(
            "/api/v1/customers?include_sentinel=true", headers=auth_headers
        )
        ids = {c["id"] for c in with_sentinel.json()}
        assert real_id in ids and local_id in ids

        without = await client.get("/api/v1/customers", headers=auth_headers)
        ids_default = {c["id"] for c in without.json()}
        assert real_id in ids_default and local_id not in ids_default

    async def test_real_customer_named_local_is_normal(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        # Un cliente REAL llamado "Local" (sin el flag) es un cliente común:
        # aparece siempre, no es centinela y es editable (R2 — identidad por flag).
        created = await client.post(
            "/api/v1/customers", json=_person("Local"), headers=auth_headers
        )
        assert created.status_code == 201
        cid = created.json()["id"]
        assert created.json()["is_sentinel"] is False

        listed = await client.get("/api/v1/customers", headers=auth_headers)
        assert cid in {c["id"] for c in listed.json()}

        patched = await client.patch(
            f"/api/v1/customers/{cid}",
            json={"name": "Local Centro"},
            headers=auth_headers,
        )
        assert patched.status_code == 200

    async def test_local_not_editable(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        local_id = await self._orphan_sale_local_id(client, auth_headers)
        resp = await client.patch(
            f"/api/v1/customers/{local_id}",
            json={"name": "Renombrado"},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    async def test_local_not_deletable(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        local_id = await self._orphan_sale_local_id(client, auth_headers)
        resp = await client.delete(
            f"/api/v1/customers/{local_id}", headers=auth_headers
        )
        assert resp.status_code == 400

    async def test_cannot_create_sentinel_manually(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        payload = _person("Falso Local", custom_fields={"_sentinel": "true"})
        resp = await client.post("/api/v1/customers", json=payload, headers=auth_headers)
        assert resp.status_code == 400


@pytest.mark.asyncio
class TestFiadoRequiresRealCustomer:
    """Fiado (``payment_method='account'``) exige cliente real, nunca "Local"."""

    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_fiado_without_customer_rejected(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        resp = await client.post(
            "/api/v1/sales",
            json={
                "amount": "500.00",
                "quantity": 1,
                "transaction_date": _TODAY,
                "payment_method": "account",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400

    async def test_fiado_with_real_customer_ok(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        cust = await client.post(
            "/api/v1/customers", json=_person("Fiado OK"), headers=auth_headers
        )
        cid = cust.json()["id"]
        resp = await client.post(
            "/api/v1/sales",
            json={
                "amount": "500.00",
                "quantity": 1,
                "transaction_date": _TODAY,
                "payment_method": "account",
                "customer_id": cid,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201
        assert resp.json()["customer_id"] == cid

    async def test_fiado_against_sentinel_rejected(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        # Forzar la creación del centinela con una venta cash huérfana.
        orphan = await client.post(
            "/api/v1/sales",
            json={
                "amount": "100.00",
                "quantity": 1,
                "transaction_date": _TODAY,
                "payment_method": "cash",
            },
            headers=auth_headers,
        )
        local_id = orphan.json()["customer_id"]
        # Intentar fiar explícitamente contra "Local" → 400.
        resp = await client.post(
            "/api/v1/sales",
            json={
                "amount": "500.00",
                "quantity": 1,
                "transaction_date": _TODAY,
                "payment_method": "account",
                "customer_id": local_id,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400


@pytest.mark.asyncio
class TestCustomerRepositorySentinelExclusion:
    """El centinela no cuenta como cliente activo ni entra en "inactivos"."""

    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_count_active_and_inactive_exclude_sentinel(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: Any,
        sample_tenant: Any,
    ) -> None:
        from app.persistence.repositories.customer_repository import CustomerRepository

        # Un cliente real + el centinela (vía venta huérfana).
        await client.post(
            "/api/v1/customers", json=_person("Real Uno"), headers=auth_headers
        )
        orphan = await client.post(
            "/api/v1/sales",
            json={
                "amount": "100.00",
                "quantity": 1,
                "transaction_date": _TODAY,
                "payment_method": "cash",
            },
            headers=auth_headers,
        )
        local_id = orphan.json()["customer_id"]

        repo = CustomerRepository(db_session)
        # Solo el cliente real cuenta como activo (el centinela queda afuera).
        assert await repo.count_active(sample_tenant.tenant_id) == 1
        # get_inactive_customers devuelve dicts; el centinela no debe aparecer.
        inactive = await repo.get_inactive_customers(sample_tenant.tenant_id)
        assert local_id not in {c["customer_id"] for c in inactive}
