"""工作区注册表（RFC-0001）。

职责边界：

- 只拥有元数据与路径策略，不读写工作区文件；
- `root_path` 只在这里生成与校验，客户端永远不能直接提交；
- 文件字节、快照、Git 仍属于 A3/A4；
- 总开关关闭时，本模块不参与运行。

路径校验分两层：本模块做词法级 `ensure_inside`；符号链接逃逸必须由
A3/A4 在 WSL/Linux 内做 `realpath` 二次校验（Windows 上的 A2 无法解析
WSL 路径）。`realpath_within()` 只在 POSIX 宿主上生效。
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from coding_agent.control.schema import (
    ControlSession,
    Workspace,
    WorkspaceRevision,
)
from coding_agent.paths import (
    PathGuardError,
    ensure_inside,
    is_within,
    normalize_root,
)

# 状态机：provisioning → active/failed；active → archived/deleted；
# archived → active；deleted 是 tombstone 终态。
WORKSPACE_STATUSES = frozenset(
    {"provisioning", "active", "archived", "failed", "deleted"}
)
WORKSPACE_TRANSITIONS: dict[str, frozenset[str]] = {
    "provisioning": frozenset({"active", "failed", "deleted"}),
    "active": frozenset({"archived", "deleted"}),
    "archived": frozenset({"active", "deleted"}),
    # 失败后允许重试：重新进入 provisioning，再由导入任务决定 active/failed。
    "failed": frozenset({"provisioning", "deleted"}),
    "deleted": frozenset(),
}

WORKSPACE_SOURCES = frozenset(
    {
        "upload",
        "template",
        "local_import",
        "local_mount",
        "legacy",
    }
)
MOUNT_SOURCES = frozenset({"local_mount", "legacy"})
# revision 的来源比工作区多一个 rollback（回滚前自动留底）。
REVISION_SOURCES = (WORKSPACE_SOURCES - {"legacy"}) | {
    "rollback",
    "restore",
}
IMPORT_MODES = frozenset({"copy", "mount"})
SCAN_PROVIDERS = frozenset({"none", "clamav"})

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_LEGACY_OWNER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class WorkspaceError(ValueError):
    """工作区注册表的通用错误。"""


class WorkspacePathError(WorkspaceError):
    """路径不合法、越界或不在允许的挂载根内。"""


class WorkspaceNotFound(WorkspaceError):
    """工作区不存在。"""


class WorkspaceStatusError(WorkspaceError):
    """非法状态迁移。"""


class WorkspaceQuotaExceeded(WorkspaceError):
    """工作区配额不足。"""


def _now() -> str:
    # revision 的保留顺序依赖 created_at；用微秒避免同一秒内插入无法排序。
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _new_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True, slots=True)
class WorkspacePolicy:
    """路径与导入策略；由 Settings 构造，测试可直接实例化。"""

    root: str = ""
    archive_root: str = ""
    revisions_keep: int = 10
    import_mode: str = "copy"
    scan_provider: str = "none"
    mount_roots: tuple[str, ...] = ()
    allow_git_clone: bool = False

    def __post_init__(self) -> None:
        if self.import_mode not in IMPORT_MODES:
            raise WorkspaceError(
                f"未知导入模式：{self.import_mode!r}（copy|mount）"
            )
        if self.scan_provider not in SCAN_PROVIDERS:
            raise WorkspaceError(
                f"未知扫描 provider：{self.scan_provider!r}（none|clamav）"
            )
        if self.revisions_keep < 0:
            raise WorkspaceError("revisions_keep 不能为负")

    @classmethod
    def from_settings(cls, settings: Any) -> WorkspacePolicy:
        raw_roots = str(
            getattr(settings, "workspace_mount_roots", "") or ""
        )
        mount_roots: list[str] = []
        for item in raw_roots.split(","):
            text = item.strip()
            if not text:
                continue
            try:
                mount_roots.append(normalize_root(text))
            except PathGuardError as exc:
                raise WorkspacePathError(
                    f"非法挂载根 {text!r}：{exc}"
                ) from exc
        return cls(
            root=str(getattr(settings, "workspace_root", "") or "").strip(),
            archive_root=str(
                getattr(settings, "workspace_archive_root", "") or ""
            ).strip(),
            revisions_keep=int(
                getattr(settings, "workspace_revisions_keep", 10) or 10
            ),
            import_mode=str(
                getattr(settings, "workspace_import_mode", "copy") or "copy"
            ).lower(),
            scan_provider=str(
                getattr(settings, "workspace_scan_provider", "none") or "none"
            ).lower(),
            mount_roots=tuple(mount_roots),
            allow_git_clone=bool(
                getattr(settings, "allow_git_clone", False)
            ),
        )


def _segment(value: str, label: str) -> str:
    text = str(value or "").strip()
    if (
        not text
        or text in {".", ".."}
        or not _SEGMENT_RE.fullmatch(text)
    ):
        raise WorkspacePathError(
            f"非法 {label}：{value!r}（只允许字母、数字、_ . -）"
        )
    return text


def slugify(name: str) -> str:
    """把显示名转成稳定 slug；中文等非 ASCII 会退化为空，由调用方兜底。"""
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9._-]+", "-", text)
    text = re.sub(r"[-_.]{2,}", "-", text).strip("-._")
    return text[:48].strip("-._")


def proposed_slug(name: str, workspace_id: str) -> str:
    base = slugify(name) or "workspace"
    if _SLUG_RE.fullmatch(base):
        return base
    return f"workspace-{workspace_id[:8]}"


def workspace_root_for(
    policy: WorkspacePolicy,
    tenant: str,
    owner: str,
    slug: str,
) -> str:
    """生成 `<root>/<tenant>/<owner>/<slug>` 并做词法越界校验。"""
    if not policy.root:
        raise WorkspacePathError("workspace_root 未配置")
    try:
        base = normalize_root(policy.root)
    except PathGuardError as exc:
        raise WorkspacePathError(f"workspace_root 非法：{exc}") from exc
    tenant_part = _segment(tenant, "tenant")
    owner_part = _segment(owner, "owner")
    slug_part = _segment(slug, "slug")
    if not _SLUG_RE.fullmatch(slug_part.lower()):
        raise WorkspacePathError(f"非法 slug：{slug_part!r}")
    root = posixpath.join(base, tenant_part, owner_part, slug_part.lower())
    try:
        return ensure_inside(root, base)
    except PathGuardError as exc:
        raise WorkspacePathError(str(exc)) from exc


def validate_root(
    policy: WorkspacePolicy,
    root: str,
    *,
    source: str,
) -> tuple[str, str, bool]:
    """校验外部传入的根路径，返回 `(root_path, root_kind, degraded_fs)`。

    - 托管来源必须落在 `policy.root` 之内；
    - 挂载来源必须落在显式允许的挂载根内，且不能开启 git clone；
    - 所有路径都先归一化为 POSIX 绝对路径。
    """
    if source not in WORKSPACE_SOURCES:
        raise WorkspacePathError(f"未知来源：{source!r}")
    try:
        normalized = normalize_root(str(root or "").strip())
    except PathGuardError as exc:
        raise WorkspacePathError(f"工作区根非法：{exc}") from exc

    if source in MOUNT_SOURCES:
        if not policy.mount_roots:
            raise WorkspacePathError(
                "未配置 AGENT_WORKSPACE_MOUNT_ROOTS，禁止挂载工作区"
            )
        if not any(
            is_within(normalized, mount_root)
            for mount_root in policy.mount_roots
        ):
            raise WorkspacePathError(
                f"挂载工作区必须位于允许的根内：{normalized}"
            )
        return normalized, "mount", True

    if not policy.root:
        raise WorkspacePathError("workspace_root 未配置")
    try:
        base = normalize_root(policy.root)
        inside = ensure_inside(normalized, base)
    except PathGuardError as exc:
        raise WorkspacePathError(str(exc)) from exc
    return inside, "ext4", False


def realpath_within(path: str, root: str) -> str:
    """POSIX 宿主上的符号链接二次校验。

    Windows 上的 A2 无法解析 WSL 路径，直接返回原值；真正的 realpath
    校验由 A3/A4 在沙箱内完成（见 `sandbox/fs.py`）。
    """
    if os.name == "nt":
        return path
    real = os.path.realpath(path)
    root_real = os.path.realpath(root)
    if not is_within(real, root_real):
        raise WorkspacePathError(
            f"符号链接逃逸：{real} 不在 {root_real} 内"
        )
    return real


def check_transition(current: str, target: str) -> str:
    if current not in WORKSPACE_STATUSES:
        raise WorkspaceStatusError(f"未知工作区状态：{current!r}")
    if target not in WORKSPACE_STATUSES:
        raise WorkspaceStatusError(f"未知目标状态：{target!r}")
    if target not in WORKSPACE_TRANSITIONS[current]:
        raise WorkspaceStatusError(
            f"非法状态迁移：{current} → {target}"
        )
    return target


def _row(workspace: Workspace) -> dict[str, Any]:
    return {
        "workspace_id": workspace.workspace_id,
        "tenant": workspace.tenant,
        "owner": workspace.owner,
        "slug": workspace.slug,
        "display_name": workspace.display_name,
        "source": workspace.source,
        "root_path": workspace.root_path,
        "root_kind": workspace.root_kind,
        "status": workspace.status,
        "access_mode": workspace.access_mode,
        "quota_bytes": int(workspace.quota_bytes or 0),
        "used_bytes": int(workspace.used_bytes or 0),
        "file_count": int(workspace.file_count or 0),
        "degraded_fs": bool(workspace.degraded_fs),
        "legacy": bool(workspace.legacy),
        "created_at": workspace.created_at,
        "updated_at": workspace.updated_at,
    }


def _revision_row(revision: WorkspaceRevision) -> dict[str, Any]:
    return {
        "revision_id": revision.revision_id,
        "workspace_id": revision.workspace_id,
        "source": revision.source,
        "actor": revision.actor,
        "git_commit": revision.git_commit,
        "archive_path": revision.archive_path,
        "archive_sha256": revision.archive_sha256,
        "bytes": int(revision.bytes or 0),
        "file_count": int(revision.file_count or 0),
        "scan_status": revision.scan_status,
        "status": revision.status,
        "pinned": bool(revision.pinned),
        "created_at": revision.created_at,
    }


def _legacy_owner(thread_id: str) -> str:
    """多用户内部 thread 形如 `user:thread`；单用户回退到 local。"""
    text = str(thread_id or "")
    prefix, sep, _ = text.partition(":")
    if sep and _LEGACY_OWNER_RE.fullmatch(prefix):
        return prefix
    return "local"


class WorkspaceStore:
    """工作区元数据与 revision 的持久化入口。

    与 `ControlStore` 共用同一个 Engine；不持有 sandbox，也不跑命令。
    """

    def __init__(self, engine: Engine, policy: WorkspacePolicy) -> None:
        self.engine = engine
        self.policy = policy

    # ---------- 工作区 ----------

    def create(
        self,
        tenant: str,
        owner: str,
        display_name: str,
        *,
        source: str = "upload",
        root_path: str | None = None,
        access_mode: str = "read_only",
        quota_bytes: int = 0,
        status: str = "provisioning",
        legacy: bool = False,
    ) -> dict[str, Any]:
        if source not in WORKSPACE_SOURCES:
            raise WorkspacePathError(f"未知来源：{source!r}")
        if source == "legacy":
            # legacy 只由 migrate_legacy_sessions 内部创建，避免外部绕过策略。
            raise WorkspacePathError("legacy 来源不能通过 create() 创建")
        if status not in WORKSPACE_STATUSES:
            raise WorkspaceStatusError(f"未知状态：{status!r}")
        tenant_part = _segment(tenant, "tenant")
        owner_part = _segment(owner, "owner")
        workspace_id = _new_id()
        slug = proposed_slug(display_name, workspace_id)

        if root_path is None:
            root_path = workspace_root_for(
                self.policy, tenant_part, owner_part, slug
            )
        normalized, root_kind, degraded = validate_root(
            self.policy, root_path, source=source
        )

        now = _now()
        for attempt in range(2):
            candidate = slug if attempt == 0 else f"{slug[:48]}-{workspace_id[:8]}"
            row = Workspace(
                workspace_id=workspace_id,
                tenant=tenant_part,
                owner=owner_part,
                slug=candidate,
                display_name=str(display_name or ""),
                source=source,
                root_path=normalized,
                root_kind=root_kind,
                status=status,
                access_mode=str(access_mode or "read_only"),
                quota_bytes=max(0, int(quota_bytes or 0)),
                used_bytes=0,
                file_count=0,
                degraded_fs=degraded,
                legacy=legacy,
                created_at=now,
                updated_at=now,
            )
            try:
                with Session(self.engine) as session, session.begin():
                    session.add(row)
                    session.flush()
                    created = _row(row)
                return created
            except IntegrityError:
                if attempt == 1:
                    raise WorkspaceError(
                        f"工作区 slug 冲突，无法创建：{candidate}"
                    ) from None
        raise WorkspaceError("工作区创建失败")  # pragma: no cover

    def get(self, workspace_id: str) -> dict[str, Any] | None:
        with Session(self.engine) as session:
            row = session.get(Workspace, str(workspace_id))
            return _row(row) if row is not None else None

    def require(self, workspace_id: str) -> dict[str, Any]:
        row = self.get(workspace_id)
        if row is None:
            raise WorkspaceNotFound(f"工作区不存在：{workspace_id}")
        if row["status"] == "deleted":
            raise WorkspaceNotFound(f"工作区已删除：{workspace_id}")
        return row

    def list(
        self,
        tenant: str,
        *,
        owner: str | None = None,
        include_deleted: bool = False,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        statement = select(Workspace).where(
            Workspace.tenant == str(tenant)
        )
        if owner is not None:
            statement = statement.where(Workspace.owner == str(owner))
        if not include_deleted:
            statement = statement.where(Workspace.status != "deleted")
        statement = statement.order_by(Workspace.updated_at.desc()).limit(
            max(1, int(limit))
        )
        with Session(self.engine) as session:
            return [_row(item) for item in session.scalars(statement).all()]

    def set_status(self, workspace_id: str, target: str) -> dict[str, Any]:
        now = _now()
        with Session(self.engine) as session, session.begin():
            row = session.get(Workspace, str(workspace_id))
            if row is None:
                raise WorkspaceNotFound(f"工作区不存在：{workspace_id}")
            row.status = check_transition(row.status, target)
            row.updated_at = now
            session.flush()
            result = _row(row)
        return result

    def update(
        self,
        workspace_id: str,
        *,
        display_name: str | None = None,
        access_mode: str | None = None,
        quota_bytes: int | None = None,
        used_bytes: int | None = None,
        file_count: int | None = None,
    ) -> dict[str, Any]:
        now = _now()
        with Session(self.engine) as session, session.begin():
            row = session.get(Workspace, str(workspace_id))
            if row is None:
                raise WorkspaceNotFound(f"工作区不存在：{workspace_id}")
            if display_name is not None:
                row.display_name = str(display_name)
            if access_mode is not None:
                row.access_mode = str(access_mode)
            if quota_bytes is not None:
                row.quota_bytes = max(0, int(quota_bytes))
            if used_bytes is not None:
                row.used_bytes = max(0, int(used_bytes))
            if file_count is not None:
                row.file_count = max(0, int(file_count))
            row.updated_at = now
            session.flush()
            result = _row(row)
        return result

    def delete(self, workspace_id: str) -> dict[str, Any]:
        """tombstone：不删文件、不删 revision、不删审计。"""
        return self.set_status(workspace_id, "deleted")

    def add_usage(
        self,
        workspace_id: str,
        *,
        bytes: int = 0,
        file_count: int = 0,
    ) -> dict[str, Any]:
        """原子增加用量；超过 quota_bytes 时拒绝（0 = 不限）。"""
        delta_bytes = max(0, int(bytes or 0))
        delta_files = max(0, int(file_count or 0))
        statement = (
            update(Workspace)
            .where(
                Workspace.workspace_id == str(workspace_id),
                or_(
                    Workspace.quota_bytes == 0,
                    Workspace.used_bytes + delta_bytes
                    <= Workspace.quota_bytes,
                ),
            )
            .values(
                used_bytes=Workspace.used_bytes + delta_bytes,
                file_count=Workspace.file_count + delta_files,
                updated_at=_now(),
            )
        )
        with Session(self.engine) as session, session.begin():
            result = session.execute(statement)
            if result.rowcount == 0:
                row = session.get(Workspace, str(workspace_id))
                if row is None:
                    raise WorkspaceNotFound(
                        f"工作区不存在：{workspace_id}"
                    )
                raise WorkspaceQuotaExceeded(
                    "工作区配额不足："
                    f"已用 {row.used_bytes}，新增 {delta_bytes}，"
                    f"配额 {row.quota_bytes}"
                )
        return self.require(workspace_id)

    def set_usage(
        self,
        workspace_id: str,
        *,
        used_bytes: int,
        file_count: int,
    ) -> dict[str, Any]:
        """绝对值回写（回滚/对账用）。"""
        with Session(self.engine) as session, session.begin():
            row = session.get(Workspace, str(workspace_id))
            if row is None:
                raise WorkspaceNotFound(
                    f"工作区不存在：{workspace_id}"
                )
            row.used_bytes = max(0, int(used_bytes or 0))
            row.file_count = max(0, int(file_count or 0))
            row.updated_at = _now()
            session.flush()
            result = _row(row)
        return result

    # ---------- revision ----------

    def add_revision(
        self,
        workspace_id: str,
        *,
        revision_id: str = "",
        source: str = "upload",
        actor: str = "",
        git_commit: str = "",
        archive_path: str = "",
        archive_sha256: str = "",
        bytes: int = 0,
        file_count: int = 0,
        scan_status: str = "skipped",
        status: str = "active",
        pinned: bool = False,
    ) -> dict[str, Any]:
        if source not in REVISION_SOURCES:
            raise WorkspaceError(f"非法 revision 来源：{source!r}")
        if scan_status not in {"clean", "warn", "failed", "skipped"}:
            raise WorkspaceError(f"非法扫描状态：{scan_status!r}")
        if status not in {"active", "superseded", "pruned"}:
            raise WorkspaceError(f"非法 revision 状态：{status!r}")
        now = _now()
        revision = WorkspaceRevision(
            revision_id=str(revision_id or _new_id())[:36],
            workspace_id=str(workspace_id),
            source=source,
            actor=str(actor or ""),
            git_commit=str(git_commit or ""),
            archive_path=str(archive_path or ""),
            archive_sha256=str(archive_sha256 or ""),
            bytes=max(0, int(bytes or 0)),
            file_count=max(0, int(file_count or 0)),
            scan_status=scan_status,
            status=status,
            pinned=bool(pinned),
            created_at=now,
        )
        with Session(self.engine) as session, session.begin():
            workspace = session.get(Workspace, str(workspace_id))
            if workspace is None:
                raise WorkspaceNotFound(f"工作区不存在：{workspace_id}")
            if status == "active":
                previous = session.scalars(
                    select(WorkspaceRevision).where(
                        WorkspaceRevision.workspace_id == str(workspace_id),
                        WorkspaceRevision.status == "active",
                    )
                ).all()
                for item in previous:
                    item.status = "superseded"
            session.add(revision)
            session.flush()
            result = _revision_row(revision)
        return result

    def list_revisions(
        self, workspace_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        statement = (
            select(WorkspaceRevision)
            .where(WorkspaceRevision.workspace_id == str(workspace_id))
            .order_by(WorkspaceRevision.created_at.desc())
            .limit(max(1, int(limit)))
        )
        with Session(self.engine) as session:
            return [
                _revision_row(item)
                for item in session.scalars(statement).all()
            ]

    def revisions_to_prune(
        self, workspace_id: str, *, keep: int | None = None
    ) -> list[dict[str, Any]]:
        """返回超出保留数的非 pinned revision（最旧优先）。

        pinned 不占用 `keep` 名额，也永不自动清理；
        只返回元数据，删除归档文件由 A4 执行。
        """
        limit = (
            self.policy.revisions_keep if keep is None else max(0, int(keep))
        )
        rows = self.list_revisions(workspace_id, limit=10_000)
        candidates = [
            item
            for item in rows
            if item["status"] != "pruned" and not item["pinned"]
        ]
        candidates.sort(
            key=lambda item: item["created_at"], reverse=True
        )
        return candidates[limit:]

    def get_revision(self, revision_id: str) -> dict[str, Any] | None:
        with Session(self.engine) as session:
            row = session.get(WorkspaceRevision, str(revision_id))
            return _revision_row(row) if row is not None else None

    def mark_revision_pruned(self, revision_id: str) -> dict[str, Any]:
        with Session(self.engine) as session, session.begin():
            row = session.get(WorkspaceRevision, str(revision_id))
            if row is None:
                raise WorkspaceNotFound(
                    f"revision 不存在：{revision_id}"
                )
            row.status = "pruned"
            session.flush()
            result = _revision_row(row)
        return result

    def set_revision_pinned(
        self, revision_id: str, pinned: bool
    ) -> dict[str, Any]:
        with Session(self.engine) as session, session.begin():
            row = session.get(WorkspaceRevision, str(revision_id))
            if row is None:
                raise WorkspaceNotFound(
                    f"revision 不存在：{revision_id}"
                )
            row.pinned = bool(pinned)
            session.flush()
            result = _revision_row(row)
        return result

    def activate_revision(
        self, workspace_id: str, revision_id: str
    ) -> dict[str, Any]:
        """把指定 revision 置为 active，其余 active 置 superseded。"""
        with Session(self.engine) as session, session.begin():
            target = session.get(WorkspaceRevision, str(revision_id))
            if target is None or target.workspace_id != str(workspace_id):
                raise WorkspaceNotFound(
                    f"revision 不存在：{revision_id}"
                )
            if target.status == "pruned":
                raise WorkspaceError("已清理的 revision 不能激活")
            previous = session.scalars(
                select(WorkspaceRevision).where(
                    WorkspaceRevision.workspace_id == str(workspace_id),
                    WorkspaceRevision.status == "active",
                )
            ).all()
            for item in previous:
                if item.revision_id != target.revision_id:
                    item.status = "superseded"
            target.status = "active"
            session.flush()
            result = _revision_row(target)
        return result

    # ---------- 旧数据回填 ----------

    def migrate_legacy_sessions(
        self,
        *,
        tenant: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """把旧 `control_sessions.workspace` 幂等映射为工作区记录。

        以 `(tenant, owner, "legacy-" + sha1(path)[:12])` 去重；重复执行
        不会产生新记录。`dry_run=True` 只统计，不写库。
        """
        statement = select(ControlSession).where(
            ControlSession.workspace_id == ""
        )
        if tenant is not None:
            statement = statement.where(
                ControlSession.tenant == str(tenant)
            )
        scanned = created = reused = skipped = 0
        with Session(self.engine) as session:
            rows = session.scalars(statement).all()
            scanned = len(rows)
            for row in rows:
                path = str(row.workspace or "").strip()
                if not path:
                    skipped += 1
                    continue
                owner = _legacy_owner(row.thread_id)
                digest = hashlib.sha1(
                    path.encode("utf-8"), usedforsecurity=False
                ).hexdigest()[:12]
                slug = f"legacy-{digest}"
                existing = session.scalars(
                    select(Workspace).where(
                        Workspace.tenant == row.tenant,
                        Workspace.owner == owner,
                        Workspace.slug == slug,
                    )
                ).first()
                if existing is None:
                    if dry_run:
                        created += 1
                        continue
                    try:
                        normalized = normalize_root(path)
                    except PathGuardError:
                        skipped += 1
                        continue
                    now = _now()
                    existing = Workspace(
                        workspace_id=_new_id(),
                        tenant=row.tenant,
                        owner=owner,
                        slug=slug,
                        display_name=str(row.title or ""),
                        source="legacy",
                        root_path=normalized,
                        root_kind=(
                            "mount"
                            if normalized.startswith("/mnt/")
                            else "ext4"
                        ),
                        status="active",
                        access_mode=str(row.access_mode or "read_only"),
                        quota_bytes=0,
                        used_bytes=0,
                        file_count=0,
                        degraded_fs=normalized.startswith("/mnt/"),
                        legacy=True,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(existing)
                    session.flush()
                    created += 1
                else:
                    reused += 1
                if not dry_run:
                    row.workspace_id = existing.workspace_id
            if not dry_run:
                session.commit()
        return {
            "scanned": scanned,
            "created": created,
            "reused": reused,
            "skipped": skipped,
            "dry_run": bool(dry_run),
        }
