"""SSE 并发冒烟：同进程 A3 + 假 runtime，验证多路流不会互相拖死。

不是容量基准；给 CI 一个可阻断的并发下限（默认 20 路 × 200 token）。
真实 WSL/模型下的 soak 仍需独立环境执行。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time

import httpx

from coding_agent import agent_server
from coding_agent.agent_protocol import AgentContext
from coding_agent.config import Settings
from coding_agent.events import AssistantToken, RunFinished, RunStarted


class _FakeRuntime:
    def __init__(self, *args, **kwargs) -> None:
        self.tokens = int(kwargs.pop("_tokens", 200))

    async def run(self, prompt: str, *, thread_id: str):
        yield RunStarted(thread_id=thread_id)
        for index in range(self.tokens):
            yield AssistantToken(node="respond", text=f"t{index} ")
        yield RunFinished(thread_id=thread_id, answer="done")

    async def resume(self, thread_id: str, decisions: dict[str, bool]):
        yield RunFinished(thread_id=thread_id, answer="resumed")

    async def aclose(self) -> None:
        return None


def _app(tokens: int, directory: str):
    settings = Settings(
        _env_file=None,
        audit_dir=directory,
        checkpoint_path=":memory:",
        deepseek_api_key="sk-test",
        wsl_workspace="/tmp/ws",
        agent_data_root=directory,
    )

    def factory(*args, **kwargs):
        kwargs["_tokens"] = tokens
        return _FakeRuntime(*args, **kwargs)

    return agent_server.create_agent_app(
        settings, token="tok", runtime_factory=factory
    )


async def _run(clients: int, tokens: int, max_seconds: float) -> dict:
    with tempfile.TemporaryDirectory(prefix="sse-load-") as directory:
        app = _app(tokens, directory)
        transport = httpx.ASGITransport(app=app)
        headers = {
            "Authorization": "Bearer tok",
            "X-Agent-Context": AgentContext(workspace="/tmp/ws").to_header(),
        }

        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=30
        ) as client:

            async def one(index: int):
                async with client.stream(
                    "POST",
                    "/v1/runs",
                    headers=headers,
                    json={
                        "prompt": "hi",
                        "thread_id": f"load-{index}",
                    },
                ) as response:
                    body = await response.aread()
                    return response.status_code, body.count(b"data: ")

            started = time.perf_counter()
            results = await asyncio.gather(
                *(one(index) for index in range(clients))
            )
            elapsed = time.perf_counter() - started
    ok = (
        all(status == 200 and frames >= 3 for status, frames in results)
        and elapsed <= max_seconds
    )
    return {
        "ok": ok,
        "clients": clients,
        "tokens": tokens,
        "elapsed_s": round(elapsed, 3),
        "max_seconds": max_seconds,
        "min_frames": min((frames for _, frames in results), default=0),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clients", type=int, default=20)
    parser.add_argument("--tokens", type=int, default=200)
    parser.add_argument("--max-seconds", type=float, default=15.0)
    args = parser.parse_args()
    result = asyncio.run(
        _run(
            max(1, int(args.clients)),
            max(1, int(args.tokens)),
            max(1.0, float(args.max_seconds)),
        )
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
