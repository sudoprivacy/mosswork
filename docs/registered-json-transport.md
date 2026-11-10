# Registered JSON connections

`octop.infra.connectors.fixed_json.FixedJsonTransport` supplies the bounded HTTP
connection for a host-resolved MCP registration or a human review read. It uses
the public HTTPX transport and HTTPCore network-backend interfaces. Pass it to
the official MCP client's HTTP client; it does not implement a second JSON-RPC
client.

The host supplies the exact canonical endpoint, optional deployment-owned IP
pins, and optional private CA file. HTTP requires a literal IP. HTTPS retains
the original hostname for certificate verification after resolving and checking
every DNS result. The socket receives only the selected numeric address.
Mapped, translated, metadata, link-local and multicast addresses remain refused
even with a pin. Unpinned DNS results must be public addresses. Literal private
addresses remain available for an explicitly registered private worker.

Each request opens a separate connection with retries, redirects, environment
proxies, connection reuse and compression disabled. MCP GET is answered locally
with 405; SSE reconnects cannot open another network stream. MCP DELETE is
transport termination only. It proves neither remote cancellation nor joined
native cleanup. Responses expose only the JSON content type and, for MCP, the
validated session header. Cookies and other upstream headers are not forwarded.

Before constructing the HTTP client, call `validate_auth_headers` on the
resolved registration credentials. This forbids credentials from overriding
framing or MCP control headers. Generated HTTPX framing headers are independently
checked at dispatch. The request bound is 2 MiB. The response bounds remain
2 MiB for MCP, 32 MiB for review JSON, and 64 KiB for review errors. Caller socket
timeouts shorter than 30 seconds are retained, with a 120-second outer bound
and a separate 10-second DNS bound. Cancellation propagates and closes the
owned connection. A redirect, connection failure, or timeout after dispatch is
unconfirmed execution; the adapter does not retry it.

The connection latches any attempted HTTP write. A later authorization refusal,
including one after the complete body or accepted tool reply, retains an
unconfirmed outcome. It cannot imply that the original operation had no effect.
Actual task cancellation remains cancellation, with the original operation
identity retained for authoritative recovery.

This is a network primitive, not a grant or a configured connector service.
Production composition must retain the original registration, current actor and
organization membership, reviewed descriptor and arguments, policy/visibility,
full model/action grants, secret resolver, effect admission and durable audit.
`RegisteredModelTransport` and the existing `RegisteredSudographPorts` must
consume that reviewed path. A working socket or an LLM billing/key fact cannot
supply those permissions. A review read is not comment admission or durable
original-session delivery.

`open_tool_protocol(ToolConnection(...), check_current=...)` supplies the official
MCP session and the original bounded tool protocol. The mandatory current
assertion must complete with `None` or raise; Boolean/JSON verdicts refuse. It
runs around discovery/calls and after DNS/TLS waits before HTTP writes.
`call_reviewed` snapshots arguments before awaiting, checks the current live
descriptor against the host-supplied reviewed descriptor, and enforces the
closed scalar or explicit `bounded-record-v1` schema profile. Discovery is
limited to 10 pages/200 unique tools; output is limited to 128 text parts and
128000 bytes. Remote annotations or an independently calculated descriptor
digest do not approve a tool. The host must obtain the review from the existing
authoritative review store and retain all independent full-model grants.

Descriptor hashes retain the original `sha256:` plus sorted-key JavaScript JSON
contract. RFC 8785 serialization preserves ECMAScript number/string ordering;
regressions independently compare the original Node algorithm, including
floating-point and UTF-16 key-order counterexamples. Invalid/noncanonical
numbers refuse instead of selecting another digest. MCP transport termination uses
the original MCP session identifier with a one-second bound, and is not an
execution cancellation or a native completion acknowledgment.

This client has no dependency on a particular native engine. The selected
runtime factory supplies its actual current actor/resource/tool admission
assertion; a fixture assertion is not production authority.

The network rules are ported from the MIT-licensed
`sudoprivacy/moss` commit `d0b84d4c8fd5905d9ec3f7c160f2ce633f535d0d`,
`src/server/agentFederation/provider/routerTransport.ts`, original SHA-256
`0957775a6ed75a8851a37b69363300637a5261db1922f4fbd6e396089cee8a81`.
The maintained implementation is in Mosswork; the archived host is not launched
or modified. Real loopback HTTP/TLS and official MCP SDK regression checks live
in `tests/unit/connectors/test_fixed_json.py`. Those checks use synthetic data
and ephemeral test certificates, without production credentials or grants.

The tool protocol is ported from the same original commit's
`src/server/agentFederation/toolProtocol.ts`, SHA-256
`9d222f17742dd450922cb29ee1a346c3a80da33796ba2659be45232d3352ba7d`.
