"""Un contacto de relleno o compartido no pisa la ficha de otra persona (I6-1).

Revisión profunda, Etapa 2: el import de clientes y proveedores usaba el email
y el teléfono como clave aunque fueran relleno («-», «0») o los compartieran
dos personas con documentos distintos. «Juan Pérez, DNI 25123456, email -»
matcheaba a «Ana Gómez, DNI 30111222, email -» y el confirm le escribía a Ana
el nombre y el DNI de Juan; el resto de las filas con «-» quedaban como
«repetidas en el archivo» y no entraban. La vinculación de una venta a su
cliente (F7c) usa el mismo motor y tenía el mismo defecto.
"""

from __future__ import annotations

import io
import unittest.mock
from collections.abc import Iterator
from typing import Any

import openpyxl
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.application.services.customer_import_service import (
    apply_import as importar_clientes,
)
from app.application.services.identity_resolution import (
    IdentityKey,
    contacto_sin_dato,
    record_keys,
)
from app.application.services.supplier_import_service import apply_import
from app.persistence.models.customer import Customer
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.repositories.customer_repository import CustomerRepository
from app.persistence.repositories.supplier_repository import SupplierRepository

_ANA_DNI = "30111222"
_CUIT_ANA = "20-12345678-6"
_CUIL_A = "20-12345678-6"
_CUIL_B = "27-23456789-1"


@pytest.fixture
def mock_s3_upload() -> Iterator[unittest.mock.AsyncMock]:
    with unittest.mock.patch(
        "app.integrations.s3.S3Client.upload",
        new_callable=unittest.mock.AsyncMock,
        return_value="tenants/test/customer-file",
    ) as mock:
        yield mock


