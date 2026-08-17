"""Tests de la carga de clientes por archivo (Fase B).

Cubre:
- Servicio determinístico (XLSX/CSV): mapea columnas → campos del cliente sin IA.
- Servicio IA (foto/PDF): mockea el cliente Anthropic, verifica la request multimodal
  (image/document block + tool_use forzado) y el parseo a ``CustomerExtraction``.
- Fail-soft: sin API key / error de red → warnings, nunca rompe.
- Import masivo: ``build_import_preview`` (crear/actualizar/inválido/duplicado) y
  ``apply_import`` (upsert idempotente, sentinela nunca creado).
- Endpoints: ``POST /customers/extract``, ``/import/preview``, ``/import/confirm`` +
  guard 413.
"""

from __future__ import annotations

import io
import unittest.mock
import uuid
from typing import Any

import openpyxl
import pytest
from httpx import AsyncClient

from app.application.services.customer_extraction_service import (
    CustomerExtraction,
    extract_customer,
    parse_customer_records,
)
from app.application.services.customer_import_service import (
    _customer_record,
    apply_import,
    build_import_preview,
)
from app.application.services.identity_resolution import IdentityKey, build_existing_index
from app.persistence.models.customer import Customer

_DOC_FIELDS = ("cuit", "dni")


def _index(existing: list[Customer]) -> dict[IdentityKey, Customer]:
    """Índice síncrono para tests de `build_import_preview` puro — sin DB, así
    que sin el tier `business_code` (que exige `session` para leer
    `entity_identifiers`; ver `build_existing_index_with_codes`)."""
    return build_existing_index(
        existing, to_record=_customer_record, doc_fields=_DOC_FIELDS, code_field="code"
    )


# CUIT con dígito verificador válido (módulo 11).
_VALID_CUIT = "20-12345678-6"
_VALID_CUIT_2 = "27-23456789-1"


def _csv_clientes() -> bytes:
    return (
        "razon social,cuit,condicion iva,email,celular,localidad,provincia\n"
        f"Distribuidora Norte SA,{_VALID_CUIT},responsable_inscripto,"
        "ventas@norte.com,+54 11 4444-0000,San Isidro,Buenos Aires\n"
    ).encode()


def _csv_un_cliente_persona() -> bytes:
    return (
        "apellido,nombre,dni,celular,direccion,localidad\n"
        "Pérez,Juan,30123456,+54 11 5555-0000,Av. Siempreviva 742,Springfield\n"
    ).encode()


def _xlsx_clientes(rows: list[list[Any]]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["razon social", "cuit", "email", "telefono"])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class _FakeBlock:
    def __init__(self, *, type_: str, **kwargs: Any) -> None:
        self.type = type_
        for k, v in kwargs.items():
            setattr(self, k, v)


class _FakeUsage:
    input_tokens = 900
    output_tokens = 50


class _FakeResponse:
    def __init__(self, tool_input: dict[str, Any]) -> None:
        self.content = [_FakeBlock(type_="tool_use", input=tool_input)]
        self.usage = _FakeUsage()


def _mock_anthropic_factory(
    tool_input: dict[str, Any],
) -> tuple[Any, unittest.mock.AsyncMock]:
    create = unittest.mock.AsyncMock(return_value=_FakeResponse(tool_input))
    client = unittest.mock.MagicMock()
    client.messages.create = create

    # MagicMock (no función plana): _is_mock_factory lo reconoce por su __module__,
    # así get_anthropic_async_client usa el factory SIN exigir ANTHROPIC_API_KEY
    # (en CI no hay key; con una función plana el servicio caía a fail-soft y nunca
    # llamaba a create).
    factory = unittest.mock.MagicMock(return_value=client)
    return factory, create


# ── Servicio determinístico (planilla) ─────────────────────────────────────────


