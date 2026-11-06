// Private stdio child of Python, not a server. Only owns its dedicated Chrome profile.
import {createRequire} from 'node:module';
import {createInterface} from 'node:readline';
import {resolve} from 'node:path';
import {spawnSync} from 'node:child_process';
import {writeFileSync} from 'node:fs';
import {verifyHhHistory} from './browser/extension-form-review.mjs';
const require=createRequire(import.meta.url);
let context=null,playwright=null,windowId=null;
const pages=new Map();
let statusFile=null;
function stage(phase){
  // Diagnostic output is not a browser safety gate. A transient filesystem error
  // must not abort every remaining vacancy before its form can be inspected.
  if(statusFile)try{writeFileSync(statusFile,JSON.stringify({phase,time:new Date().toISOString()}),{mode:0o600});}
  catch(_){} // No field values, credentials or exception prose are logged.
}
async function boundedRead(read,ms=10000){
  let timer;try{return await Promise.race([read,new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('HH_read_timeout_no_page_action')),ms);})]);}
  finally{clearTimeout(timer);}
}
async function cleanup(){
  if(context)await context.close();
  context=null;pages.clear();
}
process.once('SIGTERM',()=>{cleanup().finally(()=>process.exit(0));});
async function identify(page){const c=await context.newCDPSession(page);try{return (await c.send('Target.getTargetInfo')).targetInfo.targetId;}finally{await c.detach();}}
async function action(m){
  if(m.op==='start'){
    if(context)throw Error('profile_already_open');
    const profile=resolve(m.profile),marker='--user-data-dir='+profile;
    const listing=spawnSync('/bin/ps',['-axo','command='],{encoding:'utf8'});
    if(listing.status!==0)throw Error('profile_process_check_failed');
    if(listing.stdout.split('\n').some(line=>line.startsWith('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome ')
      &&line.split(/\s+/).includes(marker)))throw Error('agent_profile_in_use_do_not_launch_or_modify_preferences');
    statusFile=resolve(profile,'..','browser-worker-status.json');stage('start');playwright=require(m.module);
    context=await playwright.chromium.launchPersistentContext(profile,{
      channel:'chrome',headless:!!m.headless,chromiumSandbox:true,
      ignoreDefaultArgs:['--password-store=basic','--use-mock-keychain'],
      viewport:{width:1280,height:900},timeout:30000});
    const page=context.pages()[0]||await context.newPage(),id=await identify(page);pages.set(id,page);
    const c=await context.newCDPSession(page);
    try{windowId=String((await c.send('Browser.getWindowForTarget')).windowId);}finally{await c.detach();}
    return {window_id:windowId,profile:resolve(m.profile),headless:m.headless,owned_process:true,process_arch:process.arch,tabs:[{id,url:page.url()}]};
  }
  if(!context)throw Error('agent_profile_not_open');
  if(m.op==='open'){
    const url=new URL(m.url);if(url.protocol!=='https:'||!/(^|\.)hh\.ru$/.test(url.hostname))throw Error('only_official_HH_pages');
    stage('open_get_page');let id=m.id,page=id?pages.get(id):null;
    if(!page){
      const idle=[...pages].find(([,p])=>!p.isClosed()&&p.url()==='about:blank');
      if(idle){[id,page]=idle;}else{page=await context.newPage();id=await identify(page);pages.set(id,page);}
    }
    await page.bringToFront(); // Only a tab in this process/profile; no user's Chrome target is connected.
    stage('open_navigate');await page.goto(m.url,{waitUntil:'domcontentloaded',timeout:30000});stage('open_complete');return {id};
  }
  if(m.op==='tabs')return [...pages].filter(([,p])=>!p.isClosed()).map(([id,p])=>({id,url:p.url()}));
  if(m.op==='close'){
    await cleanup();return {closed:true};
  }
  const page=pages.get(m.id);if(!page||page.isClosed())throw Error('owned_agent_tab_missing');
  if(m.op==='history'){
    const facade={goto:url=>page.goto(url,{waitUntil:'domcontentloaded',timeout:30000}),playwright:{
      evaluate:(...args)=>page.evaluate(...args),domSnapshot:()=>page.locator('body').innerText({timeout:7000}),
      locator:selector=>{const locator=page.locator(selector);return {
        waitFor:({state,timeoutMs})=>locator.waitFor({state,timeout:timeoutMs}),
        getAttribute:name=>locator.getAttribute(name),click:async()=>{try{return await locator.click({timeout:7000});}
          catch(error){const message=String(error.message||'');
            const cause=message.includes('intercepts pointer events')?'pointer_intercepted':message.includes('not visible')?'not_visible':message.includes('not enabled')?'disabled':message.includes('not attached')?'detached':'not_actionable_within_bound';
            throw Error('HH_history_control_unavailable: '+selector+'; '+cause);}}};}}};
    return await verifyHhHistory(facade,m.ids);
  }
  if(m.op==='js'){stage('evaluate');return await boundedRead(page.evaluate(m.code),15000);}
  if(m.op==='refresh'){await page.reload({waitUntil:'domcontentloaded',timeout:30000});return true;}
  if(m.op==='close_tab'){await page.close();pages.delete(m.id);return true;}
  if(m.op==='profile_settings')return await page.evaluate(()=>({url:location.href,title:document.title,settings_api:typeof chrome.settingsPrivate?.getPref==='function',settings_error:/не удается получить настройки|не удаётся получить настройки|unable to (?:read|load) (?:your )?preferences|preferences cannot be read/i.test(document.body?.innerText||'')}));
  if(m.op==='profile_settings_open'){await page.goto('chrome://settings/appearance',{waitUntil:'domcontentloaded',timeout:10000});return true;}
  if(m.op==='profile_pref')return await page.evaluate(async desired=>{if(typeof chrome.settingsPrivate?.getPref!=='function')throw Error('Chrome_settings_API_not_available');const read=()=>new Promise(r=>chrome.settingsPrivate.getPref('browser.show_home_button',p=>r({key:p.key,type:p.type,value:p.value})));if(typeof desired==='boolean')await new Promise((r,reject)=>chrome.settingsPrivate.setPref('browser.show_home_button',desired,'',ok=>ok?r():reject(Error('Chrome_preference_write_failed'))));return await read();},m.value);
  if(m.op==='login_state')return await boundedRead(page.evaluate(()=>{const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);let node,text='';
    while((node=walker.nextNode())&&text.length<200000)if(!node.parentElement?.closest('script,style,noscript,input,textarea,select'))text+=' '+node.textContent;
    return {url:location.origin+location.pathname,
    login:!!document.querySelector('input[type="password"],input[data-qa="account-signup-email"]'),
    authenticated:!!document.querySelector('[data-qa="mainmenu_applicantProfile"],[data-qa="mainmenu_myResumes"],[data-qa="negotiations-item"],[data-qa="tab_filter_all"],a[href="/applicant/resumes"]'),
    protection:/captcha|подтвердите, что вы не робот|доступ ограничен/i.test(text)};}));
  throw Error('unsupported_agent_browser_operation');
}
for await(const line of createInterface({input:process.stdin,crlfDelay:Infinity})){
  let m;try{m=JSON.parse(line);const result=await action(m);process.stdout.write(JSON.stringify({id:m.seq,ok:true,result})+'\n');}
  catch(e){process.stdout.write(JSON.stringify({id:m?.seq,ok:false,error:String(e.message||e).split('\n')[0].slice(0,240)})+'\n');}
}
await cleanup();
