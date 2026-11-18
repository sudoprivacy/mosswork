import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
const require=createRequire(new URL('../../dashboard/package.json',import.meta.url));
const {build}=require('esbuild');const {JSDOM}=require('jsdom');
const output=await build({entryPoints:[fileURLToPath(new URL('../login/main.tsx',import.meta.url))],bundle:true,write:false,format:'iife',jsx:'automatic',nodePaths:[fileURLToPath(new URL('../../dashboard/node_modules',import.meta.url))],loader:{'.css':'empty'},define:{'process.env.NODE_ENV':'"production"'},plugins:[{name:'native-test',setup(b){
 b.onResolve({filter:/^\/wails\/runtime\.js$/},()=>({path:'runtime',namespace:'test'}));
 b.onLoad({filter:/.*/,namespace:'test'},()=>({contents:'export const Call={ByName:(...args)=>window.nativeCall(...args)}'}));
 b.onResolve({filter:/\/CaptchaField$/},()=>({path:'captcha',namespace:'captcha'}));
 b.onLoad({filter:/.*/,namespace:'captcha'},()=>({contents:'import {forwardRef,useEffect,useImperativeHandle} from "react";export default forwardRef(function(props,ref){useImperativeHandle(ref,()=>({getToken:async()=>"test-verification"}));useEffect(()=>props.onReadyChange(true),[props.resetKey]);return null;});',resolveDir:fileURLToPath(new URL('../../dashboard',import.meta.url))}));
}}]});
const source=output.outputFiles[0].text;
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function fixture(address='',bootstrapStatus={available:false,configured:false},overrides={},query=''){
 const dom=new JSDOM('<body><div id="login-root"></div></body>',{runScripts:'outside-only',url:'http://wails.localhost/login.html'+query});const calls=[];
 dom.window.nativeCall=async(name,...args)=>{calls.push({name,args});const method=name.split('.').at(-1);if(overrides[method])return overrides[method](...args);if(method==='GetBootstrapStatus')return bootstrapStatus;if(method==='SendCloudLoginCode')return 60;if(method==='GetCompanyMode')return address;if(method==='GetSettings')return {locale:'zh'};if(method==='GetLoginConfig')return {provider:'slider'};if(method==='LoginCompany')throw Error('账号或密码错误');};
 dom.window.eval(source);
 // Observe completion of the main form's native settings load.
 const deadline=Date.now()+1000;
 while(!dom.window.document.querySelector('form[aria-busy="false"]') && Date.now()<deadline)await sleep(5);
 assert.ok(dom.window.document.querySelector('form[aria-busy="false"]'),'native login form did not become ready');
 return {dom,calls,q:s=>dom.window.document.querySelector(s)};
}
function type(p,id,value){const el=p.q(id);Object.getOwnPropertyDescriptor(p.dom.window.HTMLInputElement.prototype,'value').set.call(el,value);el.dispatchEvent(new p.dom.window.Event('input',{bubbles:true}));}
test('startup is an employee form; server accordion starts collapsed and preserves entered credentials',async()=>{
 const p=await fixture();try{
  assert.equal(p.q('h1').textContent,'员工登录');assert.ok(p.q('#username'));assert.ok(p.q('#password'));assert.equal(p.q('#server-fields').hidden,true);
  type(p,'#username','employee');type(p,'#password','sample-password');p.q('.server-toggle').click();await sleep(10);
  assert.equal(p.q('#server-fields').hidden,false);assert.equal(p.q('#password').value,'sample-password');
  assert.equal(p.q('form').textContent.includes('连接并登录'),false);assert.equal(p.calls.some(x=>x.name.includes('LoginCompany')),false);
 }finally{p.dom.window.close();}
});
test('one submit contains address and credentials; failure stays on this form',async()=>{
 const p=await fixture();try{
  p.q('.server-toggle').click();await sleep(10);type(p,'#server-address','192.0.2.28:8088');type(p,'#username','employee');type(p,'#password','sample-password');await sleep(550);
  assert.equal(p.q('.login').disabled,false);p.q('form').dispatchEvent(new p.dom.window.Event('submit',{bubbles:true,cancelable:true}));await sleep(20);
  const login=p.calls.find(x=>x.name.endsWith('LoginCompany'));assert.equal(login.args[0].address,'192.0.2.28:8088');assert.equal(login.args[0].username,'employee');assert.equal(login.args[0].password,'sample-password');assert.equal(login.args[0].captcha_token,'test-verification');
  assert.match(p.q('[role=alert]').textContent,/账号或密码错误/);assert.equal(p.dom.window.location.pathname,'/login.html');assert.equal(p.q('#username').value,'employee');
 }finally{p.dom.window.close();}
});
test('saved server is loaded without requiring a separate connect action',async()=>{
 const p=await fixture('http://company.internal:8088');try{await sleep(500);assert.equal(p.q('#server-address').value,'http://company.internal:8088');assert.equal(p.q('#server-fields').hidden,true);assert.equal(p.q('.login').disabled,false);}finally{p.dom.window.close();}
});

test('organization sign-in opens the existing workspace with no native credentials',async()=>{
 const address='https://company.example';
 const p=await fixture(address,{available:true,configured:false},{GetLoginConfig:()=>({canonical:true})});
 try {
  await sleep(550);
  assert.equal(p.q('#username'),null);assert.equal(p.q('#password'),null);
  assert.equal(p.q('.captcha'),null);assert.equal(p.q('.login').textContent,'进入工作台登录');
  p.q('.remember input').click();p.q('.login').click();await sleep(20);
  assert.deepEqual(p.calls.find(c=>c.name.endsWith('OpenCompanyAccountLogin')).args,[address,false]);
  assert.equal(p.calls.some(c=>/\.(LoginCompany|LoginCloud|OpenBootstrapAssistant)$/.test(c.name)),false);
 }finally{p.dom.window.close();}
});

