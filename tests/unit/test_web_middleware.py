"""请求体上限：工作区上传走单独上限，其余端点保持原限制。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ubismart.web.middleware import BodySizeLimitMiddleware


def _client() -> TestClient:
    app = FastAPI()

    @app.post("/api/workspaces/x/uploads")
    async def workspace_upload() -> dict:
        return {"ok": True}

    @app.post("/api/other/uploads")
    async def other_upload() -> dict:
        return {"ok": True}

    @app.post("/api/normal")
    async def normal() -> dict:
        return {"ok": True}

    app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=10,
        upload_max_bytes=20,
        workspace_upload_max_bytes=100,
    )
    return TestClient(app)


def test_workspace_upload_uses_large_limit() -> None:
    response = _client().post(
        "/api/workspaces/x/uploads", content=b"x" * 50
    )
    assert response.status_code == 200


def test_other_upload_uses_attachment_limit() -> None:
    response = _client().post("/api/other/uploads", content=b"x" * 50)
    assert response.status_code == 413


def test_normal_endpoint_uses_base_limit() -> None:
    response = _client().post("/api/normal", content=b"x" * 50)
    assert response.status_code == 413
