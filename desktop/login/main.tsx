import { createRoot } from 'react-dom/client';
import { useEffect, useRef, useState } from 'react';
import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';
// Served by the native Wails runtime, never by the selected remote server.
// @ts-expect-error Native runtime module
import { Call } from '/wails/runtime.js';
import CaptchaField, { type CaptchaFieldHandle } from '../../dashboard/src/pages/Login/CaptchaField';
import type { PublicCaptchaConfig } from '../../dashboard/src/pages/Login/captchaAdapters';
import { useDesktopChrome } from '../../dashboard/src/hooks/useDesktopChrome';
import { loginErrorMessage } from './errorMessage';
import './style.css';

const call = (name: string, ...args: unknown[]) => Call.ByName('main.App.'+name,...args);
void i18n.use(initReactI18next).init({showSupportNotice:false,lng:'zh',fallbackLng:'zh',resources:{zh:{translation:{}},en:{translation:{}}},interpolation:{escapeValue:false}});

export function EmployeeLogin() {
  const chrome = useDesktopChrome();
  const [address,setAddress]=useState('');
  const [username,setUsername]=useState('');
  const [password,setPassword]=useState('');
  const [remember,setRemember]=useState(true);
  const [expanded,setExpanded]=useState(new URLSearchParams(location.search).has('server'));
  const [ready,setReady]=useState(false);
  const [loaded,setLoaded]=useState(false);
  const [en,setEn]=useState(false);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState<unknown>('');
  const [method,setMethod]=useState<'company'|'cloud'>('company');
  const [bootstrap,setBootstrap]=useState({available:false,configured:false});
  const [phone,setPhone]=useState('');
  const [code,setCode]=useState('');
  const [remaining,setRemaining]=useState(0);
  const [status,setStatus]=useState('');
  const [checking,setChecking]=useState(false);
  const [configAddress,setConfigAddress]=useState('');
  const [canonical,setCanonical]=useState(false);
  const [captcha,setCaptcha]=useState<PublicCaptchaConfig>({provider:'slider'});
  const [reset,setReset]=useState(0);
  const captchaRef=useRef<CaptchaFieldHandle>(null);
  const addressRef=useRef<HTMLInputElement>(null);
  const openDefault=useRef(new URLSearchParams(location.search).get('companyDefault')==='1');
  const text=(zh:string,english:string)=>en?english:zh;

  useEffect(()=>{
    if(!loaded)return;
    document.title=en?'mosswork':'mosswork';
    document.documentElement.lang=en?'en':'zh-CN';
  },[en,loaded]);

  useEffect(()=>{ void Promise.all([call('GetCompanyMode'),call('GetSettings'),call('GetBootstrapStatus').catch(()=>({available:false,configured:false}))]).then(([url,settings,capability])=>{
    const savedAddress=settings.companyUrl || url || '';
    setAddress(savedAddress);setBootstrap(capability);
    if(capability.available && !savedAddress && !new URLSearchParams(location.search).has('server'))setMethod('cloud');
    setEn(settings.locale==='en');void i18n.changeLanguage(settings.locale || 'zh');setLoaded(true);
  }).catch(()=>setLoaded(true)); },[]);

  useEffect(()=>{
    if(!loaded)return;
    let cancelled=false;
    setConfigAddress('');setCanonical(false);setReady(false);setReset(n=>n+1);setError('');
    if(method!=='company' || !address.trim()){setChecking(false);return;}
    setChecking(true);
    const timer=setTimeout(()=>{
      void call('GetLoginConfig',address.trim()).then(async(config:PublicCaptchaConfig & {canonical?:boolean})=>{
        if(cancelled)return;
        setCanonical(config.canonical===true);
        if(config.canonical){setUsername('');setPassword('');}
        else setCaptcha(config);
        setConfigAddress(address.trim());setChecking(false);
        if(config.canonical && openDefault.current) {
          openDefault.current=false;setBusy(true);
          try {await call('OpenCompanyAccountLogin',address.trim(),true);}
          catch(err){if(!cancelled){setBusy(false);setError(err);}}
        }
      }).catch((err:unknown)=>{if(!cancelled){setChecking(false);setError(err);}});
    },450);
    return ()=>{cancelled=true;clearTimeout(timer);};
  },[address,loaded,method]);

  useEffect(()=>{
    if(remaining<=0)return;
    const timer=setTimeout(()=>setRemaining(value=>Math.max(0,value-1)),1000);
    return ()=>clearTimeout(timer);
  },[remaining]);

  function chooseMethod(value:'company'|'cloud') {
    if(busy)return;
    setMethod(value);setPassword('');setCode('');setError('');setStatus('');
  }

  async function sendCode() {
    if(busy || remaining>0)return;
    setBusy(true);setError('');
    try {
      setRemaining(await call('SendCloudLoginCode',phone.trim()));
      setStatus(text('验证码已发送，请查看手机。','Code sent. Check your phone.'));
    } catch(err){setError(err);}
    finally{setBusy(false);}
  }

  async function openAssistant() {
    if(busy)return;
    setBusy(true);setError('');setStatus('');
    try {
      await call('OpenBootstrapAssistant');
      setStatus(text('部署助手已在终端打开，继续与它对话即可。','The setup assistant is open in Terminal. Continue your conversation there.'));
    } catch(err){setError(err);}
    finally{setBusy(false);}
  }

  async function chooseLanguage(locale: 'zh' | 'en') {
    try {
      await call('SaveLoginLocale', locale);
      setEn(locale === 'en');
      await i18n.changeLanguage(locale);
    } catch(err) { setError(err); }
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if(busy)return;
    if(method==='cloud') {
      setBusy(true);setError('');setStatus(text('正在登录并准备本机环境…','Signing in and preparing this device…'));
      try {
        await call('LoginCloud',phone.trim(),code.trim());
        setCode('');setBootstrap(current=>({...current,configured:true}));
        setStatus(text('部署助手已在终端打开，直接告诉它你想完成的下一步。','The setup assistant is open in Terminal. Tell it what you want to do next.'));
      } catch(err){setError(err);setStatus('');}
      finally{setBusy(false);}
      return;
    }
    if(!address.trim()){setExpanded(true);setError(text('请填写管理员提供的服务器地址','Enter the server address from your administrator'));setTimeout(()=>addressRef.current?.focus(),0);return;}
    if(configAddress!==address.trim() || (!canonical && !ready))return;
    setBusy(true);setError('');
    try {
      if(canonical){
        await call('OpenCompanyAccountLogin',configAddress,remember);
        return;
      }
      const token=await captchaRef.current?.getToken();
      await call('LoginCompany',{address:address.trim(),username,password,captcha_token:token || '',remember});
      setPassword('');
    } catch(err) {setError(err);setReady(false);setReset(n=>n+1);setBusy(false);}
  }

  return <>{chrome && <div className="window-controls" aria-label={text("窗口控制","Window controls")}>{[["minimise","−",text("最小化","Minimize")],["toggle-maximise","□",text("最大化","Maximize")],["close","×",text("关闭","Close")]].map(([action,icon,label])=><button key={action} type="button" aria-label={label} onClick={()=>{(window as unknown as {_wails?:{invoke:(value:string)=>void}})._wails?.invoke("wails:event:emit:desktop:"+action);}}>{icon}</button>)}</div>}<main className="login-page"><form className="login-card" onPointerDownCapture={()=>{openDefault.current=false}} onKeyDownCapture={()=>{openDefault.current=false}} aria-busy={!loaded || busy} onSubmit={submit}>
    <div className="login-language" role="group" aria-label={text('界面语言', 'Language')}>
      <button type="button" aria-pressed={!en} disabled={busy} onClick={()=>void chooseLanguage('zh')}>中文</button>
      <button type="button" aria-pressed={en} disabled={busy} onClick={()=>void chooseLanguage('en')}>English</button>
    </div>
    <img className="logo" src="/mosswork-icon.png" alt={text('mosswork','mosswork')}/><div className="brand">mosswork <span className="edition">{text('企业版','Enterprise')}</span></div>
    <h1>{text('员工登录','Employee sign in')}</h1><p className="intro">{method==='cloud'?text('使用已有的 SudoWork 账号，登录后开始与部署助手对话。','Use your existing SudoWork account to start with the setup assistant.'):text('使用管理员分配的账号登录','Sign in with the account provided by your administrator')}</p>
    {bootstrap.available && <div className="login-methods" role="group" aria-label={text('登录方式','Sign-in method')}>
      <button type="button" data-method="cloud" aria-pressed={method==='cloud'} disabled={busy} onClick={()=>chooseMethod('cloud')}>{text('手机验证码','Phone code')}</button>
      <button type="button" data-method="company" aria-pressed={method==='company'} disabled={busy} onClick={()=>chooseMethod('company')}>{text('企业账号','Company account')}</button>
    </div>}
    {method==='cloud'? <>
      <label htmlFor="phone">{text('手机号','Phone number')}</label><input id="phone" type="tel" autoComplete="tel" required maxLength={16} value={phone} onChange={e=>setPhone(e.target.value)} disabled={busy}/>
      <button type="button" className="server-toggle" data-action="send-code" disabled={busy || remaining>0 || !phone.trim()} onClick={()=>void sendCode()}>{remaining>0?text(`${remaining} 秒后可重新发送`,`Resend in ${remaining}s`):text('发送验证码','Send code')}</button>
      <label htmlFor="code">{text('验证码','Verification code')}</label><input id="code" inputMode="numeric" autoComplete="one-time-code" required pattern="[0-9]{4,8}" maxLength={8} value={code} onChange={e=>setCode(e.target.value)} disabled={busy}/>
    </>: <>
    {!canonical && <>
    <label htmlFor="username">{text('账号','Account')}</label><input id="username" autoComplete="username" placeholder={text('用户名或邮箱','Username or email')} required maxLength={320} value={username} onChange={e=>setUsername(e.target.value)} disabled={busy}/>
    <label htmlFor="password">{text('密码','Password')}</label><input id="password" type="password" autoComplete="current-password" placeholder={text('请输入密码','Password')} required maxLength={4096} value={password} onChange={e=>setPassword(e.target.value)} disabled={busy}/>
    {configAddress && <div className="captcha"><CaptchaField key={configAddress} ref={captchaRef} config={captcha} resetKey={reset} slideHint={text('向右滑动完成验证','Slide right to verify')} slideVerifiedLabel={text('验证通过','Verified')} unsupportedLabel={text('不支持的验证方式，请联系管理员','Unsupported verification; contact your administrator')} onReadyChange={setReady}/></div>}
    </>}
    <label className="remember"><input type="checkbox" checked={remember} onChange={e=>setRemember(e.target.checked)} disabled={busy}/>{canonical?text('下次直接打开此工作台','Open this workspace on startup'):text('记住登录状态','Remember me')}</label>
    </>}
    <p className="error" role="alert">{loginErrorMessage(error,en)}</p>
    {status && <p className="hint" role="status">{status}</p>}
    <button className="login" type="submit" disabled={!loaded || busy || (method==='company' && (checking || (!!address.trim() && (configAddress!==address.trim() || (!canonical && !ready)))))}>{busy?text('请稍候…','Please wait…'):method==='company' && canonical?text('进入工作台登录','Sign in to workspace'):text('登录','Sign in')}</button>
    {method==='company' && <>
    <div className="server-settings"><button className="server-toggle" type="button" aria-expanded={expanded} aria-controls="server-fields" onClick={()=>setExpanded(!expanded)} disabled={busy}><span>{text('服务器地址','Server address')}</span><span aria-hidden="true">{expanded?'⌃':'⌄'}</span></button>
    <div id="server-fields" hidden={!expanded}><label className="sr-only" htmlFor="server-address">{text('服务器地址','Server address')}</label><input ref={addressRef} id="server-address" value={address} onChange={e=>setAddress(e.target.value)} onBlur={()=>{if(address.trim())void call('SaveLoginAddress',address.trim()).catch((err:unknown)=>setError(err));}} placeholder={text('IP 或域名:端口','IP or hostname:port')} autoComplete="off" spellCheck={false} maxLength={2048} disabled={busy}/><p className="hint">{text('地址自动保存，默认使用 HTTPS。内网 HTTP 请填写完整的 http:// 地址。','Address is saved automatically and defaults to HTTPS. For intranet HTTP, enter the full http:// address.')}</p></div>
    {!expanded && address && <p className="saved-address">{address}</p>}
    {checking && <p className="hint" role="status">{text('正在检查服务器…','Checking server…')}</p>}</div></>}
    {method==='cloud' && <p className="hint">{text('自动使用账号已有的模型权限与额度，无需填写 API Key。','Uses your existing model access and credit. No API key to copy.')}</p>}
    {bootstrap.configured && <button type="button" className="server-toggle" data-action="open-assistant" disabled={busy} onClick={()=>void openAssistant()}>{text('继续本机部署助手','Continue this device’s setup assistant')}</button>}
  </form></main></>;
}

createRoot(document.getElementById('login-root')!).render(<EmployeeLogin/>);
