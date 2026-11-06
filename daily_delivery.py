"""Local digest mail/secret helpers. No service, search, LLM calls or employer delivery."""
import argparse
import base64
from datetime import date, datetime
from copy import deepcopy
from email.message import EmailMessage
from email import policy
from email.utils import format_datetime
from getpass import getpass
from html import escape
import json
import hashlib
import os
from pathlib import Path
import re
import secrets
import ssl
import sqlite3
import subprocess
import urllib.parse
import urllib.request

import pipeline as p

DEFAULTS={'enabled':False,'delivery_approval':None,'recipient':None,'recipient_confirmed':False,
          'sender':None,'smtp_host':None,'smtp_port':465,'smtp_user':None,'tls':'implicit_ssl',
          'transport':'smtp','apps_script_url':None,'apps_script_test_confirmed':False}


def secret_store(root=p.ROOT):
    # Reuse the existing native binding, in a distinct project-only Keychain item.
    from hh_api import Keychain
    store=Keychain(Path(root));store.service=b'Job Search Agent Starter Daily'
    store.account=('daily-'+__import__('portable_runtime').identity(root)+'-'+p.digest(str(Path(root).resolve()))[:16]).encode()
    return store


def read_secret(name,root=p.ROOT):
    if name not in ('OPENAI_API_KEY','JOB_SMTP_PASSWORD'):raise ValueError('unsupported project secret')
    value=os.environ.get(name)
    if value:return value.strip()
    try:return secret_store(root).load().get(name) or None
    except Exception:raise ValueError('project_keychain_unavailable') from None


def tls_context():
    # launchd does not inherit Terminal's SSL_CERT_FILE. Use the system CA bundle
    # when this Python installation points at a missing default certificate file.
    paths=ssl.get_default_verify_paths()
    cafile='/etc/ssl/cert.pem' if not paths.cafile and Path('/etc/ssl/cert.pem').is_file() else None
    context=ssl.create_default_context(cafile=cafile);context.minimum_version=ssl.TLSVersion.TLSv1_2
    if context.verify_mode!=ssl.CERT_REQUIRED or context.check_hostname is not True:
        raise ValueError('TLS certificate verification required')
    return context


def settings(root):
    path=Path(root)/'delivery-config.json'
    cfg={**DEFAULTS,**(json.loads(path.read_text()) if path.exists() else {})}
    if cfg['tls']!='implicit_ssl' or not isinstance(cfg['smtp_port'],int) or not 1<=cfg['smtp_port']<=65535:
        raise ValueError('unsupported SMTP TLS configuration')
    cfg['smtp_host']=cfg['smtp_host'] or os.environ.get('JOB_SMTP_HOST')
    cfg['smtp_user']=cfg['smtp_user'] or os.environ.get('JOB_SMTP_USER')
    cfg['sender']=cfg['sender'] or os.environ.get('JOB_SMTP_SENDER')
    return cfg


def address(value):
    return isinstance(value,str) and bool(re.fullmatch(r'[^\s<>@,;\r\n]+@[^\s<>@,;\r\n]+\.[A-Za-z]{2,}',value))


def safe_text(value):
    return re.sub(r'\bsk-[A-Za-z0-9_-]{12,}','[секрет удалён]',str(value))


def apps_script_token(root,*,create=False):
    store=secret_store(root);record=store.load();token=record.get('JOB_REPORT_TOKEN')
    if not token and create:
        token=secrets.token_hex(32);record['JOB_REPORT_TOKEN']=token;store.save(record)
    if not isinstance(token,str) or not re.fullmatch(r'[a-f0-9]{64}',token):
        raise ValueError('apps_script_token_missing_or_invalid')
    return token


def valid_apps_script_url(url):
    if not isinstance(url,str):return False
    parsed=urllib.parse.urlsplit(url)
    return (parsed.scheme=='https' and parsed.netloc=='script.google.com'
        and not parsed.query and not parsed.fragment
        and bool(re.fullmatch(r'/macros/s/[A-Za-z0-9_-]+/exec',parsed.path)))


class AppsScriptRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        parsed=urllib.parse.urlsplit(newurl)
        if parsed.scheme!='https' or parsed.netloc not in ('script.google.com','script.googleusercontent.com'):
            raise ValueError('apps_script_redirect_not_allowed')
        if req.get_method()=='POST':
            # ContentService redirects only its response. Never replay the token/body.
            if code not in (301,302,303):raise ValueError('apps_script_POST_replay_forbidden')
            return urllib.request.Request(newurl,method='GET')
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def apps_script_transport(message,day,url,token,*,test=False,recipient=None):
    if not valid_apps_script_url(url):raise ValueError('apps_script_url_invalid')
    if not address(recipient) or str(message['To'])!=recipient:raise ValueError('apps_script_recipient_must_be_fixed')
    attachment=list(message.iter_attachments())
    if len(attachment)!=1 or attachment[0].get_filename()!='digest.html':raise ValueError('archived_digest_required')
    raw=attachment[0].get_payload(decode=True);checksum=hashlib.sha256(raw).hexdigest()
    report_id=('test:'+day+':mailapp-v1') if test else 'daily:'+day
    payload={'token':token,'report_date':day,'report_id':report_id,'subject':str(message['Subject']),
        'text_body':message.get_body(('plain',)).get_content(),'html_body':message.get_body(('html',)).get_content(),
        'digest_html_base64':base64.b64encode(raw).decode('ascii'),'digest_sha256':checksum}
    encoded=json.dumps(payload,ensure_ascii=False).encode('utf-8')
    if len(encoded)>8_000_000:raise ValueError('apps_script_report_too_large')
    request=urllib.request.Request(url,data=encoded,headers={'Content-Type':'application/json; charset=utf-8'},method='POST')
    opener=urllib.request.build_opener(urllib.request.HTTPSHandler(context=tls_context()),AppsScriptRedirect())
    # One request, no automatic retries. Redirects retrieve the response via GET only.
    with opener.open(request,timeout=30) as response:
        result=json.loads(response.read(16384).decode('utf-8'))
    if (result.get('status')!='mailapp_accepted' or result.get('report_id')!=report_id
            or result.get('recipient')!=recipient or result.get('digest_sha256')!=checksum):
        raise ValueError('apps_script_receipt_not_confirmed')
    return {'status':'mailapp_accepted','time':result.get('sent_at') or p.now(),
        'report_id':report_id,'digest_sha256':checksum}


