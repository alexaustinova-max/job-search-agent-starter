#!/usr/bin/env python3
"""Unattended HH cycle; dispatch stays gated by real control E2E, quality and explicit enablement."""
import argparse
from collections import Counter
from contextlib import redirect_stdout,closing
from datetime import datetime
from html import escape
from io import StringIO
from itertools import combinations
import json
import os
import re
import sqlite3
from pathlib import Path
import sys
import subprocess
import uuid
from zoneinfo import ZoneInfo

import application_batch as ab
import daily_prepare as daily
import llm_prepare as letters
import pipeline as p
import daily_delivery as delivery

VERSION='daily-digest-v1'
PRESENTATION_VERSION='daily-digest-priorities-v2'
LABELS = {0: 'Основное направление', 1: 'Дополнительное направление', 2: 'Дополнительное направление'}
PREFERENCES = {}


def local_draft(job,profile,facts):
    """Reuse the existing case renderer/checker; no semantic-quality or send approval implied."""
    brief=letters.editorial_brief(job,facts);choices=[]
    allowed={fid for t in brief['tasks'] for ids in t['evidence_by_theme'].values() for fid in ids}
    eligible=[fid for fid in facts if fid in allowed and facts.get(fid,{}).get('verified') and facts[fid].get('letter_text')]
    for size in (2,3):
        for ids in combinations(eligible,size):
            groups={}
            for fid in ids:groups.setdefault(letters.editorial_case_id(facts[fid]),[]).append(fid)
            if len(groups)!=2:continue
            fragments=[{'fact_id':fid,'text':facts[fid]['letter_text']} for fid in ids]
            examples=[{'fact_ids':group,'task_ids':[t['id'] for t in brief['tasks'] if any(fid in evidence for evidence in t['evidence_by_theme'].values() for fid in group)]} for group in groups.values()]
            issues=letters.editorial_issues({'letter':fragments,'examples':examples},brief,facts)
            if issues:continue
            body='Здравствуйте!\n\nНаправляю отклик на позицию «'+job['title']+'» в '+job['employer']+'.\n\n'
            body+='\n\n'.join(letters.case_paragraph(example,fragments,brief) for example in examples)
            if ab.letter_quality(body,list(ids),job,facts,curated=True):continue
            rank=sum(len(eligible)-eligible.index(fid) for fid in ids)
            choices.append((rank,{'body':body,'fact_ids':list(ids),'examples':examples,'local_fact_and_task_checks_passed':True,
                'editorial_approved':False,'submission_allowed':False,'quality_limit':'Локальные проверки фактов и связи с задачами; смысловая редакторская проверка остаётся ручной.'}))
    return max(choices,key=lambda x:x[0])[1] if choices else None


def salary_label(job):
    salary=job.get('salary')
    if not salary:return 'Не указана'
    low,high=salary.get('min'),salary.get('max');values=[f'{v:,}'.replace(',',' ') for v in (low,high) if v is not None]
    interval='–'.join(values)
    if len(values)==1:interval=('от ' if low is not None else 'до ')+interval
    currency='₽' if salary.get('currency','RUR') in ('RUR','RUB') else salary['currency']
    period='месяц' if salary.get('interval','month')=='month' else salary['interval']
    return f"{interval} {currency} / {period}; "+('до налогов' if salary.get('gross') else 'на руки' if salary.get('gross') is False else 'налоги не уточнены')


def write_text(path,text):
    path.parent.mkdir(parents=True,exist_ok=True);temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(text);temporary.chmod(0o600);temporary.replace(path)


