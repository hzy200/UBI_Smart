"""A2 控制面的数据库 schema（Phase B1）。

设计边界：

- 只放业务/索引数据：会话目录、运行索引、审计索引、Provider 档案、用量汇总；
- 不放 checkpoint、工作区文件、API Key、Agent 运行时状态；
- 同一套模型跑 MySQL（生产）与 SQLite（开发/测试）；
- 迁移暂由 `create_all` 承担首次建表；生产升级要接 Alembic/迁移脚本，
  不能靠自动建表改已有列。
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

CONTROL_SCHEMA_VERSION = "8"


class Base(DeclarativeBase):
    metadata = MetaData()


class ControlMeta(Base):
    __tablename__ = "control_meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), default="")


class ControlSession(Base):
    """会话目录：A2 的业务事实，不替代 A3 的 checkpoint。"""

    __tablename__ = "control_sessions"

    tenant: Mapped[str] = mapped_column(String(128), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace: Mapped[str] = mapped_column(Text, default="")
    # RFC-0001：业务身份用 workspace_id；workspace 是运行时解析结果。
    # 旧行默认空串，由回填脚本幂等补齐。
    workspace_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    # 会话一旦创建就固定绑定；host 会话必须持久化这三个字段，
    # 否则目录丢失/重启后会退回 sandbox 路径，读错工作区。
    workspace_kind: Mapped[str] = mapped_column(String(16), default="")
    a2l_id: Mapped[str] = mapped_column(String(64), default="")
    root_handle: Mapped[str] = mapped_column(String(128), default="")
    access_mode: Mapped[str] = mapped_column(String(16), default="read_only")
    model: Mapped[str] = mapped_column(String(128), default="")
    provider: Mapped[str] = mapped_column(String(128), default="")
    uploaded_bytes: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(120), default="")
    # 会话目录展示状态；只影响列表，不影响审计/checkpoint。
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    tags: Mapped[str] = mapped_column(Text, default="")
    # 会话列表提示：后台完成/失败时置真，打开会话后清除。
    unread: Mapped[bool] = mapped_column(Boolean, default=False)
    # 最近一条用户/助手消息的纯文本摘要，用于列表预览。
    last_message: Mapped[str] = mapped_column(String(160), default="")
    # tombstone：列表隐藏，但审计/checkpoint 仍由 A3/A4 保留。
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str] = mapped_column(String(32), default="")
    updated_at: Mapped[str] = mapped_column(String(32), default="")

    __table_args__ = (
        Index("ix_control_sessions_tenant_deleted", "tenant", "deleted"),
    )


class ControlRun(Base):
    """运行索引；状态由 A3 上报，A2 只存查询用副本。"""

    __tablename__ = "control_runs"

    tenant: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(24), default="running")
    started_at: Mapped[str] = mapped_column(String(32), default="")
    finished_at: Mapped[str] = mapped_column(String(32), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (
        Index("ix_control_runs_tenant_thread", "tenant", "thread_id"),
    )


class ControlAuditIndex(Base):
    """审计查询索引；可信源仍是 A3 的本地哈希链。"""

    __tablename__ = "control_audit_index"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant: Mapped[str] = mapped_column(String(128), default="")
    thread_id: Mapped[str] = mapped_column(String(64), default="")
    ts: Mapped[str] = mapped_column(String(32), default="")
    kind: Mapped[str] = mapped_column(String(32), default="")
    tool: Mapped[str] = mapped_column(String(64), default="")
    level: Mapped[str] = mapped_column(String(16), default="")
    decision: Mapped[str] = mapped_column(String(16), default="")
    ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    record_hash: Mapped[str] = mapped_column(String(64), default="")

    __table_args__ = (
        Index("ix_control_audit_tenant_thread", "tenant", "thread_id", "ts"),
    )


class ControlProviderProfile(Base):
    """Provider 档案；API Key 不在此表。"""

    __tablename__ = "control_provider_profiles"

    tenant: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    base_url: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    input_cost_per_1m: Mapped[float] = mapped_column(Float, default=0.0)
    output_cost_per_1m: Mapped[float] = mapped_column(Float, default=0.0)
    max_tokens: Mapped[float] = mapped_column(Float, default=0.0)
    max_cost: Mapped[float] = mapped_column(Float, default=0.0)
    capabilities: Mapped[str] = mapped_column(Text, default="[]")
    task_types: Mapped[str] = mapped_column(Text, default="[]")
    updated_at: Mapped[str] = mapped_column(String(32), default="")

    __table_args__ = (UniqueConstraint("tenant", "name", name="uq_provider_tenant_name"),)


class ControlUsageDaily(Base):
    """按天/租户/模型的用量汇总；A3 上报，A2 查询。"""

    __tablename__ = "control_usage_daily"

    tenant: Mapped[str] = mapped_column(String(128), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    provider: Mapped[str] = mapped_column(String(128), primary_key=True)
    model: Mapped[str] = mapped_column(String(128), primary_key=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)


class RunLease(Base):
    """会话级运行租约（RFC-0001，M4）。

    A2 在转发 run/resume 前抢占；A2 崩溃后由 expires_at 自动失效。
    A3 不连接本表，仍保留进程内活跃判断。
    """

    __tablename__ = "run_leases"

    session_key: Mapped[str] = mapped_column(String(192), primary_key=True)
    lease_id: Mapped[str] = mapped_column(String(36), default="")
    owner: Mapped[str] = mapped_column(String(128), default="")
    expires_at: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[str] = mapped_column(String(32), default="")


class IdempotencyKey(Base):
    """短期幂等记录；只存 JSON 响应，不存文件内容。"""

    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    scope: Mapped[str] = mapped_column(String(32), default="")
    # running | done
    status: Mapped[str] = mapped_column(String(16), default="running")
    response: Mapped[str] = mapped_column(Text, default="")
    expires_at: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[str] = mapped_column(String(32), default="")


class Workspace(Base):
    """工作区业务身份（RFC-0001）。

    `root_path` 只由 A2 生成，客户端不能提交；文件字节属于 A3/A4。
    本表不存文件内容、checkpoint 或 API Key。
    """

    __tablename__ = "workspaces"

    workspace_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant: Mapped[str] = mapped_column(String(128), default="")
    owner: Mapped[str] = mapped_column(String(128), default="")
    slug: Mapped[str] = mapped_column(String(64), default="")
    display_name: Mapped[str] = mapped_column(String(120), default="")
    source: Mapped[str] = mapped_column(String(24), default="upload")
    # host | sandbox：host 的字节在客户端，A2 只存 handle。
    kind: Mapped[str] = mapped_column(String(16), default="sandbox")
    a2l_id: Mapped[str] = mapped_column(String(64), default="")
    root_handle: Mapped[str] = mapped_column(String(128), default="")
    root_path: Mapped[str] = mapped_column(Text, default="")
    # ext4 | mount | ntfs；mount 表示显式降级（性能/隔离更弱）。
    root_kind: Mapped[str] = mapped_column(String(16), default="ext4")
    # provisioning | active | archived | failed | deleted
    status: Mapped[str] = mapped_column(String(16), default="provisioning")
    access_mode: Mapped[str] = mapped_column(String(16), default="read_only")
    quota_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    used_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    degraded_fs: Mapped[bool] = mapped_column(Boolean, default=False)
    # 由旧 control_sessions.workspace 回填而来；不享受新配额/归档语义。
    legacy: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str] = mapped_column(String(32), default="")
    updated_at: Mapped[str] = mapped_column(String(32), default="")

    __table_args__ = (
        UniqueConstraint(
            "tenant", "owner", "slug", name="uq_workspace_tenant_owner_slug"
        ),
        Index("ix_workspaces_tenant_status", "tenant", "status"),
        Index("ix_workspaces_owner", "tenant", "owner"),
    )


class WorkspaceRevision(Base):
    """一次导入/上传的不可变快照；归档字节在 A4 的归档盘。"""

    __tablename__ = "workspace_revisions"

    revision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(36), default="")
    source: Mapped[str] = mapped_column(String(24), default="upload")
    actor: Mapped[str] = mapped_column(String(128), default="")
    git_commit: Mapped[str] = mapped_column(String(64), default="")
    archive_path: Mapped[str] = mapped_column(Text, default="")
    archive_sha256: Mapped[str] = mapped_column(String(64), default="")
    bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    # clean | warn | failed | skipped
    scan_status: Mapped[str] = mapped_column(String(16), default="skipped")
    # active | superseded | pruned
    status: Mapped[str] = mapped_column(String(16), default="active")
    # pinned 不参与自动清理，可独立于状态存在。
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str] = mapped_column(String(32), default="")

    __table_args__ = (
        Index(
            "ix_workspace_revisions_workspace_created",
            "workspace_id",
            "created_at",
        ),
    )