def mail_content(report):
    """Explicit allowlist of user-facing fields; no config, debug logs or secret records."""
    counts=report.get('counts',{});dispatch=report.get('autonomous_dispatch',{})
    sent={str(i['hh_id']):i for i in report.get('sent_today',[]) if i.get('status')=='confirmed'}
    sent.update({str(i['hh_id']):i for i in dispatch.get('outcomes',[]) if i.get('status')=='confirmed' and str(i['hh_id']) not in sent})
    day=date.fromisoformat(report['date_moscow']).strftime('%d.%m.%Y')
    subject=f"Job Search Agent · Отчет за {day} · {len(sent)} откликов"
    text=[subject,f"Подтверждено в HH: {len(sent)}.",
        f"Новых вакансий: {counts.get('new_vacancies',0)}; проверено полных описаний: {counts.get('fresh_verified_vacancies',0)}; в подборке: {len(report.get('items',[]))}.",
        'Полный дневной отчет приложен: digest.html. Его можно скачать и открыть из письма.']
    if report.get('scope')=='control':text.append('Контрольный запуск. Основной ежедневный цикл выполняется по вашему расписанию.')
    elif report.get('status')!='completed':text.append('Цикл завершён не полностью: '+str(report.get('status'))+'.')
    html='<html lang="ru"><body style="font:16px/1.55 Arial,sans-serif;color:#192b40;max-width:950px;margin:auto"><h1>'+escape(subject)+'</h1>'+''.join('<p>'+escape(safe_text(s))+'</p>' for s in text[1:])
    text+=['','Подтвержденные отклики'];html+='<h2>Подтвержденные отклики</h2>'
    for item in sent.values():
        title=' — '.join(str(item[k]) for k in ('employer','title') if item.get(k)) or 'HH '+str(item['hh_id'])
        url='https://hh.ru/vacancy/'+str(item['hh_id']);cv=item.get('cv','не указано')
        profile=cv.get('profile','не указано') if isinstance(cv,dict) else cv
        body=item.get('letter') or ''
        if isinstance(body,dict):body=body.get('body','')
        text+=['',safe_text(title),url,'CV: '+str(profile)]
        html+='<h3><a href="'+escape(url,quote=True)+'">'+escape(safe_text(title))+'</a></h3><p>CV: '+escape(str(profile))+'</p>'
        if body and not report.get('quality_review_markdown'):
            text+=['Письмо:',safe_text(body)]
            html+='<div style="white-space:pre-wrap;background:#f2f5fa;padding:15px">'+escape(safe_text(body))+'</div>'
        for answer in item.get('answers',[]):
            line=safe_text(answer.get('prompt','Вопрос'))+': '+safe_text(answer.get('answer',''))
            text.append(line);html+='<p>'+escape(line)+'</p>'
    if not sent:text.append('Подтвержденных отправок за этот день нет.');html+='<p>Подтвержденных отправок за этот день нет.</p>'
    skipped={str(i['hh_id']):i for i in dispatch.get('excluded',[])}
    skipped.update({str(i['hh_id']):i for i in dispatch.get('outcomes',[]) if i.get('status')!='confirmed'})
    text+=['','Пропущенные и отложенные вакансии'];html+='<h2>Пропущенные и отложенные вакансии</h2><ul>'
    for vid,item in skipped.items():
        title=' — '.join(str(item[k]) for k in ('employer','title') if item.get(k)) or 'HH '+vid
        reason=safe_text(item.get('reason') or item.get('status') or 'Причина не указана')
        url='https://hh.ru/vacancy/'+vid
        text+=['- '+safe_text(title)+': '+reason,url]
        html+='<li><a href="'+escape(url,quote=True)+'">'+escape(safe_text(title))+'</a>: '+escape(reason)+'</li>'
    html+='</ul>'
    for reason,count in report.get('excluded_by_reason',{}).items():
        line=safe_text(reason)+': '+str(count)
        text.append(line);html+='<p>'+escape(line)+'</p>'
    usage=report.get('ai_usage',{});spent=usage.get('day',{});month=usage.get('month',{})
    line=f"OpenAI: {spent.get('calls',report.get('ai_calls',0))} вызовов; за день ${spent.get('actual_cost_usd',report.get('ai_cost_usd',0)):.6f}; с резервами ${spent.get('cost_usd',report.get('ai_cost_usd',0)):.6f}; за месяц ${month.get('actual_cost_usd',0):.6f}."
    text+=['',line];html+='<h2>Расход OpenAI</h2><p>'+escape(line)+'</p>'
    if report.get('decision_funnel'):
        funnel=report['decision_funnel'];line='Воронка: '+', '.join(str(k)+'='+str(v) for k,v in funnel.items())
        text+=['',line];html+='<p>'+escape(line)+'</p>'
    forecast=report.get('budget_forecast',{})
    if forecast:
        line='Остаток бюджета: $'+format(forecast.get('day_remaining_usd',0),'.6f')+' за сутки; $'+format(forecast.get('month_remaining_usd',0),'.6f')+' за месяц.'
        text.append(line);html+='<p>'+escape(line)+'</p>'
    if report.get('quality_review_markdown'):
        # Deployed MailApp accepts exactly digest.html. The existing fallback is
        # the full Markdown in both mail bodies; no deployment or extra service.
        review=safe_text(report['quality_review_markdown'])
        text+=['','Подробный отчет для ChatGPT · agent-quality-review.md',review]
        html+='<h2>Подробный отчет для ChatGPT</h2><pre style="white-space:pre-wrap">'+escape(review)+'</pre>'
    for item in report.get('important_exceptions',[]):
        text.append(safe_text(item));html+='<p>'+escape(safe_text(item))+'</p>'
    return subject,'\n'.join(text)+'\n',html+'</body></html>'


