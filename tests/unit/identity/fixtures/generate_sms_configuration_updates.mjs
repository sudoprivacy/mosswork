// Execute only the pinned original managed-SMS save path with fixture vault I/O.
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const repository = process.argv[2];
if (!repository) throw new Error('Pass the read-only retained Moss repository');
const commit = 'd0b84d4c8fd5905d9ec3f7c160f2ce633f535d0d';
const paths = ['src/server/configuration/platformIntegrationSettingsRepository.ts',
  'src/server/configuration/platformConfigDefinition.ts', 'src/server/configuration/platformConfigService.ts'];
const sources = paths.map(path => execFileSync('git', ['show', `${commit}:${path}`],
  { cwd: repository, encoding: 'utf8' }));
// Original methods and SMS definition are unchanged. Imports are supplied by the
// isolated fixture below; no provider, network, billing or credential service runs.
const bodies = sources.map(source => source.replace(/^import .*\n/gm, ''));
const program = `
import { DatabaseSync } from 'node:sqlite';
const randomUUID = () => 'frozen-next-version';
const { PlatformIntegrationSettingsRepository, ensurePlatformIntegrationSettingsSchema } = (() => {
 ${bodies[0].replace(/\bexport /g, '')}
 return { PlatformIntegrationSettingsRepository, ensurePlatformIntegrationSettingsSchema };
})();
const { PLATFORM_DEFINITIONS, PLATFORM_PROVIDERS } = (() => {
 ${bodies[1].replace(/\bexport /g, '')}
 return { PLATFORM_DEFINITIONS, PLATFORM_PROVIDERS };
})();
${bodies[2]}
const nowMs = 1800000000123;
Date.now = () => nowMs;
const original = {version:'original-version',config:{enabled:true,mockDelivery:false,
 sdkAppId:'fixture-app',signName:'fixture-sign',templateId:'fixture-template',region:'ap-beijing',
 templateParams:['{code}','{ttlMinutes}'],codeTtlSec:300,resendCooldownSec:60,maxSendsPerHour:5,maxVerifyAttempts:3},
 secretRefs:{secretId:'sms.original-version.secretId',secretKey:'sms.original-version.secretKey'},
 updatedBy:'original-writer',updatedAt:17};
const cases = [
 {name:'preserve_omitted',config:{}},
 {name:'trim_text',config:{signName:'  new sign  ',templateParams:[' code: {code} ']}},
 {name:'rotate_one_secret',config:{},secrets:{secretKey:' new-fixture-key '}},
 {name:'disabled_clear_secrets',config:{enabled:false},secrets:{secretId:null,secretKey:''}},
 {name:'mock_clear_secrets',config:{mockDelivery:true},secrets:{secretId:null,secretKey:null}},
 {name:'mock_bypasses_template_requirement',config:{mockDelivery:true,templateParams:[]}},
 {name:'missing_code',config:{templateParams:['{ttlMinutes}']},status:400},
 {name:'clear_required_text',config:{sdkAppId:'   '},status:400},
 {name:'clear_required_secret',config:{},secrets:{secretKey:null},status:400},
 {name:'mask_refused',config:{},secrets:{secretKey:'****hidden'},status:400},
 {name:'foreign_secret',config:{},secrets:{other:'x'},status:400},
 {name:'foreign_config',config:{other:'x'},status:400},
 {name:'wrong_boolean',config:{enabled:1},status:400},
 {name:'wrong_lines',config:{templateParams:[12]},status:400},
 {name:'wrong_integer',config:{codeTtlSec:1.5},status:400},
 {name:'numeric_low',config:{codeTtlSec:59},status:400},
 {name:'numeric_high',config:{maxSendsPerHour:101},status:400},
 {name:'disabled_still_validates_limits',config:{enabled:false,maxVerifyAttempts:0},status:400},
 {name:'old_version',config:{},expectedVersion:'stale',status:409},
 {name:'readback_refused',config:{},badVerification:true,status:503},
 {name:'ecmascript_bom_text_trim',config:{signName:'\ufeffnew sign\ufeff'}},
 {name:'ecmascript_secret_whitespace_preserved',config:{},secrets:{secretKey:'\u0085fixture-key\u0085'}},
];
for (const item of cases) {
 const db = new DatabaseSync(':memory:');ensurePlatformIntegrationSettingsSchema(db);
 db.prepare('INSERT INTO platform_integration_settings VALUES (?,?,?,7,8)')
   .run('platform.v1.sms',JSON.stringify(original),'original-writer');
 db.prepare('INSERT INTO platform_integration_settings VALUES (?,?,?,9,10)')
   .run('unrelated','{"keep":true}','unrelated-writer');
 let depth=0;
 const driver={get:async(sql,params)=>db.prepare(sql).get(...params),
  run:async(sql,params)=>db.prepare(sql).run(...params),
  transaction:async(fn)=>{const outer=depth++===0;if(outer)db.exec('BEGIN IMMEDIATE');
   try{const value=await fn();if(outer)db.exec('COMMIT');return value;}
   catch(error){if(outer)db.exec('ROLLBACK');throw error;}finally{depth--;}
  },tryRunExclusive:async(key,fn)=>{if(key!=='platform-config:sms')throw new Error('Foreign operation');return fn();}};
 const values=new Map([['sms.original-version.secretId','fixture-original-id'],
  ['sms.original-version.secretKey','fixture-original-key']]);
 const staged=[];
 const vault={getSecret:async(namespace,key)=>{if(namespace!=='moss:platform-config')throw new Error('Foreign namespace');
   return {value:item.badVerification&&key.includes('frozen-next-version')?'incorrect-fixture':values.get(key)};},
  putSecret:async(namespace,key,value)=>{if(namespace!=='moss:platform-config')throw new Error('Foreign namespace');values.set(key,value);staged.push(key);}};
 const service=new PlatformConfigService({driver,vault,legacy:async()=>({}),instanceId:'fixture-instance'});
 let status=200,result=null;
 try{result=await service.save('sms',{config:item.config,secrets:item.secrets??{},
  expectedVersion:item.expectedVersion??'original-version'},'fixture-actor');}
 catch(error){if(!Number.isInteger(error.statusCode))throw error;status=error.statusCode;}
 if(status!==(item.status??200))throw new Error(item.name+':'+status);
 item.actualStatus=status;item.result=result;item.stagedReferences=staged;
 item.stagedFixtureValues=Object.fromEntries(staged.map(key=>[key,values.get(key)]));
 item.rows=db.prepare('SELECT * FROM platform_integration_settings ORDER BY setting_key').all()
  .map(row=>({...row,value_json:JSON.parse(row.value_json)}));
 db.close();
}
console.log(JSON.stringify({commit:${JSON.stringify(commit)},
 sourceHashes:${JSON.stringify(Object.fromEntries(paths.map((path,i)=>[path,createHash('sha256').update(sources[i]).digest('hex')])) )},
 scope:'Original managed SMS save/prepare/readSnapshot/validation/repository, actual isolated SQLite and fixture vault. No real secrets, provider, activation, financial operation or production.',
 nowMs,original,cases},null,2));
`;
const output = execFileSync(process.execPath,
  ['--experimental-transform-types', '--input-type=module-typescript', '-'],
  { input: program, encoding: 'utf8' });
writeFileSync(fileURLToPath(new URL('./sms_configuration_updates_legacy.json', import.meta.url)), output, 'utf8');
console.log(JSON.stringify({ cases: JSON.parse(output).cases.length,
  sha256: createHash('sha256').update(output).digest('hex') }));
