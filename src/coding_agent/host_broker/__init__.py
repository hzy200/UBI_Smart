"""A2L 宿主能力代理（客户端进程）。

Phase 1 只包含可直接测试的基础件；协议、生命周期与能力分发在后续迭代加入。
本包不导入 `tools/`、`sandbox/`、`graph/` 或 `runtime.py`。
"""

from coding_agent.host_broker.acceptor import (
    AcceptedHostCaller,
    HostBrokerAcceptor,
)
from coding_agent.host_broker.client import (
    HostBrokerClient,
    HostBrokerError,
    HostBrokerOffline,
)
from coding_agent.host_broker.consent import (
    AutoConsent,
    CallbackConsent,
    ConsentProvider,
    StdinConsent,
    TkConsent,
)
from coding_agent.host_broker.lifecycle import (
    STATE_OFFLINE,
    STATE_PAUSED,
    STATE_RUNNING,
    STATE_STOPPED,
    BrokerLifecycle,
    LifecycleSnapshot,
)
from coding_agent.host_broker.op_journal import (
    OP_CANCELLED,
    OP_FAILED,
    OP_FINISHED,
    OP_STARTED,
    OP_UNKNOWN,
    OpJournal,
    OpRecord,
)
from coding_agent.host_broker.ops import HostReadOnlyOps
from coding_agent.host_broker.paths import (
    HostPathError,
    HostPathPolicy,
    RootInfo,
)
from coding_agent.host_broker.policy import (
    CapabilityGrant,
    GrantStore,
    HostPolicy,
    PolicyDecision,
)
from coding_agent.host_broker.protocol import PROTOCOL_VERSION, Frame
from coding_agent.host_broker.server import (
    PHASE1_REQUEST_OPS,
    HandlerError,
    HostBrokerServer,
)
from coding_agent.host_broker.sync_client import SyncHostCaller
from coding_agent.host_broker.toolchain import ToolchainRef, ToolchainRegistry
from coding_agent.host_broker.transport import (
    MemoryTransport,
    TcpTransport,
    Transport,
    connect_tcp,
    memory_transport_pair,
    start_tcp_server,
)
from coding_agent.host_broker.workspace_registry import (
    TkWorkspaceRegistrar,
    WorkspaceRegistrar,
)

__all__ = [
    "STATE_OFFLINE",
    "STATE_PAUSED",
    "STATE_RUNNING",
    "STATE_STOPPED",
    "AcceptedHostCaller",
    "AutoConsent",
    "CallbackConsent",
    "ConsentProvider",
    "StdinConsent",
    "TkConsent",
    "OP_CANCELLED",
    "OP_FAILED",
    "OP_FINISHED",
    "OP_STARTED",
    "OP_UNKNOWN",
    "PROTOCOL_VERSION",
    "BrokerLifecycle",
    "CapabilityGrant",
    "Frame",
    "GrantStore",
    "HandlerError",
    "HostBrokerAcceptor",
    "HostBrokerClient",
    "HostBrokerError",
    "HostBrokerOffline",
    "HostBrokerServer",
    "HostPathError",
    "HostPathPolicy",
    "HostPolicy",
    "HostReadOnlyOps",
    "LifecycleSnapshot",
    "MemoryTransport",
    "OpJournal",
    "OpRecord",
    "PHASE1_REQUEST_OPS",
    "PolicyDecision",
    "RootInfo",
    "SyncHostCaller",
    "TcpTransport",
    "TkWorkspaceRegistrar",
    "ToolchainRef",
    "ToolchainRegistry",
    "Transport",
    "WorkspaceRegistrar",
    "connect_tcp",
    "memory_transport_pair",
    "start_tcp_server",
]
