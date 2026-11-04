import copy
import importlib
import json
from pathlib import Path
import plistlib
import socket
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import portable as cli
import portable_runtime as rt
import pipeline as p
import hh_api as h
import daily_prepare as daily
import daily_delivery as mail
import llm_prepare as llm
import application_batch as ab


class PortableTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='jsa-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.cfg=cli.synthetic_profile(self.root)
        self.boundary=cli.offline_boundary();self.boundary.__enter__()
        self.addCleanup(self.boundary.__exit__,None,None,None)

    def policy(self,**changes):
        policy=rt.settings(self.root);policy.update(changes);cli.save(self.root,'portable-config.json',policy)

    def candidate(self):return p.load_candidate(self.cfg['candidate'],self.root)

    def job(self):
        return p.normalize({'provider':'hh','board':'public'},{'id':'900000001','name':'Аналитик продукта',
            'employer':{'name':'Вымышленный работодатель'},'area':{'name':'Демоград'},
            'alternate_url':'https://hh.ru/vacancy/900000001','work_format':[{'id':'REMOTE','name':'Удалённо'}],
            'salary':{'from':80000,'to':120000,'currency':'RUR','gross':False,'mode':{'id':'MONTH'}},'_detail_state':'full',
            'archived':False,'description':'Обязанности:\nАнализ продуктовых метрик и подготовка материалов для команды.\nАнализ потребностей рынка и проверка исходных предположений.\nТребования:\nОпыт анализа продуктовых метрик.\nОпыт анализа потребностей рынка.'})

    def test_empty_database_and_identity(self):
        before=rt.identity(self.root);cli.initialize(self.root)
        self.assertEqual(before,rt.identity(self.root))
        con=sqlite3.connect(self.root/'var/jobs.sqlite3');self.addCleanup(con.close)
        for table in ['jobs','applications','events','runs']:
            self.assertEqual(con.execute('SELECT count(*) FROM '+table).fetchone()[0],0)

    def test_defaults_disable_all_external_actions(self):
        for action in ['hh','browser','ai','dispatch','mail']:
            with self.subTest(action=action),self.assertRaises(ValueError):rt.require(self.root,action)

    def test_doctor_reads_no_secrets_or_network(self):
        result=cli.doctor(self.root)
        self.assertEqual(result['profile'],'valid');self.assertTrue(result['database_identity_matches'])
        self.assertFalse(result['secret_access']);self.assertEqual(result['network_calls'],0)

    def test_one_resume_is_enough(self):
        rt.validate_profile(self.root)
        match=p.score(self.job(),self.candidate())
        self.assertEqual(set(match['profiles']),{'MAIN'});self.assertEqual(match['best_profile'],'MAIN')
        self.assertTrue(p.cv_choice('MAIN',self.candidate(),self.root)['available'])

    def test_multiple_resumes_select_second(self):
        cli.synthetic_profile(self.root,multiple=True)
        cfg=rt.validate_profile(self.root);job=self.job();job['title']='Координатор проектов'
        match=p.score(job,p.load_candidate(cfg['candidate'],self.root))
        self.assertEqual(match['best_profile'],'SECOND');self.assertEqual(len(match['profiles']),2)

    def test_resume_content_change_changes_hash(self):
        first=p.cv_choice('MAIN',self.candidate(),self.root)
        (self.root/'cv/demo.txt').write_text('Обновленное вымышленное резюме')
        self.assertNotEqual(first['sha256'],p.cv_choice('MAIN',self.candidate(),self.root)['sha256'])

    def test_resume_cannot_escape_project(self):
        candidate=self.candidate();candidate['cvs']['MAIN']='../outside.txt'
        with self.assertRaises(ValueError):p.cv_choice('MAIN',candidate,self.root)

    def test_facts_cannot_escape_project(self):
        candidate=self.cfg['candidate'].copy();candidate['facts_file']='../outside.json'
        with self.assertRaises(ValueError):p.load_candidate(candidate,self.root)

    def test_own_capability_patterns(self):
        cfg=copy.deepcopy(self.cfg)
        cfg['candidate']['capability_patterns']={'custom_reporting':r'отчетност|reporting'}
        cfg['candidate']['profiles']['MAIN']['capabilities'].append('custom_reporting')
        facts=rt.read(self.root,'career-facts.json');facts['facts'][0]['capabilities'].append('custom_reporting')
        cli.save(self.root,'career-facts.json',facts);cli.save(self.root,'pipeline-config.hh.json',cfg)
        rt.validate_profile(self.root);self.assertIn('custom_reporting',p.CAPABILITIES)

    def test_unverified_facts_are_never_letter_evidence(self):
        candidate=self.candidate();candidate['facts'][0]['verified']=False
        self.assertEqual(len(p.verified_facts(candidate)),1)
        letter=p.build_letter(self.job(),p.score(self.job(),candidate),candidate)
        self.assertEqual(letter['body'],'')

    def test_salary_uses_configured_floor(self):
        self.assertTrue(p.filters(self.job(),self.candidate(),self.cfg['filters'])['passed'])
        policy={**self.cfg['filters'],'min_monthly_rub':150000}
        self.assertFalse(p.filters(self.job(),self.candidate(),policy)['passed'])

    def test_gross_and_net_are_distinct(self):
        job=self.job();job['salary']['gross']=True
        gross=p.salary_assessment(job,80000,'GROSS');net=p.salary_assessment(job,80000,'NET')
        self.assertNotEqual(gross['status'],net['status'])

    def test_location_and_work_format_criteria(self):
        job=self.job();job['workplace']='on-site';job['locations']=['Другой город']
        result=p.filters(job,self.candidate(),self.cfg['filters'])
        self.assertIn('location_not_allowed',result['reasons'])
        policy={**self.cfg['filters'],'allowed_work_formats':['remote']}
        self.assertIn('work_format_not_allowed',p.filters(job,self.candidate(),policy)['reasons'])

    def test_exclusions_and_requirements(self):
        policy={**self.cfg['filters'],'exclude_title_patterns':['аналитик'],'required_text_patterns':['несуществующее требование']}
        result=p.filters(self.job(),self.candidate(),policy)
        self.assertIn('excluded_title',result['reasons']);self.assertIn('required_condition_unconfirmed',result['reasons'])

    def test_english_level_uses_own_profile(self):
        self.assertFalse(p.english_assessment('Требования:\nEnglish C1 required',{'english_level':'C1'})['critical_mismatches'])
        self.assertTrue(p.english_assessment('Требования:\nEnglish C1 required',{'english_level':'B1'})['critical_mismatches'])

    def test_real_HH_blocked_before_secret_access(self):
        with self.assertRaisesRegex(ValueError,'hh_disabled'):h.HHClient(self.root)

    def test_one_query_one_profile_HH_collection(self):
        self.policy(profile_confirmed=True,hh_enabled=True)
        class Store:
            value={'control_success':True,'contact_email':'demo@example.invalid','access_token':'fictional-token'}
            def load(self):return self.value.copy()
            def save(self,value):self.value=value
        class Transport:
            requests=[]
            def request(inner,method,path,**kwargs):
                inner.requests.append((method,path))
                if path=='/vacancies':return {'items':[{'id':'900000001','name':'Аналитик продукта'}],'found':1,'pages':1,'page':0}
                return {'id':'900000001','name':'Аналитик продукта','description':'Полное вымышленное описание','archived':False}
        client=h.HHClient(self.root,store=Store(),transport=Transport())
        jobs,complete=client.collect(self.cfg['sources'][0])
        self.assertFalse(complete);self.assertEqual(len(jobs),1)
        self.assertEqual(jobs[0]['_detail_state'],'full')
        self.assertEqual(client.collection['control_profiles'],['MAIN'])

    def test_HH_rate_limit_stops_and_sets_cooldown(self):
        self.policy(profile_confirmed=True,hh_enabled=True)
        class Store:
            value={'contact_email':'demo@example.invalid','access_token':'fictional-token'}
            def load(inner):return inner.value.copy()
            def save(inner,value):inner.value=value
        class Transport:
            requests=[]
            def request(inner,*a,**k):raise h.HHError('rate_limited',429)
        store=Store();client=h.HHClient(self.root,store=store,transport=Transport())
        with self.assertRaises(h.HHError):client.get('/vacancies')
        self.assertGreater(store.value['blocked_until'],0)
        with self.assertRaisesRegex(h.HHError,'cooldown'):client.get('/vacancies')

    def test_HH_collection_obeys_portable_limits(self):
        self.policy(profile_confirmed=True,hh_enabled=True,max_vacancies=1,max_search_requests=1)
        class Store:
            def load(inner):return {'control_success':True,'contact_email':'demo@example.invalid','access_token':'fictional-token'}
        class Transport:
            requests=[]
            def request(inner,method,path,**kwargs):
                inner.requests.append((method,path))
                if path=='/vacancies':return {'items':[{'id':'900000001'},{'id':'900000002'}],'found':100,'pages':10}
                return {'id':'900000001','description':'Вымышленное полное описание'}
        source=copy.deepcopy(self.cfg['sources'][0]);source.update(max_pages=2,per_page=5)
        source['searches'].append({'id':'second-query','profile':'MAIN','text':'другой запрос'})
        client=h.HHClient(self.root,store=Store(),transport=Transport())
        jobs,_=client.collect(source)
        self.assertEqual(len(jobs),1)
        self.assertEqual(sum(path=='/vacancies' for _,path in client.transport.requests),1)
        self.assertEqual(client.collection['detail_requests'],1)

    def test_AI_blocked_even_with_env_key(self):
        with patch.dict('os.environ',{'OPENAI_API_KEY':'fictional-key'}):
            with self.assertRaisesRegex(ValueError,'ai_disabled'):llm.OpenAIClient(llm.settings(self.root))

    def test_AI_requires_reviewed_prices(self):
        self.policy(profile_confirmed=True,ai_enabled=True)
        cfg=rt.read(self.root,'llm-config.json');cfg.update(enabled=True,spending_approval='owner-test')
        cli.save(self.root,'llm-config.json',cfg)
        with self.assertRaisesRegex(ValueError,'reviewed_prices'):rt.require(self.root,'ai')

    def test_responses_schema_binds_actual_resume_keys(self):
        request=json.loads(llm.encoded_request({'vacancies':[]},llm.settings(self.root)))
        cv=request['text']['format']['schema']['properties']['results']['items']['properties']['cv']
        self.assertEqual(cv['enum'],['MAIN']);self.assertFalse(request['store'])

    def test_budget_reserves_before_call_and_keeps_uncertainty(self):
        state=p.State(self.root/'var/jobs.sqlite3');self.addCleanup(state.close)
        cfg=llm.settings(self.root);cfg['max_cost_usd_per_day']=1
        payload={'vacancies':[{'hh_id':'900000001'}]}
        reservation=llm.reserve(state,'real',cfg,payload)
        self.assertIsNotNone(reservation)
        self.assertIsNone(llm.settle(state,reservation,'real',cfg,{}))
        self.assertIsNone(llm.reserve(state,'real',cfg,payload))
        totals=llm.spending_totals(state)
        self.assertEqual(totals['day']['calls'],1);self.assertGreater(totals['day']['cost_usd'],0)

    def test_daily_slots_count_uncertain_sends(self):
        state=p.State(self.root/'var/jobs.sqlite3');self.addCleanup(state.close)
        key=ab.reserve_daily_slot(state,'900000001',limit=1)
        ab.settle_daily_slot(state,key,'900000001','send_uncertain')
        self.assertIsNone(ab.reserve_daily_slot(state,'900000002',limit=1))
        self.assertIsNone(ab.reserve_daily_slot(state,'900000001',limit=1))

    def test_dispatch_blocked_before_browser(self):
        with self.assertRaisesRegex(ValueError,'dispatch_disabled'):ab.automatic_dispatch(self.root)

    def test_submit_method_has_own_gate(self):
        from chrome_executor import ChromeExecutor
        executor=object.__new__(ChromeExecutor);executor.root=self.root
        with self.assertRaisesRegex(ValueError,'dispatch_disabled'):executor.submit_once({},[])

    def test_owned_browser_disabled_and_personal_browser_forbidden(self):
        from chrome_executor import Chrome,standalone_chrome
        with self.assertRaisesRegex(ValueError,'browser_disabled'):standalone_chrome(self.root)
        with self.assertRaisesRegex(ValueError,'Apple_Events_disabled'):Chrome().osa('anything')

    def test_mail_preview_never_sends(self):
        report={'date_moscow':'2026-01-01','status':'completed','items':[],'counts':{}}
        folder=self.root/'var/daily-digests/2026-01-01';folder.mkdir(parents=True)
        (folder/'digest.html').write_text('<html lang="ru">Вымышленный отчет</html>')
        result=mail.deliver(self.root,report)
        self.assertEqual(result['status'],'preview_only');self.assertEqual(result['email_send_calls'],0)
        self.assertTrue((self.root/'var/daily-email-preview.eml').is_file())

    def test_mail_test_cannot_bypass_owner_switch(self):
        report={'date_moscow':'2026-01-01','status':'completed','items':[],'counts':{}}
        folder=self.root/'var/daily-digests/2026-01-01';folder.mkdir(parents=True)
        (folder/'digest.html').write_text('<html>Демо</html>')
        with self.assertRaisesRegex(ValueError,'mail_disabled'):mail.deliver(self.root,report,one_test=True)

    def test_render_schedule_never_installs(self):
        with patch('subprocess.run',side_effect=AssertionError('no_launchctl')):
            result=cli.render_schedule(self.root)
        self.assertFalse(result['installed'])
        plist=plistlib.loads(Path(result['rendered']).read_bytes())
        self.assertFalse(plist['RunAtLoad']);self.assertEqual(plist['ProgramArguments'][-1],'daily')
        self.assertEqual(plist['WorkingDirectory'],str(self.root.resolve()))

    def test_network_guard_actually_blocks_sockets(self):
        with self.assertRaises(AssertionError):socket.socket()

    def test_sourced_drafts_contain_only_selected_facts(self):
        candidate=self.candidate();facts={f['id']:f for f in candidate['facts']}
        fit=ab.content_fit(self.job(),candidate,facts,salary_policy=self.cfg['filters'])
        self.assertTrue(fit['eligible']);letter,error=ab.task_letter(self.job(),fit,facts)
        self.assertIsNone(error)
        for fid in letter['fact_ids']:self.assertIn(facts[fid]['letter_text'],letter['body'])

    def test_full_offline_pipeline_deduplicates_and_drafts(self):
        result=cli.demo(self.root)
        self.assertEqual(result['demo'],'PASS');self.assertTrue(result['draft_prepared'])
        self.assertEqual(result['repeat_unchanged'],1);self.assertEqual(result['applications_sent'],0)
        self.assertEqual(cli.doctor(self.root)['database_counts']['jobs'],0)

    def test_database_from_another_installation_is_rejected(self):
        self.policy(profile_confirmed=True,hh_enabled=True,installation_id='0'*32)
        with self.assertRaisesRegex(ValueError,'database_identity_mismatch'):rt.require(self.root,'hh')

    def test_plain_text_resume_context(self):
        state=p.State(self.root/'var/jobs.sqlite3');self.addCleanup(state.close)
        cv=p.cv_choice('MAIN',self.candidate(),self.root)
        self.assertIn('Вымышленное резюме',llm.cv_text(state,self.root,cv))

    def render_report_settings(self):
        import daily_run
        report={'date_moscow':'2026-01-01','generated_at':'2026-01-01T00:00:00+00:00',
            'items':[],'counts':{},'important_exceptions':[]}
        daily_run.publish_priorities(self.root,report)
        return [(self.root/name).read_text() for name in [
            'var/daily-digest.html','var/daily-digest.md','var/daily-digests/2026-01-01/digest.html']]

    def test_report_displays_owner_salary_budget_timezone_and_dispatch_limit(self):
        cfg=copy.deepcopy(self.cfg)
        cfg['filters'].update(min_monthly_rub=123456,desired_monthly_rub=234567,salary_basis='GROSS')
        cli.save(self.root,'pipeline-config.hh.json',cfg)
        self.policy(timezone='Asia/Tokyo',max_applications_per_day=9)
        dispatch=rt.read(self.root,'dispatch-config.json');dispatch['max_confirmed_per_day']=3
        cli.save(self.root,'dispatch-config.json',dispatch)
        llm_cfg=rt.read(self.root,'llm-config.json')
        llm_cfg.update(max_cost_usd_per_day=0.07,max_cost_usd_per_month=2.5,max_calls_per_day=4)
        cli.save(self.root,'llm-config.json',llm_cfg)
        for text in self.render_report_settings():
            self.assertIn('Минимальный фикс для отбора: 123 456 ₽ GROSS.',text)
            self.assertIn('Ожидания при отклике: 234 567 ₽ GROSS.',text)
            self.assertIn('Лимиты: $0.07 / сутки, $2.5 / месяц; вызовов в сутки: 4.',text)
            self.assertIn('За сутки (Asia/Tokyo):',text)
            self.assertIn('Дневной лимит подтвержденных откликов: 3.',text)

    def test_report_never_invents_missing_salary_amounts(self):
        for value in [None,0,'missing']:
            cfg=copy.deepcopy(self.cfg)
            for key in ['min_monthly_rub','desired_monthly_rub']:
                if value=='missing':cfg['filters'].pop(key,None)
                else:cfg['filters'][key]=value
            cli.save(self.root,'pipeline-config.hh.json',cfg)
            for text in self.render_report_settings():
                self.assertIn('Ожидания при отклике: не заданы.',text)
                self.assertNotIn('₽',text)

    def test_report_reloads_config_and_does_not_use_salary_floor_as_expectation(self):
        cfg=copy.deepcopy(self.cfg)
        cfg['filters'].update(min_monthly_rub=76543,desired_monthly_rub=0,salary_basis='NET')
        cli.save(self.root,'pipeline-config.hh.json',cfg)
        for text in self.render_report_settings():
            self.assertIn('Минимальный фикс для отбора: 76 543 ₽ NET.',text)
            self.assertIn('Ожидания при отклике: не заданы.',text)
        cfg['filters']['desired_monthly_rub']=87654
        cli.save(self.root,'pipeline-config.hh.json',cfg)
        self.policy(max_applications_per_day=2)
        llm_cfg=rt.read(self.root,'llm-config.json');llm_cfg['max_cost_usd_per_day']=0.09
        cli.save(self.root,'llm-config.json',llm_cfg)
        for text in self.render_report_settings():
            self.assertIn('Ожидания при отклике: 87 654 ₽ NET.',text)
            self.assertIn('Лимиты: $0.09 / сутки',text)
            self.assertIn('Дневной лимит подтвержденных откликов: 2.',text)

    def test_full_daily_report_and_mail_preview_single_resume(self):
        self.check_daily_report(False)

    def test_full_daily_report_multiple_resumes(self):
        self.check_daily_report(True)

    def check_daily_report(self,multiple):
        import daily_run
        if multiple:
            cli.synthetic_profile(self.root,multiple=True)
            self.cfg=rt.read(self.root,'pipeline-config.hh.json')
        profiles={}
        for key in self.cfg['candidate']['cvs']:
            observed={'resume_id':'f'*32 if key=='MAIN' else 'e'*32,'title':'Вымышленный профиль '+key,
                'experience':['Вымышленный опыт'],'skills':['Вымышленный навык']}
            version=p.digest(observed);source='cv/hh-'+key+'.json'
            cli.save(self.root,source,{'profiles':{key:observed}})
            profiles[key]={'authoritative_source':'authorized_HH_resume_DOM','source_file':source,
                'hh_version':version,'hh_resume_id':observed['resume_id'],'title_verified':observed['title']}
        cli.save(self.root,'cv/current-cv-manifest.json',{'profiles':profiles})
        raw={'id':'900000001','name':'Аналитик продукта','employer':{'name':'Вымышленный работодатель'},
            'description':self.job()['description'],'area':{'name':'Демоград'},
            'work_format':[{'id':'REMOTE'}],'salary':{'from':80000,'to':120000,'gross':False,'currency':'RUR','mode':{'id':'MONTH'}},
            'archived':False,'published_at':'2026-01-01T00:00:00+0000'}
        class FakeHH:
            root=self.root
            def get(inner,*args,**kwargs):return {'items':[raw],'found':1,'pages':1,'page':0}
            def detail(inner,vid):return raw
        with patch.object(h,'HHClient',return_value=FakeHH()):
            report=daily_run.run(self.root)
        self.assertEqual(report['status'],'completed',report)
        self.assertEqual(len(report['items']),1)
        self.assertEqual(report['items'][0]['cv']['profile'],'MAIN')
        self.assertTrue((self.root/'var/daily-digest.html').is_file())
        summary=daily_run.finish_daily(self.root,report,offline=True)
        self.assertEqual(summary['applications_sent'],0)
        self.assertEqual(summary['mail']['email_send_calls'],0)
        self.assertEqual(summary['mail']['status'],'preview_only')
        self.assertTrue((self.root/'var/daily-email-preview.eml').is_file())


if __name__=='__main__':unittest.main()
