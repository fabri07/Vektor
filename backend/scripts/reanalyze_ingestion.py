"""Reanálisis de ingestión por versión — el comando operativo de F9a (Task 4).

Recorre archivos con ``ingestion_version`` desactualizada (por defecto: `< la
versión actual del protocolo, INGESTION_VERSION`) y usa ``reread_service`` para
diagnosticar qué les pasaría con el pipeline actual, sin reinventar la
reconciliación.

Tres modos, cada uno estrictamente más invasivo que el anterior:

    # Dry-run puro (default): CERO escrituras, ni siquiera bookkeeping.
    DATABASE_URL='...' .venv/bin/python scripts/reanalyze_ingestion.py --tenant <uuid>

    # Persiste bookkeeping (latest_preview_version/reread_status/reread_summary),
    # nunca toca negocio ni auto-aplica.
    ... scripts/reanalyze_ingestion.py --tenant <uuid> --record-scan

    # Además de lo anterior, auto-aplica donde sea elegible (ver invariante).
    ... scripts/reanalyze_ingestion.py --all-active --apply

Invariante de seguridad (no negociable — ver ``ResolvedRisk`` en
``reread_service.py``): el ÚNICO ``column_risk_outcome`` que este script puede
auto-aplicar es ``"REAPPLIED"`` (mapeo REAL que el usuario eligió en el confirm
F8b+). Los outcomes ``"NO_RISK_FOUND"``, ``"FORCED_UNVERIFIED"`` y
``"AMBIGUOUS"`` son SIEMPRE un mapeo re-derivado/guess sobre un archivo pre-F8:
van al reporte de revisión manual, nunca a ``apply_reread``, sin importar qué
flag se pase. Un archivo con ediciones humanas (``file_has_user_edits``) tampoco
se auto-aplica nunca, aunque el outcome sea REAPPLIED.

NUNCA imprime la connection URL. Correr desde ``backend/``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _db import async_engine_config, insert_decision_audit  # noqa: E402
from sqlalchemy import inspect, or_, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.application.services import reread_service  # noqa: E402
from app.domain.ingestion_version import INGESTION_VERSION  # noqa: E402
from app.integrations.s3 import S3Client  # noqa: E402
from app.persistence.models.file import (  # noqa: E402
    PROCESSING_STATUS_DONE,
    REREAD_STATUS_NEEDS_REVIEW,
    UploadedFile,
)
from app.persistence.models.tenant import Tenant  # noqa: E402

TRIGGERED_BY = "scripts/reanalyze_ingestion.py"
DECISION_TYPE_AUTO_APPLY = "INGESTION_REANALYSIS_AUTO_APPLY"

# ── buckets de reporte ─────────────────────────────────────────────────────────

BUCKET_NO_RISK_FOUND = "no_risk_found"
BUCKET_FORCED_UNVERIFIED = "forced_unverified"
BUCKET_AMBIGUOUS = "ambiguous"
BUCKET_HAS_USER_EDITS = "has_user_edits"
BUCKET_APPLIED = "applied"
BUCKET_ELIGIBLE_NOT_APPLIED = "eligible_not_applied"
# Fallo puntual de I/O (S3/DB) durante la evaluación de UN archivo — aislado
# por `run_scan` para que no aborte el resto del batch (ver docstring de
# `run_scan`).
BUCKET_ERROR = "error"

_MAX_ERROR_MESSAGE_LEN = 300


def _sanitize_error(exc: Exception) -> str:
    """Mensaje de excepción truncado, sin stack trace — evita filtrar paths
    internos/frames en el reporte que puede terminar en stdout/logs."""
    message = f"{type(exc).__name__}: {exc}"
    if len(message) > _MAX_ERROR_MESSAGE_LEN:
        message = message[:_MAX_ERROR_MESSAGE_LEN] + "…(truncado)"
    return message

# Los outcomes de riesgo re-derivado (archivo pre-F8, GUESS — nunca el mapeo
# real) mapean 1:1 a un bucket homónimo. Solo "REAPPLIED" no está acá: requiere
# el chequeo adicional de ``file_has_user_edits`` antes de decidir su bucket.
_OUTCOME_TO_BUCKET: dict[str, str] = {
    "NO_RISK_FOUND": BUCKET_NO_RISK_FOUND,
    "FORCED_UNVERIFIED": BUCKET_FORCED_UNVERIFIED,
    "AMBIGUOUS": BUCKET_AMBIGUOUS,
}

_EXCLUSION_REASONS: dict[str, str] = {
    BUCKET_NO_RISK_FOUND: (
        "sin decisiones F8b+ guardadas para reaplicar (un mapeo derivado sobre "
        "datos ya importados no es el elegido por el usuario) — revisión manual"
    ),
    BUCKET_FORCED_UNVERIFIED: (
        "outcome FORCED_UNVERIFIED: única acción legal pero sobre un mapeo "
        "re-derivado (guess), no verificado — nunca auto-aplicable"
    ),
    BUCKET_AMBIGUOUS: (
        "outcome AMBIGUOUS: 2+ acciones legales sobre un mapeo re-derivado — "
        "requiere decisión humana explícita"
    ),
    BUCKET_HAS_USER_EDITS: (
        "el archivo tiene registros (venta/gasto/producto) con "
        "has_user_edits=True — la relectura podría pisar una corrección manual"
    ),
    BUCKET_ELIGIBLE_NOT_APPLIED: (
        "outcome REAPPLIED sin ediciones humanas, pero no se pasó --apply"
    ),
}


@dataclass
class ScanEntry:
    """Resultado de evaluar UN archivo candidato — unidad del reporte.

    ``ambiguous_rows``/``forced_rows`` se guardan SEPARADOS (fix round
    post-review, hallazgo Important #3) para poder alimentar el shape único de
    ``reread_summary`` (``reread_service.build_reread_summary``, que espera
    las dos listas por separado bajo ``risk_columns``) — antes se combinaban en
    un único ``risk_rows`` plano y esa distinción se perdía."""

    file_id: uuid.UUID
    tenant_id: uuid.UUID
    filename: str
    bucket: str
    outcome: str
    ambiguous_rows: list[dict[str, Any]] = field(default_factory=list)
    forced_rows: list[dict[str, Any]] = field(default_factory=list)
    exclusion_reason: str | None = None
    applied: bool = False
    from_version: int = 1
    to_version: int = INGESTION_VERSION

    @property
    def risk_rows(self) -> list[dict[str, Any]]:
        """Vista combinada (forzadas + ambiguas) para el reporte tabular —
        mismo orden que antes del fix round (forzadas primero)."""
        return [*self.forced_rows, *self.ambiguous_rows]


# ── selección de candidatos ────────────────────────────────────────────────────


async def select_active_tenant_ids(session: AsyncSession) -> list[uuid.UUID]:
    rows = await session.execute(select(Tenant.tenant_id).where(Tenant.status == "ACTIVE"))
    return [r[0] for r in rows.all()]


async def select_candidate_files(
    session: AsyncSession,
    *,
    tenant_ids: list[uuid.UUID],
    from_version: int,
    to_version: int,
    limit: int | None = None,
    skip_scanned: bool = False,
) -> list[UploadedFile]:
    """Archivos candidatos a reanálisis: procesados, no borrados, con crudo en S3
    y ``ingestion_version`` dentro de ``[from_version, to_version)``.

    ``skip_scanned`` (Important #5, fix round post-review): los outcomes
    ``NO_RISK_FOUND``/``FORCED_UNVERIFIED``/``AMBIGUOUS`` correctamente NO
    bumpean ``ingestion_version`` (por diseño — ver ``ResolvedRisk`` en
    ``reread_service.py``), lo que significaba que CADA corrida de
    ``--all-active`` volvía a re-descargar y re-parsear esos mismos archivos de
    S3 para siempre, sin forma de saltarlos. Con ``skip_scanned=True``, se
    excluyen los archivos donde ``latest_preview_version IS NOT NULL AND
    latest_preview_version >= to_version`` — ya fueron escaneados con la
    versión objetivo actual (o una mayor), así que no hace falta re-escanearlos
    hasta que ``to_version`` suba. Default ``False`` — comportamiento actual
    preservado si no se pasa el flag."""
    stmt = (
        select(UploadedFile)
        .where(
            UploadedFile.tenant_id.in_(tenant_ids),
            UploadedFile.processing_status == PROCESSING_STATUS_DONE,
            UploadedFile.deleted_at.is_(None),
            UploadedFile.s3_key.is_not(None),
            UploadedFile.ingestion_version >= from_version,
            UploadedFile.ingestion_version < to_version,
        )
        .order_by(UploadedFile.created_at)
    )
    if skip_scanned:
        stmt = stmt.where(
            or_(
                UploadedFile.latest_preview_version.is_(None),
                UploadedFile.latest_preview_version < to_version,
            )
        )
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


# ── evaluación por archivo ─────────────────────────────────────────────────────


async def evaluate_file(
    session: AsyncSession,
    s3: S3Client,
    file: UploadedFile,
    *,
    do_apply: bool,
    to_version: int = INGESTION_VERSION,
) -> ScanEntry:
    """Pasos 1–3 del flujo: preview + clasificación + auto-apply condicional.

    NO hace bookkeeping sobre ``file`` (eso es responsabilidad de
    ``record_bookkeeping``, que el caller invoca solo si corresponde según los
    flags) — así el modo dry-run puro puede llamar esto sin mutar nada.

    Invariante de seguridad: SOLO ``outcome == "REAPPLIED"`` sin ediciones
    humanas puede llegar a ``apply_reread`` — y solo si ``do_apply=True``.

    ``to_version`` es el valor que el usuario pidió vía ``--to-version``
    (default ``INGESTION_VERSION`` actual) — se usa SOLO para el reporte/
    auditoría de "qué rango pidió el usuario" (fix round post-review, hallazgo
    Minor #7). El algoritmo que ``apply_reread`` REALMENTE corre y stampea
    sobre el archivo sigue siendo siempre ``INGESTION_VERSION`` (la versión
    real del código corriendo), independientemente de este argumento.
    """
    previous_version = file.ingestion_version
    preview = await reread_service.preview_reread(session, file.id, file.tenant_id, s3=s3)
    outcome = preview.column_risk_outcome

    def _entry(bucket: str, *, applied: bool = False) -> ScanEntry:
        return ScanEntry(
            file_id=file.id,
            tenant_id=file.tenant_id,
            filename=file.original_filename,
            bucket=bucket,
            outcome=outcome,
            ambiguous_rows=preview.column_risk_ambiguous,
            forced_rows=preview.column_risk_forced_unverified,
            exclusion_reason=None if applied else _EXCLUSION_REASONS.get(bucket),
            applied=applied,
            from_version=previous_version,
            to_version=to_version,
        )

    if outcome != "REAPPLIED":
        # Mapeo re-derivado (guess) sobre un archivo pre-F8 — NUNCA se aplica,
        # ni siquiera con --apply. Ver invariante de seguridad en el docstring
        # del módulo y en ``ResolvedRisk`` (reread_service.py).
        return _entry(_OUTCOME_TO_BUCKET.get(outcome, BUCKET_NO_RISK_FOUND))

    has_edits = await reread_service.file_has_user_edits(session, file.id, file.tenant_id)
    if has_edits:
        return _entry(BUCKET_HAS_USER_EDITS)

    if not do_apply:
        return _entry(BUCKET_ELIGIBLE_NOT_APPLIED)

    # Único camino legal de auto-apply: REAPPLIED + sin ediciones humanas + --apply.
    await reread_service.apply_reread(session, file.id, file.tenant_id, s3=s3, origin="batch_auto")
    await session.commit()
    # Delega 1:1 en el helper canónico de `_db.py` — sin bifurcación por
    # dialecto acá. En producción esto SIEMPRE corre contra Postgres real
    # (`gen_random_uuid()`/`now()` server-side); no existe una segunda
    # implementación de este INSERT en el módulo. Los tests (SQLite
    # in-memory) reemplazan esta función por un fake vía monkeypatch — ver
    # `app/tests/scripts/test_reanalyze_ingestion.py`.
    await insert_decision_audit(
        session,
        tenant_id=str(file.tenant_id),
        decision_type=DECISION_TYPE_AUTO_APPLY,
        decision_data={
            "file_id": str(file.id),
            "from_version": previous_version,
            "to_version": to_version,
        },
        triggered_by=TRIGGERED_BY,
    )
    await session.commit()
    return _entry(BUCKET_APPLIED, applied=True)


def record_bookkeeping(file: UploadedFile, entry: ScanEntry) -> None:
    """Persiste ``latest_preview_version``/``reread_status``/``reread_summary``.

    El caller decide si commitear (y si llamar esto: nunca en dry-run puro). Si
    ``entry.applied`` es ``True``, ``apply_reread`` YA seteó
    ``reread_status``/``reread_summary`` con más detalle (incluyendo
    ``run_id``) — acá no se pisa, solo se completa ``latest_preview_version``
    (que ``apply_reread`` no toca).

    ``reread_summary`` se arma vía ``reread_service.build_reread_summary`` —
    fix round post-review (hallazgo Important #3): antes este módulo escribía
    un shape propio (``bucket``/``risk_columns`` plano) incompatible con el que
    escribe ``apply_reread`` (``ambiguous_columns``/``forced_unverified_columns``
    sueltos); ahora ambos convergen al mismo shape único."""
    file.latest_preview_version = INGESTION_VERSION
    if entry.applied:
        return
    file.reread_status = REREAD_STATUS_NEEDS_REVIEW
    file.reread_summary = reread_service.build_reread_summary(
        entry.outcome,
        ambiguous=entry.ambiguous_rows,
        forced_unverified=entry.forced_rows,
        extra={
            "bucket": entry.bucket,
            "has_user_edits": entry.bucket == BUCKET_HAS_USER_EDITS,
            "scanned_at": datetime.now(UTC).isoformat(),
            "scanned_by": TRIGGERED_BY,
        },
    )


async def run_scan(
    session: AsyncSession,
    s3: S3Client,
    files: list[UploadedFile],
    *,
    do_apply: bool,
    record_scan: bool,
    to_version: int = INGESTION_VERSION,
) -> list[ScanEntry]:
    """Orquesta ``evaluate_file`` + bookkeeping condicional sobre una lista de
    candidatos ya seleccionada (``select_candidate_files``). Reusado por
    ``main()`` y por los tests — la idempotencia real de correr el comando dos
    veces sale de que la SEGUNDA ``select_candidate_files`` ya no devuelve un
    archivo bumpeado a ``INGESTION_VERSION`` (fuera del filtro
    ``ingestion_version < to_version``), no de que ``evaluate_file`` sea
    idempotente por sí solo — llamarlo dos veces sobre el MISMO archivo
    seleccionado explícitamente SÍ reaplicaría de nuevo.

    Aislamiento de errores por archivo: este comando corre potencialmente
    contra TODOS los tenants activos (``--all-active``); un fallo puntual de
    I/O (objeto S3 faltante, red transitoria, contenido corrupto) en UN
    archivo no debe abortar la evaluación del resto del batch. Cualquier
    excepción durante ``evaluate_file`` se captura acá, se hace
    ``session.rollback()`` (la sesión puede quedar en estado "pending
    rollback" tras un fallo dentro de un `flush`/`commit`; sin esto, TODOS los
    archivos siguientes fallarían en cascada) y el archivo se reporta en
    ``BUCKET_ERROR`` con el mensaje sanitizado — nunca bookkeeping para un
    archivo cuya evaluación no se completó.

    Re-fetch defensivo por iteración (fix round post-review, hallazgo
    Critical): un ``session.rollback()`` del bloque ``except`` de una
    iteración ANTERIOR expira INCONDICIONALMENTE todos los objetos cargados en
    el identity map de la sesión — sin importar ``expire_on_commit`` (eso solo
    protege los ``commit()``, ver ``main()``). Confirmado empíricamente:
    incluso leer el atributo de primary key de un objeto expirado FUERA de un
    ``await`` explícito revienta con ``MissingGreenlet`` — exactamente lo que
    enmascaraba el manejo de errores anterior (funcionaba solo cuando el fake
    de test lanzaba la excepción ANTES de cualquier query real; con una query
    real de por medio, como ``preview_reread``/``_load_file`` hace siempre, el
    rollback subsiguiente dejaba el SIGUIENTE archivo de la lista expirado y
    el propio ``except`` reventaba un segundo ``MissingGreenlet`` al leer sus
    atributos para el reporte, enmascarando el error original).

    Por eso acá NUNCA se lee un atributo de un ``UploadedFile`` de la lista
    ``files`` directamente: ``inspect(stale_file).identity`` lee la identity
    key cacheada en el ``InstanceState`` (nunca dispara una carga a la DB, es
    seguro incluso sobre un objeto expirado) para obtener el id, y
    ``session.get(UploadedFile, file_id)`` — async-seguro, corre dentro del
    greenlet de SQLAlchemy — garantiza un objeto fresco antes de tocar
    cualquier otro atributo o de pasarlo a ``evaluate_file`` (que lee
    ``file.ingestion_version`` sincrónicamente ni bien entra). Si el objeto NO
    estaba expirado, ``session.get`` es un hit de identity map sin ida a la
    DB — sin costo extra en el camino feliz."""
    entries: list[ScanEntry] = []
    for stale_file in files:
        identity = inspect(stale_file).identity
        # ``identity`` solo es ``None`` para instancias transient/pending
        # (nunca flusheadas) — ``files`` siempre viene de una query ya
        # ejecutada (``select_candidate_files`` o un fixture de test
        # commiteado), así que esto nunca dispara en la práctica; el guard es
        # para mypy y para no propagar un ``TypeError`` críptico si algún
        # caller futuro rompe esa invariante.
        if identity is None:
            continue
        file_id = identity[0]
        file = await session.get(UploadedFile, file_id)
        if file is None:
            continue  # borrado concurrentemente entre la selección y el scan
        tenant_id = file.tenant_id
        filename = file.original_filename
        previous_version = file.ingestion_version
        try:
            entry = await evaluate_file(
                session, s3, file, do_apply=do_apply, to_version=to_version
            )
        except Exception as exc:  # noqa: BLE001 — aislamiento por archivo, ver docstring.
            await session.rollback()
            entries.append(
                ScanEntry(
                    file_id=file_id,
                    tenant_id=tenant_id,
                    filename=filename,
                    bucket=BUCKET_ERROR,
                    outcome="error",
                    exclusion_reason=_sanitize_error(exc),
                    applied=False,
                    from_version=previous_version,
                    to_version=to_version,
                )
            )
            continue
        if record_scan:
            record_bookkeeping(file, entry)
            await session.commit()
        entries.append(entry)
    return entries


# ── reporte ────────────────────────────────────────────────────────────────────


def _format_risk_row(row: dict[str, Any]) -> str:
    return (
        f"context_id={row.get('context_id')} source_column={row.get('source_column')!r} "
        f"target_field={row.get('target_field')!r} "
        f"action/allowed_actions={row.get('action') or row.get('allowed_actions')} "
        f"null_ratio={row.get('null_ratio')} affected_rows={row.get('affected_rows')}"
    )


def print_report(entries: list[ScanEntry]) -> None:
    for e in entries:
        print(
            f"  · {e.filename!r} tenant={e.tenant_id} file={e.file_id} "
            f"[{e.bucket}] outcome={e.outcome} v{e.from_version}→{e.to_version}"
        )
        for row in e.risk_rows:
            print(f"      columna riesgosa: {_format_risk_row(row)}")
        if e.bucket == BUCKET_ERROR:
            print(f"      ERROR: {e.exclusion_reason}")
        elif e.exclusion_reason:
            print(f"      excluido de auto-apply: {e.exclusion_reason}")

    totals: dict[str, int] = {}
    for e in entries:
        totals[e.bucket] = totals.get(e.bucket, 0) + 1
    print("\nRESUMEN por bucket:")
    for bucket, count in sorted(totals.items()):
        print(f"  {bucket}: {count}")
    print(f"  TOTAL: {len(entries)}")

    errors = [e for e in entries if e.bucket == BUCKET_ERROR]
    if errors:
        print(f"\n{len(errors)} archivo(s) con ERROR durante el scan (no abortaron el batch):")
        for e in errors:
            print(f"  · tenant={e.tenant_id} file={e.file_id} {e.filename!r}: {e.exclusion_reason}")


def build_report(entries: list[ScanEntry]) -> dict[str, Any]:
    totals: dict[str, int] = {}
    files: list[dict[str, Any]] = []
    for e in entries:
        totals[e.bucket] = totals.get(e.bucket, 0) + 1
        files.append(
            {
                "file_id": str(e.file_id),
                "tenant_id": str(e.tenant_id),
                "filename": e.filename,
                "bucket": e.bucket,
                "outcome": e.outcome,
                "applied": e.applied,
                "exclusion_reason": e.exclusion_reason,
                "from_version": e.from_version,
                "to_version": e.to_version,
                "risk_columns": e.risk_rows,
            }
        )
    return {"files": files, "totals": totals, "total_files": len(entries)}


# ── entrypoint ─────────────────────────────────────────────────────────────────


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", help="UUID de tenant puntual (piloto)")
    parser.add_argument("--all-active", action="store_true", help="Todos los tenants activos")
    parser.add_argument(
        "--from-version", type=int, default=1, help="ingestion_version mínima (inclusive)"
    )
    parser.add_argument(
        "--to-version",
        type=int,
        default=INGESTION_VERSION,
        help="ingestion_version tope (exclusive); default INGESTION_VERSION actual",
    )
    parser.add_argument("--limit", type=int, default=None, help="Tope de archivos (pilotos)")
    parser.add_argument(
        "--record-scan",
        action="store_true",
        help="Persiste bookkeeping (latest_preview_version/reread_summary/reread_status)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Además de --record-scan, auto-aplica donde el outcome sea REAPPLIED",
    )
    parser.add_argument(
        "--skip-scanned",
        action="store_true",
        help=(
            "Excluye archivos con latest_preview_version >= to_version (ya "
            "escaneados con la versión objetivo actual) — evita re-descargar/"
            "re-parsear de S3 en cada corrida de --all-active. Default: no "
            "excluye nada (comportamiento actual preservado)."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Imprime además un resumen JSON")
    args = parser.parse_args()

    if not args.tenant and not args.all_active:
        print("ERROR: indicá --tenant <uuid> (piloto) o --all-active.")
        sys.exit(2)

    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    s3 = S3Client()
    record_scan = args.record_scan or args.apply

    # F9a fix (Critical, fix round post-review): `expire_on_commit=False` evita
    # que los commits intermedios (dos DENTRO de `evaluate_file` en el camino
    # REAPPLIED+apply, uno en `record_bookkeeping`) expiren TODOS los objetos
    # cargados del identity map — incluidos los archivos candidatos que
    # `run_scan` todavía no procesó en esta misma corrida. `run_scan` además
    # se protege por sí solo (re-fetch defensivo vía `session.get` en cada
    # iteración, ver su docstring) contra la expiración INCONDICIONAL que un
    # `rollback()` (tras un error aislado por archivo) sigue disparando pese a
    # este flag — las dos partes del fix son necesarias.
    async with AsyncSession(engine, expire_on_commit=False) as session:
        tenant_ids = (
            [uuid.UUID(args.tenant)] if args.tenant else await select_active_tenant_ids(session)
        )
        files = await select_candidate_files(
            session,
            tenant_ids=tenant_ids,
            from_version=args.from_version,
            to_version=args.to_version,
            limit=args.limit,
            skip_scanned=args.skip_scanned,
        )

        mode = "APPLY" if args.apply else ("RECORD-SCAN" if args.record_scan else "DRY-RUN")
        print(
            f"[{mode}] reanálisis de ingestión: {len(files)} archivo(s) candidato(s) "
            f"(from_version={args.from_version}, to_version={args.to_version})"
        )

        entries = await run_scan(
            session,
            s3,
            files,
            do_apply=args.apply,
            record_scan=record_scan,
            to_version=args.to_version,
        )

        print()
        print_report(entries)

        if args.json:
            print("\n" + json.dumps(build_report(entries), ensure_ascii=False, default=str))

        if not record_scan:
            await session.rollback()
            print("\nDry-run: nada se escribió (ni bookkeeping).")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