def mail_report(root,report):
    """Read names for historical outcomes without changing the digest or application history."""
    result=deepcopy(report);index={str(i['hh_id']):i for i in result.get('items',[])}
    outcomes=result.get('sent_today',[])+result.get('autonomous_dispatch',{}).get('outcomes',[])+result.get('autonomous_dispatch',{}).get('excluded',[])
    path=Path(root)/'var/jobs.sqlite3'
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) if path.exists() else None
    try:
        for item in outcomes:
            job=index.get(str(item['hh_id']),{})
            if db and not job:
                row=db.execute('SELECT data FROM jobs WHERE key=?',('hh:public:'+str(item['hh_id']),)).fetchone()
                if row:job=json.loads(row[0])
            for key in ('employer','title'):
                if not item.get(key) and job.get(key):item[key]=job[key]
    finally:
        if db:db.close()
    return result


def prepare_message(root,report,cfg=None):
    root=Path(root);cfg=cfg or settings(root)
    day=date.fromisoformat(report['date_moscow']).isoformat()
    attachment=root/'var/daily-digests'/day/'digest.html'
    if not attachment.is_file():raise ValueError('archived_digest_html_missing')
    archived=attachment.read_bytes()
    subject,text,html=mail_content(mail_report(root,report))
    msg=EmailMessage();msg['Subject']=subject
    # Clearly marked placeholders are confined to previews and cannot pass delivery gates.
    msg['From']=cfg['sender'] if address(cfg['sender']) else 'preview@job-search-agent.invalid'
    msg['To']=cfg['recipient'] if address(cfg['recipient']) else 'recipient-not-confirmed@example.invalid'
    msg['Date']=format_datetime(datetime.now().astimezone())
    fingerprint=p.digest([report['date_moscow'],text,p.digest(archived.hex()),cfg['recipient']])
    msg['Message-ID']='<'+fingerprint[:40]+'@job-search-agent.local>'
    msg.set_content(text);msg.add_alternative(html,subtype='html')
    msg.add_attachment(archived,maintype='text',subtype='html',filename='digest.html')
    folder=root/'var';folder.mkdir(parents=True,exist_ok=True)
    for path,data in [(folder/'daily-email-preview.eml',msg.as_bytes(policy=policy.SMTP)),
            (folder/'daily-email-preview.html',html.encode()),(folder/'daily-email-preview.txt',text.encode())]:
        path.write_bytes(data);path.chmod(0o600)
    return msg,fingerprint


