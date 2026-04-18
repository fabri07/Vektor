"""Catálogo de apps Google Workspace soportadas por Véktor."""

from __future__ import annotations

from dataclasses import dataclass


BASE_SCOPES = [
    "openid",
    "email",
    "profile",
]


@dataclass(frozen=True)
class GoogleWorkspaceApp:
    id: str
    label: str
    description: str
    available: bool
    required_scopes: tuple[str, ...]


GOOGLE_WORKSPACE_APPS: dict[str, GoogleWorkspaceApp] = {
    "gmail": GoogleWorkspaceApp(
        id="gmail",
        label="Gmail",
        description="Correos de proveedores y borradores aprobados.",
        available=True,
        required_scopes=(
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.compose",
        ),
    ),
    "sheets": GoogleWorkspaceApp(
        id="sheets",
        label="Google Sheets",
        description="Lectura de hojas para importar ventas, gastos y compras.",
        available=True,
        required_scopes=("https://www.googleapis.com/auth/spreadsheets.readonly",),
    ),
    "drive": GoogleWorkspaceApp(
        id="drive",
        label="Google Drive",
        description="Listado de archivos compatibles como origen de datos.",
        available=True,
        required_scopes=("https://www.googleapis.com/auth/drive.readonly",),
    ),
    "docs": GoogleWorkspaceApp(
        id="docs",
        label="Google Docs",
        description="Reportes ejecutivos exportables a documentos.",
        available=True,
        required_scopes=("https://www.googleapis.com/auth/documents.readonly",),
    ),
    "photos": GoogleWorkspaceApp(
        id="photos",
        label="Google Fotos",
        description="Fotos del negocio para contexto visual.",
        available=True,
        required_scopes=("https://www.googleapis.com/auth/photoslibrary.readonly",),
    ),
}

DEFAULT_GOOGLE_APP_IDS = ["gmail"]


def normalize_app_ids(app_ids: list[str] | None) -> list[str]:
    """Filtra duplicados, valida apps disponibles y aplica default Gmail."""
    requested = app_ids or DEFAULT_GOOGLE_APP_IDS
    normalized: list[str] = []
    for raw_id in requested:
        app_id = raw_id.strip().lower()
        app = GOOGLE_WORKSPACE_APPS.get(app_id)
        if app is None or not app.available:
            continue
        if app_id not in normalized:
            normalized.append(app_id)
    return normalized or DEFAULT_GOOGLE_APP_IDS


def scopes_for_apps(app_ids: list[str] | None) -> list[str]:
    scopes = list(BASE_SCOPES)
    for app_id in normalize_app_ids(app_ids):
        for scope in GOOGLE_WORKSPACE_APPS[app_id].required_scopes:
            if scope not in scopes:
                scopes.append(scope)
    return scopes


def merge_scopes(existing: list[str] | None, incoming: list[str] | None) -> list[str]:
    merged: list[str] = []
    for scope in [*(existing or []), *(incoming or [])]:
        if scope and scope not in merged:
            merged.append(scope)
    return merged
