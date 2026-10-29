"""服务端外连 SSRF 守卫。

探测请求带真实 API Key，所以这些用例是安全承重墙：
- 内网/回环默认拒绝；
- 链路本地（云元数据）**任何开关下**都拒绝；
- userinfo、非 http(s) 直接拒绝；
- 解析出多条记录时逐条判定，不能只看第一条。
"""

from __future__ import annotations

import ipaddress
import socket

import pytest

from coding_agent.security.netguard import (
    OutboundBlocked,
    _blocked_reason,
    check_outbound_url,
)


def test_public_address_is_allowed() -> None:
    assert check_outbound_url("https://1.1.1.1/v1/models") == (
        "https://1.1.1.1/v1/models"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/v1/models",
        "http://10.0.0.5/v1/models",
        "http://172.16.3.4/v1/models",
        "http://192.168.1.1/v1/models",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/v1",
    ],
)
def test_private_and_metadata_targets_are_blocked(url: str) -> None:
    with pytest.raises(OutboundBlocked):
        check_outbound_url(url)


def test_link_local_is_blocked_even_when_private_is_allowed() -> None:
    """169.254.169.254 拿到的是云上 IAM 临时凭据，不允许靠开关放行。"""
    with pytest.raises(OutboundBlocked):
        check_outbound_url(
            "http://169.254.169.254/", allow_private=True
        )


def test_private_target_is_allowed_only_with_opt_in() -> None:
    with pytest.raises(OutboundBlocked):
        check_outbound_url("http://10.0.0.5/v1/models")
    assert (
        check_outbound_url(
            "http://10.0.0.5/v1/models", allow_private=True
        )
        == "http://10.0.0.5/v1/models"
    )
    assert (
        check_outbound_url("http://127.0.0.1:11434/v1", allow_private=True)
        == "http://127.0.0.1:11434/v1"
    )


def test_ipv4_mapped_ipv6_is_normalized_before_check() -> None:
    """`[::ffff:127.0.0.1]` 的 `is_private` 对 IPv6 对象不生效，必须归一化。"""
    addr = ipaddress.ip_address("::ffff:127.0.0.1")
    assert _blocked_reason(addr, allow_private=False)


def test_ipv6_loopback_and_ula_are_blocked() -> None:
    assert _blocked_reason(ipaddress.ip_address("::1"), allow_private=False)
    assert _blocked_reason(
        ipaddress.ip_address("fd00::1"), allow_private=False
    )
    assert not _blocked_reason(
        ipaddress.ip_address("fd00::1"), allow_private=True
    )


@pytest.mark.parametrize(
    "url",
    [
        "ftp://1.1.1.1/x",
        "file:///etc/passwd",
        "http://user:pass@1.1.1.1/v1/models",
        "http:///v1/models",
    ],
)
def test_scheme_and_userinfo_are_rejected(url: str) -> None:
    with pytest.raises(OutboundBlocked):
        check_outbound_url(url)


def test_host_allowlist_is_enforced_before_resolution() -> None:
    check_outbound_url("https://1.1.1.1/v1")
    with pytest.raises(OutboundBlocked):
        check_outbound_url(
            "https://1.1.1.1/v1", allowed_hosts="example.com"
        )
    # 白名单只按主机名后缀匹配，不能靠前缀绕过。
    with pytest.raises(OutboundBlocked):
        check_outbound_url(
            "https://example.com.evil.test/v1",
            allowed_hosts="example.com",
        )


def test_every_resolved_address_is_checked(monkeypatch) -> None:
    """只看第一条记录的实现会被"公网在前、内网在后"的 DNS 答案绕过。"""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ],
    )
    with pytest.raises(OutboundBlocked):
        check_outbound_url("https://probe.test/v1/models")


def test_unresolvable_host_is_rejected(monkeypatch) -> None:
    def _fail(*args, **kwargs):
        raise OSError("Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", _fail)
    with pytest.raises(OutboundBlocked):
        check_outbound_url("https://nope.invalid/v1")


def test_resolve_outbound_target_pins_first_valid_ip(monkeypatch) -> None:
    import socket

    from coding_agent.security.netguard import resolve_outbound_target

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_a, **_k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.35", 443)),
        ],
    )
    target = resolve_outbound_target("https://api.example.com/v1/models")
    assert target.url == "https://api.example.com/v1/models"
    assert target.host == "api.example.com"
    assert target.ip == "93.184.216.34"
