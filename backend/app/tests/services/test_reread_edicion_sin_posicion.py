"""B·nuevo (2026-10-07): la edición a mano sobrevive a que las filas se corran.

Hasta acá una edición se preservaba por su ``source_row_ref``, que es la POSICIÓN
de su fila. Si el archivo volvía con una fila de más adelante, de menos o en otro
orden, la referencia apuntaba a otra fila: esa otra se salteaba y la de la
edición entraba de nuevo, repetida. La compuerta del #81 lo frenaba.

Ahora el importador escribe la huella de la fila original (``source_row_digest``)
y la relectura reubica la edición en la fila fresca con esa huella, dentro de su
contexto, por conjuntos, con el MISMO plan en el preview y en el apply.

Lo importado antes de esto no tiene huella: queda como hoy, bajo la compuerta. Una
edición repetida PROBADA por la huella de su fila no se puede aceptar (decisión
del dueño); la estimada (dos de tres, o sin huella) se acepta como antes.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import reread_service
from app.application.services.file_deletion_service import revert_file_data
from app.domain.fila_original import huella_de_fila
from app.persistence.models.memory import OperationFingerprint
from app.persistence.models.repair import DataRepairRun
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.tests.conftest import add_business_profile
from app.tests.integration import _reread_scenario as esc
from app.tests.services.test_reread_compuerta_perdidas import (
    _CUATRO,
    _aplicar_frena,
    _csv,
    _patch_s3,
    _subir_e_importar,
)


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker en tests: ver el mismo fixture en ``test_reread_file.py``."""


@pytest_asyncio.fixture
async def tenant(db_session: AsyncSession) -> Tenant:
    t = Tenant(
        tenant_id=uuid.uuid4(),
        legal_name="Deco Test",
        display_name="Deco Test",
        currency="ARS",
        pricing_reference_mode="MEP",
        status="ACTIVE",
    )
    db_session.add(t)
    await db_session.flush()
    await add_business_profile(db_session, t.tenant_id)
    await db_session.commit()
    return t

# ── La huella ───────────────────────────────────────────────────────────────────


def test_la_huella_no_interpreta_los_valores() -> None:
    """Debe/Haber, precio × cantidad y ceros a la izquierda: filas distintas para
    el archivo, aunque «signifiquen» lo mismo. Se compara parser contra parser."""
    pares = [
        ({"debe": "5000", "haber": ""}, {"debe": "", "haber": "5000"}),
        ({"precio": "2", "cantidad": "10"}, {"precio": "10", "cantidad": "2"}),
        ({"codigo": "007"}, {"codigo": "7"}),
        ({"detalle": "Coca"}, {"detalle": "coca"}),
    ]
    for a, b in pares:
        assert huella_de_fila(a, "sale") != huella_de_fila(b, "sale"), (a, b)
    assert huella_de_fila({"x": "1"}, "sale") != huella_de_fila({"x": "1"}, "expense")


def test_la_huella_ignora_lo_del_sistema_y_usa_el_original() -> None:
    fila = {"fecha": "05/01/2026", "monto": " 1.500 ", "nota": None}
    assert huella_de_fila(fila, "sale") == huella_de_fila(
        {"fecha": "05/01/2026", "monto": "1.500", "nota": "", "__context__": "x"}, "sale"
    )
    # La fila como la ve el importador: el monto ya interpretado y el original aparte.
    interpretada = {"fecha": "05/01/2026", "monto": Decimal("1500"), "nota": None}
    assert huella_de_fila(
        interpretada, "sale", {"monto": " 1.500 "}
    ) == huella_de_fila(fila, "sale")
    assert huella_de_fila(interpretada, "sale") != huella_de_fila(fila, "sale")
    assert huella_de_fila(fila, "sale").startswith("f1:")


# ── El plan, puro ───────────────────────────────────────────────────────────────

_T = uuid.UUID(int=1)
_F = uuid.UUID(int=2)


def _ref(ctx: str | None, idx: int) -> str:
    return reread_service._hash_anchor(reread_service._row_anchor(_T, _F, ctx, idx))


_Fila = tuple[str | None, int, dict[str, Any], str]


def _filas(ctx: str | None, contenidos: list[str]) -> list[_Fila]:
    return [(ctx, i, {"detalle": c}, "expense") for i, c in enumerate(contenidos)]


def _reg(
    ctx: str | None,
    idx: int,
    contenido: str | None,
    *,
    editado: bool = True,
    ref: str | None = None,
) -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        has_user_edits=editado,
        source_row_ref=ref or _ref(ctx, idx),
        source_row_digest=(
            huella_de_fila({"detalle": contenido}, "expense") if contenido else None
        ),
    )


def _plan(
    registros: list[Any],
    filas: list[_Fila],
    vivas: set[str] | None = None,
    otros: set[str] | None = None,
) -> dict[uuid.UUID, tuple[str, str]]:
    return reread_service.planear_reubicaciones(
        _T, _F, cast(Any, registros), filas, vivas or set(), otros or set(), {}
    )


def test_cadena_e_intercambio() -> None:
    # Cadena: se inserta una fila antes de tres ediciones seguidas.
    a, b, c = _reg(None, 1, "B"), _reg(None, 2, "C"), _reg(None, 3, "D")
    plan = _plan([a, b, c], _filas(None, ["N", "A", "B", "C", "D"]))
    assert plan == {
        a.id: (_ref(None, 1), _ref(None, 2)),
        b.id: (_ref(None, 2), _ref(None, 3)),
        c.id: (_ref(None, 3), _ref(None, 4)),
    }
    # Intercambio A↔B.
    x, y = _reg(None, 0, "X"), _reg(None, 1, "Y")
    plan = _plan([x, y], _filas(None, ["Y", "X"]))
    assert plan == {x.id: (_ref(None, 0), _ref(None, 1)), y.id: (_ref(None, 1), _ref(None, 0))}


def test_destino_ocupado_no_se_mueve() -> None:
    e = _reg(None, 1, "E")
    filas = _filas(None, ["E", "Z"])
    assert _plan([e], filas) == {e.id: (_ref(None, 1), _ref(None, 0))}
    # Por un editado sin huella (preservado que se queda).
    sin = _reg(None, 0, None)
    assert _plan([e, sin], filas) == {}
    # Por una fila clasificada desde «Otros» (F-O.2 la reimporta).
    assert _plan([e], filas, otros={_ref(None, 0)}) == {}
    # Por una huella que sigue registrada (p. ej. una fila capturada a «Otros»).
    assert _plan([e], filas, vivas={_ref(None, 0)}) == {}


def test_gemelas_en_el_mismo_contexto() -> None:
    # |P| <= |F|: la edición toma la primera gemela libre.
    e = _reg(None, 1, "T")
    plan = _plan([e, _reg(None, 2, "T", editado=False)], _filas(None, ["N", "A", "T", "T"]))
    assert plan == {e.id: (_ref(None, 1), _ref(None, 2))}
    # |P| > |F|: el grupo entero queda como hoy.
    e1, e2 = _reg(None, 1, "T"), _reg(None, 2, "T")
    assert _plan([e1, e2], _filas(None, ["N", "T"])) == {}


