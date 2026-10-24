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

CONTROL_SCHEMA_VERSION = "1"


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
    access_mode: Mapped[str] = mapped_column(String(16), default="read_only")
    model: Mapped[str] = mapped_column(String(128), default="")
    provider: Mapped[str] = mapped_column(String(128), default="")
    uploaded_bytes: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(120), default="")
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
