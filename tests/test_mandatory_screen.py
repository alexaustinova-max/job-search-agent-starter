"""Local qualification evidence; no external services."""
import unittest
import application_batch as ab
class MandatoryScreenTests(unittest.TestCase):

    def setUp(self):
        self.facts = {'r': {'id': 'r', 'text': 'Руководство исследовательской практикой и командой из шести исследователей.', 'source': 'confirmed fixture', 'verified': True, 'capabilities': ['research_leadership']}}

    def screen(self, text):
        return ab.mandatory_screen(text, self.facts)

    def test_explicit_specialised_duration_not_replaced_by_research(self):
        line = 'Профессиональный опыт — от 5 лет в исследованиях рынка коммерческой недвижимости; предпочтительно торговая недвижимость.'
        r = self.screen('Требования к кандидату\n' + line)
        self.assertEqual(len(r['blocking']), 1)
        self.assertIn('от 5 лет', r['blocking'][0]['quote'])
        self.assertTrue(r['desirable'])

    def test_desirable_duration_does_not_block(self):
        for line in ['Желателен опыт в медицинской сфере от 3 лет.', 'Желательно: опыт исследований коммерческой недвижимости от 5 лет.', 'Опыт в банке от 5 лет будет преимуществом.']:
            self.assertEqual(self.screen('Требования:\n' + line)['blocking'], [])

    def test_ambiguous_no_duration_or_domain_does_not_block(self):
        self.assertEqual(self.screen('Требования:\nРелевантный опыт исследований и интерес к недвижимости.')['blocking'], [])

    def test_transferable_research_without_industry_restriction(self):
        r = self.screen('Требования:\nОпыт управления исследовательской командой.\nУмение готовить рекомендации для бизнеса.')
        self.assertFalse(r['blocking'])
        self.assertTrue(r['requirements'])

    def test_actual_specific_years_can_confirm_same_scope(self):
        self.facts['d'] = {'id': 'd', 'text': 'Опыт 6 лет в исследованиях рынка коммерческой недвижимости.', 'verified': True, 'source': 'explicit fixture'}
        self.assertFalse(self.screen('Требования:\nОпыт от 5 лет в исследованиях рынка коммерческой недвижимости.')['blocking'])

    def test_optional_tools_not_promoted_to_mandatory(self):
        self.assertFalse(self.screen('Требования:\nSQL будет полезен для самостоятельной работы.\nPython — дополнительное преимущество.')['blocking'])
        self.assertTrue(self.screen('Требования:\nЗнание SQL Server и Oracle.')['blocking'])

    def test_partial_skill_match_does_not_confirm_explicit_years(self):
        self.facts['r']['capabilities'] = ['team_management', 'research_methods']
        self.assertTrue(self.screen('Ждем от вас:\nОпыт управления исследовательской командой от 5 лет.')['blocking'])

    def test_observed_requirement_headings_are_not_lost(self):
        for heading in ('Мы ждём, что вы', 'Для нас важно для этой позиции', 'Что мы ожидаем'):
            self.assertTrue(self.screen(heading + '\nВысшее техническое образование.')['blocking'])

    def test_optional_parenthesis_does_not_erase_required_duration(self):
        r = self.screen('Требования:\nОпыт от 3 лет в CRM-маркетинге (опыт в B2B будет преимуществом).')
        self.assertTrue(r['blocking'])
        self.assertTrue(r['desirable'])
        self.assertFalse(self.screen('Требования:\nОпыт в банке (будет преимуществом).')['blocking'])

    def test_working_in_a_role_is_also_an_experience_requirement(self):
        self.assertTrue(self.screen('Мы подходим друг другу, если ты:\nРаботаешь в роли Product Owner от 2-х лет в финтехе.')['blocking'])
