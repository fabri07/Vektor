"""Bloque 5 — persistencia de decisiones EXPLÍCITAS por huella de esquema.

Wiring end-to-end: `insert_confirmed_data` (con context_mappings/context_entity/
context_confirmed/stock_treatment/shipping_decisions explícitos) → grabado →
`lookup_context_decisions` los recupera. La huella pura vive en
`test_ingestion_schema_fingerprint.py` (domain).

Consumo (preview): `build_reread_sheets`/`get_file_preview` llaman a
`lookup_remembered_decisions_for_contexts` para PRELLENAR el próximo preview
con lo que un tenant confirmó antes sobre la MISMA huella — nunca escriben,
nunca reemplazan lo que el usuario ve/edita. Esas clases de abajo prueban ese
lado (segunda sesión recupera lo que grabó la primera).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import reread_service
from app.application.services.ingestion_import_service import insert_confirmed_data
from app.application.services.ingestion_schema_decision_service import (
    lookup_context_decisions,
)
from app.config.settings import get_settings
from app.domain.ingestion_schema_fingerprint import (
    compute_context_signature,
    compute_schema_fingerprint,
)
from app.persistence.models.ingestion_schema_decision import IngestionSchemaDecision
from app.persistence.models.tenant import Tenant

pytestmark = pytest.mark.asyncio

_FILE_TYPE = "spreadsheet"
_HEADERS = ["nombre", "tienda", "precio_venta"]
_CTX = {
    "context_id": "sheet:Catalogo",
    "label": "Catalogo",
    "entity_type": "product",
    "headers": _HEADERS,
    "row_count": 1,
}
_MAPPING = {"nombre": "name", "tienda": "supplier:name", "precio_venta": "sale_price_ars"}


def _summary(headers: list[str] | None = None) -> dict[str, Any]:
    ctx = {**_CTX, "headers": headers if headers is not None else _HEADERS}
    row = {
        "nombre": "Silla de living",
        "tienda": "El pasillo",
        "precio_venta": "5000",
        "__context__": "sheet:Catalogo",
    }
    return {
        "file_type": _FILE_TYPE,
        "inferred_type": "mixed",
        "multi_sheet": True,
        "has_stock": True,
        "mapping_contexts": [ctx],
        "stock_detectado": [row],
    }


def _enable(monkeypatch: pytest.MonkeyPatch, tenant_id: Any) -> None:
    monkeypatch.setattr(
        get_settings(), "INGESTION_SCHEMA_DECISIONS_ROLLOUT_TENANT_IDS", [str(tenant_id)]
    )


def _fp_and_sig(headers: list[str] | None = None) -> tuple[str, str]:
    ctx = {**_CTX, "headers": headers if headers is not None else _HEADERS}
    fp = compute_schema_fingerprint(_FILE_TYPE, [ctx])
    sig = compute_context_signature(ctx)
    return fp, sig


async def _confirm(
    session: AsyncSession,
    tenant_id: Any,
    *,
    headers: list[str] | None = None,
    context_mappings: dict[str, dict[str, str]] | None = None,
    context_entity: dict[str, str] | None = None,
    context_confirmed: dict[str, bool] | None = None,
    stock_treatment: dict[str, str] | None = None,
    shipping_decisions: dict[str, str] | None = None,
) -> None:
    if context_confirmed is None:
        context_confirmed = {"sheet:Catalogo": True}
    await insert_confirmed_data(
        session,
        tenant_id,
        _summary(headers),
        {"productos": True},
        context_mappings=context_mappings,
        context_entity=context_entity,
        context_confirmed=context_confirmed,
        stock_treatment=stock_treatment,
        shipping_decisions=shipping_decisions,
        source="reread",
    )


async def test_misma_estructura_recupera_decisiones(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = sample_tenant.tenant_id
    _enable(monkeypatch, tid)

    await _confirm(
        db_session,
        tid,
        context_mappings={"sheet:Catalogo": _MAPPING},
        context_entity={"sheet:Catalogo": "product"},
        stock_treatment={"sheet:Catalogo": "purchase"},
    )
    await db_session.commit()

    fp, sig = _fp_and_sig()
    decisions = await lookup_context_decisions(db_session, tid, fp, sig)
    assert decisions["column_mapping"]["mapping"] == _MAPPING
    assert decisions["context_entity"]["entity"] == "product"
    assert decisions["context_included"]["included"] is True
    assert decisions["stock_treatment"]["treatment"] == "purchase"


async def test_columnas_reordenadas_recupera(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = sample_tenant.tenant_id
    _enable(monkeypatch, tid)

    await _confirm(
        db_session,
        tid,
        context_mappings={"sheet:Catalogo": _MAPPING},
    )
    await db_session.commit()

    reordered = ["precio_venta", "nombre", "tienda"]
    fp, sig = _fp_and_sig(reordered)
    decisions = await lookup_context_decisions(db_session, tid, fp, sig)
    assert decisions["column_mapping"]["mapping"] == _MAPPING


async def test_columna_agregada_no_aplica_decision_incompatible(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = sample_tenant.tenant_id
    _enable(monkeypatch, tid)

    await _confirm(
        db_session,
        tid,
        context_mappings={"sheet:Catalogo": _MAPPING},
    )
    await db_session.commit()

    changed_headers = [*_HEADERS, "codigo_barras"]
    fp, sig = _fp_and_sig(changed_headers)
    decisions = await lookup_context_decisions(db_session, tid, fp, sig)
    assert decisions == {}


async def test_mismo_archivo_en_otro_tenant_no_recupera(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.domain.verticals import Vertical
    from app.persistence.models.business import BusinessProfile
    from app.persistence.models.tenant import Tenant as TenantModel

    tid = sample_tenant.tenant_id
    other = TenantModel(
        tenant_id=uuid.uuid4(),
        legal_name="Otro tenant SRL",
        display_name="Otro tenant",
        status="ACTIVE",
    )
    db_session.add(other)
    await db_session.flush()
    db_session.add(
        BusinessProfile(
            profile_id=uuid.uuid4(),
            tenant_id=other.tenant_id,
            vertical_code=Vertical.KIOSCO_ALMACEN.value,
            data_mode="M0",
            data_confidence="LOW",
            onboarding_completed=False,
        )
    )
    await db_session.flush()

    _enable(monkeypatch, tid)  # OTHER no está habilitado ni falta que lo esté

    await _confirm(db_session, tid, context_mappings={"sheet:Catalogo": _MAPPING})
    await db_session.commit()

    fp, sig = _fp_and_sig()
    decisions_other = await lookup_context_decisions(db_session, other.tenant_id, fp, sig)
    assert decisions_other == {}


async def test_sugerencia_automatica_sin_confirmar_no_persiste(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sin ningún dict explícito (todo heurística/default), no se registra nada."""
    tid = sample_tenant.tenant_id
    _enable(monkeypatch, tid)

    await insert_confirmed_data(
        db_session,
        tid,
        _summary(),
        {"productos": True},
        source="reread",
    )
    await db_session.commit()

    fp, sig = _fp_and_sig()
    decisions = await lookup_context_decisions(db_session, tid, fp, sig)
    assert decisions == {}