def _xlsx(rows: list[list[Any]]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.append(["nombre", "dni", "cuit", "email", "celular"])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def _ana(db: AsyncSession, tenant: Tenant, **kw: Any) -> Customer:
    ana = Customer(tenant_id=tenant.tenant_id, name="Ana Gómez", dni=_ANA_DNI, **kw)
    db.add(ana)
    await db.commit()
    return ana


async def _importar(
    client: AsyncClient, headers: dict[str, Any], rows: list[list[Any]]
) -> tuple[dict[str, Any], dict[str, Any]]:
    prev = await client.post(
        "/api/v1/customers/import/preview",
        files={"file": ("clientes.xlsx", _xlsx(rows), "application/octet-stream")},
        headers=headers,
    )
    assert prev.status_code == 200, prev.text
    pbody = prev.json()
    # El frontend manda las filas a crear y a actualizar; las otras no.
    enviar = [i["customer"] for i in pbody["items"] if i["status"] in ("create", "update")]
    conf = await client.post(
        "/api/v1/customers/import/confirm", json={"rows": enviar}, headers=headers
    )
    assert conf.status_code == 200, conf.text
    return pbody, conf.json()


async def _clientes(db: AsyncSession, tenant: Tenant) -> dict[str, Customer]:
    filas = (
        await db.execute(
            select(Customer)
            .where(Customer.tenant_id == tenant.tenant_id)
            .execution_options(populate_existing=True)
        )
    ).scalars()
    return {c.name: c for c in filas}


# ── Clientes, por HTTP ───────────────────────────────────────────────────────


async def test_el_relleno_no_pisa_a_otra_persona_ni_se_pierden_filas(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    mock_s3_upload: unittest.mock.AsyncMock,
    mock_score_trigger: unittest.mock.MagicMock,
) -> None:
    ana_id = (await _ana(db_session, sample_tenant, email="-", phone="0")).id
    pbody, cbody = await _importar(
        client,
        auth_headers,
        [
            ["Juan Pérez", "25123456", None, "-", None],
            ["María López", "27123456", None, "-", None],
            ["Pepe Ruiz", "28123456", None, None, "0"],
        ],
    )
    assert [i["status"] for i in pbody["items"]] == ["create", "create", "create"]
    assert cbody["created"] == 3 and cbody["updated"] == 0

    fichas = await _clientes(db_session, sample_tenant)
    assert fichas["Ana Gómez"].id == ana_id
    assert fichas["Ana Gómez"].dni == _ANA_DNI
    assert fichas["Juan Pérez"].dni == "25123456"
    # El relleno no se guarda: no siembra la clave basura del próximo import.
    assert fichas["Juan Pérez"].email is None
    assert fichas["Pepe Ruiz"].phone is None


async def test_un_email_real_compartido_con_otro_dni_es_otra_persona(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    mock_s3_upload: unittest.mock.AsyncMock,
    mock_score_trigger: unittest.mock.MagicMock,
) -> None:
    await _ana(db_session, sample_tenant, email="familia@mail.com")
    pbody, cbody = await _importar(
        client,
        auth_headers,
        [
            ["Juan Pérez", "25123456", None, "familia@mail.com", None],
            ["Lucía Pérez", "27123456", None, "familia@mail.com", None],
        ],
    )
    assert [i["status"] for i in pbody["items"]] == ["create", "create"]
    assert cbody["created"] == 2

    fichas = await _clientes(db_session, sample_tenant)
    assert fichas["Ana Gómez"].dni == _ANA_DNI
    assert fichas["Juan Pérez"].email == "familia@mail.com"
    assert fichas["Lucía Pérez"].dni == "27123456"


async def test_mismo_dni_o_solo_email_siguen_actualizando(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    mock_s3_upload: unittest.mock.AsyncMock,
    mock_score_trigger: unittest.mock.MagicMock,
) -> None:
    ana_id = (
        await _ana(db_session, sample_tenant, email="ana@viejo.com", phone="1144440000")
    ).id
    pbody, cbody = await _importar(
        client,
        auth_headers,
        [
            ["Ana Gómez", _ANA_DNI, None, "ana@nuevo.com", None],
            ["Ana G.", None, None, None, "11-4444-0000"],
            ["Ana Gomez", _ANA_DNI, None, None, None],
        ],
    )
    # Dos filas de la misma ficha con claves distintas se aplican las dos; la
    # que repite el DNI de una anterior es repetida, como siempre.
    assert [i["status"] for i in pbody["items"]] == ["update", "update", "duplicate_in_file"]
    assert cbody["updated"] == 2 and cbody["created"] == 0
    fichas = await _clientes(db_session, sample_tenant)
    assert fichas["Ana G."].id == ana_id
    assert fichas["Ana G."].email == "ana@nuevo.com"


async def test_mismo_cuit_con_otro_dni_es_conflicto_y_no_pisa(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    mock_s3_upload: unittest.mock.AsyncMock,
    mock_score_trigger: unittest.mock.MagicMock,
) -> None:
    await _ana(db_session, sample_tenant, cuit=_CUIT_ANA)
    pbody, cbody = await _importar(
        client, auth_headers, [["Otra Persona", "25123456", _CUIT_ANA, None, None]]
    )
    assert pbody["items"][0]["status"] == "invalid"
    assert "no coincide con el de «Ana Gómez»" in pbody["items"][0]["issues"][0]
    assert cbody["created"] == 0 and cbody["updated"] == 0
    fichas = await _clientes(db_session, sample_tenant)
    assert set(fichas) == {"Ana Gómez"}
    assert fichas["Ana Gómez"].dni == _ANA_DNI


async def test_preview_y_confirm_coinciden_con_un_email_de_familia(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    mock_s3_upload: unittest.mock.AsyncMock,
    mock_score_trigger: unittest.mock.MagicMock,
) -> None:
    """La fila de Ana (sólo email) no es «repetida» de la de Juan: el preview
    dice que actualiza a Ana, y el confirm la actualiza."""
    ana_id = (await _ana(db_session, sample_tenant, email="familia@mail.com")).id
    pbody, cbody = await _importar(
        client,
        auth_headers,
        [
            ["Juan Pérez", "25123456", None, "familia@mail.com", None],
            ["Ana Gómez", None, None, "familia@mail.com", "1155556666"],
            ["Juan P.", "25123456", None, None, None],
        ],
    )
    estados = [i["status"] for i in pbody["items"]]
    assert estados == ["create", "update", "duplicate_in_file"]
    assert pbody["items"][1]["existing_id"] == str(ana_id)
    assert cbody["created"] == 1 and cbody["updated"] == 1
    fichas = await _clientes(db_session, sample_tenant)
    assert fichas["Ana Gómez"].phone == "1155556666"
    assert fichas["Juan Pérez"].phone is None


async def test_un_dni_real_con_digitos_iguales_sigue_identificando(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    mock_s3_upload: unittest.mock.AsyncMock,
    mock_score_trigger: unittest.mock.MagicMock,
) -> None:
    db_session.add(Customer(tenant_id=sample_tenant.tenant_id, name="Pedro", dni="22222222"))
    await db_session.commit()
    pbody, cbody = await _importar(
        client, auth_headers, [["Pedro", "22222222", None, "pedro@mail.com", None]]
    )
    assert pbody["items"][0]["status"] == "update"
    assert cbody["updated"] == 1
    assert (await _clientes(db_session, sample_tenant))["Pedro"].email == "pedro@mail.com"


async def test_el_codigo_le_gana_al_documento_y_un_email_real_raro_se_guarda(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Con el mismo código propio, un DNI corregido en el sistema de origen
    entra por reimport (B·7: el código es la identidad del negocio). Un email
    real mal escrito no es clave, pero se guarda."""
    ana_id = (
        await _ana(db_session, sample_tenant, external_code="C-7", cuit=_CUIT_ANA)
    ).id
    result = await importar_clientes(
        CustomerRepository(db_session),
        sample_tenant.tenant_id,
        [
            {
                "name": "Ana Gómez",
                "external_code": "C-7",
                "cuit": _CUIT_ANA,
                "dni": "30111223",
                "email": "ana@a.com; ana@b.com",
            }
        ],
    )
    await db_session.commit()
    assert result.updated_ids == [ana_id]
    ana = (await _clientes(db_session, sample_tenant))["Ana Gómez"]
    assert ana.dni == "30111223"
    assert ana.email == "ana@a.com; ana@b.com"


async def test_la_ficha_nueva_no_le_saca_el_email_compartido_a_la_anterior(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Juan (otro DNI) se crea con el email de la familia; una fila posterior
    que sólo trae ese email sigue siendo de Ana, que lo tenía primero."""
    ana_id = (await _ana(db_session, sample_tenant, email="familia@mail.com")).id
    result = await importar_clientes(
        CustomerRepository(db_session),
        sample_tenant.tenant_id,
        [
            {"name": "Juan Pérez", "dni": "25123456", "email": "familia@mail.com"},
            {"name": "Ana Gómez", "email": "familia@mail.com", "address": "Mitre 10"},
        ],
    )
    await db_session.commit()
    assert len(result.created_ids) == 1
    assert result.updated_ids == [ana_id]
    fichas = await _clientes(db_session, sample_tenant)
    assert fichas["Ana Gómez"].address == "Mitre 10"
    assert fichas["Juan Pérez"].address is None


# ── Proveedores (import de maestros del archivo, con base) ───────────────────


async def test_proveedor_con_un_0800_compartido_y_otro_cuil_no_pisa(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    a = Supplier(
        tenant_id=sample_tenant.tenant_id,
        name="Distribuidora A",
        cuil=_CUIL_A,
        phone="0800-555-1234",
    )
    db_session.add(a)
    await db_session.commit()
    result = await apply_import(
        SupplierRepository(db_session),
        sample_tenant.tenant_id,
        [
            {"name": "Distribuidora B", "cuil": _CUIL_B, "phone": "0800-555-1234"},
            {"name": "Distribuidora C", "cuil": "23-34567890-5", "email": "-", "phone": "0"},
        ],
    )
    await db_session.commit()
    assert len(result.created_ids) == 2 and result.updated_ids == []
    await db_session.refresh(a)
    assert a.name == "Distribuidora A" and a.cuil == _CUIL_A
    c = await db_session.get(Supplier, result.created_ids[1])
    assert c is not None and c.email is None and c.phone is None


# ── Venta → cliente (F7c) ────────────────────────────────────────────────────


def _ventas_con_cliente(filas: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "has_venta": True,
        "ventas_detectadas": [
            {"fecha": "2024-01-15", "valor": "1000", **f} for f in filas
        ],
    }


async def test_una_venta_con_email_compartido_y_otro_dni_no_se_le_carga_a_ana(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    ana_id = (await _ana(db_session, sample_tenant, email="familia@mail.com")).id
    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _ventas_con_cliente(
            [
                {"mail": "familia@mail.com", "doc": "25123456"},
                {"mail": "familia@mail.com", "doc": None},
            ]
        ),
        {"ventas": True},
        column_mappings={
            "valor": "amount",
            "fecha": "transaction_date",
            "mail": "customer_email",
            "doc": "customer_dni",
        },
    )
    assert counts["ventas"] == 2
    assert counts["ventas_cliente_identificado"] == 1
    assert counts["ventas_cliente_no_resuelto"] == 1
    ventas = (
        await db_session.execute(
            select(SaleEntry).where(SaleEntry.tenant_id == sample_tenant.tenant_id)
        )
    ).scalars().all()
    assert sum(1 for v in ventas if v.customer_id == ana_id) == 1


# ── Motor ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "record",
    [
        {"email": "-"},
        {"email": "sin email"},
        {"email": "no tiene"},
        {"email": "juan"},
        {"phone": "0"},
        {"phone": "000"},
        {"phone": "1111111"},
        {"phone": "4567"},
        {"dni": "0"},
        {"dni": "00000000"},
    ],
)
def test_el_relleno_no_es_clave(record: dict[str, Any]) -> None:
    assert record_keys(record, doc_fields=("cuit", "dni")) == []


def test_un_contacto_de_verdad_sigue_siendo_clave() -> None:
    keys = record_keys(
        {"dni": "25123456", "email": " Juan@Mail.com ", "phone": "11-4444-0000"},
        doc_fields=("cuit", "dni"),
    )
    assert keys == [
        IdentityKey("doc", "25123456"),
        IdentityKey("email", "juan@mail.com"),
        IdentityKey("phone", "1144440000"),
    ]


def test_un_interno_corto_se_guarda_aunque_no_identifique() -> None:
    assert not contacto_sin_dato("phone", "4567")
    assert contacto_sin_dato("phone", "000")
    assert contacto_sin_dato("email", "-")
    assert not contacto_sin_dato("email", "a@b.com")
