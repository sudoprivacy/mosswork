// Execute retained TS policy updates against isolated node:sqlite fixture rows.
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const repository = process.argv[2];
if (!repository) throw new Error('Pass the read-only retained Moss repository');
const commit = 'd0b84d4c8fd5905d9ec3f7c160f2ce633f535d0d';
const paths = {
  policy: 'src/server/configuration/clientPolicyRepository.ts',
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
const methods = ['update', 'prepareUpdate', 'assertAdmin', 'policyOrgId'].map(name =>
  extract(sources.service, new RegExp(`  (?:private )?(?:async )?${name}\\([\\s\\S]*?\\n  }(?=\\n|$)`)));
const helpers = ['splitPlatformInheritedValues', 'jsonEqual'].map(name =>
  extract(sources.service, new RegExp(`function ${name}\\([\\s\\S]*?\\n}`)));
const globalAccess = extract(sources.identity,
  /export function hasGlobalOrganizationAccess\([\s\S]*?\n}/);
const errorClass = extract(sources.service,
  /export class SudoworkSystemConfigError[\s\S]*?\n}/);
const program = `
import { DatabaseSync } from 'node:sqlite';
${sources.policy}
${errorClass}
${globalAccess}
${helpers.join('\n')}
const LOG_REPORT_SECRET_KEY = 'fixture-unused';
class OriginalLoginUpdate {
  constructor(options) { this.options = options; }
  ${methods.join('\n')}
  async isSmsConfigured() { return this.options.smsReady; }
  async hasEnabledThirdPartyAuth() { return this.options.casReady; }
  async platformInheritedValues() { return this.options.policies.getPlatform(); }
}
const at = 1800000000123;
Date.now = () => at;
const seed = { unrelated: { keep: [false, null, '原始'] }, loginMethod: 'cas' };
const cases = [];
for (const method of [0, 1, 2]) {
  cases.push({ name: 'set_' + method, body: { login_method: method }, organization: seed, platform: {} });
  cases.push({ name: 'platform_flag_' + method, body: { login_method: method }, organization: seed, platform: { loginMethodInherited: false } });
}
cases.push({ name: 'inherit', body: { inherit_login_method: true }, organization: seed, platform: { loginMethod: 1 } });
cases.push({ name: 'inherit_ignores_explicit_method', body: { inherit_login_method: true, login_method: 0 }, organization: seed, platform: {} });
cases.push({ name: 'missing_org', body: { login_method: 1 }, platform: {} });
cases.push({ name: 'malformed_org', body: { login_method: 1 }, organizationRaw: '{bad', platform: {} });
cases.push({ name: 'array_org', body: { inherit_login_method: true }, organization: [], platform: {} });
cases.push({ name: 'scoped_super_admin', role: 'super_admin', body: { login_method: 1 }, organization: seed, platform: { loginMethod: 2 } });
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
      try { const result = await fn(); if (outer) db.exec('COMMIT'); return result; }
      catch (error) { if (outer) db.exec('ROLLBACK'); throw error; }
      finally { depth--; }
    },
  };
  for (const [scope, id, value, raw] of [
    ['platform', 'default', item.platform, undefined],
    ['organization', 'fixture-org', item.organization, item.organizationRaw],
  ]) {
    if (value !== undefined || raw !== undefined) db.prepare('INSERT INTO client_delivery_policies VALUES (?,?,?,?,?,?)')
      .run(scope, id, raw ?? JSON.stringify(value), 'original-writer', 7, 8);
  }
  const service = new OriginalLoginUpdate({
    db: driver, policies: new ClientPolicyRepository(driver),
    secrets: { get: () => undefined }, smsReady: true, casReady: true,
  });
  await service.update({ userId: 'fixture-actor', orgId: 'fixture-org', role: item.role ?? 'admin', organizationScoped: true }, item.body);
  item.expected = db.prepare("SELECT * FROM client_delivery_policies WHERE scope_type='organization' AND scope_id='fixture-org'").get();
  item.expected.policy_json = JSON.parse(item.expected.policy_json);
  db.close();
}
console.log(JSON.stringify({ commit: ${JSON.stringify(commit)},
  sourceHashes: ${JSON.stringify(Object.fromEntries(Object.entries(sources).map(([key, text]) => [paths[key], createHash('sha256').update(text).digest('hex')])))},
  scope: 'Original update/prepareUpdate/repository/inheritance helpers; SMS/CAS readiness and platform defaults are explicit fixtures, not production providers.',
  nowMs: at, cases,
}, null, 2));
`;
const output = execFileSync(process.execPath,
  ['--experimental-transform-types', '--input-type=module-typescript', '-'],
  { input: program, encoding: 'utf8' });
writeFileSync(fileURLToPath(new URL('./organization_login_policy_legacy.json', import.meta.url)), output, 'utf8');
console.log(JSON.stringify({ cases: JSON.parse(output).cases.length,
  sha256: createHash('sha256').update(output).digest('hex') }));
