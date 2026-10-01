"""The pin re-authenticates Claude Code's Chrome-bridge WebSocket as the pinned account.

Claude Code opens ``wss://bridge.claudeusercontent.com/chrome/<account>`` through
HTTPS_PROXY and authenticates in the FIRST WebSocket message, not in a header:
``{"type":"connect","client_type":"claude-code","oauth_token":"<ACTIVE token>"}``.
The path already names the pinned account (the pin swaps ``/api/oauth/validate``),
so the token in that frame is the one mismatch. These tests drive the decision to
terminate the bridge, its certificate, the frame rewrite and the whole path
against a local fake TLS WebSocket upstream. Nothing here leaves loopback.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import ssl
import threading
import time
from pathlib import Path

import pytest

from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID

from cswap_pin import proxy as pp
from cswap_pin.proxy import PinProxy

BRIDGE = "bridge.claudeusercontent.com"
ACTIVE = "ACTIVE-ACCOUNT-TOKEN-aaaa1111"
PINNED = "PINNED-ACCOUNT-TOKEN-bbbb2222"
SERVER_FRAME = b"\x81\x05hello"
REPLY = b"The reply body is long enough to be written in several separate pieces."
WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


@pytest.fixture(autouse=True)
def _stdlib_ssl():
    try:
        import truststore

        truststore.extract_from_ssl()
    except ImportError:
        pass
    yield


@pytest.fixture
def certdir(tmp_path):
    d = tmp_path / "certs"
    pp.ensure_ca(d, "api.anthropic.com")
    return d


@pytest.fixture
def lifecycle(monkeypatch):
    lines: list[str] = []
    monkeypatch.setattr(pp, "_log_lifecycle", lines.append)
    return lines


def client_frame(
    payload: bytes,
    *,
    opcode: int = 1,
    fin: bool = True,
    rsv: int = 0,
    masked: bool = True,
    mask: bytes = b"\x11\x22\x33\x44",
) -> bytes:
    """A client-to-server frame built independently of the code under test."""
    n = len(payload)
    flag = 0x80 if masked else 0
    if n < 126:
        length = bytes([flag | n])
    elif n < 1 << 16:
        length = bytes([flag | 126]) + n.to_bytes(2, "big")
    else:
        length = bytes([flag | 127]) + n.to_bytes(8, "big")
    body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload)) if masked else payload
    return (
        bytes([(0x80 if fin else 0) | rsv | opcode])
        + length
        + (mask if masked else b"")
        + body
    )


def decode_client_frame(frame: bytes) -> tuple[int, int, bytes, bytes]:
    """``(first byte, length-field byte, mask key, unmasked payload)``."""
    n = frame[1] & 0x7F
    pos = 2
    if n == 126:
        n, pos = int.from_bytes(frame[2:4], "big"), 4
    elif n == 127:
        n, pos = int.from_bytes(frame[2:10], "big"), 10
    mask = frame[pos : pos + 4]
    body = frame[pos + 4 : pos + 4 + n]
    assert len(body) == n and pos + 4 + n == len(frame)
    return frame[0], frame[1], mask, bytes(b ^ mask[i % 4] for i, b in enumerate(body))


def connect_payload(token: str = ACTIVE, **extra) -> bytes:
    body = {"type": "connect", "client_type": "claude-code", "oauth_token": token}
    body.update(extra)
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def read_frame(sock) -> bytes:
    """One whole WebSocket frame off ``sock``, exactly as it came on the wire."""

    def take(n: int) -> bytes:
        out = bytearray()
        while len(out) < n:
            chunk = sock.recv(n - len(out))
            if not chunk:
                raise EOFError
            out += chunk
        return bytes(out)

    head = take(2)
    n = head[1] & 0x7F
    ext = b""
    if n == 126:
        ext = take(2)
        n = int.from_bytes(ext, "big")
    elif n == 127:
        ext = take(8)
        n = int.from_bytes(ext, "big")
    mask = take(4) if head[1] & 0x80 else b""
    return head + ext + mask + take(n)


class Sink:
    """Stands in for the upstream socket where only ``sendall`` is used."""

    def __init__(self):
        self.data = bytearray()

    def sendall(self, chunk: bytes) -> None:
        self.data += chunk


class FakeBridgeUpstream:
    """A TLS WebSocket server answering as the bridge host.

    Records the request head and every frame the client sends, answers the
    handshake with ``status`` (101 unless told otherwise) and, after the first
    data frame, sends ``SERVER_FRAME`` once. A request that is not an upgrade,
    or an upgrade answered with another status, gets ``REPLY`` back in three
    separate writes with pauses between them, after reading any request body.
    ``wrong_cert_first`` presents a certificate for another host on the first
    connection only.
    """

    def __init__(self, certdir: Path, status: int = 101, wrong_cert_first: bool = False):
        ca = pp._load_ca_if_usable(certdir / "ca.pem", certdir / "ca.key")
        self._ctx = self._context(certdir, "up", BRIDGE, ca)
        self._wrong_ctx = self._context(certdir, "wrong", "wrong.example", ca)
        self._status = status
        self._wrong_cert_first = wrong_cert_first
        self.heads: list[bytes] = []
        self.bodies: list[bytes] = []
        self.frames: list[bytes] = []
        self.first_frame = threading.Event()
        self.second_frame = threading.Event()
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(8)
        self.port = self._srv.getsockname()[1]
        self._conns = 0
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            self._conns += 1
            threading.Thread(
                target=self._serve, args=(conn, self._conns), daemon=True
            ).start()

    @staticmethod
    def _context(certdir, name, host, ca):
        cert, key = pp._make_leaf(host, *ca)
        pem, keyfile = certdir / f"{name}.pem", certdir / f"{name}.key"
        pem.write_bytes(cert.public_bytes(pp.serialization.Encoding.PEM))
        pp._write_key(keyfile, key)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(pem), str(keyfile))
        return ctx

    @staticmethod
    def _reply_in_pieces(tls, status):
        head = (
            f"HTTP/1.1 {status} Reply\r\nContent-Length: {len(REPLY)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
        third = len(REPLY) // 3
        for piece in (head, REPLY[:third], REPLY[third:]):
            tls.sendall(piece)
            time.sleep(0.2)
        tls.close()

    def _serve(self, conn, nth):
        try:
            wrong = self._wrong_cert_first and nth == 1
            tls = (self._wrong_ctx if wrong else self._ctx).wrap_socket(
                conn, server_side=True
            )
            tls.settimeout(10)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = tls.recv(4096)
                if not chunk:
                    return
                buf += chunk
            head, _, body = buf.partition(b"\r\n\r\n")
            head += b"\r\n\r\n"
            self.heads.append(head)
            if b"upgrade: websocket" not in head.lower():
                length = int(
                    next(
                        (
                            line.split(b":", 1)[1]
                            for line in head.split(b"\r\n")
                            if line.lower().startswith(b"content-length")
                        ),
                        b"0",
                    )
                )
                while len(body) < length:
                    body += tls.recv(4096)
                self.bodies.append(body)
                self._reply_in_pieces(tls, 200)
                return
            if self._status != 101:
                self._reply_in_pieces(tls, self._status)
                return
            key = next(
                line.split(b":", 1)[1].strip()
                for line in head.split(b"\r\n")
                if line.lower().startswith(b"sec-websocket-key")
            )
            accept = base64.b64encode(hashlib.sha1(key + WS_GUID).digest())
            tls.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n"
            )
            self.frames.append(read_frame(tls))
            self.first_frame.set()
            tls.sendall(SERVER_FRAME)
            self.frames.append(read_frame(tls))
            self.second_frame.set()
            tls.recv(1)
        except (OSError, EOFError):
            pass

    def stop(self):
        self._srv.close()


class ConnectChain:
    """A loopback CONNECT proxy in front of the fake upstream.

    The pin reaches the bridge host through the same egress chain it uses for
    every tunnel, so this is the seam: the pin dials HERE and is told to
    CONNECT ``bridge.claudeusercontent.com:443``, and the chain forwards to the
    fake upstream. Records the CONNECT targets it was asked for.
    """

    def __init__(self, upstream_port: int):
        self._target = ("127.0.0.1", upstream_port)
        self.targets: list[str] = []
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(8)
        self.port = self._srv.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = conn.recv(1)
                if not chunk:
                    return
                head += chunk
            self.targets.append(head.split(b" ")[1].decode())
            up = socket.create_connection(self._target, timeout=10)
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            for a, b in ((conn, up), (up, conn)):
                threading.Thread(target=self._pipe, args=(a, b), daemon=True).start()
        except OSError:
            conn.close()

    @staticmethod
    def _pipe(a, b):
        try:
            while True:
                data = a.recv(65536)
                if not data:
                    break
                b.sendall(data)
        except OSError:
            pass
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def stop(self):
        self._srv.close()


class Rig:
    """A started pin and a fake bridge upstream.

    By default the pin reaches the upstream through a loopback CONNECT chain,
    which the pin trusts without verifying. ``direct=True`` has no chain: the
    pin dials the bridge host itself, so its upstream TLS is VERIFIED, and the
    test points that dial at the fake (see ``rig_for``).
    """

    def __init__(self, certdir, provider, direct=False, **upstream_kw):
        self.certdir = certdir
        (certdir / "trace-to").write_text(str(certdir / "trace.log"))
        self.upstream = FakeBridgeUpstream(certdir, **upstream_kw)
        self.chain = None if direct else ConnectChain(self.upstream.port)
        self.proxy = PinProxy(
            certdir=certdir,
            pin_token_provider=provider,
            chain_proxy=None if direct else ("127.0.0.1", self.chain.port),
        )
        self.proxy.start()
        self.sockets: list = []

    def open_tunnel(self):
        """CONNECT through the pin and return the socket after its 200."""
        raw = socket.create_connection(("127.0.0.1", self.proxy.port), timeout=10)
        self.sockets.append(raw)
        raw.sendall(
            f"CONNECT {BRIDGE}:443 HTTP/1.1\r\nHost: {BRIDGE}:443\r\n\r\n".encode()
        )
        reply = b""
        while b"\r\n\r\n" not in reply:
            chunk = raw.recv(4096)
            assert chunk, "the pin closed the CONNECT"
            reply += chunk
        assert reply.startswith(b"HTTP/1.1 200")
        return raw

    def refuse_certificate(self) -> BaseException:
        """A client that does not trust the pin CA: its handshake fails."""
        raw = self.open_tunnel()
        try:
            ssl.create_default_context().wrap_socket(raw, server_hostname=BRIDGE)
        except (ssl.SSLError, OSError) as exc:
            return exc
        raise AssertionError("the client accepted the bridge leaf")

    def abort_handshake(self) -> None:
        """A client that sends its ClientHello and then hangs up."""
        raw = self.open_tunnel()
        incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        obj = ssl.create_default_context().wrap_bio(
            incoming, outgoing, server_hostname=BRIDGE
        )
        try:
            obj.do_handshake()
        except ssl.SSLWantReadError:
            pass
        raw.sendall(outgoing.read())
        raw.close()

    def connect(self, extensions: bool = True, request: "bytes | list[bytes] | None" = None):
        """CONNECT through the pin, TLS to the bridge host, send a request.

        The request is the Chrome-bridge Upgrade unless ``request`` is given;
        a list is sent as separate writes. Returns ``(tls, response head)``.
        """
        raw = self.open_tunnel()
        ctx = ssl.create_default_context(cafile=str(self.certdir / "ca.pem"))
        tls = ctx.wrap_socket(raw, server_hostname=BRIDGE)
        self.sockets.append(tls)
        tls.settimeout(10)
        ext = "Sec-WebSocket-Extensions: permessage-deflate; client_max_window_bits\r\n"
        pieces = request if request is not None else [
            (
                f"GET /chrome/acct-uuid HTTP/1.1\r\nHost: {BRIDGE}\r\n"
                "Connection: Upgrade\r\nUpgrade: websocket\r\n"
                "Origin: https://claude.ai\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                + (ext if extensions else "")
                + "\r\n"
            ).encode()
        ]
        for piece in [pieces] if isinstance(pieces, bytes) else pieces:
            tls.sendall(piece)
            time.sleep(0.1)
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = tls.recv(4096)
            if not chunk:
                break
            head += chunk
        return tls, head

    def trace_text(self) -> str:
        path = self.certdir / "trace.log"
        return path.read_text() if path.exists() else ""

    def stop(self):
        for s in self.sockets:
            try:
                s.close()
            except OSError:
                pass
        self.proxy.stop()
        if self.chain is not None:
            self.chain.stop()
        self.upstream.stop()


@pytest.fixture
def rig_for(certdir, monkeypatch):
    """Build rigs. A ``direct=True`` rig makes the pin's own dial of
    ``bridge.claudeusercontent.com:443`` land on the fake upstream, by
    redirecting only that address in ``socket.create_connection``."""
    rigs: list[Rig] = []
    real_connect = socket.create_connection

    def make(provider, **kw) -> Rig:
        rig = Rig(certdir, provider, **kw)
        rigs.append(rig)
        if kw.get("direct"):

            def redirected(address, *a, **k):
                if address == (BRIDGE, 443):
                    address = ("127.0.0.1", rig.upstream.port)
                return real_connect(address, *a, **k)

            monkeypatch.setattr(socket, "create_connection", redirected)
        return rig

    yield make
    for rig in rigs:
        rig.stop()


def connect_json_seen_upstream(rig: Rig) -> dict:
    assert rig.upstream.first_frame.wait(10), "upstream never got a data frame"
    return json.loads(decode_client_frame(rig.upstream.frames[0])[3])


def assert_no_token_leaked(rig: Rig, lifecycle: list[str]) -> None:
    text = "\n".join(lifecycle) + rig.trace_text()
    for token in (ACTIVE, PINNED):
        assert token not in text, "a token reached the daemon log or trace"
    assert "oauth_token" not in text


class TestHostDecision:
    """CONNECT bridge:443 is terminated only when the pin can improve it."""

    def make(self, certdir, provider):
        return PinProxy(certdir=certdir, pin_token_provider=provider)

    def test_bridge_host_with_a_pinned_token_is_terminated(self, certdir):
        proxy = self.make(certdir, lambda: PINNED)
        assert proxy._pins_chrome_bridge(f"{BRIDGE}:443") is True

    def test_no_pinned_token_stays_blind(self, certdir):
        proxy = self.make(certdir, lambda: None)
        assert proxy._pins_chrome_bridge(f"{BRIDGE}:443") is False

    def test_a_noop_pin_stays_blind(self, certdir):
        def provider():
            return PINNED

        provider.pin_is_noop = lambda: True
        proxy = self.make(certdir, provider)
        assert proxy._pins_chrome_bridge(f"{BRIDGE}:443") is False

    def test_a_failing_token_lookup_stays_blind_and_says_so(self, certdir, lifecycle):
        def provider():
            raise RuntimeError("store down")

        proxy = self.make(certdir, provider)
        assert proxy._pins_chrome_bridge(f"{BRIDGE}:443") is False
        assert any("token lookup failed" in line for line in lifecycle)

    @pytest.mark.parametrize(
        "target",
        [
            "example.com:443",
            "claudeusercontent.com:443",
            f"evil.{BRIDGE}:443",
            f"{BRIDGE}.evil.example:443",
            f"x{BRIDGE}:443",
            f"{BRIDGE}.:443",
            "api.anthropic.com:443",
        ],
    )
    def test_any_other_host_stays_blind(self, certdir, target):
        proxy = self.make(certdir, lambda: PINNED)
        assert proxy._pins_chrome_bridge(target) is False

    @pytest.mark.parametrize("target", [f"{BRIDGE}:8443", f"{BRIDGE}:80", BRIDGE, f"{BRIDGE}:"])
    def test_any_other_port_stays_blind(self, certdir, target):
        proxy = self.make(certdir, lambda: PINNED)
        assert proxy._pins_chrome_bridge(target) is False

    def test_a_certificate_that_cannot_be_issued_stays_blind_once_logged(
        self, certdir, lifecycle, monkeypatch
    ):
        def refuse(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(pp, "ensure_bridge_leaf", refuse)
        proxy = self.make(certdir, lambda: PINNED)
        assert proxy._pins_chrome_bridge(f"{BRIDGE}:443") is False
        assert proxy._pins_chrome_bridge(f"{BRIDGE}:443") is False
        assert sum("could not issue the bridge certificate" in x for x in lifecycle) == 1

    @pytest.mark.parametrize("failure", [OSError("disk full"), "busy"])
    def test_a_failed_or_busy_certificate_build_is_not_retried_inside_its_delay(
        self, certdir, lifecycle, monkeypatch, failure
    ):
        attempts: list[int] = []
        tokens: list[int] = []

        def refuse(*a, **k):
            attempts.append(1)
            raise pp.SpawnLockBusy("held") if failure == "busy" else failure

        monkeypatch.setattr(pp, "ensure_bridge_leaf", refuse)
        proxy = self.make(certdir, lambda: tokens.append(1) or PINNED)
        for _ in range(4):
            assert proxy._pins_chrome_bridge(f"{BRIDGE}:443") is False
        assert attempts == [1]
        assert tokens == [1], "the credential store was read for a connection going blind"

        proxy._chrome_bridge_ctx_retry_at = 0.0
        assert proxy._pins_chrome_bridge(f"{BRIDGE}:443") is False
        assert attempts == [1, 1]

    def test_a_certificate_that_builds_after_a_failure_is_used_and_cached(
        self, certdir, monkeypatch
    ):
        proxy = self.make(certdir, lambda: PINNED)
        real = pp.ensure_bridge_leaf
        monkeypatch.setattr(
            pp, "ensure_bridge_leaf", lambda *a, **k: (_ for _ in ()).throw(OSError("x"))
        )
        assert proxy._pins_chrome_bridge(f"{BRIDGE}:443") is False
        monkeypatch.setattr(pp, "ensure_bridge_leaf", real)
        proxy._chrome_bridge_ctx_retry_at = 0.0
        assert proxy._pins_chrome_bridge(f"{BRIDGE}:443") is True
        assert proxy._chrome_bridge_ctx is not None

    def test_the_client_refusal_window_blocks_the_bridge_without_reading_credentials(
        self, certdir
    ):
        tokens: list[int] = []
        proxy = self.make(certdir, lambda: tokens.append(1) or PINNED)
        proxy._chrome_bridge_blind_until = time.monotonic() + 900
        assert proxy._pins_chrome_bridge(f"{BRIDGE}:443") is False
        assert tokens == []
        proxy._chrome_bridge_blind_until = time.monotonic() - 1
        assert proxy._pins_chrome_bridge(f"{BRIDGE}:443") is True

    def test_two_refusals_in_a_row_open_the_window_one_does_not(self, certdir, lifecycle):
        proxy = self.make(certdir, lambda: PINNED)
        proxy._note_chrome_bridge_client(False, "SSLError")
        assert proxy._pins_chrome_bridge(f"{BRIDGE}:443") is True
        proxy._note_chrome_bridge_client(False, "SSLError")
        assert proxy._pins_chrome_bridge(f"{BRIDGE}:443") is False
        assert any("2 times in a row" in x for x in lifecycle)
        proxy._chrome_bridge_blind_until = 0.0
        proxy._note_chrome_bridge_client(False, "SSLError")
        assert proxy._pins_chrome_bridge(f"{BRIDGE}:443") is True

    def routed(self, certdir, provider, target):
        proxy = self.make(certdir, provider)
        calls: list[tuple] = []
        proxy._blind_tunnel = lambda t, c, **kw: calls.append((t, kw))
        client, server = socket.socketpair()
        try:
            client.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
            proxy._handle_client(server)
        finally:
            client.close()
            server.close()
        return calls

    def test_handle_client_routes_the_bridge_to_the_terminating_path(self, certdir):
        assert self.routed(certdir, lambda: PINNED, f"{BRIDGE}:443") == [
            (f"{BRIDGE}:443", {"bridge": True})
        ]

    def test_handle_client_keeps_every_other_connect_on_the_plain_blind_path(
        self, certdir
    ):
        assert self.routed(certdir, lambda: None, f"{BRIDGE}:443") == [
            (f"{BRIDGE}:443", {})
        ]
        assert self.routed(certdir, lambda: PINNED, "example.com:443") == [
            ("example.com:443", {})
        ]


class TestBridgeCertificate:
    def test_leaf_is_for_the_bridge_host_only_and_signed_by_the_same_ca(self, certdir):
        before = {n: (certdir / n).read_bytes() for n in ("ca.pem", "ca.key", "leaf.pem", "leaf.key")}
        bundle = pp.ensure_bridge_leaf(certdir, BRIDGE)
        ca = x509.load_pem_x509_certificate((certdir / "ca.pem").read_bytes())
        leaf = x509.load_pem_x509_certificate(bundle.leaf_path.read_bytes())

        assert bundle.leaf_path.name == "leaf-bridge.pem"
        assert bundle.leaf_key_path.name == "leaf-bridge.key"
        san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        assert san.value.get_values_for_type(x509.DNSName) == [BRIDGE]
        eku = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        assert list(eku) == [ExtendedKeyUsageOID.SERVER_AUTH]
        aki = leaf.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier)
        assert aki.value == x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.public_key())
        assert leaf.issuer == ca.subject
        span = leaf.not_valid_after_utc - leaf.not_valid_before_utc
        assert span.days <= pp._LEAF_DAYS + 1
        assert pp._certs_consistent(
            certdir / "ca.pem", certdir / "ca.key", bundle.leaf_path, bundle.leaf_key_path, BRIDGE
        )
        after = {n: (certdir / n).read_bytes() for n in before}
        assert after == before, "the CA or the api leaf was touched"
        assert (bundle.leaf_key_path.stat().st_mode & 0o777) == 0o600

    def test_a_good_leaf_is_reused_not_reissued(self, certdir):
        first = pp.ensure_bridge_leaf(certdir, BRIDGE).leaf_path.read_bytes()
        second = pp.ensure_bridge_leaf(certdir, BRIDGE).leaf_path.read_bytes()
        assert first == second

    def test_a_leaf_under_a_replaced_ca_is_reissued_under_the_new_one(self, certdir):
        pp.ensure_bridge_leaf(certdir, BRIDGE)
        old = (certdir / "leaf-bridge.pem").read_bytes()
        ca_cert, ca_key = pp._make_ca()
        pp._write_public(certdir / "ca.pem", ca_cert.public_bytes(pp.serialization.Encoding.PEM))
        pp._write_key(certdir / "ca.key", ca_key)

        pp.ensure_bridge_leaf(certdir, BRIDGE)
        new = (certdir / "leaf-bridge.pem").read_bytes()

        assert new != old
        leaf = x509.load_pem_x509_certificate(new)
        ca_cert.public_key().verify(
            leaf.signature,
            leaf.tbs_certificate_bytes,
            pp.padding.PKCS1v15(),
            leaf.signature_hash_algorithm,
        )

    def test_an_unusable_ca_raises_and_is_never_replaced(self, tmp_path):
        empty = tmp_path / "no-ca"
        with pytest.raises(RuntimeError):
            pp.ensure_bridge_leaf(empty, BRIDGE)
        assert not (empty / "ca.pem").exists()
        assert not (empty / "ca.key").exists()


class TestFrameCodec:
    def rewrite(self, frame: bytes, token=PINNED):
        return pp._ws_rewrite_connect_frame(frame, lambda: token)

    def test_a_short_connect_frame_gets_the_pinned_token_and_keeps_every_other_field(self):
        extra = {"nested": {"list": [1, 2.5, None, True], "text": "é✓"}, "n": 7}
        payload = connect_payload(**extra)
        new, reason = self.rewrite(client_frame(payload))

        assert reason == ""
        first, lenbyte, mask, body = decode_client_frame(new)
        assert first == 0x81 and lenbyte & 0x80
        sent = json.loads(body)
        assert sent == {
            "type": "connect",
            "client_type": "claude-code",
            "oauth_token": PINNED,
            **extra,
        }
        assert b" " not in body, "not compact"

    def test_a_seven_bit_length_frame_is_re_framed_with_a_seven_bit_length(self):
        payload = json.dumps(
            {"type": "connect", "client_type": "claude-code", "oauth_token": ACTIVE}, separators=(",", ":")
        ).encode()
        assert len(payload) < 126
        new, _ = self.rewrite(client_frame(payload), token="p" * 10)
        _, lenbyte, _, body = decode_client_frame(new)
        assert lenbyte & 0x7F == len(body) < 126

    def test_a_sixteen_bit_length_frame_is_re_framed_with_a_sixteen_bit_length(self):
        payload = connect_payload(pad="x" * 400)
        assert 125 < len(payload) < 1 << 16
        new, _ = self.rewrite(client_frame(payload))
        _, lenbyte, _, body = decode_client_frame(new)
        assert lenbyte & 0x7F == 126
        assert int.from_bytes(new[2:4], "big") == len(body)
        assert json.loads(body)["pad"] == "x" * 400

    def test_a_rewrite_that_grows_past_125_bytes_switches_to_a_sixteen_bit_length(self):
        payload = json.dumps(
            {"type": "connect", "client_type": "claude-code", "oauth_token": "t"}, separators=(",", ":")
        ).encode()
        assert len(payload) < 126
        new, _ = self.rewrite(client_frame(payload), token="p" * 200)
        _, lenbyte, _, body = decode_client_frame(new)
        assert lenbyte & 0x7F == 126 and len(body) > 125

    def test_a_sixty_four_bit_length_is_encoded_and_round_trips(self):
        payload = b"y" * 70000
        frame = pp._ws_encode_masked_text_frame(payload, b"\x01\x02\x03\x04")
        assert frame[1] & 0x7F == 127
        assert int.from_bytes(frame[2:10], "big") == 70000
        assert decode_client_frame(frame)[3] == payload

    def test_a_sixty_four_bit_length_on_the_wire_is_parsed(self):
        payload = b"z" * 70000
        header = pp._ws_frame_header(client_frame(payload))
        assert header == (14, 70000)

    def test_a_new_random_mask_is_applied_to_the_new_payload(self, monkeypatch):
        monkeypatch.setattr(pp.os, "urandom", lambda n: b"\xde\xad\xbe\xef"[:n])
        new, _ = self.rewrite(client_frame(connect_payload(), mask=b"\x01\x02\x03\x04"))
        _, _, mask, body = decode_client_frame(new)
        assert mask == b"\xde\xad\xbe\xef"
        assert json.loads(body)["oauth_token"] == PINNED
        raw_payload = new[6:]
        assert raw_payload != body, "the payload is still in the clear on the wire"

    def test_the_mask_is_not_reused_between_rewrites(self):
        frame = client_frame(connect_payload())
        masks = {decode_client_frame(self.rewrite(frame)[0])[2] for _ in range(6)}
        assert len(masks) > 1

    @pytest.mark.parametrize(
        "name,frame,fragment",
        [
            ("binary", client_frame(connect_payload(), opcode=2), "text frame"),
            ("fragmented first", client_frame(connect_payload(), fin=False), "text frame"),
            ("continuation", client_frame(connect_payload(), opcode=0), "text frame"),
            ("compressed", client_frame(connect_payload(), rsv=0x40), "text frame"),
            ("unmasked", client_frame(connect_payload(), masked=False), "masked"),
            ("not json", client_frame(b"{nope"), "JSON"),
            ("invalid utf8", client_frame(b"\xff\xfe\xfd"), "JSON"),
            ("json array", client_frame(b'["connect"]'), "connect"),
            ("other type", client_frame(b'{"type":"ping","oauth_token":"t"}'), "connect"),
            ("no type", client_frame(b'{"oauth_token":"t"}'), "connect"),
            ("no token key", client_frame(b'{"type":"connect","client_type":"claude-code"}'), "oauth_token"),
            ("null token", client_frame(b'{"type":"connect","client_type":"claude-code","oauth_token":null}'), "oauth_token"),
            ("truncated", client_frame(connect_payload())[:-3], "whole frame"),
            ("trailing bytes", client_frame(connect_payload()) + b"\x00", "whole frame"),
            ("empty", b"", "whole frame"),
        ],
    )
    def test_anything_but_a_single_text_connect_frame_is_not_rewritten(
        self, name, frame, fragment
    ):
        new, reason = self.rewrite(frame)
        assert new is None, name
        assert fragment in reason

    def test_no_pinned_token_at_frame_time_is_not_a_rewrite(self):
        new, reason = self.rewrite(client_frame(connect_payload()), token=None)
        assert new is None and "no pinned token" in reason

    def test_a_failing_token_provider_is_not_a_rewrite_and_the_reason_names_no_secret(self):
        def boom():
            raise RuntimeError(f"cannot read {PINNED}")

        new, reason = pp._ws_rewrite_connect_frame(client_frame(connect_payload()), boom)
        assert new is None
        assert PINNED not in reason and "RuntimeError" in reason

    def test_the_token_is_only_fetched_once_the_frame_qualified(self):
        calls: list[int] = []

        def provider():
            calls.append(1)
            return PINNED

        pp._ws_rewrite_connect_frame(client_frame(b'{"type":"ping"}'), provider)
        pp._ws_rewrite_connect_frame(client_frame(connect_payload(), opcode=2), provider)
        assert calls == []

    @pytest.mark.parametrize(
        "payload",
        [
            b'{"type":"connect","client_type":"other-client","oauth_token":"t"}',
            b'{"type":"connect","client_type":null,"oauth_token":"t"}',
            b'{"type":"connect","oauth_token":"t"}',
        ],
    )
    def test_only_the_claude_code_client_type_is_rewritten(self, payload):
        calls: list[int] = []
        new, reason = pp._ws_rewrite_connect_frame(
            client_frame(payload), lambda: calls.append(1) or PINNED
        )
        assert new is None and "client_type" in reason
        assert calls == []

    def test_a_lone_surrogate_in_the_frame_or_the_token_does_not_raise(self):
        payload = (
            b'{"type":"connect","client_type":"claude-code",'
            b'"oauth_token":"t","note":"\\ud800 half"}'
        )
        new, reason = self.rewrite(client_frame(payload), token="tok\ud800en")
        assert reason == "" and new is not None
        sent = json.loads(decode_client_frame(new)[3])
        assert sent["oauth_token"] == "tok\ud800en"
        assert sent["note"] == "\ud800 half"

    @pytest.mark.parametrize("token", [b"PINNED-AS-BYTES", 12345, ["x"]])
    def test_a_token_that_is_not_text_is_not_a_rewrite(self, token):
        new, reason = self.rewrite(client_frame(connect_payload()), token=token)
        assert new is None and "not text" in reason

    def test_a_serialization_failure_returns_the_original_and_leaks_nothing(
        self, monkeypatch
    ):
        def boom(*a, **k):
            raise ValueError(f"cannot dump {PINNED}")

        frame = client_frame(connect_payload())
        monkeypatch.setattr(pp.json, "dumps", boom)
        new, reason = self.rewrite(frame)
        assert new is None
        assert "ValueError" in reason and PINNED not in reason

    def test_the_relay_forwards_the_original_bytes_when_the_token_is_not_text(
        self, certdir, lifecycle
    ):
        frame = client_frame(connect_payload(ACTIVE))
        out = TestFirstFrameRelay().relay(certdir, [frame], provider=lambda: b"bytes")
        assert out == frame
        assert any("not text" in x for x in lifecycle)


class TestFirstFrameRelay:
    """The reader that finds the connect frame in the client's byte stream."""

    def relay(self, certdir, chunks, provider=lambda: PINNED, hangup=False):
        proxy = PinProxy(certdir=certdir, pin_token_provider=provider)
        a, b = socket.socketpair()
        sink = Sink()
        try:
            def feed():
                try:
                    for chunk in chunks:
                        if chunk is None:
                            return
                        b.sendall(chunk)
                        time.sleep(0.05)
                    if hangup:
                        b.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

            t = threading.Thread(target=feed, daemon=True)
            t.start()
            proxy._relay_chrome_bridge_first_frame(a, sink)
            t.join(5)
        finally:
            a.close()
            b.close()
        return bytes(sink.data)

    def sent_payload(self, data: bytes) -> dict:
        return json.loads(decode_client_frame(data)[3])

    def test_the_connect_frame_is_rewritten(self, certdir, lifecycle):
        out = self.relay(certdir, [client_frame(connect_payload())])
        assert self.sent_payload(out)["oauth_token"] == PINNED
        assert any("re-authenticated" in x for x in lifecycle)

    def test_a_frame_split_across_reads_is_reassembled(self, certdir):
        frame = client_frame(connect_payload(pad="p" * 300))
        out = self.relay(certdir, [frame[:1], frame[1:5], frame[5:100], frame[100:]])
        assert self.sent_payload(out)["oauth_token"] == PINNED

    def test_a_control_frame_before_the_connect_frame_is_forwarded_as_is(self, certdir):
        ping = client_frame(b"hi", opcode=9)
        out = self.relay(certdir, [ping + client_frame(connect_payload())])
        assert out.startswith(ping)
        assert self.sent_payload(out[len(ping):])["oauth_token"] == PINNED

    def test_bytes_after_the_connect_frame_follow_it_untouched(self, certdir):
        later = client_frame(b'{"type":"x"}', mask=b"\x09\x08\x07\x06")
        out = self.relay(certdir, [client_frame(connect_payload()) + later])
        assert out.endswith(later)
        assert self.sent_payload(out[: -len(later)])["oauth_token"] == PINNED

    def test_a_frame_over_the_cap_is_forwarded_untouched(self, certdir, lifecycle):
        big = client_frame(connect_payload(pad="q" * (pp._WS_FIRST_FRAME_CAP + 10)))
        sent = big[: pp._WS_FIRST_FRAME_CAP // 2]
        out = self.relay(certdir, [sent])
        assert len(out) > 14 and sent.startswith(out), "bytes were altered"
        assert any("too large" in x for x in lifecycle)

    def test_a_non_connect_first_frame_is_forwarded_untouched(self, certdir, lifecycle):
        frame = client_frame(b'{"type":"other","oauth_token":"%s"}' % ACTIVE.encode())
        assert self.relay(certdir, [frame]) == frame
        assert any("forwarded as sent" in x for x in lifecycle)

    def test_a_fragmented_first_frame_is_forwarded_untouched(self, certdir):
        frame = client_frame(connect_payload(), fin=False)
        assert self.relay(certdir, [frame]) == frame

    def test_no_pinned_token_at_frame_time_forwards_the_original(self, certdir, lifecycle):
        frame = client_frame(connect_payload())
        assert self.relay(certdir, [frame], provider=lambda: None) == frame
        assert any("no pinned token" in x for x in lifecycle)

    def test_silence_is_bounded_and_forwards_nothing(self, certdir, lifecycle, monkeypatch):
        monkeypatch.setattr(pp, "_CHROME_BRIDGE_FIRST_FRAME_WAIT_S", 0.3)
        started = time.monotonic()
        out = self.relay(certdir, [None])
        assert out == b"" and time.monotonic() - started < 3
        assert any("no data frame in time" in x for x in lifecycle)

    def test_a_client_that_hangs_up_first_forwards_what_it_sent(self, certdir, lifecycle):
        frame = client_frame(connect_payload())
        out = self.relay(certdir, [frame[:5]], hangup=True)
        assert out == frame[:5]
        assert any("client closed" in x for x in lifecycle)

    def test_the_log_lines_carry_no_token_or_payload(self, certdir, lifecycle):
        self.relay(certdir, [client_frame(connect_payload())])
        self.relay(certdir, [client_frame(connect_payload())], provider=lambda: None)
        text = "\n".join(lifecycle)
        assert ACTIVE not in text and PINNED not in text and "oauth_token" not in text


class TestThroughThePin:
    """Client, pin, egress chain and a fake TLS WebSocket upstream, end to end."""

    def test_the_connect_frame_reaches_the_bridge_with_the_pinned_token(
        self, rig_for, lifecycle
    ):
        rig = rig_for(lambda: PINNED)
        tls, head = rig.connect()
        assert head.startswith(b"HTTP/1.1 101")

        first = client_frame(connect_payload(ACTIVE))
        tls.sendall(first)
        sent = connect_json_seen_upstream(rig)
        assert sent == {
            "type": "connect",
            "client_type": "claude-code",
            "oauth_token": PINNED,
        }
        assert rig.upstream.frames[0] != first

        assert read_frame(tls) == SERVER_FRAME
        second = client_frame(b'{"type":"message","n":1}', mask=b"\xa1\xb2\xc3\xd4")
        tls.sendall(second)
        assert rig.upstream.second_frame.wait(10)
        assert rig.upstream.frames[1] == second

        assert rig.chain.targets == [f"{BRIDGE}:443"]
        assert any("connect frame re-authenticated" in x for x in lifecycle)
        assert_no_token_leaked(rig, lifecycle)

    def test_sec_websocket_extensions_is_dropped_and_the_rest_of_the_request_kept(
        self, rig_for
    ):
        rig = rig_for(lambda: PINNED)
        tls, head = rig.connect(extensions=True)
        assert head.startswith(b"HTTP/1.1 101")
        tls.sendall(client_frame(connect_payload()))
        connect_json_seen_upstream(rig)

        sent = rig.upstream.heads[0].lower()
        assert b"sec-websocket-extensions" not in sent
        assert b"permessage-deflate" not in sent
        assert sent.startswith(b"get /chrome/acct-uuid http/1.1\r\n")
        for kept in (
            b"host: " + BRIDGE.encode(),
            b"origin: https://claude.ai",
            b"sec-websocket-key: dgh",
            b"sec-websocket-version: 13",
            b"upgrade: websocket",
        ):
            assert kept in sent

    def test_a_pinned_token_that_vanishes_at_frame_time_sends_the_original(
        self, rig_for, lifecycle
    ):
        state = {"token": PINNED}
        rig = rig_for(lambda: state["token"])
        tls, head = rig.connect()
        assert head.startswith(b"HTTP/1.1 101")
        state["token"] = None
        first = client_frame(connect_payload(ACTIVE))
        tls.sendall(first)

        assert connect_json_seen_upstream(rig)["oauth_token"] == ACTIVE
        assert rig.upstream.frames[0] == first
        assert any("no pinned token" in x for x in lifecycle)
        assert not any("re-authenticated" in x for x in lifecycle)
        assert_no_token_leaked(rig, lifecycle)

    @staticmethod
    def read_to_eof(tls) -> bytes:
        tls.settimeout(10)
        out = b""
        while True:
            try:
                chunk = tls.recv(4096)
            except (ssl.SSLError, OSError):
                return out
            if not chunk:
                return out
            out += chunk

    def test_a_refused_upgrade_is_relayed_whole_not_cut_at_its_first_chunk(self, rig_for):
        rig = rig_for(lambda: PINNED, status=403)
        tls, head = rig.connect()
        assert head.startswith(b"HTTP/1.1 403")
        assert b"sec-websocket-extensions" not in rig.upstream.heads[0].lower()
        _, _, first_body = head.partition(b"\r\n\r\n")
        assert first_body + self.read_to_eof(tls) == REPLY

    def test_a_request_with_a_body_is_forwarded_whole_and_its_reply_relayed_whole(
        self, rig_for
    ):
        rig = rig_for(lambda: PINNED)
        tls, head = rig.connect(
            request=[
                f"POST /x HTTP/1.1\r\nHost: {BRIDGE}\r\nContent-Length: 5\r\n\r\n".encode(),
                b"hello",
            ]
        )
        assert head.startswith(b"HTTP/1.1 200")
        _, _, first_body = head.partition(b"\r\n\r\n")
        assert first_body + self.read_to_eof(tls) == REPLY
        assert rig.upstream.bodies == [b"hello"]
        assert not rig.upstream.frames

    def test_the_tunnel_owes_nothing_once_it_is_up(self, rig_for):
        rig = rig_for(lambda: PINNED)
        tls, _ = rig.connect()
        tls.sendall(client_frame(connect_payload()))
        assert connect_json_seen_upstream(rig)["oauth_token"] == PINNED
        assert read_frame(tls) == SERVER_FRAME
        assert rig.proxy.inflight_requests() == 0

    def test_stopping_the_pin_closes_the_bridge_tunnel(self, rig_for):
        rig = rig_for(lambda: PINNED)
        tls, _ = rig.connect()
        tls.sendall(client_frame(connect_payload()))
        assert connect_json_seen_upstream(rig)["oauth_token"] == PINNED
        assert read_frame(tls) == SERVER_FRAME
        assert pp._PUMP.live_pairs() >= 1

        rig.proxy.stop()
        tls.settimeout(10)
        try:
            data = tls.recv(4096)
        except (ssl.SSLError, OSError):
            data = b""
        assert data == b""
        assert pp._PUMP.live_pairs() == 0

    def test_no_pin_means_the_old_blind_tunnel_and_the_original_token(
        self, rig_for, lifecycle
    ):
        rig = rig_for(lambda: None)
        tls, head = rig.connect()
        assert head.startswith(b"HTTP/1.1 101")
        first = client_frame(connect_payload(ACTIVE))
        tls.sendall(first)

        assert connect_json_seen_upstream(rig)["oauth_token"] == ACTIVE
        assert rig.upstream.frames[0] == first
        assert b"sec-websocket-extensions" in rig.upstream.heads[0].lower()
        assert not any("chrome bridge" in x for x in lifecycle)

    def test_a_verified_upstream_gets_the_pinned_token(
        self, rig_for, lifecycle, monkeypatch
    ):
        loopback_flags: list[bool] = []
        real_ctx = PinProxy._upstream_ctx

        def spy(self, via_loopback):
            loopback_flags.append(via_loopback)
            return real_ctx(self, via_loopback)

        monkeypatch.setattr(PinProxy, "_upstream_ctx", spy)
        rig = rig_for(lambda: PINNED, direct=True)
        tls, head = rig.connect()
        assert head.startswith(b"HTTP/1.1 101")
        tls.sendall(client_frame(connect_payload(ACTIVE)))

        assert connect_json_seen_upstream(rig)["oauth_token"] == PINNED
        assert loopback_flags == [False], "the upstream was not verified"
        assert any("re-authenticated" in x for x in lifecycle)
        assert_no_token_leaked(rig, lifecycle)

    def test_an_upstream_certificate_for_another_host_falls_back_to_the_blind_tunnel(
        self, rig_for, lifecycle
    ):
        rig = rig_for(lambda: PINNED, direct=True, wrong_cert_first=True)
        tls, head = rig.connect()
        assert head.startswith(b"HTTP/1.1 101")
        first = client_frame(connect_payload(ACTIVE))
        tls.sendall(first)

        assert connect_json_seen_upstream(rig)["oauth_token"] == ACTIVE
        assert rig.upstream.frames[0] == first
        assert any(
            "TLS to" in x and "SSLCertVerificationError" in x and "blind" in x
            for x in lifecycle
        )
        assert_no_token_leaked(rig, lifecycle)

    def test_a_client_that_refuses_the_leaf_twice_sends_the_bridge_blind_then_recovers(
        self, rig_for, lifecycle
    ):
        rig = rig_for(lambda: PINNED)
        for _ in range(2):
            rig.refuse_certificate()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not any(
            "2 times in a row" in x for x in lifecycle
        ):
            time.sleep(0.05)
        assert any("2 times in a row" in x and "15 minutes" in x for x in lifecycle)

        tls, head = rig.connect()
        assert head.startswith(b"HTTP/1.1 101")
        first = client_frame(connect_payload(ACTIVE))
        tls.sendall(first)
        assert connect_json_seen_upstream(rig)["oauth_token"] == ACTIVE
        assert rig.upstream.frames[0] == first

        rig.proxy._chrome_bridge_blind_until = time.monotonic() - 1
        assert rig.proxy._pins_chrome_bridge(f"{BRIDGE}:443") is True
        assert not any(ACTIVE in x or PINNED in x for x in lifecycle)

    def test_one_refusal_does_not_blind_the_bridge_and_an_accepted_handshake_resets_the_count(
        self, rig_for
    ):
        rig = rig_for(lambda: PINNED)
        rig.refuse_certificate()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not rig.proxy._chrome_bridge_client_failures:
            time.sleep(0.05)
        assert rig.proxy._chrome_bridge_client_failures == 1
        assert rig.proxy._pins_chrome_bridge(f"{BRIDGE}:443") is True

        tls, head = rig.connect()
        assert head.startswith(b"HTTP/1.1 101")
        assert rig.proxy._chrome_bridge_client_failures == 0

    def test_a_client_that_aborts_mid_handshake_is_not_a_refusal_and_resets_nothing(
        self, rig_for, lifecycle
    ):
        rig = rig_for(lambda: PINNED)
        rig.refuse_certificate()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not rig.proxy._chrome_bridge_client_failures:
            time.sleep(0.05)
        assert rig.proxy._chrome_bridge_client_failures == 1

        for _ in range(3):
            rig.abort_handshake()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and rig.proxy.live_client_count():
            time.sleep(0.05)
        assert rig.proxy.live_client_count() == 0
        assert rig.proxy._chrome_bridge_client_failures == 1
        assert rig.proxy._chrome_bridge_blind_until == 0.0
        assert rig.proxy._pins_chrome_bridge(f"{BRIDGE}:443") is True
        assert sum("refused the bridge certificate" in x for x in lifecycle) == 1

    def test_only_certificate_alerts_are_rejections(self):
        def error(reason, cls=ssl.SSLError):
            exc = cls(1, "x")
            exc.reason = reason
            return exc

        for reason in (
            "TLSV1_ALERT_UNKNOWN_CA",
            "SSLV3_ALERT_BAD_CERTIFICATE",
            "TLSV1_ALERT_CERTIFICATE_UNKNOWN",
            "SSLV3_ALERT_CERTIFICATE_EXPIRED",
        ):
            assert pp._is_certificate_rejection(error(reason)), reason
        for reason in (
            "TLSV1_ALERT_PROTOCOL_VERSION",
            "TLSV1_ALERT_INTERNAL_ERROR",
            "UNEXPECTED_EOF_WHILE_READING",
            "UNKNOWN_CA",
            None,
        ):
            assert not pp._is_certificate_rejection(error(reason)), reason
        assert not pp._is_certificate_rejection(ConnectionResetError())
        assert not pp._is_certificate_rejection(TimeoutError())
        assert not pp._is_certificate_rejection(ssl.SSLEOFError(8, "eof"))

    def test_a_client_that_never_handshakes_is_dropped_within_its_budget(
        self, rig_for, monkeypatch
    ):
        monkeypatch.setattr(pp, "_CHROME_BRIDGE_CLIENT_BUDGET_S", 0.5)
        rig = rig_for(lambda: PINNED)
        raw = rig.open_tunnel()
        raw.settimeout(5)
        try:
            data = raw.recv(4096)
        except ConnectionResetError:
            data = b""
        assert data == b"", "the pin kept waiting for a client that never spoke"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and rig.proxy.live_client_count():
            time.sleep(0.05)
        assert rig.proxy.live_client_count() == 0
        assert rig.proxy._chrome_bridge_client_failures == 0