def test_el_grupo_que_no_alcanza_ocupa_su_lugar_y_frena_al_que_iba_ahi() -> None:
    """Punto fijo: la gemela que se queda bloquea su posición, aunque otra
    edición (de otro grupo) la hubiera tomado en la primera vuelta."""
    t1, t2 = _reg(None, 0, "T"), _reg(None, 1, "T")  # quedan sin lugar: una sola T
    u = _reg(None, 2, "U")  # su fila pasó a la posición 1, la de t2
    assert _plan([t1, t2, u], _filas(None, ["X", "U", "T"])) == {}


def test_la_que_sigue_en_su_fila_no_se_muda_a_una_gemela_nueva() -> None:
    """[E', E] con E editada en la fila 1: la 0 es una gemela nueva libre, pero
    la edición tiene su fila y se queda ahí. Sin la regla, se muda a la 0."""
    e = _reg(None, 1, "E")
    assert _plan([e], _filas(None, ["E", "E"])) == {}


def test_no_se_cruza_de_contexto_ni_se_reubica_sin_contexto() -> None:
    e = _reg("hoja:A", 1, "E")
    filas = [*_filas("hoja:A", ["X"]), *_filas("hoja:B", ["E"])]
    assert _plan([e], filas) == {}
    # El contexto cambió de id (D0): no se reubica.
    assert _plan([e], _filas("hoja:Nueva", ["X", "Y", "E"])) == {}


def test_sin_huella_legacy_o_fila_compartida_no_se_reubica() -> None:
    filas = _filas(None, ["N", "E"])
    # Un registro no editado sin ref: el archivo es legacy entero.
    e = _reg(None, 0, "E")
    legacy = _reg(None, 5, "Q", editado=False)
    legacy.source_row_ref = None
    assert _plan([e, legacy], filas) == {}
    # Venta y gasto de la misma fila: comparten ref y quedan como hoy.
    otro = _reg(None, 0, "E", editado=False)
    assert _plan([e, otro], filas) == {}


# ── El apply real ───────────────────────────────────────────────────────────────


async def _editar(session: AsyncSession, file_id: uuid.UUID, monto: str) -> uuid.UUID:
    rec = (
        await session.execute(
            select(ExpenseEntry).where(
                ExpenseEntry.source_upload_id == file_id,
                ExpenseEntry.amount == Decimal(monto),
                ExpenseEntry.voided_at.is_(None),
            )
        )
    ).scalar_one()
    rec.has_user_edits = True
    rec.description = f"corregido a mano {monto}"
    await session.commit()
    return rec.id


async def _vivos(session: AsyncSession, file_id: uuid.UUID) -> list[tuple[str, str]]:
    filas = (
        await session.execute(
            select(ExpenseEntry).where(
                ExpenseEntry.source_upload_id == file_id, ExpenseEntry.voided_at.is_(None)
            )
        )
    ).scalars().all()
    return sorted(
        (str(Decimal(e.amount).quantize(Decimal("1"))), e.description or "") for e in filas
    )


async def _huellas_del_archivo(
    session: AsyncSession, tenant_id: uuid.UUID, file_id: uuid.UUID, n: int = 12
) -> set[str]:
    anclas = {
        reread_service._hash_anchor(reread_service._row_anchor(tenant_id, file_id, None, i))
        for i in range(n)
    }
    return set(
        (
            await session.execute(
                select(OperationFingerprint.fingerprint).where(
                    OperationFingerprint.tenant_id == tenant_id,
                    OperationFingerprint.fingerprint.in_(anclas),
                )
            )
        ).scalars()
    )


async def _refs_vivas(session: AsyncSession, file_id: uuid.UUID) -> set[str]:
    return {
        r
        for r in (
            await session.execute(
                select(ExpenseEntry.source_row_ref).where(
                    ExpenseEntry.source_upload_id == file_id, ExpenseEntry.voided_at.is_(None)
                )
            )
        ).scalars()
        if r
    }


_N = ("2026-01-02", "Azúcar", "333")


@pytest.mark.parametrize(
    "nuevo",
    [
        pytest.param([_N, *_CUATRO], id="insertada-antes"),
        pytest.param(list(reversed(_CUATRO)), id="reordenadas"),
        pytest.param(_CUATRO[1:], id="eliminada-antes"),
    ],
)
async def test_la_edicion_sobrevive_al_corrimiento_sin_duplicarse(
    db_session: AsyncSession,
    tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
    nuevo: list[tuple[str, str, str]],
) -> None:
    file = await _subir_e_importar(
        db_session, tenant.tenant_id, _csv(_CUATRO), "text/csv", "g.csv"
    )
    tid, fid = tenant.tenant_id, file.id
    editado = await _editar(db_session, fid, "2200")
    _patch_s3(monkeypatch, _csv(nuevo))

    preview = await reread_service.preview_reread(db_session, fid, tid)
    assert preview.edited_at_risk == 0
    se_pierde = 0 if len(nuevo) >= len(_CUATRO) else 1
    assert preview.to_lose == se_pierde

    resultado = await reread_service.apply_reread(
        db_session, fid, tid, accept_data_loss=bool(se_pierde)
    )
    await db_session.commit()
    run = await db_session.get(DataRepairRun, resultado.run_id)
    assert run is not None
    perdida = (run.details_json or {}).get("data_loss") or {}
    # El preview y el apply dicen lo mismo.
    assert (int(perdida.get("to_lose") or 0), int(perdida.get("edited_at_risk") or 0)) == (
        preview.to_lose,
        preview.edited_at_risk,
    )
    esperado = sorted(
        (m, "corregido a mano 2200" if m == "2200" else p) for _d, p, m in nuevo
    )
    assert await _vivos(db_session, fid) == esperado
    rec = await db_session.get(ExpenseEntry, editado)
    assert rec is not None and rec.voided_at is None, "la edición es el MISMO registro"
    pos = next(i for i, (_d, _p, m) in enumerate(nuevo) if m == "2200")
    assert rec.source_row_ref == _ref_de(tid, fid, pos)
    if pos != 2:
        reubicaciones = (run.details_json or {})["reubicaciones"]
        assert [r["id"] for r in reubicaciones] == [str(editado)]
    # Ninguna huella huérfana: las del archivo son exactamente las de sus registros.
    assert await _huellas_del_archivo(db_session, tid, fid) == await _refs_vivas(db_session, fid)


def _ref_de(tenant_id: uuid.UUID, file_id: uuid.UUID, idx: int) -> str:
    return reread_service._hash_anchor(reread_service._row_anchor(tenant_id, file_id, None, idx))


