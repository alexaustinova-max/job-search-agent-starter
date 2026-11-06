// Existing connected Chrome extension: review and explicitly approved preparation.
// No driver/transport/API and no submission operation.
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';

function resumeContext(vacancyId) {
  const manifest=JSON.parse(readFileSync(new URL('../cv/current-cv-manifest.json',import.meta.url),'utf8'));
  return {vacancyId,resumeTitles:Object.values(manifest.profiles||{}).map(cv=>cv.title_verified).filter(Boolean)};
}

export function preparationReasons(request) {
  const item=request?.candidate;const approval=request?.preparation_approval;
  const reasons=[];
  if(request?.backend!=='existing_chrome_extension'||request?.submission_allowed!==false)reasons.push('safe_extension_request_required');
  if(request?.status!=='review_authorized'||!item||!request.fingerprint)reasons.push('main_process_admission_missing');
  if(!approval||approval.source!=='user'||approval.action!=='prepare_without_submission')reasons.push('user_preparation_approval_missing');
  const digest=item?.letter?createHash('sha256').update(JSON.stringify(item.letter)).digest('hex'):null;
  if(!digest||digest!==item?.letter_sha256||approval?.letter_sha256!==digest)reasons.push('exact_letter_not_approved');
  if(approval?.cv_sha256!==item?.cv?.sha256||!item?.cv?.sha256)reasons.push('approved_cv_version_missing');
  if(!item?.hh_resume_id||!item?.hh_resume_title)reasons.push('exact_HH_resume_mapping_missing');
  let currentCv;
  try{currentCv=JSON.parse(readFileSync(new URL('../cv/current-cv-manifest.json',import.meta.url),'utf8')).profiles[item?.cv?.profile];}catch{}
  if(!currentCv?.hh_version||!item?.cv?.hh_version||currentCv.hh_version!==item.cv.hh_version
      ||currentCv.hh_resume_id!==item.hh_resume_id||currentCv.title_verified!==item.hh_resume_title
      ||item.hh_resume_id!==item.cv.hh_resume_id||item.hh_resume_title!==item.cv.hh_title
      ||approval?.hh_cv_version!==item.cv.hh_version)reasons.push('current_HH_resume_version_not_approved');
  if(!approval?.fact_validation_passed||!approval?.editorial_passed)reasons.push('letter_review_not_passed');
  if(approval?.request_fingerprint!==request?.fingerprint)reasons.push('approved_request_context_mismatch');
  const age=Date.now()-Date.parse(approval?.checked_at||'');
  if(!Number.isFinite(age)||age<0||age>900000)reasons.push('preparation_approval_not_current');
  if(item?.test_only||item?.model==='mock')reasons.push('mock_letter_forbidden');
  return reasons;
}