def presentation_context(root,report):
    """Enrich the digest only. Never re-score, remove candidates or write SQLite."""
    root=Path(root);dbpath=root/'var/jobs.sqlite3'
    if not dbpath.exists():return
    cfg=json.loads((root/'pipeline-config.hh.json').read_text())
    base=json.loads((root/cfg['candidate']['facts_file']).read_text())
    facts={f['id']:f for f in base['facts'] if f.get('verified') and f.get('source')}
    fitpath=root/'var/daily-fit-decisions.json'
    fits={x['hh_id']:x for x in json.loads(fitpath.read_text()).get('items',[])} if fitpath.exists() else {}
    oldpath=root/'var/daily-digest.json'
    previous={x['hh_id']:x for x in json.loads(oldpath.read_text()).get('items',[])} if oldpath.exists() else {}
    packet={'purpose':'editorial_only_not_selection_or_submission','vacancies':[]}
    with sqlite3.connect(dbpath.as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        has_meta=bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone())
        for item in report.get('items',[]):
            row=db.execute('SELECT data FROM jobs WHERE key=?',('hh:public:'+item['hh_id'],)).fetchone()
            if not row:continue
            job=json.loads(row[0]);fit=item.get('fit_assessment') or fits.get(item['hh_id'],{})
            if has_meta:
                state=type('ReadOnlyState',(),{'db':db})()
                saved=daily.setting(state,'daily:letter:hh:public:'+item['hh_id'],{});letter=saved.get('letter',{})
                checked={'hh_id':item['hh_id'],'job_key':'hh:public:'+item['hh_id'],'cv':item['cv'],
                    'letter':letter.get('body',''),'letter_fact_ids':letter.get('fact_ids',[]),
                    'letter_sha256':p.digest(letter.get('body','')),'context_fingerprint':saved.get('fingerprint'),
                    'model':'reviewed_letter','test_only':False}
                if letter and not ab.letter_readiness(state,checked,root):
                    item['letter']={**letter,'editorial_checks_passed':True,'editorial_approved':True,
                        'approval_kind':saved.get('editorial_review',{}).get('approved_by')}
            item.setdefault('fit_assessment',fit)
            item['salary']=salary_label(job)
            fingerprint=p.digest([daily.material(job),item['cv'].get('hh_version'),base])
            review=item.get('presentation_review') or previous.get(item['hh_id'],{}).get('presentation_review',{})
            if review.get('context_fingerprint')!=fingerprint:review={}
            item.pop('presentation_review',None)
            if review:item['presentation_review']=review
            experience=fit.get('experience','needs_data');career=fit.get('career','needs_scope')
            ids=list(dict.fromkeys(fid for e in fit.get('evidence',[]) for fid in e['fact_ids']))
            ai_checked=bool(item.get('letter',{}).get('editorial_checks_passed')) if item.get('letter') else False
            ai_review=bool(item.get('letter',{}).get('provenance')=='openai_source_cited_needs_review') if item.get('letter') else False
            item['display']={
                'group':review.get('group','priority' if experience=='high' and career=='high' else 'consider'),
                'interest':review.get('interest',LABELS.get(item['priority'],'Смежная роль')+'. '+ '; '.join(fit.get('tasks',[])[:2])),
                'experience':review.get('experience',{'level':experience,'text':'Подтверждённые кейсы покрывают часть задач; отрасль, стаж и отдельные инструменты этим не подтверждаются.'}),
                'career':review.get('career',{'level':career,'text':'Оценка уровня роли и содержания задач из существующего отбора; не оценка профессиональных навыков.'}),
                'fact_ids':review.get('fact_ids',ids[:4]),
                'unconfirmed':review.get('unconfirmed',[risk_text(x) for x in item.get('risks',[]) if risk_text(x)]),
                'work_format':{'remote':'Удалённо','hybrid':'Гибрид','on-site':'Офис'}.get(job.get('workplace'),'Формат не уточнён')+' · '+', '.join(job.get('locations',[])),
                'letter_status':('AI-черновик: требуется фактическая проверка' if ai_review else 'Письмо проверено; форма проверяется перед отправкой' if ai_checked else 'Требуется AI-редактура') if item.get('letter') else 'Нужна подготовка письма',
                'review_source':'Содержательная проверка сохранённого описания' if review else 'Предварительная группировка по двум существующим оценкам',
                'evidence':[{'fact_id':fid,'text':facts[fid]['text'],'source':facts[fid]['source']} for fid in review.get('fact_ids',ids[:4]) if fid in facts]}
            # Examples are drafts too; no editorial or send permission is created here.
            item['letter_status']=('needs_review' if ai_review else 'ai_checked_draft' if ai_checked else 'needs_ai_editing') if item.get('letter') else 'not_prepared'
            if item.get('letter'):
                item['letter'].setdefault('editorial_approved',False);item['letter']['submission_allowed']=False
            item['submission_allowed']=False
            packet['vacancies'].append({'hh_id':item['hh_id'],'title':job['title'],'employer':job['employer'],
                'cv':item['cv']['profile'],'full_description':job['description'],
                'current_draft':item.get('letter',{}).get('body','') if item.get('letter') else '',
                'editorial_focus':item['display']['interest'],'unconfirmed':item['display']['unconfirmed']})
    report['presentation_version']=PRESENTATION_VERSION
    report.setdefault('editorial_stage',{'enabled':False,'packet':'var/daily-editorial-batch.json','paid_calls_performed':0,
        'reason':'Требуется отдельное разрешение бюджета; текущая команда только экспортирует контекст.'})
    daily.write_json(root/'var/daily-editorial-batch.json',packet)


def publish(root,report):
    presentation_context(root,report)
    return publish_priorities(root,report)