class TestTabularExtraction:
    async def test_csv_company_first_record(self) -> None:
        extraction, usage = await extract_customer(_csv_clientes(), "clientes.csv")
        assert usage is None  # sin IA
        assert extraction.confidence == "HIGH"
        f = extraction.fields
        assert f["name"] == "Distribuidora Norte SA"
        assert f["cuit"] == _VALID_CUIT
        assert f["doc_type"] == "cuit"
        assert f["iva_condition"] == "responsable_inscripto"
        assert f["email"] == "ventas@norte.com"
        assert f["locality"] == "San Isidro"
        assert f["province"] == "Buenos Aires"

    async def test_csv_person_splits_name_and_infers_type(self) -> None:
        extraction, _ = await extract_customer(
            _csv_un_cliente_persona(), "cliente.csv"
        )
        f = extraction.fields
        assert f["name"] == "Juan"
        assert f["last_name"] == "Pérez"
        assert f["dni"] == "30123456"
        assert f["doc_type"] == "dni"
        assert f["customer_type"] == "person"

    async def test_multiple_rows_takes_first_and_warns(self) -> None:
        content = _xlsx_clientes(
            [
                ["Norte SA", _VALID_CUIT, "a@a.com", "111"],
                ["Sur SA", _VALID_CUIT_2, "b@b.com", "222"],
            ]
        )
        extraction, _ = await extract_customer(content, "clientes.xlsx")
        assert extraction.fields["name"] == "Norte SA"
        assert any("se tomó la primera" in w for w in extraction.warnings)

    async def test_unrecognized_columns_warn(self) -> None:
        extraction, _ = await extract_customer(b"foo,bar\n1,2\n", "x.csv")
        assert extraction.fields == {}
        assert extraction.warnings

    async def test_business_code_column_detected(self) -> None:
        content = (
            b"nombre,dni,codigo_cliente\nJuan Perez,30111222,CLI-EXT-9\n"
        )
        extraction, _ = await extract_customer(content, "clientes.csv")
        assert extraction.fields["business_code"] == "CLI-EXT-9"

    async def test_business_code_no_colisiona_con_postal_code(self) -> None:
        content = (
            b"nombre,codigo_postal,codigo_cliente\n"
            b"Juan Perez,1642,CLI-EXT-9\n"
        )
        extraction, _ = await extract_customer(content, "clientes.csv")
        assert extraction.fields["postal_code"] == "1642"
        assert extraction.fields["business_code"] == "CLI-EXT-9"


# ── Servicio IA (foto / PDF) ───────────────────────────────────────────────────


class TestAIExtraction:
    _TOOL_INPUT = {
        "customer_type": "person",
        "name": "María",
        "last_name": "Gómez",
        "dni": "28999111",
        "iva_condition": "consumidor_final",
        "phone": "+54 11 6666-0000",
    }

    async def test_image_builds_multimodal_request_and_parses(self) -> None:
        factory, create = _mock_anthropic_factory(self._TOOL_INPUT)
        png = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 64
        extraction, usage = await extract_customer(
            png, "dni.png", client_factory=factory
        )
        assert usage == {"input_tokens": 900, "output_tokens": 50}
        assert extraction.fields["name"] == "María"
        assert extraction.fields["last_name"] == "Gómez"
        assert extraction.fields["doc_type"] == "dni"

        kwargs = create.call_args.kwargs
        assert kwargs["model"] == "claude-sonnet-4-6"
        assert kwargs["tool_choice"]["type"] == "tool"
        blocks = kwargs["messages"][0]["content"]
        assert blocks[0]["type"] == "image"
        assert blocks[0]["source"]["type"] == "base64"

    async def test_pdf_uses_document_block(self) -> None:
        factory, create = _mock_anthropic_factory(self._TOOL_INPUT)
        pdf = b"%PDF-1.4\n" + b"0" * 64
        extraction, usage = await extract_customer(
            pdf, "ficha.pdf", client_factory=factory
        )
        assert usage is not None
        assert extraction.fields["name"] == "María"
        blocks = create.call_args.kwargs["messages"][0]["content"]
        assert blocks[0]["type"] == "document"
        assert blocks[0]["source"]["media_type"] == "application/pdf"

    async def test_ai_no_name_low_confidence(self) -> None:
        factory, _ = _mock_anthropic_factory({"phone": "123"})
        png = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 64
        extraction, _ = await extract_customer(png, "x.png", client_factory=factory)
        assert extraction.confidence == "LOW"
        assert extraction.warnings

    async def test_user_hint_wrapped(self) -> None:
        factory, create = _mock_anthropic_factory(self._TOOL_INPUT)
        png = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 64
        await extract_customer(
            png, "x.png", user_hint="es cliente mayorista", client_factory=factory
        )
        text_block = create.call_args.kwargs["messages"][0]["content"][1]["text"]
        assert "<user_message>" in text_block

    async def test_ai_failure_is_fail_soft(self) -> None:
        create = unittest.mock.AsyncMock(side_effect=RuntimeError("boom"))
        client = unittest.mock.MagicMock()
        client.messages.create = create
        factory = unittest.mock.MagicMock(return_value=client)

        png = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 64
        extraction, usage = await extract_customer(png, "x.png", client_factory=factory)
        assert usage is None
        assert extraction.fields == {}
        assert extraction.warnings  # no rompe, sugiere carga manual


