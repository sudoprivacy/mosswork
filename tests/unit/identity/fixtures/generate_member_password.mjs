// Execute the original admin password method, scope helpers and SQLite update.
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';

const repository = process.argv[2];
if (!repository) throw new Error('Pass the retained Moss source repository');
const commit = 'd0b84d4c8fd5905d9ec3f7c160f2ce633f535d0d';
const paths = ['src/server/auth/service.ts', 'src/server/authCenter/db.ts'];
const sources = paths.map(path => execFileSync('git', ['show', `${commit}:${path}`],
  { cwd: repository, encoding: 'utf8', env: {...process.env, GIT_NO_LAZY_FETCH:'1'} }));
function method(source, name) {
  const match = source.match(new RegExp(`^  (?:private )?(?:async )?${name}\\([\\s\\S]*?^  }\\r?$`, 'm'));
  if (!match) throw new Error('Missing original method ' + name);
  return match[0].replace(/^  private /, '  ');
}
function fn(source, name) {
  const match = source.match(new RegExp(`^(?:export )?function ${name}\\([\\s\\S]*?^}`, 'm'));
  if (!match) throw new Error('Missing original function ' + name);
  return match[0].replace(/^export /, '');
}
const schemas = ['organizations', 'departments', 'users'].map(table => {
  const match = sources[1].match(new RegExp(`CREATE TABLE IF NOT EXISTS ${table} \\([\\s\\S]*?\\n\\s*\\);`));
  if (!match) throw new Error('Missing original DDL ' + table);
  return match[0];
}).join('\n');
const extension = ['local_execution_allowed INTEGER NOT NULL DEFAULT 1', 'local_auth INTEGER NOT NULL DEFAULT 0', 'token_limit INTEGER'].map(column => {
  const sql = 'ALTER TABLE users ADD COLUMN ' + column;
  if (!sources[1].includes(sql)) throw new Error('Missing original column addition');
  return sql + ';';
}).join('\n');
const seed = JSON.parse(readFileSync(new URL('./organization_directory_legacy.json', import.meta.url), 'utf8'));
const human = JSON.parse(readFileSync(new URL('./human_tokens_legacy.json', import.meta.url), 'utf8'));
const inputs = [
  ['admin_user','admin','directory-user','x'],
  ['admin_super','admin','directory-super','PublicNew123'],
  ['super_admin','super_admin','directory-orgadmin','PublicNew123'],
  ['admin_disabled','admin','directory-disabled','PublicNew123'],
  ['admin_unassigned','admin','directory-unassigned','PublicNew123'],
  ['admin_foreign','admin','directory-foreign','PublicNew123'],
  ['admin_missing','admin','absent','PublicNew123'],
  ['admin_empty','admin','directory-user',''],
  ['admin_whitespace','admin','directory-user',' \uFEFF '],
  ['admin_long','admin','directory-user','a'.repeat(100)],
  ['admin_unicode','admin','directory-user','用户α🙂'],
  ['admin_nul','admin','directory-user','a\0b'],
  ['admin_surrogate','admin','directory-user','a\ud800b'],
  ['dept_user','dept_admin','directory-user','PublicNew123'],
  ['dept_self','dept_admin',human.ids.homeMember,'PublicNew123'],
  ['dept_subadmin','dept_admin','directory-subadmin','PublicNew123'],
  ['dept_disabled','dept_admin','directory-disabled','PublicNew123'],
  ['dept_admin_denied','dept_admin','directory-orgadmin','PublicNew123'],
  ['dept_super_denied','dept_admin','directory-super','PublicNew123'],
  ['dept_sibling_denied','dept_admin','directory-sibling','PublicNew123'],
  ['dept_unassigned_denied','dept_admin','directory-unassigned','PublicNew123'],
  ['dept_actor_unassigned','dept_admin','directory-user','PublicNew123'],
];
const sourceHashes = Object.fromEntries(paths.map((path, i) =>
  [path, createHash('sha256').update(sources[i]).digest('hex')]));
