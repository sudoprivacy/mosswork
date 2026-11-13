// Execute pinned original platform scope/update code against isolated SQLite.
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const repository = process.argv[2];
if (!repository) throw new Error('Pass the read-only retained Moss repository');
const commit = 'd0b84d4c8fd5905d9ec3f7c160f2ce633f535d0d';
const paths = {
  policy: 'src/server/configuration/clientPolicyRepository.ts',
  scope: 'src/server/configuration/adminScope.ts',
  service: 'src/server/api/compat/sudowork/systemConfigService.ts',
  identity: 'src/server/identity/organizationIdentityService.ts',
};
const sources = Object.fromEntries(Object.entries(paths).map(([key, path]) =>
  [key, execFileSync('git', ['show', `${commit}:${path}`], { cwd: repository, encoding: 'utf8' })]));
function extract(source, pattern) {
  const match = source.match(pattern);
  if (!match) throw new Error(`Original source did not match ${pattern}`);
  return match[0];
}
const methods = ['update', 'prepareUpdate', 'assertAdmin', 'policyOrgId', 'policy',
  'isSmsConfigured', 'hasEnabledThirdPartyAuth', 'enabledCasProviderIds'].map(name =>
  extract(sources.service, new RegExp(`  (?:private )?(?:async )?${name}\\([\\s\\S]*?\\n  }(?=\\n|$)`)));
const helpers = ['object', 'string', 'flag', 'splitPlatformInheritedValues', 'jsonEqual'].map(name =>
  extract(sources.service, new RegExp(`function ${name}\\([\\s\\S]*?\\n}`)));
const globalAccess = extract(sources.identity,
  /export function hasGlobalOrganizationAccess\([\s\S]*?\n}/);
const errorClass = extract(sources.service,
  /export class SudoworkSystemConfigError[\s\S]*?\n}/);
