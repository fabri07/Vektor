from httpx import ASGITransport, AsyncClient

import app.main as main_module
from app.main import create_app


async def test_request_id_in_response_headers():
    app = create_app()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        response = await ac.get("/health", headers={"X-Request-ID": "req-test-123"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req-test-123"


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
