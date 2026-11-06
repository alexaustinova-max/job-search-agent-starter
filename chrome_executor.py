"""Local Chrome Apple Events executor. DOM UI only; no cookies, private API or stealth."""
import json
import re
from pathlib import Path
import subprocess
import time
from urllib.parse import urlparse,parse_qs,urlencode,urlunparse

import pipeline as p


class Chrome:
    window_id=None
    managed=False

    def __init__(self,*,preserve_focus=False):
        self.window_id=None;self.managed=False
        self.preserve_focus=preserve_focus;self._focus_anchor=None

    def active_window(self,expected=None):
        """Read Chrome's own focus state; never activate an app or change window order."""
        if self.preserve_focus:
            observed=self.osa('''tell application "Google Chrome"
if (count windows) is 0 then error "chrome_has_no_window"
return (id of front window as text) & ":" & (frontmost as text) & ":" & (visible of front window as text)
end tell''').split(':')
            if len(observed)!=3 or not observed[0].isdigit() or observed[2]!='true':
                raise ValueError('Chrome visible window state not verified')
            anchor=(observed[0],observed[1])
            if self._focus_anchor is None:self._focus_anchor=anchor
            if anchor!=self._focus_anchor:
                raise ValueError('Chrome focus/window changed; expected '+repr(self._focus_anchor)+', observed '+repr(anchor)+'; stop without restoring focus')
            if expected is not None and observed[0]!=str(expected):raise ValueError('Chrome front window changed; stop without restoring focus')
            return observed[0]
        current=self.osa('''tell application "Google Chrome"
if frontmost is not true then error "chrome_not_frontmost"
if (count windows) is 0 then error "chrome_has_no_window"
if visible of front window is not true then error "chrome_front_window_not_visible"
return id of front window as text
end tell''')
        if not current.isdigit():raise ValueError('Chrome returned an invalid front window ID')
        if expected is not None and current!=str(expected):
            raise ValueError('Chrome front window changed; stop without restoring focus')
        return current

    def managed_window(self,existing_window_id=None):
        # A fresh window gives provable ownership; legacy windows/tabs are not adopted.
        self.owned_tabs=set();self.bootstrap_tabs=set();self.work_tab=None;self.history_tab=None
        # Reuse only a persisted agent window. Do not launch/focus Chrome to discover one.
        if existing_window_id and str(existing_window_id).isdigit():
            running=subprocess.run(['/usr/bin/pgrep','-x','Google Chrome'],capture_output=True,timeout=3).returncode==0
            if running and self.osa('tell application "Google Chrome" to return exists window id '+str(existing_window_id))=='true':
                self.active_window()
                self.window_id=str(existing_window_id);self.managed=True;return self.window_id
        self.active_window()
        self.managed=True
        try:self.agent_window()
        finally:
            if self.window_id:self.owned_tabs.update(t['id'] for t in self.tabs())
            self.bootstrap_tabs=set(self.owned_tabs)
        return self.window_id

    def cleanup_item(self):
        if not self.managed:return
        for tid in list(self.owned_tabs-self.bootstrap_tabs):
            self.close_owned_tab(tid)
        self.work_tab=None;self.history_tab=None

    def close_owned_tab(self,tid):
        if not self.managed or str(tid) not in self.owned_tabs:return False
        # Never close a tab moved out of the owned window, or a user's tab.
        self.osa('tell application "Google Chrome"\nif exists window id '+self.window_id+' then\nrepeat with t in tabs of window id '+self.window_id+'\nif (id of t as text) is "'+str(tid)+'" then\nclose t\nexit repeat\nend if\nend repeat\nend if\nend tell')
        self.owned_tabs.discard(str(tid));return True

    def finish(self):
        if not self.managed:return False
        self.cleanup_item()
        remaining={t['id'] for t in self.tabs()}
        if not remaining.issubset(self.owned_tabs):return False
        self.osa('tell application "Google Chrome"\nif exists window id '+self.window_id+' then close window id '+self.window_id+'\nend tell')
        self.owned_tabs.clear();self.window_id=None;return True

    def layout(self):
        """Ownership/focus evidence only: window geometry and tab IDs, never page contents."""
        return self.osa('''tell application "Google Chrome"
set rows to ""
repeat with w in windows
set rows to rows & (id of w as text) & ":" & (index of w as text) & ":" & (active tab index of w as text) & ":" & (bounds of w as text) & ":"
repeat with t in tabs of w
set rows to rows & (id of t as text) & ","
end repeat
set rows to rows & ASCII character 10
end repeat
return rows
end tell''')

    def agent_window(self,window_id=None):
        """Create/reuse only the agent's own window; restore the existing front window."""
        wanted=str(window_id) if window_id is not None else '0'
        if not wanted.isdigit():raise ValueError('invalid agent window ID')
        previous_window=self.active_window()
        result=self.osa('''tell application "Google Chrome"
set previousId to 0
if (count windows) > 0 then set previousId to id of front window
set agentId to 0
repeat with w in windows
if (id of w as text) is "'''+wanted+'''" then set agentId to id of w
end repeat
if agentId is 0 then
set agentWindow to make new window with properties {visible:false}
set agentId to id of agentWindow
end if
if previousId is not 0 and previousId is not agentId then
set index of window id previousId to 1
end if
return (agentId as text) & ":" & (id of front window as text) & ":" & (previousId as text)
end tell''')
        agent,front,previous=result.split(':')
        self.window_id=agent  # Retain ownership even if the subsequent focus guard stops work.
        if previous!='0' and front!=previous:raise ValueError('Chrome agent window cannot preserve working window focus')
        self.active_window(previous_window)
        self.window_id=agent
        return agent

    def refresh(self,tab_id):
        if not str(tab_id).isdigit():raise ValueError('invalid tab')
        self.osa('tell application "Google Chrome"\nrepeat with w in windows\nrepeat with t in tabs of w\nif (id of t as text) is "'+str(tab_id)+'" then reload t\nend repeat\nend repeat\nend tell')

    def osa(self,script):
        raise ValueError('personal_Chrome_Apple_Events_disabled_use_owned_profile')
        result=subprocess.run(['/usr/bin/osascript','-e',script],capture_output=True,text=True,timeout=20)
        if result.returncode:
            raise ValueError('Chrome Apple Events failed: '+result.stderr.strip()[-600:])
        return result.stdout.strip()

    def tabs(self):
        raw=self.osa('''tell application "Google Chrome"
set rows to ""
repeat with w in windows
repeat with t in tabs of w
set rows to rows & (id of t as text) & ASCII character 9 & (URL of t as text) & ASCII character 10
end repeat
end repeat
return rows
end tell''')
        tabs=[{'id':line.split('\t',1)[0],'url':line.split('\t',1)[1]} for line in raw.splitlines() if '\t' in line]
        if self.window_id:
            ids=self.osa('tell application "Google Chrome" to get id of every tab of window id '+self.window_id).split(', ')
            tabs=[t for t in tabs if t['id'] in ids]
        return tabs

    def js(self,tab_id,code):
        if not str(tab_id).isdigit(): raise ValueError('invalid Chrome tab ID')
        if self.preserve_focus:
            if not self.managed or str(tab_id) not in self.owned_tabs:raise ValueError('native_control_requires_owned_tab')
            self.active_window()
        result=self.osa('''tell application "Google Chrome"
repeat with w in windows
repeat with t in tabs of w
if (id of t as text) is '''+json.dumps(str(tab_id))+''' then
return execute t javascript '''+json.dumps(code,ensure_ascii=False)+'''
end if
end repeat
end repeat
return "TAB_NOT_FOUND"
end tell''')
        if self.preserve_focus:self.active_window()
        if result=='TAB_NOT_FOUND': raise ValueError('Chrome tab no longer exists')
        if not result: raise ValueError('Chrome returned no DOM result')
        return json.loads(result)

    def open(self,url):
        parsed=urlparse(url)
        if parsed.scheme!='https' or not (parsed.hostname=='hh.ru' or (parsed.hostname or '').endswith('.hh.ru')):
            raise ValueError('only official HTTPS HH pages are allowed')
        if self.managed:
            role='history_tab' if parsed.path=='/applicant/negotiations' else 'work_tab'
            existing=getattr(self,role,None)
            available={t['id']:t['url'] for t in self.tabs()}
            if existing and existing in available:
                if available[existing]==url:return existing
                self.osa('tell application "Google Chrome" to set URL of tab id '+existing+' of window id '+self.window_id+' to '+json.dumps(url))
                return existing
            tid=self.osa('tell application "Google Chrome"\nset t to make new tab at end of tabs of window id '+self.window_id+' with properties {URL:'+json.dumps(url)+'}\nreturn id of t\nend tell')
            self.owned_tabs.add(tid);setattr(self,role,tid);return tid
        for tab in self.tabs():
            seen=urlparse(tab['url'])
            if (seen.hostname=='hh.ru' or (seen.hostname or '').endswith('.hh.ru')) and seen.path==parsed.path and seen.query==parsed.query:
                return tab['id']
        target='window id '+self.window_id if self.window_id else 'front window'
        return self.osa('tell application "Google Chrome"\nset t to make new tab at end of tabs of '+target+' with properties {URL:'+json.dumps(url)+'}\nreturn id of t\nend tell')

    def snapshot(self,tab_id):
        return self.js(tab_id,r"""JSON.stringify((()=>{
const visible=e=>!!(e.getClientRects().length)&&getComputedStyle(e).visibility!=='hidden';
const attrs=e=>({qa:e.getAttribute('data-qa'),name:e.name||'',id:e.id||'',role:e.getAttribute('role'),control_index:[...document.querySelectorAll('input,textarea,select')].indexOf(e)});
const labelled=e=>(e?.getAttribute('aria-labelledby')||'').split(/\s+/).filter(Boolean).map(id=>document.getElementById(id)?.innerText||'').join(' ').trim();
const group=e=>e.closest('fieldset,[role=radiogroup],[role=group]');
const groupPrompt=e=>{const g=group(e);return g?(labelled(g)||g.getAttribute('aria-label')||g.querySelector('legend')?.innerText||'').trim():'';};
const label=e=>(e.getAttribute('aria-label')||labelled(e)||[...(e.labels||[])].map(l=>l.innerText).join(' ')).trim();
const obligation=e=>{const g=group(e);if(e.required||e.getAttribute('aria-required')==='true'||g?.getAttribute('aria-required')==='true')return true;
if(e.getAttribute('aria-required')==='false'||g?.getAttribute('aria-required')==='false')return false;return null;};
const questionToggle=e=>/вопрос|анкет|question|survey|assessment|тест/i.test((e.innerText||'')+' '+(e.getAttribute('data-qa')||''));
const hiddenControls=[...document.querySelectorAll('input,textarea,select')].filter(e=>!visible(e)&&!['hidden','radio'].includes(e.type)&&!e.disabled);
const closedQuestions=[...document.querySelectorAll('[aria-expanded=false],details:not([open])')].filter(questionToggle);
const uncheckedToggles=[...document.querySelectorAll('button,[role=button],a')].filter(visible).filter(questionToggle).filter(e=>e.getAttribute('aria-expanded')!=='true');
return {url:location.href,title:document.title,ready:document.readyState,
question_scan:{complete:document.readyState==='complete'&&hiddenControls.length===0&&closedQuestions.length===0&&uncheckedToggles.length===0&&document.querySelectorAll('[data-qa="vacancy-response-submit-popup"]').length===1,
hidden_controls:hiddenControls.length,closed_question_blocks:closedQuestions.length,unchecked_question_controls:uncheckedToggles.length,
invalid_required:[...document.querySelectorAll('input:required,textarea:required,select:required')].filter(visible).filter(e=>!e.validity.valid).length},
warnings:[...document.querySelectorAll('[role=alert],.error')].filter(visible).map(e=>e.innerText.trim()).filter(Boolean),
text:(document.body?.innerText||'').slice(0,22000),
buttons:[...document.querySelectorAll('button,[role=button]')].filter(visible).map(e=>({...attrs(e),text:e.innerText,disabled:e.disabled||false,type:e.type||''})),
links:[...document.querySelectorAll('a[href]')].filter(visible).map(e=>({...attrs(e),text:e.innerText,url:e.href})),
fields:[...document.querySelectorAll('input,textarea,select')].filter(visible).map(e=>({...attrs(e),tag:e.tagName,type:e.type,
max_length:Number.isInteger(e.maxLength)&&e.maxLength>=0?e.maxLength:null,
label:label(e),question_prompt:['radio','checkbox'].includes(e.type)?groupPrompt(e):(label(e)||groupPrompt(e)),
required:obligation(e),disabled:e.disabled||false,
group_key:group(e)?'group:'+([...document.querySelectorAll('fieldset,[role=radiogroup],[role=group]')].indexOf(group(e))):e.name?'name:'+e.name:'',
options:e.tagName==='SELECT'?[...e.options].map(o=>({text:o.label||o.text,value:o.value,disabled:o.disabled,selected:o.selected})):[],
placeholder:e.placeholder||'',value:e.type==='password'?'':e.value,checked:e.checked}))};})())""")


    def review_form(self,tab_id,vacancy_id):
        """Reuse the existing extension's pure DOM reader/classifier over native Chrome.

        Only the transport changes; unknown notices/linked controls still block preparation.
        """
        root=getattr(self,'root',p.ROOT)
        source=(root/'browser/extension-form-review.mjs').read_text()
        profiles=json.loads((root/'cv/current-cv-manifest.json').read_text()).get('profiles',{})
        context={'vacancyId':vacancy_id,'resumeTitles':[cv['title_verified'] for cv in profiles.values() if cv.get('title_verified')]}
        functions=source[source.index('export function classifyForm('):source.index('export function layout(')]
        functions=functions.replace('export function ','function ')
        end=time.monotonic()+12;previous=None;since=None
        while True:
            stable_time=since is not None and time.monotonic()-since>=1.6
            code='JSON.stringify((()=>{'+functions+'const sample=readFormDOM('+json.dumps(context)+');const {ready,...meaningful}=sample;const signature=JSON.stringify(meaningful);const stable=signature==='+json.dumps(previous)+'&&'+json.dumps(stable_time)+';return {sample,signature,stable,review:classifyForm(sample,stable)};})())'
            result=self.js(tab_id,code)
            if result['signature']!=previous:since=time.monotonic();previous=result['signature']
            if result['stable'] or time.monotonic()>=end:return result
            time.sleep(.35)


    @staticmethod
    def page_diagnostic(state,stage):
        """Only route, public page title and fixed markers; never body/field values."""
        location=urlparse(state.get('url',''))
        path=location.path
        if not re.fullmatch(r'/(?:applicant/(?:vacancy_response|negotiations|resumes|profile/me)|vacancy/\d+|account/(?:login|captcha)|login|captcha|oauth/authorize)?/?',path):
            path='/[redacted-path]'
        vacancy_id=parse_qs(location.query).get('vacancyId',[])
        query=urlencode({'vacancyId':vacancy_id[0]}) if vacancy_id and vacancy_id[0].isdigit() else ''
        url=urlunparse((location.scheme,location.hostname or '',path,'',query,''))
        title=' '.join(state.get('title','').split())
        public_titles=r'HeadHunter|hh\.ru|Вход|Авторизация|Отклик на вакансию|Ошибка|Error|Доступ ограничен|Access Denied|404(?: Not Found)?|50[23](?: Bad Gateway| Service Unavailable)?|Just a moment\.{0,3}|Security check'
        title_safe=not title or re.fullmatch(public_titles,title,re.I) is not None
        text=' '.join(state.get('text','').casefold().split())
        protection=any(marker in text for marker in ('подтвердите, что вы не робот','captcha','доступ ограничен','проверка безопасности','checking your browser'))
        login=('/login' in location.path or any(f.get('type')=='password' for f in state.get('fields',[]))
               or any(marker in text for marker in ('вход в личный кабинет','войти в аккаунт','войти на hh.ru')))
        errors=[marker for marker in ('вакансия в архиве','вакансия не найдена','страница не найдена','что-то пошло не так','bad gateway','service unavailable') if marker in text]
        resume='резюме для отклика' in text
        category=('protection' if protection else 'login' if login else 'error' if errors or location.scheme=='chrome-error'
                  else 'loading' if state.get('ready')!='complete' or not text else 'response_form' if resume
                  else 'unexpected_form' if state.get('fields') else 'unexpected_page')
        return {'waiting_for':stage,'url':url,'title':title if title_safe else '[redacted: nonstandard title]',
                'title_redacted':not title_safe,'document_readyState':state.get('ready'),
                'content_kind':category,'error_markers':errors,'resume_heading_normalized':resume,
                'resume_heading_literal':'Резюме для отклика' in state.get('text',''),
                'questions_heading':'ответьте на вопросы' in text,
                'questions_notice':'необходимо ответить' in text}

    def wait(self,tab_id,predicate,timeout=15,*,stage='unspecified'):
        end=time.monotonic()+timeout
        while True:
            state=self.snapshot(tab_id)
            if predicate(state):return state
            if time.monotonic()>=end:
                diagnostic=self.page_diagnostic(state,stage)
                error=ValueError('HH UI did not reach expected state: '+json.dumps(diagnostic,ensure_ascii=False))
                error.diagnostic=diagnostic
                raise error
            time.sleep(.4)