def deliver(root,report,*,offline=False,one_test=False,apps_script_test=False):
    root=Path(root);cfg=settings(root);msg,fingerprint=prepare_message(root,report,cfg)
    channel='apps_script' if apps_script_test else cfg['transport'];test=one_test or apps_script_test
    result={'status':'preview_only','email_send_calls':0,'delivery_confirmed':False,
        'transport':channel,'preview':'var/daily-email-preview.eml','recipient_confirmed':bool(cfg['recipient_confirmed'] and address(cfg['recipient']))}
    if offline or (not cfg['enabled'] and not test):return result
    __import__('portable_runtime').require(root,'mail')
    if (not test and not cfg['delivery_approval']) or not result['recipient_confirmed']:
        return {**result,'status':'blocked','reason':'recurring_delivery_approval_or_confirmed_recipient_missing'}
    if channel=='apps_script':
        if cfg['recipient']!=cfg.get('apps_script_recipient'):return {**result,'status':'blocked','reason':'apps_script_recipient_must_be_fixed'}
        if not apps_script_test and not cfg['apps_script_test_confirmed']:
            return {**result,'status':'blocked','reason':'apps_script_test_receipt_not_confirmed_by_user'}
        if not valid_apps_script_url(cfg['apps_script_url']):return {**result,'status':'blocked','reason':'apps_script_url_missing_or_invalid'}
        try:token=apps_script_token(root)
        except Exception:return {**result,'status':'blocked','reason':'apps_script_keychain_token_unavailable'}
    elif channel=='smtp':
        if not all([cfg['smtp_host'],cfg['smtp_user'],address(cfg['sender'])]):
            return {**result,'status':'blocked','reason':'project_SMTP_configuration_missing'}
        try:password=read_secret('JOB_SMTP_PASSWORD',root)
        except ValueError:return {**result,'status':'blocked','reason':'project_keychain_unavailable'}
        if not password:return {**result,'status':'blocked','reason':'SMTP_password_missing'}
        if cfg['recipient'].endswith('.invalid') or cfg['sender'].endswith('.invalid'):
            return {**result,'status':'blocked','reason':'preview_address_cannot_receive_real_mail'}
    else:return {**result,'status':'blocked','reason':'unknown_mail_transport'}
    # Same existing SQLite/meta journal, no application records or new tables.
    with p.exclusive_run(root/'var'):
        state=p.State(root/'var/jobs.sqlite3')
        try:
            key=('daily:mail-apps-script-test:' if apps_script_test else 'daily:mail:')+report['date_moscow']
            protected=[key] if apps_script_test else [key,'daily:mail-test:'+report['date_moscow']]
            previous=state.db.execute('SELECT value FROM meta WHERE key IN ('+','.join('?' for _ in protected)+')',protected).fetchone()
            if previous:
                return {**result,'status':'previous_attempt_no_automatic_repeat',
                    'previous_status':json.loads(previous['value']).get('status')}
            payload={'status':'attempt_reserved','fingerprint':fingerprint,'time':p.now(),'transport':channel}
            if channel=='apps_script':payload['deployment_hash']=p.digest(cfg['apps_script_url'])
            state.db.execute('INSERT INTO meta VALUES(?,?)',(key,json.dumps(payload)));state.db.commit()
            try:
                if channel=='apps_script':
                    payload.update(apps_script_transport(msg,report['date_moscow'],cfg['apps_script_url'],token,test=apps_script_test,recipient=cfg['apps_script_recipient']),delivery_confirmed=False)
                else:
                    receipt=p.smtp_transport(msg.as_bytes(policy=policy.SMTP),cfg['smtp_host'],cfg['smtp_port'],cfg['sender'],cfg['recipient'],user=cfg['smtp_user'],password=password,tls_context=tls_context())
                    payload.update(status='smtp_accepted',delivery_confirmed=False,time=receipt['smtp_accepted_at'],message_id=msg['Message-ID'])
            except Exception as exc:payload.update(status='send_uncertain_no_retry',delivery_confirmed=False,error_type=type(exc).__name__)
            state.db.execute('UPDATE meta SET value=? WHERE key=?',(json.dumps(payload),key))
            state.event('daily_digest_email',status=payload['status'],date=report['date_moscow'],fingerprint=fingerprint,test_email=test,transport=channel)
            state.db.commit();return {**result,'status':payload['status'],'email_send_calls':1,'message_id':payload.get('message_id'),'report_id':payload.get('report_id'),'error_type':payload.get('error_type')}
        finally:state.close()