test('a fresh deployment with a configured organization opens its existing sign-in in the same window',async()=>{
 const address='https://company.example';
 for(const interacted of [false,true]){
  const p=await fixture(address,{available:true,configured:false},{GetLoginConfig:()=>({canonical:true})},'?companyDefault=1');
  try{
   if(interacted)p.q('form').dispatchEvent(new p.dom.window.Event('keydown',{bubbles:true}));
   await sleep(550);
   assert.equal(p.calls.filter(c=>c.name.endsWith('OpenCompanyAccountLogin')).length,interacted?0:1);
   assert.equal(p.calls.some(c=>/\.(LoginCompany|SendCloudLoginCode|LoginCloud|OpenBootstrapAssistant)$/.test(c.name)),false);
  }finally{p.dom.window.close();}
 }
});
test('main login offers phone authentication and prepares the assistant without an API key',async()=>{
 const p=await fixture('',{available:true,configured:false});try{
  const form=p.q('form');assert.equal(p.q('h1').textContent,'员工登录');assert.ok(p.q('#phone'));assert.ok(p.q('#code'));assert.equal(p.q('#password'),null);
  assert.equal(p.q('.brand').textContent.includes('Private'),false);
  assert.equal(p.calls.some(c=>c.name.endsWith('SendCloudLoginCode')),false);
  type(p,'#phone','13800000000');await sleep(10);p.q('[data-action=send-code]').click();await sleep(20);
  assert.deepEqual(p.calls.find(c=>c.name.endsWith('SendCloudLoginCode')).args,['13800000000']);
  assert.equal(p.q('[data-action=send-code]').disabled,true);
  type(p,'#code','123456');await sleep(10);form.dispatchEvent(new p.dom.window.Event('submit',{bubbles:true,cancelable:true}));await sleep(25);
  assert.deepEqual(p.calls.find(c=>c.name.endsWith('LoginCloud')).args,['13800000000','123456']);
  assert.equal(p.q('form'),form);assert.equal(p.q('#code').value,'');assert.ok(p.q('[data-action=open-assistant]'));assert.match(p.q('[role=status]').textContent,/部署助手已在终端打开/);
  assert.equal(p.calls.some(c=>c.name.endsWith('LoginCompany')),false);
 }finally{p.dom.window.close();}
});
test('configured deployment is an explicit resume action, not a replacement login page',async()=>{
 const p=await fixture('',{available:true,configured:true});try{
  assert.ok(p.q('#phone'));assert.ok(p.q('[data-method=company]'));p.q('[data-action=open-assistant]').click();await sleep(25);
  assert.equal(p.calls.filter(c=>c.name.endsWith('OpenBootstrapAssistant')).length,1);
  assert.equal(p.calls.some(c=>c.name.endsWith('LoginCloud')||c.name.endsWith('SendCloudLoginCode')),false);
 }finally{p.dom.window.close();}
});

test('adding bootstrap preserves the saved company login and allows switching in the same form',async()=>{
 const p=await fixture('https://company.example',{available:true,configured:false});try{
  const form=p.q('form');assert.ok(p.q('#username'));assert.equal(p.q('#phone'),null);
  type(p,'#password','company-secret');p.q('[data-method=cloud]').click();await sleep(20);
  assert.equal(p.q('form'),form);assert.ok(p.q('#phone'));type(p,'#code','123456');
  p.q('[data-method=company]').click();await sleep(500);
  assert.equal(p.q('form'),form);assert.equal(p.q('#password').value,'');assert.equal(p.q('#server-address').value,'https://company.example');assert.equal(p.q('.login').disabled,false);
  type(p,'#username','employee');type(p,'#password','new-secret');form.dispatchEvent(new p.dom.window.Event('submit',{bubbles:true,cancelable:true}));await sleep(20);
  assert.equal(p.calls.filter(c=>c.name.endsWith('LoginCompany')).length,1);assert.equal(p.calls.some(c=>c.name.endsWith('LoginCloud')),false);
  p.q('[data-method=cloud]').click();await sleep(20);assert.equal(p.q('#code').value,'');
 }finally{p.dom.window.close();}
});

test('optional deployment inventory failure does not block company login',async()=>{
 const p=await fixture('https://company.example',undefined,{GetBootstrapStatus:()=>{throw Error('unavailable')}});try{
  await sleep(500);assert.ok(p.q('#username'));assert.equal(p.q('.login').disabled,false);assert.equal(p.q('[data-method=cloud]'),null);
 }finally{p.dom.window.close();}
});

test('Cloud failure is visible and switching methods cannot race an outstanding login',async()=>{
 let rejectLogin;const pending=new Promise((_,reject)=>{rejectLogin=reject});
 const p=await fixture('',{available:true,configured:false},{LoginCloud:()=>pending});try{
  type(p,'#phone','13800000000');type(p,'#code','123456');await sleep(10);
  p.q('form').dispatchEvent(new p.dom.window.Event('submit',{bubbles:true,cancelable:true}));await sleep(20);
  assert.equal(p.q('[data-method=company]').disabled,true);p.q('[data-method=company]').click();assert.ok(p.q('#phone'));
  rejectLogin(Error('Cloud model access is not ready'));await sleep(20);
  assert.match(p.q('[role=alert]').textContent,/Cloud model access is not ready/);assert.equal(p.q('[data-action=open-assistant]'),null);
  assert.equal(p.calls.filter(c=>c.name.endsWith('LoginCloud')).length,1);assert.equal(p.calls.some(c=>c.name.endsWith('LoginCompany')),false);
  p.q('[data-method=company]').click();await sleep(20);assert.ok(p.q('#username'));
 }finally{p.dom.window.close();}
});
