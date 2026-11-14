// Verify synthetic Python-created hashes with the exact original TS helper.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const {verifyPassword} = await import(pathToFileURL(resolve(process.argv[2])).href);
const cases=JSON.parse(readFileSync(resolve(process.argv[3]),'utf8'));
for(const item of cases){
  if(!verifyPassword(item.password,item.hash)||verifyPassword('wrong-public-password',item.hash)){
    throw new Error('Python password change hash failed original TS verification');
  }
}
console.log(JSON.stringify({ok:true,cases:cases.map((c:any)=>c.name)},null,2));
