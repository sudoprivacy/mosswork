"""Fixed-endpoint JSON egress, ported from Moss routerTransport.ts (MIT).

Source: sudoprivacy/moss d0b84d4c, src/server/agentFederation/provider/
routerTransport.ts. This transport does not authorize a caller or a tool.
"""

from __future__ import annotations

import asyncio
import ipaddress
import math
import re
import socket
import ssl
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import Any, Literal

import httpcore
import httpx

type Resolver = Callable[[str], Awaitable[Sequence[str]]]
type DispatchAssertion = Callable[[], Awaitable[None]]
type IP = ipaddress.IPv4Address | ipaddress.IPv6Address


async def assert_dispatch_current(assertion: DispatchAssertion) -> None:
    """A host assertion completes with None or raises; verdict objects refuse."""
    try:
        pending: Awaitable[object] = assertion()
        result = await pending
    except Exception:
        raise FixedJsonError("EGRESS_DENIED") from None
    if result is not None:
        raise FixedJsonError("EGRESS_DENIED")


_NONPUBLIC_V4 = tuple(
    ipaddress.ip_network(value)
    for value in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
    )
)
_NONPUBLIC_V6 = tuple(
    ipaddress.ip_network(v)
    for v in (
        "2001::/23",
        "2001:db8::/32",
        "2002::/16",
        "3fff::/20",
    )
)
_FORBIDDEN_V4 = tuple(
    ipaddress.ip_network(v)
    for v in (
        "0.0.0.0/8",
        "169.254.0.0/16",
        "224.0.0.0/4",
        "240.0.0.0/4",
    )
)
_FORBIDDEN_V6 = tuple(
    ipaddress.ip_network(v)
    for v in (
        "::/96",
        "::ffff:0:0/96",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "2001::/32",
        "2002::/16",
        "fe80::/10",
        "fec0::/10",
        "ff00::/8",
    )
)
_JSON = re.compile(r"^application/json(?:\s*;|$)", re.I)
_SESSION = re.compile(r"^[\x21-\x7e]{1,256}$")
_REQUEST_LIMIT = 2 * 1024 * 1024
_ERROR_LIMIT = 65536
_AUTH_FORBIDDEN = frozenset(
    (
        "host",
        "connection",
        "content-length",
        "transfer-encoding",
        "mcp-session-id",
        "mcp-protocol-version",
        "traceparent",
    )
)


