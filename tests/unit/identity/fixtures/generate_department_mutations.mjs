// Execute the pinned original department methods and SQL, without a Python model.
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const repository = process.argv[2];
if (!repository) throw new Error('Pass the retained Moss source repository');
const commit = 'd0b84d4c8fd5905d9ec3f7c160f2ce633f535d0d';
const paths = ['src/server/auth/service.ts', 'src/server/authCenter/db.ts', 'src/server/db/driver.ts'];
const sources = paths.map(path => execFileSync('git', ['show', `${commit}:${path}`],
  { cwd: repository, encoding: 'utf8' }));
function method(source, name) {
  const match = source.match(new RegExp(`^  (?:private )?(?:async )?${name}\\([\\s\\S]*?^  }\\r?$`, 'm'));
  if (!match) throw new Error('Missing original method ' + name);
  return match[0].replace(/^  private /, '  ');
}
function fn(source, name) {
  const match = source.match(new RegExp(`^(?:export )?(?:async )?function ${name}(?:<[^>]+>)?\\([\\s\\S]*?^}`, 'm'));
  if (!match) throw new Error('Missing original function ' + name);
  return match[0].replace(/^export /, '');
}
const schemas = ['organizations', 'departments', 'users'].map(table => {
  const match = sources[1].match(new RegExp(`CREATE TABLE IF NOT EXISTS ${table} \\([\\s\\S]*?\\n\\s*\\);`));
  if (!match) throw new Error('Missing original DDL ' + table);
  return match[0];
}).join('\n');
const extension = sources[1].match(/ALTER TABLE departments ADD COLUMN ext_dept_id TEXT/)[0];
const index = sources[1].match(/CREATE UNIQUE INDEX IF NOT EXISTS departments_ext_uniq[\s\S]*?;/)[0];
const seed = JSON.parse(readFileSync(new URL('./organization_directory_legacy.json', import.meta.url), 'utf8'));
const human = JSON.parse(readFileSync(new URL('./human_tokens_legacy.json', import.meta.url), 'utf8'));
const inputs = [
  ['create_admin_root', 'admin', 'create', null, {name:' New root '}],
  ['create_admin_child', 'admin', 'create', null, {name:'New',parentId:'child'}],
  ['create_dept_child', 'dept_admin', 'create', null, {name:'New',parentId:'child'}],
  ['create_dept_descendant', 'dept_admin', 'create', null, {name:'New',parentId:'grand'}],
  ['create_dept_root', 'dept_admin', 'create', null, {name:'New'}],
  ['create_dept_sibling', 'dept_admin', 'create', null, {name:'New',parentId:'sibling'}],
  ['create_foreign_parent', 'admin', 'create', null, {name:'New',parentId:'foreign'}],
  ['create_missing_parent', 'admin', 'create', null, {name:'New',parentId:'absent'}],
  ['create_blank_name', 'admin', 'create', null, {name:' \uFEFF '}],
  ['create_sibling_conflict', 'admin', 'create', null, {name:'Grand',parentId:'child'}],
  ['create_external_conflict', 'admin', 'create', null, {name:'New',extDeptId:'external-root'}],
  ['create_foreign_external', 'admin', 'create', null, {name:'New',extDeptId:'foreign-only'}],
  ['create_trim', 'dept_admin', 'create', null, {name:'\uFEFF New \u00A0',parentId:' child ',extDeptId:' external-new '}],
  ['create_user_denied', 'user', 'create', null, {name:'New',parentId:'child'}],
  ['create_unassigned_dept_admin', 'dept_admin', 'create', null, {name:'New',parentId:'child'}],
  ['update_admin_name', 'admin', 'update', 'empty', {name:' Renamed '}],
  ['update_dept_name', 'dept_admin', 'update', 'empty', {name:'Renamed'}],
  ['update_dept_foreign_scope', 'dept_admin', 'update', 'sibling', {name:'Renamed'}],
  ['update_admin_root', 'admin', 'update', 'empty', {parentId:null}],
  ['update_dept_root', 'dept_admin', 'update', 'empty', {parentId:null}],
  ['update_dept_foreign_parent_scope', 'dept_admin', 'update', 'empty', {parentId:'sibling'}],
  ['update_self_parent', 'admin', 'update', 'empty', {parentId:'empty'}],
  ['update_descendant_parent', 'admin', 'update', 'child', {parentId:'grand'}],
  ['update_foreign_parent', 'admin', 'update', 'empty', {parentId:'foreign'}],
  ['update_missing_target', 'admin', 'update', 'absent', {name:'Renamed'}],
  ['update_foreign_target', 'admin', 'update', 'foreign', {name:'Renamed'}],
  ['update_empty_name', 'admin', 'update', 'empty', {name:' '}],
  ['update_empty_patch', 'admin', 'update', 'empty', {}],
  ['update_sibling_conflict', 'admin', 'update', 'empty', {name:'Grand'}],
  ['update_external_conflict', 'admin', 'update', 'empty', {extDeptId:'external-root'}],
  ['update_clear_external', 'dept_admin', 'update', 'empty', {extDeptId:null}],
  ['update_retain_external', 'dept_admin', 'update', 'empty', {extDeptId:'external-empty'}],
  ['delete_admin_empty', 'admin', 'delete', 'empty', {}],
  ['delete_dept_empty', 'dept_admin', 'delete', 'empty', {}],
  ['delete_foreign', 'admin', 'delete', 'foreign', {}],
  ['delete_missing', 'admin', 'delete', 'absent', {}],
  ['delete_unmanaged', 'dept_admin', 'delete', 'sibling', {}],
  ['delete_children', 'admin', 'delete', 'child', {}],
  ['delete_assigned', 'admin', 'delete', 'grand', {}],
  ['delete_disabled', 'admin', 'delete', 'empty', {}],
];
const sourceHashes = Object.fromEntries(paths.map((path, i) =>
  [path, createHash('sha256').update(sources[i]).digest('hex')]));
