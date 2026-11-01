"""服务端外连目标的 SSRF 守卫（provider 连通性探测等）。

只依赖标准库与 `config`，不 import `sandbox` / `tools` / `graph`（见 ./AGENTS.md）。

判定在**解析之后按 IP 做**，不做字符串匹配：`http://127.0.0.1.nip.io/`、
`http://[::ffff:169.254.169.254]/` 这类写法在字符串层看不出问题，解析后
都落在被禁的网段上。

为什么必须存在：探测请求会带上 provider 的真实 API Key。目标一旦可被请求方
控制，Key 就明文出去了；目标若是 `169.254.169.254`，拿到的是云厂商的临时
凭据。

残余风险（写进 `docs/THREAT_MODEL.md`，不要假装它不存在）：

- **DNS rebinding**：`resolve_outbound_target` 返回校验通过的 IP；provider
  探测把连接钉到该 IP，并保留原 Host/SNI 与证书校验。仍建议配置
  `AGENT_PROVIDER_PROBE_ALLOWED_HOSTS` 缩小目标范围。
- **目标主机二次转发**：本模块只判定目标 IP，不阻止目标主机把请求再转到内网。
- **显式代理**：`trust_env=False` 只让 httpx 忽略环境变量代理，不覆盖显式配置。
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import SplitResult, urlsplit

_ALLOWED_SCHEMES = frozenset({"http", "https"})


class OutboundBlocked(ValueError):
    """目标不允许外连。路由层应转成 422，不要透传底层异常文本。"""


def _normalize(raw: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """`::ffff:127.0.0.1` 这类 IPv4-mapped 地址按 IPv4 判定。

    `IPv6Address.is_private` 对映射地址不生效，不归一化的话
    `http://[::ffff:127.0.0.1]/` 会直接绕过。
    """
    addr = ipaddress.ip_address(raw)
    mapped = getattr(addr, "ipv4_mapped", None)
    return mapped or addr


def _blocked_reason(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address, *, allow_private: bool
) -> str:
    """返回拒绝原因；空串表示允许。"""
    if addr.is_unspecified:
        return f"目标 IP {addr} 是未指定地址"
    if addr.is_multicast:
        return f"目标 IP {addr} 是组播地址"
    # 链路本地（169.254.169.254 / fe80::）永远拒绝：本机与内网端点都不会用它，
    # 放开它等于把云上 IAM 临时凭据交给任何能改 provider 档案的人。
    if addr.is_link_local:
        return f"目标 IP {addr} 是链路本地地址（含云元数据端点）"
    if allow_private:
        # 到这里的都不是链路本地/未指定/组播；内网与回环按显式开关放行。
        return ""
    if addr.is_loopback:
        return (
            f"目标 IP {addr} 是回环地址；本机端点请显式开启 "
            "AGENT_PROVIDER_PROBE_ALLOW_PRIVATE_NETWORK=true"
        )
    # 用 `is_global` 而不是 `is_private` 兜底：CGNAT（100.64/10，云厂商常用）
    # 在 Python 里既不是 private 也不是 reserved，只看这两个属性会漏掉它。
    if not addr.is_global:
        return (
            f"目标 IP {addr} 不是公网地址（内网 / CGNAT / 保留段）；"
            "内网端点请显式开启 "
            "AGENT_PROVIDER_PROBE_ALLOW_PRIVATE_NETWORK=true"
        )
    return ""


def _host_allowed(host: str, allowed_hosts: str) -> bool:
    """主机后缀白名单；留空表示不额外限制（仍需过 IP 判定）。"""
    entries = [
        item.strip().lower().rstrip(".")
        for item in str(allowed_hosts or "").split(",")
    ]
    entries = [item for item in entries if item]
    if not entries:
        return True
    name = host.strip().lower().rstrip(".")
    return any(
        name == entry or name.endswith("." + entry) for entry in entries
    )


@dataclass(frozen=True)
class PinnedTarget:
    """校验通过的 URL 与要钉住的 IP；连接应直接使用 `ip`。"""

    url: str
    host: str
    ip: str


def _split_target(text: str) -> SplitResult:
    """URL 层校验（scheme / userinfo / host），返回解析结果。"""
    try:
        parts = urlsplit(text)
    except ValueError as exc:  # 畸形 URL（含非法字符）
        raise OutboundBlocked(f"URL 无法解析：{exc}") from exc
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise OutboundBlocked("只允许 http / https 目标")
    # userinfo（`http://a@127.0.0.1/`）会让后续的 host 判定失效；直接拒绝，
    # 正常 provider 也不需要它。
    if parts.username or parts.password:
        raise OutboundBlocked("目标 URL 不允许携带用户名/密码")
    if not (parts.hostname or "").strip():
        raise OutboundBlocked("目标 URL 缺少主机名")
    return parts


def _resolve_infos(host: str, port: int) -> list[tuple[Any, ...]]:
    """解析主机；解析失败与空结果都不允许外连。"""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise OutboundBlocked(f"无法解析目标主机 {host}：{exc}") from exc
    if not infos:
        raise OutboundBlocked(f"无法解析目标主机 {host}")
    return infos


def _pin_address(infos: list[tuple[Any, ...]], *, allow_private: bool) -> str:
    """把解析结果逐个过 IP 判定，返回第一个可用地址。

    解析出多条记录时**全部**都要通过：只要有一条落到内网就足以发动 SSRF，
    攻击者在自己的 DNS 里配一条私有记录就能绕过"只看第一条"的实现。
    """
    first_ip = ""
    for info in infos:
        addr = _normalize(str(info[4][0]))
        reason = _blocked_reason(addr, allow_private=allow_private)
        if reason:
            raise OutboundBlocked(reason)
        if not first_ip:
            first_ip = str(info[4][0])
    return first_ip


def resolve_outbound_target(
    url: str,
    *,
    allowed_hosts: str = "",
    allow_private: bool = False,
) -> PinnedTarget:
    """校验目标并返回第一个通过校验的 IP，供调用方钉住连接。"""
    text = str(url or "").strip()
    parts = _split_target(text)
    host = (parts.hostname or "").strip()
    if not _host_allowed(host, allowed_hosts):
        raise OutboundBlocked(
            f"目标主机 {host} 不在 AGENT_PROVIDER_PROBE_ALLOWED_HOSTS 白名单内"
        )
    port = parts.port or (443 if parts.scheme == "https" else 80)
    infos = _resolve_infos(host, port)
    return PinnedTarget(
        url=text,
        host=host,
        ip=_pin_address(infos, allow_private=allow_private),
    )


def check_outbound_url(
    url: str,
    *,
    allowed_hosts: str = "",
    allow_private: bool = False,
) -> str:
    """兼容入口：只返回 URL（不钉 IP）。新代码应使用 `resolve_outbound_target`。"""
    return resolve_outbound_target(
        url,
        allowed_hosts=allowed_hosts,
        allow_private=allow_private,
    ).url
