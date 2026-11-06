"""Question answers with synthetic facts and fake model responses only."""
import json
from unittest import TestCase
from unittest.mock import patch,MagicMock
import application_batch as ab
import daily_prepare as daily
import daily_run
import llm_prepare as llm
import pipeline as p
from update_fixtures import Fixture
LEAD='В вымышленной компании я руководил аналитической командой. Планировал загрузку и проверял качество нескольких параллельных проектов.'
DIGITAL='В вымышленной компании я разрабатывал требования к CRM вместе с командой. Прототип аналитического инструмента оставался разработкой без промышленного внедрения.'
class QuestionAnswersTests(TestCase):
    add_job = Fixture.add_job
    configure_ai = Fixture.configure_ai
    policy = Fixture.policy
    candidate = Fixture.candidate

    def setUp(self):
        Fixture.setUp(self)
        self.configure_ai()
        self.job = self.add_job('1')
        base = json.loads((self.root / 'career-facts.json').read_text())
        base['facts'] = [{'id': fid, 'text': text, 'verified': True, 'source': 'fictional confirmed fixture', 'capabilities': ['product_metrics']} for fid, text in [('lead', LEAD), ('digital', DIGITAL)]]
        base['facts'].append({'id': 'fun', 'text': 'Synthetic excluded source', 'verified': True, 'source': 'fictional', 'allow_in_letters': False})
        (self.root / 'career-facts.json').write_text(json.dumps(base))
        self.known = {'salary': {'value': '90 000 ₽ NET в месяц, фикс', 'source': 'direct fictional fixture', 'verified': True, 'scope': 'global'}}
        self.client = MagicMock()
        self.cvpatch = patch.object(llm, 'cv_text', return_value='Exact synthetic current CV MAIN fixture')
        self.cvpatch.start()
        self.addCleanup(self.cvpatch.stop)

    def model(self, text, fid):

        def generate(payload):
            self.assertNotIn('fun', payload['facts'])
            if payload['task'] == 'prepare_HH_answers':
                answer = {'index': payload['questions'][0]['index'], 'answerable': True, 'category': 'career_fact', 'text': text, 'fact_ids': [fid], 'selected_values': [], 'gap': ''}
                result = {'answers': [answer]}
            else:
                result = {'answers': [{'index': a['index'], **{k: True for k in ('coverage_complete', 'complete', 'correct_cv', 'relevant', 'field_limit_ok', 'correct_choice')}, 'claims': [{'segment': i, 'supported': True, 'kind': 'candidate_claim', 'vacancy_quotes': [], 'evidence': [{'fact_id': fid, 'source_indices': [0]}]} for i in range(len(llm.letter_segments(a['text'])))], 'issues': []} for a in payload['answers']]}
            return {'text': json.dumps(result), 'usage': {'input_tokens': 500, 'output_tokens': 100}}
        self.client.generate.side_effect = generate

    def test_leadership_and_digital_prose_independent_review_cache_and_common_budget(self):
        for fid, text, prompt in [('lead', LEAD, 'Расскажите, как вы руководили исследовательской командой?'), ('digital', DIGITAL, 'Расскажите о развитии цифровых продуктов и AI-инструментов?')]:
            self.model(text, fid)
            q = {'prompt': prompt, 'type': 'textarea', 'required': None, 'max_length': 2000}
            known = ab.resolve_form_questions(self.state, [q], self.known, '1', self.root, client=self.client)
            answers, unknown = ab.standard_answers([q], known, '1')
            self.assertFalse(unknown)
            self.assertEqual(answers[0]['answer'], text)
            count = self.client.generate.call_count
            again = ab.resolve_form_questions(self.state, [q], known, '1', self.root, client=self.client)
            self.assertEqual(ab.standard_answers([q], again, '1')[0][0]['answer'], text)
            self.assertEqual(self.client.generate.call_count, count)
        self.assertEqual(self.client.generate.call_count, 4)
        self.assertEqual(llm.spending_totals(self.state)['day']['calls'], 4)
        self.assertEqual(self.state.db.execute('SELECT count(*) FROM applications').fetchone()[0], 0)

    def test_unknown_user_confirmation_scope_card_and_form_change(self):
        q = {'prompt': 'Готовы ли вы к гибридному формату 3 дня в офисе?', 'type': 'radio', 'required': True, 'options': [{'text': 'Да', 'value': 'yes'}, {'text': 'Нет', 'value': 'no'}]}
        resolved = ab.resolve_form_questions(self.state, [q], self.known, '1', self.root, client=self.client)
        self.assertFalse(self.client.generate.called)
        item = ab.question_review_items(self.state)[0]
        self.assertEqual(item['category'], 'WAITING_FOR_USER_ANSWER')
        html = ab.question_review_html([item], token='local-test')
        self.assertIn('Подтвердить ответ и продолжить отклик', html)
        self.assertIn(q['prompt'], html)
        with self.assertRaisesRegex(ValueError, 'vacancy_scoped'):
            ab.approve_question_answer(self.state, self.root, item['id'], ['yes'], reuse=True, expected_revision=item['updated_at'])
        ab.approve_question_answer(self.state, self.root, item['id'], ['yes'], expected_revision=item['updated_at'])
        resolved = ab.resolve_form_questions(self.state, [q], resolved, '1', self.root, client=self.client)
        answers, unknown = ab.standard_answers([q], resolved, '1')
        self.assertFalse(unknown)
        self.assertEqual(answers[0]['selected_values'], ['yes'])
        self.assertEqual(ab.pending_question_resumes(self.state, self.root), ['1'])
        changed = {**q, 'options': q['options'] + [{'text': 'Другое', 'value': 'other'}]}
        self.assertTrue(ab.standard_answers([changed], resolved, '1')[1])
        self.assertTrue(ab.standard_answers([q], resolved, '2')[1])
        self.assertFalse(self.client.generate.called)

    def test_legal_test_and_technical_are_not_user_fact_requests(self):
        qs = [{'prompt': 'Выполните тестовое задание', 'type': 'textarea', 'required': True}, {'prompt': 'Согласны на обработку персональных данных?', 'type': 'checkbox', 'required': True}, {'prompt': 'unread_choice_question', 'type': 'radio', 'required': True}]
        ab.resolve_form_questions(self.state, qs, self.known, '1', self.root, client=self.client)
        categories = {x['question']['prompt']: x['category'] for x in ab.question_review_items(self.state)}
        self.assertEqual(categories[qs[0]['prompt']], 'REQUIRES_USER_ACTION')
        self.assertEqual(categories[qs[1]['prompt']], 'REQUIRES_USER_ACTION')
        self.assertEqual(categories[qs[2]['prompt']], 'BLOCKED_TECHNICAL')
        self.assertFalse(self.client.generate.called)
        with self.assertRaises(ValueError):
            ab.approve_question_answer(self.state, self.root, ab.question_review_items(self.state)[0]['id'], 'Да')
        ab.resolve_form_questions(self.state, [{'prompt': 'Опишите управление командой', 'required': True}], self.known, 'missing', self.root, client=self.client)
        self.assertEqual(ab.question_review_items(self.state)[-1]['category'], 'BLOCKED_TECHNICAL')

    def test_standard_salary_no_ai_and_no_net_gross_guess(self):
        qs = [{'prompt': 'Зарплатные ожидания', 'type': 'textarea', 'required': True}]
        known = ab.resolve_form_questions(self.state, qs, self.known, '1', self.root, client=self.client)
        self.assertEqual(ab.standard_answers(qs, known, '1')[0][0]['answer'], self.known['salary']['value'])
        self.assertFalse(self.client.generate.called)
        for prompt in ('Зарплатные ожидания GROSS', 'Зарплатные ожидания NET'):
            self.assertTrue(ab.standard_answers([{'prompt': prompt, 'type': 'number', 'required': True}], known, '1')[1])

    def test_proof_field_limit_and_known_english_guards(self):
        self.model(LEAD, 'lead')
        q = {'prompt': 'Расскажите о руководстве командой', 'type': 'textarea', 'required': True}
        known = ab.resolve_form_questions(self.state, [q], self.known, '1', self.root, client=self.client)
        record = known['_resolved_questions']['1'][ab.question_hash(q)]
        record['value'] = 'Вымышленный ответ.'
        self.assertTrue(ab.standard_answers([q], known, '1')[1])
        short = {'prompt': 'Зарплатные ожидания', 'required': True, 'max_length': 3}
        self.assertTrue(ab.standard_answers([short], self.known, '1')[1])
        base = json.loads((self.root / 'career-facts.json').read_text())
        base.update(english_level='B2', english_source='direct confirmed fixture')
        (self.root / 'career-facts.json').write_text(json.dumps(base))
        english = {'prompt': 'Ваш уровень английского?', 'type': 'radio', 'required': True, 'options': [{'text': 'B1 — Intermediate', 'value': 'b1'}, {'text': 'B2 — Upper Intermediate', 'value': 'b2'}]}
        before = self.client.generate.call_count
        known = ab.resolve_form_questions(self.state, [english], self.known, '1', self.root, client=self.client)
        self.assertEqual(ab.standard_answers([english], known, '1')[0][0]['selected_values'], ['b2'])
        self.assertEqual(self.client.generate.call_count, before)

    def test_unsupported_claim_bounded_repair_not_approved(self):
        self.model(LEAD, 'lead')
        original = self.client.generate.side_effect

        def reject(payload):
            response = original(payload)
            if payload['task'] == 'check_HH_answers':
                data = json.loads(response['text'])
                data['answers'][0]['claims'][0]['supported'] = False
                response['text'] = json.dumps(data)
            return response
        self.client.generate.side_effect = reject
        q = {'prompt': 'Опишите руководство командой', 'type': 'textarea', 'required': True}
        known = ab.resolve_form_questions(self.state, [q], self.known, '1', self.root, client=self.client)
        self.assertTrue(ab.standard_answers([q], known, '1')[1])
        self.assertEqual(self.client.generate.call_count, 3)
        self.assertEqual(ab.question_review_items(self.state)[0]['category'], 'NEEDS_ANSWER_REWORK')
        ab.resolve_form_questions(self.state, [q], known, '1', self.root, client=self.client)
        self.assertEqual(self.client.generate.call_count, 3)

    def test_live_daily_and_monthly_hard_limits_precede_client(self):
        payload = {'vacancies': [{'hh_id': '1'}], 'task': 'prepare_HH_answers'}
        stale = {**llm.settings(self.root), 'max_cost_usd_per_day': 5.0, 'max_cost_usd_per_month': 50.0}
        for day, month in [(0.99999, 0.99999), (0.0, 19.99999)]:
            totals = {'day': {'calls': 0, 'cost_usd': day}, 'month': {'calls': 0, 'cost_usd': month}}
            with patch.object(llm, 'spending_totals', return_value=totals):
                self.assertIsNone(llm.reserve(self.state, 'real', stale, payload))
                with self.assertRaisesRegex(ValueError, 'budget_exhausted'):
                    llm.bounded_task(self.state, payload, 'fixture', llm._object({}), self.root, client=self.client)
        self.assertFalse(self.client.generate.called)
        self.assertEqual(self.state.db.execute("SELECT count(*) FROM events WHERE stage='llm_call_reserved'").fetchone()[0], 0)

    def test_completed_daily_resumes_only_explicit_answer_without_search_or_reset(self):
        day = llm.spending_totals(self.state)['date_moscow']
        key = 'daily:execution:' + day
        previous = {'status': 'completed', 'applications_sent': 8}
        daily.remember(self.state, key, previous)
        daily.remember(self.state, 'application:question-resume:1', {'status': 'pending'})
        self.state.db.commit()
        with patch.object(ab, 'resume_question_queue', return_value={'status': 'answers_resumed', 'applications_sent': 0}) as resume, patch.object(daily_run, 'run', side_effect=AssertionError('no search')):
            result = daily_run.execute_daily(self.root)
        resume.assert_called_once()
        self.assertEqual(result['status'], 'answers_resumed')
        self.assertEqual(daily.setting(self.state, key), previous)

    def test_resumption_uses_existing_preparation_dispatch_and_marks_pending(self):
        cfg=json.loads((self.root/'dispatch-config.json').read_text());cfg.update(enabled=True);(self.root/'dispatch-config.json').write_text(json.dumps(cfg))
        daily.remember(self.state, 'application:question-resume:1', {'status': 'pending'})
        self.state.db.commit()
        with patch.object(ab, 'prepare_automatic_queue', return_value={'verified_queue_ids': ['1']}) as prepare, patch.object(ab, 'automatic_dispatch', return_value={'confirmed': 0, 'applications_sent': 0, 'outcomes': [{'hh_id': '1', 'status': 'send_uncertain'}]}) as dispatch:
            result = ab.resume_question_queue(self.root)
        prepare.assert_called_once_with(self.root, ids={'1'})
        dispatch.assert_called_once_with(self.root, ids={'1'})
        self.assertEqual(daily.setting(self.state, 'application:question-resume:1')['status'], 'processed')
        self.assertEqual(ab.pending_question_resumes(self.state, self.root), [])

    def test_explicit_question_continuation_never_starts_full_daily_search(self):
        daily.remember(self.state, 'application:question-resume:1', {'status': 'pending'})
        self.state.db.commit()
        with patch.object(ab, 'resume_question_queue', return_value={'status': 'answers_resumed', 'applications_sent': 0}), patch.object(daily_run, 'run', side_effect=AssertionError('no full cycle')):
            result = daily_run.execute_daily(self.root, resume_questions_only=True)
        self.assertEqual(result['status'], 'answers_resumed')
        self.assertEqual(self.state.db.execute("SELECT count(*) FROM meta WHERE key LIKE 'daily:execution:%'").fetchone()[0], 0)
