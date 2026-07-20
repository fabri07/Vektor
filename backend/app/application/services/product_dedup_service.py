"""Dedup de productos — FASE de LECTURA/PLANIFICACIÓN (F3-T4).

Detecta grupos de productos duplicados de un tenant, elige el canónico, clasifica
conflictos de identidad y la decisión de stock por procedencia (funciones PURAS,
sin session), calcula un fingerprint determinístico por grupo y — en dry-run —
persiste el PLAN (``DataRepairRun`` + ``DataRepairItem``) sin tocar NINGÚN dato de
negocio (Product / SaleEntry / ExpenseEntry / InventoryBalance / InventoryMovement).

Las MUTACIONES reales de negocio (fusionar, re-apuntar FKs, consolidar balances,
desactivar duplicados) son F3-T5; el revert es F3-T6. Este módulo NO las implementa.
Sí escribe en las tablas de AUDITORÍA y PLAN (``data_repair_runs`` /
``data_repair_items`` / ``decision_audit_log``) — no son datos de negocio.

Reusa el motor de identidad de F2 (``_load_product_identity_indexes`` +
``ProductIdentityIndexes`` de ``ingestion_import_service``) — NO hay un segundo motor.

## Contrato para T5/T6 (consumen estas dos funciones puras)

``classify_group_stock_decision(canonical, duplicates) -> StockDecision``
    Decisión de stock a NIVEL DE GRUPO (canónico + TODOS los duplicados): devuelve
    UN solo ``StockDecision(kind, delta, reason)`` ya total y sumable-safe. ``delta``
    (``canonical_delta``) es el ÚNICO entero que T5 aplica INCREMENTALMENTE al
    ``stock_units`` Y al ``current_qty`` del canónico (misma política para ambos), o
    ``None`` si ``kind == REVIEW`` (no se toca nada). NUNCA recomputa el saldo desde
    el ledger (invariante 2d) — la descomposición residuo=stock_units−Σledger es solo
    para DECIDIR, no para asignar el saldo. **T5 aplica UN delta por GRUPO**, no uno
    por duplicado (la decisión pairwise NO era sumable: sobre-contaba catálogos
    repetidos y filas compartidas entre duplicados hermanos).

    ``classify_stock_decision(duplicate, canonical)`` queda como envoltorio del caso
    de un solo duplicado (``classify_group_stock_decision(canonical, [duplicate])``).

``compute_group_fingerprint(records, canonical_id, stock_decision) -> str``
    sha256 de un JSON canónico ordenado del estado ACTUAL relevante del grupo
    (ids, canónico, #FKs, stock/balance, hashes de movimientos-evidencia, la ÚNICA
    decisión de stock group-level y ``DECISION_VERSION``). T5 lo recomputa justo
    antes de aplicar y ABORTA el grupo si cambió (cierra el hueco dry-run↔apply).
    Determinístico: NO usa ``datetime.now()`` ni ``random``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

from app.application.services.inventory_movement_origin import (
    SOURCE_CATALOG_INITIAL_STOCK,
    SOURCE_TYPES,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.application.services.ingestion_import_service import ProductIdentityIndexes

# ── Versionado de la lógica de decisión ──────────────────────────────────────────
# Cambiar CUALQUIER regla de identidad/stock/fingerprint OBLIGA a subir esta versión:
# entra al fingerprint, así T5 aborta planes calculados con una versión distinta.
DECISION_VERSION = 1

# ── Tipos de arista ──────────────────────────────────────────────────────────────
EDGE_STRONG = "STRONG"  # comparten barcode_normalized (identidad fuerte)
EDGE_MEDIUM = "MEDIUM"  # comparten sku_normalized
EDGE_WEAK = "WEAK"      # comparten (name_normalized, brand_normalized) — NO propaga merge

# Solo fuerte+medio arman componentes de merge. La cadena transitiva
# ``A—nombre—B—sku—C`` NO fusiona A+C: el nombre no conecta.
_MERGE_EDGE_KINDS = frozenset({EDGE_STRONG, EDGE_MEDIUM})

# ── Tipos de grupo ───────────────────────────────────────────────────────────────
GROUP_MERGE = "MERGE"            # componente fuerte+medio, size≥2 (candidato a fusión)
GROUP_WEAK_REVIEW = "WEAK_REVIEW"  # colisión débil-sola (mismo nombre+marca, no fusiona)

# ── Decisión de stock ────────────────────────────────────────────────────────────
STOCK_KEEP_ONE = "KEEP_ONE"      # misma fila importada 2× → no sumar (delta=0)
STOCK_MOST_RECENT = "MOST_RECENT"  # solo catalog_initial_stock repetido → saldo más reciente
STOCK_SUM = "SUM"                # compras reales distintas → sumar incremental
STOCK_REVIEW = "REVIEW"          # ambiguo → no tocar (delta=None)

# Motivos de review (identidad y stock) — van al CSV/auditoría.
REVIEW_BARCODE_DIVERGENCE = "BARCODE_DIVERGENCE"
REVIEW_SKU_DIVERGENCE = "SKU_DIVERGENCE"
REVIEW_COST_DIVERGENCE = "COST_DIVERGENCE"
STOCK_REASON_RESERVED = "RESERVED_QTY_NONZERO"
STOCK_REASON_RESIDUO = "RESIDUO_NO_LEDGER"
STOCK_REASON_MIXED = "MIXED_PROVENANCE"
STOCK_REASON_KEEP_ONE = "SHARED_SOURCE_ROW_HASH"
STOCK_REASON_MOST_RECENT = "CATALOG_INITIAL_STOCK_REPEATED"
STOCK_REASON_SUM = "DISTINCT_PURCHASES"
STOCK_REASON_EMPTY = "NO_LIVE_LEDGER"

# Chunk determinístico de re-apuntado de FKs (before_json {table, chunk_index, rows}).
REPOINT_CHUNK_SIZE = 500

_REPAIR_TYPE = "PRODUCT_DEDUP"
_DECISION_TYPE = "PRODUCT_DEDUP"


# ── Estructuras de datos (puras) ─────────────────────────────────────────────────


@dataclass(frozen=True)
class MovementRow:
    """Un ``InventoryMovement`` VIVO (voided_at IS NULL) reducido a lo que la
    decisión de stock necesita. ``created_at`` nunca es None (fallback temporal)."""

    qty: int
    movement_type: str
    source_type: str | None
    source_row_hash: str | None
    occurred_at: datetime | None
    created_at: datetime


@dataclass(frozen=True)
class ProductRecord:
    """Estado ACTUAL de un producto relevante para el dedup (solo lectura)."""

    id: uuid.UUID
    has_user_edits: bool
    barcode_normalized: str | None
    sku_normalized: str | None
    created_at: datetime
    unit_cost_ars: Decimal | None
    stock_units: int
    fk_count: int
    current_qty: int | None
    reserved_qty: int | None
    movements: tuple[MovementRow, ...] = ()


@dataclass(frozen=True)
class Edge:
    """Arista del grafo de duplicados. ``kind`` ∈ {STRONG, MEDIUM, WEAK}."""

    from_id: uuid.UUID
    to_id: uuid.UUID
    kind: str
    reason: str


@dataclass(frozen=True)
class StockDecision:
    """Decisión de stock a NIVEL DE GRUPO. ``delta`` (``canonical_delta``) = el ÚNICO
    entero que T5 aplica incremental al canónico (``stock_units`` y ``current_qty``);
    ``None`` si ``kind == REVIEW``. Ya total y sumable-safe: NO se suman deltas por
    duplicado."""

    kind: str
    delta: int | None
    reason: str


@dataclass
class DedupGroup:
    """Un grupo detectado. Los MERGE no-review generan ``DataRepairItem``; los
    review (identidad o stock) y los WEAK_REVIEW van solo a CSV/details_json."""

    group_id: str
    kind: str
    member_ids: list[uuid.UUID]
    edges: list[Edge]
    canonical_id: uuid.UUID | None = None
    requires_review: bool = False
    review_reasons: list[str] = field(default_factory=list)
    # UNA sola decisión de stock por GRUPO (canonical_delta), no una por duplicado.
    stock_decision: StockDecision | None = None
    fingerprint: str | None = None

    @property
    def is_mergeable(self) -> bool:
        """MERGE sin review — el ÚNICO tipo que genera items de mutación en T5."""
        return self.kind == GROUP_MERGE and not self.requires_review


@dataclass
class DedupPlan:
    """Salida de ``plan_dedup``: grupos + cobertura (nunca silenciar el desglose)."""

    groups: list[DedupGroup]
    records: dict[uuid.UUID, ProductRecord]

    @property
    def merge_groups(self) -> list[DedupGroup]:
        return [g for g in self.groups if g.kind == GROUP_MERGE]

    @property
    def mergeable_groups(self) -> list[DedupGroup]:
        return [g for g in self.groups if g.is_mergeable]

    @property
    def review_groups(self) -> list[DedupGroup]:
        return [g for g in self.groups if not g.is_mergeable]

    def coverage(self) -> dict[str, Any]:
        """Contadores de cobertura para el reporte (detectados/mergeables/review)."""
        reason_counter: dict[str, int] = defaultdict(int)
        for g in self.review_groups:
            for reason in g.review_reasons or [g.kind]:
                reason_counter[reason] += 1
        return {
            "groups_detected": len(self.groups),
            "merge_groups": len(self.merge_groups),
            "mergeable_groups": len(self.mergeable_groups),
            "review_groups": len(self.review_groups),
            "review_by_reason": dict(sorted(reason_counter.items())),
            "products_in_groups": sum(len(g.member_ids) for g in self.groups),
        }


# ── Detección: aristas + componentes (funciones puras) ───────────────────────────


def _edges_for_bucket(
    ids: list[uuid.UUID], kind: str, reason: str
) -> list[Edge]:
    """Estrella determinística ids[0]→ids[1..] (O(n) aristas conexas por bucket)."""
    if len(ids) < 2:
        return []
    anchor = ids[0]
    return [Edge(anchor, other, kind, reason) for other in ids[1:]]


def build_edges(indexes: ProductIdentityIndexes) -> list[Edge]:
    """Clasifica todas las aristas del tenant desde los índices F2.

    FUERTE = mismo ``barcode_normalized``; MEDIO = mismo ``sku_normalized``;
    DÉBIL = mismo ``(name_normalized, brand_normalized)``. Solo buckets con ≥2 ids.
    Orden determinístico (los índices vienen ordenados por ``created_at, id``).
    """
    edges: list[Edge] = []
    for key, ids in indexes.by_barcode.items():
        edges.extend(_edges_for_bucket(ids, EDGE_STRONG, f"barcode:{key}"))
    for key, ids in indexes.by_sku.items():
        edges.extend(_edges_for_bucket(ids, EDGE_MEDIUM, f"sku:{key}"))
    for (name, brand), ids in indexes.by_name_brand.items():
        edges.extend(_edges_for_bucket(ids, EDGE_WEAK, f"name+brand:{name}|{brand}"))
    return edges


class _UnionFind:
    """Union-find minimalista (path compression + union by size)."""

    def __init__(self) -> None:
        self._parent: dict[uuid.UUID, uuid.UUID] = {}
        self._size: dict[uuid.UUID, int] = {}

    def __contains__(self, x: uuid.UUID) -> bool:
        return x in self._parent

    def add(self, x: uuid.UUID) -> None:
        if x not in self._parent:
            self._parent[x] = x
            self._size[x] = 1

    def find(self, x: uuid.UUID) -> uuid.UUID:
        self.add(x)
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: uuid.UUID, b: uuid.UUID) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self._size[ra] < self._size[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        self._size[ra] += self._size[rb]


def _group_id_for(ids: list[uuid.UUID]) -> str:
    """Id de grupo determinístico y estable: el menor uuid del grupo (disjuntos)."""
    return str(min(ids))


def build_groups(edges: list[Edge]) -> list[DedupGroup]:
    """Componentes conexos de merge (SOLO fuerte+medio) + grupos débil-solo.

    - Los MERGE salen de fuerte+medio (la arista débil NO propaga): así
      ``A—nombre—B—sku—C`` NO fusiona A+C.
    - Una arista DÉBIL cuyos extremos NO quedaron en el mismo componente de merge
      es una colisión débil-sola → se agrupa (por componentes sobre débiles) en un
      ``WEAK_REVIEW`` (candidato a revisión, NO se fusiona).
    - Grupos de tamaño 1 se descartan.
    """
    merge_uf = _UnionFind()
    for e in edges:
        if e.kind in _MERGE_EDGE_KINDS:
            merge_uf.union(e.from_id, e.to_id)

    def merge_root(x: uuid.UUID) -> uuid.UUID:
        return merge_uf.find(x) if x in merge_uf else x

    # Componentes de merge (fuerte+medio).
    merge_members: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    seen: set[uuid.UUID] = set()
    for e in edges:
        if e.kind not in _MERGE_EDGE_KINDS:
            continue
        for node in (e.from_id, e.to_id):
            if node not in seen:
                seen.add(node)
                merge_members[merge_uf.find(node)].append(node)

    # Ids que YA quedan en un grupo de merge (size≥2): se excluyen de weak-review para
    # que un producto no cuente en dos grupos a la vez (MINOR 4). Todo nodo tocado por
    # una arista fuerte/medio queda en un componente de size≥2 (la arista une dos).
    merged_ids = {nid for members in merge_members.values() for nid in members if len(members) >= 2}

    # Aristas débiles "no unidas" por fuerte+medio (extremos en componentes distintos)
    # y que NO tocan un id ya mergeado (si no, ese id se doble-contaría).
    weak_leftover = [
        e
        for e in edges
        if e.kind == EDGE_WEAK
        and merge_root(e.from_id) != merge_root(e.to_id)
        and e.from_id not in merged_ids
        and e.to_id not in merged_ids
    ]
    weak_uf = _UnionFind()
    for e in weak_leftover:
        weak_uf.union(e.from_id, e.to_id)
    weak_members: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    weak_seen: set[uuid.UUID] = set()
    for e in weak_leftover:
        for node in (e.from_id, e.to_id):
            if node not in weak_seen:
                weak_seen.add(node)
                weak_members[weak_uf.find(node)].append(node)

    groups: list[DedupGroup] = []

    for members in merge_members.values():
        if len(members) < 2:
            continue
        member_set = set(members)
        internal = [
            e for e in edges if e.from_id in member_set and e.to_id in member_set
        ]
        groups.append(
            DedupGroup(
                group_id=_group_id_for(members),
                kind=GROUP_MERGE,
                member_ids=sorted(members, key=str),
                edges=internal,
            )
        )

    for members in weak_members.values():
        if len(members) < 2:
            continue
        member_set = set(members)
        internal = [e for e in weak_leftover if e.from_id in member_set and e.to_id in member_set]
        groups.append(
            DedupGroup(
                group_id=_group_id_for(members),
                kind=GROUP_WEAK_REVIEW,
                member_ids=sorted(members, key=str),
                edges=internal,
                requires_review=True,
                review_reasons=["WEAK_NAME_BRAND_COLLISION"],
            )
        )

    groups.sort(key=lambda g: g.group_id)
    return groups


# ── Canónico (determinístico, función pura) ──────────────────────────────────────


def choose_canonical(records: list[ProductRecord]) -> uuid.UUID:
    """Elige el producto canónico del grupo por el orden COMPLETO:

    ``has_user_edits`` DESC → ``barcode`` NOT NULL DESC → ``sku`` NOT NULL DESC →
    ``#FKs`` DESC → ``created_at`` ASC → ``id`` ASC.

    Cada criterio decide dentro de su nivel; el ``id`` ASC final rompe todo empate.
    """
    if not records:
        raise ValueError("choose_canonical requiere al menos un ProductRecord")

    def sort_key(r: ProductRecord) -> tuple[Any, ...]:
        return (
            not r.has_user_edits,               # has_user_edits DESC (True primero)
            r.barcode_normalized is None,        # barcode NOT NULL DESC
            r.sku_normalized is None,            # sku NOT NULL DESC
            -r.fk_count,                         # #FKs DESC
            r.created_at,                        # created_at ASC
            str(r.id),                           # id ASC (desempate final)
        )

    return min(records, key=sort_key).id


# ── Conflicto de identidad → requires_review (función pura) ──────────────────────


def classify_identity_conflict(records: list[ProductRecord]) -> tuple[bool, list[str]]:
    """¿El grupo va a review por identidad ambigua? (``DECISION_VERSION = 1``).

    - ≥2 ``barcode_normalized`` válidos DISTINTOS → review (identidades fuertes en
      conflicto; ni siquiera un SKU compartido las reconcilia).
    - ≥2 ``sku_normalized`` DISTINTOS → review, SALVO la EXCEPCIÓN de barcode
      fuerte: si el grupo tiene exactamente UN barcode válido distinto y **TODOS**
      los miembros con SKU lo comparten (cada miembro con SKU divergente tiene ese
      barcode fuerte), ese barcode puentea la divergencia (dos etiquetas de SKU para
      el mismo código de barras físico) → NO es review. Si algún miembro con SKU NO
      lleva el barcode, no está puenteado → review.
    - Costo divergente: ≥2 ``unit_cost_ars`` NO nulos DISTINTOS (igualdad exacta de
      ``Decimal``) → review. ``NULL`` vs valor = completable (NO divergencia).
    """
    reasons: list[str] = []

    barcodes = [r.barcode_normalized for r in records if r.barcode_normalized]
    distinct_barcodes = set(barcodes)
    if len(distinct_barcodes) >= 2:
        reasons.append(REVIEW_BARCODE_DIVERGENCE)

    members_with_sku = [r for r in records if r.sku_normalized]
    distinct_skus = {r.sku_normalized for r in members_with_sku}
    if len(distinct_skus) >= 2:
        # Excepción: un único barcode fuerte que comparten TODOS los miembros con SKU
        # (cada miembro con SKU divergente debe llevar ese barcode) puentea.
        bridged_by_barcode = (
            len(distinct_barcodes) == 1
            and len(members_with_sku) >= 2
            and all(r.barcode_normalized for r in members_with_sku)
        )
        if not bridged_by_barcode:
            reasons.append(REVIEW_SKU_DIVERGENCE)

    distinct_costs = {r.unit_cost_ars for r in records if r.unit_cost_ars is not None}
    if len(distinct_costs) >= 2:
        reasons.append(REVIEW_COST_DIVERGENCE)

    return (len(reasons) > 0, reasons)


# ── Decisión de stock por procedencia (función pura) ─────────────────────────────


def _movement_time(m: MovementRow) -> datetime:
    """COALESCE(occurred_at, created_at) — invariante 2d (nunca solo created_at)."""
    when = m.occurred_at if m.occurred_at is not None else m.created_at
    # Normaliza a UTC-aware para comparar naive (tests) y aware (DB) sin romper.
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def _anchor_sort_key(m: MovementRow) -> tuple[datetime, int]:
    """Orden TOTAL para elegir el ancla catálogo más reciente de forma
    determinística: (tiempo, qty). Ante empate de tiempo gana la mayor qty; y si
    también empata la qty, el ``delta`` resultante es idéntico (solo se usa ``.qty``),
    así ``max`` no depende del orden de la lista (MINOR 2 del review)."""
    return (_movement_time(m), m.qty)


def classify_group_stock_decision(
    canonical: ProductRecord, duplicates: list[ProductRecord]
) -> StockDecision:
    """Decisión de stock a NIVEL DE GRUPO: UN solo ``canonical_delta`` sumable-safe.

    Recibe el canónico + TODOS los duplicados del grupo y devuelve la ÚNICA
    ``StockDecision`` que T5 aplica incremental al canónico (``stock_units`` Y
    ``current_qty``). NUNCA recomputa el saldo desde el ledger (invariante 2d).

    Para cada duplicado descompone (sin recalcular el saldo):

        ledger_identificable = Σ qty de movimientos VIVOS con ``source_type``
                               reconocido (SOURCE_TYPES).
        residuo_no_ledger    = duplicado.stock_units − ledger_identificable

    Semántica group-level (evita el sobre-conteo de la decisión pairwise):
    - CUALQUIER duplicado con ``reserved_qty`` ≠ 0 o ``residuo_no_ledger`` ≠ 0 no
      atribuible → GRUPO REVIEW (delta=None, conservador: no perder ni doble-contar).
    - Un mismo duplicado con anclas Y no-anclas → REVIEW (procedencia no atribuible).
    - MEZCLAR semántica de catálogo (MOST_RECENT) con compras (SUM) en el MISMO
      grupo → REVIEW (ambiguo).
    - Toda la evidencia no-canónica es catálogo (``catalog_initial_stock``):
      MOST_RECENT — ``target`` = qty del ancla catálogo MÁS RECIENTE de TODO el grupo
      (canónico + duplicados, por COALESCE(occurred_at, created_at));
      ``delta`` = ``target`` − ``canonical.stock_units`` (puede ser 0 o negativo).
    - Compras distintas (SUM): ``delta`` = Σ qty vivos de los duplicados dedup-eados
      por ``source_row_hash`` a nivel de TODO el grupo (una fila compartida por varios
      miembros —canónico o duplicados hermanos— cuenta UNA sola vez).
    - Sin ledger vivo en ningún duplicado (stock 0) → KEEP_ONE (delta=0).

    El mismo ``delta`` aplica a ``stock_units`` Y ``current_qty`` en T5.
    """
    # Orden determinístico de los duplicados (dedup global de source_row_hash estable).
    ordered = sorted(duplicates, key=lambda r: str(r.id))

    dup_live_by_id: dict[uuid.UUID, list[MovementRow]] = {}
    for dup in ordered:
        if (dup.reserved_qty or 0) != 0:
            return StockDecision(STOCK_REVIEW, None, STOCK_REASON_RESERVED)
        dup_live = [m for m in dup.movements if m.source_type in SOURCE_TYPES]
        residuo_no_ledger = dup.stock_units - sum(m.qty for m in dup_live)
        if residuo_no_ledger != 0:
            return StockDecision(STOCK_REVIEW, None, STOCK_REASON_RESIDUO)
        has_anchor = any(m.source_type == SOURCE_CATALOG_INITIAL_STOCK for m in dup_live)
        has_non_anchor = any(m.source_type != SOURCE_CATALOG_INITIAL_STOCK for m in dup_live)
        if has_anchor and has_non_anchor:
            # Mezcla dentro de un mismo duplicado → no atribuible limpio.
            return StockDecision(STOCK_REVIEW, None, STOCK_REASON_MIXED)
        dup_live_by_id[dup.id] = dup_live

    group_has_catalog = any(
        m.source_type == SOURCE_CATALOG_INITIAL_STOCK
        for movs in dup_live_by_id.values()
        for m in movs
    )
    group_has_purchase = any(
        m.source_type != SOURCE_CATALOG_INITIAL_STOCK
        for movs in dup_live_by_id.values()
        for m in movs
    )

    # Mezclar catálogo (MOST_RECENT) con compras (SUM) en el mismo grupo → ambiguo.
    if group_has_catalog and group_has_purchase:
        return StockDecision(STOCK_REVIEW, None, STOCK_REASON_MIXED)

    # Catálogo puro → el canónico toma el ancla MÁS RECIENTE de TODO el grupo.
    if group_has_catalog:
        all_anchors = [
            m for m in canonical.movements if m.source_type == SOURCE_CATALOG_INITIAL_STOCK
        ]
        for movs in dup_live_by_id.values():
            all_anchors.extend(
                m for m in movs if m.source_type == SOURCE_CATALOG_INITIAL_STOCK
            )
        target = max(all_anchors, key=_anchor_sort_key).qty
        return StockDecision(
            STOCK_MOST_RECENT, target - canonical.stock_units, STOCK_REASON_MOST_RECENT
        )

    # Compras puras → SUM con dedup GLOBAL de source_row_hash (canónico + hermanos).
    if group_has_purchase:
        seen_hashes = {m.source_row_hash for m in canonical.movements if m.source_row_hash}
        total = 0
        for dup in ordered:
            for m in dup_live_by_id[dup.id]:
                if m.source_row_hash and m.source_row_hash in seen_hashes:
                    continue  # fila ya contada por el canónico o un hermano previo.
                if m.source_row_hash:
                    seen_hashes.add(m.source_row_hash)
                total += m.qty
        if total == 0:
            # Todo compartido con el canónico (misma fila importada 2×) → no sumar.
            return StockDecision(STOCK_KEEP_ONE, 0, STOCK_REASON_KEEP_ONE)
        return StockDecision(STOCK_SUM, total, STOCK_REASON_SUM)

    # Sin ledger vivo en ningún duplicado → stock 0, nada que sumar.
    return StockDecision(STOCK_KEEP_ONE, 0, STOCK_REASON_EMPTY)


def classify_stock_decision(
    duplicate: ProductRecord, canonical: ProductRecord
) -> StockDecision:
    """Envoltorio del caso de UN solo duplicado — delega en el motor group-level
    (``classify_group_stock_decision(canonical, [duplicate])``). Se mantiene para
    testear un duplicado aislado; el plan real SIEMPRE decide a nivel de grupo."""
    return classify_group_stock_decision(canonical, [duplicate])


# ── Fingerprint por grupo (función pura) ─────────────────────────────────────────


def compute_group_fingerprint(
    records: list[ProductRecord],
    canonical_id: uuid.UUID,
    stock_decision: StockDecision | None,
    decision_version: int = DECISION_VERSION,
) -> str:
    """sha256 determinístico del estado ACTUAL relevante del grupo.

    Cierra el hueco dry-run↔apply: T5 recomputa este hash justo antes de aplicar y
    aborta el grupo si cambió (alguien tocó un producto/movimiento/balance entre el
    plan y la ejecución). Incluye: ids ordenados, canónico, ``#FKs`` por producto,
    ``stock_units``/balance (``current_qty``/``reserved_qty``) por producto, los
    ``source_row_hash`` de los movimientos-evidencia, la ÚNICA decisión de stock
    group-level (kind+delta) y ``DECISION_VERSION``. NO usa ``datetime.now()``/
    ``random`` (romperían el determinismo).
    """
    products_payload = [
        {
            "id": str(r.id),
            "fk_count": r.fk_count,
            "stock_units": r.stock_units,
            "current_qty": r.current_qty,
            "reserved_qty": r.reserved_qty,
            "movement_hashes": sorted(
                m.source_row_hash for m in r.movements if m.source_row_hash
            ),
        }
        for r in sorted(records, key=lambda r: str(r.id))
    ]
    decision_payload = (
        {"kind": stock_decision.kind, "delta": stock_decision.delta}
        if stock_decision is not None
        else None
    )
    payload = {
        "decision_version": decision_version,
        "canonical_id": str(canonical_id),
        "products": products_payload,
        "stock_decision": decision_payload,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ── Lectura de la DB (nunca muta datos de negocio) ───────────────────────────────


async def count_active_products_missing_identity(
    session: AsyncSession, tenant_id: uuid.UUID
) -> int:
    """Cuenta productos ACTIVOS con ``name_normalized IS NULL`` (falta el backfill
    ``20260731_0002``). > 0 ⇒ el run del tenant debe abortar (precondición)."""
    from sqlalchemy import func, select  # noqa: PLC0415

    from app.persistence.models.product import Product  # noqa: PLC0415

    result = await session.execute(
        select(func.count())
        .select_from(Product)
        .where(
            Product.tenant_id == tenant_id,
            Product.is_active.is_(True),
            Product.name_normalized.is_(None),
        )
    )
    return int(result.scalar_one())


async def load_product_records(
    session: AsyncSession, tenant_id: uuid.UUID, ids: set[uuid.UUID]
) -> dict[uuid.UUID, ProductRecord]:
    """Carga el estado ACTUAL (solo lectura) de los productos ``ids`` del tenant:
    campos de identidad/stock, balance, movimientos VIVOS y ``#FKs``.

    ``#FKs`` = filas que referencian el producto en ``sales_entries`` +
    ``expense_entries`` + ``inventory_movements`` + ``inventory_balances`` (todas,
    incluidas voided: son FKs que T5 igual re-apunta). Nunca escribe.
    """
    if not ids:
        return {}

    from sqlalchemy import select  # noqa: PLC0415

    from app.persistence.models.inventory import (  # noqa: PLC0415
        InventoryBalance,
        InventoryMovement,
    )
    from app.persistence.models.product import Product  # noqa: PLC0415

    id_list = list(ids)

    prod_rows = (
        await session.execute(
            select(
                Product.id,
                Product.has_user_edits,
                Product.barcode_normalized,
                Product.sku_normalized,
                Product.created_at,
                Product.unit_cost_ars,
                Product.stock_units,
            ).where(Product.tenant_id == tenant_id, Product.id.in_(id_list))
        )
    ).all()

    # Balances (un balance por producto).
    balances: dict[uuid.UUID, tuple[int, int]] = {}
    for pid, cur, res in (
        await session.execute(
            select(
                InventoryBalance.product_id,
                InventoryBalance.current_qty,
                InventoryBalance.reserved_qty,
            ).where(
                InventoryBalance.tenant_id == tenant_id,
                InventoryBalance.product_id.in_(id_list),
            )
        )
    ).all():
        balances[pid] = (cur, res)

    # Movimientos VIVOS (voided_at IS NULL).
    movements: dict[uuid.UUID, list[MovementRow]] = defaultdict(list)
    for pid, qty, mtype, stype, srh, occ, cre in (
        await session.execute(
            select(
                InventoryMovement.product_id,
                InventoryMovement.qty,
                InventoryMovement.movement_type,
                InventoryMovement.source_type,
                InventoryMovement.source_row_hash,
                InventoryMovement.occurred_at,
                InventoryMovement.created_at,
            )
            .where(
                InventoryMovement.tenant_id == tenant_id,
                InventoryMovement.product_id.in_(id_list),
                InventoryMovement.voided_at.is_(None),
            )
            # Orden determinístico (MINOR 2): la selección del ancla más reciente y el
            # fingerprint no pueden depender del orden físico de las filas.
            .order_by(
                InventoryMovement.occurred_at,
                InventoryMovement.created_at,
                InventoryMovement.id,
            )
        )
    ).all():
        movements[pid].append(
            MovementRow(
                qty=qty,
                movement_type=mtype,
                source_type=stype,
                source_row_hash=srh,
                occurred_at=occ,
                created_at=cre,
            )
        )

    fk_counts = await _load_fk_counts(session, tenant_id, id_list)

    records: dict[uuid.UUID, ProductRecord] = {}
    for (pid, edits, bc_n, sku_n, created, cost, stock) in prod_rows:
        cur_res = balances.get(pid)
        records[pid] = ProductRecord(
            id=pid,
            has_user_edits=bool(edits),
            barcode_normalized=bc_n,
            sku_normalized=sku_n,
            created_at=created,
            unit_cost_ars=cost,
            stock_units=int(stock),
            fk_count=fk_counts.get(pid, 0),
            current_qty=cur_res[0] if cur_res else None,
            reserved_qty=cur_res[1] if cur_res else None,
            movements=tuple(movements.get(pid, ())),
        )
    return records


async def _load_fk_counts(
    session: AsyncSession, tenant_id: uuid.UUID, id_list: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Σ de filas que referencian cada producto en las 4 tablas de FK."""
    from sqlalchemy import func, select  # noqa: PLC0415

    from app.persistence.models.inventory import (  # noqa: PLC0415
        InventoryBalance,
        InventoryMovement,
    )
    from app.persistence.models.transaction import ExpenseEntry, SaleEntry  # noqa: PLC0415

    counts: dict[uuid.UUID, int] = defaultdict(int)
    for model in (SaleEntry, ExpenseEntry, InventoryMovement, InventoryBalance):
        rows = (
            await session.execute(
                select(model.product_id, func.count())
                .where(model.tenant_id == tenant_id, model.product_id.in_(id_list))
                .group_by(model.product_id)
            )
        ).all()
        for pid, n in rows:
            if pid is not None:
                counts[pid] += int(n)
    return dict(counts)


