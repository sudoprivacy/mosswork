// Public dummy passwords/SQLite only. Executes exact pinned original methods.
import { DatabaseSync } from 'node:sqlite';
class Database {
  private db: DatabaseSync;
  constructor(path: string) { this.db=new DatabaseSync(path); }
  exec(sql: string) { this.db.exec(sql); }
  run(sql: string, params: any[] = []) { return this.db.prepare(sql).run(...params); }
  query(sql: string) { return this.db.prepare(sql); }
  close() { this.db.close(); }
}
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { join, resolve, dirname } from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';

const root = resolve(process.argv[2]);
const bcryptPath = resolve(process.argv[3]);
const source = readFileSync(join(root, 'src/server/auth/service.ts'), 'utf8');
const dbSource = readFileSync(join(root, 'src/server/authCenter/db.ts'), 'utf8');
const section = (text: string, start: string, end: string) => {
  const a = text.indexOf(start), b = text.indexOf(end, a + start.length);
  if(a < 0 || b < 0) throw new Error('Pinned source boundary missing');
  return text.slice(a,b);
};
const helpers = join(root,'profile-password-helpers.ts');
writeFileSync(helpers,
  `import {randomBytes,scryptSync,timingSafeEqual} from 'node:crypto';\n`+
  `import {compareSync as compareBcryptSync} from ${JSON.stringify(pathToFileURL(bcryptPath).href)};\n`+
  section(dbSource,'function toPasswordHashRecord(','export function createTemporaryPassword(')+
  '\nexport {toPasswordHashRecord};\n');
const {toPasswordHashRecord,verifyPassword} = await import(pathToFileURL(helpers).href);
const {hashSync}=await import(pathToFileURL(bcryptPath).href);
const originalPath=join(root,'original-profile-domain.ts');
writeFileSync(originalPath,
  `import {randomUUID} from 'node:crypto';\nimport {hashPassword,verifyPassword} from './profile-password-helpers.ts';\n`+
  `export class AuthServiceError extends Error {constructor(readonly statusCode:number,message:string){super(message)}}\n`+
  `export class OriginalProfile {constructor(public db:any,public identityRepository:any){}\n`+
  `async requireAuthUser(auth:any){const u=await this.db.getUserById(auth.userId);if(!u||u.status!=='active')throw new AuthServiceError(401,'Invalid user');return u;}\n`+
  section(source,'  /** Account-owned profile fields,','  async getMe(')+
  section(source,'  async changeOwnPassword(','  async listApiKeys(')+'\n}\n');
