from fastapi.testclient import TestClient

from app.main import app


def test_health() -> None:
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_missing_session_returns_404() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/sessions/missing/messages")

    assert response.status_code == 404
    assert response.json() == {"detail": "Session not found"}


def test_chat_rejects_empty_message() -> None:
    with TestClient(app) as client:
        response = client.post("/api/v1/chat", json={"message": ""})

    assert response.status_code == 422
