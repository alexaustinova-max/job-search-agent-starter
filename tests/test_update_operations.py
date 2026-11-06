"""Operational regression cases with isolated SQLite, fake model and browser doubles."""
import copy
import json
from datetime import datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock,patch
import application_batch as ab
import chrome_executor as ce
import daily_prepare as daily
import daily_run as run
import daily_delivery as mail
import llm_prepare as llm
import pipeline as p
import portable as cli
import portable_runtime as rt
from update_fixtures import Fixture

class UpdateOperationsTests(Fixture,TestCase):
    def config(self,name,**changes):
        cfg=rt.read(self.root,name);cfg.update(changes);cli.save(self.root,name,cfg);return cfg

    def test_configured_due_time_and_timezone(self):
        self.policy(daily_time='18:45',timezone='Etc/UTC')
        with patch.object(run,'run',side_effect=AssertionError('not due')),patch.object(rt,'timezone',wraps=rt.timezone) as timezone:
            result=run.execute_daily(self.root,trigger='launchd',now=datetime(2026,11,6,18,44))
        self.assertEqual(result['status'],'not_due')
        with patch.object(rt,'timezone',return_value='Etc/UTC') as timezone,patch.object(run,'run',return_value={'run_id':'demo','status':'completed','items':[]}),patch.object(run,'finish_daily',return_value={}):
            run.execute_daily(self.root,force=True)
        timezone.assert_any_call(self.root)

    def test_daily_singleton_lock_prevents_all_work(self):
        with (self.root/'var/.daily-cycle.lock').open('a') as held:
            run.fcntl.flock(held,run.fcntl.LOCK_EX|run.fcntl.LOCK_NB)
            with patch.object(run,'run',side_effect=AssertionError('locked')):
                self.assertEqual(run.execute_daily(self.root)['status'],'already_running')

    def test_completed_day_never_repeats_search_generation_or_delivery(self):
        report={'run_id':'demo','status':'completed','items':[],'applications_sent':0}
        with patch.object(run,'run',return_value=report) as search,patch.object(run,'finish_daily',return_value={'dispatch_status':'completed'}) as finish:
            self.assertEqual(run.execute_daily(self.root)['status'],'completed')
            self.assertEqual(run.execute_daily(self.root)['status'],'already_completed')
        self.assertEqual(search.call_count,1);self.assertEqual(finish.call_count,1)

    def test_saved_partial_day_resumes_without_new_search(self):
        day=datetime.now(run.ZoneInfo(rt.timezone(self.root))).date().isoformat()
        cli.save(self.root,'var/daily-digest.json',{'date_moscow':day,'run_id':'old','status':'partial','items':[],
            'editorial_stage':{'verified_queue_ids':['900000001']}})
        with patch.object(run,'run',side_effect=AssertionError('no repeat search')),patch.object(run,'finish_daily',return_value={}) as finish:
            result=run.execute_daily(self.root)
        self.assertTrue(result['resumed_saved_cycle']);self.assertEqual(result['hh_requests_this_run'],0)
        self.assertTrue(finish.call_args.kwargs['reuse_prepared'])

    def test_browser_failure_does_not_mark_day_completed(self):
        self.config('dispatch-config.json',enabled=True)
        report={'run_id':'demo','status':'completed','items':[],'autonomous_dispatch':{'outcomes':[{'reason':'agent_browser_transport_timeout_no_retry'}]}}
        with patch.object(run,'run',return_value=report),patch.object(run,'finish_daily',return_value={'dispatch_status':'completed'}):
            self.assertEqual(run.execute_daily(self.root)['status'],'retryable_failure')

    def test_one_off_permission_is_not_recurring_consent(self):
        self.assertFalse(run.recurring_ai_approved({'enabled':True,'spending_approval':'one-off'}))
        self.assertFalse(run.recurring_ai_approved({'enabled':True,'spending_approval':'recurring-user-approved:demo'}))
        self.assertTrue(run.recurring_ai_approved({'enabled':True,'spending_approval':'recurring-user-approved:demo','recurring_spending_approved':True}))

    def test_pause_blocks_preparation_and_dispatch(self):
        self.config('dispatch-config.json',paused=True)
        with patch.object(ab,'queue_candidates',side_effect=AssertionError('paused')):
            with self.assertRaisesRegex(ValueError,'paused'):ab.prepare_automatic_queue(self.root)
            with self.assertRaisesRegex(ValueError,'paused'):ab.automatic_dispatch(self.root,dry_run=True)

    def test_empty_id_set_never_dispatches_all_candidates(self):
        with patch.object(ab,'queue_candidates',return_value=[{'hh_id':'900000001'}]),patch.object(ab,'automatic_manifest',return_value=({'items':[]},[])) as manifest,patch.object(ce,'standalone_chrome',side_effect=AssertionError('no browser')):
            result=ab.automatic_dispatch(self.root,dry_run=True,ids=set())
        self.assertEqual(manifest.call_args.args[2],[]);self.assertEqual(result['applications_sent'],0)

    def test_preclick_proof_cannot_release_uncertain_or_changed_material(self):
        job=self.add_job();self.application(job,'needs_review')
        self.state.event('dispatching',job['key'],job['id']);event=self.state.db.execute('SELECT max(id) FROM events').fetchone()[0]
        self.state.event('needs_review',job['key'],job['id'],no_automatic_retry=False);self.state.db.commit()
        row=self.state.db.execute('SELECT * FROM jobs WHERE key=?',(job['key'],)).fetchone()
        self.assertEqual(daily.exclusion(self.state,row,{}),'delivery_history')
        proof={'dispatch_event_id':event,'history':{'checked':True,'found':False,'scopes':[{'scope':x,'pages':[1]} for x in ('tab_filter_all','tab_filter_archived','tab_filter_deleted')]},'job_material_sha256':p.digest(daily.material(job))}
        daily.remember(self.state,'application:preclick-history-proof:'+job['key'],proof)
        self.assertIsNone(daily.exclusion(self.state,row,{}))
        proof['job_material_sha256']='changed';daily.remember(self.state,'application:preclick-history-proof:'+job['key'],proof)
        self.assertEqual(daily.exclusion(self.state,row,{}),'delivery_history')
        proof['job_material_sha256']=p.digest(daily.material(job));daily.remember(self.state,'application:preclick-history-proof:'+job['key'],proof)
        self.state.event('send_uncertain',job['key'],job['id']);self.state.db.commit()
        self.assertEqual(daily.exclusion(self.state,row,{}),'delivery_history')

    def test_pause_during_form_check_stops_before_click(self):
        self.policy(profile_confirmed=True,dispatch_enabled=True,hh_enabled=True,browser_enabled=True)
        self.config('dispatch-config.json',enabled=True,max_confirmed_per_day=1)
        executor=ce.ChromeExecutor.__new__(ce.ChromeExecutor);executor.root=self.root;executor.supports_submission=True
        item={'hh_id':'900000001','letter':'Approved letter','hh_resume_id':'fictional-resume'}
        executor.sessions={item['hh_id']:{'tab':'own','confirmed_answers':[],'prepared':{}}}
        executor.history=MagicMock(return_value={'found':False});executor.chrome=MagicMock()
        executor.chrome.independent_profile=False;executor.chrome.snapshot.return_value={'fields':[{'value':item['letter']}]}
        executor.guard=MagicMock();executor.checked_form=MagicMock(return_value={'cv_verified':True,'resume_id':item['hh_resume_id']})
        def paused(*a,**k):self.policy(dispatch_enabled=False);return {'ready':True}
        with patch.object(ab,'readiness',side_effect=paused):
            with self.assertRaisesRegex(ValueError,'dispatch_disabled'):executor.submit_once(item,[])
        executor.chrome.js.assert_not_called();self.assertFalse(executor.sessions[item['hh_id']].get('clicked',False))

    def test_missing_history_cannot_confirm_or_leak_form_text(self):
        executor=ce.ChromeExecutor.__new__(ce.ChromeExecutor);executor.history=MagicMock(return_value={'found':False})
        with self.assertRaises(ValueError) as error:executor.verify_history({'hh_id':'900000001'},{'post_click':{'text':'PRIVATE_FIELD_MARKER'}})
        self.assertNotIn('PRIVATE_FIELD_MARKER',str(error.exception));self.assertIn('history_missing',str(error.exception))

    def test_live_limits_tighten_running_snapshot(self):
        self.configure_ai();stale=llm.settings(self.root)
        self.config('llm-config.json',max_calls_per_day=0)
        self.assertIsNone(llm.reserve(self.state,'real',stale,{'vacancies':[{'hh_id':'900000001'}]}))
        self.assertEqual(self.state.db.execute("SELECT count(*) FROM events WHERE stage='llm_call_reserved'").fetchone()[0],0)

    def test_incomplete_provider_response_charged_once_without_retry(self):
        self.configure_ai();client=MagicMock();client.generate.return_value={'text':'','response_status':'incomplete','incomplete_reason':'max_output_tokens','usage':{'input_tokens':12,'output_tokens':512}}
        payload={'task':'independent_letter_review','vacancies':[{'hh_id':'900000001'}]}
        with self.assertRaisesRegex(ValueError,'incomplete_max_output_tokens_no_retry'):llm.bounded_task(self.state,payload,'local',llm.POLICY_SCHEMA,self.root,client=client)
        with self.assertRaisesRegex(ValueError,'previous_AI_attempt'):llm.bounded_task(self.state,payload,'local',llm.POLICY_SCHEMA,self.root,client=client)
        self.assertEqual(client.generate.call_count,1);self.assertGreater(llm.spending_totals(self.state)['day']['actual_cost_usd'],0)

    def test_uncertain_transport_keeps_reservation_and_never_retries(self):
        self.configure_ai();client=MagicMock();client.generate.side_effect=OSError('transport')
        payload={'task':'check_HH_answers','vacancies':[{'hh_id':'900000001'}]}
        with self.assertRaisesRegex(ValueError,'transport_uncertain'):llm.bounded_task(self.state,payload,'local',llm.POLICY_SCHEMA,self.root,client=client)
        with self.assertRaisesRegex(ValueError,'previous_AI_attempt'):llm.bounded_task(self.state,payload,'local',llm.POLICY_SCHEMA,self.root,client=client)
        self.assertEqual(client.generate.call_count,1);self.assertGreater(llm.spending_totals(self.state)['day']['cost_usd'],0)

    def test_quality_report_and_mail_use_allowlisted_fields(self):
        job=self.add_job();report={'date_moscow':'2026-11-06','items':[{'hh_id':job['id'],'cv':{'profile':'MAIN'}}],
            'autonomous_dispatch':{'outcomes':[{'hh_id':job['id'],'status':'needs_review','reason':'required evidence missing'}]},'debug_secret':'PRIVATE_DEBUG_MARKER'}
        with patch.object(llm.OpenAIClient,'generate',side_effect=AssertionError('no paid report')):
            review=run.publish_quality_review(self.root,report)
        self.assertIn(job['page_url'],review);self.assertIn('required evidence missing',review);self.assertNotIn('PRIVATE_DEBUG_MARKER',review)
        subject,text,html=mail.mail_content(report)
        self.assertIn(review,text);self.assertIn('Подробный отчет для ChatGPT',html);self.assertNotIn('PRIVATE_DEBUG_MARKER',html)
        self.assertEqual(report['decision_funnel']['hard_daily_limit'],min(rt.settings(self.root)['max_applications_per_day'],rt.read(self.root,'dispatch-config.json')['max_confirmed_per_day']))

    def test_confirmed_send_survives_policy_change(self):
        job=self.add_job();self.application(job,'confirmed')
        row=self.state.db.execute('SELECT * FROM jobs WHERE key=?',(job['key'],)).fetchone()
        with patch.object(llm,'QUALITY_POLICY','new-policy'):
            self.assertEqual(daily.exclusion(self.state,row,{}),'confirmed_send')

    def test_one_and_two_resume_cycles_cache_and_remain_no_send(self):
        for multiple in (False,True):
            if multiple:cli.synthetic_profile(self.root,multiple=True)
            if not multiple:self.add_job()
            first=daily.daily_cycle(self.root,offline=True,check_forms=False)
            second=daily.daily_cycle(self.root,offline=True,check_forms=False)
            self.assertEqual(first['applications_sent'],0);self.assertEqual(second['applications_sent'],0)
            self.assertGreater(second['counts'].get('assessment_cache_hits',0),0)
            self.assertEqual(self.state.db.execute('SELECT count(*) FROM jobs').fetchone()[0],1)

    def test_complete_generation_independent_review_binding_and_reuse(self):
        self.configure_ai();self.config('llm-config.json',editorial_mode='cited_paraphrase')
        job=self.add_job();base=rt.read(self.root,'career-facts.json');facts={f['id']:f for f in base['facts']}
        for f in base['facts']:f['text']=f['letter_text']
        cli.save(self.root,'career-facts.json',base);facts={f['id']:f for f in base['facts']}
        body='Здравствуйте!\n\nНаправляю отклик на позицию «'+job['title']+'».\n\n'+'\n\n'.join(f['text'] for f in facts.values())
        raw={'hh_id':job['id'],'cv':'MAIN','letter_body':body+'\n\nС уважением','fact_ids':list(facts),'analysis':{'employer_need':'Анализ метрик','experience_focus':['Аналитика']},'questions':[]}
        review={'hh_id':job['id'],'coverage_complete':True,'professional_profile_present':True,'argumentation_clear':True,'generic_resume_replay':False,'natural':True,'task_relevant':True,'correct_cv':True,'personalized':True,'issues':[],'style_notes':[],
            'claims':[{'segment':i,'supported':True,'kind':'candidate_claim','vacancy_quotes':[],'evidence':[{'fact_id':fid,'source_indices':[0]} for fid in facts]} for i in range(len(llm.letter_segments(body)))],
            'task_links':[{'requirement_quote':job['description'],'letter_quote':facts['demo_metrics']['text'],'fact_ids':['demo_metrics'],'transfer_explanation':'Аналитический кейс соответствует задачам анализа метрик из полного описания вакансии.'}]}
        client=MagicMock();client.generate.side_effect=lambda payload:{'text':json.dumps({'results':[raw if payload['task']=='cover_letters' else review]}),'usage':{'input_tokens':10,'output_tokens':10}}
        packet={'vacancies':[{'hh_id':job['id'],'cv':'MAIN','title':job['title'],'employer':job['employer'],'full_description':job['description']}]}
        result=llm.prepare_automatic_letters(self.state,packet,self.root,client=client)
        self.assertEqual(client.generate.call_count,2);self.assertEqual(len(result['results']),1)
        approved=result['results'][0];self.assertEqual(approved['letter_body'],body);self.assertFalse(approved['submission_allowed'])
        self.assertEqual(approved['review_binding']['letter_sha256'],p.digest(body))
        self.assertEqual(approved['review_binding']['facts_sha256'],p.digest(base))
        llm.prepare_automatic_letters(self.state,packet,self.root,client=client)
        self.assertEqual(client.generate.call_count,2)
        changed=copy.deepcopy(job);changed['description']+=' Changed requirements.'
        self.assertNotEqual(approved['review_binding'],llm.review_binding(approved,changed,p.cv_choice('MAIN',self.cfg['candidate'],self.root),base))

    def test_owner_source_exclusion_and_signature_handling(self):
        self.assertFalse(llm.letter_source_allowed('x',{'verified':True,'source':'CV','text':'Own fact','allow_in_letters':False}))
        self.assertTrue(llm.letter_source_allowed('x',{'verified':True,'source':'CV','text':'Own fact'}))
        body='Содержательное окончание.'
        self.assertEqual(llm.strip_letter_signature(body+'\n\nKind regards,\nDemo Candidate'),body)
        self.assertEqual(llm.strip_letter_signature(body),body)

    def test_history_before_paid_generation_rejects_duplicate_or_incomplete_scan(self):
        job=self.add_job();entry={'hh_id':job['id']}
        for history,reason in [({'checked':True,'found':[job['id']]},'already_applied_in_HH'),({'checked':False},'HH_history_before_paid_generation_blocked')]:
            # Each subcase gets a fresh journal; confirmed HH history is durable.
            self.state.db.execute("DELETE FROM events WHERE stage='application_exists_hh'");self.state.db.commit()
            browser=MagicMock();browser.call.return_value=history
            with patch.object(ab,'queue_candidates',return_value=[entry]),patch.object(ab,'salary_readiness',return_value=[]),patch('hh_api.HHClient') as api,patch('hh_api.normalize_job',return_value=job),patch.object(ce,'standalone_chrome',return_value=browser),patch.object(llm,'prepare_automatic_letters',side_effect=AssertionError('no paid generation')):
                api.return_value.detail.return_value={'archived':False}
                result=ab.prepare_automatic_queue(self.root)
            self.assertFalse(result['verified_queue_ids']);self.assertTrue(any(reason in x['reason'] for x in result['preparation_exclusions']))
            browser.finish.assert_called_once()

    def test_mandatory_evidence_blocks_before_history_and_paid_generation(self):
        job=self.add_job();job['description']+='\nТребования:\nОпыт от 8 лет в промышленной робототехнике.'
        self.state.db.execute('UPDATE jobs SET data=? WHERE key=?',(json.dumps(job),job['key']));self.state.db.commit()
        with patch.object(ab,'queue_candidates',return_value=[{'hh_id':job['id']}]),patch.object(ab,'salary_readiness',return_value=[]),patch('hh_api.HHClient') as api,patch('hh_api.normalize_job',return_value=job),patch.object(ce,'standalone_chrome',side_effect=AssertionError('not yet eligible')),patch.object(llm,'prepare_automatic_letters',side_effect=AssertionError('no paid generation')):
            api.return_value.detail.return_value={'archived':False}
            result=ab.prepare_automatic_queue(self.root)
        self.assertFalse(result['matched_queue_ids']);self.assertTrue(any('mandatory_qualification_requires_evidence' in x['reason'] for x in result['preparation_exclusions']))

    def test_user_answer_rejects_stale_card_context_and_utf16_overflow(self):
        job=self.add_job();q={'prompt':'Когда вы готовы приступить к работе?','type':'textarea','required':True,'max_length':3}
        ab.resolve_form_questions(self.state,[q],{},job['id'],self.root,allow_ai=False)
        item=ab.question_review_items(self.state)[0]
        with self.assertRaisesRegex(ValueError,'card_changed'):ab.approve_question_answer(self.state,self.root,item['id'],'Да',expected_revision='stale')
        with self.assertRaisesRegex(ValueError,'exceeds_field'):ab.approve_question_answer(self.state,self.root,item['id'],'😀😀',expected_revision=item['updated_at'])
        base=rt.read(self.root,'career-facts.json');base['facts'][0]['text']+=' Changed.';cli.save(self.root,'career-facts.json',base)
        with self.assertRaisesRegex(ValueError,'context_changed'):ab.approve_question_answer(self.state,self.root,item['id'],'Да',expected_revision=item['updated_at'])

    def test_apps_script_uses_exact_owner_recipient_and_receipt(self):
        import hashlib
        from email.message import EmailMessage
        from io import BytesIO
        msg=EmailMessage();msg['To']='owner@example.invalid';msg['Subject']='Fictional report'
        msg.set_content('Local test');msg.add_alternative('<p>Local test</p>',subtype='html')
        attachment=b'<html>fictional</html>';msg.add_attachment(attachment,maintype='text',subtype='html',filename='digest.html')
        url='https://script.google.com/macros/s/FICTIONAL_DEPLOYMENT/exec'
        response={'status':'mailapp_accepted','report_id':'daily:2026-11-06','recipient':'owner@example.invalid','digest_sha256':hashlib.sha256(attachment).hexdigest()}
        with patch.object(mail,'tls_context',return_value=None),patch('urllib.request.build_opener') as opener:
            opener.return_value.open.return_value=BytesIO(json.dumps(response).encode())
            result=mail.apps_script_transport(msg,'2026-11-06',url,'fictional-token',recipient='owner@example.invalid')
            self.assertEqual(result['status'],'mailapp_accepted');self.assertEqual(opener.return_value.open.call_count,1)
            with self.assertRaisesRegex(ValueError,'recipient_must_be_fixed'):
                mail.apps_script_transport(msg,'2026-11-06',url,'fictional-token',recipient='different@example.invalid')
            self.assertEqual(opener.return_value.open.call_count,1)
            response['recipient']='different@example.invalid';opener.return_value.open.return_value=BytesIO(json.dumps(response).encode())
            with self.assertRaisesRegex(ValueError,'receipt_not_confirmed'):
                mail.apps_script_transport(msg,'2026-11-06',url,'fictional-token',recipient='owner@example.invalid')

    def test_question_ui_rejects_cross_origin_host_and_missing_token(self):
        captured={}
        class Server:
            server_port=12345
            def __init__(inner,address,handler):captured.update(address=address,handler=handler)
            def serve_forever(inner,**kw):pass
            def server_close(inner):pass
        with patch('http.server.HTTPServer',Server),patch('secrets.token_urlsafe',return_value='fictional-review-token'),patch.object(ab,'approve_question_answer',side_effect=AssertionError('untrusted request')):
            ab.serve_question_review(self.root,open_browser=False)
            self.assertEqual(captured['address'],('127.0.0.1',0))
            for changes in ({'Host':'remote.invalid:12345'},{'Origin':'https://remote.invalid'},{'X-Review-Token':''}):
                handler=captured['handler'].__new__(captured['handler']);handler.server=SimpleNamespace(server_port=12345);handler.path='/approve'
                handler.headers={'Host':'127.0.0.1:12345','Origin':'http://127.0.0.1:12345','X-Review-Token':'fictional-review-token',**changes}
                handler.reply=MagicMock();handler.do_POST();self.assertEqual(handler.reply.call_args.args[0],403)