class FixedJsonError(httpx.TransportError):
    """Sanitized failure; an unconfirmed execution must never be replayed."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def validate_auth_headers(headers: httpx.Headers) -> None:
    """Check resolved registration credentials before HTTPX adds framing headers."""
    if any(name.lower() in _AUTH_FORBIDDEN for name in headers):
        raise FixedJsonError("EGRESS_DENIED")


def _address(value: str) -> IP:
    try:
        if not isinstance(value, str) or "%" in value:
            raise ValueError
        return ipaddress.ip_address(value)
    except ValueError:
        raise FixedJsonError("EGRESS_DENIED") from None


def _forbidden(ip: IP) -> bool:
    if isinstance(ip, ipaddress.IPv4Address):
        return any(ip in net for net in _FORBIDDEN_V4)
    return str(ip) != "::1" and any(ip in net for net in _FORBIDDEN_V6)


def _public(ip: IP) -> bool:
    if isinstance(ip, ipaddress.IPv4Address):
        return not any(ip in net for net in _NONPUBLIC_V4)
    return ip in ipaddress.ip_network("2000::/3") and not any(ip in net for net in _NONPUBLIC_V6)


async def _resolve(hostname: str) -> Sequence[str]:
    answers = await asyncio.get_running_loop().getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    return [str(item[4][0]) for item in answers]


class _PinnedBackend(httpcore.AsyncNetworkBackend):
    def __init__(
        self, hostname: str, port: int, address: str, assertion: DispatchAssertion | None
    ) -> None:
        self.hostname, self.port, self.address = hostname, port, address
        self.assertion = assertion
        self.backend = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if host != self.hostname or port != self.port or local_address is not None:
            raise FixedJsonError("EGRESS_DENIED")
        # The backend receives a numeric address, never the original DNS name.
        # HTTPcore retains that original name for SNI/certificate verification.
        stream = await self.backend.connect_tcp(
            self.address, port, timeout=timeout, socket_options=socket_options
        )
        return _AdmittedStream(stream, self.assertion)


class _AdmittedStream(httpcore.AsyncNetworkStream):
    def __init__(
        self, stream: httpcore.AsyncNetworkStream, assertion: DispatchAssertion | None
    ) -> None:
        self.stream, self.assertion = stream, assertion
        self.sent = False

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        if self.assertion is not None:
            try:
                await assert_dispatch_current(self.assertion)
            except Exception:
                if self.sent:
                    raise FixedJsonError("EXECUTION_UNCONFIRMED") from None
                raise
        # A failed/partial write can already reach the worker. Later admission
        # refusal must retain that uncertainty, including after the final body.
        self.sent = self.sent or bool(buffer)
        await self.stream.write(buffer, timeout)

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return await self.stream.read(max_bytes, timeout)

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        # Admission surrounds authenticated HTTP bytes, after the TLS wait.
        return _AdmittedStream(
            await self.stream.start_tls(ssl_context, server_hostname, timeout), self.assertion
        )

    def get_extra_info(self, info: str) -> Any:
        return self.stream.get_extra_info(info)

    async def aclose(self) -> None:
        await self.stream.aclose()


class FixedJsonTransport(httpx.AsyncBaseTransport):
    """One checked connection per request, JSON-only, no proxy or replay.

    Endpoint, IP pins and CA file belong to trusted host composition. Never
    construct them from tool arguments. Review profile allows 32 MiB HTML JSON;
    the MCP profile keeps the original 2 MiB bound and refuses SSE locally.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        profile: Literal["mcp", "review"],
        allowed_addresses: Sequence[str] | None = None,
        resolve: Resolver = _resolve,
        ca_file: str | None = None,
        before_dispatch: DispatchAssertion | None = None,
    ) -> None:
        try:
            url = httpx.URL(endpoint)
        except httpx.InvalidURL:
            raise FixedJsonError("INVALID_ENDPOINT") from None
        try:
            literal = ipaddress.ip_address(url.host)
        except ValueError:
            literal = None
        if (
            str(url) != endpoint
            or url.scheme not in ("http", "https")
            or url.userinfo
            or url.query
            or url.fragment
            or endpoint.endswith("/")
            or not url.host
            or (url.scheme == "http" and literal is None)
            or profile not in ("mcp", "review")
        ):
            raise FixedJsonError("INVALID_ENDPOINT")
        if allowed_addresses is not None and (
            isinstance(allowed_addresses, (str, bytes)) or not 1 <= len(allowed_addresses) <= 32
        ):
            raise FixedJsonError("INVALID_NETWORK_POLICY")
        self.pins = (
            None if allowed_addresses is None else frozenset(_address(v) for v in allowed_addresses)
        )
        if self.pins is not None and any(_forbidden(ip) for ip in self.pins):
            raise FixedJsonError("INVALID_NETWORK_POLICY")
        self.url, self.profile, self.literal = url, profile, literal
        self.resolve = resolve
        self.before_dispatch = before_dispatch
        self.ssl_context = ssl.create_default_context(cafile=ca_file)
        self.ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.closed = False

    async def _selected_address(self) -> str:
        try:
            async with asyncio.timeout(10):
                values = [str(self.literal)] if self.literal else await self.resolve(self.url.host)
        except (TimeoutError, OSError):
            raise FixedJsonError("EGRESS_UNAVAILABLE") from None
        if not 1 <= len(values) <= 32:
            raise FixedJsonError("EGRESS_DENIED")
        addresses = [_address(value) for value in values]
        if any(
            _forbidden(ip)
            or (
                ip not in self.pins
                if self.pins is not None
                else self.literal is None and not _public(ip)
            )
            for ip in addresses
        ):
            raise FixedJsonError("EGRESS_DENIED")
        return str(addresses[0])

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            return await self._handle(request)
        except TimeoutError:
            # The outer bound may expire after a POST was sent. Do not imply
            # zero dispatch or allow a caller to silently retry it.
            raise FixedJsonError("EXECUTION_UNCONFIRMED") from None

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.closed or request.url != self.url:
            raise FixedJsonError("EGRESS_DENIED")
        if self.profile == "mcp" and request.method == "GET":
            return httpx.Response(405)
        deleting = self.profile == "mcp" and request.method == "DELETE"
        if request.method != "POST" and not deleting:
            raise FixedJsonError("EGRESS_DENIED")
        if not isinstance(request.stream, httpx.AsyncByteStream):
            raise FixedJsonError("EGRESS_DENIED")
        # Bound streamed bodies before DNS or any authenticated network request.
        async with asyncio.timeout(120):
            body = bytearray()
            async for chunk in request.stream:
                body.extend(chunk)
                if len(body) > _REQUEST_LIMIT:
                    raise FixedJsonError("EGRESS_DENIED")
            headers = self._headers(request, len(body))
            if deleting and body:
                raise FixedJsonError("EGRESS_DENIED")
            address = await self._selected_address()
            try:
                return await self._exchange(request, bytes(body), headers, address, deleting)
            except (httpcore.NetworkError, httpcore.ProtocolError, httpcore.TimeoutException):
                raise FixedJsonError("EXECUTION_UNCONFIRMED") from None

    def _headers(self, request: httpx.Request, size: int) -> list[tuple[bytes, bytes]]:
        headers = request.headers
        if (
            headers.get("host") != self.url.netloc.decode("ascii")
            or "transfer-encoding" in headers
            or ("content-length" in headers and headers["content-length"] != str(size))
            or (request.method == "POST" and not _JSON.match(headers.get("content-type", "")))
        ):
            raise FixedJsonError("EGRESS_DENIED")
        # HTTPX generates Host/framing/Connection itself; never forward overrides.
        removed = {b"connection", b"content-length", b"accept-encoding"}
        result = [(key, value) for key, value in headers.raw if key.lower() not in removed]
        return result + [
            (b"Connection", b"close"),
            (b"Content-Length", str(size).encode("ascii")),
            (b"Accept-Encoding", b"identity"),
        ]

    async def _exchange(
        self,
        request: httpx.Request,
        body: bytes,
        headers: list[tuple[bytes, bytes]],
        address: str,
        deleting: bool,
    ) -> httpx.Response:
        backend = _PinnedBackend(
            self.url.host,
            self.url.port or (443 if self.url.scheme == "https" else 80),
            address,
            self.before_dispatch,
        )
        async with httpcore.AsyncConnectionPool(
            ssl_context=self.ssl_context,
            network_backend=backend,
            retries=0,
            max_connections=1,
            max_keepalive_connections=0,
            http2=False,
        ) as pool:
            response = await pool.handle_async_request(
                httpcore.Request(
                    request.method,
                    httpcore.URL(
                        scheme=self.url.raw_scheme,
                        host=self.url.raw_host,
                        port=self.url.port,
                        target=self.url.raw_path,
                    ),
                    headers=headers,
                    content=body,
                    extensions={"timeout": _timeouts(request)},
                )
            )
            try:
                return await self._response(response, deleting)
            finally:
                await response.aclose()

    async def _response(self, response: httpcore.Response, deleting: bool) -> httpx.Response:
        status = response.status
        if 300 <= status < 400:
            raise FixedJsonError("EXECUTION_UNCONFIRMED")
        success = 200 <= status < 300
        if not success and self.profile == "mcp":
            return httpx.Response(status)
        headers = httpx.Headers(response.headers)
        session = headers.get("mcp-session-id")
        if self.profile == "mcp":
            if session is not None and (
                len(headers.get_list("mcp-session-id")) != 1 or not _SESSION.fullmatch(session)
            ):
                raise FixedJsonError("EXECUTION_UNCONFIRMED")
            if status in (202, 204) or deleting:
                return httpx.Response(status)
        limit = (
            (32 * 1024 * 1024 if self.profile == "review" else _REQUEST_LIMIT)
            if success
            else _ERROR_LIMIT
        )
        body = bytearray()
        async for chunk in response.aiter_stream():
            body.extend(chunk)
            if len(body) > limit:
                raise FixedJsonError("OUTPUT_INVALID")
        content_type = headers.get("content-type", "")
        if not success and not _JSON.match(content_type):
            return httpx.Response(status)
        if (
            not _JSON.match(content_type)
            or headers.get("content-encoding", "identity") != "identity"
        ):
            raise FixedJsonError("OUTPUT_INVALID")
        kept = {"content-type": content_type}
        if self.profile == "mcp" and session is not None:
            kept["mcp-session-id"] = session
        return httpx.Response(
            status, headers=kept, content=None if status in (204, 205) else bytes(body)
        )

    async def aclose(self) -> None:
        self.closed = True


def _timeouts(request: httpx.Request) -> dict[str, float]:
    """Honor shorter caller deadlines; never expand the original 30s bound."""
    supplied = request.extensions.get("timeout", {})
    result = dict.fromkeys(("connect", "read", "write", "pool"), 30.0)
    if not isinstance(supplied, dict):
        raise FixedJsonError("EGRESS_DENIED")
    for key in result:
        value = supplied.get(key)
        if value is None:
            continue
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise FixedJsonError("EGRESS_DENIED")
        result[key] = min(30.0, value)
    return result
