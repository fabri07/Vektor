"""Bloque 7 — dry-run del pipeline de ingestión contra el Excel REAL de
Asteria, sobre Postgres LOCAL/descartable. NUNCA toca Neon ni Railway.

Uso — DOS sesiones de PROCESO separadas (correr dos veces en el mismo
intérprete no prueba que Bloque 5 recupere lo persistido entre procesos):

    source <archivo con DATABASE_URL de Postgres LOCAL + S3_* de R2>
    .venv/bin/python scripts/asteria_dryrun_bloque7.py --session a
    # cerrar esta terminal / abrir un intérprete nuevo, MISMO Postgres local
    .venv/bin/python scripts/asteria_dryrun_bloque7.py --session b

Cada sesión descarga el Excel real desde R2 (read-only) y confirma la
importación contra la base LOCAL con los mismos datos: `insert_confirmed_data`
directo (mismo camino que usa toda la suite de tests), sin servidor HTTP ni
auth de por medio.

Guarda de seguridad: aborta si `DATABASE_URL` huele a un host administrado
(Neon/Railway/RDS/Supabase) — este script solo corre contra Postgres
local/descartable.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from typing import Any

# Tenant + archivo determinísticos: fijos para que las dos sesiones (procesos
# DISTINTOS) coincidan sin pasarse nada a mano. Nunca son un tenant/id real.
DRYRUN_TENANT_ID = uuid.UUID("a57e21a0-0000-4000-8000-000000000001")
S3_KEY = (
    "uploads/ef97804b-79a9-4c34-a50d-f83b3b9c9e77/"
    "de003be6-330f-48f4-9337-fca82c9d90bf/ASTERIA_home_deco.xlsx"
)
FILENAME = "ASTERIA_home_deco.xlsx"

# Las 3 flags de rollout se habilitan ACÁ, solo para el tenant de dry-run de
# arriba, ANTES de importar Settings (`get_settings()` cachea la instancia) —
# así no depende de que quien corra el script se acuerde de exportarlas, y
# nunca se cuela un tenant real.
for _var in (
    "PRODUCT_SUPPLIER_LINKS_ROLLOUT_TENANT_IDS",
    "CATALOG_FINAL_COST_ROLLOUT_TENANT_IDS",
    "INGESTION_SCHEMA_DECISIONS_ROLLOUT_TENANT_IDS",
):
    os.environ.setdefault(_var, str(DRYRUN_TENANT_ID))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_PROD_MARKERS = ("neon.tech", "railway", "amazonaws", "rds.", "supabase")


def _abort_if_prod_like(url: str) -> None:
    lowered = url.lower()
    for marcador in _PROD_MARKERS:
        if marcador in lowered:
            raise SystemExit(
                f"ABORTADO: DATABASE_URL parece producción/managed ({marcador!r} "
                "detectado). Este script SOLO corre contra Postgres local/descartable."
            )


def p(title: str) -> None:
    print(f"\n{'=' * 70}\n  {title}\n{'=' * 70}")


async def _ensure_tenant(session: Any) -> bool:
    """True si lo creó ahora (sesión A), False si ya existía (sesión B+)."""
    from app.domain.verticals import Vertical
    from app.persistence.models.business import BusinessProfile
    from app.persistence.models.tenant import Tenant

    existing = await session.get(Tenant, DRYRUN_TENANT_ID)
    if existing is not None:
        return False
    session.add(
        Tenant(
            tenant_id=DRYRUN_TENANT_ID,
            legal_name="Asteria Dry-Run (Bloque 7) — NO ES UN CLIENTE REAL",
            display_name="Asteria Dry-Run",
            status="ACTIVE",
        )
    )
    await session.flush()
    session.add(
        BusinessProfile(
            profile_id=uuid.uuid4(),
            tenant_id=DRYRUN_TENANT_ID,
            vertical_code=Vertical.DECORACION_HOGAR.value,
            data_mode="M0",
            data_confidence="LOW",
            onboarding_completed=True,
        )
    )
    await session.commit()
    return True


async def _ensure_uploaded_file(session: Any, content_len: int) -> uuid.UUID:
    from sqlalchemy import select

    from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile

    existing = (
        await session.execute(
            select(UploadedFile).where(
                UploadedFile.tenant_id == DRYRUN_TENANT_ID,
                UploadedFile.original_filename == FILENAME,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing.id  # type: ignore[no-any-return]

    record = UploadedFile(
        tenant_id=DRYRUN_TENANT_ID,
        uploaded_by=None,
        original_filename=FILENAME,
        s3_key=S3_KEY,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=content_len,
        purpose="ingestion",
        status="uploaded",
        processing_status=PROCESSING_STATUS_DONE,
    )
    session.add(record)
    await session.commit()
    return record.id


async def _download_asteria_file() -> bytes:
    from app.integrations.s3 import S3Client

    s3 = S3Client()
    content = await s3.download(S3_KEY)
    print(f"  descargado read-only de R2: {len(content)} bytes")
    return content


def _normalize(header: str) -> str:
    import re
    import unicodedata

    stripped = "".join(
        c for c in unicodedata.normalize("NFKD", str(header)) if not unicodedata.combining(c)
    )
    return re.sub(r"[\s\-_/]+", "_", stripped.strip().lower())


async def _build_confirm_payload(
    session: Any, summary: dict[str, Any]
) -> tuple[dict[str, dict[str, str]], dict[str, str], dict[str, bool], dict[str, str]]:
    """Decisiones EXPLÍCITAS que simula lo que Asteria confirmaría a mano:
    mapeo sugerido por el motor determinístico de la propia app (sin LLM,
    mismo criterio que el preview real) + la corrección real de Bloque 2
    ("Tienda" → proveedor) en la hoja de catálogo. Las hojas derivadas
    (Ganancias) y la de movimientos ambiguos quedan afuera de
    `context_confirmed` — comportamiento default, Bloque 1."""
    from app.application.services.column_mapping_service import ColumnMappingService
    from app.application.services.ingestion_import_service import _rows_for_context

    mapping_svc = ColumnMappingService(session)
    context_mappings: dict[str, dict[str, str]] = {}
    context_entity: dict[str, str] = {}
    context_confirmed: dict[str, bool] = {}
    stock_treatment: dict[str, str] = {}

    bucket_by_entity = {
        "product": "stock_detectado",
        "sale": "ventas_detectadas",
        "expense": "gastos_detectados",
    }

    for ctx in summary.get("mapping_contexts") or []:
        entity = ctx.get("entity_type")
        cid = ctx.get("context_id")
        if not cid or entity not in bucket_by_entity or ctx.get("is_summary_or_derived"):
            continue
        headers = ctx.get("headers") or []
        bucket = summary.get(bucket_by_entity[entity]) or []
        rows = _rows_for_context(bucket, cid)
        if not rows:
            continue
        suggestions = await mapping_svc.suggest_mappings(
            DRYRUN_TENANT_ID, entity, headers, rows[:10], allow_llm=False
        )
        mapping = {
            s["source_column"]: s["target_field"]
            for s in suggestions
            if s["status"] == "mapped" and s["target_field"]
        }
        if entity == "product":
            for h in headers:
                if _normalize(h) in ("tienda", "proveedor"):
                    mapping[h] = "supplier:name"
            stock_treatment[cid] = "opening_balance"
        context_mappings[cid] = mapping
        context_entity[cid] = entity
        context_confirmed[cid] = True

    return context_mappings, context_entity, context_confirmed, stock_treatment


async def run(session_label: str) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.application.services.file_parsing import parse_uploaded_content
    from app.application.services.ingestion_import_service import insert_confirmed_data
    from app.application.services.ingestion_schema_decision_service import (
        lookup_remembered_decisions_for_contexts,
    )
    from app.config.settings import get_settings
    from app.domain.ingestion_schema_fingerprint import compute_schema_fingerprint

    settings = get_settings()
    _abort_if_prod_like(settings.DATABASE_URL)
    _abort_if_prod_like(settings.DATABASE_URL_SYNC)

    p(f"SESIÓN {session_label.upper()} — {settings.DATABASE_URL_SYNC.split('@')[-1]}")

    engine = create_async_engine(
        settings.DATABASE_URL, pool_pre_ping=True, connect_args=settings.pg_connect_args
    )
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with session_maker() as session:
        created = await _ensure_tenant(session)
        print(f"  tenant {'creado' if created else 'reusado'}: {DRYRUN_TENANT_ID}")

        content = await _download_asteria_file()
        uploaded_file_id = await _ensure_uploaded_file(session, len(content))
        print(f"  uploaded_file_id: {uploaded_file_id}")

        summary = parse_uploaded_content(
            content,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            FILENAME,
        )

        # Bloque 5 — ANTES de confirmar nada en esta sesión: ¿qué recuerda de
        # una sesión previa? En la sesión A tiene que dar vacío; en la B tiene
        # que traer lo que la A confirmó.
        remembered = await lookup_remembered_decisions_for_contexts(
            session,
            DRYRUN_TENANT_ID,
            str(summary.get("file_type") or ""),
            summary.get("mapping_contexts") or [],
        )
        p("BLOQUE 5 — decisiones recordadas ANTES de confirmar esta sesión")
        if not remembered:
            print("  (vacío)")
        else:
            for cid, decisions in remembered.items():
                print(f"  {cid}: {list(decisions.keys())}")
                if "column_mapping" in decisions:
                    tienda_targets = {
                        k: v
                        for k, v in decisions["column_mapping"]["mapping"].items()
                        if _normalize(k) in ("tienda", "proveedor")
                    }
                    if tienda_targets:
                        print(f"    Tienda → {tienda_targets}")

        context_mappings, context_entity, context_confirmed, stock_treatment = (
            await _build_confirm_payload(session, summary)
        )

        p("CONFIRMANDO (insert_confirmed_data) contra la base LOCAL")
        counts = await insert_confirmed_data(
            session,
            DRYRUN_TENANT_ID,
            summary,
            {"productos": True, "ventas": True, "gastos": True},
            context_mappings=context_mappings,
            context_entity=context_entity,
            context_confirmed=context_confirmed,
            stock_treatment=stock_treatment,
            source="reread",
            uploaded_file_id=uploaded_file_id,
        )
        await session.commit()
        print("  counts:")
        for k, v in sorted(counts.items()):
            if isinstance(v, int | float) and v:
                print(f"    {k}: {v}")

        fp = compute_schema_fingerprint(
            str(summary.get("file_type") or ""), summary.get("mapping_contexts") or []
        )
        print(f"\n  schema_fingerprint: {fp}")

    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", choices=["a", "b"], required=True)
    args = parser.parse_args()
    asyncio.run(run(args.session))
