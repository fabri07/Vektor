from httpx import ASGITransport, AsyncClient

import pytest

import app.main as main_module
from app.main import create_app


@pytest.mark.asyncio
async def test_unhandled_exception_is_generic_outside_development(monkeypatch):
    monkeypatch.setattr(main_module.settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(main_module.settings, "DEBUG", False)

    app = create_app()

    async def boom() -> None:
        raise RuntimeError("sensitive production detail")

    app.add_api_route("/boom-prod", boom, methods=["GET"])

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as ac:
        response = await ac.get("/boom-prod")

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal Server Error"}


@pytest.mark.asyncio
async def test_unhandled_exception_is_detailed_in_development(monkeypatch):
    monkeypatch.setattr(main_module.settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(main_module.settings, "DEBUG", True)

    app = create_app()

    async def boom() -> None:
        raise RuntimeError("development detail")

    app.add_api_route("/boom-dev", boom, methods=["GET"])

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as ac:
        response = await ac.get("/boom-dev")

    assert response.status_code == 500
    assert response.json()["detail"] == "[DEBUG] RuntimeError: development detail"
