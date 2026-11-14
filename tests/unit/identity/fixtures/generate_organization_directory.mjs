// Freeze original directory methods against SQLite, without a Python model.
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const repository = process.argv[2];
if (!repository) throw new Error('Pass the read-only retained Moss repository');
const commit = 'd0b84d4c8fd5905d9ec3f7c160f2ce633f535d0d';
const paths = ['src/server/auth/service.ts', 'src/server/authCenter/db.ts'];
const sources = paths.map(path => execFileSync('git', ['show', `${commit}:${path}`],
  { cwd: repository, encoding: 'utf8' }));
function method(source, name) {
  const match = source.match(new RegExp(`^  (?:private )?(?:async )?${name}\\([\\s\\S]*?^  }\\r?$`, 'm'));
  if (!match) throw new Error('Missing original method ' + name);
  return match[0].replace(/^  private /, '  ');
}
function fn(source, name) {
  const match = source.match(new RegExp(`^function ${name}\\([\\s\\S]*?^}`, 'm'));
  if (!match) throw new Error('Missing original mapper ' + name);
  return match[0];
}
const schemas = ['organizations', 'departments', 'users'].map(table => {
  const match = sources[1].match(new RegExp(`CREATE TABLE IF NOT EXISTS ${table} \\([\\s\\S]*?\\n\\s*\\);`));
  if (!match) throw new Error('Missing original DDL ' + table);
  return match[0];
}).join('\n');
const ids = JSON.parse(readFileSync(new URL('./human_tokens_legacy.json', import.meta.url), 'utf8')).ids;
const sourceHashes = Object.fromEntries(paths.map((path, index) =>
  [path, createHash('sha256').update(sources[index]).digest('hex')]));
const code = `
import { DatabaseSync } from 'node:sqlite';
const ADMIN_ROLES = new Set(['admin', 'super_admin']);
${fn(sources[1], 'mapUser')}
${fn(sources[1], 'mapDepartment')}
class OriginalDb {
  constructor(public driver) {}
${['getUserByIdAndOrg', 'listUsersByOrg', 'listDepartmentsByOrg'].map(name => method(sources[1], name)).join('\n')}
}
class OriginalDirectory {
  constructor(public db) {}
  async requireAuthUser(auth) {
    const actor = await this.db.getUserByIdAndOrg(auth.userId, auth.orgId);
    if (!actor || actor.status !== 'active') throw new Error('Current actor unavailable');
    return actor;
  }
${['listDepartments', 'listVisibleUsers', 'getVisibleDepartmentIds'].map(name => method(sources[0], name)).join('\n')}
}
const ids = ${JSON.stringify(ids)};
const org = ids.homeOrg, foreign = ids.companyOrg, actor = ids.homeMember;
const cases = [];
for (const scenario of ['admin', 'super_admin', 'dept_admin', 'no_department', 'cycle', 'foreign_root']) {
  const db = new DatabaseSync(':memory:');
  db.exec(${JSON.stringify(schemas)});
  const driver = {
    async all(sql, params=[]) { return db.prepare(sql).all(...params); },
    async get(sql, params=[]) { return db.prepare(sql).get(...params); },
  };
  db.prepare('INSERT INTO organizations VALUES (?,?,1)').run(org, 'Original');
  db.prepare('INSERT INTO organizations VALUES (?,?,1)').run(foreign, 'Foreign');
  const departments = [
    {id:'root', org_id:org, parent_id:null, name:'Root', created_at:1, updated_at:1},
    {id:'child', org_id:org, parent_id:'root', name:'Child', created_at:2, updated_at:2},
    {id:'grand', org_id:org, parent_id:'child', name:'Grand', created_at:3, updated_at:3},
    {id:'sibling', org_id:org, parent_id:'root', name:'Sibling', created_at:4, updated_at:4},
    {id:'foreign', org_id:foreign, parent_id:null, name:'Foreign', created_at:5, updated_at:5},
  ];
  if(scenario === 'cycle') departments[1].parent_id = 'grand';
  for (const row of departments) db.prepare('INSERT INTO departments VALUES (?,?,NULL,?,?,?)')
    .run(row.id,row.org_id,row.name,row.created_at,row.updated_at);
  for (const row of departments) db.prepare('UPDATE departments SET parent_id=? WHERE id=?')
    .run(row.parent_id,row.id);
  const role = ['admin','super_admin'].includes(scenario) ? scenario : 'dept_admin';
  const root = scenario === 'no_department' ? null : scenario === 'foreign_root' ? 'foreign' : 'child';
  const users = [
    {id:actor, org_id:org, name:'home', role, status:'active', department_id:root, created_at:1},
    {id:'directory-user', org_id:org, name:'user', role:'user', status:'active', department_id:'child', created_at:2},
    {id:'directory-subadmin', org_id:org, name:'subadmin', role:'dept_admin', status:'active', department_id:'grand', created_at:3},
    {id:'directory-orgadmin', org_id:org, name:'orgadmin', role:'admin', status:'active', department_id:'child', created_at:4},
    {id:'directory-super', org_id:org, name:'super', role:'super_admin', status:'active', department_id:'grand', created_at:5},
    {id:'directory-sibling', org_id:org, name:'sibling', role:'user', status:'active', department_id:'sibling', created_at:6},
    {id:'directory-unassigned', org_id:org, name:'none', role:'user', status:'active', department_id:null, created_at:7},
    {id:'directory-disabled', org_id:org, name:'disabled', role:'user', status:'disabled', department_id:'grand', created_at:8},
    {id:'directory-foreign', org_id:foreign, name:'foreign', role:'user', status:'active', department_id:'foreign', created_at:9},
  ];
  for (const row of users) db.prepare('INSERT INTO users(id,org_id,email,name,role,status,department_id,created_at) VALUES(?,?,?,?,?,?,?,?)')
    .run(row.id,row.org_id,row.id+'@example.test',row.name,row.role,row.status,row.department_id,row.created_at);
  const original = new OriginalDirectory(new OriginalDb(driver));
  const auth = { userId:actor, orgId:org };
  const visibleMembers = await original.listVisibleUsers(org, auth);
  const result = await original.listDepartments(org, auth);
  cases.push({name:scenario, departments, users,
    expectedMembers:visibleMembers.map(row=>row.id),
    expectedDepartments:result.departments.map(row=>({department_id:row.id, organization_id:row.orgId,
      parent_id:row.parentId,name:row.name,created_at:row.createdAt,updated_at:row.updatedAt,user_count:row.userCount}))});
  db.close();
}
console.log(JSON.stringify({sourceCommit:${JSON.stringify(commit)},sourceHashes:${JSON.stringify(sourceHashes)},
  producer:'Exact original listDepartments/listVisibleUsers/getVisibleDepartmentIds and AuthCenterDb SQL against node:sqlite; same-org authenticated actor fixture only',cases},null,2));
`;
const output = execFileSync(process.execPath, ['--experimental-transform-types', '--input-type=module-typescript', '-'],
  { input: code, encoding: 'utf8', maxBuffer: 4 * 1024 * 1024 });
writeFileSync(fileURLToPath(new URL('./organization_directory_legacy.json', import.meta.url)), output, 'utf8');
console.log(JSON.stringify({cases:JSON.parse(output).cases.length,sha256:createHash('sha256').update(output).digest('hex')}));