# ── Import masivo: preview + apply (unitario) ──────────────────────────────────


def _row(**kw: Any) -> dict[str, Any]:
    return kw


class TestImportPreview:
    def test_classifies_create_update_needs_review_duplicate(self) -> None:
        existing = [
            Customer(
                tenant_id=uuid.uuid4(),
                name="Ya Existe SA",
                cuit=_VALID_CUIT,
                doc_type="cuit",
            )
        ]
        records = [
            _row(name="Nuevo", dni="30111222"),  # create
            _row(name="Actualizar SA", cuit=_VALID_CUIT),  # update (match por cuit)
            _row(name="Sin Doc"),  # needs_review (sin ninguna clave fuerte)
            _row(name="Repe", dni="30111222"),  # duplicate_in_file
        ]
        preview = build_import_preview(records, _index(existing))
        assert preview.to_create == 1
        assert preview.to_update == 1
        assert preview.needs_review == 1
        assert preview.invalid == 0
        assert preview.duplicates == 1

    def test_invalid_cuit_check_digit_flagged(self) -> None:
        records = [_row(name="Mal CUIT", cuit="20-12345678-0")]
        preview = build_import_preview(records, {})
        assert preview.invalid == 1
        assert preview.items[0].issues

    def test_missing_name_is_invalid_not_needs_review(self) -> None:
        # Sin nombre no hay ni siquiera señal débil — sigue siendo "invalid".
        records = [_row(dni="30111222")]
        preview = build_import_preview(records, {})
        assert preview.invalid == 1
        assert preview.needs_review == 0

    def test_email_only_matches_existing_customer(self) -> None:
        existing = [
            Customer(tenant_id=uuid.uuid4(), name="Con Email SA", email="ventas@norte.com")
        ]
        records = [_row(name="Con Email SA", email="Ventas@Norte.com")]
        preview = build_import_preview(records, _index(existing))
        assert preview.to_update == 1
        assert preview.items[0].existing_id == existing[0].id

    def test_conflict_between_doc_and_email_is_invalid(self) -> None:
        existing = [
            Customer(tenant_id=uuid.uuid4(), name="A", cuit=_VALID_CUIT),
            Customer(tenant_id=uuid.uuid4(), name="B", email="b@b.com"),
        ]
        records = [_row(name="Ambiguo", cuit=_VALID_CUIT, email="b@b.com")]
        preview = build_import_preview(records, _index(existing))
        assert preview.invalid == 1
        assert preview.needs_review == 0
        assert preview.to_create == 0
        assert preview.to_update == 0


