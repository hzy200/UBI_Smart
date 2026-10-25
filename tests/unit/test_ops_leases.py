"""M4：租约强制释放与维护端点。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from coding_agent.web.auth import AuthUser
from coding_agent.web.dependencies import current_user
from coding_agent.web.routes.ops import OpsDeps, register_ops_routes


class _Leases:
    def __init__(self) -> None:
        self.released: list[str] = []

    def force_release(self, key: str) -> bool:
        self.released.append(key)
        return True

    def purge_expired(self) -> int:
        return 2


class _Idem:
    def purge_expired(self) -> int:
        return 3


async def _noop(*_args, **_kwargs) -> None:
    return None


def _client(leases: _Leases, idem: _Idem) -> TestClient:
    app = FastAPI()
    register_ops_routes(
        app,
        OpsDeps(
            tenant=lambda _user: None,
            sweep_expired=_noop,
            load_configs=lambda *_a: None,
            config_for=lambda *_a, **_k: None,
            deleted_sessions=set(),
            agent_client=None,
            agent_json=_noop,
            ops_runtime=lambda *_a: None,
            rate_limit=lambda _user: None,
            get_entry=lambda *_a: None,
            control_store=None,
            run_leases=leases,
            idempotency=idem,
        ),
    )
    app.dependency_overrides[current_user] = lambda: AuthUser(
        name="alice"
    )
    return TestClient(app)


def test_force_release_uses_user_scoped_key() -> None:
    leases = _Leases()
    client = _client(leases, _Idem())
    response = client.post("/api/ops/leases/t1/release")
    assert response.status_code == 200
    assert response.json() == {"released": True}
    assert leases.released == ["alice:t1"]


def test_purge_reports_counts() -> None:
    client = _client(_Leases(), _Idem())
    response = client.post("/api/ops/maintenance/purge")
    assert response.status_code == 200
    assert response.json() == {"leases": 2, "idempotency": 3}