const code = `
import { DatabaseSync } from 'node:sqlite';
const ADMIN_ROLES = new Set(['admin','super_admin']);
class AuthServiceError extends Error { constructor(public status:number,message:string){super(message)} }
${fn(sources[2], 'isUniqueViolation')}
${fn(sources[0], 'isUniqueViolationOn')}
${fn(sources[0], 'withExtIdConflict')}
${fn(sources[1], 'mapUser')}
${fn(sources[1], 'mapDepartment')}
const timestamp = ${human.verificationTime * 1000 + 123};
Date.now = () => timestamp;
const now = () => timestamp;
const createdId = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd';
const randomUUID = () => createdId;
class OriginalDb {
  constructor(public driver){}
${['getUserByIdAndOrg','listUsersByOrg','getDepartmentById','getDepartmentByIdAndOrg','listDepartmentsByOrg','createDepartment','updateDepartment','deleteDepartment'].map(name=>method(sources[1],name)).join('\n')}
}
class OriginalDepartments {
  constructor(public db){}
  async requireAuthUser(auth){
    const actor = await this.db.getUserByIdAndOrg(auth.userId,auth.orgId);
    if(!actor || actor.status!=='active') throw new AuthServiceError(401,'Unavailable actor');
    return actor;
  }
${['createDepartment','updateDepartment','deleteDepartment','assertCanManageDepartment','getVisibleDepartmentIds','findSiblingDepartment','isDepartmentDescendant','countUsersForDepartment'].map(name=>method(sources[0],name)).join('\n')}
}
const base = ${JSON.stringify(seed.cases.find(c=>c.name==='admin'))};
const actor = ${JSON.stringify(human.ids.homeMember)}, orgId=${JSON.stringify(human.ids.homeOrg)};
const cases=[];
for(const [name,role,action,departmentId,changes] of ${JSON.stringify(inputs)}) {
  const db=new DatabaseSync(':memory:');
  db.exec(${JSON.stringify(schemas+'\n'+extension+';\n'+index)});
  for(const [id,label] of [[orgId,'Original'],[${JSON.stringify(human.ids.companyOrg)},'Foreign']]) db.prepare('INSERT INTO organizations VALUES (?,?,1)').run(id,label);
  const departments=base.departments.map(row=>({...row,ext_dept_id:row.id==='foreign'?'foreign-only':'external-'+row.id}));
  departments.push({id:'empty',org_id:orgId,parent_id:'child',name:'Empty',created_at:6,updated_at:6,ext_dept_id:'external-empty'});
  for(const row of departments) db.prepare('INSERT INTO departments(id,org_id,parent_id,name,created_at,updated_at,ext_dept_id) VALUES(?,?,NULL,?,?,?,?)').run(row.id,row.org_id,row.name,row.created_at,row.updated_at,row.ext_dept_id);
  for(const row of departments) db.prepare('UPDATE departments SET parent_id=? WHERE id=?').run(row.parent_id,row.id);
  const users=structuredClone(base.users);
  users[0].role=role;
  if(name==='create_unassigned_dept_admin') users[0].department_id=null;
  if(name==='delete_disabled') users.push({id:'only-disabled',org_id:orgId,name:'only-disabled',role:'user',status:'disabled',department_id:'empty',created_at:10});
  for(const row of users) db.prepare('INSERT INTO users(id,org_id,email,name,role,status,department_id,created_at) VALUES(?,?,?,?,?,?,?,?)').run(row.id,row.org_id,row.id+'@example.test',row.name,row.role,row.status,row.department_id,row.created_at);
  const driver={async get(sql,params=[]){return db.prepare(sql).get(...params)},async all(sql,params=[]){return db.prepare(sql).all(...params)},async run(sql,params=[]){return db.prepare(sql).run(...params)}};
  const original=new OriginalDepartments(new OriginalDb(driver));
  let error=null,result=null;
  try {
    const value=await original[action+'Department']({orgId,departmentId,...changes},{userId:actor,orgId});
    if(action!=='delete') { const row=value.department; result={department_id:row.id,organization_id:row.orgId,parent_id:row.parentId,name:row.name,external_id:row.extDeptId,created_at:row.createdAt,updated_at:row.updatedAt,user_count:row.userCount}; }
    else result=value;
  } catch(e) {error={status:e.status??null,message:e.message}}
  const after=db.prepare('SELECT * FROM departments ORDER BY created_at,id').all();
  cases.push({name,role,action,departmentId,changes,departments,users,result,error,after});
  db.close();
}
console.log(JSON.stringify({sourceCommit:${JSON.stringify(commit)},sourceHashes:${JSON.stringify(sourceHashes)},timestamp,createdId,supplementalSchema:${JSON.stringify(extension+';\n'+index)},producer:'Exact original department CRUD/scope/helpers and AuthCenterDb SQL; fixture same-org authenticated actor wrapper. Python tests separately use actual canonical JWT/current-writer checks.',cases},null,2));
`;
const output=execFileSync(process.execPath,['--experimental-transform-types','--input-type=module-typescript','-'],{input:code,encoding:'utf8',maxBuffer:8*1024*1024});
writeFileSync(fileURLToPath(new URL('./department_mutations_legacy.json',import.meta.url)),output,'utf8');
console.log(JSON.stringify({cases:JSON.parse(output).cases.length,sha256:createHash('sha256').update(output).digest('hex')}));