async def test_un_contexto_editado_entero_se_reubica_y_dos_relecturas_no_dejan_huerfanas(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Todo editado: ``_ediciones_en_riesgo`` no tenía con qué ver el corrimiento.
    Después, una segunda relectura con otro corrimiento."""
    file = await _subir_e_importar(
        db_session, tenant.tenant_id, _csv(_CUATRO), "text/csv", "g.csv"
    )
    tid, fid = tenant.tenant_id, file.id
    for _d, _p, m in _CUATRO:
        await _editar(db_session, fid, m)
    esperado = sorted((m, f"corregido a mano {m}") for _d, _p, m in _CUATRO)

    _patch_s3(monkeypatch, _csv([_CUATRO[3], _CUATRO[1], _CUATRO[0], _CUATRO[2]]))
    await reread_service.apply_reread(db_session, fid, tid)
    await db_session.commit()
    assert await _vivos(db_session, fid) == esperado

    _patch_s3(monkeypatch, _csv([_N, *_CUATRO]))
    resultado = await reread_service.apply_reread(db_session, fid, tid)
    await db_session.commit()
    assert await _vivos(db_session, fid) == sorted([*esperado, ("333", "Azúcar")])
    # Después de la primera: D, B, A, C. La C sigue en su lugar; las otras tres no.
    assert len(resultado.reubicaciones) == 3
    assert await _huellas_del_archivo(db_session, tid, fid) == await _refs_vivas(db_session, fid)


async def test_gemelas_editada_una_se_reubica_y_la_otra_vuelve_una_vez(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    gemela = ("2026-01-06", "Pan Lactal", "800")
    filas = [_CUATRO[0], gemela, gemela, _CUATRO[3]]
    file = await _subir_e_importar(db_session, tenant.tenant_id, _csv(filas), "text/csv", "g.csv")
    tid, fid = tenant.tenant_id, file.id
    rec = (
        await db_session.execute(
            select(ExpenseEntry)
            .where(ExpenseEntry.source_upload_id == fid, ExpenseEntry.amount == Decimal("800"))
            .order_by(ExpenseEntry.source_row_ref)
        )
    ).scalars().first()
    assert rec is not None
    rec.has_user_edits = True
    rec.description = "corregido a mano"
    await db_session.commit()

    _patch_s3(monkeypatch, _csv([_N, *filas]))
    await reread_service.apply_reread(db_session, fid, tid)
    await db_session.commit()
    assert await _vivos(db_session, fid) == sorted(
        [("333", "Azúcar"), ("1500", "Coca Cola"), ("800", "corregido a mano"),
         ("800", "Pan Lactal"), ("950", "Fideos")]
    )


async def test_gemelas_editadas_que_ya_no_alcanzan_quedan_como_hoy_y_frenan(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    gemela = ("2026-01-06", "Pan Lactal", "800")
    filas = [_CUATRO[0], gemela, gemela]
    file = await _subir_e_importar(db_session, tenant.tenant_id, _csv(filas), "text/csv", "g.csv")
    tid, fid = tenant.tenant_id, file.id
    for rec in (
        await db_session.execute(
            select(ExpenseEntry).where(
                ExpenseEntry.source_upload_id == fid, ExpenseEntry.amount == Decimal("800")
            )
        )
    ).scalars():
        rec.has_user_edits = True
        rec.description = "corregido a mano"
    await db_session.commit()

    _patch_s3(monkeypatch, _csv([gemela, _CUATRO[0]]))
    perdida = await _aplicar_frena(db_session, tid, fid, accept_data_loss=True)
    assert perdida.edited_at_risk >= 1
    # La gemela que entra es la fila original de una de ellas: probado.
    assert perdida.edited_repeated_by_digest >= 1
    # El camino real: el worker acepta SIEMPRE con la medición como tope.
    otra = await _aplicar_frena(
        db_session, tid, fid, accept_data_loss=True, tope_aceptado=perdida.as_dict()
    )
    assert otra.edited_repeated_by_digest >= 1


def _foto(kind: str, monto: str, texto: str, ref: str, **extra: Any) -> dict[str, Any]:
    clave = "notes" if kind == "sale" else "description"
    return {"kind": kind, "amount": monto, "transaction_date": "2026-10-01",
            clave: texto, "source_row_ref": ref, **extra}


@pytest.mark.parametrize("al_reves", [False, True], ids=["original-al-final", "original-primero"])
def test_la_fila_original_de_una_edicion_no_se_empareja_con_otro_anulado(al_reves: bool) -> None:
    """Revisión del #86: la venta N (sin editar, mismo importe y día que la
    fila original de la edición E) se anula y vuelve. Si el anulado de N tomaba
    en el nivel 1 la fila original de E, la repetición probada desaparecía según
    el ORDEN de las filas, y la vuelta real de N quedaba suelta sin frenar."""
    anulado = _foto("sale", "100", "Pepsi", "rB")
    vuelta_n = _foto("sale", "100", "Pepsi", "rC")
    original_e = _foto("sale", "100", "Coca", "rD")
    insertadas = [original_e, vuelta_n] if al_reves else [vuelta_n, original_e]
    items = [{"action": reread_service.ACTION_VOID, "before": anulado}] + [
        {"action": reread_service.ACTION_INSERT, "after": f} for f in insertadas
    ]
    edicion = _foto("sale", "150", "Coca editado", "rE", id="e1")
    perdida = reread_service.medir_perdida_real(
        items, [edicion], [], filas_de_ediciones={"e1": {"rD"}}
    )
    assert (perdida.to_lose, perdida.edited_at_risk, perdida.edited_repeated_by_digest) == (
        0, 1, 1,
    )


@pytest.mark.parametrize("texto_anulado", ["Pepsi", "B"], ids=["otra-venta", "otro-texto"])
def test_otra_operacion_del_mismo_importe_y_dia_no_se_lleva_la_fila_original(
    texto_anulado: str,
) -> None:
    """Revisión 3 del #86 (caso D0): la venta A (sin editar, mismo importe y día
    que la fila original de la edición E, otro texto) pierde su fila. Si su
    anulado tomaba la fila original de E en el nivel 1, se tapaban a la vez la
    pérdida de A y la repetición probada de E: el apply no frenaba."""
    items = [
        {"action": reread_service.ACTION_VOID, "before": _foto("sale", "100", texto_anulado, "rA")},
        {"action": reread_service.ACTION_INSERT, "after": _foto("sale", "100", "Coca", "rX")},
    ]
    edicion = _foto("sale", "120", "Coca", "rE", id="e1")
    perdida = reread_service.medir_perdida_real(
        items, [edicion], [], filas_de_ediciones={"e1": {"rX"}}
    )
    assert (perdida.to_lose, perdida.edited_at_risk, perdida.edited_repeated_by_digest) == (
        1, 1, 1,
    )


@pytest.mark.parametrize("texto", ["Coca", ""], ids=["con-texto", "sin-texto"])
def test_la_gemela_sin_editar_de_la_misma_huella_vuelve_sobre_la_fila_original(
    texto: str,
) -> None:
    """La gemela T (contenido entero igual al de la fila original de E, texto
    vacío incluido) se anula y entra una sola fila con esa huella: T vuelve en
    ella. Sin esta pareja, T contaba como perdida y E como repetida probada, sin
    salida."""
    items = [
        {"action": reread_service.ACTION_VOID, "before": _foto("sale", "100", texto, "rT")},
        {"action": reread_service.ACTION_INSERT, "after": _foto("sale", "100", texto, "rD")},
    ]
    edicion = _foto("sale", "150", "Coca editado", "rE", id="e1")
    perdida = reread_service.medir_perdida_real(
        items, [edicion], [], filas_de_ediciones={"e1": {"rD"}}
    )
    assert (perdida.to_lose, perdida.edited_at_risk, perdida.edited_repeated_by_digest) == (
        0, 0, 0,
    )


async def test_sin_posiciones_por_el_tope_la_repeticion_queda_estimada(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revisión 3: con el tope de anclas no se sabe el contexto de la edición.
    Buscar su huella en todas las hojas contaba como probada (irrechazable) una
    fila igual de otra hoja; ahora queda estimada y se puede aceptar."""
    gemela = ("2026-01-06", "Pan Lactal", "800")
    filas = [_CUATRO[0], gemela, gemela]
    file = await _subir_e_importar(db_session, tenant.tenant_id, _csv(filas), "text/csv", "g.csv")
    tid, fid = tenant.tenant_id, file.id
    for rec in (
        await db_session.execute(
            select(ExpenseEntry).where(
                ExpenseEntry.source_upload_id == fid, ExpenseEntry.amount == Decimal("800")
            )
        )
    ).scalars():
        rec.has_user_edits = True
        rec.description = "corregido a mano"
    await db_session.commit()

    monkeypatch.setattr(reread_service, "_MAX_ANCLAS_DE_EDICIONES", 0)
    _patch_s3(monkeypatch, _csv([gemela, _CUATRO[0]]))
    perdida = await _aplicar_frena(db_session, tid, fid)
    assert perdida.edited_at_risk >= 1
    assert perdida.edited_repeated_by_digest == 0
    await reread_service.apply_reread(
        db_session, fid, tid, accept_data_loss=True, tope_aceptado=perdida.as_dict()
    )
    await db_session.commit()


