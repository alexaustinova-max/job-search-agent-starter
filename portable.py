#!/usr/bin/env python3
"""Starter CLI. No external action occurs during import, init, doctor or demo."""
import argparse
from contextlib import contextmanager, ExitStack, closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import sys
import tempfile
from unittest.mock import patch
import uuid
from zoneinfo import ZoneInfo

import portable_runtime as rt

ROOT=Path(__file__).resolve().parent


def save(root,name,value):
    path=rt.inside(root,name);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');path.chmod(0o600)


def initialize(root=ROOT):
    import pipeline as p
    cfg=rt.settings(root)
    if not cfg['installation_id']:
        cfg['installation_id']=uuid.uuid4().hex;save(root,'portable-config.json',cfg)
    rt.identity(root)
    state=p.State(Path(root)/'var/jobs.sqlite3')
    try:
        row=state.db.execute("SELECT value FROM meta WHERE key='starter:installation-id'").fetchone()
        if row and json.loads(row['value'])!=cfg['installation_id']:
            raise ValueError('database_belongs_to_another_installation')
        state.db.execute("INSERT OR IGNORE INTO meta VALUES('starter:installation-id',?)",(json.dumps(cfg['installation_id']),))
        state.db.commit()
    finally:state.close()
    return {'initialized':True,'installation_id':cfg['installation_id'],'database':'var/jobs.sqlite3','integrations_unchanged':True}


def doctor(root=ROOT):
    import sqlite3
    result={'project':str(Path(root).resolve()),'python':sys.executable,'policy':rt.settings(root),
        'browser_profile':str(rt.inside(root,rt.read(root,'browser-config.json')['profile_dir'])),
        'database':str(rt.inside(root,'var/jobs.sqlite3')),'network_calls':0,'secret_access':False}
    try:
        result['installation_id']=rt.identity(root);rt.validate_profile(root);result['profile']='valid'
    except (ValueError,KeyError,OSError) as exc:result['profile_error']=str(exc)
    result['configuration_sha256']={name:hashlib.sha256((Path(root)/name).read_bytes()).hexdigest()
        for name in ['portable-config.json','pipeline-config.hh.json','career-facts.json','browser-config.json',
            'llm-config.json','dispatch-config.json','delivery-config.json']}
    db=Path(root)/'var/jobs.sqlite3'
    if db.exists():
        with closing(sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True)) as con:
            result['database_counts']={name:con.execute('SELECT count(*) FROM '+name).fetchone()[0] for name in ['jobs','applications','events']}
            row=con.execute("SELECT value FROM meta WHERE key='starter:installation-id'").fetchone()
            result['database_identity_matches']=bool(row and json.loads(row[0])==rt.settings(root)['installation_id'])
    result['secrets']='macOS Keychain isolated by installation ID and project path; not read by doctor'
    result['environment_key_present']=bool(os.environ.get('OPENAI_API_KEY'))
    return result


@contextmanager
def offline_boundary():
    """Fail immediately if a regression tries any network, secret or child-process action."""
    with ExitStack() as stack:
        for target in ['socket.socket','socket.create_connection','urllib.request.urlopen',
            'urllib.request.OpenerDirector.open','subprocess.Popen','ctypes.CDLL','smtplib.SMTP','smtplib.SMTP_SSL']:
            stack.enter_context(patch(target,side_effect=AssertionError('offline_external_action_forbidden:'+target)))
        yield


