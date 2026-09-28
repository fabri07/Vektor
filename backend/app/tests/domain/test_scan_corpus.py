"""El corpus compartido de escaneo, contra la implementación del servidor.

El MISMO archivo lo lee el test del puerto TypeScript que usa la caja sin red
(`frontend/src/lib/pos/__tests__/scan_code_local.test.ts`). Es la única
compuerta contra que las dos implementaciones se separen: si divergen, la caja
offline puede cobrar un producto distinto del que el servidor habría resuelto,
y nadie se entera hasta el arqueo.
"""

import json
from pathlib import Path

import pytest

from app.domain.scan_code import TipoDeCodigo, clasificar, variantes_gtin

_CORPUS = Path(__file__).resolve().parents[3].parent / "shared" / "pos" / "scan_corpus.json"


def _casos() -> list[dict]:
    datos = json.loads(_CORPUS.read_text(encoding="utf-8"))
    assert datos["casos"], "el corpus no puede estar vacío"
    return list(datos["casos"])


def test_el_corpus_existe_donde_lo_busca_el_frontend() -> None:
    """Si se mueve, el test del puerto TS deja de compararse contra nada."""
    assert _CORPUS.is_file(), _CORPUS


@pytest.mark.parametrize("caso", _casos(), ids=lambda c: c["crudo"] or "(vacío)")
def test_clasifica_como_dice_el_corpus(caso: dict) -> None:
    resultado = clasificar(caso["crudo"])
    assert resultado.tipo.value == caso["tipo"], caso["por_que"]
    assert resultado.valor == caso["valor"], caso["por_que"]
    assert resultado.aprendible is caso["aprendible"], caso["por_que"]
    esperadas = caso["variantes"]
    if resultado.tipo in (TipoDeCodigo.GTIN, TipoDeCodigo.SCALE):
        assert list(variantes_gtin(resultado.valor)) == esperadas, caso["por_que"]
    else:
        assert esperadas == [], "sólo un GTIN tiene variantes"