class TestApplyImport:
    async def test_upsert_idempotent_and_no_sentinel(
        self, db_session: Any, sample_tenant: Any
    ) -> None:
        from app.persistence.repositories.customer_repository import CustomerRepository

        repo = CustomerRepository(db_session)
        tid = sample_tenant.tenant_id
        records = [
            _row(name="Norte SA", cuit=_VALID_CUIT, iva_condition="responsable_inscripto"),
            _row(name="Juan Pérez", dni="30123456"),
        ]
        first = await apply_import(
            repo, tid, records, session=db_session, uploaded_file_id=None
        )
        assert len(first.created_ids) == 2
        assert first.skipped == 0

        # Reaplicar el MISMO archivo: ahora todo matchea → actualiza, no duplica.
        second = await apply_import(
            repo, tid, records, session=db_session, uploaded_file_id=None
        )
        assert len(second.created_ids) == 0
        assert len(second.updated_ids) == 2

        # Total de clientes activos no-sentinela = 2 (no se duplicó).
        assert await repo.count_active(tid) == 2
        # Ninguno quedó marcado como sentinela.
        for c in await repo.list_for_dedup(tid):
            assert c.is_sentinel is False

    async def test_in_batch_duplicate_goes_to_others_not_merged(
        self, db_session: Any, sample_tenant: Any
    ) -> None:
        """F-I(B): antes, la 2ª fila con el mismo documento matcheaba al
        recién creado por la 1ª y lo actualizaba en silencio (merge secuencial
        sin que el usuario lo viera venir). Ahora va a "Otros" — la entidad de
        la 1ª fila queda intacta, con sus propios datos únicamente."""
        from app.persistence.repositories.customer_repository import CustomerRepository

        repo = CustomerRepository(db_session)
        tid = sample_tenant.tenant_id
        records = [
            _row(name="Dup A", cuit=_VALID_CUIT),
            _row(name="Dup B", cuit=_VALID_CUIT),  # mismo documento
        ]
        result = await apply_import(
            repo, tid, records, session=db_session, uploaded_file_id=None
        )
        assert len(result.created_ids) == 1
        assert len(result.updated_ids) == 0
        assert result.sent_to_others == 1
        assert await repo.count_active(tid) == 1
        created = await repo.get_by_id(result.created_ids[0], tid)
        assert created is not None
        assert created.name == "Dup A"  # nunca lo tocó la fila 2

    async def test_needs_review_never_created(
        self, db_session: Any, sample_tenant: Any
    ) -> None:
        from app.persistence.repositories.customer_repository import CustomerRepository

        repo = CustomerRepository(db_session)
        tid = sample_tenant.tenant_id
        records = [_row(name="Solo Nombre")]  # sin ninguna clave fuerte
        result = await apply_import(
            repo, tid, records, session=db_session, uploaded_file_id=None
        )
        assert result.created_ids == []
        assert result.updated_ids == []
        assert result.skipped == 1
        assert await repo.count_active(tid) == 0

    async def test_conflict_never_created_or_updated(
        self, db_session: Any, sample_tenant: Any
    ) -> None:
        from app.persistence.repositories.customer_repository import CustomerRepository

        repo = CustomerRepository(db_session)
        tid = sample_tenant.tenant_id
        a = Customer(tenant_id=tid, name="A", cuit=_VALID_CUIT)
        b = Customer(tenant_id=tid, name="B", email="b@b.com")
        db_session.add_all([a, b])
        await db_session.commit()

        records = [_row(name="Ambiguo", cuit=_VALID_CUIT, email="b@b.com")]
        result = await apply_import(
            repo, tid, records, session=db_session, uploaded_file_id=None
        )
        assert result.created_ids == []
        assert result.updated_ids == []
        assert result.skipped == 1
        # A y B siguen sin tocarse.
        assert (await repo.get_by_id(a.id, tid)).name == "A"  # type: ignore[union-attr]
        assert (await repo.get_by_id(b.id, tid)).name == "B"  # type: ignore[union-attr]


