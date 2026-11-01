"""Panel SUPERADMIN: `GET /admin/sentry/issues` (lectura de Sentry, mockeada)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import sentry_issues_service as svc
from app.config.settings import get_settings
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.utils.security import create_access_token, hash_password

_URL = "/api/v1/admin/sentry/issues"
_TOKEN = "sntrys_token_secreto_de_prueba"


async def _headers(db: AsyncSession, tenant: Tenant, rol: str) -> dict[str, str]:
    user = User(
        user_id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        email=f"{rol.lower()}-{uuid.uuid4().hex[:6]}@vektor.app",
        full_name=f"Usuario {rol}",
        password_hash=hash_password("Secure789"),
        role_code=rol,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    token = create_access_token(
        {"sub": str(user.user_id), "tenant_id": str(tenant.tenant_id), "role_code": rol}
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def superadmin_headers(db_session: AsyncSession, sample_tenant: Tenant) -> dict[str, str]:
    return await _headers(db_session, sample_tenant, "SUPERADMIN")


@pytest.fixture
def configurado(monkeypatch: pytest.MonkeyPatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "SENTRY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setattr(s, "SENTRY_ORG", "vektor-g9")
    monkeypatch.setattr(s, "SENTRY_PROJECTS", "python-fastapi,javascript-nextjs")


def _issue(
    n: int,
    proyecto: str = "python-fastapi",
    *,
    ultimo: str = "2026-10-30T12:00:00Z",
    service: str | None = "worker",
) -> dict[str, Any]:
    return {
        "id": str(n),
        "shortId": f"{proyecto.upper()}-{n}",
        "title": f"Error {n}",
        "project": {"slug": proyecto},
        "level": "error",
        "count": "7",
        "firstSeen": "2026-10-20T12:00:00Z",
        "lastSeen": ultimo,
        "permalink": f"https://vektor-g9.sentry.io/issues/{n}/",
        "tags": [{"key": "service", "value": service}] if service else [],
    }


def _sentry_falso(
    por_proyecto: dict[str, Any], visto: list[httpx.Request] | None = None
) -> Callable[[], httpx.AsyncClient]:
    def manejar(request: httpx.Request) -> httpx.Response:
        if visto is not None:
            visto.append(request)
        proyecto = request.url.path.rstrip("/").split("/")[-2]
        valor = por_proyecto[proyecto]
        if isinstance(valor, httpx.Response):
            return valor
        if isinstance(valor, Exception):
            raise valor
        return httpx.Response(200, json=valor)

    def fabrica() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="https://us.sentry.io",
            headers={"Authorization": f"Bearer {get_settings().SENTRY_AUTH_TOKEN}"},
            transport=httpx.MockTransport(manejar),
        )

    return fabrica


# ── Autorización ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("rol", ["OWNER", "ADMIN", "VIEWER", "CASHIER"])
async def test_solo_el_superadmin_entra(
    client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant, rol: str
) -> None:
    headers = await _headers(db_session, sample_tenant, rol)
    assert (await client.get(_URL, headers=headers)).status_code == 403


async def test_sin_token_es_401(client: AsyncClient) -> None:
    assert (await client.get(_URL)).status_code == 401


# ── Configuración ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("faltante", ["SENTRY_AUTH_TOKEN", "SENTRY_ORG"])
async def test_sin_configurar_no_es_un_error_ni_dice_cero_errores(
    client: AsyncClient,
    superadmin_headers: dict[str, str],
    configurado: None,
    monkeypatch: pytest.MonkeyPatch,
    faltante: str,
) -> None:
    monkeypatch.setattr(get_settings(), faltante, "")
    llamadas: list[httpx.Request] = []
    monkeypatch.setattr(svc, "_cliente", _sentry_falso({}, llamadas))

    r = await client.get(_URL, headers=superadmin_headers)

    assert r.status_code == 200
    assert r.json()["configured"] is False
    assert r.json()["issues"] == []
    assert llamadas == []  # sin credenciales ni siquiera se intenta


# ── Lectura ────────────────────────────────────────────────────────────────────


async def test_solo_consulta_y_muestra_los_proyectos_permitidos(
    client: AsyncClient,
    superadmin_headers: dict[str, str],
    configurado: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    visto: list[httpx.Request] = []
    monkeypatch.setattr(
        svc,
        "_cliente",
        _sentry_falso(
            {
                # Aunque la API devolviera un issue de la otra app, no se muestra.
                "python-fastapi": [_issue(1), _issue(2, "app-gim")],
                "javascript-nextjs": [_issue(3, "javascript-nextjs", service=None)],
            },
            visto,
        ),
    )

    r = await client.get(_URL, headers=superadmin_headers)

    assert r.status_code == 200
    cuerpo = r.json()
    assert {i["project"] for i in cuerpo["issues"]} == {"python-fastapi", "javascript-nextjs"}
    assert all("GIM" not in i["short_id"].upper() for i in cuerpo["issues"])
    assert cuerpo["total"] == 2
    # Nunca se le pidió nada a la ruta de app-gim.
    assert all("app-gim" not in str(req.url) for req in visto)
    assert {req.url.path for req in visto} == {
        "/api/0/projects/vektor-g9/python-fastapi/issues/",
        "/api/0/projects/vektor-g9/javascript-nextjs/issues/",
    }
    # Se manda el token como Bearer y es de sólo lectura (GET).
    assert all(req.method == "GET" for req in visto)
    assert all(req.headers["Authorization"] == f"Bearer {_TOKEN}" for req in visto)


async def test_mapea_campos_y_ordena_por_ultimo_visto(
    client: AsyncClient,
    superadmin_headers: dict[str, str],
    configurado: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        svc,
        "_cliente",
        _sentry_falso(
            {
                "python-fastapi": [
                    _issue(1, ultimo="2026-10-01T00:00:00Z"),
                    _issue(2, ultimo="2026-10-30T00:00:00Z", service="api"),
                ],
                "javascript-nextjs": [],
            }
        ),
    )

    issues = (await client.get(_URL, headers=superadmin_headers)).json()["issues"]

    assert [i["id"] for i in issues] == ["2", "1"]
    assert issues[0]["service"] == "api"
    assert issues[0]["events"] == 7
    assert issues[0]["url"] == "https://vektor-g9.sentry.io/issues/2/"


async def test_la_lista_se_recorta_pero_informa_el_total(
    client: AsyncClient,
    superadmin_headers: dict[str, str],
    configurado: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        svc,
        "_cliente",
        _sentry_falso(
            {"python-fastapi": [_issue(n) for n in range(40)], "javascript-nextjs": []}
        ),
    )

    cuerpo = (await client.get(_URL, headers=superadmin_headers)).json()

    assert len(cuerpo["issues"]) == svc.MAX_ISSUES
    assert cuerpo["total"] == 40


async def test_segunda_consulta_sale_del_cache(
    client: AsyncClient,
    superadmin_headers: dict[str, str],
    configurado: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    visto: list[httpx.Request] = []
    monkeypatch.setattr(
        svc,
        "_cliente",
        _sentry_falso({"python-fastapi": [_issue(1)], "javascript-nextjs": []}, visto),
    )

    primero = await client.get(_URL, headers=superadmin_headers)
    llamadas_primera = len(visto)
    segundo = await client.get(_URL, headers=superadmin_headers)

    assert llamadas_primera == 2
    assert len(visto) == llamadas_primera
    assert segundo.json() == primero.json()


# ── Fallos de Sentry ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("falla", "codigo"),
    [
        (httpx.Response(401, json={"detail": "bad token"}), "SENTRY_TOKEN_REJECTED"),
        (httpx.Response(403, json={"detail": "forbidden"}), "SENTRY_TOKEN_REJECTED"),
        (httpx.Response(500, text="boom"), "SENTRY_BAD_RESPONSE"),
        (httpx.Response(200, json={"no": "es una lista"}), "SENTRY_BAD_RESPONSE"),
        (httpx.ConnectError("sin red"), "SENTRY_UNREACHABLE"),
    ],
)
async def test_un_fallo_de_sentry_es_502_con_codigo_y_sin_filtrar_el_token(
    client: AsyncClient,
    superadmin_headers: dict[str, str],
    configurado: None,
    monkeypatch: pytest.MonkeyPatch,
    falla: Any,
    codigo: str,
) -> None:
    monkeypatch.setattr(
        svc, "_cliente", _sentry_falso({"python-fastapi": falla, "javascript-nextjs": []})
    )

    r = await client.get(_URL, headers=superadmin_headers)

    assert r.status_code == 502
    assert r.json()["detail"]["code"] == codigo
    assert _TOKEN not in r.text