def publish_priorities(root,report):
    """Presentation groups are deliberately independent of admission/matching."""
    root=Path(root);counts=report.get('counts',{});items=report.get('items',[])
    intro=f"{len(items)} вакансий сохранено в подборке. Отправок: {report.get('applications_sent',0)}. Платных AI-вызовов: {report.get('ai_calls',0)}."
    stats=f"Карточек HH: {counts.get('unique_cards_seen',0)} · Полных вакансий проверено: {counts.get('fresh_verified_vacancies',0)} · Данные: {report.get('data_checked_at',report['generated_at'])}"
    salary_policy=json.loads((root/'pipeline-config.hh.json').read_text())['filters']
    salary_notes=[]
    for field,label in [('min_monthly_rub','Минимальный фикс для отбора'),('desired_monthly_rub','Ожидания при отклике')]:
        amount=salary_policy.get(field)
        if isinstance(amount,(int,float)) and not isinstance(amount,bool) and amount>0:
            salary_notes.append(label+': '+format(amount,',').replace(',',' ')+' ₽ '+salary_policy['salary_basis']+'.')
        else:
            salary_notes.append(label+(': не задан.' if field=='min_monthly_rub' else ': не заданы.'))
    note='Приоритет — порядок просмотра, а не новый фильтр. Неподтверждённый опыт означает риск, а не несоответствие. Неизвестная зарплата допускается. '+' '.join(salary_notes)+' Бонусы не заменяют фикс.'
    markdown=['# Job Search Agent · '+report['date_moscow'],'',intro,stats,note]
    sections=[]
    labels={'high':'Высокое','prospective':'Перспективное','needs_data':'Недостаточно данных','needs_scope':'Уточнить уровень','low_priority':'Невысокое'}
    for group,title in [('priority','Приоритетные'),('consider','Рассмотреть')]:
        cards=[];selected=[x for x in items if x.get('display',{}).get('group','consider')==group]
        markdown+=['','## '+title+f' · {len(selected)}']
        for item in selected:
            cv=item['cv'];view=item.get('display',{});letter=item.get('letter')
            body=letter['body'] if letter else 'Недостаточно подтверждённых связей с ключевыми задачами. Письмо не подготовлено.'
            experience=view.get('experience',{});career=view.get('career',{})
            axes=[('Профессиональный опыт',experience),('Карьерная привлекательность',career)]
            evidence=view.get('evidence',[]);gaps=view.get('unconfirmed',[]) or ['Существенные пробелы не перечислены; это не подтверждение полного соответствия.']
            status=view.get('letter_status','Требуется AI-редактура')
            action=('Агент проверит историю, вопросы и заполнение перед отправкой. Неизвестные обязательные вопросы будут отложены.'
                if report.get('daily_dispatch_enabled') else 'Отправка выключена до разрешения регулярного бюджета. Подтвержденные письма будут использованы повторно.')
            textarea_id='letter-'+item['hh_id']
            text='<article id="hh-'+item['hh_id']+'"><h3>'+escape(item['employer'])+' — '+escape(item['title'])+'</h3>'
            text+='<p><a href="'+escape(item['url'],quote=True)+'">Вакансия HH · '+item['hh_id']+'</a> · <a href="https://hh.ru/resume/'+escape(cv['hh_resume_id'],quote=True)+'">CV '+cv['profile']+'</a></p>'
            text+='<p><b>Чем интересна:</b> '+escape(view.get('interest','; '.join(item.get('why',[]))))+'</p><small>'+escape(view.get('review_source',''))+'</small>'
            text+='<div class="axes">'+''.join('<div><b>'+name+': '+escape(labels.get(axis.get('level'),axis.get('level','Не оценено')) )+'</b><p>'+escape(axis.get('text',''))+'</p></div>' for name,axis in axes)+'</div>'
            text+='<p><b>Подходит подтверждённый опыт:</b></p><ul>'+''.join('<li>'+escape(e['text'])+' <details><summary>Источник · '+escape(e['fact_id'])+'</summary>'+escape(str(e['source']))+'</details></li>' for e in evidence)+'</ul>'
            text+='<p><b>Не подтверждено / требует уточнения:</b></p><ul>'+''.join('<li>'+escape(x)+'</li>' for x in gaps)+'</ul>'
            text+='<p><b>Зарплата:</b> '+escape(item['salary'])+'<br><b>Работа:</b> '+escape(view.get('work_format','Не уточнено'))+'<br><b>CV '+cv['profile']+':</b> '+escape(cv['hh_title'])+'</p>'
            text+='<div class="draft"><b>'+escape(status)+'</b><p>Фактический допуск и успешная проверка формы обязательны; стилистические замечания не блокируют.</p><textarea id="'+textarea_id+'" readonly aria-label="Черновик письма" onclick="this.select()">'+escape(body)+'</textarea><button type="button" data-copy="'+textarea_id+'">Копировать письмо</button><span role="status" aria-live="polite"></span></div><p><b>Действие:</b> '+escape(action)+'</p></article>'
            cards.append(text)
            markdown+=['',f"### {item['employer']} — {item['title']}",item['url'],view.get('interest',''),
                *[name+': '+labels.get(axis.get('level'),'Не оценено')+'. '+axis.get('text','') for name,axis in axes],
                'Подтверждено: '+'; '.join(e['text'] for e in evidence),'Не подтверждено: '+'; '.join(gaps),
                f"CV {cv['profile']}: https://hh.ru/resume/{cv['hh_resume_id']}",
                'Зарплата: '+item['salary']+'. '+view.get('work_format',''),'','Письмо · '+status,'',body,'',action]
        sections.append('<section><h2>'+title+f' · {len(selected)}</h2>'+''.join(cards)+'</section>')
    important=report.get('important_exceptions',[])
    script="""document.querySelectorAll('[data-copy]').forEach(button=>button.addEventListener('click',async()=>{const field=document.getElementById(button.dataset.copy);const status=button.nextElementSibling;try{await navigator.clipboard.writeText(field.value);status.textContent=' Скопировано';}catch(e){field.focus();field.select();status.textContent=' Текст выделен: нажмите ⌘C / Ctrl+C';}}));"""
    html='<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Job Search Agent — '+report['date_moscow']+'</title><style>body{max-width:1000px;margin:32px auto;padding:0 20px;font:16px/1.55 system-ui;background:#f5f6f8;color:#17212d}article,header,aside{background:white;border:1px solid #dce1e8;border-radius:14px;padding:24px;margin:18px 0}h1{font-size:27px}h2{font-size:24px}h3{font-size:21px;margin-top:0}.axes{display:grid;grid-template-columns:repeat(2,1fr);gap:18px;background:#f1f5fa;padding:14px;border-radius:8px}.axes p{margin:6px 0}textarea{box-sizing:border-box;width:100%;min-height:310px;padding:14px;font:15px/1.55 system-ui;border:1px solid #ccd6e3;border-radius:8px}a{color:#1359b5}details,small{color:#596779}summary{cursor:pointer}button{font:inherit;padding:8px 14px;margin-top:8px;cursor:pointer}.draft{border-top:1px solid #e0e5ed;padding-top:16px}.draft>b{color:#7a4a00}@media(max-width:650px){.axes{grid-template-columns:1fr}article{padding:16px}}</style></head><body><header><h1>Job Search Agent · '+report['date_moscow']+'</h1><p>'+escape(intro)+'</p><p>'+escape(stats)+'</p><p>'+escape(note)+'</p><p><a href="#priority">Приоритетные</a> · <a href="#consider">Рассмотреть</a></p></header>'
    html+=sections[0].replace('<section>','<section id="priority">',1)+sections[1].replace('<section>','<section id="consider">',1)
    dispatch=report.get('autonomous_dispatch',{})
    if dispatch or report.get('sent_today'):
        outcomes=list(report.get('sent_today',[]));seen={x['hh_id'] for x in outcomes}
        outcomes += [o for o in dispatch.get('outcomes',[]) if o['hh_id'] not in seen]
        html+='<section><h2>Отправки за сутки и исключения</h2><p>Подтверждено в HH: '+str(report.get('applications_sent',0))+'. Отложено: '+str(sum(o['status']!='confirmed' for o in outcomes)+len(dispatch.get('excluded',[])))+'.</p>'
        for item in outcomes:
            cv=p.cv_choice(item['cv'],json.loads((root/'pipeline-config.hh.json').read_text())['candidate'],root)
            html+='<article><h3><a href="https://hh.ru/vacancy/'+escape(item['hh_id'])+'">HH '+escape(item['hh_id'])+'</a></h3><p>'+escape(item['status'])+' · <a href="https://hh.ru/resume/'+escape(cv['hh_resume_id'])+'">CV '+escape(item['cv'])+'</a></p><p>'+escape(item.get('reason','Подтверждено историей HH'))+'</p><pre style="white-space:pre-wrap">'+escape(item.get('letter',''))+'</pre>'
            if item.get('answers'):html+='<p>Ответы:</p><ul>'+''.join('<li>'+escape(a['prompt'])+': '+escape(str(a['answer']))+'</li>' for a in item['answers'])+'</ul>'
            html+='</article>'
        html+='<details><summary>Исключения подготовки</summary><ul>'+''.join('<li>HH '+escape(e['hh_id'])+': '+escape(e['reason'])+'</li>' for e in dispatch.get('excluded',[]))+'</ul></details></section>'
    usage=report.get('ai_usage',{});day=usage.get('day',{});month=usage.get('month',{})
    portable_cfg=__import__('portable_runtime').settings(root)
    llm_cfg=json.loads((root/'llm-config.json').read_text())
    dispatch_cfg=json.loads((root/'dispatch-config.json').read_text())
    daily_limit=min(portable_cfg['max_applications_per_day'],dispatch_cfg['max_confirmed_per_day'])
    ai_note='За сутки ('+portable_cfg['timezone']+'): '+str(day.get('calls',0))+' вызовов; фактически $'+format(day.get('actual_cost_usd',0),'.6f')+'; с неопределенными резервами $'+format(day.get('cost_usd',0),'.6f')+'. За месяц: $'+format(month.get('cost_usd',0),'.6f')+'. Лимиты: $'+str(llm_cfg['max_cost_usd_per_day'])+' / сутки, $'+str(llm_cfg['max_cost_usd_per_month'])+' / месяц; вызовов в сутки: '+str(llm_cfg['max_calls_per_day'])+'.'
    dispatch_note='Дневной лимит подтвержденных откликов: '+str(daily_limit)+'. Каждый отклик требует проверки истории, CV, письма и вопросов. Неизвестные обязательные вопросы и неопределенные отправки откладываются без автоматического повтора.'
    html+='<aside><h2>OpenAI · расход</h2><p>'+escape(ai_note)+'</p></aside>'
    html+='<aside><h2>Важные ограничения</h2><p>'+escape(dispatch_note)+'</p><details><summary>Подробности</summary><ul>'+''.join('<li>'+escape(x)+'</li>' for x in important)+'</ul></details></aside><small>Оформление обновлено: '+escape(report.get('presentation_updated_at',report['generated_at']))+'</small><script>'+script+'</script></body></html>'
    markdown+=['','## OpenAI · расход',ai_note,'','## Важные ограничения',dispatch_note,*['- '+x for x in important]]
    folder=root/'var';daily.write_json(folder/'daily-digest.json',report);write_text(folder/'daily-digest.md','\n'.join(markdown)+'\n');write_text(folder/'daily-digest.html',html)
    archive=folder/'daily-digests'/report['date_moscow'];daily.write_json(archive/'digest.json',report);write_text(archive/'digest.html',html)