class TestApplyImportBusinessCode:
    """F-I(B): `business_code` en el import masivo — se persiste al crear/
    actualizar, resuelve identidad en corridas futuras, y un duplicado (por
    cualquier clave, incluido business_code) va a "Otros", nunca fusiona solo.
    """

    async def test_business_code_nuevo_se_registra_al_crear(
        self, db_session: Any, sample_tenant: Any
    ) -> None:
        from sqlalchemy import select

        from app.persistence.models.entity_identifier import EntityIdentifier
        from app.persistence.repositories.customer_repository import CustomerRepository

        repo = CustomerRepository(db_session)
        tid = sample_tenant.tenant_id
        records = [_row(name="Nuevo", dni="30111222", business_code="ERP-9")]
        result = await apply_import(
            repo, tid, records, session=db_session, uploaded_file_id=None
        )
        assert len(result.created_ids) == 1

        identifiers = (
            await db_session.execute(
                select(EntityIdentifier).where(
                    EntityIdentifier.tenant_id == tid,
                    EntityIdentifier.entity_type == "customer",
                    EntityIdentifier.identifier_type == "business_code",
                )
            )
        ).scalars().all()
        assert len(identifiers) == 1
        assert identifiers[0].entity_id == result.created_ids[0]
        assert identifiers[0].normalized_value == "erp-9"

    async def test_fila_con_solo_business_code_matchea_entidad_indexada(
        self, db_session: Any, sample_tenant: Any
    ) -> None:
        """El código quedó registrado en una importación ANTERIOR (o por
        F-I(A), vía una venta) — una fila de un import nuevo que sólo trae el
        código, sin documento, debe resolver contra esa misma entidad."""
        from app.application.services.entity_code_service import record_identifier
        from app.persistence.repositories.customer_repository import CustomerRepository

        repo = CustomerRepository(db_session)
        tid = sample_tenant.tenant_id
        existing = Customer(tenant_id=tid, name="Cliente Viejo", dni="30111222")
        db_session.add(existing)
        await db_session.flush()
        await record_identifier(
            db_session,
            tid,
            "customer",
            existing.id,
            identifier_type="business_code",
            namespace="business",
            raw_value="ERP-9",
            origin="business",
        )
        await db_session.commit()

        records = [_row(name="Cliente Viejo", business_code="ERP-9")]
        result = await apply_import(
            repo, tid, records, session=db_session, uploaded_file_id=None
        )
        assert result.created_ids == []
        assert result.updated_ids == [existing.id]

    async def test_duplicate_por_business_code_va_a_otros(
        self, db_session: Any, sample_tenant: Any
    ) -> None:
        from app.persistence.repositories.customer_repository import CustomerRepository

        repo = CustomerRepository(db_session)
        tid = sample_tenant.tenant_id
        records = [
            _row(name="Cliente A", dni="30111222", business_code="ERP-9"),
            # Documento DISTINTO, mismo business_code — conflicto real de
            # datos dentro del archivo, misma mecánica que un documento repetido.
            _row(name="Cliente B", dni="30999888", business_code="ERP-9"),
        ]
        result = await apply_import(
            repo, tid, records, session=db_session, uploaded_file_id=None
        )
        assert len(result.created_ids) == 1
        assert len(result.updated_ids) == 0
        assert result.sent_to_others == 1
        assert await repo.count_active(tid) == 1


# ── Endpoints ──────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_s3_upload():
    with unittest.mock.patch(
        "app.integrations.s3.S3Client.upload",
        new_callable=unittest.mock.AsyncMock,
        return_value="tenants/test/customer-file",
    ) as mock:
        yield mock


