"""CRUD común de los maestros (clientes y proveedores), parametrizado.

Los routers `/customers` y `/suppliers` comparten el esqueleto
create / requires-name / list / get / 404 / patch / soft-delete /
aislamiento-de-tenants, y estos 8 comportamientos eran pares clonados en
`test_customers.py` / `test_suppliers.py`. Acá corren una vez por entidad;
lo específico (ficha fiscal, centinelas, remitos, marcas colapsadas, fiado)
sigue en el archivo de cada maestro.
"""

import itertools
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest
from httpx import AsyncClient

_DNI_SEQ = itertools.count(41_000_000)


def _customer_min(name: str) -> dict[str, Any]:
    """Payload mínimo completo de persona (la obligatoriedad fiscal exige doc)."""
    return {
        "name": name,
        "customer_type": "person",
        "last_name": "Test",
        "dni": str(next(_DNI_SEQ)),
        "phone": "+54 11 5555-0000",
    }


def _supplier_min(name: str) -> dict[str, Any]:
    return {"name": name}


@dataclass(frozen=True)
class _Master:
    base: str
    payload: dict[str, Any]
    #: campos que el create debe devolver tal cual, además de name/is_active.
    echo_fields: dict[str, Any]
    deactivate_message: str
    #: factory de payload mínimo por nombre (para list).
    minimo: Any = field(hash=False, compare=False, default=None)


_CUSTOMER = _Master(
    base="/api/v1/customers",
    payload={
        "name": "Cliente Uno",
        "customer_type": "person",
        "last_name": "Pérez",
        "dni": "30123456",
        "email": "uno@example.com",
        "phone": "+54 11 1234-5678",
        "telegram_username": "@cliente_uno",
    },
    echo_fields={"email": "uno@example.com", "telegram_username": "@cliente_uno"},
    deactivate_message="Customer deactivated.",
    minimo=_customer_min,
)

_SUPPLIER = _Master(
    base="/api/v1/suppliers",
    payload={
        "name": "Proveedor Uno",
        "email": "uno@proveedor.com",
        "phone": "+54 11 1234-5678",
        "notes": "Mayorista",
    },
    echo_fields={"email": "uno@proveedor.com", "phone": "+54 11 1234-5678"},
    deactivate_message="Supplier deactivated.",
    minimo=_supplier_min,
)

_MAESTROS = [
    pytest.param(_CUSTOMER, id="customers"),
    pytest.param(_SUPPLIER, id="suppliers"),
]


@pytest.mark.asyncio
@pytest.mark.usefixtures("mock_score_trigger")
@pytest.mark.parametrize("m", _MAESTROS)
class TestMasterCRUD:
    async def test_create(
        self, client: AsyncClient, auth_headers: dict[str, Any], m: _Master
    ) -> None:
        resp = await client.post(m.base, json=m.payload, headers=auth_headers)
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == m.payload["name"]
        for campo, valor in m.echo_fields.items():
            assert body[campo] == valor
        assert body["is_active"] is True
        assert body["custom_fields"] == {}
        assert "id" in body and "tenant_id" in body

    async def test_create_requires_name(
        self, client: AsyncClient, auth_headers: dict[str, Any], m: _Master
    ) -> None:
        resp = await client.post(m.base, json={"name": ""}, headers=auth_headers)
        assert resp.status_code == 422

    async def test_list(
        self, client: AsyncClient, auth_headers: dict[str, Any], m: _Master
    ) -> None:
        await client.post(m.base, json=m.minimo("A"), headers=auth_headers)
        await client.post(m.base, json=m.minimo("B"), headers=auth_headers)
        resp = await client.get(m.base, headers=auth_headers)
        assert resp.status_code == 200
        names = {r["name"] for r in resp.json()}
        assert {"A", "B"} <= names

    async def test_get(
        self, client: AsyncClient, auth_headers: dict[str, Any], m: _Master
    ) -> None:
        created = await client.post(m.base, json=m.payload, headers=auth_headers)
        rid = created.json()["id"]
        resp = await client.get(f"{m.base}/{rid}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["id"] == rid

    async def test_get_404(
        self, client: AsyncClient, auth_headers: dict[str, Any], m: _Master
    ) -> None:
        resp = await client.get(f"{m.base}/{uuid.uuid4()}", headers=auth_headers)
        assert resp.status_code == 404

    async def test_patch(
        self, client: AsyncClient, auth_headers: dict[str, Any], m: _Master
    ) -> None:
        created = await client.post(m.base, json=m.payload, headers=auth_headers)
        rid = created.json()["id"]
        resp = await client.patch(
            f"{m.base}/{rid}",
            json={"name": "Renombrado", "phone": "+54 11 9999-0000"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "Renombrado"
        assert body["phone"] == "+54 11 9999-0000"
        # Campo no enviado queda intacto.
        assert body["email"] == m.payload["email"]

    async def test_soft_delete(
        self, client: AsyncClient, auth_headers: dict[str, Any], m: _Master
    ) -> None:
        created = await client.post(m.base, json=m.payload, headers=auth_headers)
        rid = created.json()["id"]
        resp = await client.delete(f"{m.base}/{rid}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["message"] == m.deactivate_message
        # El detalle del inactivo sigue siendo abrible (historial read-only + reactivar).
        detail = await client.get(f"{m.base}/{rid}", headers=auth_headers)
        assert detail.status_code == 200
        assert detail.json()["is_active"] is False
        # La lista por defecto lo excluye; con include_inactive aparece.
        listed = await client.get(m.base, headers=auth_headers)
        assert rid not in {r["id"] for r in listed.json()}
        listed_all = await client.get(
            f"{m.base}?include_inactive=true", headers=auth_headers
        )
        assert rid in {r["id"] for r in listed_all.json()}

    async def test_other_tenant_cannot_see_or_access(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        second_auth_headers: dict[str, Any],
        m: _Master,
    ) -> None:
        created = await client.post(m.base, json=m.payload, headers=auth_headers)
        rid = created.json()["id"]

        # El segundo tenant no lo ve en su listado…
        other_list = await client.get(m.base, headers=second_auth_headers)
        assert rid not in {r["id"] for r in other_list.json()}

        # …ni lo puede leer por id (404, no 403, para no filtrar existencia).
        other_get = await client.get(f"{m.base}/{rid}", headers=second_auth_headers)
        assert other_get.status_code == 404
