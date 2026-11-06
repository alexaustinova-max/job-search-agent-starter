"""Synthetic complete prose, source coverage and independent review guards."""
import unittest
import json
import llm_prepare as llm
from update_fixtures import Fixture
class CompleteLetterQualityTests(Fixture,unittest.TestCase):

    def inputs(self):
        job = {'id': '900000001', 'title': 'Аналитик продукта', 'employer': 'Вымышленный работодатель', 'description': 'Анализировать продуктовые метрики и готовить материалы для обсуждения решений команды.'}
        fact = {'verified': True, 'source': 'cv/fictional.txt', 'text': 'Я анализировал продуктовые метрики и готовил материалы для обсуждения решений команды.'}
        body = 'Здравствуйте!\n\nНаправляю отклик на позицию «Аналитик продукта».\n\n' + fact['text']
        raw = {'letter_body': body, 'fact_ids': ['f'], 'analysis': {'employer_need': 'Анализ метрик', 'experience_focus': ['Аналитика']}, 'questions': []}
        draft = {**llm.complete_letter_draft(raw, job, {'f': fact}), 'generation_format': 'complete_letter_v1'}
        review = {'coverage_complete': True, 'professional_profile_present': True, 'argumentation_clear': True, 'generic_resume_replay': False, 'task_relevant': True, 'correct_cv': True, 'personalized': True, 'issues': [], 'natural': False, 'style_notes': ['Optional style note'], 'claims': [{'text': body, 'kind': 'candidate_claim', 'vacancy_quotes': [], 'supported': True, 'quotes': [{'fact_id': 'f', 'quote': fact['text']}]}], 'task_links': [{'requirement_quote': job['description'], 'letter_quote': fact['text'], 'fact_ids': ['f'], 'transfer_explanation': 'Анализ продуктовых метрик помогает команде принимать решения по описанным задачам.'}]}
        return (raw, draft, review, job, {'f': fact})

    def test_compact_review_requires_all_segments_and_valid_source_references(self):
        _, draft, review, job, facts = self.inputs()
        payload = {'facts': {'f': {'evidence_sources': [facts['f']['text']]}}}
        raw = {**review, 'claims': [{'segment': i, 'kind': 'candidate_claim', 'supported': True, 'vacancy_quotes': [], 'evidence': [{'fact_id': 'f', 'source_indices': [0]}]} for i in range(len(llm.letter_segments(draft['letter_body'])))]}
        self.assertEqual(llm.policy_issues(draft, llm.expand_independent_review(raw, draft, payload), facts, job), [])
        raw['claims'][-1]['evidence'][0]['source_indices'] = [123]
        self.assertIn('unverified_or_invented_source_quote', llm.policy_issues(draft, llm.expand_independent_review(raw, draft, payload), facts, job))
        raw['claims'].pop()
        self.assertIn('unreviewed_letter_text', llm.policy_issues(draft, llm.expand_independent_review(raw, draft, payload), facts, job))

    def test_independent_checker_can_cite_another_exact_confirmed_batch_source(self):
        _, draft, review, job, facts = self.inputs()
        facts['better'] = {**facts['f']}
        review['claims'][0]['quotes'][0]['fact_id'] = 'better'
        self.assertEqual(llm.policy_issues(draft, review, facts, job), [])
        facts['better']['verified'] = False
        self.assertIn('unverified_or_invented_source_quote', llm.policy_issues(draft, review, facts, job))

    def test_opinion_is_not_experience_but_factual_invitation_is_blocked(self):
        _, draft, review, job, facts = self.inputs()
        for body, blocked in [('Я рассматриваю исследования как основу продуктовых решений.', False), ('В новой роли я бы уделяла внимание проверяемости результата.', False), ('Я бы уделяла внимание этому, потому что реализовала систему.', True), ('Буду рада обсудить, как я увеличила выручку на 40%.', True)]:
            draft = {**draft, 'letter_body': body}
            review = {**review, 'claims': [{'text': body, 'kind': 'argument', 'supported': True, 'quotes': [], 'vacancy_quotes': []}]}
            self.assertEqual('candidate_claim_misclassified_as_argument' in llm.policy_issues(draft, review, facts, job), blocked)
            draft['editorial_issues'] = ['candidate_claim_misclassified_as_argument']
            self.assertEqual('candidate_claim_misclassified_as_argument' in llm.policy_issues(draft, review, facts, job), blocked)

    def test_complete_model_prose_is_preserved_and_style_is_not_a_hard_score(self):
        raw, draft, review, job, facts = self.inputs()
        self.assertEqual(draft['letter_body'], raw['letter_body'])
        self.assertFalse(draft['quality_passed'])
        self.assertFalse(draft['fact_validation_passed'])
        self.assertEqual(llm.policy_issues(draft, review, facts, job), [])
        self.assertNotIn('500–800', llm.DAILY_PARAPHRASE_INSTRUCTIONS)
        request = json.loads(llm.encoded_request({'task': 'cover_letters', 'vacancies': []}, {**llm.settings(self.root), '_daily_editorial': True}))
        props = request['text']['format']['schema']['properties']['results']['items']['properties']
        self.assertIn('letter_body', props)
        self.assertNotIn('letter', props)

    def test_natural_position_wording_does_not_override_independent_role_check(self):
        raw, draft, review, job, facts = self.inputs()
        job = {**job, 'title': 'Руководитель исследовательской группы'}
        changed = llm.complete_letter_draft(raw, job, facts)
        draft = {**draft, **changed, 'generation_format': 'complete_letter_v1'}
        self.assertNotIn('greeting_or_position', draft['editorial_issues'])
        self.assertEqual(llm.policy_issues(draft, review, facts, job), [])
        review = {**review, 'task_relevant': False, 'correct_cv': False}
        issues = llm.policy_issues(draft, review, facts, job)
        self.assertIn('letter_not_relevant_to_vacancy', issues)
        self.assertTrue(any(('cv' in issue.lower() for issue in issues)))

    def test_unreviewed_or_invented_claims_and_generic_prose_cannot_pass(self):
        raw, draft, review, job, facts = self.inputs()
        draft['letter_body'] += ' Увеличила выручку на 40%.'
        self.assertIn('unreviewed_letter_text', llm.policy_issues(draft, review, facts, job))
        draft['letter_body'] = raw['letter_body']
        review['claims'][0]['quotes'][0]['quote'] = 'Запустила HRTech и увеличила выручку'
        self.assertIn('unverified_or_invented_source_quote', llm.policy_issues(draft, review, facts, job))
        review['generic_resume_replay'] = True
        self.assertIn('generic_resume_replay', llm.policy_issues(draft, review, facts, job))
        review['claims'][0].update(kind='argument', quotes=[])
        self.assertIn('candidate_claim_misclassified_as_argument', llm.policy_issues(draft, review, facts, job))

    def test_unconfirmed_qualifications_are_risks_not_mandatory_HH_questions(self):
        raw, draft, review, job, facts = self.inputs()
        raw['questions'] = ['Подтвержден ли опыт SSO?']
        raw['analysis']['unconfirmed_requirements'] = ['Опыт рейтинговых агентств не подтвержден']
        result = {**llm.complete_letter_draft(raw, job, facts), 'generation_format': 'complete_letter_v1'}
        self.assertFalse(result['questions'])
        self.assertEqual(len(result['qualification_risks']), 2)
        self.assertEqual(llm.policy_issues(result, review, facts, job), [])

    def test_expanded_letter_not_assembled_from_fixed_fragments(self):
        raw, draft, review, job, facts = self.inputs()
        raw['letter_body'] += '\n\n' + 'Контекст исследовательской работы и связь с продуктовыми решениями раскрыты подробно. ' * 30
        expanded = llm.complete_letter_draft(raw, job, facts)
        self.assertGreater(len(expanded['letter_body']), 2200)
        self.assertNotIn('letter_length', expanded['editorial_issues'])
        self.assertFalse(expanded['quality_passed'])