CV_TITLES={}


class OwnedChrome(Chrome):
    """Existing DOM executor over a private Playwright/Chrome process, no user-window access."""
    independent_profile=True

    def __init__(self,root=p.ROOT,*,headless=None):
        super().__init__();self.root=Path(root).resolve()
        self.config=json.loads((self.root/'browser-config.json').read_text())
        __import__('portable_runtime').require(self.root,'browser')
        self.profile=(self.root/self.config['profile_dir']).resolve()
        if not self.profile.is_relative_to(self.root/'var') or self.profile==self.root/'var':
            raise ValueError('agent_profile_must_be_separate_project_directory')
        self.profile.mkdir(parents=True,exist_ok=True);self.profile.chmod(0o700)
        self.headless=self.config.get('headless',True) if headless is None else headless
        self.process=None;self.seq=0;self.owned_tabs=set();self.bootstrap_tabs=set();self.transport_failed=False;self.profile_lock=None
        self.work_tab=None;self.history_tab=None;self.proof={}

    def call(self,op,**values):
        if self.transport_failed:raise ValueError('agent_browser_transport_failed_no_retry')
        if self.process is None:
            import fcntl
            lock_path=self.profile/'.job-search-agent.lock'
            self.profile_lock=lock_path.open('a+');lock_path.chmod(0o600)
            try:fcntl.flock(self.profile_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                self.profile_lock.close();self.profile_lock=None
                raise ValueError('agent_profile_busy_in_another_process') from None
            command=[self.config['node'],str(self.root/'chrome_worker.mjs')]
            if self.config.get('native_arch'):
                if self.config['native_arch'] not in ('arm64','x86_64'):raise ValueError('unsupported_browser_runtime_architecture')
                command=['/usr/bin/arch','-'+self.config['native_arch'],*command]
            self.process=subprocess.Popen(command,
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,bufsize=1)
        self.seq+=1;self.process.stdin.write(json.dumps({'seq':self.seq,'op':op,**values},ensure_ascii=False)+'\n');self.process.stdin.flush()
        import select
        if not select.select([self.process.stdout],[],[],45)[0]:
            self.transport_failed=True
            raise ValueError('agent_browser_transport_timeout_no_retry: '+op)
        line=self.process.stdout.readline()
        if not line:raise ValueError('agent_browser_worker_stopped')
        response=json.loads(line)
        if response.get('id')!=self.seq:raise ValueError('agent_browser_response_mismatch')
        if response.get('ok') is not True:raise ValueError('agent_browser: '+response.get('error','unknown'))
        return response['result']

    def managed_window(self,existing_window_id=None):
        if self.window_id:return self.window_id
        self.proof=self.call('start',profile=str(self.profile),module=self.config['playwright_module'],headless=self.headless)
        if self.proof['profile']!=str(self.profile) or not self.proof['owned_process']:raise ValueError('agent_process_ownership_not_verified')
        if self.config.get('native_arch') and self.proof.get('process_arch')!=self.config['native_arch']:
            raise ValueError('agent_browser_runtime_architecture_not_verified')
        self.window_id=self.proof['window_id'];self.managed=True
        self.owned_tabs={t['id'] for t in self.proof['tabs']};self.bootstrap_tabs=set(self.owned_tabs)
        return self.window_id

    def active_window(self,expected=None):return self.managed_window()
    def layout(self):return json.dumps(self.tabs(),sort_keys=True) if self.window_id else 'closed_owned_profile'
    def tabs(self):return self.call('tabs') if self.window_id else []

    def open(self,url):
        role='history_tab' if urlparse(url).path=='/applicant/negotiations' else 'work_tab'
        previous=getattr(self,role,None)
        result=self.call('open',url=url,id=previous);self.owned_tabs.add(result['id']);setattr(self,role,result['id'])
        return result['id']

    def js(self,tab_id,code):
        if tab_id not in self.owned_tabs:raise ValueError('only_owned_agent_tabs')
        value=self.call('js',id=tab_id,code=code)
        return json.loads(value) if isinstance(value,str) else value

    def refresh(self,tab_id):return self.call('refresh',id=tab_id)
    def close_owned_tab(self,tid):
        if tid not in self.owned_tabs:return False
        result=self.call('close_tab',id=tid);self.owned_tabs.discard(tid);return result

    def finish(self):
        closed=False
        try:
            if self.window_id and not self.transport_failed:closed=self.call('close')['closed']
        finally:
            if self.process:
                self.process.stdin.close()
                try:self.process.wait(timeout=8)
                except subprocess.TimeoutExpired:self.process.terminate();self.process.wait(timeout=5)
            self.process=None;self.window_id=None;self.managed=False;self.owned_tabs.clear()
            if self.profile_lock:
                self.profile_lock.close();self.profile_lock=None
        return closed


def standalone_chrome(root=p.ROOT,*,headless=None):
    path=Path(root)/'browser-config.json'
    if not path.exists() or json.loads(path.read_text()).get('backend')!='owned_playwright_chrome':
        raise ValueError('configure_owned_browser_backend')
    return OwnedChrome(root,headless=headless)


class ChromeExecutor:
    """Local DOM form preparation and one-shot delivery, invoked by an approved batch only."""
    supports_submission=True
    def __init__(self,root=p.ROOT,chrome=None,*,preparation_only=False,state=None):
        self.root=Path(root); self.chrome=chrome or standalone_chrome(root); self.sessions={}
        self.cv_titles={key:entry.get('title_verified','') for key,entry in json.loads((self.root/'cv/current-cv-manifest.json').read_text()).get('profiles',{}).items()}
        self.supports_submission=not preparation_only;self.state=state

    def cleanup_item(self):
        self.chrome.cleanup_item()
        self.sessions.clear()

    def guard(self,state):
        host=urlparse(state['url']).hostname or ''
        if host!='hh.ru' and not host.endswith('.hh.ru'):raise ValueError('unexpected non-HH page; stop')
        text=state['text'].casefold()
        if any(x in text for x in ('подтвердите, что вы не робот','captcha','доступ ограничен','проверка безопасности')):
            raise ValueError('HH protection encountered; stop without bypass')
        if any(f.get('type')=='password' for f in state['fields']) or '/login' in state['url']:
            raise ValueError('authorized HH session is unavailable')

    def history(self,vid):
        if getattr(self.chrome,'independent_profile',False):
            tab=self.chrome.open('https://hh.ru/applicant/negotiations')
            checked=self.chrome.call('history',id=tab,ids=[vid])
            if checked.get('checked') is not True:raise ValueError('complete_HH_history_not_verified')
            return {**checked,'page_url':'https://hh.ru/applicant/negotiations',
                'found':vid in checked['found'],'text':'\n'.join(m['text'] for m in checked.get('matches',[]) if m['vacancy_id']==vid)}
        tab=self.chrome.open('https://hh.ru/applicant/negotiations')
        self.chrome.refresh(tab)
        state=self.chrome.wait(tab,lambda s:'Отклики' in s['text'] and len(s['links'])>5,timeout=30,stage='hh_history')
        self.guard(state)
        card=self.chrome.js(tab,"""JSON.stringify((()=>{const id="""+json.dumps(vid)+""";
const a=[...document.querySelectorAll('a[href]')].find(a=>new URL(a.href).pathname==='/vacancy/'+id);
if(!a)return {found:false};
const e=a.parentElement?.parentElement?.parentElement;
return {found:true,url:a.href,text:e?.innerText||a.innerText};})())""")
        return {'page_url':state['url'],**card}


    def inspect(self,item):
        vid=item['hh_id']
        if not vid.isdigit(): raise ValueError('invalid HH vacancy ID')
        tab=self.chrome.open(item['url'])
        state=self.chrome.wait(tab,lambda s:'/vacancy/'+vid in s['url'] and len(s['text'])>100,stage='vacancy_page')
        self.guard(state)
        if 'Вакансия в архиве' in state['text'] or 'Вакансия не найдена' in state['text']:
            raise ValueError('vacancy_closed')
        history=self.history(vid)
        responded='Вы откликнулись' in ' '.join(state['text'].split()) or 'Отклик другим резюме' in state['text']
        if history['found'] or responded:
            return {'vacancy_id':vid,'history_checked':True,'already_applied':True,
                    'evidence':history,'vacancy_status':'already_responded','questions':[],'has_test':False}
        # Navigate to HH's existing response form; never click a one-click vacancy response control.
        parsed=urlparse(state['url'])
        form_url=parsed.scheme+'://'+parsed.netloc+'/applicant/vacancy_response?vacancyId='+vid
        form_tab=self.chrome.open(form_url)
        form=self.chrome.wait(form_tab,lambda s:'Резюме для отклика' in ' '.join(s['text'].split()) or 'Войти' in s['text'],stage='response_form')
        self.guard(form)
        # HH initially collapses the letter and resume choices on its response page.
        if not any(f.get('tag')=='TEXTAREA' and 'letter' in (f.get('qa') or '') for f in form['fields']) and any(b.get('qa')=='vacancy-response-letter-toggle' for b in form['buttons']):
            self.chrome.js(form_tab,'''JSON.stringify((()=>{document.querySelector('[data-qa="vacancy-response-letter-toggle"]').click();return true;})())''')
            form=self.chrome.wait(form_tab,lambda s:any(f.get('tag')=='TEXTAREA' for f in s['fields']),stage='cover_letter_field')
        if not any(f.get('type')=='radio' for f in form['fields']):
            self.chrome.js(form_tab,'''JSON.stringify((()=>{const b=[...document.querySelectorAll('[role=button]')].filter(e=>e.getClientRects().length&&/Head of CX Research|Head of Research|Product Lead/.test(e.innerText));if(b.length!==1)return false;b[0].click();return true;})())''')
            form=self.chrome.wait(form_tab,lambda s:any(f.get('type')=='radio' for f in s['fields']),stage='resume_choices')
        if (not self.supports_submission or getattr(self.chrome,'independent_profile',False)) and hasattr(self.chrome,'review_form'):
            form['canonical_review']=self.chrome.review_form(form_tab,vid)
        controls=[f for f in form['fields'] if f['tag'] in ('TEXTAREA','INPUT','SELECT') and f.get('type') not in ('radio','checkbox','password','hidden','submit','button')]
        letter=[f for f in controls if any(x in ((f.get('qa') or '')+' '+(f.get('name') or '')+' '+(f.get('label') or '')+' '+f.get('placeholder','')).casefold()
                for x in ('сопровод','cover','letter'))]
        if len(letter)!=1: raise ValueError('letter_field_not_unambiguously_identified')
        questions=self.form_questions(form,letter[0])
        reasons=self.question_review(form,questions)
        self.sessions[vid]={'tab':form_tab,'letter_field':letter[0],'form':form}
        return {'vacancy_id':vid,'history_checked':True,'already_applied':False,'resume_id':None,
                'questions':questions,'questions_checked':not reasons,
                'questions_absence_confirmed':not reasons and not questions,
                'status':'needs_review' if reasons else 'form_inspected','reasons':reasons,
                'has_test':any(x in form['text'].casefold() for x in ('тестовое задание','мини-кейс','пройти тест'))}

    @staticmethod
    def field_identity(field):
        for key in ('id','qa','name'):
            if field.get(key):return (key,field[key],field.get('tag'),field.get('type'))
        return tuple(field.get(k,'') for k in ('tag','type','control_index','label','placeholder'))

    @classmethod
    def form_questions(cls,form,letter):
        fields=form['fields'];canonical=form.get('canonical_review')
        if canonical:
            metadata={f['control_index']:f for f in canonical['sample']['fields'] if f.get('control_index',-1)>=0}
            fields=[{**f,'question_prompt':metadata[f['control_index']].get('prompt'),
                     'required':metadata[f['control_index']].get('required'),
                     'cover_letter':metadata[f['control_index']].get('cover_letter'),
                     'cv_choice':metadata[f['control_index']].get('cv_choice')} for f in fields if f.get('control_index') in metadata]
        controls=[f for f in fields if f['tag'] in ('TEXTAREA','INPUT','SELECT') and f.get('type') not in ('radio','checkbox','password','hidden','submit','button')]
        other=[f for f in controls if not f.get('cover_letter') and cls.field_identity(f)!=cls.field_identity(letter)]
        questions=[{'prompt':f.get('question_prompt') or f.get('label') or 'unread_question',
                    'type':f.get('type') or f['tag'].casefold(),'required':f.get('required'),
                    'options':f.get('options',[]),'field':f} for f in other]
        groups={}
        for f in fields:
            if f.get('type') in ('radio','checkbox') and not f.get('cv_choice'):
                key=(f.get('type'),f.get('group_key') or f.get('name') or cls.field_identity(f))
                if key not in groups:
                    groups[key]={'prompt':f.get('question_prompt') or 'unread_choice_question',
                        'type':f['type'],'required':f.get('required'),'options':[],'field':f,'fields':[]}
                    questions.append(groups[key])
                q=groups[key];q['fields'].append(f)
                if f.get('required') is True:q['required']=True
                q['options'].append({'text':f.get('label',''),'value':f.get('value'),
                                     'disabled':f.get('disabled',False)})
        questions.sort(key=lambda q:fields.index(q['field']))
        return questions


    @staticmethod
    def question_review(form,questions,answered_indices=()):
        """Do not guess HH-specific toggles, prompts from placeholders, or optionality."""
        reasons=[];scan=form.get('question_scan',{})
        canonical=form.get('canonical_review')
        if canonical:
            reasons.extend(canonical['review']['reasons'])
            if sum(len(q.get('fields',[])) or 1 for q in questions)!=len(canonical['review']['questions']):reasons.append('form_question_readers_disagree')
            if canonical['review']['status']!='inspected':reasons.append('canonical_question_review_incomplete')
        else:
            if scan.get('closed_question_blocks') or scan.get('unchecked_question_controls'):
                reasons.append('additional_questions_control_not_verified_or_not_expanded')
            if scan.get('hidden_controls'):reasons.append('question_fields_hidden_or_unread')
            if scan.get('complete') is not True:reasons.append('full_question_scan_not_confirmed')
            text=' '.join(form.get('text','').casefold().split())
            if not questions and ('необходимо ответить' in text or 'ответьте на вопросы' in text):
                reasons.append('employer_questions_indicated_but_not_read')
        for index,q in enumerate(questions):
            if q['prompt'] in ('unread_question','unread_choice_question'):
                reasons.append('question_text_not_associated_with_field')
            if q['required'] is None and index not in answered_indices:
                reasons.append('question_requiredness_unknown: '+q['prompt'])
            if q['type'] in ('select-one','select-multiple','select','radio','checkbox') and (
                    not q['options'] or any(not o.get('text') or o.get('value') is None for o in q['options'])):
                reasons.append('question_options_not_fully_read: '+q['prompt'])
        return list(dict.fromkeys(reasons))

    @classmethod
    def question_signature(cls,question):
        return (cls.field_identity(question['field']),question.get('prompt'),question.get('type'),
                question.get('required'),[{k:o.get(k) for k in ('text','value','disabled')} for o in question.get('options',[])])

    def checked_form(self,item,snapshot,confirmed):
        """Read back the entire form; certify absence only after a complete DOM survey."""
        from application_batch import standard_answers,READINESS_VERSION
        session=self.sessions[item['hh_id']];initial=session['inspection']
        questions=self.form_questions(snapshot,session['letter_field'])
        answers,unknown=standard_answers(questions,confirmed,item['hh_id'])
        same_questions=([self.question_signature(q) for q in questions]==
                        [self.question_signature(q) for q in initial['questions']])
        verified=[]
        for answer in answers:
            question=questions[answer['question_index']]
            if 'selected_values' in answer:
                actual=([f['value'] for f in question.get('fields',[]) if f.get('checked')]
                    if question['type'] in ('radio','checkbox') else [o['value'] for o in question['options'] if o.get('selected')])
                valid=sorted(actual)==sorted(answer['selected_values'])
            else:valid=question['field'].get('value')==answer['answer']
            if valid:verified.append(answer)
        scan=snapshot.get('question_scan',{})
        location=urlparse(snapshot['url'])
        question_issues=self.question_review(snapshot,questions,{a['question_index'] for a in verified})
        complete=(not question_issues and same_questions and location.path=='/applicant/vacancy_response'
                  and parse_qs(location.query).get('vacancyId')==[item['hh_id']])
        expected=self.cv_titles[item['cv']['profile']]
        cv_verified=any(f.get('type')=='radio' and f.get('checked') and f.get('label','').strip()==expected
                        and f.get('value')==session['prepared']['resume_id'] for f in snapshot['fields'])
        cv_verified=cv_verified or (not any(f.get('type')=='radio' and f.get('checked') for f in snapshot['fields'])
                                   and any(b.get('text','').strip()==expected for b in snapshot['buttons']))
        canonical=snapshot.get('canonical_review')
        warnings=(['active_form_warning'] if canonical['sample']['warning_count'] else []) if canonical else list(snapshot.get('warnings',['warning_check_missing']))
        if scan.get('invalid_required')!=0:warnings.append('required_fields_not_validated')
        text=snapshot['text'].casefold()
        return {**initial,**session['prepared'],'readiness_version':READINESS_VERSION,'checked_at':p.now(),
                'form_filled':any(f.get('value')==item['letter'] for f in snapshot['fields']),
                'questions':questions,'unknown_questions':unknown,'questions_checked':complete,
                'questions_absence_confirmed':complete and not questions,'answers_verified':verified,
                'cv_verified':cv_verified,'cv_sha256':item['cv']['sha256'],
                'job_material_sha256':item['job_material_sha256'],
                'warnings':warnings,'has_test':any(x in text for x in ('тестовое задание','мини-кейс','пройти тест')),
                'visibility_change_required':canonical['sample']['visibility_change_required'] if canonical else 'поменяйте видимость резюме' in text,
                'employer_questions_notice':canonical['review']['status']!='inspected' if canonical else 'необходимо ответить' in text,
                'status':'needs_review' if question_issues or not same_questions else 'form_prepared_not_submitted',
                'reasons':question_issues+([] if same_questions else ['questions_changed_after_inspection']),
                'submission_performed':False,'url':snapshot['url']}


    def field_js(self,field,value):
        # Target an observed field by exact UI identity. Native setter + input/change support React forms.
        return """JSON.stringify((()=>{const f="""+json.dumps(field,ensure_ascii=False)+", value="+json.dumps(value,ensure_ascii=False)+""";
const visible=e=>!!e.getClientRects().length;
const list=[...document.querySelectorAll('input,textarea,select')].filter(visible).filter(e=>
f.id?e.id===f.id:f.qa?e.getAttribute('data-qa')===f.qa:f.name?e.name===f.name:e.placeholder===f.placeholder);
if(list.length!==1)return {ok:false,reason:'ambiguous_field'};
const e=list[0]; if(e.tagName!=='TEXTAREA'&&e.type!=='text'&&e.type!=='email'&&e.type!=='tel'&&e.type!=='number')return {ok:false,reason:'unsupported_field'};
const proto=e.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
Object.getOwnPropertyDescriptor(proto,'value').set.call(e,value);
e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));
return {ok:e.value===value&&(!e.validity||e.validity.valid)};})())"""

    def prepare(self,item,confirmed_answers,*,resolve_questions=True):
        from application_batch import standard_answers,resolve_form_questions
        result=self.inspect(item)
        if result['already_applied']:return {**result,'status':'already_applied_no_form_opened','form_filled':False}
        if result['has_test']:return {**result,'status':'exception_test','form_filled':False}
        # Unknown optionality is not an unread question. A sourced answer may handle it;
        # every other inspection blocker still stops before any field is changed.
        structural=[r for r in result.get('reasons',[]) if not r.startswith('question_requiredness_unknown: ')]
        if structural or result.get('status')=='needs_review' and not result.get('reasons'):
            return {**result,'form_filled':False}
        if self.state is not None:
            confirmed_answers=resolve_form_questions(self.state,result['questions'],confirmed_answers,item['hh_id'],self.root,allow_ai=resolve_questions,selected_cv=item['cv'])
        answers,unknown=standard_answers(result['questions'],confirmed_answers,item['hh_id'])
        if unknown:return {**result,'status':'needs_review','unknown_questions':unknown,
                           # The question and controls were read; optionality is
                           # still unknown, not an unread block. The resolver may
                           # find sourced answers, then prepare()/readback must
                           # validate them before any readiness or submit gate.
                           'questions_checked':True,
                           'reasons':['unknown_questions_require_confirmed_answers'],'form_filled':False}
        pending=self.question_review(self.sessions[item['hh_id']]['form'],result['questions'],
            {a['question_index'] for a in answers})
        if pending:return {**result,'status':'needs_review','reasons':pending,'form_filled':False}
        session=self.sessions[item['hh_id']]; tab=session['tab']
        expected=self.cv_titles.get(item['cv']['profile'])
        if not expected:raise ValueError('profile_HH_title_not_verified')
        # Only select a visibly identified CV; never choose the first option or another profile.
        expected_id=item.get('hh_resume_id') or item['cv'].get('hh_resume_id')
        selected=self.chrome.js(tab,"""JSON.stringify((()=>{const title="""+json.dumps(expected,ensure_ascii=False)+", expectedId="+json.dumps(expected_id)+""";
const radios=[...document.querySelectorAll('input[type=radio]')].filter(e=>[...(e.labels||[])].some(l=>l.innerText.trim()===title));
if(radios.length===1&&(!expectedId||radios[0].value===expectedId)){radios[0].click();return {selected:radios[0].checked,resume_id:radios[0].value};}
return {selected:false,reason:'resume_choice_requires_explicit_mapping'};})())""")
        if not selected.get('selected') or not selected.get('resume_id'):raise ValueError('real_HH_resume_not_verified')
        if expected_id and selected['resume_id']!=expected_id:raise ValueError('exact_HH_resume_id_mismatch')
        for answer in answers:
            question=result['questions'][answer['question_index']]
            script=self.choice_js(question,answer['selected_values']) if 'selected_values' in answer else self.field_js(question['field'],answer['answer'])
            if not self.chrome.js(tab,script).get('ok'):
                raise ValueError('standard_answer_not_verified')
        if not self.chrome.js(tab,self.field_js(session['letter_field'],item['letter'])).get('ok'):
            raise ValueError('letter_fill_not_verified')
        readback=self.chrome.snapshot(tab); self.guard(readback)
        if (not self.supports_submission or getattr(self.chrome,'independent_profile',False)) and hasattr(self.chrome,'review_form'):
            review=self.chrome.review_form(tab,item['hh_id'])
            readback=self.chrome.snapshot(tab);self.guard(readback)
            readback['canonical_review']=review
        if not any(f.get('value')==item['letter'] for f in readback['fields']):raise ValueError('letter_readback_failed')
        session['prepared']={'resume_id':selected['resume_id'],'letter_sha256':p.digest(item['letter']),
                             'hh_cv_version':item['cv'].get('hh_version')}
        session['inspection']=result;session['confirmed_answers']=confirmed_answers
        return self.checked_form(item,readback,confirmed_answers)


    def choice_js(self,question,values):
        return r'''JSON.stringify((()=>{const q='''+json.dumps(question,ensure_ascii=False)+', wanted='+json.dumps(values,ensure_ascii=False)+r''';
const f=q.field;const all=[...document.querySelectorAll('input,select')];
const controls=all.filter(e=>f.id?e.id===f.id:f.name?e.name===f.name:f.qa?e.getAttribute('data-qa')===f.qa:false);
if(q.type==='radio'||q.type==='checkbox'){
 const inputs=controls.filter(e=>e.type===q.type);if(inputs.length!==q.options.length)return {ok:false};
 if(inputs.some(e=>!q.options.some(o=>o.value===e.value))||wanted.some(v=>!inputs.some(e=>e.value===v&&!e.disabled)))return {ok:false};
 for(const e of inputs){const checked=wanted.includes(e.value);if(e.checked!==checked)e.click();}
 return {ok:inputs.every(e=>e.checked===wanted.includes(e.value))};
}
if(controls.length!==1||controls[0].tagName!=='SELECT')return {ok:false};const e=controls[0];
if(wanted.length>1&&!e.multiple||wanted.some(v=>![...e.options].some(o=>o.value===v&&!o.disabled)))return {ok:false};
for(const o of e.options)o.selected=wanted.includes(o.value);
e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));
return {ok:JSON.stringify([...e.selectedOptions].map(o=>o.value).sort())===JSON.stringify([...wanted].sort())};})())'''

    def submit_once(self,item,answers):
        __import__('portable_runtime').require(self.root,'dispatch')
        dispatch_path=self.root/'dispatch-config.json'
        if dispatch_path.exists() and json.loads(dispatch_path.read_text()).get('paused'):
            raise ValueError('all_application_submission_paused_by_user')
        if not self.supports_submission:raise ValueError('control_preparation_cannot_submit')
        session=self.sessions[item['hh_id']]
        if session.get('clicked'):raise ValueError('automatic_repeat_forbidden')
        if self.history(item['hh_id']).get('found'):raise ValueError('already_applied_before_click')
        state=self.chrome.snapshot(session['tab']);self.guard(state)
        if not any(f.get('value')==item['letter'] for f in state['fields']):raise ValueError('letter_changed_before_click')
        if getattr(self.chrome,'independent_profile',False):
            state['canonical_review']=self.chrome.review_form(session['tab'],item['hh_id'])
        # This path cannot bypass editorial admission, even when called outside a batch.
        import sqlite3
        from contextlib import closing
        from types import SimpleNamespace
        from application_batch import readiness
        current=self.checked_form(item,state,session['confirmed_answers'])
        if not current.get('cv_verified') or (item.get('hh_resume_id') and current.get('resume_id')!=item['hh_resume_id']):raise ValueError('needs_review: resume_changed_before_click')
        with closing(sqlite3.connect('file:'+str(self.root/'var/jobs.sqlite3')+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row
            check=readiness(SimpleNamespace(db=db),item,current,self.root)
        if not check['ready']:raise ValueError('needs_review: '+'; '.join(check['reasons']))
        # Re-read the emergency stop after history/readback/quality checks, immediately
        # before the only operation that can submit. A live batch may be paused mid-form.
        __import__('portable_runtime').require(self.root,'dispatch')
        if dispatch_path.exists() and json.loads(dispatch_path.read_text()).get('paused'):
            raise ValueError('all_application_submission_paused_by_user')
        session['clicked']=True  # No retry even if transport fails while clicking.
        click=self.chrome.js(session['tab'],"""JSON.stringify((()=>{const b=[...document.querySelectorAll('[data-qa="vacancy-response-submit-popup"]')].filter(e=>e.getClientRects().length);if(b.length!==1||b[0].disabled||b[0].innerText.trim()!=='Откликнуться')return {clicked:false};b[0].click();return {clicked:true};})())""")
        if not click.get('clicked'):raise ValueError('submit_button_not_unambiguous; no_retry')
        time.sleep(2)
        state=self.chrome.snapshot(session['tab']);self.guard(state)
        return {**session['prepared'],'post_click':{'url':state['url'],'text':state['text'][:10000],
                'fields':state['fields'],'buttons':state['buttons']}}


    def verify_history(self,item,sent):
        history=self.history(item['hh_id'])
        if not history.get('found'):
            # A mounted, hidden captcha button in the raw DOM is not an active
            # protection challenge. guard() already checks the actual page after
            # the click. Do not turn unrelated markup/field values into global
            # stop codes or leak them into exception summaries. Never retry.
            # Body text alone also cannot establish an active visibility error.
            raise ValueError('unconfirmed_send_or_followup_question: history_missing')
        return {'vacancy_id':item['hh_id'],'resume_id':sent['resume_id'],
                'letter_sha256':sent['letter_sha256'],'history_verified':True,
                'evidence':history,'hh_status':history['text'],'verified_at':p.now()}