async def _count_fk_3t(
    session: AsyncSession, tenant_id: uuid.UUID, product_id: uuid.UUID
) -> int:
    """#FKs de un producto en las 3 tablas que el REPOINT_FK re-apunta (ventas +
    gastos + movimientos). Excluye ``inventory_balances`` a propósito: el balance lo
    manejan CONSOLIDATE/DELETE, no el REPOINT, y contarlo enturbiaría la reconstrucción
    del baseline (el apply puede CREAR el balance del canónico). T5 lo snapshotea antes
    del REPOINT (baseline); T6 lo revalida (baseline + filas re-apuntadas)."""
    from sqlalchemy import func, select  # noqa: PLC0415

    from app.persistence.models.inventory import InventoryMovement  # noqa: PLC0415
    from app.persistence.models.transaction import ExpenseEntry, SaleEntry  # noqa: PLC0415

    total = 0
    for model in (SaleEntry, ExpenseEntry, InventoryMovement):
        n = (
            await session.execute(
                select(func.count())
                .select_from(model)
                .where(model.tenant_id == tenant_id, model.product_id == product_id)
            )
        ).scalar_one()
        total += int(n)
    return total


# ── Orquestación: plan (lectura, sin persistir) ──────────────────────────────────


async def plan_dedup(session: AsyncSession, tenant_id: uuid.UUID) -> DedupPlan:
    """Detecta grupos, elige canónico, clasifica identidad/stock y calcula el
    fingerprint por grupo. SOLO lee la DB (no persiste, no muta)."""
    from app.application.services.ingestion_import_service import (  # noqa: PLC0415
        _load_product_identity_indexes,
    )

    indexes = await _load_product_identity_indexes(session, tenant_id)
    edges = build_edges(indexes)
    groups = build_groups(edges)

    candidate_ids: set[uuid.UUID] = set()
    for g in groups:
        candidate_ids.update(g.member_ids)
    records = await load_product_records(session, tenant_id, candidate_ids)

    for group in groups:
        if group.kind != GROUP_MERGE:
            continue
        group_records = [records[pid] for pid in group.member_ids if pid in records]
        if len(group_records) < 2:
            # Defensa: un miembro desapareció entre índice y carga → a review.
            group.requires_review = True
            group.review_reasons.append("MEMBER_VANISHED")
            continue

        canonical_id = choose_canonical(group_records)
        group.canonical_id = canonical_id
        canonical = records[canonical_id]

        identity_review, identity_reasons = classify_identity_conflict(group_records)
        group.review_reasons.extend(identity_reasons)

        duplicates = [r for r in group_records if r.id != canonical_id]
        decision = classify_group_stock_decision(canonical, duplicates)
        group.stock_decision = decision
        if decision.kind == STOCK_REVIEW:
            group.review_reasons.append(f"stock:{decision.reason}")

        group.requires_review = identity_review or decision.kind == STOCK_REVIEW
        group.fingerprint = compute_group_fingerprint(
            group_records, canonical_id, decision
        )

    return DedupPlan(groups=groups, records=records)


