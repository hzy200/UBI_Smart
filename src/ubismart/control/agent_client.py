"""A2 → A3 的内部客户端（Phase B4）。

A2 不执行图、不持有 runtime；它把运行生命周期请求转发给独立 Agent Server。
上下文（租户/工作区/权限/模型）放在签名受控的内部请求头里，Agent Server
只信任持有内部 token 的 A2。

SSE 保持原始字节透传，避免 A2 重新序列化事件、破坏 `id:`/`data:` 帧与
`Last-Event-ID` 语义。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

import httpx

from ubismart.agent_protocol import (
    AgentContext,
    AgentMeta,
    AgentRunRequest,
    sign_context,
)
from ubismart.platform.request_context import REQUEST_ID_HEADER, current_request_id


class AgentClientError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail or f"Agent Server HTTP {status_code}")
        self.status_code = status_code
        self.detail = detail


@dataclass
class AgentStream:
    """一个已打开的 Agent SSE 响应；用完必须 close。"""

    response: httpx.Response
    client: httpx.AsyncClient

    @property
    def status_code(self) -> int:
        return int(self.response.status_code)

    @property
    def headers(self) -> dict[str, str]:
        return dict(self.response.headers)

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self.response.aiter_raw():
                yield chunk
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        try:
            await self.response.aclose()
        finally:
            await self.client.aclose()


class AgentClient(Protocol):
    async def probe(self) -> AgentMeta: ...
    async def open_run(self, request: AgentRunRequest) -> AgentStream: ...
    async def open_stream(
        self, context: AgentContext, thread_id: str, *, after: int = 0
    ) -> AgentStream: ...
    async def open_resume(
        self,
        context: AgentContext,
        thread_id: str,
        decisions: dict[str, bool],
    ) -> AgentStream: ...
    async def open_resume_steering(
        self, context: AgentContext, thread_id: str
    ) -> AgentStream: ...
    async def steer(
        self,
        context: AgentContext,
        thread_id: str,
        *,
        action: str,
        text: str = "",
        role: str = "explore",
    ) -> dict: ...
    async def approvals(
        self, context: AgentContext, thread_id: str
    ) -> dict: ...
    async def status(
        self, context: AgentContext, thread_id: str
    ) -> dict: ...
    async def history(
        self,
        context: AgentContext,
        thread_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict: ...
    async def json_request(
        self,
        context: AgentContext,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
    ) -> dict: ...
    async def raw_request(
        self,
        context: AgentContext,
        path: str,
        *,
        params: dict | None = None,
        range_header: str = "",
        if_none_match: str = "",
    ) -> tuple[int, dict[str, str], bytes]: ...
    async def register_host_workspace(
        self, context: AgentContext
    ) -> dict: ...
    async def upload_files(
        self,
        context: AgentContext,
        thread_id: str,
        files: list[tuple[str, bytes, str]],
    ) -> dict: ...
    async def upload_library_file(
        self,
        context: AgentContext,
        name: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> dict: ...
    async def workspace_import_start(
        self,
        context: AgentContext,
        *,
        source_path: str,
        mode: str = "copy",
        quota_bytes: int = 0,
        max_files: int = 200_000,
        max_bytes: int = 0,
    ) -> dict: ...
    async def workspace_import_status(
        self, context: AgentContext, job_id: str
    ) -> dict: ...
    async def workspace_import_cancel(
        self, context: AgentContext, job_id: str
    ) -> dict: ...
    async def workspace_upload_start(
        self,
        context: AgentContext,
        *,
        file_path: str,
        filename: str,
        content_type: str = "application/octet-stream",
        quota_bytes: int = 0,
        max_files: int = 200_000,
        max_bytes: int = 0,
    ) -> dict: ...
    async def workspace_upload_status(
        self, context: AgentContext, job_id: str
    ) -> dict: ...
    async def workspace_upload_cancel(
        self, context: AgentContext, job_id: str
    ) -> dict: ...
    async def workspace_rollback(
        self,
        context: AgentContext,
        *,
        revision_id: str,
        git_commit: str,
    ) -> dict: ...
    async def workspace_restore(
        self,
        context: AgentContext,
        *,
        revision_id: str,
        archive_path: str,
        archive_sha256: str = "",
        git_commit: str = "",
    ) -> dict: ...
    async def workspace_archive_delete(
        self, context: AgentContext, archive_path: str
    ) -> dict: ...
    async def aclose(self) -> None: ...


class HttpAgentClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        hmac_key: str = "",
    ) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.token = str(token or "")
        if not self.base_url:
            raise ValueError("Agent Server URL 为空")
        if not self.token:
            raise ValueError("Agent Server 内部 token 为空")
        self.timeout = timeout
        self.transport = transport
        self.hmac_key = str(hmac_key or "")

    def _headers(self, context: AgentContext) -> dict[str, str]:
        header = context.to_header()
        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-Agent-Context": header,
        }
        signature = sign_context(header, self.hmac_key)
        if signature:
            headers["X-Agent-Signature"] = signature
        request_id = current_request_id()
        if request_id:
            headers[REQUEST_ID_HEADER] = request_id
        return headers

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(
                connect=min(self.timeout, 10.0),
                read=None,
                write=self.timeout,
                pool=self.timeout,
            ),
            transport=self.transport,
            # 内部 loopback 调用不能走系统 HTTP(S)_PROXY；Windows 桌面上
            # 用户级代理会让 127.0.0.1 请求被转发到外部代理。
            trust_env=False,
        )

    async def _open(
        self,
        method: str,
        path: str,
        context: AgentContext,
        **kwargs,
    ) -> AgentStream:
        client = self._client()
        request = client.build_request(
            method, path, headers=self._headers(context), **kwargs
        )
        try:
            response = await client.send(request, stream=True)
        except Exception:
            await client.aclose()
            raise
        if response.status_code >= 400:
            body = (await response.aread()).decode("utf-8", "replace")
            await response.aclose()
            await client.aclose()
            raise AgentClientError(response.status_code, body)
        return AgentStream(response=response, client=client)

    async def _json(
        self,
        method: str,
        path: str,
        context: AgentContext,
        **kwargs,
    ) -> dict:
        async with self._client() as client:
            response = await client.request(
                method, path, headers=self._headers(context), **kwargs
            )
            if response.status_code >= 400:
                raise AgentClientError(
                    response.status_code, response.text
                )
            return response.json()

    async def probe(self) -> AgentMeta:
        """读取 Agent 元信息；握手用短超时，避免 A3 卡住时拖死 A2 启动。"""
        timeout = min(max(1.0, float(self.timeout)), 10.0)
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            transport=self.transport,
            trust_env=False,
        ) as client:
            response = await client.get(
                "/v1/meta",
                headers={"Authorization": f"Bearer {self.token}"},
            )
            if response.status_code >= 400:
                raise AgentClientError(
                    response.status_code, response.text
                )
            return AgentMeta.from_dict(response.json())

    async def open_run(self, request: AgentRunRequest) -> AgentStream:
        return await self._open(
            "POST",
            "/v1/runs",
            request.context,
            json={
                "prompt": request.prompt,
                "thread_id": request.thread_id,
            },
        )

    async def open_stream(
        self, context: AgentContext, thread_id: str, *, after: int = 0
    ) -> AgentStream:
        return await self._open(
            "GET",
            f"/v1/stream/{quote(thread_id, safe='')}",
            context,
            params={"after": max(0, int(after))},
        )

    async def open_resume(
        self,
        context: AgentContext,
        thread_id: str,
        decisions: dict[str, bool],
    ) -> AgentStream:
        return await self._open(
            "POST",
            f"/v1/resume/{quote(thread_id, safe='')}",
            context,
            json={"decisions": decisions},
        )

    async def open_resume_steering(
        self, context: AgentContext, thread_id: str
    ) -> AgentStream:
        return await self._open(
            "POST",
            f"/v1/resume_steering/{quote(thread_id, safe='')}",
            context,
        )

    async def steer(
        self,
        context: AgentContext,
        thread_id: str,
        *,
        action: str,
        text: str = "",
        role: str = "explore",
    ) -> dict:
        return await self._json(
            "POST",
            f"/v1/steer/{quote(thread_id, safe='')}",
            context,
            json={"action": action, "text": text, "role": role},
        )

    async def approvals(
        self, context: AgentContext, thread_id: str
    ) -> dict:
        return await self._json(
            "GET",
            f"/v1/approvals/{quote(thread_id, safe='')}",
            context,
        )

    async def status(
        self, context: AgentContext, thread_id: str
    ) -> dict:
        return await self._json(
            "GET",
            f"/v1/status/{quote(thread_id, safe='')}",
            context,
        )

    async def history(
        self,
        context: AgentContext,
        thread_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict:
        params: dict[str, int] = {"offset": max(0, int(offset))}
        if limit is not None:
            params["limit"] = max(1, int(limit))
        return await self._json(
            "GET",
            f"/v1/history/{quote(thread_id, safe='')}",
            context,
            params=params,
        )

    async def json_request(
        self,
        context: AgentContext,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
    ) -> dict:
        return await self._json(
            method.upper(), path, context, params=params, json=json
        )

    async def raw_request(
        self,
        context: AgentContext,
        path: str,
        *,
        params: dict | None = None,
        range_header: str = "",
        if_none_match: str = "",
    ) -> tuple[int, dict[str, str], bytes]:
        headers = self._headers(context)
        if range_header:
            headers["Range"] = range_header
        if if_none_match:
            headers["If-None-Match"] = if_none_match
        async with self._client() as client:
            response = await client.get(path, headers=headers, params=params)
            if response.status_code >= 400:
                raise AgentClientError(response.status_code, response.text)
            return (
                int(response.status_code),
                dict(response.headers),
                response.content,
            )

    async def register_host_workspace(
        self, context: AgentContext
    ) -> dict:
        return await self._json(
            "POST", "/v1/host-workspaces/register", context
        )

    async def upload_files(
        self,
        context: AgentContext,
        thread_id: str,
        files: list[tuple[str, bytes, str]],
    ) -> dict:
        multipart = [
            ("files", (name, data, content_type))
            for name, data, content_type in files
        ]
        return await self._json(
            "POST",
            f"/v1/uploads/{quote(thread_id, safe='')}",
            context,
            files=multipart,
        )

    async def upload_library_file(
        self,
        context: AgentContext,
        name: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> dict:
        return await self._json(
            "POST",
            "/v1/library/items",
            context,
            files={"file": (name, data, content_type)},
        )

    async def workspace_import_start(
        self,
        context: AgentContext,
        *,
        source_path: str,
        mode: str = "copy",
        quota_bytes: int = 0,
        max_files: int = 200_000,
        max_bytes: int = 0,
    ) -> dict:
        return await self._json(
            "POST",
            "/v1/workspaces/import",
            context,
            json={
                "source_path": str(source_path),
                "mode": str(mode or "copy"),
                "quota_bytes": max(0, int(quota_bytes)),
                "max_files": max(1, int(max_files)),
                "max_bytes": max(0, int(max_bytes)),
            },
        )

    async def workspace_import_status(
        self, context: AgentContext, job_id: str
    ) -> dict:
        return await self._json(
            "GET",
            f"/v1/workspaces/import/{quote(str(job_id), safe='')}",
            context,
        )

    async def workspace_import_cancel(
        self, context: AgentContext, job_id: str
    ) -> dict:
        return await self._json(
            "DELETE",
            f"/v1/workspaces/import/{quote(str(job_id), safe='')}",
            context,
        )

    async def workspace_upload_start(
        self,
        context: AgentContext,
        *,
        file_path: str,
        filename: str,
        content_type: str = "application/octet-stream",
        quota_bytes: int = 0,
        max_files: int = 200_000,
        max_bytes: int = 0,
    ) -> dict:
        # 从磁盘流式发送，避免 A2 把整包读进内存。
        with open(file_path, "rb") as handle:
            return await self._json(
                "POST",
                "/v1/workspaces/uploads",
                context,
                data={
                    "quota_bytes": str(max(0, int(quota_bytes))),
                    "max_files": str(max(1, int(max_files))),
                    "max_bytes": str(max(0, int(max_bytes))),
                },
                files=[
                    (
                        "archive",
                        (
                            str(filename or "upload.bin"),
                            handle,
                            str(
                                content_type
                                or "application/octet-stream"
                            ),
                        ),
                    )
                ],
            )

    async def workspace_upload_status(
        self, context: AgentContext, job_id: str
    ) -> dict:
        return await self._json(
            "GET",
            f"/v1/workspaces/uploads/{quote(str(job_id), safe='')}",
            context,
        )

    async def workspace_upload_cancel(
        self, context: AgentContext, job_id: str
    ) -> dict:
        return await self._json(
            "DELETE",
            f"/v1/workspaces/uploads/{quote(str(job_id), safe='')}",
            context,
        )

    async def workspace_rollback(
        self,
        context: AgentContext,
        *,
        revision_id: str,
        git_commit: str,
    ) -> dict:
        return await self._json(
            "POST",
            "/v1/workspaces/rollback",
            context,
            json={
                "revision_id": str(revision_id),
                "git_commit": str(git_commit),
            },
        )

    async def workspace_restore(
        self,
        context: AgentContext,
        *,
        revision_id: str,
        archive_path: str,
        archive_sha256: str = "",
        git_commit: str = "",
    ) -> dict:
        return await self._json(
            "POST",
            "/v1/workspaces/restore",
            context,
            json={
                "revision_id": str(revision_id),
                "archive_path": str(archive_path),
                "archive_sha256": str(archive_sha256),
                "git_commit": str(git_commit),
            },
        )

    async def workspace_archive_delete(
        self, context: AgentContext, archive_path: str
    ) -> dict:
        return await self._json(
            "DELETE",
            "/v1/workspaces/revisions/archive",
            context,
            json={"archive_path": str(archive_path)},
        )

    async def aclose(self) -> None:
        return None
