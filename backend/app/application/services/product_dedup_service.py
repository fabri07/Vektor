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
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

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