def synthetic_profile(root,multiple=False):
    """Write fictional fixtures only into the caller-provided temporary project."""
    root=Path(root)
    previous_id=rt.settings(root).get('installation_id') if (root/'portable-config.json').exists() else None
    for name in ['portable-config.json','pipeline-config.hh.json','pipeline-config.json','career-facts.json',
        'llm-config.json','browser-config.json','dispatch-config.json','delivery-config.json']:
        shutil.copyfile(ROOT/name,root/name)
    cfg=rt.read(root,'pipeline-config.hh.json')
    candidate=cfg['candidate'];candidate.update(name='Демо Кандидат',email='demo@example.invalid',english_level='B2')
    candidate['cvs']={'MAIN':'cv/demo.txt'}
    candidate['profiles']={'MAIN':{'title':'Демонстрационный аналитик','title_patterns':['аналитик|analyst'],
        'capabilities':['product_analytics','market_analysis'],'priority':0}}
    candidate['capability_patterns']={}
    if multiple:
        candidate['cvs']['SECOND']='cv/second.txt'
        candidate['profiles']['SECOND']={'title':'Демонстрационный координатор','title_patterns':['координатор'],
            'capabilities':['team_management'],'priority':1}
    cfg['sources'][0]['searches']=[{'id':key+'-query','profile':key,'text':pr['title']} for key,pr in candidate['profiles'].items()]
    cfg['filters'].update(min_monthly_rub=60000,desired_monthly_rub=100000,cities=['Демоград'],allowed_work_formats=['remote','on-site'])
    facts={'english_level':'B2','facts':[
        {'id':'demo_metrics','organization':'Вымышленная компания Альфа','text':'Анализ продуктовых метрик.',
         'source':'cv/demo.txt','verified':True,'capabilities':['product_analytics'],'scope':'fictional',
         'letter_text':'В вымышленной компании Альфа я анализировал продуктовые метрики и готовил аналитические материалы для обсуждения решений с командой.'},
        {'id':'demo_market','organization':'Вымышленная компания Бета','text':'Анализ потребностей рынка.',
         'source':'cv/demo.txt','verified':True,'capabilities':['market_analysis'],'scope':'fictional',
         'letter_text':'В вымышленной компании Бета я анализировал потребности рынка и систематизировал наблюдения, чтобы команда могла проверить исходные предположения.'}]}
    for name in ['pipeline-config.hh.json','pipeline-config.json']:save(root,name,cfg)
    save(root,'career-facts.json',facts)
    (root/'cv').mkdir(exist_ok=True);(root/'cv/demo.txt').write_text('Вымышленное резюме: анализ продуктовых метрик и потребностей рынка.\n')
    if multiple:(root/'cv/second.txt').write_text('Вымышленное дополнительное резюме.\n')
    save(root,'cv/current-cv-manifest.json',{'profiles':{}})
    policy=rt.settings(root)
    policy.update(installation_id=previous_id,profile_confirmed=False,hh_enabled=False,browser_enabled=False,ai_enabled=False,dispatch_enabled=False,mail_enabled=False)
    save(root,'portable-config.json',policy)
    initialize(root)
    return cfg