# ── Persistencia del PLAN (audit/plan, NUNCA datos de negocio) ───────────────────


def _plan_block(group: DedupGroup) -> dict[str, Any]:
    """Bloque común de traza (va dentro de before/after de cada item)."""
    return {
        "group_id": group.group_id,
        "canonical_product_id": str(group.canonical_id) if group.canonical_id else None,
        "decision_version": DECISION_VERSION,
        "fingerprint": group.fingerprint,
    }


async def _build_repoint_items(
    session: AsyncSession,
    run_id: uuid.UUID,
    tenant_id: uuid.UUID,
    group: DedupGroup,
    duplicate_id: uuid.UUID,
) -> list[Any]:
    """REPOINT_FK chunked (lotes determinísticos de ``REPOINT_CHUNK_SIZE``) por
    tabla de FK con filas del duplicado. Solo LEE los ids a re-apuntar."""
    from sqlalchemy import select  # noqa: PLC0415

    from app.persistence.models.inventory import InventoryMovement  # noqa: PLC0415
    from app.persistence.models.repair import DataRepairItem  # noqa: PLC0415
    from app.persistence.models.transaction import ExpenseEntry, SaleEntry  # noqa: PLC0415

    canonical_id = group.canonical_id
    items: list[Any] = []
    tables = (
        ("sales_entries", SaleEntry),
        ("expense_entries", ExpenseEntry),
        ("inventory_movements", InventoryMovement),
    )
    for table_name, model in tables:
        row_ids = [
            rid
            for (rid,) in (
                await session.execute(
                    select(model.id)
                    .where(model.tenant_id == tenant_id, model.product_id == duplicate_id)
                    .order_by(model.id.asc())
                )
            ).all()
        ]
        for chunk_index in range(0, len(row_ids), REPOINT_CHUNK_SIZE):
            chunk = row_ids[chunk_index : chunk_index + REPOINT_CHUNK_SIZE]
            items.append(
                DataRepairItem(
                    run_id=run_id,
                    tenant_id=tenant_id,
                    product_id=duplicate_id,
                    action="REPOINT_FK",
                    before_json={
                        "plan": _plan_block(group),
                        "table": table_name,
                        "chunk_index": chunk_index // REPOINT_CHUNK_SIZE,
                        "rows": [
                            {"id": str(rid), "old_product_id": str(duplicate_id)}
                            for rid in chunk
                        ],
                    },
                    after_json={
                        "table": table_name,
                        "chunk_index": chunk_index // REPOINT_CHUNK_SIZE,
                        "new_product_id": str(canonical_id),
                    },
                    confidence="HIGH",
                )
            )
    return items