async def test_un_cambio_de_formato_no_casa_y_queda_la_red_de_la_compuerta(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cambio de parser (o del archivo): la fila de la edición ya no se lee igual,
    no hay evidencia de cuál es, y no se adivina. La repetición es ESTIMADA (dos
    de tres): frena y se puede aceptar, como antes de B·nuevo."""
    file = await _subir_e_importar(
        db_session, tenant.tenant_id, _csv(_CUATRO), "text/csv", "g.csv"
    )
    tid, fid = tenant.tenant_id, file.id
    await _editar(db_session, fid, "2200")
    otra_forma = [(d, p, "2200.00" if m == "2200" else m) for d, p, m in _CUATRO]
    _patch_s3(monkeypatch, _csv([_N, *otra_forma]))
    perdida = await _aplicar_frena(db_session, tid, fid)
    assert (perdida.edited_at_risk, perdida.edited_repeated_by_digest) == (1, 0)
    await reread_service.apply_reread(
        db_session, fid, tid, accept_data_loss=True, tope_aceptado=perdida.as_dict()
    )
    await db_session.commit()


async def test_lo_importado_antes_no_se_reubica_y_la_edicion_repetida_se_acepta(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sin huella (ASTERIA, todo lo importado antes de B·nuevo): como antes. La
    repetición es estimada, frena con el mensaje de qué edición, se puede aceptar
    y el rollback del frenado no deja nada movido. Si no se pudiera aceptar, la
    relectura de un archivo legacy con una edición quedaría sin salida: borrarlo
    y volver a subirlo (F11) también descarta la edición."""
    file = await _subir_e_importar(
        db_session, tenant.tenant_id, _csv(_CUATRO), "text/csv", "g.csv"
    )
    tid, fid = tenant.tenant_id, file.id
    editado = await _editar(db_session, fid, "2200")
    rec = await db_session.get(ExpenseEntry, editado)
    assert rec is not None
    rec.source_row_digest = None
    ref_antes = rec.source_row_ref
    await db_session.commit()
    huellas_antes = await _huellas_del_archivo(db_session, tid, fid)
    _patch_s3(monkeypatch, _csv([_N, *_CUATRO]))

    with pytest.raises(reread_service.RereadWouldLoseDataError) as frenado:
        await reread_service.apply_reread(db_session, fid, tid)
    await db_session.rollback()
    perdida = frenado.value.perdida
    assert (perdida.edited_at_risk, perdida.edited_repeated_by_digest) == (1, 0)
    texto = str(frenado.value)
    assert "no se puede aplicar igual" not in texto
    assert "confirmá si querés aplicarla igual" in texto
    assert "corregido a mano 2200" in texto
    # Rollback completo: nada se movió.
    rec = await db_session.get(ExpenseEntry, editado)
    assert rec is not None and rec.source_row_ref == ref_antes
    assert await _huellas_del_archivo(db_session, tid, fid) == huellas_antes
    assert len(await _vivos(db_session, fid)) == 4

    await reread_service.apply_reread(
        db_session, fid, tid, accept_data_loss=True, tope_aceptado=perdida.as_dict()
    )
    await db_session.commit()
    rec = await db_session.get(ExpenseEntry, editado)
    assert rec is not None and rec.voided_at is None


@pytest.mark.parametrize(
    "nueva",
    [
        pytest.param(("2026-01-07", "Mate cocido", "2200"), id="mismo-dia-e-importe"),
        # Idéntica a la fila original de la edición: la huella es la misma, pero
        # la edición sigue sobre SU fila, así que ésta es otra operación.
        pytest.param(_CUATRO[2], id="gemela-identica"),
    ],
)
async def test_un_alta_nueva_parecida_a_una_edicion_se_puede_aceptar(
    db_session: AsyncSession,
    tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
    nueva: tuple[str, str, str],
) -> None:
    """Falso positivo de la heurística de dos de tres: la edición sigue en su
    fila (su huella lo prueba) y al final entra OTRA operación del mismo día y el
    mismo importe (un kiosco: «Venta mostrador»). Frena (estimado) y se acepta:
    no hay que tirar la edición ni dejar afuera una fila legítima del archivo."""
    file = await _subir_e_importar(
        db_session, tenant.tenant_id, _csv(_CUATRO), "text/csv", "g.csv"
    )
    tid, fid = tenant.tenant_id, file.id
    editado = await _editar(db_session, fid, "2200")
    _patch_s3(monkeypatch, _csv([*_CUATRO, nueva]))

    perdida = await _aplicar_frena(db_session, tid, fid)
    assert (perdida.to_lose, perdida.edited_at_risk) == (0, 1)
    assert perdida.edited_repeated_by_digest == 0
    await reread_service.apply_reread(
        db_session, fid, tid, accept_data_loss=True, tope_aceptado=perdida.as_dict()
    )
    await db_session.commit()
    assert await _vivos(db_session, fid) == sorted(
        [(m, p) for _d, p, m in _CUATRO if m != "2200"]
        + [("2200", "corregido a mano 2200"), ("2200", nueva[1])]
    )
    rec = await db_session.get(ExpenseEntry, editado)
    assert rec is not None and rec.voided_at is None


async def test_si_frena_por_otra_perdida_la_reubicacion_tambien_se_revierte(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = await _subir_e_importar(
        db_session, tenant.tenant_id, _csv(_CUATRO), "text/csv", "g.csv"
    )
    tid, fid = tenant.tenant_id, file.id
    editado = await _editar(db_session, fid, "2200")
    rec = await db_session.get(ExpenseEntry, editado)
    assert rec is not None
    ref_antes = rec.source_row_ref
    huellas_antes = await _huellas_del_archivo(db_session, tid, fid)
    _patch_s3(monkeypatch, _csv(_CUATRO[1:]))  # se pierde la primera: frena

    perdida = await _aplicar_frena(db_session, tid, fid)
    assert (perdida.to_lose, perdida.edited_at_risk) == (1, 0)
    rec = await db_session.get(ExpenseEntry, editado)
    assert rec is not None and rec.source_row_ref == ref_antes
    assert await _huellas_del_archivo(db_session, tid, fid) == huellas_antes


# ── Stock, undo y F11 ───────────────────────────────────────────────────────────

_COMPRAS = [
    esc.fila(5, "Vela aromatica 200g", "5", "6000"),
    esc.fila(6, "Jabon de glicerina", "3", "3600"),
    esc.fila(7, "Difusor de bambu", "2", "2400"),
]


async def _compras(
    session: AsyncSession, tenant_id: uuid.UUID, filas: list[dict[str, str]]
) -> uuid.UUID:
    from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile

    fid = uuid.uuid4()
    session.add(
        UploadedFile(
            id=fid,
            tenant_id=tenant_id,
            original_filename="compras.xlsx",
            s3_key="k",
            content_type="application/vnd.ms-excel",
            size_bytes=1,
            purpose="gastos",
            processing_status=PROCESSING_STATUS_DONE,
            parsed_summary_json=esc.summary(filas),
        )
    )
    await session.commit()
    from app.application.services.ingestion_import_service import insert_confirmed_data

    await insert_confirmed_data(
        session, tenant_id, esc.summary(filas), {"gastos": True}, uploaded_file_id=fid
    )
    await session.commit()
    return fid


async def _stock(session: AsyncSession, tenant_id: uuid.UUID) -> dict[str, int]:
    from app.persistence.models.product import Product

    return {
        p.name: int(p.stock_units)
        for p in (
            await session.execute(select(Product).where(Product.tenant_id == tenant_id))
        ).scalars()
    }


async def _movimientos(session: AsyncSession, file_id: uuid.UUID) -> dict[str, list[Any]]:
    from app.persistence.models.inventory import InventoryMovement

    movs = (
        await session.execute(
            select(InventoryMovement).where(
                InventoryMovement.source_upload_id == file_id,
                InventoryMovement.voided_at.is_(None),
            )
        )
    ).scalars().all()
    out: dict[str, list[Any]] = {}
    for m in movs:
        out.setdefault(m.source_row_ref or "", []).append((m.id, m.qty))
    return out


async def test_stock_undo_y_borrado_del_archivo_despues_de_reubicar(
    db_session: AsyncSession, tenant: Tenant
) -> None:
    tid = tenant.tenant_id
    fid = await _compras(db_session, tid, _COMPRAS)
    editado = await _editar(db_session, fid, "3600")
    rec = await db_session.get(ExpenseEntry, editado)
    assert rec is not None
    ref_antes = rec.source_row_ref or ""
    movs_antes = await _movimientos(db_session, fid)
    (mov_editado,) = movs_antes[ref_antes]
    stock_antes = await _stock(db_session, tid)
    huellas_antes = await _huellas_del_archivo(db_session, tid, fid)
    refs_antes = await _refs_vivas(db_session, fid)

    nuevo = [esc.fila(4, "Sahumerio palo santo", "1", "1200"), *_COMPRAS]
    resultado = await reread_service.apply_reread(
        db_session, fid, tid, fresh_override=esc.summary(nuevo)
    )
    await db_session.commit()

    rec = await db_session.get(ExpenseEntry, editado)
    assert rec is not None and rec.source_row_ref == _ref_de(tid, fid, 2)
    movs = await _movimientos(db_session, fid)
    # El movimiento de la edición es el MISMO, y se fue con ella por id.
    assert movs[_ref_de(tid, fid, 2)] == [mov_editado]
    assert all(len(v) == 1 for v in movs.values()), movs
    assert await _stock(db_session, tid) == {**stock_antes, "Sahumerio palo santo": 1}
    assert resultado.reubicaciones[0]["movimientos"] == [str(mov_editado[0])]
    assert await _huellas_del_archivo(db_session, tid, fid) == await _refs_vivas(db_session, fid)

    await reread_service.undo_reread(db_session, resultado.run_id, tid)
    await db_session.commit()
    rec = await db_session.get(ExpenseEntry, editado)
    assert rec is not None and rec.source_row_ref == ref_antes
    assert await _movimientos(db_session, fid) == movs_antes
    assert {k: v for k, v in (await _stock(db_session, tid)).items() if v} == stock_antes
    # La de la fila nueva (2) la conserva el no editado repuesto. La de la 3 es de
    # una fila que esta relectura INSERTÓ: el undo nunca suelta esas huellas
    # (preexistente, fuera de este paso).
    assert await _huellas_del_archivo(db_session, tid, fid) == huellas_antes | {
        _ref_de(tid, fid, 3)
    }
    assert _ref_de(tid, fid, 2) in huellas_antes
    assert await _refs_vivas(db_session, fid) == refs_antes



async def test_borrar_el_archivo_despues_de_reubicar_no_deja_huellas(
    db_session: AsyncSession, tenant: Tenant
) -> None:
    """F11 deriva las huellas de las refs de los registros: la de la fila nueva
    es la del registro editado, y la vieja ya no existe."""
    tid = tenant.tenant_id
    fid = await _compras(db_session, tid, _COMPRAS)
    await _editar(db_session, fid, "3600")
    nuevo = [esc.fila(4, "Sahumerio palo santo", "1", "1200"), *_COMPRAS]
    resultado = await reread_service.apply_reread(
        db_session, fid, tid, fresh_override=esc.summary(nuevo)
    )
    await db_session.commit()
    assert resultado.reubicaciones
    await revert_file_data(db_session, fid, tid)
    await db_session.commit()
    assert await _huellas_del_archivo(db_session, tid, fid) == set()


async def test_la_venta_del_mismo_producto_editada_se_reubica_en_su_fila(
    db_session: AsyncSession, tenant: Tenant
) -> None:
    """El mismo código en varias ventas: el código no identifica la venta; la fila
    entera sí."""
    from app.application.services.ingestion_import_service import insert_confirmed_data
    from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile

    def _venta(dia: int, total: str) -> dict[str, str]:
        return {
            "fecha": f"2026-02-{dia:02d}",
            "producto": "Coca 500",
            "codigo": "C500",
            "total": total,
        }

    def _summary(filas: list[dict[str, str]]) -> dict[str, Any]:
        return {
            "file_type": "spreadsheet",
            "inferred_type": "ventas",
            "multi_sheet": False,
            "headers": ["fecha", "producto", "codigo", "total"],
            "ventas_detectadas": filas,
            "row_count": len(filas),
        }

    filas = [_venta(1, "500"), _venta(2, "700"), _venta(3, "500")]
    tid, fid = tenant.tenant_id, uuid.uuid4()
    db_session.add(
        UploadedFile(
            id=fid, tenant_id=tid, original_filename="v.xlsx", s3_key="k",
            content_type="application/vnd.ms-excel", size_bytes=1, purpose="ventas",
            processing_status=PROCESSING_STATUS_DONE, parsed_summary_json=_summary(filas),
        )
    )
    await db_session.commit()
    await insert_confirmed_data(
        db_session, tid, _summary(filas), {"ventas": True}, uploaded_file_id=fid
    )
    await db_session.commit()
    venta = (
        await db_session.execute(
            select(SaleEntry).where(
                SaleEntry.source_upload_id == fid, SaleEntry.amount == Decimal("700")
            )
        )
    ).scalar_one()
    venta.has_user_edits = True
    venta.notes = "corregida"
    await db_session.commit()

    nuevo = [_venta(9, "900"), *filas]
    await reread_service.apply_reread(db_session, fid, tid, fresh_override=_summary(nuevo))
    await db_session.commit()
    vivas = (
        await db_session.execute(
            select(SaleEntry).where(
                SaleEntry.source_upload_id == fid, SaleEntry.voided_at.is_(None)
            )
        )
    ).scalars().all()
    assert sorted(str(Decimal(v.amount).quantize(Decimal("1"))) for v in vivas) == [
        "500", "500", "700", "900"
    ]
    await db_session.refresh(venta)
    assert venta.voided_at is None and venta.source_row_ref == _ref_de(tid, fid, 2)


# ── Multi-hoja: el contexto manda ───────────────────────────────────────────────


def _gasto(dia: int, detalle: str, monto: str) -> dict[str, str]:
    return {
        "fecha": f"2026-03-{dia:02d}",
        "producto": detalle,
        "monto": monto,
        "proveedor": "Proveedor Sur",
    }


def _hojas(hojas: dict[str, list[dict[str, str]]]) -> dict[str, Any]:
    gastos = [{**r, "__context__": ctx} for ctx, filas in hojas.items() for r in filas]
    return {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {
                "context_id": ctx,
                "entity_type": "expense",
                "source_kind": "sheet",
                "label": ctx,
                "headers": ["fecha", "producto", "monto", "proveedor"],
                "row_count": len(filas),
            }
            for ctx, filas in hojas.items()
        ],
        "ventas_detectadas": [],
        "gastos_detectados": gastos,
        "row_count": len(gastos),
    }


_HOJA = [_gasto(1, "Flete", "1000"), _gasto(2, "Luz", "2000"), _gasto(3, "Gas", "3000")]


async def _importar_hojas(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    summary: dict[str, Any],
    **kwargs: Any,
) -> uuid.UUID:
    from app.application.services.ingestion_import_service import insert_confirmed_data
    from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile

    fid = uuid.uuid4()
    session.add(
        UploadedFile(
            id=fid, tenant_id=tenant_id, original_filename="libro.xlsx", s3_key="k",
            content_type="application/vnd.ms-excel", size_bytes=1, purpose="gastos",
            processing_status=PROCESSING_STATUS_DONE,
            parsed_summary_json={"inferred_type": "mixed", "confirmed_fields": {"gastos": True}},
        )
    )
    await session.commit()
    await insert_confirmed_data(
        session, tenant_id, summary, {"gastos": True}, uploaded_file_id=fid, **kwargs
    )
    await session.commit()
    return fid


async def test_multi_hoja_con_columna_ignorada_reubica_dentro_de_su_hoja(
    db_session: AsyncSession, tenant: Tenant
) -> None:
    """La huella del importador es la de la fila SIN las columnas ignoradas: el
    plan las saca igual, con el mapeo que va a usar el reimport (el borrador)."""
    tid = tenant.tenant_id
    otra = [_gasto(9, "Luz", "2000")]  # gemela de la editada, en OTRA hoja
    ignorar = {"hoja:A": {"proveedor": "ignore"}, "hoja:B": {"proveedor": "ignore"}}
    fid = await _importar_hojas(
        db_session, tid, _hojas({"hoja:A": _HOJA, "hoja:B": otra}), context_mappings=ignorar
    )
    # Hay dos de 2000 (una por hoja): se edita la de la hoja A.
    rec = (
        await db_session.execute(
            select(ExpenseEntry).where(
                ExpenseEntry.source_upload_id == fid,
                ExpenseEntry.amount == Decimal("2000"),
                ExpenseEntry.transaction_date < datetime(2026, 3, 5),
            )
        )
    ).scalar_one()
    rec.has_user_edits = True
    rec.description = "corregido a mano"
    editado = rec.id
    await db_session.commit()

    run = DataRepairRun(
        tenant_id=tid,
        repair_type=reread_service.REPAIR_TYPE_REREAD,
        status="APPLYING",
        dry_run=False,
        details_json={
            "file_id": str(fid),
            "draft": {
                "column_mappings": [
                    {"context_id": c, "source_column": "proveedor", "target_field": "ignore"}
                    for c in ("hoja:A", "hoja:B")
                ]
            },
        },
    )
    db_session.add(run)
    await db_session.flush()
    nuevo = _hojas({"hoja:A": [_gasto(1, "Agua", "500"), *_HOJA], "hoja:B": otra})
    resultado = await reread_service.apply_reread(
        db_session, fid, tid, run=run, fresh_override=nuevo
    )
    await db_session.commit()

    movida = await db_session.get(ExpenseEntry, editado)
    assert movida is not None and movida.voided_at is None
    nueva_ref = reread_service._hash_anchor(reread_service._row_anchor(tid, fid, "hoja:A", 2))
    assert movida.source_row_ref == nueva_ref
    assert [r["id"] for r in resultado.reubicaciones] == [str(editado)]
    montos = sorted(
        str(Decimal(e.amount).quantize(Decimal("1")))
        for e in (
            await db_session.execute(
                select(ExpenseEntry).where(
                    ExpenseEntry.source_upload_id == fid, ExpenseEntry.voided_at.is_(None)
                )
            )
        ).scalars()
    )
    assert montos == ["1000", "2000", "2000", "3000", "500"]


async def test_un_contexto_que_cambia_de_id_no_se_reubica_y_frena_sin_poder_aceptarse(
    db_session: AsyncSession, tenant: Tenant
) -> None:
    """D0: la hoja vuelve con otro id. No hay contexto donde buscar la fila: queda
    como hoy, la edición volvería repetida, y eso no se acepta."""
    tid = tenant.tenant_id
    fid = await _importar_hojas(db_session, tid, _hojas({"hoja:Vieja": _HOJA}))
    await _editar(db_session, fid, "2000")
    nuevo = _hojas({"hoja:Nueva": [_gasto(1, "Agua", "500"), *_HOJA]})
    with pytest.raises(reread_service.RereadWouldLoseDataError) as frenado:
        await reread_service.apply_reread(
            db_session, fid, tid, fresh_override=nuevo, accept_data_loss=True
        )
    await db_session.rollback()
    # Con huella, la fila que entra en la hoja nueva ES su fila original.
    perdida = frenado.value.perdida
    assert perdida.edited_at_risk == 1
    assert perdida.edited_repeated_by_digest == 1
    assert "no se puede aplicar igual" in str(frenado.value)
    # Ni con el tope que pasa el worker al aceptar.
    with pytest.raises(reread_service.RereadWouldLoseDataError) as otra_vez:
        await reread_service.apply_reread(
            db_session, fid, tid, fresh_override=nuevo, accept_data_loss=True,
            tope_aceptado=perdida.as_dict(),
        )
    await db_session.rollback()
    assert otra_vez.value.perdida.edited_repeated_by_digest == 1


async def test_la_prueba_por_huella_saca_las_columnas_ignoradas(
    db_session: AsyncSession, tenant: Tenant
) -> None:
    """D0 con una columna ignorada: la huella fresca se calcula sin ella, como la
    escribió el importador. Si no, no casa y la repetición probada se degrada a
    estimada, que se puede aceptar: la edición quedaría duplicada."""
    tid = tenant.tenant_id
    fid = await _importar_hojas(
        db_session, tid, _hojas({"hoja:Vieja": _HOJA}),
        context_mappings={"hoja:Vieja": {"proveedor": "ignore"}},
    )
    await _editar(db_session, fid, "2000")
    run = DataRepairRun(
        tenant_id=tid,
        repair_type=reread_service.REPAIR_TYPE_REREAD,
        status="APPLYING",
        dry_run=False,
        details_json={
            "file_id": str(fid),
            "draft": {
                "column_mappings": [
                    {"context_id": "hoja:Nueva", "source_column": "proveedor",
                     "target_field": "ignore"}
                ]
            },
        },
    )
    db_session.add(run)
    await db_session.commit()
    # Otro proveedor en la hoja nueva: sólo casa si la columna ignorada no entra.
    nuevo = _hojas({"hoja:Nueva": [
        {**g, "proveedor": "Otro"} for g in [_gasto(1, "Agua", "500"), *_HOJA]
    ]})
    with pytest.raises(reread_service.RereadWouldLoseDataError) as frenado:
        await reread_service.apply_reread(
            db_session, fid, tid, run=run, fresh_override=nuevo, accept_data_loss=True
        )
    await db_session.rollback()
    assert frenado.value.perdida.edited_repeated_by_digest == 1


async def test_una_fila_igual_en_otra_hoja_es_otra_operacion_y_se_acepta(
    db_session: AsyncSession, tenant: Tenant
) -> None:
    """Revisión del #86: la edición sigue con su hoja en la lectura nueva, así que
    su fila original se busca SÓLO ahí (como el plan). Una fila idéntica que
    aparece en otra hoja es otra operación: estimada, se acepta."""
    tid = tenant.tenant_id
    fid = await _importar_hojas(
        db_session, tid, _hojas({"hoja:A": _HOJA, "hoja:B": [_gasto(9, "Agua", "500")]})
    )
    await _editar(db_session, fid, "2000")
    nuevo = _hojas({
        "hoja:A": [_HOJA[0], _gasto(2, "Luz", "2100"), _HOJA[2]],
        "hoja:B": [_gasto(9, "Agua", "500"), _gasto(2, "Luz", "2000")],
    })
    with pytest.raises(reread_service.RereadWouldLoseDataError) as frenado:
        await reread_service.apply_reread(db_session, fid, tid, fresh_override=nuevo)
    await db_session.rollback()
    perdida = frenado.value.perdida
    assert (perdida.edited_at_risk, perdida.edited_repeated_by_digest) == (1, 0)
    await reread_service.apply_reread(
        db_session, fid, tid, fresh_override=nuevo, accept_data_loss=True,
        tope_aceptado=perdida.as_dict(),
    )
    await db_session.commit()


# ── La pantalla: una edición repetida probada no se ofrece para aceptar ───────


@pytest.mark.parametrize("probada", [True, False], ids=["por-huella", "estimada"])
async def test_el_estado_del_run_frenado_por_una_edicion_es_un_error_con_el_motivo(
    client: Any,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
    probada: bool,
) -> None:
    """Probada por la huella: un frontend que no conoce el código nuevo lo trata
    como cualquier FAILED: deja de consultar y muestra `error`, que es el mensaje
    del servidor. Sin `data_loss`, ni el viejo ni el nuevo ofrecen «Aplicar
    igual». Estimada (dos de tres, sin huella): se ofrece aceptar, como antes."""
    import unittest.mock

    from app.jobs import reread_worker

    file = await _subir_e_importar(
        db_session, sample_tenant.tenant_id, _csv(_CUATRO), "text/csv", "gastos.csv"
    )
    fid = file.id
    _patch_s3(monkeypatch, _csv([_N, *_CUATRO]))
    pre = await client.post(f"/api/v1/ingestion/files/{fid}/reread/preview", headers=auth_headers)
    assert pre.status_code == 200, pre.text
    body = pre.json()
    run_id = uuid.UUID(body["run_id"])
    pedido = {"run_id": body["run_id"], "draft_version": body["draft_version"]}
    sin_worker = unittest.mock.patch(
        "app.application.services.reread_diagnostics_service.check_ingestion_workers_available",
        return_value=None,
    )
    with sin_worker, unittest.mock.patch.object(reread_worker.reread_apply, "delay"):
        r = await client.post(
            f"/api/v1/ingestion/files/{fid}/reread/apply", headers=auth_headers, json=pedido
        )
        assert r.status_code == 202, r.text
    run = await db_session.get(DataRepairRun, run_id)
    assert run is not None
    await db_session.refresh(run)
    foto = {"kind": "expense", "amount": "2200.00", "transaction_date": "2026-01-07",
            "description": "corregido a mano 2200"}
    reread_service.volver_a_confirmar(
        run,
        reread_service.PerdidaDeRelectura(
            edited_at_risk=1,
            edited_repeated_by_digest=int(probada),
            samples=[{"action": "edited_at_risk", "before": foto}],
        ),
    )
    await db_session.commit()

    retomado = await client.post(
        f"/api/v1/ingestion/files/{fid}/reread/preview", headers=auth_headers
    )
    assert retomado.status_code == 200, retomado.text
    assert (retomado.json()["data_loss"] is None) is probada

    estado = await client.get(
        f"/api/v1/ingestion/files/{fid}/reread/runs/{run_id}", headers=auth_headers
    )
    assert estado.status_code == 200, estado.text
    e = estado.json()
    assert e["status"] == "FAILED"
    assert "gasto del 2026-01-07 por $2200.00" in e["error"]
    if probada:
        assert e["error_code"] == reread_service.REREAD_EDITS_WOULD_REPEAT
        assert e["data_loss"] is None
        assert "no se puede aplicar igual" in e["error"]
    else:
        assert e["error_code"] == reread_service.REREAD_WOULD_LOSE_DATA
        assert e["data_loss"]["edited_at_risk"] == 1
        assert "confirmá si querés aplicarla igual" in e["error"]


@pytest.mark.parametrize(
    ("nuevo", "suelta"),
    [
        # La posición vieja de C (2) no tiene fila nueva: nadie re-registra su
        # huella al reimportar. El undo la devuelve, o la próxima relectura
        # importaría encima de la edición.
        pytest.param(_CUATRO[1:3], 2, id="se-acorta"),
        # La posición nueva de C (3) no la ocupaba nadie antes: el undo la suelta,
        # o la próxima relectura saltearía la fila que caiga ahí.
        pytest.param([_N, *_CUATRO[:3]], None, id="se-alarga"),
    ],
)
async def test_deshacer_una_reubicacion_deja_las_huellas_como_estaban(
    db_session: AsyncSession,
    tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
    nuevo: list[tuple[str, str, str]],
    suelta: int | None,
) -> None:
    tres = _CUATRO[:3]
    file = await _subir_e_importar(db_session, tenant.tenant_id, _csv(tres), "text/csv", "g.csv")
    tid, fid = tenant.tenant_id, file.id
    await _editar(db_session, fid, "2200")
    huellas_antes = await _huellas_del_archivo(db_session, tid, fid)
    _patch_s3(monkeypatch, _csv(nuevo))
    resultado = await reread_service.apply_reread(
        db_session, fid, tid, accept_data_loss=len(nuevo) < len(tres)
    )
    await db_session.commit()
    assert len(resultado.reubicaciones) == 1
    if suelta is not None:
        assert _ref_de(tid, fid, suelta) not in await _huellas_del_archivo(db_session, tid, fid)

    await reread_service.undo_reread(db_session, resultado.run_id, tid)
    await db_session.commit()
    assert await _huellas_del_archivo(db_session, tid, fid) == huellas_antes
    assert await _refs_vivas(db_session, fid) == huellas_antes


# ── Coberturas que pidió la revisión ────────────────────────────────────────────


def _venta_hoja(dia: int, producto: str, monto: str) -> dict[str, str]:
    return {"fecha": f"2026-04-{dia:02d}", "producto": producto, "monto": monto, "cliente": "Ana"}


def _hoja_de_ventas(ctx: str, filas: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {
                "context_id": ctx,
                "entity_type": "sale",
                "source_kind": "sheet",
                "label": ctx,
                "headers": ["fecha", "producto", "monto", "cliente"],
                "row_count": len(filas),
            }
        ],
        "ventas_detectadas": [{**r, "__context__": ctx} for r in filas],
        "gastos_detectados": [],
        "row_count": len(filas),
    }