const program = `
import { DatabaseSync } from 'node:sqlite';
${sources.policy}
${sources.scope}
${errorClass}
${globalAccess}
${helpers.join('\n')}
const LOG_REPORT_SECRET_KEY = 'fixture-unused';
class OriginalPlatformUpdate {
  constructor(options) { this.options = options; }
  ${methods.join('\n')}
}
const at = 1800000000123;
Date.now = () => at;
const seed = { unrelated: { keep: [false, null, '原始'] }, loginMethod: 'cas', loginMethodInherited: false,
  thirdPartyAuth: { enabled: 1, defaultProvider: 'foreign-default' } };
const cases = [];
for (const method of [0, 1, 2]) {
  cases.push({ name: 'set_' + method, body: { login_method: method }, platform: seed });
  cases.push({ name: 'missing_platform_' + method, body: { login_method: method } });
}
// CAS must be enabled in platform policy, unlike the explicit provider inventory fixture.
cases.find(item => item.name === 'missing_platform_2').expectedStatus = 400;
cases.push({ name: 'malformed_platform', body: { login_method: 1 }, platformRaw: '{bad' });
cases.push({ name: 'array_platform', body: { login_method: 1 }, platform: [] });
cases.push({ name: 'null_platform', body: { login_method: 1 }, platform: null });
cases.push({ name: 'no_provider', body: { login_method: 2 }, platform: seed, casEnabled: false, expectedStatus: 400 });
cases.push({ name: 'disabled_platform_cas', body: { login_method: 2 }, platform: { thirdPartyAuth: { enabled: 0 } }, expectedStatus: 400 });
cases.push({ name: 'sms_unavailable', body: { login_method: 0 }, platform: seed, smsReady: false, expectedStatus: 400 });
cases.push({ name: 'inherit_refused', body: { inherit_login_method: true }, platform: seed, expectedStatus: 400 });
for (const role of ['admin', 'dept_admin', 'user']) {
  cases.push({ name: 'deny_' + role, role, body: { login_method: 1 }, platform: seed, expectedStatus: 403 });
}
for (const item of cases) {
  const db = new DatabaseSync(':memory:');
  ensureClientPolicySchema(db);
  let depth = 0;
  const driver = {
    get: async (sql, params) => db.prepare(sql).get(...params),
    run: async (sql, params) => db.prepare(sql).run(...params),
    transaction: async (fn) => {
      const outer = depth++ === 0;
      if (outer) db.exec('BEGIN IMMEDIATE');
      try { const value = await fn(); if (outer) db.exec('COMMIT'); return value; }
      catch (error) { if (outer) db.exec('ROLLBACK'); throw error; }
      finally { depth--; }
    },
  };
  if (Object.hasOwn(item, 'platform') || item.platformRaw !== undefined) {
    db.prepare('INSERT INTO client_delivery_policies VALUES (?,?,?,?,?,?)')
      .run('platform', 'default', item.platformRaw ?? JSON.stringify(item.platform), 'original-writer', 7, 8);
  }
  db.prepare('INSERT INTO client_delivery_policies VALUES (?,?,?,?,?,?)')
    .run('organization', 'fixture-org', JSON.stringify({ loginMethod: 0, keep: 'original org' }), 'original-org-writer', 3, 4);
  const before = db.prepare('SELECT * FROM client_delivery_policies ORDER BY scope_type,scope_id').all();
  const service = new OriginalPlatformUpdate({
    db: driver, policies: new ClientPolicyRepository(driver), secrets: { get: () => undefined },
    getSmsReadiness: async () => ({ ready: item.smsReady ?? true }),
    identities: {
      listOrganizationProfiles: async () => [{ orgId: 'fixture-org' }, { orgId: 'other-org' }],
      listIntegrationConnections: async (orgId, type) => {
        if (type !== 'cas') throw new Error('Unexpected provider kind');
        return [{ id: 'fixture-' + orgId, enabled: item.casEnabled ?? true }];
      },
    },
  });
  let status = 200;
  try {
    const actor = resolveConfigurationActor({ userId: 'fixture-actor', orgId: 'fixture-org',
      role: item.role ?? 'super_admin', organizationScoped: true }, 'platform');
    if (actor.organizationScoped !== false) throw new Error('Explicit platform scope not established');
    await service.update(actor, item.body);
  } catch (error) {
    if (!Number.isInteger(error.statusCode)) throw error;
    status = error.statusCode;
  }
  if (status !== (item.expectedStatus ?? 200)) throw new Error('Unexpected original result: ' + item.name + ':' + status);
  item.status = status;
  item.unchangedOnRefusal = status === 200 ? null : JSON.stringify(before) === JSON.stringify(
    db.prepare('SELECT * FROM client_delivery_policies ORDER BY scope_type,scope_id').all());
  item.expected = db.prepare("SELECT * FROM client_delivery_policies WHERE scope_type='platform' AND scope_id='default'").get() ?? null;
  if (item.expected) item.expected.policy_json = JSON.parse(item.expected.policy_json);
  item.organization = db.prepare("SELECT * FROM client_delivery_policies WHERE scope_type='organization' AND scope_id='fixture-org'").get();
  db.close();
}
console.log(JSON.stringify({ commit: ${JSON.stringify(commit)},
  sourceHashes: ${JSON.stringify(Object.fromEntries(Object.entries(sources).map(([key, text]) => [paths[key], createHash('sha256').update(text).digest('hex')])))},
  scope: 'Original explicit scope, update, policy repository, actual SMS readiness and CAS platform/inventory checks. Provider inventory and SMS runtime readiness are fixtures; no production provider or new signing authority.',
  nowMs: at, cases,
}, null, 2));
`;
const output = execFileSync(process.execPath,
  ['--experimental-transform-types', '--input-type=module-typescript', '-'],
  { input: program, encoding: 'utf8' });
writeFileSync(fileURLToPath(new URL('./platform_login_policy_legacy.json', import.meta.url)), output, 'utf8');
console.log(JSON.stringify({ cases: JSON.parse(output).cases.length,
  sha256: createHash('sha256').update(output).digest('hex') }));