async def persist_dedup_plan(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    plan: DedupPlan,
    *,
    triggered_by: str = "script:dedupe_products_by_name",
) -> uuid.UUID:
    """Persiste el PLAN (dry-run): ``DataRepairRun`` + ``DataRepairItem`` por grupo
    MERGEABLE + ``decision_audit_log``. NO toca Product/SaleEntry/ExpenseEntry/
    InventoryBalance/InventoryMovement. Devuelve el ``run_id``. El caller commitea."""
    from app.persistence.models.audit import DecisionAuditLog  # noqa: PLC0415
    from app.persistence.models.repair import DataRepairItem, DataRepairRun  # noqa: PLC0415

    now = datetime.now(UTC)
    coverage = plan.coverage()

    run = DataRepairRun(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        repair_type=_REPAIR_TYPE,
        status="COMPLETED",
        dry_run=True,
        candidates_found=coverage["groups_detected"],
        products_detected=coverage["products_in_groups"],
        details_json={
            "decision_version": DECISION_VERSION,
            "coverage": coverage,
            "review_groups": [
                {
                    "group_id": g.group_id,
                    "kind": g.kind,
                    "canonical_product_id": str(g.canonical_id) if g.canonical_id else None,
                    "member_ids": [str(m) for m in g.member_ids],
                    "review_reasons": g.review_reasons,
                    "fingerprint": g.fingerprint,
                }
                for g in plan.review_groups
            ],
        },
        created_at=now,
        completed_at=now,
    )
    session.add(run)
    await session.flush()

    products_skipped = 0
    for group in plan.groups:
        if not group.is_mergeable or group.canonical_id is None:
            products_skipped += len(group.member_ids)
            continue

        decision = group.stock_decision
        # UN solo delta group-level que T5 aplica al canónico (stock_units Y current_qty).
        stock_decision_json = (
            {"kind": decision.kind, "delta": decision.delta, "reason": decision.reason}
            if decision is not None
            else None
        )
        session.add(
            DataRepairItem(
                run_id=run.id,
                tenant_id=tenant_id,
                product_id=group.canonical_id,
                action="MERGE_PRODUCT",
                before_json={
                    "plan": _plan_block(group),
                    "duplicate_ids": [
                        str(m) for m in group.member_ids if m != group.canonical_id
                    ],
                    # UNA decisión por GRUPO (canonical_delta), no una por duplicado.
                    "stock_decision": stock_decision_json,
                },
                after_json={"plan": _plan_block(group)},
                confidence="HIGH",
            )
        )

        for dup_id in group.member_ids:
            if dup_id == group.canonical_id:
                continue
            dup = plan.records.get(dup_id)

            # REPOINT_FK chunked (sales / expenses / movements).
            for item in await _build_repoint_items(
                session, run.id, tenant_id, group, dup_id
            ):
                session.add(item)

            # Balance: consolidar + borrar el del duplicado. El delta de stock es
            # group-level (viaja en MERGE_PRODUCT) — acá NO hay delta por duplicado.
            if dup is not None and dup.current_qty is not None:
                session.add(
                    DataRepairItem(
                        run_id=run.id,
                        tenant_id=tenant_id,
                        product_id=group.canonical_id,
                        action="CONSOLIDATE_BALANCE",
                        before_json={
                            "plan": _plan_block(group),
                            "duplicate_id": str(dup_id),
                            "duplicate_current_qty": dup.current_qty,
                        },
                        after_json={"plan": _plan_block(group)},
                        confidence="HIGH",
                    )
                )
                session.add(
                    DataRepairItem(
                        run_id=run.id,
                        tenant_id=tenant_id,
                        product_id=dup_id,
                        action="DELETE_BALANCE",
                        before_json={
                            "plan": _plan_block(group),
                            "current_qty": dup.current_qty,
                            "reserved_qty": dup.reserved_qty,
                        },
                        after_json={"plan": _plan_block(group)},
                        confidence="HIGH",
                    )
                )

            # Desactivar el duplicado (soft-delete).
            session.add(
                DataRepairItem(
                    run_id=run.id,
                    tenant_id=tenant_id,
                    product_id=dup_id,
                    action="DEACTIVATE_DUPLICATE",
                    before_json={
                        "plan": _plan_block(group),
                        "is_active": True,
                        "deactivation_reason": None,
                    },
                    after_json={
                        "plan": _plan_block(group),
                        "is_active": False,
                        "deactivation_reason": "DUPLICATE",
                    },
                    confidence="HIGH",
                )
            )

    run.products_skipped = products_skipped

    session.add(
        DecisionAuditLog(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            decision_type=_DECISION_TYPE,
            decision_data={
                "run_id": str(run.id),
                "dry_run": True,
                "coverage": coverage,
                "decision_version": DECISION_VERSION,
            },
            triggered_by=triggered_by,
            created_at=now,
        )
    )
    await session.flush()
    return run.id


# ── EJECUCIÓN (--apply): mutaciones reales del plan (F3-T5) ───────────────────────
#
# Consume los ``DataRepairItem`` del run dry-run de T4 (fuente de verdad de las
# formas), revalida el fingerprint por grupo desde el estado ACTUAL de la DB y
# ejecuta las mutaciones en su PROPIA transacción por grupo (no una gigante). El
# delta de stock group-level (``MERGE_PRODUCT.before_json["stock_decision"]["delta"]``)
# se aplica UNA vez a ``stock_units`` Y a ``current_qty`` del canónico. NUNCA
# recomputa el saldo desde el ledger (invariante 2d). El revert es F3-T6.

# Campos que el MERGE completa en el canónico SOLO si están NULL/"" — completa desde
# los duplicados (orden determinístico por str(id), gana el primero no vacío). El
# listener before_update de Product re-sincroniza ``*_normalized`` al setear barcode/sku.
_MERGE_COMPLETABLE_FIELDS = (
    "unit_cost_ars",
    "category",
    "low_stock_threshold_units",
    "acquired_at",
    "barcode",
    "sku",
    "description",
    "expiry_date",
)

# Orden seguro de re-apunte de FKs (el mismo que T6 revierte en inverso).
_REPOINT_TABLE_ORDER = ("sales_entries", "expense_entries", "inventory_movements")

