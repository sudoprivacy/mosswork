import assert from 'node:assert/strict'
import { test } from 'node:test'
import { execFile } from 'node:child_process'
import { appendFile, copyFile, cp, mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { promisify } from 'node:util'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { createCompilerContextWorker } from '../dist/compilerContextWorker.js'
import { compilerWorkerConfiguration } from '../dist/compilerWorkerConfig.js'

const token = 'fixture-worker-token-for-acceptance-only'
const config = {
  token,
  binding: { authorityRef: 'authority', modelRef: 'model', sourceRefs: ['source'] },
  actor: 'worker',
  runtime: { python: '/deliberately-unavailable/private-python', compilerRoot: '/private-model-root', modelDirectory: '/private-model-data' },
}

test('missing and weak deployment credentials refuse before binding a listener', () => {
  for (const token of ['', 'short', 'x'.repeat(513), 'x'.repeat(32) + '\n']) {
    assert.throws(() => createCompilerContextWorker({ ...config, token }), /deployment-owned worker token/)
  }
})

test('installed worker refuses foreign HTTP/review inputs and preserves exact MCP grammar', async t => {
  const server = createCompilerContextWorker(config)
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  t.after(async () => {
    server.closeAllConnections()
    await new Promise(resolve => server.close(resolve))
  })
  const endpoint = new URL(`http://127.0.0.1:${server.address().port}/mcp`)
  const headers = { authorization: `Bearer ${token}`, 'content-type': 'application/json' }
  assert.equal((await fetch(endpoint, { method: 'POST', body: '{}' })).status, 401)
  assert.equal((await fetch(endpoint, { method: 'POST', headers: { authorization: 'Bearer wrong' }, body: '{}' })).status, 401)
  assert.equal((await fetch(endpoint, { method: 'POST', headers: { ...headers, origin: 'https://foreign.invalid' }, body: '{}' })).status, 403)
  assert.equal((await fetch(new URL('/mcp/foreign-model', endpoint), { method: 'POST', headers, body: '{}' })).status, 403)
  const review = new URL('/review', endpoint)
  for (const request of [
    { operation: 'head', model_ref: 'foreign' }, { operation: 'head', actor: 'foreign' },
    { operation: 'head', path: '/private-other' }, { operation: 'apply', model: 'foreign' },
    { operation: 'export' }, { operation: 'html', endpoint: 'https://foreign.invalid/steal' },
  ]) {
    assert.equal((await fetch(review, { method: 'POST', headers, body: JSON.stringify(request) })).status, 400)
  }
  assert.equal((await fetch(review, { method: 'POST', headers, body: ' '.repeat(65536) + '{"operation":"head"}' })).status, 400)
  const refused = await fetch(review, { method: 'POST', headers, body: '{"operation":"head"}' })
  assert.equal(refused.status, 503)
  const diagnostic = await refused.text()
  assert(diagnostic.includes('VIEW_UNAVAILABLE'))
  assert(!diagnostic.includes('/private-') && !diagnostic.includes(token))

  const client = new Client({ name: 'worker-acceptance', version: '1.0.0' })
  t.after(() => client.close())
  await client.connect(new StreamableHTTPClientTransport(endpoint, { requestInit: { headers } }))
  const { tools } = await client.listTools()
  assert.deepEqual(tools.map(tool => tool.name).sort(), ['sudograph_context', 'sudograph_context_read', 'sudograph_model_apply', 'sudograph_model_contract'])
  const reader = tools.find(tool => tool.name === 'sudograph_context_read')
  assert.equal(reader._meta['moss/host-review-profile'], 'sudograph-worker-review/v2')
  const pointer = { revision: 'revision', context_digest: 'digest', id: 'catalog' }
  for (const args of [
    { ...pointer, path: '/foreign-model' }, { ...pointer, model_ref: 'foreign' },
    { ...pointer, actor: 'foreign' }, { ...pointer, limit: 101 },
    { ...pointer, offset: -1 }, { ...pointer, records: 'true' }, { ...pointer, revision: '' },
  ]) {
    assert.equal((await client.callTool({ name: reader.name, arguments: args })).isError, true)
  }
  for (const [name, args] of [
    ['sudograph_context', {}], ['sudograph_context_read', pointer],
    ['sudograph_model_contract', {}],
    ['sudograph_model_apply', { base_revision: 'revision', operation_id: 'operation', model: '' }],
  ]) {
    const result = await client.callTool({ name, arguments: args })
    assert.equal(result.isError, true)
    assert(!JSON.stringify(result).includes('/private-') && !JSON.stringify(result).includes(token))
  }
})

test('source configuration uses injected secrets and retains exact provider scope', () => {
  const deployment = {
    binding: config.binding, actor: config.actor, runtime: {
      ...config.runtime, source: { dsn: 'registered://source', connector: {
        sourceType: 'postgresql', host: 'fixture.invalid', database: 'fixture', username: 'fixture', params: { schema: 'visible' },
      } },
    }, port: 0,
  }
  assert.throws(() => compilerWorkerConfiguration(deployment, {}), /injected SUDOGRAPH_SOURCE_PASSWORD/)
  const parsed = compilerWorkerConfiguration(deployment, { SUDOGRAPH_SOURCE_PASSWORD: 'fixture-only' })
  assert.equal(parsed.runtime.source.connector.password, 'fixture-only')
  assert.equal(deployment.runtime.source.connector.password, undefined)
  assert.throws(() => compilerWorkerConfiguration({ ...deployment, runtime: {
    ...deployment.runtime, source: { ...deployment.runtime.source, connector: {
      ...deployment.runtime.source.connector, password: 'in-config',
    } },
  } }, { SUDOGRAPH_SOURCE_PASSWORD: 'fixture-only' }))
  assert.throws(() => compilerWorkerConfiguration({ ...deployment, runtime: {
    ...deployment.runtime, source: { ...deployment.runtime.source, connector: {
      ...deployment.runtime.source.connector, sourceType: 'mysql',
    } },
  } }, { SUDOGRAPH_SOURCE_PASSWORD: 'fixture-only' }), /MySQL scope is connector.database/)
})

test('artifact verifier rejects altered installed worker bytes', async t => {
  const root = fileURLToPath(new URL('..', import.meta.url))
  const temporary = await mkdtemp(join(tmpdir(), 'sudograph-worker-artifact-'))
  t.after(async () => {
    assert.equal(dirname(resolve(temporary)), resolve(tmpdir()))
    assert(temporary.startsWith(join(tmpdir(), 'sudograph-worker-artifact-')))
    await rm(temporary, { recursive: true, force: true })
  })
  await cp(join(root, 'dist'), join(temporary, 'dist'), { recursive: true })
  for (const name of ['package.json', 'npm-shrinkwrap.json', 'SOURCE.json', 'LICENSE']) {
    await copyFile(join(root, name), join(temporary, name))
  }
  const execute = promisify(execFile)
  const args = [join(root, 'tools/artifact.mjs'), '--verify', temporary]
  await execute(process.execPath, args)
  await appendFile(join(temporary, 'dist/compilerContextWorker.js'), '\n// altered fixture bytes\n')
  await assert.rejects(execute(process.execPath, args), error => {
    assert(error.stderr.includes('Installed bytes differ: dist/compilerContextWorker.js'))
    return true
  })
})
