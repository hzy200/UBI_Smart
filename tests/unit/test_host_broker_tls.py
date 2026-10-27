"""A2L mTLS：真实 TLS 握手、客户端证书与指纹绑定。

拓扑必须与生产一致：A3 监听 TLS，A2L 作为 TLS 客户端连出；
HostBrokerServer 跑在 A2L 的出站连接上，HostBrokerClient 跑在 A3 的
已接受连接上。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from coding_agent.host_broker.client import HostBrokerClient, HostBrokerError
from coding_agent.host_broker.server import HostBrokerServer
from coding_agent.host_broker.transport import (
    ServerTlsConfig,
    TlsConfig,
    certificate_fingerprint,
    connect_tcp,
    start_tcp_server,
)


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _write(
    tmp_path: Path,
    name: str,
    key,
    *,
    issuer_key=None,
    issuer_cert=None,
    is_ca: bool = False,
    san: list | None = None,
    eku: list | None = None,
) -> tuple[Path, Path]:
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer_cert.subject if issuer_cert else subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
    )
    if is_ca:
        builder = builder.add_extension(
            x509.BasicConstraints(ca=True, path_length=None), critical=True
        )
    else:
        builder = builder.add_extension(
            x509.ExtendedKeyUsage(eku or []), critical=False
        )
    if san:
        builder = builder.add_extension(
            x509.SubjectAlternativeName(san), critical=False
        )
    cert = builder.sign(issuer_key or key, hashes.SHA256())
    key_path = tmp_path / f"{name}.key"
    cert_path = tmp_path / f"{name}.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key_path, cert_path


def _certs(tmp_path: Path) -> dict[str, Path]:
    ca_key = _key()
    _, ca_cert = _write(tmp_path, "ca", ca_key, is_ca=True)
    ca = x509.load_pem_x509_certificate(ca_cert.read_bytes())
    server_key = _key()
    server_key_path, server_cert = _write(
        tmp_path,
        "server",
        server_key,
        issuer_key=ca_key,
        issuer_cert=ca,
        san=[x509.DNSName("localhost")],
        eku=[ExtendedKeyUsageOID.SERVER_AUTH],
    )
    client_key = _key()
    client_key_path, client_cert = _write(
        tmp_path,
        "client",
        client_key,
        issuer_key=ca_key,
        issuer_cert=ca,
        eku=[ExtendedKeyUsageOID.CLIENT_AUTH],
    )
    return {
        "ca": ca_cert,
        "server_key": server_key_path,
        "server_cert": server_cert,
        "client_key": client_key_path,
        "client_cert": client_cert,
    }


async def _pair(certs: dict[str, Path]):
    accepted: asyncio.Future = asyncio.get_running_loop().create_future()
    done = asyncio.Event()

    async def handler(transport):
        accepted.set_result(transport)
        await done.wait()

    tls_server, port = await start_tcp_server(
        handler,
        host="127.0.0.1",
        port=0,
        tls=ServerTlsConfig(
            cert=str(certs["server_cert"]),
            key=str(certs["server_key"]),
            ca=str(certs["ca"]),
            require_client_cert=True,
        ),
    )
    a2l_transport = await connect_tcp(
        f"tcp://127.0.0.1:{port}",
        tls=TlsConfig(
            ca=str(certs["ca"]),
            cert=str(certs["client_cert"]),
            key=str(certs["client_key"]),
            server_name="localhost",
        ),
    )
    a3_transport = await accepted
    return tls_server, done, a2l_transport, a3_transport


def test_mtls_handshake_binds_client_and_server_fingerprints(tmp_path) -> None:
    certs = _certs(tmp_path)
    client_fp = certificate_fingerprint(str(certs["client_cert"]))
    server_fp = certificate_fingerprint(str(certs["server_cert"]))

    async def scenario() -> None:
        tls_server, done, a2l_transport, a3_transport = await _pair(certs)
        broker = HostBrokerServer(
            a2l_transport,
            broker_id="b-1",
            device_fingerprint=client_fp,
            require_encrypted=True,
        )
        broker_task = asyncio.create_task(broker.run())
        client = HostBrokerClient(
            a3_transport,
            client_id="a3",
            a2l_id="b-1",
            server_fingerprint=server_fp,
            device_fingerprint=client_fp,
            require_encrypted=True,
            heartbeat_s=0.1,
        )
        try:
            hello = await client.connect(timeout=5)
            assert hello.meta["a2l_id"] == "b-1"
            assert hello.meta["device_fingerprint"] == client_fp
        finally:
            await client.close()
            await broker.shutdown()
            await broker_task
            done.set()
            tls_server.close()
            await tls_server.wait_closed()

    asyncio.run(scenario())


def test_mtls_rejects_wrong_server_fingerprint(tmp_path) -> None:
    certs = _certs(tmp_path)
    client_fp = certificate_fingerprint(str(certs["client_cert"]))

    async def scenario() -> None:
        tls_server, done, a2l_transport, a3_transport = await _pair(certs)
        broker = HostBrokerServer(
            a2l_transport,
            broker_id="b-1",
            device_fingerprint=client_fp,
            require_encrypted=True,
        )
        broker_task = asyncio.create_task(broker.run())
        client = HostBrokerClient(
            a3_transport,
            client_id="a3",
            a2l_id="b-1",
            server_fingerprint="0" * 64,
            device_fingerprint=client_fp,
            require_encrypted=True,
        )
        try:
            with pytest.raises(HostBrokerError):
                await client.connect(timeout=5)
        finally:
            await client.close()
            await broker.shutdown()
            await broker_task
            done.set()
            tls_server.close()
            await tls_server.wait_closed()

    asyncio.run(scenario())
