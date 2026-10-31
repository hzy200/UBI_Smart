"""安全辅助：secret 识别/脱敏、写路径策略与外连目标守卫。"""

from ubismart.security.netguard import (
    OutboundBlocked,
    check_outbound_url,
)
from ubismart.security.secrets import redact_secrets, secret_kinds
from ubismart.security.write_policy import (
    DEFAULT_WRITE_DENY_GLOBS,
    protected_write_reason,
)

__all__ = [
    "DEFAULT_WRITE_DENY_GLOBS",
    "OutboundBlocked",
    "check_outbound_url",
    "protected_write_reason",
    "redact_secrets",
    "secret_kinds",
]
