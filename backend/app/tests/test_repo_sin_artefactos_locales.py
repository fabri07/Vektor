"""El repo no versiona entornos virtuales ni archivos subidos por clientes.

``backend/.venv312`` (10.646 archivos, un virtualenv de macOS) estuvo
versionado desde 2026-03-10 hasta 2026-10. ``.gitignore`` ya tenía ``.venv*/``,
pero un patrón de ignore no tiene efecto sobre archivos que ya están
trackeados, y el ``.dockerignore`` sólo excluía ``.venv/``: el venv viajaba en
la imagen de producción. Un primer intento de sacarlo (``af8fd208``) se
revirtió al día siguiente. Este test es lo que impide que vuelva a entrar.

Lo mismo para ``.local_uploads/``: es el almacenamiento local de ``S3Client``
y lo que hay ahí son archivos de clientes.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_PROHIBIDOS = (".venv", "venv", ".local_uploads")


def _archivos_trackeados() -> list[str]:
    if shutil.which("git") is None or not (_REPO / ".git").exists():
        pytest.skip("sin checkout de git (p. ej. dentro de la imagen Docker)")
    salida = subprocess.run(
        ["git", "ls-files"], cwd=_REPO, capture_output=True, text=True, check=True
    )
    return salida.stdout.splitlines()


def test_no_hay_venvs_ni_uploads_versionados() -> None:
    culpables = [
        ruta
        for ruta in _archivos_trackeados()
        if any(parte.startswith(_PROHIBIDOS) for parte in Path(ruta).parts[:-1])
    ]
    assert not culpables, (
        f"{len(culpables)} archivos versionados bajo un venv o .local_uploads, "
        f"p. ej. {culpables[:3]}. Sacarlos con `git rm -r --cached <carpeta>`."
    )
