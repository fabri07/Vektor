"""Compuerta de tamaño de ``CLAUDE.md``.

Claude Code carga ``CLAUDE.md`` entero en cada sesión y avisa que degrada el
rendimiento por encima de 40.000 caracteres. El archivo llegó a 204.000 porque
cada bloque le agregaba su bullet de detalle; el detalle vive ahora en
``docs/claude/`` (se lee a demanda) y acá queda sólo el índice.
"""

from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_LIMITE = 40_000


def test_claude_md_bajo_el_limite() -> None:
    texto = (_REPO / "CLAUDE.md").read_text(encoding="utf-8")
    assert len(texto) < _LIMITE, (
        f"CLAUDE.md tiene {len(texto)} caracteres (límite {_LIMITE}). "
        "El detalle va en el archivo de su dominio en docs/claude/, no acá."
    )


def test_los_archivos_de_dominio_del_indice_existen() -> None:
    texto = (_REPO / "CLAUDE.md").read_text(encoding="utf-8")
    dominios = (
        "ingesta", "pos", "pos-offline", "suscripciones", "maestros", "plataforma", "agentes",
    )
    for nombre in dominios:
        ruta = f"docs/claude/{nombre}.md"
        assert ruta in texto, f"CLAUDE.md no enlaza {ruta}"
        assert (_REPO / ruta).is_file(), f"falta {ruta}"
