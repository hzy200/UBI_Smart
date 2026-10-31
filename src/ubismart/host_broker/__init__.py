"""A2L 宿主能力代理（客户端进程）。

Phase 1 只包含可直接测试的基础件；协议、生命周期与能力分发在后续迭代加入。
本包不导入 `tools/`、`sandbox/`、`graph/` 或 `runtime.py`。
"""

from ubismart.host_broker.acceptor import (
    AcceptedHostCaller,
    HostBrokerAcceptor,
)
from ubismart.host_broker.client import (
    HostBrokerClient,
    HostBrokerError,
    HostBrokerOffline,
)
from ubismart.host_broker.consent import (
    AutoConsent,
    CallbackConsent,
    ConsentProvider,
    StdinConsent,
    TkConsent,
)
from ubismart.host_broker.lifecycle import (
    STATE_OFFLINE,
    STATE_PAUSED,
    STATE_RUNNING,
    STATE_STOPPED,
    BrokerLifecycle,
    LifecycleSnapshot,
)
from ubismart.host_broker.op_journal import (
    OP_CANCELLED,
    OP_FAILED,
    OP_FINISHED,
    OP_STARTED,
    OP_UNKNOWN,
    OpJournal,
    OpRecord,
)
from ubismart.host_broker.ops import HostReadOnlyOps
from ubismart.host_broker.paths import (
    HostPathError,
    HostPathPolicy,
    RootInfo,
)
from ubismart.host_broker.policy import (
    CapabilityGrant,
    GrantStore,
    HostPolicy,
    PolicyDecision,
)
from ubismart.host_broker.protocol import PROTOCOL_VERSION, Frame
from ubismart.host_broker.server import (
    PHASE1_REQUEST_OPS,
    HandlerError,
    HostBrokerServer,
)
from ubismart.host_broker.sync_client import SyncHostCaller
from ubismart.host_broker.toolchain import ToolchainRef, ToolchainRegistry
from ubismart.host_broker.transport import (
    MemoryTransport,
    TcpTransport,
    Transport,
    connect_tcp,
    memory_transport_pair,
    start_tcp_server,
)
from ubismart.host_broker.workspace_registry import (
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