async def test_multi_hoja_la_venta_editada_tiene_huella_y_se_reubica(
    db_session: AsyncSession, tenant: Tenant
) -> None:
    """La huella de una venta de un libro multihoja la escribe `_add_sale` (la
    forma de ASTERIA). Sin ella, la venta editada no se reubica y vuelve repetida."""
    from app.application.services.ingestion_import_service import insert_confirmed_data
    from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile

    tid, fid, ctx = tenant.tenant_id, uuid.uuid4(), "hoja:Ventas"
    filas = [_venta_hoja(1, "Vela", "1000"), _venta_hoja(2, "Jabon", "2000")]
    db_session.add(
        UploadedFile(
            id=fid, tenant_id=tid, original_filename="libro.xlsx", s3_key="k",
            content_type="application/vnd.ms-excel", size_bytes=1, purpose="ventas",
            processing_status=PROCESSING_STATUS_DONE,
            parsed_summary_json={"inferred_type": "mixed", "confirmed_fields": {"ventas": True}},
        )
    )
    await db_session.commit()
    await insert_confirmed_data(
        db_session, tid, _hoja_de_ventas(ctx, filas), {"ventas": True}, uploaded_file_id=fid
    )
    await db_session.commit()
    venta = (
        await db_session.execute(
            select(SaleEntry).where(
                SaleEntry.source_upload_id == fid, SaleEntry.amount == Decimal("2000")
            )
        )
    ).scalar_one()
    assert venta.source_row_digest, "la venta multihoja nace con la huella de su fila"
    venta.has_user_edits = True
    venta.notes = "corregida"
    editada = venta.id
    await db_session.commit()

    nuevo = _hoja_de_ventas(ctx, [_venta_hoja(1, "Vela", "500"), *filas])
    resultado = await reread_service.apply_reread(db_session, fid, tid, fresh_override=nuevo)
    await db_session.commit()
    assert [r["id"] for r in resultado.reubicaciones] == [str(editada)]
    movida = await db_session.get(SaleEntry, editada)
    assert movida is not None and movida.voided_at is None
    assert movida.source_row_ref == reread_service._hash_anchor(
        reread_service._row_anchor(tid, fid, ctx, 2)
    )
    vivas = (
        await db_session.execute(
            select(SaleEntry).where(
                SaleEntry.source_upload_id == fid, SaleEntry.voided_at.is_(None)
            )
        )
    ).scalars().all()
    assert sorted(str(Decimal(v.amount).quantize(Decimal("1"))) for v in vivas) == [
        "1000", "2000", "500"
    ]