def edit_digest_letters(root,report):
    """Existing editorial stage; recurring spending needs its own explicit grant."""
    root=Path(root);cfg=letters.settings(root)
    if not recurring_ai_approved(cfg):
        raise ValueError('paid LLM disabled: separate user spending approval required')
    packet=json.loads((root/'var/daily-editorial-batch.json').read_text())
    if not packet['vacancies']:return report
    with p.exclusive_run(root/'var'):
        state=p.State(root/'var/jobs.sqlite3')
        try:result=letters.prepare_letters(state,packet,root,mode='real',single_batch=True)
        finally:state.close()
    indexed={r['hh_id']:r for r in result['results'] if r.get('letter_body') and not r.get('test_only')}
    for item in report['items']:
        if item['hh_id'] in indexed:
            checked=indexed[item['hh_id']]
            item['letter']={'body':checked['letter_body'],'fact_ids':checked['fact_ids'],'sources':checked['sources'],
                'provenance':'openai_source_cited_needs_review','editorial_checks_passed':False,
                'status':'needs_review','questions':checked.get('questions',[]),'fact_validation_passed':False,
                'editorial_approved':False,'submission_allowed':False}
            item['risks']=list(dict.fromkeys(item.get('risks',[])+checked.get('questions',[])))
    report.update(ai_calls=result['api_calls'],ai_cost_usd=result['cost_usd'],
        editorial_stage={'enabled':True,'packet':'var/daily-editorial-batch.json','paid_calls_performed':result['api_calls'],
            'stopped':result['stopped'],'cache_hits':result['cache_hits'],'letters_validated':result['letters_validated']})
    daily.write_json(root/'var/daily-editorial-results.json',result)
    publish(root,report)
    return report