export async function verifyHhHistory(tab,ids) {
  if(!ids.length||ids.some(id=>!/^\d+$/.test(id)))throw new Error('invalid_history_ids');
  await tab.goto('https://hh.ru/applicant/negotiations');
  const result={checked:false,ids,found:[],matches:[],scopes:[],source:'visible_HH_history'};
  const read=async()=>await tab.playwright.evaluate(targets=>{
    const selected=document.querySelector('[role="tab"][aria-selected="true"]');
    const rows=[...document.querySelectorAll('[data-qa="negotiations-item"]')];
    const links=[...document.querySelectorAll('a[href]')].filter(e=>e.closest('[data-qa^="negotiations-item"]'));
    const found=links.map(e=>(e.href.match(/\/vacancy\/(\d+)(?:[/?#]|$)/)||[])[1]).filter(id=>targets.includes(id));
    const pages=[...document.querySelectorAll('[data-qa^="number-pages-"]')].map(e=>Number(e.innerText.trim())).filter(Number.isFinite);
    const matches=links.map(e=>({vacancy_id:(e.href.match(/\/vacancy\/(\d+)(?:[/?#]|$)/)||[])[1],
      text:e.closest('[data-qa="negotiations-item"]')?.innerText||e.innerText})).filter(e=>targets.includes(e.vacancy_id));
    return {scope:selected?.getAttribute('data-qa'),page:Number(document.querySelector('[data-qa*="number-pages-selected"]')?.innerText)||1,
      signature:links.map(e=>e.getAttribute('href')).join('|'),found:[...new Set(found)],pages,rows:rows.length,
      has_next:!!document.querySelector('[data-qa="number-pages-next"]'),
      matches,
      link_count:links.length,expected_count:Number((selected?.innerText.match(/\d+/)||[])[0])||0,
      login:!!document.querySelector('input[type="password"]'),
      protection:/captcha|подтвердите, что вы не робот|доступ ограничен/i.test(document.body?.innerText||'')};
  },ids);
  let precedingSignature=null;
  const settled=async scope=>{
    const end=Date.now()+7000;let last='',since=Date.now(),sample;
    do{
      sample=await read();if(sample.login||sample.protection)throw new Error('HH_history_unavailable');
      const signature=JSON.stringify([sample.scope,sample.page,sample.signature,sample.pages,sample.rows,sample.expected_count]);
      if(signature!==last){last=signature;since=Date.now();}
      if(sample.scope===scope&&Date.now()-since>=500&&(precedingSignature===null||sample.signature!==precedingSignature||!sample.rows&&!sample.expected_count))return sample;
      await new Promise(r=>setTimeout(r,150));
    }while(Date.now()<end);
    throw new Error('HH_history_scope_content_not_stable');
  };
  for(const scope of ['tab_filter_all','tab_filter_archived','tab_filter_deleted']){
    await tab.playwright.locator('[data-qa="'+scope+'"]').waitFor({state:'visible',timeoutMs:7000});
    if(await tab.playwright.locator('[data-qa="'+scope+'"]').getAttribute('aria-selected')!=='true')
      await tab.playwright.locator('[data-qa="'+scope+'"]').click();
    await tab.playwright.locator('[data-qa="'+scope+'"][aria-selected="true"]').waitFor({state:'visible',timeoutMs:7000});
    await tab.playwright.domSnapshot();
    let sample=await settled(scope);
    const total=Math.max(1,...sample.pages);if(total>30)throw new Error('HH_history_exceeds_bounded_check');
    if(!sample.link_count&&(sample.expected_count||total>1))throw new Error('HH_history_rows_not_readable');
    const checkedPages=[];
    for(let page=1;page<=total;page++){
      if(page!==sample.page){
        const previous=sample.signature;
        const control=sample.pages.includes(page)?'[data-qa~="number-pages-'+page+'"]':page===sample.page+1&&sample.has_next?'[data-qa="number-pages-next"]':null;
        if(!control)throw new Error('HH_history_page_control_not_rendered: '+page);
        await tab.playwright.locator(control).click();
        await tab.playwright.locator('[data-qa~="number-pages-'+page+'"][data-qa~="number-pages-selected"]').waitFor({state:'visible',timeoutMs:7000});
        const end=Date.now()+5000;
        do{sample=await read();if(sample.signature!==previous)break;await new Promise(r=>setTimeout(r,200));}while(Date.now()<end);
        if(sample.signature===previous)throw new Error('HH_history_content_not_updated');
      }
      if(sample.scope!==scope||sample.login||sample.protection)throw new Error('HH_history_scope_not_verified');
      result.found.push(...sample.found);result.matches.push(...sample.matches.map(m=>({...m,scope,page})));checkedPages.push(page);
    }
    result.scopes.push({scope,pages:checkedPages});
    precedingSignature=sample.signature;
  }
  result.found=[...new Set(result.found)];result.checked=true;result.checked_at=new Date().toISOString();
  return result;
}

export async function fillApprovedFields(tab,request,review,history) {
  const reasons=preparationReasons(request);const item=request.candidate;
  if(reasons.length)throw new Error(reasons.join('; '));
  if(!history?.checked||!history.ids?.includes(item.hh_id)||history.found?.includes(item.hh_id))throw new Error('HH_duplicate_or_history_unverified');
  if(history.source!=='visible_HH_history'||['tab_filter_all','tab_filter_archived','tab_filter_deleted'].some(scope=>!history.scopes?.some(s=>s.scope===scope)))
    throw new Error('HH_history_scope_incomplete');
  if(review.status!=='inspected'||review.questions_state!=='absence_confirmed')throw new Error('questions_require_confirmed_answers');
  // First supported preparation deliberately handles only reliably question-free forms.
  const before=await tab.playwright.evaluate(readFormDOM,resumeContext(item.hh_id));
  if(classifyForm(before,true).questions_state!=='absence_confirmed')throw new Error('questions_changed_before_fill');
  const radios=tab.playwright.getByRole('radio',{name:item.hh_resume_title,exact:true});
  if(await radios.count()!==1){
    const titles=Object.values(JSON.parse(readFileSync(new URL('../cv/current-cv-manifest.json',import.meta.url),'utf8')).profiles)
      .map(cv=>cv.title_verified).filter(Boolean);
    const currentTitle=await tab.playwright.evaluate(titles=>{
      const matches=[...document.querySelectorAll('button,[role="button"]')].map(e=>(e.innerText||'').trim())
        .filter(t=>titles.includes(t));
      return matches.length===1?matches[0]:null;
    },titles);
    if(!currentTitle)throw new Error('resume_chooser_not_unambiguous');
    await tab.playwright.getByRole('button',{name:currentTitle,exact:true}).click();
  }
  if(await radios.count()!==1)throw new Error('exact_resume_option_missing');
  const observedId=await radios.evaluate(e=>e.value);
  if(observedId!==item.hh_resume_id)throw new Error('HH_resume_id_mismatch');
  await radios.check();
  const letter=tab.playwright.locator('textarea[data-qa="vacancy-response-popup-form-letter-input"]');
  if(await letter.count()!==1)throw new Error('exact_letter_field_missing');
  await letter.fill(item.letter);
  const readback=await letter.evaluate(e=>e.value);
  if(readback!==item.letter)throw new Error('letter_readback_mismatch');
  const cv=await tab.playwright.evaluate(expected=>{
    const choices=[...document.querySelectorAll('input[type="radio"]')].filter(e=>e.checked&&e.value===expected.id);
    return choices.length===1;
  },{id:item.hh_resume_id,title:item.hh_resume_title});
  if(!cv)throw new Error('selected_HH_resume_readback_failed');
  const after=await tab.playwright.evaluate(readFormDOM,resumeContext(item.hh_id));
  if(classifyForm(after,true).questions_state!=='absence_confirmed')throw new Error('questions_changed_after_fill');
  return {form_filled:true,cv_verified:true,resume_id:item.hh_resume_id,cv_sha256:item.cv.sha256,
    hh_cv_version:item.cv.hh_version,
    letter_sha256:item.letter_sha256,letter_readback_verified:true,submission_allowed:false,submitted:false};
}
export function classifyForm(sample, stable) {
  const reasons = [];
  const found = sample.questions || [];
  if (sample.protection) reasons.push('hh_protection');
  if (sample.login_required) reasons.push('hh_login_required');
  if (sample.has_test) reasons.push('employer_test_requires_user');
  if (sample.visibility_change_required) reasons.push('resume_visibility_requires_user');
  if (sample.warning_count) reasons.push('form_warning_unresolved');
  if (!sample.expected_url) reasons.push('unexpected_url');
  if (!sample.form || !sample.cover_letter || !sample.submit_control || !sample.resume_control)
    reasons.push('core_form_elements_missing');
  if (!stable) reasons.push('meaningful_form_dom_not_stable');
  if (sample.busy) reasons.push('form_busy');
  if (!sample.coverage_complete) reasons.push('inaccessible_or_unclassified_form_elements');
  if (sample.question_controls?.length || sample.closed_sections?.length)
    reasons.push('question_controls_not_resolved');
  let state = found.length ? 'detected' : 'unknown';
  if (!found.length && sample.questions_heading) reasons.push('question_heading_without_readable_fields');
  // Placement outside FORM and lack of an ARIA target do not establish what HH means.
  if (!found.length && sample.question_view?.state==='present') reasons.push('active_questionnaire_without_readable_fields');
  const emptyView=sample.question_view?.state==='absent_confirmed'
    &&sample.question_view?.factory_shape_verified===true
    &&sample.question_view?.source==='active_HH_questionnaire_renderer';
  if (!found.length && sample.notice?.questions && !emptyView)
    reasons.push(sample.notice.visible===true?'employer_questions_announced_but_unreadable':'question_notice_meaning_not_confirmed');
  if (!reasons.length && !found.length) state = 'absence_confirmed';
  return {questions_state:state, questions_absence_confirmed:state==='absence_confirmed',
    status:state==='unknown'||reasons.length?'needs_review':'inspected',
    reasons:[...new Set(reasons)], questions:found, submission_allowed:false};
}

export function readFormDOM(context) {
  const vacancyId=typeof context==='object'?context.vacancyId:context;
  const resumeTitles=typeof context==='object'&&Array.isArray(context.resumeTitles)?context.resumeTitles:[];
  const norm=s=>(s||'').replace(/[\u200B-\u200D\uFEFF]/g,'').replace(/\s+/g,' ').trim();
  const visible=e=>{
    if(!e.getClientRects().length)return false;
    const rect=e.getBoundingClientRect?.();
    if(rect&&(rect.width<=0||rect.height<=0))return false;
    for(let node=e;node;node=node.parentElement){
      const style=getComputedStyle(node),box=node.getBoundingClientRect?.();
      if(node.hidden||node.getAttribute?.('aria-hidden')==='true'||style.display==='none'
          ||['hidden','collapse'].includes(style.visibility)||style.opacity==='0')return false;
      // HH keeps the warning text mounted in a zero-height collapsible container.
      if(box&&((['hidden','clip'].includes(style.overflowY||style.overflow)&&box.height<=0)
          ||(['hidden','clip'].includes(style.overflowX||style.overflow)&&box.width<=0)))return false;
    }
    return true;
  };
  const desc=e=>({tag:e.tagName,id:e.id||null,name:e.name||null,control_index:[...document.querySelectorAll('input,textarea,select')].indexOf(e),data_qa:e.getAttribute('data-qa'),role:e.getAttribute('role'),
    aria_expanded:e.getAttribute('aria-expanded'),aria_controls:e.getAttribute('aria-controls'),visible:visible(e)});
  const placeholder=s=>/^(писать тут|ваш ответ|ответ|type here)$/i.test(norm(s));
  const labelText=e=>{
    if(!e)return '';
    if(!document.createTreeWalker)return norm(e.textContent);
    const walker=document.createTreeWalker(e,NodeFilter.SHOW_TEXT);let node,text='';
    while(node=walker.nextNode()){
      if(node.parentElement?.closest('input,textarea,select,script,style')||placeholder(node.textContent))continue;
      text+=' '+node.textContent;
    }
    return norm(text);
  };
  const label=e=>norm(e.getAttribute('aria-label')||(e.getAttribute('aria-labelledby')||'').split(/\s+/)
    .filter(Boolean).map(id=>labelText(document.getElementById(id))).join(' ')||[...(e.labels||[])].map(labelText).join(' '));
  const questionContext=e=>{
    const value=norm(e.value),explicit=label(e);
    if(!['radio','checkbox'].includes(e.type)&&explicit&&explicit!==value&&!placeholder(explicit))return explicit;
    const group=e.closest('fieldset,[role="radiogroup"],[role="group"]');
    const legend=group?.querySelector('legend')?.textContent;if(norm(legend))return norm(legend);
    for(let node=e.parentElement,depth=0;node&&depth<5&&node.tagName!=='FORM';node=node.parentElement,depth++){
      const controls=[...(node.querySelectorAll?.('input,textarea,select')||[])].filter(c=>c.type!=='hidden');
      if(controls.some(c=>c!==e&&(!e.name||c.name!==e.name)))continue;
      const walker=document.createTreeWalker?.(node,NodeFilter.SHOW_TEXT);if(!walker)continue;
      let part,text='';while(part=walker.nextNode()){
        if(part.parentElement?.closest('input,textarea,select,button,script,style'))continue;
        if(placeholder(part.textContent))continue;
        if(value&&norm(part.textContent)===value)continue;
        const l=part.parentElement?.closest('label');if(l?.querySelector('input[type="radio"],input[type="checkbox"]'))continue;
        text+=' '+part.textContent;
      }
      text=norm(text);if(text.length>=8&&text.length<=1200&&!/^(писать тут|ваш ответ)$/i.test(text))return text;
    }
    return null;
  };
  const form=document.getElementById('RESPONSE_MODAL_FORM_ID');
  const notice=document.querySelector('[data-qa="title-description"]');
  const roots=form?[form]:[];
  const seenRoots=new Set(roots);
  for(let i=0;i<roots.length;i++)for(const e of [roots[i],...roots[i].querySelectorAll('*')]){
    if(e.shadowRoot&&!seenRoots.has(e.shadowRoot)){seenRoots.add(e.shadowRoot);roots.push(e.shadowRoot);}
    for(const attr of ['aria-controls','aria-owns'])for(const id of (e.getAttribute?.(attr)||'').split(/\s+/).filter(Boolean)){
      const target=document.getElementById(id);
      if(target&&!seenRoots.has(target)&&!form.contains(target)&&!target.contains(form)){
        seenRoots.add(target);roots.push(target);
      }
    }
  }
  // HTML form-associated controls can live outside the FORM subtree.
  const fields=[...new Set([...(form?.elements||[]),...roots.flatMap(r=>[...r.querySelectorAll('input,textarea,select')])])]
    .filter(e=>['INPUT','TEXTAREA','SELECT'].includes(e.tagName));
  const allFields=fields.map(e=>{
    const cv=e.type==='radio'&&/Head of Research|Product Lead|Head of CX Research/.test(label(e));
    const cover=/letter|cover/i.test((e.getAttribute('data-qa')||'')+' '+(e.getAttribute('name')||''));
    const group=e.closest('fieldset,[role="radiogroup"],[role="group"]');
    const groupLabel=group?norm(group.getAttribute('aria-label')||(group.getAttribute('aria-labelledby')||'').split(/\s+/)
      .map(id=>document.getElementById(id)?.textContent||'').join(' ')||group.querySelector('legend')?.textContent):'';
    return {...desc(e),type:e.type||null,cover_letter:cover,cv_choice:cv,
      prompt:cv?'[CV choice]':cover?'Сопроводительное письмо':groupLabel||questionContext(e),
      required:e.required||e.getAttribute('aria-required')==='true'||group?.getAttribute('aria-required')==='true'?true:
        /\*\s*$/.test(groupLabel||questionContext(e)||'')?true:
        e.getAttribute('aria-required')==='false'||group?.getAttribute('aria-required')==='false'||/необязательн|optional/i.test(groupLabel||questionContext(e)||'')?false:null,
      options:e.tagName==='SELECT'?[...e.options].map(o=>({text:o.label||o.text,value:o.value,disabled:o.disabled})):[],
      choice_label:!cv&&['radio','checkbox'].includes(e.type)?label(e):null};
  });
  const questions=allFields.filter(f=>f.type!=='hidden'&&!f.cover_letter&&!f.cv_choice);
  const custom=roots.flatMap(r=>[...r.querySelectorAll('[role="textbox"],[role="combobox"],[role="radiogroup"],[role="radio"],[role="checkbox"],[contenteditable="true"]')])
    .filter(e=>!fields.includes(e)&&!(e.getAttribute('role')==='radiogroup'&&e.querySelectorAll('input[type="radio"]').length
      &&!e.querySelector('[role="radio"]:not(input)')));
  const closed=roots.flatMap(r=>[...r.querySelectorAll('details:not([open]),[aria-expanded="false"]')]);
  const frames=roots.flatMap(r=>[...r.querySelectorAll('iframe')]);
  const questionControls=[...document.querySelectorAll('button,[role="button"],summary,a,[aria-controls]')].filter(e=>{
    // Observed HH alternate submit action, not a questionnaire expander.
    // Never click it; all actual questions remain subject to answer validation.
    if(e.getAttribute('data-qa')==='vacancy-response-link-no-questions')return false;
    const words=/вопрос|анкет|question|survey/i.test(norm(e.textContent)+' '+(e.getAttribute('data-qa')||''));
    const linked=(e.getAttribute('aria-controls')||'').split(/\s+/).some(id=>{
      const target=document.getElementById(id);return target&&(target===form||form?.contains(target));
    });
    return (words&&(form?.contains(e)||notice?.parentElement?.contains(e)))||linked;
  });
  const body=norm(document.body?.innerText);
  const buttons=[...(form||document).querySelectorAll('button,[role="button"]')];
  let hash=0;for(const c of norm(form?.textContent))hash=(Math.imul(hash,31)+c.charCodeAt(0))|0;
  const noticeText=norm(notice?.textContent);
  // HH's observed page template always prints vacancy.response.page.description.
  // Inspect only its already-loaded, pure conditional view factory, NEVER submit,
  // event handlers, Redux actions, network or candidate values. A changed factory
  // shape fails closed. Its empty branch is evidence in addition to DOM coverage.
  let questionView={state:'unknown',source:'active_HH_questionnaire_renderer'};
  const fiberKey=form&&Object.keys(form).find(k=>k.startsWith('__reactFiber$'));
  let fiber=fiberKey?form[fiberKey]:null;
  const pureQuestionView=/^\(\)=>[$\w]+&&\(0,[$\w]+\.jsx\)\([$\w]+,\{test:[$\w]+,vacancyId:[$\w]+\}\)$/;
  for(let depth=0;fiber&&depth<24;depth++,fiber=fiber.return){
    const props=fiber.memoizedProps;
    if(String(props?.vacancyId)!==String(vacancyId)||typeof props?.renderContent!=='function')continue;
    const source=String(props.renderContent);
    if(!pureQuestionView.test(source))break;
    try{
      const view=props.renderContent();
      if(view===undefined||view===null||view===false)
        questionView={state:'absent_confirmed',source:'active_HH_questionnaire_renderer',factory_shape_verified:true};
      else if(view&&typeof view==='object'&&view.props?.test)
        questionView={state:'present',source:'active_HH_questionnaire_renderer',factory_shape_verified:true};
    }catch(_){} // Unknown is never no questions.
    break;
  }
  const visibilityWarnings=form?[...form.querySelectorAll('[data-qa="hidden-resume-warning"]')]:[];
  const locationURL=new URL(location.href);
  return {url:locationURL.origin+locationURL.pathname+'?vacancyId='+vacancyId,
    ready:document.readyState, expected_url:locationURL.pathname==='/applicant/vacancy_response'&&
      locationURL.searchParams.get('vacancyId')===vacancyId&&/(^|\.)hh\.ru$/.test(locationURL.hostname),
    form:!!form&&visible(form), cover_letter:allFields.some(f=>f.cover_letter&&f.visible),
    submit_control:buttons.some(e=>visible(e)&&norm(e.textContent)==='Откликнуться'),
    resume_control:buttons.some(e=>visible(e)&&resumeTitles.includes(norm(e.textContent)))||allFields.some(f=>f.cv_choice),
    question_view:questionView,fields:allFields, questions, coverage_complete:!!form&&!custom.length&&!frames.length&&questions.every(q=>q.prompt),
    custom_controls:custom.map(desc),frames:frames.map(desc),closed_sections:closed.map(desc),question_controls:questionControls.map(desc),
    busy:roots.some(r=>r.querySelector('[aria-busy="true"],[role="progressbar"]')),
    questions_heading:/Ответьте на вопросы/i.test(body),
    notice:{present:!!notice,visible:!!notice&&visible(notice),inside_form:!!(notice&&form?.contains(notice)),questions:/необходимо ответить/i.test(noticeText),
      element:notice?desc(notice):null,closest_control:notice?.closest('button,a,summary,[role="button"]')?desc(notice.closest('button,a,summary,[role="button"]')):null,
      text:/^Для отклика необходимо ответить на несколько вопросов работодателя[.!]?$/.test(noticeText)?noticeText:notice?'[nonstandard notice text omitted]':null},
    protection:/подтвердите, что вы не робот|captcha|доступ ограничен|проверка безопасности/i.test(body),
    login_required:/\/login/.test(locationURL.pathname)||!!document.querySelector('input[type="password"]'),
    has_test:/тестовое задание|мини-кейс|пройти тест/i.test(body),
    visibility_change_required:visibilityWarnings.some(e=>visible(e)&&/поменяйте видимость резюме/i.test(norm(e.textContent))),
    resume_visibility_evidence:visibilityWarnings.map(e=>({...desc(e),active:visible(e),
      height:e.getBoundingClientRect?.().height??null,
      overflow:getComputedStyle(e).overflow,max_height:getComputedStyle(e).maxHeight})),
    warning_count:roots.flatMap(r=>[...r.querySelectorAll('[role="alert"]')]).filter(e=>visible(e)&&norm(e.textContent)).length,
    meaningful_text_checksum:hash,field_values_omitted:true};
}

export function layout(windows) {
  return windows.map(w=>({id:w.id,focused:w.focused,state:w.state,left:w.left,top:w.top,width:w.width,height:w.height,
    tabs:(w.tabs||[]).map(t=>({id:t.id,windowId:t.windowId,index:t.index,active:t.active,pinned:t.pinned,groupId:t.groupId,url:t.url}))
      .sort((a,b)=>a.index-b.index)})).sort((a,b)=>a.id-b.id);
}

export async function reviewForm(browser,{vacancyId=null,maxWaitMs=12000,stableForMs=1600,pollMs=350,historyIds=null,historyOnly=false,prepareRequest=null}={}) {
  if(!/^\d+$/.test(vacancyId))throw new Error('invalid HH ID');
  if(prepareRequest){const reasons=preparationReasons(prepareRequest);if(reasons.length)
    return {status:'needs_review',reasons,form_filled:false,submission_allowed:false};}
  const management=await browser.capabilities.get('management');
  const before=await management.windows.getAll({populate:true});
  const originalWindows=new Set(before.map(w=>w.id));
  const originalTabs=new Set(before.flatMap(w=>(w.tabs||[]).map(t=>String(t.id))));
  const report={vacancy_id:vacancyId,form_read:false,status:'needs_review',questions_state:'unknown',
    questions_absence_confirmed:false,submission_allowed:false};
  let ownTab=null,ownWindow=null;
  try {
    ownTab=await browser.tabs.new();report.own_tab_id=ownTab.id;
    if(originalTabs.has(String(ownTab.id)))throw new Error('tab_ownership_not_confirmed');
    const created=await management.windows.create({tabId:Number(ownTab.id),focused:false,type:'normal'});
    ownWindow=created.id;report.own_window_id=ownWindow;
    const owned=await management.tabs.query({windowId:ownWindow});
    if(originalWindows.has(ownWindow)||owned.length!==1||String(owned[0].id)!==String(ownTab.id))
      throw new Error('window_isolation_not_confirmed');
    report.isolation_confirmed=true;
    if(historyIds||prepareRequest){
      report.history=await verifyHhHistory(ownTab,historyIds||[vacancyId]);
      if(historyOnly)return report;
      if(report.history.found.includes(vacancyId)){report.reasons=['already_applied_in_HH'];return report;}
    }
    await ownTab.goto('https://hh.ru/applicant/vacancy_response?vacancyId='+vacancyId);
    const started=Date.now();let previous=null,stableSince=null,sample=null,stable=false,samples=0;
    while(Date.now()-started<=maxWaitMs){
      sample=await ownTab.playwright.evaluate(readFormDOM,resumeContext(vacancyId));samples++;
      if(sample.protection||sample.login_required)break;
      // readyState is diagnostic only; stability covers anchors, fields, notices and controls.
      const {ready,...meaningful}=sample;const signature=JSON.stringify(meaningful);
      const anchors=sample.form&&sample.cover_letter&&sample.submit_control&&sample.resume_control&&!sample.busy;
      if(anchors&&signature===previous){if(stableSince!==null&&Date.now()-stableSince>=stableForMs){stable=true;break;}}
      else stableSince=anchors?Date.now():null;
      previous=signature;
      await new Promise(resolve=>setTimeout(resolve,pollMs));
    }
    report.form_read=!!sample?.form;report.dom=sample;report.stable=stable;report.samples=samples;
    report.elapsed_ms=Date.now()-started;
    Object.assign(report,classifyForm(sample||{},stable));
    if(prepareRequest)report.preparation=await fillApprovedFields(ownTab,prepareRequest,report,report.history);
  } catch(error){report.error=String(error.message||error);}
  finally {
    if(ownTab){
      try {
        if(originalTabs.has(String(ownTab.id)))throw new Error('refusing_to_close_original_tab');
        const current=await management.tabs.get(Number(ownTab.id));
        if(ownWindow!==null&&current.windowId!==ownWindow)throw new Error('own_tab_moved; cleanup_stopped');
        if(ownWindow!==null){const tabs=await management.tabs.query({windowId:ownWindow});
          if(tabs.some(t=>String(t.id)!==String(ownTab.id)))throw new Error('foreign_tabs_in_own_window; cleanup_stopped');}
        await ownTab.close();
      }catch(error){report.cleanup_error=String(error.message||error);}
    }
    const after=await management.windows.getAll({populate:true});
    report.own_tab_removed=!ownTab||!after.some(w=>(w.tabs||[]).some(t=>String(t.id)===String(ownTab.id)));
    report.own_window_removed=ownWindow===null||!after.some(w=>w.id===ownWindow);
    report.cleanup_confirmed=report.own_tab_removed&&report.own_window_removed;
    report.user_state_unchanged=JSON.stringify(layout(before))===JSON.stringify(layout(after.filter(w=>originalWindows.has(w.id))));
  }
  return report;
}

// Request comes from application_batch.py --extension-review; blocked requests never open Chrome.
export async function reviewPreparedForm(browser,request,options={}) {
  if(request?.backend!=='existing_chrome_extension'||request.mode!=='review_only'||
      request.status!=='review_authorized'||!request.candidate||!request.fingerprint)
    return {status:'needs_review',reason:'current_approved_candidate_missing',form_read:false,form_filled:false,
      submission_allowed:false,mode:'review_only'};
  const report=await reviewForm(browser,{...options,vacancyId:request.candidate.hh_id,prepareRequest:null});
  return {...report,checked_at:new Date().toISOString(),request_fingerprint:request.fingerprint,
    mode:'review_only',form_filled:false,submission_allowed:false};
}

export async function prepareApprovedForm(browser,request,options={}) {
  const reasons=preparationReasons(request);
  if(reasons.length)return {status:'needs_review',reasons,form_filled:false,submission_allowed:false};
  const report=await reviewForm(browser,{...options,vacancyId:request.candidate.hh_id,
    historyIds:[request.candidate.hh_id],historyOnly:false,prepareRequest:request});
  return {...report,checked_at:new Date().toISOString(),request_fingerprint:request.fingerprint,
    mode:'prepare_form_only',form_filled:report.preparation?.form_filled===true,
    submission_allowed:false,submitted:false};
}
