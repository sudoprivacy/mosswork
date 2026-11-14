// Freeze original organization-scoped invitation listing against SQLite.
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const repository = process.argv[2];
if (!repository) throw new Error('Pass the retained Moss repository');
const commit = 'd0b84d4c8fd5905d9ec3f7c160f2ce633f535d0d';
const paths = ['src/server/identity/identityRepository.ts',
  'src/server/identity/organizationIdentityService.ts', 'src/server/authCenter/db.ts'];
const sources = paths.map(path => execFileSync('git', ['show', `${commit}:${path}`],
  { cwd: repository, encoding: 'utf8' }));
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
const schema = sources[0].match(/CREATE TABLE IF NOT EXISTS invitations \([\s\S]*?\n\s*\);/)[0];
const parentSchema = ['organizations', 'departments', 'users'].map(table =>
  sources[2].match(new RegExp(`CREATE TABLE IF NOT EXISTS ${table} \\([\\s\\S]*?\\n\\s*\\);`))[0]).join('\n');
const sourceHashes = Object.fromEntries(paths.map((path, index) =>
  [path, createHash('sha256').update(sources[index]).digest('hex')]));
const code = `
import { DatabaseSync } from 'node:sqlite';
class IdentityDomainError extends Error { constructor(public code, message) { super(message); } }
${fn(sources[0], 'mapInvitation')}
${fn(sources[1], 'hasGlobalOrganizationAccess')}
class OriginalRepo {
  constructor(public driver) {}
${['listInvitations', 'listInvitationsAsync'].map(name => method(sources[0], name)).join('\n')}
}
class OriginalService {
  constructor(public repository) {}
${method(sources[1], 'listInvitations')}
}
const rows = [
  ['home-a','home','CODE-A','pending',0,null,null,20,null],
  ['home-z','home','CODE-Z','used',125,1.25,'original-member',20,50],
  ['home-old','home','CODE-OLD','revoked',10.5,0,null,10,null],
  ['home-pending','home','CODE-PENDING','pending',99,null,null,15,null],
  ['foreign-a','foreign','FOREIGN-A','pending',99,null,null,100,null],
  ['foreign-z','foreign','FOREIGN-Z','used',77,5,'foreign-member',100,200],
];
const cases=[];
for (const role of ['admin','super_admin','dept_admin','user']) {
  for (const [name,input] of Object.entries({default:{},pending:{status:'pending'},
    used:{status:'used'},revoked:{status:'revoked'},page:{limit:1,offset:1},
    minimum:{limit:0,offset:-3},maximum:{limit:999},past_end:{offset:99},
    foreign_selector:{orgId:'foreign'}})) {
    const db=new DatabaseSync(':memory:');
    db.exec(${JSON.stringify(parentSchema + '\n' + schema)});
    for(const org of ['home','foreign'])
      db.prepare('INSERT INTO organizations(id,name,created_at) VALUES (?,?,1)').run(org,org);
    for(const [id,org] of [['original-member','home'],['foreign-member','foreign']])
      db.prepare('INSERT INTO users(id,org_id,email,name,role,status,created_at) VALUES (?,?,?,?,?,?,1)')
        .run(id,org,id+'@example.test',id,'user','active');
    const insert=db.prepare('INSERT INTO invitations VALUES (?,?,?,?,?,?,?,?,?)');
    for(const row of rows)insert.run(...row);
    const driver={async get(sql,params=[]){return db.prepare(sql).get(...params)},
      async all(sql,params=[]){return db.prepare(sql).all(...params)}};
    const service=new OriginalService(new OriginalRepo(driver));
    try {
      const result=await service.listInvitations(input,
        {userId:'actor',orgId:'home',role,organizationScoped:true});
      cases.push({name:role+'_'+name,role,input,status:200,expected:{total:result.total,
        items:result.items.map(row=>({invitation_id:row.id,organization_id:row.orgId,
          code:row.code,status:row.status,initial_credit_units:row.initialCreditUnits,
          legacy_initial_quota_usd:row.legacyInitialQuotaUsd,
          used_by_member_id:row.usedByUserId,created_at:row.createdAt,used_at:row.usedAt}))}});
    } catch(error) {if(error.code!=='FORBIDDEN')throw error;
      cases.push({name:role+'_'+name,role,input,status:403});}
    db.close();
  }
}
console.log(JSON.stringify({sourceCommit:${JSON.stringify(commit)},
  sourceHashes:${JSON.stringify(sourceHashes)},schema:${JSON.stringify(schema)},rows,
  producer:'Exact original listInvitations/listInvitationsAsync/mapInvitation and organization-scoped actor predicate against node:sqlite; no issuance/redemption or live store',cases},null,2));
`;
const output = execFileSync(process.execPath,
  ['--experimental-transform-types', '--input-type=module-typescript', '-'],
  { input: code, encoding: 'utf8', maxBuffer: 4 * 1024 * 1024 });
writeFileSync(fileURLToPath(new URL('./invitation_directory_legacy.json', import.meta.url)), output, 'utf8');
console.log(JSON.stringify({cases:JSON.parse(output).cases.length,
  sha256:createHash('sha256').update(output).digest('hex')}));
