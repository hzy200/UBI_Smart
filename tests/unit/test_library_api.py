"""A2 资料库路由的本地同进程路径。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.unit.test_library_store import MemoryFs

from ubismart.agent_protocol import AgentContext
from ubismart.config import Settings
from ubismart.library.store import LibraryStore
from ubismart.servers.agent_server_routes.library import (
    LibraryDeps as AgentLibraryDeps,
)
from ubismart.servers.agent_server_routes.library import (
    register_library_routes as register_agent_library_routes,
)
from ubismart.web.auth import AuthUser
from ubismart.web.dependencies import current_user
from ubismart.web.routes.library import LibraryDeps, register_library_routes


class _Runtime:
    def __init__(self, store: LibraryStore) -> None:
        self.store = store

    def library_ingest(
        self, name: str, data: bytes, *, content_type: str = ""
    ) -> dict:
        return self.store.ingest(name, data, content_type=content_type)

    def library_start_ingest(
        self, name: str, data: bytes, *, content_type: str = ""
    ) -> dict:
        started = self.store.start_ingest(
            name, data, content_type=content_type
        )
        job = started.get("job")
        if isinstance(job, dict):
            finished = self.store.process_job(str(job["job_id"]))
            item = self.store.get(str(finished.get("item_id") or ""))
            return {
                "item": item or started.get("item"),
                "job": finished,
                "duplicate": False,
            }
        return started

    def library_process_job(self, job_id: str) -> dict:
        return self.store.process_job(job_id)

    def library_get_job(self, job_id: str) -> dict:
        job = self.store.get_job(job_id)
        if job is None:
            raise RuntimeError("missing")
        return job

    def library_list_jobs(self, *, limit: int = 50) -> dict:
        return {"jobs": self.store.list_jobs(limit=limit)}

    def library_cancel_job(self, job_id: str) -> dict:
        return self.store.cancel_job(job_id)

    def library_start_reindex(self, item_id: str) -> dict:
        started = self.store.start_reindex(item_id)
        job = started.get("job")
        if isinstance(job, dict):
            finished = self.store.process_job(str(job["job_id"]))
            item = self.store.get(item_id)
            return {"item": item, "job": finished}
        return started

    def library_start_reindex_all(self) -> dict:
        started = self.store.start_reindex_all()
        job = started.get("job")
        if isinstance(job, dict):
            return {
                "job": self.store.process_job(str(job["job_id"])),
                "count": started.get("count", 0),
            }
        return started

    def library_list(self, **kwargs: Any) -> dict:
        query = str(kwargs.pop("q", "") or "")
        return {
            **self.store.list_page(query=query, **kwargs),
            "usage": self.store.stats(),
        }

    def library_get(self, item_id: str) -> dict:
        item = self.store.get(item_id)
        if item is None:
            raise RuntimeError("missing")
        return item

    def library_delete(self, item_id: str, *, purge: bool = False) -> dict:
        return self.store.delete(item_id, purge=purge)

    def library_reindex(self, item_id: str) -> dict:
        return self.store.reindex(item_id)

    def library_detail(
        self, item_id: str, *, chunk_limit: int = 20
    ) -> dict:
        detail = self.store.get_detail(item_id, chunk_limit=chunk_limit)
        if detail is None:
            raise RuntimeError("missing")
        return detail

    def library_set_tags(self, item_id: str, tags: list[str]) -> dict:
        return self.store.set_tags(item_id, tags)

    def library_search(
        self,
        query: str,
        *,
        top_k: int = 8,
        mode: str = "",
        tags: list[str] | None = None,
    ) -> list[dict]:
        return self.store.search(
            query, top_k=top_k, mode=mode, tags=tags
        )

    async def aclose(self) -> None:
        return None


def _client(settings: Settings) -> tuple[TestClient, LibraryStore]:
    fs = MemoryFs("/lib")
    store = LibraryStore(fs, "/lib")
    runtime = _Runtime(store)
    app = FastAPI()
    app.dependency_overrides[current_user] = lambda: AuthUser(name="alice")

    async def _not_used(*args: Any, **kwargs: Any) -> dict:
        raise AssertionError("remote path should not be used")

    register_library_routes(
        app,
        LibraryDeps(
            settings=settings,
            tenant=lambda user: SimpleNamespace(
                user=user.name, library_dir="/lib", workspace="/ws"
            ),
            rate_limit=lambda user: None,
            agent_client=None,
            agent_library_upload=_not_used,
            agent_library_json=_not_used,
            ops_runtime=lambda tenant, config: runtime,
        ),
    )
    return TestClient(app), store


def test_upload_list_get_delete_local() -> None:
    settings = Settings(
        _env_file=None, library_enabled=True, library_root="/lib"
    )
    client, _ = _client(settings)

    response = client.post(
        "/api/library/items",
        files={"files": ("a.md", b"# title\n\nneedle", "text/markdown")},
    )
    assert response.status_code == 202
    item = response.json()["items"][0]
    assert item["status"] == "ready"
    item_id = item["item_id"]

    listing = client.get("/api/library/items")
    assert listing.status_code == 200
    assert listing.json()["items"][0]["item_id"] == item_id
    assert listing.json()["usage"]["items"] == 1

    detail = client.get(f"/api/library/items/{item_id}")
    assert detail.status_code == 200
    assert detail.json()["item_id"] == item_id

    tagged = client.patch(
        f"/api/library/items/{item_id}", json={"tags": ["规范"]}
    )
    assert tagged.status_code == 200
    assert tagged.json()["tags"] == ["规范"]

    item_detail = client.get(f"/api/library/items/{item_id}/detail")
    assert item_detail.status_code == 200
    assert item_detail.json()["chunks"]

    search = client.get("/api/library/search?q=needle")
    assert search.status_code == 200
    assert search.json()["hits"]

    jobs = client.get("/api/library/jobs")
    assert jobs.status_code == 200
    assert jobs.json()["jobs"]

    reindex_all = client.post("/api/library/reindex-all")
    assert reindex_all.status_code == 202
    assert reindex_all.json()["job"]["total"] == 1

    deleted = client.delete(f"/api/library/items/{item_id}")
    assert deleted.status_code == 200
    assert deleted.json()["status"] == "deleted"


def test_disabled_library_returns_404() -> None:
    settings = Settings(_env_file=None, library_enabled=False)
    client, _ = _client(settings)
    response = client.get("/api/library/items")
    assert response.status_code == 404


def test_a3_route_upload_and_list() -> None:
    fs = MemoryFs("/lib")
    store = LibraryStore(fs, "/lib")
    runtime = _Runtime(store)
    app = FastAPI()
    close_calls: list[Any] = []

    async def _close(item: Any) -> None:
        close_calls.append(item)

    register_agent_library_routes(
        app,
        AgentLibraryDeps(
            context=lambda request: AgentContext(user="alice"),
            new_runtime=lambda context: runtime,
            close_runtime=_close,
        ),
    )
    client = TestClient(app)

    response = client.post(
        "/v1/library/items",
        files={"file": ("a.xlsx", b"not an xlsx", "application/vnd.ms-excel")},
    )
    assert response.status_code == 202
    payload = response.json()
    assert payload["items"][0]["status"] == "failed"
    assert payload["jobs"]
    assert client.get("/v1/library/items").status_code == 200
    assert close_calls