const code = `
import { DatabaseSync } from 'node:sqlite';
import { randomBytes,scryptSync } from 'node:crypto';
const ADMIN_ROLES=new Set(['admin','super_admin']);
class AuthServiceError extends Error {constructor(public status:number,message:string){super(message)}}
${['toPasswordHashRecord','hashPassword','mapUser','mapDepartment'].map(name=>fn(sources[1],name)).join('\n')}
const timestamp=${human.verificationTime * 1000 + 123};
Date.now=()=>timestamp;
class OriginalDb {
  constructor(public driver){}
${['getUserByIdAndOrg','listDepartmentsByOrg','updateUserPassword'].map(name=>method(sources[1],name)).join('\n')}
}
class OriginalAdmin {
  constructor(public db){}
  async requireAuthUser(auth){const actor=await this.db.getUserByIdAndOrg(auth.userId,auth.orgId);if(!actor||actor.status!=='active')throw new AuthServiceError(401,'Unavailable actor');return actor;}
${['setUserPassword','assertCanManageExistingUser','canViewUser','getVisibleDepartmentIds'].map(name=>method(sources[0],name)).join('\n')}
}
const base=${JSON.stringify(seed.cases.find(c=>c.name==='admin'))};
const orgId=${JSON.stringify(human.ids.homeOrg)},actor=${JSON.stringify(human.ids.homeMember)};
const cases=[];
for(const [name,role,memberId,password] of ${JSON.stringify(inputs)}) {
  const db=new DatabaseSync(':memory:');db.exec(${JSON.stringify(schemas+'\n'+extension)});
  for(const [id,label] of [[orgId,'Original'],[${JSON.stringify(human.ids.companyOrg)},'Foreign']])db.prepare('INSERT INTO organizations VALUES(?,?,1)').run(id,label);
  for(const row of base.departments)db.prepare('INSERT INTO departments(id,org_id,parent_id,name,created_at,updated_at)VALUES(?,?,NULL,?,?,?)').run(row.id,row.org_id,row.name,row.created_at,row.updated_at);
  for(const row of base.departments)db.prepare('UPDATE departments SET parent_id=? WHERE id=?').run(row.parent_id,row.id);
  const users=structuredClone(base.users);users[0].role=role;
  if(name==='dept_actor_unassigned')users[0].department_id=null;
  for(const row of users)db.prepare('INSERT INTO users(id,org_id,email,name,role,status,department_id,created_at,local_auth,local_execution_allowed,token_limit,password_hash,password_updated_at,membership_revision)VALUES(?,?,?,?,?,?,?,?,0,0,17,?,123,7)').run(row.id,row.org_id,row.id+'@example.test',row.name,row.role,row.status,row.department_id,row.created_at,toPasswordHashRecord('PublicOld123','public-admin-fixture-salt'));
  const before=db.prepare('SELECT * FROM users ORDER BY id').all();
  const driver={async get(sql,params=[]){return db.prepare(sql).get(...params)},async all(sql,params=[]){return db.prepare(sql).all(...params)},async run(sql,params=[]){return db.prepare(sql).run(...params)}};
  let status=200,result=null;
  try{result=await new OriginalAdmin(new OriginalDb(driver)).setUserPassword({orgId,userId:memberId,password},{userId:actor,orgId})}catch(error){status=error.status??500}
  const after=db.prepare('SELECT * FROM users ORDER BY id').all();
  cases.push({name,role,memberId,password,status,result,before,after});db.close();
}
console.log(JSON.stringify({sourceCommit:${JSON.stringify(commit)},sourceHashes:${JSON.stringify(sourceHashes)},timestamp,producer:'Exact original setUserPassword/canViewUser/department-scope methods, hashPassword and AuthCenterDb SQL. Fixture same-org actor wrapper; current JWT/writer/race checks run separately in Python.',cases},null,2));
`;
const output=execFileSync(process.execPath,['--experimental-transform-types','--input-type=module-typescript','-'],{input:code,encoding:'utf8',maxBuffer:8*1024*1024});
writeFileSync(new URL('./member_password_legacy.json',import.meta.url),output,'utf8');
console.log(JSON.stringify({cases:JSON.parse(output).cases.length,sha256:createHash('sha256').update(output).digest('hex')}));
