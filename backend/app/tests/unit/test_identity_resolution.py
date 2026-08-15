"""Tests del motor común de identidad (F7b) — puro, sin DB.

Cubre las 4 salidas de ``resolve_identity``: matched (por cada tipo de clave),
conflict (claves distintas apuntan a entidades distintas), needs_review (sin
ninguna clave fuerte) y none (clave fuerte sin match). También la prioridad de
match (documento > email > teléfono) y ``build_existing_index``.
"""

from __future__ import annotations

from app.application.services.identity_resolution import (
    IdentityKey,
    build_existing_index,
    normalize_digits,
    normalize_email,
    normalize_name,
    record_keys,
    resolve_identity,
)

_ENTITY_A = object()
_ENTITY_B = object()


class TestNormalizers:
    def test_normalize_digits_strips_non_digits(self) -> None:
        assert normalize_digits("20-12345678-6") == "20123456786"

    def test_normalize_digits_none_is_empty(self) -> None:
        assert normalize_digits(None) == ""

    def test_normalize_email_lowercases_and_trims(self) -> None:
        assert normalize_email("  Ana@Norte.COM ") == "ana@norte.com"

    def test_normalize_name_collapses_spaces(self) -> None:
        assert normalize_name("  Juan   Pérez ") == "juan pérez"


class TestRecordKeys:
    def test_priority_order_doc_then_email_then_phone(self) -> None:
        record = {"cuit": "20-12345678-6", "dni": "30111222", "email": "a@a.com", "phone": "111"}
        keys = record_keys(record, doc_fields=("cuit", "dni"))
        assert [k.type for k in keys] == ["doc", "doc", "email", "phone"]
        assert keys[0].value == "20123456786"  # cuit antes que dni
        assert keys[1].value == "30111222"

    def test_missing_fields_produce_no_keys(self) -> None:
        assert record_keys({}, doc_fields=("cuit", "dni")) == []

    def test_supplier_single_doc_field(self) -> None:
        keys = record_keys({"cuil": "20-12345678-6"}, doc_fields=("cuil",))
        assert keys == [IdentityKey("doc", "20123456786")]


class TestResolveIdentity:
    def test_matched_by_document(self) -> None:
        index = {IdentityKey("doc", "20123456786"): _ENTITY_A}
        keys = record_keys({"cuit": "20-12345678-6"}, doc_fields=("cuit", "dni"))
        result = resolve_identity(keys, index)
        assert result.outcome == "matched"
        assert result.entity is _ENTITY_A
        assert result.matched_key == IdentityKey("doc", "20123456786")

    def test_matched_by_email(self) -> None:
        index = {IdentityKey("email", "a@a.com"): _ENTITY_A}
        keys = record_keys({"email": "A@A.com"}, doc_fields=("cuit", "dni"))
        result = resolve_identity(keys, index)
        assert result.outcome == "matched"
        assert result.entity is _ENTITY_A
        assert result.matched_key == IdentityKey("email", "a@a.com")

    def test_matched_by_phone(self) -> None:
        index = {IdentityKey("phone", "1144440000"): _ENTITY_A}
        keys = record_keys({"phone": "11-4444-0000"}, doc_fields=("cuit", "dni"))
        result = resolve_identity(keys, index)
        assert result.outcome == "matched"
        assert result.entity is _ENTITY_A

    def test_document_wins_over_email_when_both_match_same_entity(self) -> None:
        index = {
            IdentityKey("doc", "30111222"): _ENTITY_A,
            IdentityKey("email", "a@a.com"): _ENTITY_A,
        }
        keys = record_keys(
            {"dni": "30111222", "email": "a@a.com"}, doc_fields=("cuit", "dni")
        )
        result = resolve_identity(keys, index)
        assert result.outcome == "matched"
        assert result.matched_key is not None
        assert result.matched_key.type == "doc"

    def test_conflict_when_doc_and_email_point_to_different_entities(self) -> None:
        index = {
            IdentityKey("doc", "30111222"): _ENTITY_A,
            IdentityKey("email", "a@a.com"): _ENTITY_B,
        }
        keys = record_keys(
            {"dni": "30111222", "email": "a@a.com"}, doc_fields=("cuit", "dni")
        )
        result = resolve_identity(keys, index)
        assert result.outcome == "conflict"
        assert set(result.conflicting_entities) == {_ENTITY_A, _ENTITY_B}

    def test_needs_review_when_no_strong_key(self) -> None:
        # Solo nombre — record_keys ni siquiera lo ve (no forma parte de las claves).
        keys = record_keys({"name": "Juan"}, doc_fields=("cuit", "dni"))
        result = resolve_identity(keys, {})
        assert result.outcome == "needs_review"
        assert result.entity is None

    def test_none_when_strong_key_present_but_no_match(self) -> None:
        keys = record_keys({"cuit": "20-12345678-6"}, doc_fields=("cuit", "dni"))
        result = resolve_identity(keys, {})
        assert result.outcome == "none"