def demo(root=ROOT):
    import pipeline as p
    import daily_prepare as daily
    import application_batch as ab
    with tempfile.TemporaryDirectory(prefix='jsa-demo-') as temporary, offline_boundary():
        testroot=Path(temporary);cfg=synthetic_profile(testroot)
        class FixtureHH:
            collection={'queries':[]}
            def collect(self,source,prefilter=None):
                return [{'id':'900000001','name':'Аналитик продукта — демонстрация','employer':{'name':'Вымышленный работодатель'},
                    'alternate_url':'https://hh.ru/vacancy/900000001','description':'Обязанности:\nАнализ продуктовых метрик и подготовка аналитических материалов для команды.\nАнализ потребностей рынка и проверка исходных предположений.\nТребования:\nОпыт анализа продуктовых метрик.\nОпыт анализа потребностей рынка.',
                    'area':{'name':'Демоград'},'work_format':[{'id':'REMOTE','name':'Удалённо'}],
                    'salary':{'from':80000,'to':120000,'currency':'RUR','gross':False,'mode':{'id':'MONTH'}},'archived':False,
                    '_detail_state':'full','published_at':'2026-01-01T00:00:00+0000'}],True
        first=p.run(cfg,root=testroot,hh_client=FixtureHH())
        second=p.run(cfg,root=testroot,hh_client=FixtureHH())
        summary=daily.daily_cycle(testroot,offline=True,check_forms=False)
        job=first['jobs'][0]
        state=p.State(testroot/'var/jobs.sqlite3')
        try:
            stored=json.loads(state.db.execute('SELECT data FROM jobs').fetchone()[0])
            candidate=p.load_candidate(cfg['candidate'],testroot);facts={f['id']:f for f in candidate['facts']}
            fit=ab.content_fit(stored,candidate,facts,salary_policy=cfg['filters'])
            letter,error=ab.task_letter(stored,fit,facts)
            if not fit['eligible'] or not letter:raise AssertionError(('demo_did_not_produce_draft',fit,error))
            counts={'jobs':state.db.execute('SELECT count(*) FROM jobs').fetchone()[0],
                'sent':state.db.execute("SELECT count(*) FROM applications WHERE state IN ('sent','confirmed','dispatching')").fetchone()[0]}
        finally:state.close()
        out=Path(root)/'var/demo';out.mkdir(parents=True,exist_ok=True)
        report={'synthetic':True,'first_counts':first['counts'],'repeat_counts':second['counts'],
            'counts':counts,'summary':summary,'fit':fit,'letter':letter,'external_calls':0,'applications_sent':0}
        # Do not publish temporary absolute paths in the demo report.
        encoded=json.dumps(report,ensure_ascii=False,indent=2).replace(str(testroot),'<temporary-demo-project>')
        (out/'demo-report.json').write_text(encoded+'\n')
        (out/'draft.txt').write_text(letter['body']+'\n')
        (out/'README.md').write_text('# Вымышленная офлайн-подборка\n\n'+stored['title']+'\n\n'+stored['employer']+'\n\n'+letter['body']+'\n')
        return {'demo':'PASS','directory':'var/demo','jobs':counts['jobs'],'draft_prepared':True,
            'repeat_unchanged':second['counts'].get('unchanged',0),'network_calls':0,'applications_sent':0,'cost_usd':0}


def sync_resume(profile,resume_id,root=ROOT):
    """Read the owner's authorized resume DOM; never submit or invent a mapping."""
    import pipeline as p
    from chrome_executor import standalone_chrome
    rt.require(root,'browser');cfg=rt.validate_profile(root)
    if profile not in cfg['candidate']['cvs']:raise ValueError('unknown_resume_profile')
    import re
    if not re.fullmatch(r'[a-fA-F0-9]{16,64}',resume_id):raise ValueError('use_exact_HH_resume_id_from_your_URL')
    browser=standalone_chrome(root,headless=False)
    try:
        browser.managed_window();tab=browser.open('https://hh.ru/resume/'+resume_id)
        snapshot=browser.wait(tab,lambda s:bool(s.get('text')) and s.get('ready')=='complete',stage='own_resume_read')
        from chrome_executor import ChromeExecutor
        ChromeExecutor(root,browser,preparation_only=True).guard(snapshot)
        record=browser.js(tab,"JSON.stringify((()=>{const pick=s=>document.querySelector(s)?.innerText?.trim()||'';return {title:pick('[data-qa=resume-block-title-position]'),experience:pick('[data-qa=resume-block-experience]'),skills:pick('[data-qa=resume-block-skills]')};})())")
        if not all(isinstance(record.get(k),str) and record[k].strip() for k in ['title','experience','skills']):
            raise ValueError('HH_resume_DOM_incomplete_manual_review_required')
        record['experience']=[x.strip() for x in record['experience'].splitlines() if x.strip()]
        record['skills']=[x.strip() for x in record['skills'].splitlines() if x.strip()]
        record['resume_id']=resume_id
        version=p.digest({key:record[key] for key in ['resume_id','title','experience','skills']})
        source_file='cv/hh-snapshot-'+profile+'.json'
        save(root,source_file,{'profiles':{profile:record}})
        manifest=rt.read(root,'cv/current-cv-manifest.json')
        manifest['profiles'][profile]={'authoritative_source':'authorized_HH_resume_DOM','source_file':source_file,
            'hh_version':version,'hh_resume_id':resume_id,'title_verified':record['title']}
        save(root,'cv/current-cv-manifest.json',manifest)
        return {'profile':profile,'title':record['title'],'hh_resume_id':resume_id,'snapshot_saved':True,'submission_allowed':False}
    finally:browser.finish()