class TestCustomerFileEndpoints:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_extract_happy_path_csv(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        mock_s3_upload: unittest.mock.AsyncMock,
    ) -> None:
        resp = await client.post(
            "/api/v1/customers/extract",
            files={"file": ("cliente.csv", _csv_clientes(), "text/csv")},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["name"] == "Distribuidora Norte SA"
        assert body["cuit"] == _VALID_CUIT
        assert body["confidence"] == "HIGH"
        assert body["source_upload_id"] is not None
        mock_s3_upload.assert_called()

    async def test_import_preview_then_confirm_creates(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        mock_s3_upload: unittest.mock.AsyncMock,
    ) -> None:
        content = _xlsx_clientes(
            [
                ["Norte SA", _VALID_CUIT, "a@a.com", "111"],
                ["Sur SA", _VALID_CUIT_2, "b@b.com", "222"],
            ]
        )
        prev = await client.post(
            "/api/v1/customers/import/preview",
            files={"file": ("clientes.xlsx", content, "application/octet-stream")},
            headers=auth_headers,
        )
        assert prev.status_code == 200, prev.text
        pbody = prev.json()
        assert pbody["to_create"] == 2
        rows = [item["customer"] for item in pbody["items"] if item["status"] == "create"]

        conf = await client.post(
            "/api/v1/customers/import/confirm",
            json={"rows": rows},
            headers=auth_headers,
        )
        assert conf.status_code == 200, conf.text
        cbody = conf.json()
        assert cbody["created"] == 2
        assert cbody["updated"] == 0

        # Aparecen en el listado normal.
        listed = await client.get("/api/v1/customers", headers=auth_headers)
        names = {c["name"] for c in listed.json()}
        assert {"Norte SA", "Sur SA"} <= names

    async def test_import_preview_reports_needs_review_and_confirm_skips_it(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        mock_s3_upload: unittest.mock.AsyncMock,
    ) -> None:
        # Una fila sin documento/email/teléfono (solo nombre) → needs_review en el
        # preview; si igual se manda al confirm, apply_import la saltea (no crea).
        content = _xlsx_clientes([["Solo Nombre SA", "", "", ""]])
        prev = await client.post(
            "/api/v1/customers/import/preview",
            files={"file": ("clientes.xlsx", content, "application/octet-stream")},
            headers=auth_headers,
        )
        assert prev.status_code == 200, prev.text
        pbody = prev.json()
        assert pbody["needs_review"] == 1
        assert pbody["items"][0]["status"] == "needs_review"

        rows = [item["customer"] for item in pbody["items"]]
        conf = await client.post(
            "/api/v1/customers/import/confirm",
            json={"rows": rows},
            headers=auth_headers,
        )
        assert conf.status_code == 200, conf.text
        cbody = conf.json()
        assert cbody["created"] == 0
        assert cbody["skipped"] == 1

        listed = await client.get("/api/v1/customers", headers=auth_headers)
        assert "Solo Nombre SA" not in {c["name"] for c in listed.json()}

    async def test_import_preview_rejects_photo_with_warning(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
    ) -> None:
        png = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 64
        resp = await client.post(
            "/api/v1/customers/import/preview",
            files={"file": ("foto.png", png, "image/png")},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["to_create"] == 0
        assert body["warnings"]

    async def test_extract_other_tenant_isolated(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        second_auth_headers: dict[str, Any],
        mock_s3_upload: unittest.mock.AsyncMock,
    ) -> None:
        # El import de un tenant no contamina al otro.
        content = _xlsx_clientes([["Norte SA", _VALID_CUIT, "a@a.com", "111"]])
        prev = await client.post(
            "/api/v1/customers/import/preview",
            files={"file": ("c.xlsx", content, "application/octet-stream")},
            headers=auth_headers,
        )
        rows = [i["customer"] for i in prev.json()["items"]]
        await client.post(
            "/api/v1/customers/import/confirm",
            json={"rows": rows},
            headers=auth_headers,
        )
        other = await client.get("/api/v1/customers", headers=second_auth_headers)
        assert "Norte SA" not in {c["name"] for c in other.json()}


def test_customer_extraction_dataclass_defaults() -> None:
    ext = CustomerExtraction()
    assert ext.fields == {}
    assert ext.confidence == "LOW"


def test_parse_customer_records_shared_parser() -> None:
    records, warnings = parse_customer_records(
        _csv_clientes(), "clientes.csv", "text/csv"
    )
    assert len(records) == 1
    assert records[0]["name"] == "Distribuidora Norte SA"