def enable_apps_script(root,day,*,received=False):
    """Only after an accepted test AND explicit confirmation that the inbox received it."""
    root=Path(root);cfg=settings(root);date.fromisoformat(day)
    if not received:raise ValueError('user_inbox_confirmation_required')
    with sqlite3.connect((root/'var/jobs.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
        row=db.execute('SELECT value FROM meta WHERE key=?',('daily:mail-apps-script-test:'+day,)).fetchone()
    receipt=json.loads(row[0]) if row else {}
    if (receipt.get('status')!='mailapp_accepted' or receipt.get('deployment_hash')!=p.digest(cfg['apps_script_url'])
            or cfg['recipient']!=cfg.get('apps_script_recipient') or not cfg['delivery_approval']):
        raise ValueError('successful_test_for_current_deployment_required')
    cfg.update(transport='apps_script',apps_script_test_confirmed=True,enabled=True)
    path=root/'delivery-config.json';path.write_text(json.dumps(cfg,ensure_ascii=False,indent=2)+'\n');path.chmod(0o600)


def configure(root,kind):
    root=Path(root)
    if kind=='apps-script':
        cfg=settings(root);url=input('URL опубликованного Apps Script (/exec): ').strip()
        if not valid_apps_script_url(url):raise ValueError('valid Apps Script HTTPS /exec URL required')
        apps_script_token(root,create=True)
        cfg.update(apps_script_url=url,apps_script_test_confirmed=False)
        path=root/'delivery-config.json';path.write_text(json.dumps(cfg,ensure_ascii=False,indent=2)+'\n');path.chmod(0o600)
        print('URL сохранён. Новый канал не включён; сначала нужны тест и подтверждение получения.');return
    store=secret_store(root);record=store.load()
    if kind=='openai':
        value=os.environ.get('OPENAI_API_KEY') or getpass('Уже существующий OpenAI API-ключ (скрытый ввод): ')
        if not value.strip():raise ValueError('existing API key required')
        record['OPENAI_API_KEY']=value.strip();store.save(record)
    else:
        cfg=settings(root)  # Preserve an already granted recurring delivery approval.
        for key,prompt in [('smtp_host','SMTP_SSL host'),('smtp_user','SMTP user'),('sender','Email отправителя')]:
            cfg[key]=input(prompt+(' ['+cfg[key]+']' if cfg[key] else '')+': ').strip() or cfg[key]
        if not (cfg['recipient_confirmed'] and address(cfg['recipient'])):
            cfg['recipient']=input('Подтверждённый email получателя: ').strip()
        if not address(cfg['sender']) or not address(cfg['recipient']):raise ValueError('valid sender and recipient required')
        password=os.environ.get('JOB_SMTP_PASSWORD') or getpass('SMTP пароль / пароль приложения (скрытый ввод): ')
        if not password:raise ValueError('SMTP password required')
        record['JOB_SMTP_PASSWORD']=password;store.save(record);cfg['recipient_confirmed']=True
        path=root/'delivery-config.json';path.write_text(json.dumps(cfg,ensure_ascii=False,indent=2)+'\n');path.chmod(0o600)
    print('Секрет сохранён в Keychain проекта. Настройка ничего не отправляет и не меняет разрешения на расходы AI.')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    action=parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--configure',choices=['smtp','openai','apps-script'])
    action.add_argument('--send-test-email',action='store_true',help='Only after separate permission: one email to confirmed recipient; recurring delivery stays disabled')
    action.add_argument('--copy-apps-script-token',action='store_true',help='Copy the project Keychain token to macOS clipboard without displaying it')
    action.add_argument('--send-apps-script-test',action='store_true',help='One Apps Script test, separate ID/journal; never replays the normal daily report')
    action.add_argument('--enable-apps-script',action='store_true',help='Requires successful test and explicit inbox confirmation')
    parser.add_argument('--report-date',help='Archived report date YYYY-MM-DD; defaults to the last completed digest')
    args=parser.parse_args()
    if args.copy_apps_script_token:
        subprocess.run(['/usr/bin/pbcopy'],input=apps_script_token(p.ROOT,create=True).encode(),check=True)
        print('Токен из Keychain скопирован. Вставьте его в Script Properties → JOB_REPORT_TOKEN. В чат не присылайте.')
    elif args.configure:configure(p.ROOT,args.configure)
    else:
        report=json.loads((p.ROOT/'var/daily-digest.json').read_text())
        day=date.fromisoformat(args.report_date or report['date_moscow']).isoformat()
        if args.enable_apps_script:
            received=input('Письмо с digest.html действительно получено на '+str(settings(p.ROOT)['recipient'])+'? Введите ПОЛУЧЕНО: ')=='ПОЛУЧЕНО'
            enable_apps_script(p.ROOT,day,received=received);print('Ежедневная доставка через Apps Script включена после подтверждённого теста.')
        else:
            report=json.loads((p.ROOT/'var/daily-digests'/day/'digest.json').read_text())
            print(json.dumps(deliver(p.ROOT,report,one_test=not args.send_apps_script_test,apps_script_test=args.send_apps_script_test),ensure_ascii=False))