async def test_la_edicion_no_se_reubica_sobre_la_fila_clasificada_desde_otros(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-O.2 reimporta por POSICIÓN la fila de un registro clasificado desde
    «Otros»; la edición cuya fila se corrió a esa posición no la toma (ni en el
    preview ni en el apply), o quedarían dos registros para una misma fila."""
    from app.persistence.models.unclassified_record import (
        UNCLASSIFIED_STATUS_IMPORTED,
        UnclassifiedRecord,
        unclassified_row_ref,
    )

    a, mala, c = _CUATRO[0], ("cuando pueda", "Pan Lactal", "800"), _CUATRO[2]
    file = await _subir_e_importar(
        db_session, tenant.tenant_id, _csv([a, mala, c]), "text/csv", "g.csv"
    )
    tid, fid = tenant.tenant_id, file.id
    pendiente = (
        await db_session.execute(
            select(UnclassifiedRecord).where(UnclassifiedRecord.uploaded_file_id == fid)
        )
    ).scalars().one()
    db_session.add(
        ExpenseEntry(
            tenant_id=tid, amount=Decimal("800"), transaction_date=datetime(2026, 1, 6),
            category="Otros", description="Pan Lactal", provenance="REAL",
            source_upload_id=fid, source_row_ref=unclassified_row_ref(pendiente.id),
        )
    )
    pendiente.status = UNCLASSIFIED_STATUS_IMPORTED
    await db_session.commit()
    editado = await _editar(db_session, fid, "2200")
    rec = await db_session.get(ExpenseEntry, editado)
    assert rec is not None
    ref_antes = rec.source_row_ref
    assert ref_antes == _ref_de(tid, fid, 2)

    # La fila de la edición pasa a la posición 1, la de la fila clasificada. En
    # el apply la huella de esa fila ya se soltó (F-O.2 la reimporta): lo único
    # que impide tomarla es `refs_de_otros`. En el preview la huella sigue
    # registrada y la frena también `vivas`.
    _patch_s3(monkeypatch, _csv([a, c, mala]))
    preview = await reread_service.preview_reread(db_session, fid, tid)
    assert (preview.to_lose, preview.edited_at_risk) == (0, 0)
    resultado = await reread_service.apply_reread(db_session, fid, tid)
    await db_session.commit()
    assert resultado.reubicaciones == []
    rec = await db_session.get(ExpenseEntry, editado)
    assert rec is not None and rec.voided_at is None and rec.source_row_ref == ref_antes
    # Lo que F-O.2 hace después con esa posición (la reimporta y reemplaza a la
    # clasificada, por POSICIÓN) es su límite declarado (c), previo a B·nuevo, y
    # no se afirma acá.