const {OriginalProfile}=await import(pathToFileURL(originalPath).href);
const human=JSON.parse(readFileSync(join(dirname(fileURLToPath(import.meta.url)),'human_tokens_legacy.json'),'utf8'));
const ids=human.ids,now=human.verificationTime;
Date.now=()=>now*1000;
const extension=readFileSync(join(root,'profile-extension.sql'),'utf8');
const schema=readFileSync(join(root,'canonical-fixture.sql'),'utf8')+extension;
const oldPassword='PublicOld123';
const passwordHash=toPasswordHashRecord(oldPassword,'public-profile-fixture-salt');
const bcryptHash=hashSync(oldPassword,'$2b$04$abcdefghijklmnopqrstuu');
const avatar='data:image/webp;base64,UklGRiIAAABXRUJQVlA4IBYAAAAwAQCdASoBAAEADsD+JaQAA3AAAAAA';
function environment(hash=passwordHash){
  const sql=new Database(':memory:');sql.exec(schema);
  sql.run('INSERT INTO organizations VALUES (?,?,1)',[ids.homeOrg,'Home']);
  sql.run("INSERT INTO users(id,org_id,email,name,role,status,password_hash,created_at,display_name,local_auth,local_execution_allowed) VALUES (?,?,?,'home','admin','active',?,1,'Original',0,0)",[ids.homeMember,ids.homeOrg,'home@example.test',hash]);
  const db={
    getUserById:async(id:string)=>{const r:any=sql.query('SELECT * FROM users WHERE id=?').get(id);return r?{...r,orgId:r.org_id,passwordHash:r.password_hash,displayName:r.display_name}:null},
    getUserAvatar:async(id:string)=>(sql.query('SELECT data_url FROM user_profile_avatars WHERE user_id=?').get(id) as any)?.data_url??null,
    updateUser:async(id:string,p:any)=>sql.run('UPDATE users SET display_name=? WHERE id=?',[p.displayName,id]),
    updateUserPassword:async(id:string,h:string,at:number)=>sql.run('UPDATE users SET password_hash=?,password_updated_at=? WHERE id=?',[h,at,id]),
    driver:{transaction:async(fn:any)=>{sql.exec('BEGIN IMMEDIATE');try{const r=await fn();sql.exec('COMMIT');return r}catch(e){sql.exec('ROLLBACK');throw e}},run:async(q:string,p:any[])=>sql.run(q,p)},
  };
  const repo={findAuthIdentityByUser:async(id:string,p:string,i:string)=>sql.query('SELECT * FROM user_auth_identities WHERE user_id=? AND provider=? AND issuer=?').get(id,p,i),createAuthIdentity:async(v:any)=>sql.run('INSERT INTO user_auth_identities VALUES (?,?,?,?,?,?,?,?,?)',[v.id,v.orgId,v.userId,v.provider,v.issuer,v.normalizedSubject,JSON.stringify(v.metadata),Date.now(),Date.now()])};
  return {sql,service:new OriginalProfile(db,repo),auth:{userId:ids.homeMember,orgId:ids.homeOrg}};
}
const nicknames=[['trim','\ufeff \tNickname\u00a0'],['empty',''],['whitespace','\u3000\ufeff'],['sixty_utf16','😀'.repeat(30)],['over_sixty_utf16','😀'.repeat(31)],['unicode','用户α🙂'],['not_js_whitespace','\u0085Name\u0085'],['lone_surrogate','Name\ud800']];
const nicknameCases=[];
for(const [name,nickname] of nicknames){const {sql,service,auth}=environment();let status=200;try{await service.updateOwnNickname(auth,nickname)}catch(e:any){status=e.statusCode??500}nicknameCases.push({name,nickname,status,profile:await service.getOwnProfileDetails(auth)});sql.close()}
const passwords=[['minimum','Abcdefg1'],['too_short','Abcdef1'],['maximum','A'+'a'.repeat(18)+'1'],['too_long','A'+'a'.repeat(19)+'1'],['astral_twenty','Aa1'+'😀'.repeat(8)+'x'],['astral_twenty_one','Aa1'+'😀'.repeat(9)],['unicode_digit','Abcdefg１'],['no_upper','abcdefg1'],['no_lower','ABCDEFG1'],['no_digit','Abcdefgh'],['lone_surrogate','Abcdef1\ud800'],['nul','Abcdef1\0'],['wrong_old','Abcdefg1'],['wrong_old_and_bad_new','bad'],['bcrypt_2a','Abcdefg1'],['bcrypt_2b','Abcdefg1'],['bcrypt_2y','Abcdefg1']];
const passwordCases=[];
for(const [name,newPassword] of passwords){const legacy=name.startsWith('bcrypt_');const oldHash=legacy?bcryptHash.replace('$2b$',`$${name.slice(-2)}$`):passwordHash;const {sql,service,auth}=environment(oldHash);let status=200;const suppliedOld=name.startsWith('wrong_old')?'incorrect':oldPassword;try{await service.changeOwnPassword(auth,suppliedOld,newPassword)}catch(e:any){status=e.statusCode??500}const row:any=sql.query('SELECT password_hash,password_updated_at,local_auth,local_execution_allowed,name,display_name FROM users').get();passwordCases.push({name,oldPassword:suppliedOld,newPassword,oldHash,status,row,newVerified:verifyPassword(newPassword,row.password_hash),identities:sql.query('SELECT org_id,user_id,provider,issuer,normalized_subject,metadata_json FROM user_auth_identities').all()});sql.close()}
const {sql,service,auth}=environment();sql.run('INSERT INTO user_profile_avatars VALUES (?,?)',[ids.homeMember,avatar]);const profile=await service.getOwnProfileDetails(auth);sql.close();
const paths=['src/server/auth/service.ts','src/server/authCenter/db.ts','src/server/identity/identityRepository.ts'];
console.log(JSON.stringify({sourceCommit:'d0b84d4c8fd5905d9ec3f7c160f2ce633f535d0d',sourceHashes:Object.fromEntries(paths.map(p=>[p,createHash('sha256').update(readFileSync(join(root,p))).digest('hex')])),ids,now,extension,oldPassword,passwordHash,bcryptHash,avatar,profile,nicknameCases,passwordCases},null,2));