_LEASE_LOST_REASON = "lease_lost"
_FINGERPRINT_CHANGED_REASON = "fingerprint_changed"
_BALANCE_INCONSISTENCY_REASON = "balance_inconsistency"


class RepointCountMismatchError(RuntimeError):
    """El ``rowcount`` de un REPOINT_FK no coincidió con el conteo planificado del
    chunk (alguien movió/borró filas de FK) → abortar el grupo (rollback)."""


class LeaseLostError(RuntimeError):
    """El lease de mantenimiento dejó de ser de este run (expiró o lo robaron) →
    no hay exclusión mutua garantizada; abortar el grupo y detener el run."""


@dataclass
class ApplyResult:
    """Resumen de un ``apply_dedup_plan``: estado del run + conteo por grupo."""

    run_id: uuid.UUID
    status: str
    groups_total: int
    groups_applied: int
    groups_skipped: int
    groups_failed: int
    group_results: list[dict[str, Any]] = field(default_factory=list)


def _is_empty(value: Any) -> bool:
    """Un campo es 'completable' si es ``None`` o string vacío/espacios."""
    return value is None or (isinstance(value, str) and value.strip() == "")


def _json_scalar(value: Any) -> Any:
    """Serializa un valor de columna a algo JSON-safe para before/after_json."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


async def _lease_still_ours(
    session: AsyncSession, tenant_id: uuid.UUID, lease_id: uuid.UUID | None
) -> bool:
    """Fencing: ¿el lease vivo del tenant sigue siendo el de este run? (``True`` si
    ``lease_id`` es ``None`` — corridas sin lease, p.ej. tests unitarios directos)."""
    if lease_id is None:
        return True
    from sqlalchemy import func, select  # noqa: PLC0415

    from app.persistence.models.maintenance_lock import TenantMaintenanceLock  # noqa: PLC0415

    row = (
        await session.execute(
            select(TenantMaintenanceLock.lease_id).where(
                TenantMaintenanceLock.tenant_id == tenant_id,
                TenantMaintenanceLock.expires_at > func.now(),
            )
        )
    ).scalar_one_or_none()
    return row == lease_id


def _group_source_items(items: list[Any]) -> dict[str, dict[str, Any]]:
    """Agrupa los ``DataRepairItem`` del source dry-run por ``group_id`` (que viaja en
    ``before_json["plan"]`` de cada item). Extrae canónico, fingerprint, duplicados y
    la decisión de stock del ``MERGE_PRODUCT``; junta los ``REPOINT_FK`` por grupo."""
    grouped: dict[str, dict[str, Any]] = {}
    for it in items:
        before = it.before_json or {}
        plan = before.get("plan") or {}
        gid = plan.get("group_id")
        canon = plan.get("canonical_product_id")
        if gid is None or canon is None:
            continue
        g = grouped.setdefault(
            gid,
            {
                "plan_block": plan,
                "canonical_id": uuid.UUID(canon),
                "fingerprint": plan.get("fingerprint"),
                "merge": None,
                "repoint": [],
                "duplicate_ids": [],
                "stock_decision": None,
            },
        )
        if it.action == "MERGE_PRODUCT":
            g["merge"] = True  # marca de presencia (los datos ya se extrajeron a plano)
            g["duplicate_ids"] = [uuid.UUID(x) for x in before.get("duplicate_ids", [])]
            g["stock_decision"] = before.get("stock_decision")
        elif it.action == "REPOINT_FK":
            # Extraemos a plano AHORA (product_id + before_json): los ORM ``DataRepairItem``
            # del source se EXPIRAN cuando un grupo PREVIO hace ``rollback`` (skip por
            # fingerprint / fallo) → acceder ``it.product_id``/``it.before_json`` en un grupo
            # posterior dispararía un lazy-load sync → ``MissingGreenlet``. Con dicts planos,
            # ``_apply_one_group`` nunca toca el ORM del source tras un rollback intermedio.
            g["repoint"].append({"product_id": it.product_id, "before_json": before})
    return grouped


async def _apply_one_group(
    session: AsyncSession,
    run_id: uuid.UUID,
    tenant_id: uuid.UUID,
    gdata: dict[str, Any],
    *,
    lease_id: uuid.UUID | None,
) -> tuple[str, str]:
    """Aplica las mutaciones de UN grupo (NO commitea — el caller maneja la txn).

    Devuelve ``("applied", kind)`` o ``("skipped", motivo)``. Levanta
    ``RepointCountMismatch`` o ``LeaseLost`` (o cualquier error) → el caller hace
    rollback del grupo. Orden de mutaciones (T6 revierte en inverso): MERGE_PRODUCT
    → REPOINT_FK → CONSOLIDATE_BALANCE → DELETE_BALANCE → DEACTIVATE_DUPLICATE.
    """
    from sqlalchemy import func, select, update  # noqa: PLC0415
    from sqlalchemy.engine import CursorResult  # noqa: PLC0415

    from app.application.services import maintenance_lock_service as mls  # noqa: PLC0415
    from app.domain.product_completion import recompute_requires_completion  # noqa: PLC0415
    from app.persistence.models.inventory import (  # noqa: PLC0415
        InventoryBalance,
        InventoryMovement,
    )
    from app.persistence.models.product import Product  # noqa: PLC0415
    from app.persistence.models.repair import DataRepairItem  # noqa: PLC0415
    from app.persistence.models.transaction import ExpenseEntry, SaleEntry  # noqa: PLC0415

    canonical_id: uuid.UUID = gdata["canonical_id"]
    duplicate_ids: list[uuid.UUID] = gdata["duplicate_ids"]
    planned_fp: str | None = gdata["fingerprint"]
    plan_block: dict[str, Any] = gdata["plan_block"]
    stock_dec: dict[str, Any] = gdata.get("stock_decision") or {}
    delta = int(stock_dec.get("delta") or 0)
    member_ids = [canonical_id, *duplicate_ids]
    ordered_dups = sorted(duplicate_ids, key=str)

    # (a) Barrera real (advisory exclusive; no-op SQLite) + fencing del lease.
    await mls.acquire_maintenance_lock_exclusive(session, tenant_id)
    if not await _lease_still_ours(session, tenant_id, lease_id):
        raise LeaseLostError(f"lease {lease_id} ya no es de este run (tenant {tenant_id})")

    # (b) Revalidar el fingerprint desde el estado ACTUAL: es una guarda de seguridad
    # (cierra el hueco dry-run↔apply) que nunca se saltea.
    records = await load_product_records(session, tenant_id, set(member_ids))
    if canonical_id not in records or len(records) != len(member_ids):
        return ("skipped", _FINGERPRINT_CHANGED_REASON)
    canonical_rec = records[canonical_id]
    dup_recs = [records[d] for d in duplicate_ids if d in records]
    current_decision = classify_group_stock_decision(canonical_rec, dup_recs)
    current_fp = compute_group_fingerprint(list(records.values()), canonical_id, current_decision)
    if current_fp != planned_fp:
        return ("skipped", _FINGERPRINT_CHANGED_REASON)

    # ── (c) MUTACIONES ───────────────────────────────────────────────────────────
    # 1) MERGE_PRODUCT — completa NULLs del canónico desde los duplicados + delta stock.
    canonical_obj = await session.get(Product, canonical_id)
    if canonical_obj is None:  # pragma: no cover — el fingerprint ya lo garantiza
        return ("skipped", _FINGERPRINT_CHANGED_REASON)
    dup_objs = {
        o.id: o
        for o in (
            await session.execute(
                select(Product).where(
                    Product.tenant_id == tenant_id, Product.id.in_(duplicate_ids)
                )
            )
        )
        .scalars()
        .all()
    }

    stock_before = canonical_obj.stock_units
    rc_before = canonical_obj.requires_completion
    prev_cf = dict(canonical_obj.custom_fields or {})
    # Baseline de FKs del canónico (pre-REPOINT) para que T6 detecte actividad posterior:
    # tras el apply el canónico debe tener exactamente `baseline + Σ filas re-apuntadas`.
    canonical_fk_3t_before = await _count_fk_3t(session, tenant_id, canonical_id)

    fields_completed: dict[str, Any] = {}
    for f in _MERGE_COMPLETABLE_FIELDS:
        if not _is_empty(getattr(canonical_obj, f)):
            continue
        for d in ordered_dups:
            dup = dup_objs.get(d)
            if dup is None:
                continue
            val = getattr(dup, f)
            if not _is_empty(val):
                setattr(canonical_obj, f, val)
                fields_completed[f] = _json_scalar(val)
                break

    # custom_fields: shallow merge, canónico gana, excluí claves internas ("_"-prefijo).
    new_cf = dict(prev_cf)
    cf_added: dict[str, Any] = {}
    for d in ordered_dups:
        dup = dup_objs.get(d)
        if dup is None:
            continue
        dcf = dup.custom_fields or {}
        if not isinstance(dcf, dict):
            continue
        for k, v in dcf.items():
            if k.startswith("_") or k in new_cf:
                continue
            new_cf[k] = v
            cf_added[k] = v
    if cf_added:
        canonical_obj.custom_fields = new_cf

    # Delta de stock group-level: UNA vez a stock_units (y más abajo a current_qty).
    canonical_obj.stock_units = stock_before + delta
    recompute_requires_completion(canonical_obj)

    session.add(
        DataRepairItem(
            run_id=run_id,
            tenant_id=tenant_id,
            product_id=canonical_id,
            action="MERGE_PRODUCT",
            before_json={
                "plan": plan_block,
                "duplicate_ids": [str(d) for d in duplicate_ids],
                "stock_decision": stock_dec,
                "canonical_prev": {
                    "stock_units": stock_before,
                    "requires_completion": rc_before,
                    "custom_fields": prev_cf,
                    "fk_count_3t": canonical_fk_3t_before,
                },
            },
            after_json={
                "plan": plan_block,
                "fields_completed": fields_completed,
                "custom_fields_added": cf_added,
                "stock_units_before": stock_before,
                "stock_units_after": canonical_obj.stock_units,
                "stock_delta_applied": delta,
                "requires_completion_after": canonical_obj.requires_completion,
            },
            confidence="HIGH",
        )
    )

    # 2) REPOINT_FK — re-apunta EXACTAMENTE las filas planificadas (id ∈ chunk AND
    # product_id == dup) e incluye activas y anuladas. rowcount ≠ esperado → abortar.
    model_by_table: dict[str, Any] = {
        "sales_entries": SaleEntry,
        "expense_entries": ExpenseEntry,
        "inventory_movements": InventoryMovement,
    }

    def _repoint_sort_key(rp: dict[str, Any]) -> tuple[int, str, int]:
        before = rp["before_json"] or {}
        table = before.get("table", "")
        order = (
            _REPOINT_TABLE_ORDER.index(table) if table in _REPOINT_TABLE_ORDER else len(
                _REPOINT_TABLE_ORDER
            )
        )
        return (order, str(rp["product_id"]), int(before.get("chunk_index", 0)))

    for rp in sorted(gdata["repoint"], key=_repoint_sort_key):
        before = rp["before_json"] or {}
        table = before["table"]
        model = model_by_table[table]
        dup_id = rp["product_id"]
        row_ids = [uuid.UUID(r["id"]) for r in before.get("rows", [])]
        if not row_ids:
            continue
        result = await session.execute(
            update(model)
            .where(
                model.tenant_id == tenant_id,
                model.id.in_(row_ids),
                model.product_id == dup_id,
            )
            .values(product_id=canonical_id)
        )
        rowcount = cast("CursorResult[Any]", result).rowcount
        if rowcount != len(row_ids):
            raise RepointCountMismatchError(
                f"{table} chunk {before.get('chunk_index')} dup {dup_id}: "
                f"esperado {len(row_ids)}, afectó {rowcount}"
            )
        session.add(
            DataRepairItem(
                run_id=run_id,
                tenant_id=tenant_id,
                product_id=dup_id,
                action="REPOINT_FK",
                before_json={
                    "plan": plan_block,
                    "table": table,
                    "chunk_index": before.get("chunk_index"),
                    "rows": [
                        {"id": r["id"], "old_product_id": str(dup_id)} for r in before["rows"]
                    ],
                },
                after_json={
                    "plan": plan_block,
                    "table": table,
                    "chunk_index": before.get("chunk_index"),
                    "new_product_id": str(canonical_id),
                    "rowcount": rowcount,
                },
                confidence="HIGH",
            )
        )

    # 3/4) Balances: consolidar el delta group-level UNA vez en el canónico + borrar
    # los balances de los duplicados (registrando su estado real para la reversa).
    bal_by_pid = {
        b.product_id: b
        for b in (
            await session.execute(
                select(InventoryBalance).where(
                    InventoryBalance.tenant_id == tenant_id,
                    InventoryBalance.product_id.in_(member_ids),
                )
            )
        )
        .scalars()
        .all()
    }
    canon_bal = bal_by_pid.get(canonical_id)
    prev_current = canon_bal.current_qty if canon_bal is not None else None
    if canon_bal is not None:
        # Balance existente: incremental. NUNCA se clampea stock existente (regla dura):
        # incluso si el delta lo dejara negativo, se aplica tal cual (la reversa lo deshace).
        canon_bal.current_qty = canon_bal.current_qty + delta
        new_current: int | None = canon_bal.current_qty
    elif delta < 0:
        # No existía balance (= 0 implícito) y el delta lo dejaría negativo: crear un
        # balance NUEVO con current_qty<0 desde cero es una inconsistencia (no había
        # balance previo que "clampear"). Conservador (no-invention): saltear el grupo →
        # el caller hace rollback de TODO (MERGE/REPOINT incluidos) y el negocio de ese
        # grupo queda intacto. NO es un clamp de stock existente (esto es sólo el caso de
        # CREAR un balance desde cero).
        return ("skipped", _BALANCE_INCONSISTENCY_REASON)
    elif delta != 0:
        canon_bal = InventoryBalance(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            product_id=canonical_id,
            current_qty=delta,
            reserved_qty=0,
        )
        session.add(canon_bal)
        new_current = delta
    else:
        new_current = None
    session.add(
        DataRepairItem(
            run_id=run_id,
            tenant_id=tenant_id,
            product_id=canonical_id,
            action="CONSOLIDATE_BALANCE",
            before_json={
                "plan": plan_block,
                "canonical_current_qty_before": prev_current,
                "delta": delta,
                "duplicate_ids": [str(d) for d in duplicate_ids],
            },
            after_json={"plan": plan_block, "canonical_current_qty_after": new_current},
            confidence="HIGH",
        )
    )
    for d in ordered_dups:
        dbal = bal_by_pid.get(d)
        if dbal is None:
            continue
        session.add(
            DataRepairItem(
                run_id=run_id,
                tenant_id=tenant_id,
                product_id=d,
                action="DELETE_BALANCE",
                before_json={
                    "plan": plan_block,
                    "duplicate_id": str(d),
                    "current_qty": dbal.current_qty,
                    "reserved_qty": dbal.reserved_qty,
                },
                after_json={"plan": plan_block},
                confidence="HIGH",
            )
        )
        await session.delete(dbal)

    # 5) DEACTIVATE_DUPLICATE — soft-delete con reloj de la DB. NO se pone stock en 0
    # (queda como estaba; la reversa reactiva y resta el delta del canónico).
    for d in ordered_dups:
        dup = dup_objs.get(d)
        dup_stock = dup.stock_units if dup is not None else None
        await session.execute(
            update(Product)
            .where(Product.tenant_id == tenant_id, Product.id == d)
            .values(is_active=False, deactivated_at=func.now(), deactivation_reason="DUPLICATE")
        )
        session.add(
            DataRepairItem(
                run_id=run_id,
                tenant_id=tenant_id,
                product_id=d,
                action="DEACTIVATE_DUPLICATE",
                before_json={
                    "plan": plan_block,
                    "is_active": True,
                    "deactivated_at": None,
                    "deactivation_reason": None,
                    "stock_units": dup_stock,
                },
                after_json={
                    "plan": plan_block,
                    "is_active": False,
                    "deactivation_reason": "DUPLICATE",
                },
                confidence="HIGH",
            )
        )

    return ("applied", str(stock_dec.get("kind") or ""))


async def _validate_source_run(
    session: AsyncSession, tenant_id: uuid.UUID, source_run_id: uuid.UUID
) -> Any:
    """Carga y valida el run dry-run source: tipo PRODUCT_DEDUP, dry_run, status
    COMPLETED (no APPLIED — idempotencia) y tenant coincidente. Aborta con
    ``ValueError`` si no cumple."""
    from app.persistence.models.repair import DataRepairRun  # noqa: PLC0415

    source = await session.get(DataRepairRun, source_run_id)
    if source is None:
        raise ValueError(f"source-run-id {source_run_id} no existe")
    if source.repair_type != _REPAIR_TYPE:
        raise ValueError(
            f"source-run-id {source_run_id} no es un run {_REPAIR_TYPE} (es {source.repair_type!r})"
        )
    if not source.dry_run:
        raise ValueError(f"source-run-id {source_run_id} no es un dry-run")
    if source.status != "COMPLETED":
        raise ValueError(
            f"source-run-id {source_run_id} tiene status {source.status!r} "
            f"(se esperaba 'COMPLETED'; ¿ya se aplicó?)"
        )
    if source.tenant_id != tenant_id:
        raise ValueError(
            f"source-run-id {source_run_id} es de otro tenant ({source.tenant_id}), "
            f"no de {tenant_id}"
        )
    return source


async def apply_dedup_plan(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    source_run_id: uuid.UUID,
    *,
    lease_id: uuid.UUID | None = None,
    ttl_seconds: int = 300,
    triggered_by: str = "script:dedupe_products_by_name",
) -> ApplyResult:
    """Ejecuta las mutaciones planificadas por un run dry-run (T4), grupo por grupo,
    cada uno en su PROPIA transacción (commit/rollback por grupo — no una gigante).

    - Revalida el fingerprint de cada grupo contra el estado ACTUAL: si cambió, saltea
      el grupo (``PARTIALLY_APPLIED``) sin mutar.
    - El delta de stock group-level se aplica UNA vez a ``stock_units`` y a
      ``current_qty`` del canónico. NUNCA recomputa desde el ledger.
    - Los ``DataRepairItem`` del run de apply son el REGISTRO real (before/after) que
      T6 revierte en orden inverso.
    - Marca el source dry-run como ``APPLIED`` SOLO si el run terminó full-``APPLIED``.
      Si quedó ``PARTIALLY_APPLIED``/``FAILED``, el source queda en ``COMPLETED`` →
      re-aplicable: re-correr ``--apply`` con el mismo source toma los grupos salteados
      por fingerprint (idempotente; los grupos ya aplicados caen solos por skip).
    """
    from sqlalchemy import select  # noqa: PLC0415

    from app.application.services import maintenance_lock_service as mls  # noqa: PLC0415
    from app.persistence.models.audit import DecisionAuditLog  # noqa: PLC0415
    from app.persistence.models.repair import DataRepairItem, DataRepairRun  # noqa: PLC0415

    await _validate_source_run(session, tenant_id, source_run_id)

    source_items = (
        (
            await session.execute(
                select(DataRepairItem).where(DataRepairItem.run_id == source_run_id)
            )
        )
        .scalars()
        .all()
    )
    grouped = _group_source_items(list(source_items))

    # Run de apply (fuente de verdad para T6). Se commitea ANTES de los grupos: así el
    # rollback de un grupo fallido NO borra el run ni los items de los grupos previos.
    run = DataRepairRun(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        repair_type=_REPAIR_TYPE,
        status="RUNNING",
        dry_run=False,
        source_run_id=source_run_id,
        candidates_found=len(grouped),
        created_at=datetime.now(UTC),
    )
    session.add(run)
    await session.flush()
    run_id = run.id
    await session.commit()

    groups_total = len(grouped)
    applied = skipped = failed = 0
    group_results: list[dict[str, Any]] = []
    lease_lost = False

    for gid in sorted(grouped):
        gdata = grouped[gid]
        if gdata.get("merge") is None:  # pragma: no cover — todo grupo mergeable trae MERGE
            continue
        if lease_id is not None:
            renewed = await mls.renew(session, lease_id, ttl_seconds)
            # Heartbeat en su PROPIA transacción committeada, independiente del ciclo del
            # grupo: si el grupo se saltea/falla y hacemos rollback, la renovación del lease
            # YA quedó persistida → el lease no expira a mitad de un run largo con muchos skips.
            await session.commit()
            if not renewed:
                group_results.append(
                    {"group_id": gid, "status": "failed", "reason": _LEASE_LOST_REASON}
                )
                failed += 1
                lease_lost = True
                break
        try:
            status, reason = await _apply_one_group(
                session, run_id, tenant_id, gdata, lease_id=lease_id
            )
        except LeaseLostError:
            await session.rollback()
            group_results.append(
                {"group_id": gid, "status": "failed", "reason": _LEASE_LOST_REASON}
            )
            failed += 1
            lease_lost = True
            break
        except Exception as exc:  # noqa: BLE001 — cualquier error del grupo → rollback aislado
            await session.rollback()
            group_results.append(
                {"group_id": gid, "status": "failed", "reason": f"{type(exc).__name__}: {exc}"}
            )
            failed += 1
            continue
        if status == "skipped":
            await session.rollback()
            group_results.append({"group_id": gid, "status": "skipped", "reason": reason})
            skipped += 1
        else:
            await session.commit()
            group_results.append({"group_id": gid, "status": "applied", "reason": reason})
            applied += 1

    if applied == groups_total and failed == 0 and skipped == 0:
        final_status = "APPLIED"
    elif applied == 0 and failed > 0 and skipped == 0:
        final_status = "FAILED"
    else:
        final_status = "PARTIALLY_APPLIED"

    # Finalización: re-fetch (los commits/rollbacks intermedios pudieron expirar el ORM).
    apply_run = await session.get(DataRepairRun, run_id)
    if apply_run is not None:
        apply_run.status = final_status
        apply_run.products_updated = applied
        apply_run.products_skipped = skipped
        apply_run.completed_at = datetime.now(UTC)
        apply_run.details_json = {
            "decision_version": DECISION_VERSION,
            "source_run_id": str(source_run_id),
            "groups_total": groups_total,
            "applied": applied,
            "skipped": skipped,
            "failed": failed,
            "lease_lost": lease_lost,
            "groups": group_results,
        }

    # Consume el source dry-run SOLO si el run terminó full-APPLIED. Si quedó
    # PARTIALLY_APPLIED/FAILED, dejamos el source en COMPLETED → re-aplicable: se puede
    # re-correr `--apply --source-run-id <mismo>` para tomar los grupos salteados por
    # fingerprint. El re-apply es idempotente y seguro: los grupos ya aplicados caen
    # solos (su duplicado quedó is_active=False y el fingerprint/#FKs cambió → skip).
    # Contrato para T6: T6 revierte SIEMPRE un run de APPLY, nunca el source dry-run.
    source = await session.get(DataRepairRun, source_run_id)
    if (
        source is not None
        and source.dry_run
        and source.status == "COMPLETED"
        and final_status == "APPLIED"
    ):
        source.status = "APPLIED"

    session.add(
        DecisionAuditLog(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            decision_type=_DECISION_TYPE,
            decision_data={
                "run_id": str(run_id),
                "source_run_id": str(source_run_id),
                "dry_run": False,
                "status": final_status,
                "applied": applied,
                "skipped": skipped,
                "failed": failed,
                "decision_version": DECISION_VERSION,
            },
            triggered_by=triggered_by,
            created_at=datetime.now(UTC),
        )
    )
    await session.commit()

    return ApplyResult(
        run_id=run_id,
        status=final_status,
        groups_total=groups_total,
        groups_applied=applied,
        groups_skipped=skipped,
        groups_failed=failed,
        group_results=group_results,
    )


# ── REVERSA (--revert-run): inversa exacta de un run de APPLY (F3-T6) ─────────────
#
# Revierte SIEMPRE un run de APPLY (``dry_run==False``, ``source_run_id`` seteado),
# NUNCA el source dry-run. Un source puede tener N runs de apply; se revierte el que se
# pasa, con los ``DataRepairItem`` que ESE run registró (las mutaciones que hizo).
#
# Antes de tocar cada registro se valida que su estado ACTUAL coincide con el
# ``after_json`` que el apply dejó. Divergencia → se ABORTA ese grupo (queda sin
# revertir, se reporta) y el resto del run se revierte igual. Excepción de tolerancia:
# reactivar un duplicado YA activo es un no-op idempotente (no aborta). NO existe
# ``--force`` en v1 (forzar la reversa de inventario tras actividad real corrompe stock,
# aunque sea incremental).
#
# La inversa es EXACTA e INCREMENTAL y se aplica en orden INVERSO al del apply
# (DEACTIVATE → DELETE_BALANCE → CONSOLIDATE → REPOINT → MERGE): resta el delta
# group-level de ``stock_units``/``current_qty``, re-inserta el balance del dup, mueve
# las FKs registradas de vuelta al dup (check ``rowcount``), reactiva el dup y revierte
# los campos completados + custom_fields agregadas. NUNCA recomputa stock desde el
# ledger (invariante 2d); NUNCA clampea.

_REVERT_STATE_DIVERGED = "state_diverged"          # el estado ACTUAL ≠ after_json
_REVERT_FK_ACTIVITY = "fk_activity_after_apply"    # FKs nuevas/menos en el canónico
_REVERT_REPOINT_DIVERGED = "repoint_rows_diverged"  # las filas re-apuntadas cambiaron
_REVERT_BALANCE_DIVERGED = "balance_diverged"      # balance recreado/inconsistente
_REVERT_CANONICAL_MISSING = "canonical_missing"    # el canónico ya no existe
_REVERT_DUP_MISSING = "duplicate_missing"          # un duplicado ya no existe


@dataclass
class RevertResult:
    """Resumen de un ``revert_dedup_run``: estado + conteo por grupo. ``status`` es a
    nivel de resultado (``REVERTED`` full / ``PARTIALLY_REVERTED`` / ``FAILED``); el run
    de apply solo pasa a ``REVERTED`` en la DB si se revirtió TODO."""

    run_id: uuid.UUID
    status: str
    groups_total: int
    groups_reverted: int
    groups_skipped: int
    groups_failed: int
    group_results: list[dict[str, Any]] = field(default_factory=list)


async def _validate_apply_run(
    session: AsyncSession, tenant_id: uuid.UUID, run_id: uuid.UUID
) -> Any:
    """Carga y valida el run a revertir: existe, tipo PRODUCT_DEDUP, tenant coincide, es
    un run de APPLY (``dry_run==False`` + ``source_run_id`` seteado) y su ``status`` es
    revertible. Aborta con ``ValueError`` (mensaje claro) si no cumple."""
    from app.persistence.models.repair import DataRepairRun  # noqa: PLC0415

    run = await session.get(DataRepairRun, run_id)
    if run is None:
        raise ValueError(f"run-id {run_id} no existe")
    if run.repair_type != _REPAIR_TYPE:
        raise ValueError(
            f"run-id {run_id} no es un run {_REPAIR_TYPE} (es {run.repair_type!r})"
        )
    if run.tenant_id != tenant_id:
        raise ValueError(
            f"run-id {run_id} es de otro tenant ({run.tenant_id}), no de {tenant_id}"
        )
    if run.dry_run or run.source_run_id is None:
        raise ValueError(
            f"run-id {run_id} es un dry-run (plan), no un run de apply — "
            f"la reversa opera SOLO sobre un run de apply"
        )
    if run.status == "REVERTED":
        raise ValueError(f"run-id {run_id} ya fue REVERTED (nada que revertir)")
    if run.status not in ("APPLIED", "PARTIALLY_APPLIED", "COMPLETED_WITH_ERRORS"):
        raise ValueError(
            f"run-id {run_id} tiene status {run.status!r}; solo se revierte un run "
            f"de apply en APPLIED/PARTIALLY_APPLIED/COMPLETED_WITH_ERRORS"
        )
    return run


def _group_revert_items(items: list[Any]) -> dict[str, dict[str, Any]]:
    """Agrupa los ``DataRepairItem`` del run de apply por ``group_id`` (que viaja en
    ``before_json["plan"]``) y extrae TODO a dicts planos ANTES del loop de reversa
    (evita el ``MissingGreenlet`` de T5: tras un commit/rollback por grupo, el ORM del
    run está expirado y volver a leerlo dispararía un lazy-load sync)."""
    grouped: dict[str, dict[str, Any]] = {}
    for it in items:
        before = it.before_json or {}
        after = it.after_json or {}
        plan = before.get("plan") or {}
        gid = plan.get("group_id")
        canon = plan.get("canonical_product_id")
        if gid is None or canon is None:
            continue
        g = grouped.setdefault(
            gid,
            {
                "group_id": gid,
                "canonical_id": uuid.UUID(canon),
                "plan_block": plan,
                "merge": None,
                "repoint": [],
                "consolidate": None,
                "delete_balance": [],
                "deactivate": [],
            },
        )
        if it.action == "MERGE_PRODUCT":
            g["merge"] = {"before": before, "after": after}
        elif it.action == "REPOINT_FK":
            g["repoint"].append({"product_id": it.product_id, "before": before})
        elif it.action == "CONSOLIDATE_BALANCE":
            g["consolidate"] = {"before": before, "after": after}
        elif it.action == "DELETE_BALANCE":
            g["delete_balance"].append({"product_id": it.product_id, "before": before})
        elif it.action == "DEACTIVATE_DUPLICATE":
            g["deactivate"].append({"product_id": it.product_id, "before": before, "after": after})
    return grouped


async def _revert_one_group(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    gdata: dict[str, Any],
    *,
    lease_id: uuid.UUID | None,
) -> tuple[str, str]:
    """Revierte UN grupo (NO commitea — el caller maneja la txn). Valida el estado
    ACTUAL contra ``after_json`` (read-only); si diverge → ``("skipped", motivo)`` sin
    mutar. Si valida → aplica la inversa en orden INVERSO al apply y devuelve
    ``("reverted", "")``. Levanta ``LeaseLostError``/``RepointCountMismatchError`` → el
    caller hace rollback."""
    from sqlalchemy import func, select, update  # noqa: PLC0415
    from sqlalchemy.engine import CursorResult  # noqa: PLC0415

    from app.application.services import maintenance_lock_service as mls  # noqa: PLC0415
    from app.persistence.models.inventory import (  # noqa: PLC0415
        InventoryBalance,
        InventoryMovement,
    )
    from app.persistence.models.product import Product  # noqa: PLC0415
    from app.persistence.models.transaction import ExpenseEntry, SaleEntry  # noqa: PLC0415

    canonical_id: uuid.UUID = gdata["canonical_id"]
    merge = gdata["merge"]
    if merge is None:  # pragma: no cover — todo grupo aplicado registró un MERGE
        return ("skipped", "no_merge_item")
    repoint_items: list[dict[str, Any]] = gdata["repoint"]
    consolidate = gdata["consolidate"]
    delete_balances: list[dict[str, Any]] = gdata["delete_balance"]
    deactivates: list[dict[str, Any]] = gdata["deactivate"]

    model_by_table: dict[str, Any] = {
        "sales_entries": SaleEntry,
        "expense_entries": ExpenseEntry,
        "inventory_movements": InventoryMovement,
    }

    # (a) Barrera real (advisory exclusive; no-op SQLite) + fencing del lease.
    await mls.acquire_maintenance_lock_exclusive(session, tenant_id)
    if not await _lease_still_ours(session, tenant_id, lease_id):
        raise LeaseLostError(f"lease {lease_id} ya no es de este run (tenant {tenant_id})")

    # ── (b) VALIDACIÓN (read-only): estado ACTUAL == after_json ──────────────────
    m_after = merge["after"]
    m_before = merge["before"]
    canonical_prev = m_before.get("canonical_prev") or {}

    canon = await session.get(Product, canonical_id)
    if canon is None:
        return ("skipped", _REVERT_CANONICAL_MISSING)
    if canon.stock_units != m_after.get("stock_units_after"):
        return ("skipped", _REVERT_STATE_DIVERGED)
    if canon.requires_completion != m_after.get("requires_completion_after"):
        return ("skipped", _REVERT_STATE_DIVERGED)
    for f, val in (m_after.get("fields_completed") or {}).items():
        if _json_scalar(getattr(canon, f, None)) != val:
            return ("skipped", _REVERT_STATE_DIVERGED)
    canon_cf = canon.custom_fields or {}
    for k, v in (m_after.get("custom_fields_added") or {}).items():
        if canon_cf.get(k) != v:
            return ("skipped", _REVERT_STATE_DIVERGED)

    # FK baseline: el canónico debe tener exactamente `baseline + Σ filas re-apuntadas`.
    fk_before = canonical_prev.get("fk_count_3t")
    if fk_before is not None:
        expected_fk = fk_before + sum(
            len(rp["before"].get("rows") or []) for rp in repoint_items
        )
        if await _count_fk_3t(session, tenant_id, canonical_id) != expected_fk:
            return ("skipped", _REVERT_FK_ACTIVITY)

    # REPOINT: las filas registradas deben seguir apuntando al canónico (nadie las tocó).
    for rp in repoint_items:
        rows = rp["before"].get("rows") or []
        if not rows:
            continue
        model = model_by_table[rp["before"]["table"]]
        row_ids = [uuid.UUID(r["id"]) for r in rows]
        cnt = (
            await session.execute(
                select(func.count())
                .select_from(model)
                .where(
                    model.tenant_id == tenant_id,
                    model.id.in_(row_ids),
                    model.product_id == canonical_id,
                )
            )
        ).scalar_one()
        if int(cnt) != len(row_ids):
            return ("skipped", _REVERT_REPOINT_DIVERGED)

    # DELETE_BALANCE: el balance del dup fue borrado por el apply → no debe existir hoy.
    for db_item in delete_balances:
        dup_id = db_item["product_id"]
        exists = (
            await session.execute(
                select(func.count())
                .select_from(InventoryBalance)
                .where(
                    InventoryBalance.tenant_id == tenant_id,
                    InventoryBalance.product_id == dup_id,
                )
            )
        ).scalar_one()
        if int(exists) != 0:
            return ("skipped", _REVERT_BALANCE_DIVERGED)

    # CONSOLIDATE: el balance del canónico debe coincidir con lo que dejó el apply.
    if consolidate is not None:
        after_current = consolidate["after"].get("canonical_current_qty_after")
        canon_bal_row = (
            await session.execute(
                select(InventoryBalance).where(
                    InventoryBalance.tenant_id == tenant_id,
                    InventoryBalance.product_id == canonical_id,
                )
            )
        ).scalar_one_or_none()
        cur = canon_bal_row.current_qty if canon_bal_row is not None else None
        if cur != after_current:
            return ("skipped", _REVERT_BALANCE_DIVERGED)

    # DEACTIVATE: por dup — tolerancia si ya está activo; si no, debe seguir como lo
    # dejó el apply (is_active=False, reason=DUPLICATE).
    dup_objs: dict[uuid.UUID, Any] = {}
    for da in deactivates:
        dup_id = da["product_id"]
        dup = await session.get(Product, dup_id)
        if dup is None:
            return ("skipped", _REVERT_DUP_MISSING)
        dup_objs[dup_id] = dup
        if dup.is_active:
            continue  # tolerancia: reactivación previa → no-op idempotente
        if dup.deactivation_reason != da["after"].get("deactivation_reason"):
            return ("skipped", _REVERT_STATE_DIVERGED)

    # ── (c) MUTACIÓN (inversa exacta, orden INVERSO al apply) ────────────────────
    # 1) DEACTIVATE⁻¹ — reactivar el dup (no-op si ya está activo).
    for da in deactivates:
        dup_obj = dup_objs[da["product_id"]]
        if dup_obj.is_active:
            continue
        b = da["before"]
        dup_obj.is_active = bool(b.get("is_active", True))
        dup_obj.deactivated_at = None
        dup_obj.deactivation_reason = b.get("deactivation_reason")

    # 2) DELETE_BALANCE⁻¹ — re-insertar el balance del dup con sus valores originales.
    for db_item in delete_balances:
        b = db_item["before"]
        session.add(
            InventoryBalance(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                product_id=db_item["product_id"],
                current_qty=int(b["current_qty"]),
                reserved_qty=int(b["reserved_qty"]),
            )
        )

    # 3) CONSOLIDATE⁻¹ — restar el delta del canónico (o borrar el balance que el apply
    # CREÓ desde cero, cuando no había balance previo). Incremental, sin clamp.
    if consolidate is not None:
        cb = consolidate["before"]
        before_current = cb.get("canonical_current_qty_before")
        delta = int(cb.get("delta") or 0)
        canon_bal_row = (
            await session.execute(
                select(InventoryBalance).where(
                    InventoryBalance.tenant_id == tenant_id,
                    InventoryBalance.product_id == canonical_id,
                )
            )
        ).scalar_one_or_none()
        if before_current is None:
            # No existía balance previo → el apply lo creó (o no hay). Inversa = borrarlo.
            if canon_bal_row is not None:
                await session.delete(canon_bal_row)
        elif canon_bal_row is not None:
            canon_bal_row.current_qty = canon_bal_row.current_qty - delta

    # 4) REPOINT⁻¹ — mover las filas registradas de vuelta al dup. rowcount ≠ esperado
    # (alguien las tocó entre validación y mutación bajo el lock) → abortar el grupo.
    for rp in repoint_items:
        b = rp["before"]
        model = model_by_table[b["table"]]
        dup_id = rp["product_id"]
        row_ids = [uuid.UUID(r["id"]) for r in (b.get("rows") or [])]
        if not row_ids:
            continue
        result = await session.execute(
            update(model)
            .where(
                model.tenant_id == tenant_id,
                model.id.in_(row_ids),
                model.product_id == canonical_id,
            )
            .values(product_id=dup_id)
        )
        rowcount = cast("CursorResult[Any]", result).rowcount
        if rowcount != len(row_ids):
            raise RepointCountMismatchError(
                f"revert {b['table']} chunk {b.get('chunk_index')} dup {dup_id}: "
                f"esperado {len(row_ids)}, afectó {rowcount}"
            )

    # 5) MERGE⁻¹ — restar el delta de stock del canónico, revertir campos completados a
    # NULL, quitar SOLO las custom_fields agregadas y restaurar requires_completion.
    delta_stock = int(m_after.get("stock_delta_applied") or 0)
    canon.stock_units = canon.stock_units - delta_stock  # incremental, sin clamp
    for f in m_after.get("fields_completed") or {}:
        setattr(canon, f, None)  # el campo completado (estaba vacío) vuelve a NULL
    new_cf = dict(canon.custom_fields or {})
    for k in m_after.get("custom_fields_added") or {}:
        new_cf.pop(k, None)
    canon.custom_fields = new_cf
    if "requires_completion" in canonical_prev:
        canon.requires_completion = bool(canonical_prev["requires_completion"])

    return ("reverted", "")


async def revert_dedup_run(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    lease_id: uuid.UUID | None = None,
    ttl_seconds: int = 300,
    triggered_by: str = "script:dedupe_products_by_name",
) -> RevertResult:
    """Revierte un run de APPLY (T5), grupo por grupo en orden INVERSO, cada uno en su
    PROPIA transacción (commit/rollback por grupo). Valida el estado ACTUAL contra el
    ``after_json`` del apply antes de tocar cada grupo; divergencia → ese grupo se
    ABORTA (queda sin revertir, se reporta) y el resto se revierte igual. El run de
    apply pasa a ``REVERTED`` SOLO si se revirtió TODO; si quedó incompleto, su
    ``status`` no cambia y ``details_json['revert']`` documenta qué se revirtió y qué no.
    NO existe ``--force`` en v1."""
    from sqlalchemy import select  # noqa: PLC0415

    from app.application.services import maintenance_lock_service as mls  # noqa: PLC0415
    from app.persistence.models.audit import DecisionAuditLog  # noqa: PLC0415
    from app.persistence.models.repair import DataRepairItem, DataRepairRun  # noqa: PLC0415

    await _validate_apply_run(session, tenant_id, run_id)

    run_items = (
        (
            await session.execute(
                select(DataRepairItem).where(DataRepairItem.run_id == run_id)
            )
        )
        .scalars()
        .all()
    )
    # Extraé TODO a dicts planos ANTES del loop (evitá el MissingGreenlet de T5).
    grouped = _group_revert_items(list(run_items))

    groups_total = len(grouped)
    reverted = skipped = failed = 0
    group_results: list[dict[str, Any]] = []
    lease_lost = False

    # Orden INVERSO de grupos (el apply los recorrió en orden ascendente).
    for gid in sorted(grouped, reverse=True):
        gdata = grouped[gid]
        if lease_id is not None:
            renewed = await mls.renew(session, lease_id, ttl_seconds)
            # Heartbeat en su propia txn committeada (independiente del ciclo del grupo).
            await session.commit()
            if not renewed:
                group_results.append(
                    {"group_id": gid, "status": "failed", "reason": _LEASE_LOST_REASON}
                )
                failed += 1
                lease_lost = True
                break
        try:
            status, reason = await _revert_one_group(
                session, tenant_id, gdata, lease_id=lease_id
            )
        except LeaseLostError:
            await session.rollback()
            group_results.append(
                {"group_id": gid, "status": "failed", "reason": _LEASE_LOST_REASON}
            )
            failed += 1
            lease_lost = True
            break
        except Exception as exc:  # noqa: BLE001 — cualquier error del grupo → rollback aislado
            await session.rollback()
            group_results.append(
                {"group_id": gid, "status": "failed", "reason": f"{type(exc).__name__}: {exc}"}
            )
            failed += 1
            continue
        if status == "skipped":
            await session.rollback()
            group_results.append({"group_id": gid, "status": "skipped", "reason": reason})
            skipped += 1
        else:
            await session.commit()
            group_results.append({"group_id": gid, "status": "reverted", "reason": reason})
            reverted += 1

    fully_reverted = reverted == groups_total and skipped == 0 and failed == 0
    if fully_reverted:
        result_status = "REVERTED"
    elif reverted == 0 and failed > 0 and skipped == 0:
        result_status = "FAILED"
    else:
        result_status = "PARTIALLY_REVERTED"

    # Finalización: re-fetch (los commits/rollbacks intermedios expiran el ORM). El run
    # de apply solo pasa a REVERTED si se revirtió TODO; si no, se documenta en
    # details_json['revert'] y el status NO cambia (PARTIALLY_APPLIED no aplica al revert).
    apply_run = await session.get(DataRepairRun, run_id)
    if apply_run is not None:
        details = dict(apply_run.details_json or {})
        details["revert"] = {
            "decision_version": DECISION_VERSION,
            "groups_total": groups_total,
            "reverted": reverted,
            "skipped": skipped,
            "failed": failed,
            "lease_lost": lease_lost,
            "fully_reverted": fully_reverted,
            "groups": group_results,
        }
        apply_run.details_json = details
        if fully_reverted:
            apply_run.status = "REVERTED"

    session.add(
        DecisionAuditLog(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            decision_type=_DECISION_TYPE,
            decision_data={
                "run_id": str(run_id),
                "action": "revert",
                "status": result_status,
                "reverted": reverted,
                "skipped": skipped,
                "failed": failed,
                "decision_version": DECISION_VERSION,
            },
            triggered_by=triggered_by,
            created_at=datetime.now(UTC),
        )
    )
    await session.commit()

    return RevertResult(
        run_id=run_id,
        status=result_status,
        groups_total=groups_total,
        groups_reverted=reverted,
        groups_skipped=skipped,
        groups_failed=failed,
        group_results=group_results,
    )