class TestBuildExistingIndex:
    def test_indexes_every_key_of_every_entity(self) -> None:
        class _Fake:
            def __init__(self, cuit: str, email: str) -> None:
                self.cuit = cuit
                self.email = email

        entities = [_Fake("20-12345678-6", "a@a.com"), _Fake("27-23456789-1", "b@b.com")]
        index = build_existing_index(
            entities,
            to_record=lambda e: {"cuit": e.cuit, "email": e.email},
            doc_fields=("cuit",),
        )
        assert index[IdentityKey("doc", "20123456786")] is entities[0]
        assert index[IdentityKey("email", "b@b.com")] is entities[1]

    def test_first_entity_wins_on_duplicate_key(self) -> None:
        class _Fake:
            def __init__(self, cuit: str) -> None:
                self.cuit = cuit
                self.email = None
                self.phone = None

        entities = [_Fake("20-12345678-6"), _Fake("20-12345678-6")]
        index = build_existing_index(
            entities, to_record=lambda e: {"cuit": e.cuit}, doc_fields=("cuit",)
        )
        assert index[IdentityKey("doc", "20123456786")] is entities[0]


class TestCodeField:
    """F-ID: 'code' es el tier de MÁS prioridad — un código externo/Véktor le
    gana a documento, email y teléfono."""

    def test_code_field_produces_code_key_first(self) -> None:
        record = {"vektor_code": "CLI-0001", "cuit": "20-12345678-6"}
        keys = record_keys(record, doc_fields=("cuit",), code_field="vektor_code")
        assert [k.type for k in keys] == ["code", "doc"]
        assert keys[0].value == "cli-0001"

    def test_code_field_ausente_no_agrega_clave(self) -> None:
        keys = record_keys(
            {"cuit": "20-12345678-6"}, doc_fields=("cuit",), code_field="vektor_code"
        )
        assert [k.type for k in keys] == ["doc"]

    def test_code_gana_sobre_documento_en_match(self) -> None:
        keys = [IdentityKey("code", "cli-0001"), IdentityKey("doc", "30111222")]
        index = {
            IdentityKey("code", "cli-0001"): _ENTITY_A,
            IdentityKey("doc", "30111222"): _ENTITY_B,
        }
        result = resolve_identity(keys, index)
        # Dos entidades DISTINTAS matcheadas por claves distintas → conflict,
        # nunca "gana el primero en la lista" en silencio.
        assert result.outcome == "conflict"
        assert set(result.conflicting_entities) == {_ENTITY_A, _ENTITY_B}

    def test_code_gana_sobre_documento_cuando_apuntan_a_la_misma_entidad(self) -> None:
        keys = [IdentityKey("code", "cli-0001"), IdentityKey("doc", "30111222")]
        index = {
            IdentityKey("code", "cli-0001"): _ENTITY_A,
            IdentityKey("doc", "30111222"): _ENTITY_A,
        }
        result = resolve_identity(keys, index)
        assert result.outcome == "matched"
        assert result.entity is _ENTITY_A
        assert result.matched_key == IdentityKey("code", "cli-0001")

    def test_build_existing_index_con_code_field(self) -> None:
        class _Fake:
            def __init__(self, vektor_code: str) -> None:
                self.vektor_code = vektor_code
                self.cuit = None
                self.email = None
                self.phone = None

        entities = [_Fake("CLI-0001")]
        index = build_existing_index(
            entities,
            to_record=lambda e: {"vektor_code": e.vektor_code},
            doc_fields=(),
            code_field="vektor_code",
        )
        assert index[IdentityKey("code", "cli-0001")] is entities[0]