async def test_decision_posterior_reemplaza_la_anterior(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = sample_tenant.tenant_id
    _enable(monkeypatch, tid)

    await _confirm(db_session, tid, context_entity={"sheet:Catalogo": "product"})
    await db_session.commit()
    await _confirm(db_session, tid, context_entity={"sheet:Catalogo": "expense"})
    await db_session.commit()

    fp, sig = _fp_and_sig()
    decisions = await lookup_context_decisions(db_session, tid, fp, sig)
    assert decisions["context_entity"]["entity"] == "expense"

    rows = (
        await db_session.execute(
            select(IngestionSchemaDecision).where(
                IngestionSchemaDecision.tenant_id == tid,
                IngestionSchemaDecision.decision_type == "context_entity",
            )
        )
    ).scalars().all()
    assert len(rows) == 1


async def test_hoja_derivada_excluida_por_defecto_no_se_registra(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Una hoja derivada que queda excluida por DEFAULT (nunca aparece en
    `context_confirmed` porque el usuario no la tocó) no debe dejar un
    `context_included` grabado — eso sería promover un default a preferencia."""
    tid = sample_tenant.tenant_id
    _enable(monkeypatch, tid)

    ctx = {
        "context_id": "sheet:Ganancias",
        "label": "Ganancias",
        "entity_type": None,
        "is_summary_or_derived": True,
        "headers": ["fecha", "monto"],
        "row_count": 0,
    }
    summary = {
        "file_type": _FILE_TYPE,
        "multi_sheet": True,
        "mapping_contexts": [ctx],
        "derived_detected": [],
    }
    # NO se pasa context_confirmed para "sheet:Ganancias" — el usuario nunca la tocó.
    await insert_confirmed_data(
        db_session, tid, summary, {"productos": True}, source="reread"
    )
    await db_session.commit()

    fp = compute_schema_fingerprint(_FILE_TYPE, [ctx])
    sig = compute_context_signature(ctx)
    decisions = await lookup_context_decisions(db_session, tid, fp, sig)
    assert decisions == {}


async def test_relectura_doble_genera_el_mismo_estado(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = sample_tenant.tenant_id
    _enable(monkeypatch, tid)

    for _ in range(2):
        await _confirm(
            db_session,
            tid,
            context_mappings={"sheet:Catalogo": _MAPPING},
            context_entity={"sheet:Catalogo": "product"},
        )
        await db_session.commit()

    rows = (
        await db_session.execute(
            select(IngestionSchemaDecision).where(IngestionSchemaDecision.tenant_id == tid)
        )
    ).scalars().all()
    # 3 decision_types distintos (column_mapping + context_entity +
    # context_included, este último porque `_confirm` siempre manda
    # `context_confirmed` por default) — 1 fila cada uno, no duplicadas.
    assert len(rows) == 3
    assert len({r.decision_type for r in rows}) == 3


async def test_flag_apagado_mantiene_el_comportamiento_previo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    assert get_settings().INGESTION_SCHEMA_DECISIONS_ROLLOUT_TENANT_IDS == []

    await _confirm(db_session, tid, context_mappings={"sheet:Catalogo": _MAPPING})
    await db_session.commit()

    fp, sig = _fp_and_sig()
    decisions = await lookup_context_decisions(db_session, tid, fp, sig)
    assert decisions == {}


# ── Consumo: el preview PRECARGA lo que una sesión anterior confirmó ─────────
#
# Estas pruebas simulan dos sesiones separadas contra la MISMA huella de
# esquema: la sesión 1 confirma (graba, vía `insert_confirmed_data`, igual que
# arriba) y la sesión 2 pide un preview NUEVO — sin ningún borrador propio
# (`draft=None`) — y debe encontrar lo que la sesión 1 dejó, en
# `sheet["remembered_decisions"]`, vía `build_reread_sheets`.


async def _sheet(sheets: list[dict[str, Any]], context_id: str) -> dict[str, Any]:
    return next(s for s in sheets if s["context_id"] == context_id)


async def test_segunda_sesion_relectura_recupera_y_precarga_decisiones(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = sample_tenant.tenant_id
    _enable(monkeypatch, tid)

    # Sesión 1: el usuario corrige y confirma.
    await _confirm(
        db_session,
        tid,
        context_mappings={"sheet:Catalogo": _MAPPING},
        context_entity={"sheet:Catalogo": "product"},
        stock_treatment={"sheet:Catalogo": "purchase"},
    )
    await db_session.commit()

    # Sesión 2: preview fresco, nadie tocó nada todavía.
    sheets, _ = await reread_service.build_reread_sheets(
        db_session, tid, _summary(), None, {"productos": True}
    )
    sheet = await _sheet(sheets, "sheet:Catalogo")
    remembered = sheet["remembered_decisions"]
    assert remembered is not None
    assert remembered["column_mapping"]["mapping"] == _MAPPING
    assert remembered["context_entity"]["entity"] == "product"
    assert remembered["stock_treatment"]["treatment"] == "purchase"


async def test_preview_refleja_tienda_a_proveedor_recordado(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El caso real de Asteria (Bloque 2): "Tienda" mapeada a `supplier:name`
    en una carga anterior debe aparecer así en el preview de la siguiente,
    no como marca (comportamiento por default sin memoria)."""
    tid = sample_tenant.tenant_id
    _enable(monkeypatch, tid)

    await _confirm(db_session, tid, context_mappings={"sheet:Catalogo": _MAPPING})
    await db_session.commit()

    sheets, _ = await reread_service.build_reread_sheets(
        db_session, tid, _summary(), None, {"productos": True}
    )
    sheet = await _sheet(sheets, "sheet:Catalogo")
    assert sheet["remembered_decisions"]["column_mapping"]["mapping"]["tienda"] == "supplier:name"


async def test_inclusion_stock_y_envio_aparecen_precargados(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = sample_tenant.tenant_id
    _enable(monkeypatch, tid)

    await _confirm(
        db_session,
        tid,
        context_confirmed={"sheet:Catalogo": True},
        stock_treatment={"sheet:Catalogo": "purchase"},
        shipping_decisions={"sheet:Catalogo": "gasto_aparte"},
    )
    await db_session.commit()

    sheets, _ = await reread_service.build_reread_sheets(
        db_session, tid, _summary(), None, {"productos": True}
    )
    remembered = (await _sheet(sheets, "sheet:Catalogo"))["remembered_decisions"]
    assert remembered["context_included"]["included"] is True
    assert remembered["stock_treatment"]["treatment"] == "purchase"
    assert remembered["shipping_decision"]["decision"] == "gasto_aparte"


async def test_modificar_decision_recordada_la_reemplaza_en_el_preview(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = sample_tenant.tenant_id
    _enable(monkeypatch, tid)

    await _confirm(db_session, tid, context_entity={"sheet:Catalogo": "product"})
    await db_session.commit()
    sheets_1, _ = await reread_service.build_reread_sheets(
        db_session, tid, _summary(), None, {"productos": True}
    )
    assert (await _sheet(sheets_1, "sheet:Catalogo"))["remembered_decisions"][
        "context_entity"
    ]["entity"] == "product"

    # El usuario corrige lo que Véktor le había recordado y vuelve a confirmar.
    await _confirm(db_session, tid, context_entity={"sheet:Catalogo": "expense"})
    await db_session.commit()

    sheets_2, _ = await reread_service.build_reread_sheets(
        db_session, tid, _summary(), None, {"productos": True}
    )
    assert (await _sheet(sheets_2, "sheet:Catalogo"))["remembered_decisions"][
        "context_entity"
    ]["entity"] == "expense"


async def test_flag_apagado_no_precarga_nada_en_el_preview(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    assert get_settings().INGESTION_SCHEMA_DECISIONS_ROLLOUT_TENANT_IDS == []

    sheets, _ = await reread_service.build_reread_sheets(
        db_session, tid, _summary(), None, {"productos": True}
    )
    sheet = await _sheet(sheets, "sheet:Catalogo")
    assert sheet["remembered_decisions"] is None


async def test_cambio_de_esquema_no_precarga_nada_en_el_preview(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = sample_tenant.tenant_id
    _enable(monkeypatch, tid)

    await _confirm(db_session, tid, context_mappings={"sheet:Catalogo": _MAPPING})
    await db_session.commit()

    changed_headers = [*_HEADERS, "codigo_barras"]
    sheets, _ = await reread_service.build_reread_sheets(
        db_session, tid, _summary(changed_headers), None, {"productos": True}
    )
    sheet = await _sheet(sheets, "sheet:Catalogo")
    assert sheet["remembered_decisions"] is None


async def test_decision_recordada_no_se_aplica_silenciosamente(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lo recordado se OFRECE (visible en `remembered_decisions`) — nunca
    reemplaza en silencio lo que el preview ya venía a mostrar. Acá la
    entidad recordada ("expense") difiere de la que detectó el parser para
    esta corrida ("product", ver `_CTX`): la hoja debe seguir mostrando
    "product" — aplicar la memoria es un paso EXPLÍCITO del usuario (mandar
    un draft), no un efecto lateral de generar el preview. Tampoco debe
    escribir nada: es un GET, no un confirm."""
    tid = sample_tenant.tenant_id
    _enable(monkeypatch, tid)

    await _confirm(db_session, tid, context_entity={"sheet:Catalogo": "expense"})
    await db_session.commit()

    rows_before = (
        (await db_session.execute(select(IngestionSchemaDecision))).scalars().all()
    )

    sheets, _ = await reread_service.build_reread_sheets(
        db_session, tid, _summary(), None, {"productos": True}
    )
    sheet = await _sheet(sheets, "sheet:Catalogo")

    assert sheet["remembered_decisions"]["context_entity"]["entity"] == "expense"
    assert sheet["entity_type"] == "product"  # la del parser, NO la recordada

    rows_after = (
        (await db_session.execute(select(IngestionSchemaDecision))).scalars().all()
    )
    assert len(rows_after) == len(rows_before)