def recurring_ai_approved(cfg):
    return (cfg['enabled'] is True and isinstance(cfg.get('spending_approval'),str)
        and cfg.get('recurring_spending_approved') is True
        and cfg['spending_approval'].startswith('recurring-user-approved:'))


def runtime_check(root=p.ROOT):
    """Check launchd prerequisites without starting Chrome, HH requests or paid AI."""
    root=Path(root);result={'checked_at':p.now(),'pid':os.getpid(),'parent_pid':os.getppid(),
        'python':sys.executable,'paid_calls':0,'applications_sent':0,'errors':[]}
    try:
        browser=json.loads((root/'browser-config.json').read_text())
        command=[browser['node'],'-e',"const p=require(process.argv[1]);if(!p.chromium)process.exit(2);console.log(JSON.stringify({arch:process.arch,playwright:true}));",browser['playwright_module']]
        if browser.get('native_arch'):command=['/usr/bin/arch','-'+browser['native_arch'],*command]
        probe=subprocess.run(command,capture_output=True,text=True,timeout=15,check=True)
        observed=json.loads(probe.stdout);result['browser_runtime']=observed
        if observed['arch']!=browser.get('native_arch',observed['arch']):result['errors'].append('browser_arch_mismatch')
        profile=root/browser['profile_dir'];result['agent_profile_writable']=profile.is_dir() and os.access(profile,os.W_OK)
        if not result['agent_profile_writable']:result['errors'].append('agent_profile_not_writable')
        result['chrome_available']=Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome').is_file()
        if not result['chrome_available']:result['errors'].append('Chrome_missing')
        result['openai_key_available']=bool(delivery.read_secret('OPENAI_API_KEY',root))
        result['key_source']='environment' if os.environ.get('OPENAI_API_KEY') else 'project_Keychain'
        if not result['openai_key_available']:result['errors'].append('OpenAI_key_missing')
        tls=delivery.tls_context();result['tls_verified']=tls.check_hostname and bool(tls.cert_store_stats()['x509_ca'])
        if not result['tls_verified']:result['errors'].append('TLS_CA_store_missing')
        with closing(sqlite3.connect((root/'var/jobs.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row;state=type('ReadOnlyState',(),{'db':db})()
            proof=daily.setting(state,'dispatch:control-send-confirmed',{});receipt=proof.get('receipt',{})
            row=db.execute('SELECT state,receipt FROM applications WHERE job_key=?',('hh:public:'+str(proof.get('hh_id')),)).fetchone()
            result['confirmed_control_reused']=bool(row and row['state']=='confirmed' and receipt.get('history_verified') is True
                and json.loads(row['receipt']).get('letter_sha256')==receipt.get('letter_sha256'))
            if not result['confirmed_control_reused']:result['errors'].append('confirmed_control_missing')
    except Exception as exc:result['errors'].append(type(exc).__name__)
    result['ready']=not result['errors'];daily.write_json(root/'var/daily-runtime-status.json',result);return result


def finish_daily(root,report,*,offline=False,ai_already_run=False):
    """Same scheduled command: optional editorial gate -> mail preview/delivery."""
    root=Path(root);cfg=letters.settings(root)
    summary={'offline':offline,'paid_calls_this_stage':0,'ai_status':'disabled','applications_sent':0}
    dispatchpath=root/'dispatch-config.json'
    dispatchcfg=json.loads(dispatchpath.read_text()) if dispatchpath.exists() else {}
    if cfg.get('runtime_preflight_enabled') and not offline:
        summary['runtime']=runtime_check(root)
    if dispatchcfg.get('enabled') and not offline:
        ai_already_run=True  # Never fall through to the older second editorial path.
        with closing(sqlite3.connect((root/'var/jobs.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row;state=type('ReadOnlyState',(),{'db':db})()
            control=daily.setting(state,'dispatch:control-send-confirmed',{})
            policy=daily.setting(state,'dispatch:quality-policy-accepted',{})
        if summary.get('runtime',{}).get('ready') is False:
            summary['dispatch_status']='blocked_runtime_prerequisites'
        elif not control or policy.get('policy')!=letters.QUALITY_POLICY or not recurring_ai_approved(cfg):
            summary['dispatch_status']='blocked_control_quality_or_spending_gate'
        elif report.get('status') not in ('completed','partial'):
            summary['dispatch_status']='blocked_HH_daily_cycle_failed'
        else:
            try:
                editorial=ab.prepare_automatic_queue(root)
                ai_already_run=True
                verified=set(editorial.get('verified_queue_ids',[]))
                candidates={e['hh_id']:e for e in ab.queue_candidates(root)}
                report['items']=[e for vid,e in candidates.items() if vid in verified]
                report['counts']['carried_forward_candidates']=len(verified)
                presentation_context(root,report)
                result=ab.automatic_dispatch(root)
                summary.update(dispatch_status='completed',applications_sent=result['confirmed'])
                report.update(autonomous_dispatch=result,applications_sent=result['confirmed'],editorial_stage=editorial)
                publish_priorities(root,report);record(root,report)
            except Exception as exc:
                summary['dispatch_status']='stopped_no_automatic_retry: '+type(exc).__name__
    elif cfg.get('runtime_preflight_enabled') and summary.get('runtime',{}).get('ready') and not offline:
        # Keep saved, unsubmitted candidates visible, but fresh-check them before
        # carrying them forward. This is a plan, not a generation or form action.
        try:
            plan=ab.prepare_automatic_queue(root,plan_only=True)
            candidates={e['hh_id']:e for e in ab.queue_candidates(root)}
            report['items']=[e for vid,e in candidates.items() if vid in set(plan['verified_queue_ids'])]
            report['counts']['carried_forward_candidates']=len(report['items'])
            report['queue_plan']=plan;presentation_context(root,report)
            summary['queue_plan']={k:plan[k] for k in ('eligible_queued','reused_quality_letters','letters_waiting_for_AI','preparation_exclusions')}
        except Exception as exc:summary['queue_plan']={'status':'blocked','reason':type(exc).__name__}
    if cfg['daily_editorial_enabled'] and not offline and not ai_already_run:
        if not recurring_ai_approved(cfg):summary['ai_status']='blocked_separate_recurring_spending_approval_required'
        else:
            try:
                report=edit_digest_letters(root,report);record(root,report)
                summary.update(ai_status=report['editorial_stage'].get('stopped') or 'drafts_need_review',paid_calls_this_stage=report.get('ai_calls',0))
            except Exception:summary['ai_status']='editorial_stage_failed_no_automatic_retry'
    packetpath=root/'var/daily-editorial-batch.json'
    if packetpath.exists() and report.get('items'):
        packet=json.loads(packetpath.read_text())
        if {v['hh_id'] for v in packet['vacancies']}=={v['hh_id'] for v in report['items']}:
            try:
                with closing(sqlite3.connect((root/'var/jobs.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
                    db.row_factory=sqlite3.Row
                    plan=letters.prepare_letters(type('ReadOnlyState',(),{'db':db})(),packet,root,mode='real',single_batch=True,plan_only=True)
                daily.write_json(root/'var/daily-editorial-plan.json',plan)
                summary['editorial_plan']={k:plan[k] for k in ('planned_calls','cost_upper_bound_usd','within_configured_daily_limits','within_configured_monthly_limit')}
            except Exception:summary['editorial_plan']={'status':'saved_context_requires_review'}
    with closing(sqlite3.connect((root/'var/jobs.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row;state=type('ReadOnlyState',(),{'db':db})()
        report['ai_usage']=letters.spending_totals(state)
        sent_ids=ab.daily_send_ids(state,report['ai_usage']['date_moscow']);report['sent_today']=[]
        for row in db.execute("SELECT job_key,payload,receipt FROM applications WHERE state='confirmed'"):
            vid=row['job_key'].removeprefix('hh:public:')
            if vid not in sent_ids:continue
            payload=json.loads(row['payload']);receipt=json.loads(row['receipt'])
            report['sent_today'].append({'hh_id':vid,'status':'confirmed','cv':payload['profile'],
                'letter':payload.get('body',''),'answers':receipt.get('answers',payload.get('answers',[])),'receipt':receipt})
    report['applications_sent']=len(report['sent_today']);report['daily_dispatch_enabled']=bool(dispatchcfg.get('enabled') and recurring_ai_approved(cfg))
    report['ai_calls']=report['ai_usage']['day']['calls'];report['ai_cost_usd']=report['ai_usage']['day']['actual_cost_usd']
    publish_priorities(root,report);record(root,report)
    summary['ai_usage']=report['ai_usage']
    try:summary['mail']=delivery.deliver(root,report,offline=offline)
    except Exception:
        # Mail is the last independent stage. Never replay HH or expose transport/secret errors.
        summary['mail']={'status':'mail_stage_failed_no_automatic_retry','delivery_confirmed':False}
    daily.write_json(root/'var/daily-delivery-summary.json',summary)
    return summary


def run(root=p.ROOT,*,trigger='manual',force=False):
    root=Path(root);folder=root/'var';day=datetime.now(ZoneInfo(__import__('portable_runtime').timezone())).date().isoformat()
    cfg=json.loads((root/'pipeline-config.hh.json').read_text());base=json.loads((root/cfg['candidate']['facts_file']).read_text());facts={f['id']:f for f in base['facts'] if f.get('verified')};cvs={pr:p.cv_choice(pr,cfg['candidate'],root) for pr in cfg['candidate'].get('cvs',{})}
    input_hash=p.digest([VERSION,daily.DAILY_CYCLE_VERSION,ab.CONTENT_POLICY,cfg,base,cvs])
    report={'run_id':uuid.uuid4().hex,'generated_at':p.now(),'date_moscow':day,'trigger':trigger,'pid':os.getpid(),'parent_pid':os.getppid(),'python':sys.executable,'input_hash':input_hash,'status':'blocked','items':[],'important_exceptions':[],'applications_sent':0,'ai_calls':0,'ai_cost_usd':0,'cache_hit':False}
    old_path=folder/'daily-digest.json'
    if old_path.exists() and not force:
        old=json.loads(old_path.read_text())
        if old.get('date_moscow')==day and old.get('input_hash')==input_hash and old.get('status')=='completed':
            report.update({k:v for k,v in old.items() if k not in ('run_id','generated_at','trigger','pid','parent_pid','python','cache_hit')})
            report.update(cache_hit=True,source_run_id=old.get('source_run_id',old['run_id']),data_checked_at=old.get('data_checked_at',old['generated_at']),hh_requests_this_run=0)
            publish(root,report);record(root,report);return report
    try:
        if not all(c.get('available') and c.get('hh_resume_id') for c in cvs.values()):raise ValueError('current_HH_CV_mapping_missing')
        log=StringIO()
        with redirect_stdout(log):summary=daily.daily_cycle(root,check_forms=False,fresh_only=True,limit=__import__('portable_runtime').settings(root)['max_applications_per_day'])
        write_text(folder/'daily-cycle-progress.log',log.getvalue())
        report['counts']=summary['counts'];report['hh_requests_this_run']=summary['counts'].get('search_requests',0)+summary['counts'].get('detail_requests',0)
        errors=summary.get('errors',[]);report['status']='partial' if errors else 'completed'
        report['important_exceptions']+=[f"HH: {e.get('code','ошибка')} (HTTP {e.get('status') or 'не получен'}). Повторы и обходы не выполняются; старые списки не подставляются." for e in errors]
        packet=json.loads((folder/'daily-batch-ai.json').read_text());fit_rows=json.loads((folder/'daily-fit-decisions.json').read_text())['items'];report['relevant_total']=sum(x.get('eligible') is True for x in fit_rows)
        state=p.State(folder/'jobs.sqlite3')
        try:
            held={r['key'].removeprefix('daily:lifecycle:'):json.loads(r['value']) for r in state.db.execute("SELECT key,value FROM meta WHERE key LIKE 'daily:lifecycle:%'")}
            entries=packet.get('reviewed_drafts',[])+packet.get('vacancies',[]);seen=set()
            for entry in sorted(entries,key=lambda e:e.get('priority',e.get('fit',{}).get('priority',9))):
                vid=entry['hh_id'];key='hh:public:'+vid
                if vid in seen:continue
                if len(report['items'])>=__import__('portable_runtime').settings(root)['max_applications_per_day']:break
                seen.add(vid);row=state.db.execute('SELECT * FROM jobs WHERE key=?',(key,)).fetchone()
                if not row or daily.exclusion(state,row,held):continue
                job=json.loads(row['data']);profile=entry['cv']['profile'] if isinstance(entry['cv'],dict) else entry['cv'];cv=cvs[profile];fit=entry.get('fit',entry)
                cachekey='daily:editorial:'+key;cached=daily.setting(state,cachekey,{})
                draft=cached.get('summary_draft') if cached.get('summary_draft_version')==VERSION and cached.get('fingerprint')==entry['context_fingerprint'] else None
                if not draft:
                    draft=local_draft(job,profile,facts);cached.update(fingerprint=entry['context_fingerprint'],summary_draft=draft,summary_draft_version=VERSION);daily.remember(state,cachekey,cached)
                brief=letters.editorial_brief(job,facts)
                tasks={t['id']:t['text'] for t in brief['tasks']}
                why=[tasks[e['task_ids'][0]][:160]+' → '+facts[e['fact_ids'][0]]['text'] for e in draft['examples']] if draft else fit.get('tasks',[])[:3]
                risks=[risk for risk in fit.get('risks',[]) if job.get('salary') or not re_salary_note(risk)]
                decision=json.loads(row['decision']) if row['decision'] else {}
                for gap in decision.get('match',{}).get('unconfirmed_requirements',[]):
                    if (gap.get('kind')=='mandatory' and re.search(r'опыт|знани|умени|навык|образован|владени|понимани|\bSQL\b|\bPython\b',gap['text'],re.I)
                            and not re.fullmatch(r'высшее образование[.!;]?',gap['text'].strip(),re.I)):
                        risks.append('Нет однозначного подтверждения: '+gap['text'])
                report['items'].append({'hh_id':vid,'employer':job['employer'],'title':job['title'],'url':'https://hh.ru/vacancy/'+vid,'cv':cv,'priority':fit.get('priority',entry.get('priority',2)),'salary':salary_label(job),'why':why,'risks':risks[:5],'letter':draft,'letter_status':'needs_editorial_review' if draft else 'not_prepared','questions_state':'unknown','submission_allowed':False})
            state.db.commit()
        finally:state.close()
        if not report['items']:report['important_exceptions'].append('Среди актуально проверенных вакансий подходящая подборка не сформирована. Количество не добирается нерелевантными позициями.')
        reason_counts=Counter(x.get('reason','other') for x in fit_rows if not x.get('eligible'))
        report['excluded_by_reason']=dict(reason_counts)
        grouped=Counter(reason for row in fit_rows if not row.get('eligible') for reason in row.get('reason','other').split(','))
        for label,keys in [('Подтверждённый доход ниже порога',('salary_below_floor','confirmed_fixed_NET_below_floor')),
                           ('Неподходящая география',('office_outside_moscow','russia_explicitly_excluded','mandatory_relocation')),
                           ('Чужая профессия или коммерческий ассортимент вместо цифрового продукта',('different_core_profession','commercial_assortment_not_digital_product')),
                           ('Тестовое задание — нужна ручная проверка',('test_requires_user',))]:
            count=sum(grouped[k] for k in keys)
            if count:report['important_exceptions'].append(f'{label}: {count} причин исключения. Одна вакансия может иметь несколько причин.')
        report['important_exceptions'].append('Отправка использует подтвержденный браузерный исполнитель; неизвестные обязательные вопросы, тесты и защиты HH откладываются без повторной отправки.')
    except Exception as exc:
        # Never publish stale candidates or exception messages containing credential/server data.
        report['status']='blocked';report['items']=[];report['error_code']=getattr(exc,'code',type(exc).__name__)
        report['important_exceptions'].append('Ежедневный запуск остановлен: '+report['error_code']+'. Свежие результаты не подтверждены; старые вакансии не подставлены.')
    publish(root,report);record(root,report);return report


def record(root,report):
    with p.exclusive_run(root/'var'):
        state=p.State(root/'var/jobs.sqlite3')
        try:
            daily.remember(state,'daily:last_digest',report)
            state.event('daily_digest_published',run_id=report['run_id'],trigger=report['trigger'],status=report['status'],cache_hit=report['cache_hit'],source_run_id=report.get('source_run_id'),hh_requests=report.get('hh_requests_this_run',0),candidates=len(report['items']),drafts=sum(bool(x.get('letter')) for x in report['items']),applications_sent=report.get('applications_sent',0),ai_calls=report.get('ai_calls',0),ai_cost_usd=report.get('ai_cost_usd',0),summary='var/daily-digest.html');state.db.commit()
        finally:state.close()


def re_salary_note(text):
    return any(word in str(text).casefold() for word in ('зарплат','доход','salary','compensation'))


def risk_text(text):
    return {'published_net_min_meets_floor_rub':'','published_gross_min_meets_floor_rub':'',
            'salary_gross_range_requires_negotiation':'Вилка GROSS требует обсуждения фиксированной части относительно вашего порога.',
            'published_NET_kept_without_GROSS_conversion':'Указана зарплата NET; GROSS не рассчитывался и не приравнивался к NET.',
            'fixed_part_not_disclosed':'Указан доход с переменной частью; размер фиксированной части не раскрыт.',
            'salary_ceiling_not_published':'Указана только нижняя граница дохода; условия предложения уточняются.',
            'salary_net_range_requires_negotiation':'Вилка дохода пересекает настроенный порог; фиксированная часть не подтверждена.',
            'foreign_salary_no_verified_fx':'Доход указан в иностранной валюте; соответствие выбранному зарплатному порогу не подтверждено.',
            'remote_from_russia_requires_confirmation':'Допустимая география удалённой работы требует подтверждения.'}.get(text,text)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--trigger',choices=('manual','launchd'),default='manual');parser.add_argument('--force',action='store_true',help='Explicitly run another fresh incremental cycle today')
    parser.add_argument('--edit-letters',action='store_true',help='Optional one-batch OpenAI editorial stage; separate configured spending approval required. Off in launchd.')
    parser.add_argument('--preview-delivery',action='store_true',help='Offline end-stage replay of saved digest; no HH, SMTP, secrets or paid calls')
    parser.add_argument('--check-runtime',action='store_true',help='Check Keychain/TLS/native browser runtime and saved control receipt; no HH or paid calls')
    args=parser.parse_args();os.umask(0o077)
    if args.check_runtime:
        result=runtime_check();print(json.dumps(result,ensure_ascii=False));raise SystemExit(0 if result['ready'] else 1)
    if args.preview_delivery:
        if args.edit_letters or args.force:parser.error('offline mail preview cannot enable paid AI or search')
        report=json.loads((p.ROOT/'var/daily-digest.json').read_text())
        print(json.dumps(finish_daily(p.ROOT,report,offline=True),ensure_ascii=False));raise SystemExit(0)
    if args.edit_letters:
        cfg=letters.settings(p.ROOT)
        if not recurring_ai_approved(cfg):parser.error('paid LLM disabled: separate user spending approval required')
    result=run(trigger=args.trigger,force=args.force)
    if args.edit_letters and result['items']:
        result=edit_digest_letters(p.ROOT,result);record(p.ROOT,result)
    finish_daily(p.ROOT,result,ai_already_run=args.edit_letters)
    print(json.dumps({k:result.get(k) for k in ('run_id','status','cache_hit','trigger','pid','parent_pid','counts','hh_requests_this_run','applications_sent','ai_calls')},ensure_ascii=False));raise SystemExit(0 if result['status']=='completed' else 1)