def render_schedule(root=ROOT):
    rt.identity(root);cfg=rt.settings(root);rt.validate_profile(root)
    label='local.job-search-agent.starter.'+rt.identity(root)[:12]
    hour,minute=map(int,cfg['daily_time'].split(':'))
    system_zone=datetime.now().astimezone().tzinfo
    value={'Label':label,'ProgramArguments':[str(Path(sys.executable).resolve()),str(Path(root).resolve()/'portable.py'),'daily'],
        'WorkingDirectory':str(Path(root).resolve()),'StartCalendarInterval':{'Hour':hour,'Minute':minute},
        'RunAtLoad':False,'StandardOutPath':str(Path(root).resolve()/'var/schedule.stdout.log'),
        'StandardErrorPath':str(Path(root).resolve()/'var/schedule.stderr.log')}
    path=Path(root)/'var'/f'{label}.plist';path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(plistlib.dumps(value));path.chmod(0o600)
    # Rendering is local. Installation is an explicit terminal action by the new owner.
    return {'rendered':str(path),'label':label,'installed':False,'clock':'launchd uses macOS local time',
        'configured_timezone':cfg['timezone'],'current_system_timezone':str(system_zone)}


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    for name in ['init','doctor','demo','validate','search','daily','render-schedule']:sub.add_parser(name)
    acceptance=sub.add_parser('accept-dispatch-policy');acceptance.add_argument('--acknowledge-reviewed',action='store_true',required=True)
    sub.add_parser('pause')
    sync=sub.add_parser('sync-resume');sync.add_argument('profile');sync.add_argument('resume_id')
    args=parser.parse_args()
    if args.command=='init':result=initialize()
    elif args.command=='doctor':result=doctor()
    elif args.command=='demo':result=demo()
    elif args.command=='validate':
        rt.validate_profile();result={'profile':'valid','no_integrations_called':True}
    elif args.command=='sync-resume':result=sync_resume(args.profile,args.resume_id)
    elif args.command=='render-schedule':result=render_schedule()
    elif args.command=='pause':
        policy=rt.settings()
        for key in ['hh_enabled','browser_enabled','ai_enabled','dispatch_enabled','mail_enabled']:policy[key]=False
        save(ROOT,'portable-config.json',policy)
        result={'all_external_actions_disabled':True,'schedule_changed':False}
    elif args.command=='accept-dispatch-policy':
        import pipeline as p
        import daily_prepare as daily
        import llm_prepare as llm
        rt.identity();rt.validate_profile()
        with p.exclusive_run(ROOT/'var'):
            state=p.State(ROOT/'var/jobs.sqlite3')
            try:
                proof=daily.setting(state,'dispatch:control-e2e',{})
                if proof.get('status')!='WORKS':raise ValueError('successful_no_send_E2E_required_first')
                daily.remember(state,'dispatch:quality-policy-accepted',{'policy':llm.QUALITY_POLICY,'accepted_by':'owner','time':p.now()})
                state.db.commit()
            finally:state.close()
        result={'policy_accepted':llm.QUALITY_POLICY,'dispatch_enabled':False,'note':'switches unchanged; one explicit control send is still required'}
    elif args.command=='search':
        rt.require(ROOT,'hh');import pipeline as p
        result=p.summary(p.run(rt.validate_profile(),root=ROOT))
    else:
        rt.require(ROOT,'hh');import daily_run as daily
        report=daily.run(ROOT)
        if report['status']=='blocked':raise ValueError('daily_run_blocked_check_var_daily_digest')
        result=daily.finish_daily(ROOT,report)
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,KeyError,OSError) as exc:
        # Do not expose HTTP bodies or credentials in setup errors.
        print(json.dumps({'error':str(exc),'action_stopped':True},ensure_ascii=False));raise SystemExit(1)
