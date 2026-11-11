"""Real HTTP/TLS/MCP boundaries; no production keys or external network."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import datetime
import inspect
import json
import shutil
import ssl
import subprocess
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from octop.infra.connectors.fixed_json import (
    FixedJsonError,
    FixedJsonTransport,
    validate_auth_headers,
)
from octop.infra.connectors.tool_protocol import (
    ToolConnection,
    ToolProtocolError,
    content_digest,
    open_tool_protocol,
    snapshot_arguments,
    validate_arguments,
    validate_host_read_arguments,
)


@contextlib.asynccontextmanager
async def server(respond, *, tls=None):
    requests, tasks = [], set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            lines = (await reader.readuntil(b"\r\n\r\n")).decode("ascii").split("\r\n")
            headers = dict(line.lower().split(": ", 1) for line in lines[1:] if line)
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            request = {"line": lines[0], "headers": headers, "body": body}
            requests.append(request)
            result = respond(request)
            if inspect.isawaitable(result):
                result = await result
            status, headers, body = result
            headers = {"Content-Length": str(len(body)), "Connection": "close", **headers}
            writer.write(
                f"HTTP/1.1 {status} Test\r\n".encode()
                + b"".join(f"{k}: {v}\r\n".encode() for k, v in headers.items())
                + b"\r\n"
                + body
            )
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            tasks.discard(task)

    listener = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=tls)
    try:
        yield listener.sockets[0].getsockname()[1], requests
    finally:
        listener.close()
        await listener.wait_closed()
        if tasks:
            await asyncio.gather(*tasks)


def reply(_):
    return (
        200,
        {"Content-Type": "application/json", "Set-Cookie": "private=discard"},
        b'{"ok":true}',
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "file:///x",
        "http://worker.example/mcp",
        "http://127.0.0.1/mcp/",
        "http://127.0.0.1/mcp?key=secret",
        "http://user:secret@127.0.0.1/mcp",
        "http://127.0.0.1/mcp#fragment",
        "http://127.0.0.1:80/mcp",
    ],
)
def test_invalid_endpoint(endpoint):
    with pytest.raises(FixedJsonError, match="INVALID_ENDPOINT"):
        FixedJsonTransport(endpoint, profile="mcp")


@pytest.mark.parametrize(
    "pins", [[], ["127.0.0.1%scope"], ["169.254.169.254"], ["::ffff:127.0.0.1"], ["224.0.0.1"]]
)
def test_invalid_pins(pins):
    with pytest.raises(FixedJsonError):
        FixedJsonTransport("https://worker.example/mcp", profile="mcp", allowed_addresses=pins)


@pytest.mark.parametrize(
    "answers",
    [
        ["8.8.8.8", "127.0.0.1"],
        ["127.0.0.1"],
        ["10.1.1.1"],
        ["169.254.169.254"],
        ["::ffff:8.8.8.8"],
        ["2001:db8::1"],
        ["3fff::1"],
        [],
        ["8.8.8.8"] * 33,
    ],
)
async def test_all_dns_answers_checked(answers):
    async def resolve(_):
        return answers

    transport = FixedJsonTransport("https://worker.example/mcp", profile="mcp", resolve=resolve)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(FixedJsonError, match="EGRESS_DENIED"):
            await client.post("https://worker.example/mcp", json={})


async def test_real_json_no_proxy_cookie_or_reuse(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    async with server(reply) as (port, requests):
        url = f"http://127.0.0.1:{port}/mcp"
        async with httpx.AsyncClient(transport=FixedJsonTransport(url, profile="mcp")) as client:
            for _ in range(2):
                response = await client.post(url, json={"field": "名字"})
                assert response.json() == {"ok": True}
                assert "set-cookie" not in response.headers
        assert len(requests) == 2
        assert all(
            r["headers"]["connection"] == "close"
            and r["headers"]["accept-encoding"] == "identity"
            and "cookie" not in r["headers"]
            for r in requests
        )


@pytest.mark.parametrize(
    "status,headers,body,code",
    [
        (302, {"Location": "http://127.0.0.1:1/other"}, b"", "EXECUTION_UNCONFIRMED"),
        (200, {"Content-Type": "text/event-stream"}, b"data: private", "OUTPUT_INVALID"),
        (
            200,
            {"Content-Type": "application/json", "Content-Encoding": "gzip"},
            b"gzip",
            "OUTPUT_INVALID",
        ),
        (
            200,
            {"Content-Type": "application/json", "Mcp-Session-Id": "bad value"},
            b"{}",
            "EXECUTION_UNCONFIRMED",
        ),
        (200, {"Content-Type": "application/json"}, b"x" * (2 * 1024 * 1024 + 1), "OUTPUT_INVALID"),
    ],
    ids=["redirect", "sse", "compressed", "invalid-session", "oversized"],
)
async def test_response_refused_without_replay(status, headers, body, code):
    async with server(lambda _: (status, headers, body)) as (port, requests):
        url = f"http://127.0.0.1:{port}/mcp"
        async with httpx.AsyncClient(
            transport=FixedJsonTransport(url, profile="mcp"), follow_redirects=True
        ) as client:
            with pytest.raises(FixedJsonError, match=code):
                await client.post(url, json={})
        assert len(requests) == 1


async def test_request_refused_before_socket():
    async with server(reply) as (port, requests):
        url = f"http://127.0.0.1:{port}/mcp"
        transport = FixedJsonTransport(url, profile="mcp")
        async with httpx.AsyncClient(transport=transport) as client:
            assert (await client.get(url)).status_code == 405
            for method, target, kwargs in (
                ("POST", url + "/other", {"json": {}}),
                ("POST", url, {"json": {}, "headers": {"host": "other"}}),
                ("POST", url, {"json": {}, "headers": {"content-length": "999"}}),
                ("POST", url, {"json": {}, "headers": {"transfer-encoding": "chunked"}}),
                ("POST", url, {"content": b"x" * (2 * 1024 * 1024 + 1)}),
                ("PUT", url, {"json": {}}),
            ):
                with pytest.raises(FixedJsonError, match="EGRESS_DENIED"):
                    await client.request(method, target, **kwargs)
        assert requests == []
        with pytest.raises(FixedJsonError):
            await transport.handle_async_request(httpx.Request("POST", url, json={}))


async def test_review_larger_bound_and_error_json():
    payload = {"html": "x" * (2 * 1024 * 1024 + 1)}
    async with server(
        lambda _: (200, {"Content-Type": "application/json"}, json.dumps(payload).encode())
    ) as (port, _):
        url = f"http://127.0.0.1:{port}/review"
        async with httpx.AsyncClient(transport=FixedJsonTransport(url, profile="review")) as client:
            assert (await client.post(url, json={})).json() == payload
            with pytest.raises(FixedJsonError):
                await client.get(url)
    async with server(
        lambda _: (403, {"Content-Type": "application/json"}, b'{"code":"DENIED"}')
    ) as (port, _):
        url = f"http://127.0.0.1:{port}/review"
        async with httpx.AsyncClient(transport=FixedJsonTransport(url, profile="review")) as client:
            response = await client.post(url, json={})
            assert response.status_code == 403 and response.json() == {"code": "DENIED"}


@pytest.fixture
def tls_files(tmp_path: Path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "worker.example")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("worker.example")]), critical=False
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    ca, private = tmp_path / "ca.pem", tmp_path / "key.pem"
    ca.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(ca, private)
    return tls, str(ca)


async def test_tls_original_hostname_and_one_dns_resolution(tls_files):
    tls, ca = tls_files
    calls = []

    async def resolve(host):
        calls.append(host)
        return ["127.0.0.1"]

    async with server(reply, tls=tls) as (port, requests):
        url = f"https://worker.example:{port}/mcp"
        async with httpx.AsyncClient(
            transport=FixedJsonTransport(
                url, profile="mcp", allowed_addresses=["127.0.0.1"], resolve=resolve, ca_file=ca
            )
        ) as client:
            assert (await client.post(url, json={})).json() == {"ok": True}
        assert calls == ["worker.example"]
        assert requests[0]["headers"]["host"] == f"worker.example:{port}"
        async with httpx.AsyncClient(
            transport=FixedJsonTransport(
                url, profile="mcp", allowed_addresses=["127.0.0.1"], resolve=resolve
            )
        ) as client:
            with pytest.raises(FixedJsonError, match="EXECUTION_UNCONFIRMED"):
                await client.post(url, json={})
        wrong = f"https://different.example:{port}/mcp"
        async with httpx.AsyncClient(
            transport=FixedJsonTransport(
                wrong, profile="mcp", allowed_addresses=["127.0.0.1"], resolve=resolve, ca_file=ca
            )
        ) as client:
            with pytest.raises(FixedJsonError, match="EXECUTION_UNCONFIRMED"):
                await client.post(wrong, json={})
        assert len(requests) == 1


async def test_official_mcp_sdk_json_call_no_network_sse():
    def respond(request):
        if request["line"].startswith("DELETE"):
            return 204, {}, b""
        message = json.loads(request["body"])
        if "id" not in message:
            return 202, {}, b""
        result = {
            "initialize": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fixture", "version": "1"},
            },
            "tools/list": {
                "tools": [{"name": "sudograph_context_read", "inputSchema": {"type": "object"}}]
            },
            "tools/call": {"content": [{"type": "text", "text": "same revision"}]},
        }[message["method"]]
        return (
            200,
            {"Content-Type": "application/json", "Mcp-Session-Id": "original-session"},
            json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode(),
        )

    async with server(respond) as (port, requests):
        url = f"http://127.0.0.1:{port}/mcp"
        async with (
            httpx.AsyncClient(transport=FixedJsonTransport(url, profile="mcp")) as client,
            streamable_http_client(url, http_client=client) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            assert (await session.list_tools()).tools[0].name == "sudograph_context_read"
            result = await session.call_tool("sudograph_context_read", {"object": "widgets"})
            assert result.content[0].text == "same revision"
        assert all(not r["line"].startswith("GET") for r in requests)
        assert (
            sum(json.loads(r["body"]).get("method") == "tools/call" for r in requests if r["body"])
            == 1
        )


@pytest.mark.parametrize(
    "name",
    [
        "host",
        "connection",
        "mcp-session-id",
        "mcp-protocol-version",
        "traceparent",
        "content-length",
        "transfer-encoding",
    ],
)
def test_auth_cannot_replace_control_headers(name):
    with pytest.raises(FixedJsonError):
        validate_auth_headers(httpx.Headers({name: "value"}))
    validate_auth_headers(httpx.Headers({"Authorization": "Bearer private"}))


@pytest.mark.parametrize("cancel", [False, True], ids=["caller-timeout", "task-cancel"])
async def test_post_timeout_or_cancel_closes_original_without_replay(cancel):
    received, closed = asyncio.Event(), asyncio.Event()
    calls = []

    async def handle(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            calls.append(head)
            received.set()
            await reader.read()  # The server deliberately never sends a response.
        finally:
            writer.close()
            await writer.wait_closed()
            closed.set()

    listener = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        url = f"http://127.0.0.1:{listener.sockets[0].getsockname()[1]}/mcp"
        async with httpx.AsyncClient(
            transport=FixedJsonTransport(url, profile="mcp"), timeout=0.1 if not cancel else 5
        ) as client:
            task = asyncio.create_task(client.post(url, json={"operation_id": "original"}))
            await asyncio.wait_for(received.wait(), 2)
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert task.cancelled()
            else:
                with pytest.raises(FixedJsonError, match="EXECUTION_UNCONFIRMED"):
                    await task
            await asyncio.wait_for(closed.wait(), 2)
        assert len(calls) == 1
    finally:
        listener.close()
        await listener.wait_closed()


async def test_cancel_during_dns_never_opens_authenticated_connection():
    entered = asyncio.Event()

    async def resolve(_):
        entered.set()
        await asyncio.Event().wait()

    url = "https://worker.example/mcp"
    async with httpx.AsyncClient(
        transport=FixedJsonTransport(url, profile="mcp", resolve=resolve)
    ) as client:
        task = asyncio.create_task(client.post(url, json={}))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()


async def test_late_admission_failure_after_actual_body_is_unconfirmed():
    applied = asyncio.Event()
    effects, assertions = [], 0

    async def handle(reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            headers = dict(line.lower().split(": ", 1) for line in head[1:] if line)
            body = await reader.readexactly(int(headers["content-length"]))
            effects.append(json.loads(body)["operation_id"])
            applied.set()
        finally:
            writer.close()
            await writer.wait_closed()

    async def current():
        nonlocal assertions
        assertions += 1
        # Force revocation at the actual post-body admission boundary.
        if assertions >= 3:
            await asyncio.wait_for(applied.wait(), 2)
            raise PermissionError("Revoked after body")

    listener = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        url = f"http://127.0.0.1:{listener.sockets[0].getsockname()[1]}/mcp"
        async with httpx.AsyncClient(
            transport=FixedJsonTransport(url, profile="mcp", before_dispatch=current)
        ) as client:
            with pytest.raises(FixedJsonError, match="EXECUTION_UNCONFIRMED"):
                await client.post(url, json={"operation_id": "original"})
        assert effects == ["original"]
    finally:
        listener.close()
        await listener.wait_closed()


@pytest.mark.parametrize("verdict", [False, True, {"allowed": True}], ids=["false", "true", "json"])
async def test_dispatch_assertion_runs_after_connection_before_http_bytes(verdict):
    closed = asyncio.Event()
    received = []

    async def handle(reader, writer):
        received.append(await reader.read())
        writer.close()
        await writer.wait_closed()
        closed.set()

    async def assertion():
        return verdict

    listener = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        url = f"http://127.0.0.1:{listener.sockets[0].getsockname()[1]}/mcp"
        async with httpx.AsyncClient(
            transport=FixedJsonTransport(url, profile="mcp", before_dispatch=assertion)
        ) as client:
            with pytest.raises(FixedJsonError, match="EGRESS_DENIED"):
                await client.post(url, json={})
            await asyncio.wait_for(closed.wait(), 2)
        assert received == [b""]
    finally:
        listener.close()
        await listener.wait_closed()


TOOL = {
    "name": "sudograph_context_read",
    "inputSchema": {
        "type": "object",
        "properties": {"projection": {"type": "string"}},
        "additionalProperties": False,
    },
    "_meta": {
        "moss/input-profile": "bounded-record-v1",
        "worker-review": "sudograph-worker-review/v2",
    },
}


def reviewed_server(state):
    def respond(request):
        if request["line"].startswith("DELETE"):
            return 204, {}, b""
        msg = json.loads(request["body"])
        method = msg["method"]
        if "id" not in msg:
            return 202, {}, b""
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fixture", "version": "1"},
            }
        elif method == "tools/list":
            result = state.get("list", {"tools": [state.get("tool", TOOL)]})
        else:
            state.setdefault("calls", []).append(msg["params"])
            result = state.get("result", {"content": [{"type": "text", "text": "bound context"}]})
        return (
            200,
            {"Content-Type": "application/json", "Mcp-Session-Id": "original-session"},
            json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}).encode(),
        )

    return respond


async def current():
    return None


async def test_reviewed_protocol_current_descriptor_and_original_arguments():
    state = {}
    entered, release = asyncio.Event(), asyncio.Event()
    block = False

    async def check():
        if block:
            entered.set()
            await release.wait()

    async with server(reviewed_server(state)) as (port, requests):
        connection = ToolConnection(
            f"http://127.0.0.1:{port}/mcp", (("Authorization", "Bearer synthetic"),)
        )
        assert "synthetic" not in repr(connection)
        async with open_tool_protocol(connection, check_current=check) as protocol:
            descriptor = (await protocol.descriptors())[0]
            args = {"projection": "original"}
            block = True
            task = asyncio.create_task(protocol.call_reviewed(descriptor, args))
            await entered.wait()
            args["projection"] = "changed while waiting"
            release.set()
            assert await task == {"content": [{"type": "text", "text": "bound context"}]}
            assert state["calls"] == [
                {"name": "sudograph_context_read", "arguments": {"projection": "original"}}
            ]
        assert requests[-1]["line"].startswith("DELETE")


async def test_changed_review_descriptor_refuses_before_tool_call():
    state = {}
    async with server(reviewed_server(state)) as (port, _):
        async with open_tool_protocol(
            ToolConnection(f"http://127.0.0.1:{port}/mcp", ()), check_current=current
        ) as protocol:
            descriptor = (await protocol.descriptors())[0]
            changed = copy.deepcopy(TOOL)
            changed["_meta"]["worker-review"] = "changed"
            state["tool"] = changed
            with pytest.raises(ToolProtocolError, match="TOOL_DESCRIPTOR_CHANGED"):
                await protocol.call_reviewed(descriptor, {})
        assert state.get("calls", []) == []


@pytest.mark.parametrize(
    "listing,code",
    [
        ({"tools": [TOOL, TOOL]}, "TOOL_DESCRIPTOR_INVALID"),
        ({"tools": [], "nextCursor": "loop"}, "TOOL_DISCOVERY_LIMIT"),
        (
            {"tools": [{**TOOL, "name": f"t{index}"} for index in range(201)]},
            "TOOL_DISCOVERY_LIMIT",
        ),
    ],
    ids=["duplicate", "cursor-cycle", "tool-limit"],
)
async def test_bounded_discovery(listing, code):
    async with (
        server(reviewed_server({"list": listing})) as (port, _),
        open_tool_protocol(
            ToolConnection(f"http://127.0.0.1:{port}/mcp", ()), check_current=current
        ) as protocol,
    ):
        with pytest.raises(ToolProtocolError, match=code):
            await protocol.descriptors()


async def test_nontext_result_refused_after_one_actual_call():
    state = {"result": {"content": [{"type": "image", "data": "AA==", "mimeType": "image/png"}]}}
    async with server(reviewed_server(state)) as (port, _):
        async with open_tool_protocol(
            ToolConnection(f"http://127.0.0.1:{port}/mcp", ()), check_current=current
        ) as protocol:
            descriptor = (await protocol.descriptors())[0]
            with pytest.raises(ToolProtocolError, match="TOOL_OUTPUT_UNSUPPORTED"):
                await protocol.call_reviewed(descriptor, {})
        assert len(state["calls"]) == 1


async def test_revocation_after_tool_reply_preserves_execution_uncertainty():
    state = {}
    respond = reviewed_server(state)

    def effect(request):
        result = respond(request)
        if state.get("calls"):
            state["applied"] = True
        return result

    async def current():
        if state.get("applied"):
            raise PermissionError("Revoked after effect")

    async with server(effect) as (port, _):
        async with open_tool_protocol(
            ToolConnection(f"http://127.0.0.1:{port}/mcp", ()), check_current=current
        ) as protocol:
            descriptor = (await protocol.descriptors())[0]
            with pytest.raises(ToolProtocolError, match="TOOL_EXECUTION_UNCONFIRMED"):
                await protocol.call_reviewed(descriptor, {})
        assert len(state["calls"]) == 1


@pytest.mark.parametrize("cancel_outer", [False, True], ids=["call-task", "entire-sdk-scope"])
async def test_official_sdk_cancellation_preserves_task_and_one_operation(cancel_outer):
    state = {}
    accepted, release = asyncio.Event(), asyncio.Event()
    respond = reviewed_server(state)

    async def delayed(request):
        result = respond(request)
        if request["body"] and json.loads(request["body"]).get("method") == "tools/call":
            accepted.set()
            await release.wait()
        return result

    async with server(delayed) as (port, _):

        async def exercise():
            async with open_tool_protocol(
                ToolConnection(f"http://127.0.0.1:{port}/mcp", ()), check_current=current
            ) as protocol:
                descriptor = (await protocol.descriptors())[0]
                task = asyncio.create_task(
                    protocol.call_reviewed(descriptor, {"projection": "original"})
                )
                if cancel_outer:
                    await task
                else:
                    await asyncio.wait_for(accepted.wait(), 2)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                    assert task.cancelled()

        outer = asyncio.create_task(exercise())
        try:
            if cancel_outer:
                await asyncio.wait_for(accepted.wait(), 2)
                outer.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await outer
                assert outer.cancelled()
            else:
                await outer
            assert state["calls"] == [
                {"name": "sudograph_context_read", "arguments": {"projection": "original"}}
            ]
        finally:
            release.set()


@pytest.mark.parametrize(
    "value",
    [
        [],
        {"constructor": {}},
        {"x": float("inf")},
        {"x": "x" * 8193},
        {"x": [1] * 101},
        {"x": object()},
    ],
    ids=["root-list", "prototype-key", "nonfinite", "string-bound", "array-bound", "object"],
)
def test_argument_profile_refusals(value):
    with pytest.raises(ToolProtocolError, match="INVALID_TOOL_ARGUMENTS"):
        snapshot_arguments(value)


def test_closed_schema_and_utf16_scalar_bounds():
    schema = {
        "name": "tool",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "number": {"type": "integer"},
                "text": {"type": "string", "maxLength": 1},
            },
        },
    }
    validate_arguments(schema, {"number": 1, "text": "字"})
    for args in ({"number": True}, {"number": 1.5}, {"text": "😀"}, {"extra": "value"}):
        with pytest.raises(ToolProtocolError, match="INVALID_TOOL_ARGUMENTS"):
            validate_arguments(schema, args)


def test_malformed_metadata_and_out_of_range_numbers_refuse_cleanly():
    with pytest.raises(ToolProtocolError, match="INVALID_TOOL_ARGUMENTS"):
        snapshot_arguments({"number": 10**400})
    with pytest.raises(ToolProtocolError, match="TOOL_SCHEMA_UNSUPPORTED"):
        validate_arguments({"inputSchema": {}, "_meta": None}, {})


def test_descriptor_digest_matches_original_javascript_stable_json():
    # Independent original stableJson algorithm on the existing Node runtime.
    node = shutil.which("node")
    assert node, "Node is a required repository tool"
    vectors = [
        TOOL,
        {"number": 1e-7, "zero": -0.0, "exponent": 1e21, "fraction": 1.5},
        {"😀": "名字", "\ue000": [1, False, None]},
    ]
    script = "const fs=require('fs'),crypto=require('crypto');function stable(v){if(Array.isArray(v))return '['+v.map(stable).join(',')+']';if(v&&typeof v==='object')return '{'+Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+stable(v[k])).join(',')+'}';return JSON.stringify(v)};process.stdout.write(JSON.stringify(JSON.parse(fs.readFileSync(0,'utf8')).map(v=>'sha256:'+crypto.createHash('sha256').update(stable(v)).digest('hex'))))"
    result = subprocess.run(
        [node, "-e", script],
        input=json.dumps(vectors, ensure_ascii=False).encode(),
        capture_output=True,
        check=True,
    )
    assert [content_digest(v) for v in vectors] == json.loads(result.stdout)


def _review_read_tool():
    return {
        "name": "sudograph_context_read",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "id": {"type": "string"},
                "records": {"type": "boolean", "enum": [True]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1},
            },
            "required": ["id"],
        },
        "_meta": {"moss/host-review-profile": "sudograph-worker-review/v2"},
    }


def test_nonpage_host_read_checks_schema_without_fabricating_required_arguments():
    tool = _review_read_tool()
    before = copy.deepcopy(tool)
    assert validate_host_read_arguments(tool) is None
    assert tool == before
    with pytest.raises(ToolProtocolError, match="INVALID_TOOL_ARGUMENTS"):
        validate_arguments(tool, {})
    with pytest.raises(ToolProtocolError, match="INVALID_TOOL_ARGUMENTS"):
        validate_host_read_arguments(tool, {})


@pytest.mark.parametrize(
    "params",
    [
        {"id": "object", "records": False, "limit": 1},
        {"id": "object", "records": True, "limit": 2},
        {"id": "object", "records": True, "limit": True},
        {"id": "object", "records": True, "limit": 1, "unreviewed": "source"},
    ],
)
def test_page_host_read_retains_exact_reviewed_record_and_scalar_bounds(params):
    with pytest.raises(ToolProtocolError, match="INVALID_TOOL_ARGUMENTS"):
        validate_host_read_arguments(_review_read_tool(), params)


def test_page_host_read_accepts_only_original_approved_params_without_mutation():
    tool = _review_read_tool()
    params = {"id": "object", "records": True, "limit": 1}
    before = copy.deepcopy((tool, params))
    assert validate_host_read_arguments(tool, params) is None
    assert (tool, params) == before
    tool["inputSchema"]["properties"]["records"]["enum"] = [False]
    with pytest.raises(ToolProtocolError, match="INVALID_TOOL_ARGUMENTS"):
        validate_host_read_arguments(tool, params)


@pytest.mark.parametrize(
    "patch",
    [
        {"additionalProperties": True},
        {"properties": None},
        {"required": ["unreviewed"]},
        {"required": "id"},
        {"oneOf": []},
        {"properties": {"id": {"type": "object"}}},
        {"properties": {"id": {"type": "string", "enum": []}}},
        {"properties": {"id": {"type": "string", "maxLength": float("inf")}}},
    ],
)
def test_nonpage_host_read_refuses_unsupported_review_schema(patch):
    tool = _review_read_tool()
    tool["inputSchema"].update(patch)
    for check in (
        lambda: validate_host_read_arguments(tool),
        lambda: validate_arguments(tool, {"id": "object"}),
    ):
        with pytest.raises(ToolProtocolError, match="TOOL_SCHEMA_UNSUPPORTED"):
            check()


def test_host_read_bounded_record_profile_uses_the_original_value_validation():
    tool = {
        "name": "tool",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"record": {"type": "object", "additionalProperties": {}}},
            "required": ["record"],
        },
        "_meta": {"moss/input-profile": "bounded-record-v1"},
    }
    assert validate_host_read_arguments(tool) is None
    assert validate_host_read_arguments(tool, {"record": {"field": "value"}}) is None
    with pytest.raises(ToolProtocolError, match="INVALID_TOOL_ARGUMENTS"):
        validate_host_read_arguments(tool, {"record": {"constructor": "value"}})


@pytest.mark.parametrize("metadata", [None, {"moss/input-profile": "unreviewed-profile"}])
def test_host_read_malformed_or_unsupported_profile_refuses(metadata):
    tool = _review_read_tool()
    tool["_meta"] = metadata
    with pytest.raises(ToolProtocolError):
        validate_host_read_arguments(tool)


async def test_schema_only_host_read_does_not_allow_a_tool_invocation_with_none():
    state = {"tool": _review_read_tool()}
    async with server(reviewed_server(state)) as (port, _):
        connection = ToolConnection(f"http://127.0.0.1:{port}/mcp", ())
        async with open_tool_protocol(connection, check_current=current) as protocol:
            descriptor = (await protocol.descriptors())[0]
            assert validate_host_read_arguments(descriptor.tool) is None
            with pytest.raises(ToolProtocolError, match="INVALID_TOOL_ARGUMENTS"):
                await protocol.call_reviewed(descriptor, None)
            assert protocol.call_started is False
            assert state.get("calls", []) == []
