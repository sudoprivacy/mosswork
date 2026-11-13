/** Ephemeral typed SDK/mTLS fixture, without a native engine or real grants. */
import assert from 'node:assert/strict'
import { X509Certificate, randomUUID } from 'node:crypto'
import { writeFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { isAbsolute, join } from 'node:path'
import { createInterface } from 'node:readline'
import { pathToFileURL } from 'node:url'

const source = process.argv[2]
assert.ok(isAbsolute(source))
const require = createRequire(join(source, 'package.json'))
const { SessionCodec, userRuntimeServerName } = await import(pathToFileURL(require.resolve('@nexus-ai-fs/vfs-client')).href)
const { serve, testAuthority } = await import(pathToFileURL(join(source, 'test/helpers/tls.ts')).href)
const lines = createInterface({ input: process.stdin })
const iterator = lines[Symbol.asyncIterator]()
const boot = await iterator.next()
const { directory } = JSON.parse(boot.value)
assert.ok(isAbsolute(directory))
const closers = []
const scope = { after: close => { closers.push(close) } }
const { ca, issue } = testAuthority()
const operator = issue('operator', [{ type: 6, value: 'nexus://agent/moss' }])
const issuer = issue('issuer', [{ type: 2, value: 'issuer.test' }])
const runtime = issue('runtime', [{ type: 2, value: userRuntimeServerName('alice') }])
const subjectId = `session-${randomUUID()}`
const delegated = issue(subjectId, [{ type: 6, value: `nexus://agent/${subjectId}` },
  { type: 6, value: 'nexus://owner/alice' }])
let minted = 0
let calls = 0
let reads = 0
let fileReads = 0
let writes = 0
let inputs = 0
let lost = false
let context
const endpoint = { protocol: 'acp-mailbox/1', channel_id: 'original-channel',
  agent: 'original-agent', controller: subjectId, transcript: `/conversations/${'a'.repeat(32)}/transcript` }
const codec = new SessionCodec(endpoint, 'agent')
const records = []
const append = message => records.push(codec.encode({ type: 'rpc', message }))
const verify = call => {
  assert.equal(call.request.auth_token, '')
  assert.equal(call.request.path, endpoint.transcript)
  assert.deepEqual(call.getAuthContext().sslPeerCertificate?.raw, new X509Certificate(delegated.cert).raw)
}
const issuerEndpoint = await serve(scope, ca, issuer, 'nexus.raft.ZoneApiService', {
  MintSessionAgent(call, reply) {
    assert.equal(call.getAuthContext().sslPeerCertificate?.subject.CN, 'operator')
    assert.equal(call.request.owner_id, 'alice')
    minted++
    reply(null, { success: true, subject_id: subjectId, ca_pem: ca,
      agent_cert_pem: delegated.cert, agent_key_pem: delegated.key })
  },
})
const runtimeEndpoint = await serve(scope, ca, runtime, 'nexus.grpc.vfs.NexusVFSService', {
  Read(call, reply) {
    assert.equal(call.request.auth_token, '')
    assert.deepEqual(call.getAuthContext().sslPeerCertificate?.raw, new X509Certificate(delegated.cert).raw)
    fileReads++
    if (call.request.path === '/original/unavailable') {
      reply({ code: 14, details: 'fixture read unavailable' }, null)
      return
    }
    assert.ok(['/original/transcript', '/original/empty'].includes(call.request.path))
    reply(null, { content: call.request.path === '/original/empty'
      ? Buffer.alloc(0) : Buffer.from('original transcript 客户 📝\n') })
  },
  Call(call, reply) {
    calls++
    assert.equal(call.request.auth_token, '')
    assert.equal(call.request.method, 'managed_agent.get_session_v1')
    reply(null, { payload: Buffer.from(JSON.stringify({ session_id: '19.3', session_endpoint: endpoint })) })
  },
  StreamReadAt(call, reply) {
    verify(call)
    reads++
    const data = records[Number(call.request.offset)]
    reply(null, { data: data ?? Buffer.alloc(0),
      next_offset: data ? String(Number(call.request.offset) + 1) : call.request.offset, timed_out: !data })
  },
  StreamWriteNowait(call, reply) {
    verify(call)
    writes++
    records.push(call.request.data)
    const decoded = codec.decode(call.request.data)
    if (decoded?.type !== 'rpc') { reply(null, {}); return }
    const request = decoded.message
    if (request.method === 'initialize') append({ jsonrpc: '2.0', id: request.id, result: {
      protocolVersion: 1, agentCapabilities: { loadSession: true },
      _meta: { sudocode: { hostContext: { version: 1 }, inputDelivery: { version: 1 } } },
    } })
    else if (request.method === 'session/load') append({ jsonrpc: '2.0', id: request.id,
      ...(request.params.sessionId === 'history-original' ? { result: {} } : {
        error: { code: -32002, message: 'Original missing', data: { sessionId: request.params.sessionId } },
      }) })
    else if (request.method === 'session/prompt') {
      inputs++
      if (lost) {
        reply({ code: 14, details: 'fixture append committed, reply lost' }, null)
        return
      }
      append({ jsonrpc: '2.0', id: 'context-original', method: '_sudocode/session/host_context',
        params: { sessionId: 'history-original' } })
      context = request.id
    } else if (request.id === 'context-original' && request.result) {
      assert.equal(request.result.appendSystemPrompt, '当前原始上下文 🧩')
      append({ jsonrpc: '2.0', method: '_sudocode/session/input_update', params: {
        sessionId: 'history-original', update: { clientId: 'original-input', kind: 'committed' },
      } })
      append({ jsonrpc: '2.0', id: context, result: { stopReason: 'end_turn' } })
    }
    reply(null, {})
  },
})
const paths = { ca: join(directory, 'ca.pem'), cert: join(directory, 'operator.pem'), key: join(directory, 'operator-key.pem') }
await Promise.all([[paths.ca, ca], [paths.cert, operator.cert], [paths.key, operator.key]].map(
  async ([path, bytes]) => writeFile(path, bytes, { mode: 0o600 })))
const emit = value => process.stdout.write(JSON.stringify(value) + '\n')
emit({ type: 'fixture-ready', issuerEndpoint, runtimeEndpoint, ...paths, endpoint })
try {
  for await (const line of iterator) {
    const request = JSON.parse(line)
    if (request.operation === 'lose-reply') lost = true
    emit({ minted, calls, reads, fileReads, writes, inputs, lost })
  }
} finally {
  for (const close of closers.reverse()) close()
  lines.close()
}
