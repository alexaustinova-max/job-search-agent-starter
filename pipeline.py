#!/usr/bin/env python3
"""Local, deterministic job pipeline. Public GET only in `run`; email requires approval."""
import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from email import policy
from email.parser import BytesParser
import fcntl
import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import smtplib
import socketserver
import sqlite3
import ssl
import subprocess
import threading
from urllib.parse import urlencode, urlparse
import uuid
import hh_api
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
VERSION = '4.4.1'
STAGES = {'found': 'найдена', 'checked': 'проверена', 'matched': 'соответствует',
          'prepared': 'подготовлена', 'approved': 'согласована', 'sent': 'отправлена',
          'confirmed': 'подтверждена'}
CLOSED_SEND_STATES = {'dispatching', 'send_uncertain', 'sent', 'confirmed'}
CAPABILITIES = {
    'research_leadership': r'(?i)(?:head|lead|director).{0,30}research|research.{0,20}(?:lead|manager)|управлен.{0,20}команд.{0,20}исслед|руковод.{0,35}исслед|развит.{0,20}исследовательск.{0,12}(?:практик|команд)',
    'ux_research': r'(?i)ux.{0,12}research|user research|usability|юзабилити|пользовательск.{0,12}исслед',
    'quant_research': r'(?i)quantitative|количественн',
    'research_ops': r'(?i)research.?ops|research operations|research repository|исследовательск.{0,15}процесс',
    'product_discovery': r'(?i)research and discovery|product discovery|lead discovery across (?:users|customers)|discovery (?:process|research|phase)|customer (?:interviews|discovery)|провер.{0,15}гипотез|custdev|jtbd',
    'ai_products': r'(?i)\bLLM\b|\bAI\b.{0,25}(?:products?|solutions?|platform)|(?:products?|solutions?|platform).{0,25}\bAI\b|ИИ.{0,15}(?:продукт|решени)|генеративн',
    'internal_products': r'(?i)internal (?:tools|products|platform|business)|внутренн.{0,20}(?:продукт|заказчик)|automation|автоматизац',
    'research_methods': r'(?i)research methods?|методолог\w*|метод.{0,15}исслед\w*',
    'team_management': r'(?i)(?:manage|lead|руковод|управл).{0,30}(?:team|команд)',
    'b2b_context': r'(?i)\bb2b\b|enterprise|\bsaas\b',
    'market_analysis': r'(?i)market needs|market analysis|потребност.{0,12}рынк|анализ.{0,12}рынк',
    'mvp_concept': r'(?i)\bmvp\b|minimum viable product',
    'digital_products': r'(?i)digital products?|цифров.{0,12}продукт',
    'web_products': r'(?i)\bweb\b|веб',
    'crm': r'(?i)\bcrm\b',
    'telephony': r'(?i)telephony|телефони',
    'backlog': r'(?i)backlog|бэклог|беклог',
    'business_processes': r'(?i)business processes|бизнес.процесс',
    'product_analytics': r'(?i)product analytics|product metrics|metrics|продуктов(?:ая|ой|ую|ые|ых)\s+аналитик|метрик',
    'releases': r'(?i)releases?|релиз|shipping software',
    'research_systems': r'(?i)research systems?|исследовательск.{0,12}систем',
    'ai_architecture': r'(?i)AI systems?|AI architecture|архитектур.{0,20}(?:AI|ИИ)|AI.систем',
    'agent_skills': r'(?i)agent skills|speciali[sz]ed skills|специализированн.{0,10}skills',
    'orchestration': r'(?i)orchestration|оркестрац',
    'evidence_traceability': r'(?i)evidence traceability|прослеживаемост.{0,15}доказательств',
    'human_review': r'(?i)human review|human.in.the.loop|ручн.{0,10}проверк',
    'synthetic_tests': r'(?i)synthetic tests?|синтетическ.{0,10}тест',
    'cx_research': r'(?i)customer (?:experience|insights)|cx research|cjm|csat|клиентск.{0,12}опыт',
}
PROFILE_CAPS = {}


def verified_facts(candidate):
    return [f for f in candidate.get('facts', [])
            if f.get('verified') is True and f.get('id') and f.get('source') and f.get('text')]


def load_candidate(candidate, root):
    """Read local evidence on every run, so edits invalidate cached assessments."""
    candidate = dict(candidate)
    CAPABILITIES.update(candidate.get('capability_patterns',{}))
    if candidate.get('facts_file'):
        path = (root / candidate['facts_file']).resolve()
        if not path.is_relative_to(root.resolve()): raise ValueError('facts must be inside the project')
        evidence = json.loads(path.read_text())
        facts = evidence['facts']
        if not isinstance(facts, list): raise ValueError('facts must be a list')
        ids = set()
        for fact in facts:
            if not isinstance(fact, dict) or not fact.get('id') or fact['id'] in ids:
                raise ValueError('invalid or duplicate fact ID')
            ids.add(fact['id'])
            if not fact.get('source') or not fact.get('text') or not fact.get('capabilities'):
                raise ValueError('fact needs provenance, text and permitted capabilities')
            if any(cap not in CAPABILITIES for cap in fact['capabilities']):
                raise ValueError('unknown fact capability')
        candidate['facts'] = facts
        candidate['english_level'] = evidence['english_level']
        candidate['facts_source'] = candidate['facts_file']
    return candidate


REQUIRED_HEADING = r"(?:обязательные |желательные )?требования(?: к кандидатам)?|обязательный опыт|ключевые (?:компетенции|hard skills)|какие (?:навыки пригодятся|знания и навыки важны)|наши ожидания от кандидата|что для нас важно|мы ожидаем|наш идеальный кандидат|requirements?|qualifications?|skills (?:and|&) qualifications|what (?:you(?:'|’)ll bring|we(?:'|’)re looking for)|требования|что (?:мы )?ожидаем|необходимые навыки(?: и знания)?|требования к (?:кандидат[ау]|опыту работы)|наши (?:ожидания|требования|пожелания(?: к кандидатам)?)|(?:для нас|нам) важно|мы (?:ждем|ждём) от (?:тебя|вас)|кого мы ищем|что (?:важно(?: для этой роли)?|потребуется)|важные для нас навыки и опыт|мы ищем человека, который|что для этого нужно"
REQUIREMENT_VERSION = 'sections-v2'
REQUIRED_HEADING += r'|мы (?:ждем|ждём),? что у вас(?: есть)?'
OPTIONAL_MARKER = r'nice[ -]to[ -]have|(?:a (?:big |significant )?|significant )plus|preferred|prefer candidates|bonus(?: points)?|desirable|ideally|advantage|beneficial|преимуществ|плюсом|желательно'
OPTIONAL_MARKER += r'|будет здорово'
OPTIONAL_HEADING = r'(?:будет|будут) (?:(?:большим|большими|дополнительным|дополнительными) )?(?:плюсом|плюсами|преимуществом|преимуществами)|желательные требования(?: к кандидатам)?|'+OPTIONAL_MARKER
REQUIRED_MARKER = r'\bmust\b|\brequired\b|mandatory|обязатель|необходим|требуется|должны'
ROLE_HEADING = r"responsibilities|обязанности|задачи|чем предстоит заниматься|why this team|about you|about this opportunity|how you(?:'|’)ll contribute|what we.*"
EXCLUDED_HEADING = r"(?:что )?(?:мы )?предлагаем|как откликнуться|about us|benefits.*|условия|мы предлагаем|equal employment.*|rights under.*|a few notes"
EXCLUDED_HEADING += r'|что (?:вы|ты) получи(?:те|шь)|мы (?:даём|даем|предоставляем|гарантируем)|наши преимущества|что мы даём взамен'
EXCLUDED_HEADING += r'|what we offer|what you get'
ROLE_HEADING += r'|чем предстоит заниматься исследователю|ключевая задача роли|о команде'


def description_lines(text):
    """Preserve requirements, including mandatory text before an optional clause."""
    section = 'unspecified'
    for raw in text.splitlines():
        line = raw.strip(' \t•*-')
        if not line: continue
        # Support both standalone headings and "Requirements: ...".
        parts = line.split(':', 1)
        heading = re.sub(r'^[^A-Za-zА-Яа-яЁё]+', '', parts[0]).strip(' .?!')
        new_section = None
        if re.fullmatch(EXCLUDED_HEADING, heading, re.I): new_section = 'excluded'
        elif re.fullmatch(OPTIONAL_HEADING, heading, re.I): new_section = 'optional'
        elif re.fullmatch(REQUIRED_HEADING, heading, re.I): new_section = 'mandatory'
        elif re.fullmatch(ROLE_HEADING, heading, re.I): new_section = 'context'
        elif re.match(r'(?i)^(?:мы\s+)?(?:предлагаем|предоставляем|гарантируем|обеспечиваем)\b|^у нас вы получите\b', line):
            new_section = 'excluded'
        if new_section:
            section = new_section
            if len(parts) == 1 or not parts[1].strip(): continue
            line = parts[1].strip()
        if section == 'excluded': continue
        # Do not let "We prefer..." erase earlier mandatory sentences in a bullet.
        chunks = re.split(r'(?<=[.!?])\s+(?=(?:It is (?:a |an |a significant )?(?:plus|advantage)|We prefer|Familiarity with[^.]*\bplus\b|Nice.to.have|Будет плюсом))', line, flags=re.I)
        expanded = []
        for chunk in chunks:
            expanded.extend(re.split(r',\s*(?=ideally\b)', chunk, flags=re.I))
        for chunk in expanded:
            explicit_required = bool(re.search(REQUIRED_MARKER, chunk, re.I))
            optional = bool(re.search(OPTIONAL_MARKER, chunk, re.I)) and not explicit_required
            kind = 'optional' if optional or section == 'optional' else ('mandatory' if explicit_required else section)
            yield chunk, kind


def english_assessment(text, candidate):
    # Cyrillic lookalikes В2/С1 are common in Russian vacancy descriptions.
    text = re.sub(r'(?<!\w)([АВС])([12])(?!\w)', lambda m: {'А':'A','В':'B','С':'C'}[m[1]]+m[2], text)
    evidence = []; risks = []; critical = []; english_context = False
    text = re.sub(r'(\bB2\b[^;,.]*?)[(—–-]\s*(?=C[12]\b)', r'\1\n', text, flags=re.I)
    for line, kind in description_lines(text):
        previous_english_context = english_context
        english_context = bool(re.search(r'english|английск', line, re.I))
        # A benefit elsewhere in a paragraph must not cancel a mandatory language clause.
        clauses = re.split(r'[;,.!?]\s*', line)
        for clause in clauses:
            if not clause.strip(): continue
            if not re.search(r'english|английск', clause, re.I) and re.search(r'french|german|russian|spanish|немецк|французск|русск|испанск', clause, re.I): continue
            has_english = bool(re.search(r'english|английск', clause, re.I))
            level_only = (english_context or previous_english_context) and bool(re.search(r'\b[ABC][12]\b', clause, re.I))
            if not (has_english or level_only): continue
            english_context = has_english or level_only
            if re.search(r'not required|not mandatory|no english required|не требуется|не обязател|lessons|courses|classes|обучени|изучени', clause, re.I): continue
            explicit = bool(re.search(REQUIRED_MARKER, clause, re.I))
            optional = bool(re.search(OPTIONAL_MARKER, clause, re.I)) or (kind == 'optional' and not explicit)
            high = bool(re.search(r'\bC[12]\b|fluent|native(?:[ -]level)?|свободн', clause, re.I))
            b2 = bool(re.search(r'\bB2\b|upper.intermediate', clause, re.I))
            if optional:
                evidence.append({'text': clause.strip(), 'mandatory': False, 'status': 'preferred_only'})
                continue
            if high:
                required_level='C2' if re.search(r'C2|native',clause,re.I) else 'C1'
                levels=['A1','A2','B1','B2','C1','C2']
                level=candidate.get('english_level')
                if level not in levels or levels.index(level)<levels.index(required_level):
                    critical.append('required_english_not_confirmed')
                status='confirmed' if not critical else 'critical_mismatch'
            elif b2:
                if candidate.get('english_level') not in ('B2', 'C1', 'C2'):
                    risks.append('english_b2_risk')
                status = 'b2_requirement'
            elif explicit or re.search(r'strong|excellent|professional', clause, re.I):
                risks.append('english_level_requires_review'); status = 'level_unclear'
            else: continue
            evidence.append({'text': clause.strip(), 'mandatory': True, 'status': status})
    return {'evidence': evidence, 'risks': sorted(set(risks)), 'critical_mismatches': sorted(set(critical)),
            'candidate_level': candidate.get('english_level'),
            'description_language_is_not_a_filter': True}


def now():
    return datetime.now(ZoneInfo(__import__('portable_runtime').timezone())).isoformat()


def digest(value):
    data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(data).hexdigest()


class Plain(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts = []; self.skip = 0
    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'): self.skip += 1
        if tag in ('p', 'li', 'br', 'div', 'h2', 'h3'): self.parts.append('\n')
    def handle_endtag(self, tag):
        if tag in ('script', 'style'): self.skip = max(0, self.skip - 1)
        if tag in ('p', 'li', 'div'): self.parts.append('\n')
    def handle_data(self, data):
        if not self.skip: self.parts.append(data)


def plain(value):
    parser = Plain(); parser.feed(html.unescape(html.unescape(value or '')))
    return '\n'.join(re.sub(r'\s+', ' ', x).strip() for x in ''.join(parser.parts).splitlines() if x.strip())


class FetchError(Exception):
    def __init__(self, reason, status=None):
        super().__init__(reason); self.status = status


class PublicHTTP:
    """No credentials, cookies, curl config, redirects, retries, HH or arbitrary hosts."""
    HOSTS = {'boards-api.greenhouse.io', 'api.lever.co', 'api.eu.lever.co'}
    def __init__(self): self.requests = []
    def get(self, url):
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname not in self.HOSTS or parsed.username or parsed.password:
            raise FetchError('url_not_allowlisted')
        marker = b'\n__JOB_AGENT_HTTP_META_2a54__\n'
        command = ['/usr/bin/curl', '-q', '--silent', '--show-error', '--proto', '=https',
                   '--retry', '0', '--max-redirs', '0', '--connect-timeout', '10', '--max-time', '30',
                   '--max-filesize', '10485760', '--header', 'Accept: application/json',
                   '--user-agent', 'job-search-agent/0.1', '--write-out', marker.decode() + '%{http_code}', url]
        try:
            result = subprocess.run(command, capture_output=True, timeout=35, check=False)
        except (OSError, subprocess.TimeoutExpired):
            self.requests.append({'url': url, 'time': now(), 'status': None, 'error': 'transport'})
            raise FetchError('transport') from None
        body, separator, metadata = result.stdout.rpartition(marker)
        status = int(metadata) if separator and metadata.isdigit() and metadata != b'000' else None
        self.requests.append({'url': url, 'time': now(), 'status': status, 'bytes': len(body), 'curl_exit': result.returncode})
        if result.returncode or status != 200:
            raise FetchError('transport' if result.returncode else 'http_error', status)
        try: return json.loads(body)
        except (ValueError, UnicodeError): raise FetchError('invalid_json', status) from None


def collect(source, http, hh_client=None, prefilter=None):
    provider, board = source['provider'], source['board']
    if provider == 'hh':
        if board != 'public': raise FetchError('hh_board_must_be_public')
        if hh_client is None: raise FetchError('hh_client_not_configured')
        try: return hh_client.collect(source, prefilter=prefilter)
        except hh_api.HHError as exc: raise FetchError(exc.code, exc.status) from None
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', board): raise ValueError('invalid board')
    if provider == 'greenhouse':
        response = http.get(f'https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true')
        if not isinstance(response, dict) or not isinstance(response.get('jobs'), list): raise FetchError('invalid_jobs_schema')
        return response['jobs'], True
    if provider != 'lever': raise ValueError('unsupported provider')
    host = 'api.eu.lever.co' if source.get('region') == 'eu' else 'api.lever.co'
    jobs = []; limit = 100
    for page in range(source.get('max_pages', 10)):
        batch = http.get(f'https://{host}/v0/postings/{board}?{urlencode({"mode": "json", "skip": page * limit, "limit": limit})}')
        if not isinstance(batch, list): raise FetchError('invalid_postings_schema')
        jobs.extend(batch)
        if len(batch) < limit: return jobs, True
    return jobs, False  # Do not retire unseen jobs after a truncated/failed listing.


def normalize(source, raw):
    if source['provider'] == 'hh': return hh_api.normalize_job(source, raw, plain, digest)
    provider, board = source['provider'], source['board']
    if not raw.get('id'): raise ValueError('job has no ID')
    if provider == 'greenhouse':
        title = raw.get('title', '')
        description = plain(raw.get('content', ''))
        locations = [raw.get('location', {}).get('name', '')]
        ranges = raw.get('pay_input_ranges') or []
        salary = ({'ranges': [{'min': x.get('min_cents', 0) / 100,
                              'max': x.get('max_cents', 0) / 100,
                              'currency': x.get('currency_type'), 'interval': 'year', 'gross': x.get('gross')} for x in ranges]} if ranges else None)
        workplace = 'remote' if re.search(r'remote|удал[её]н', ' '.join(locations), re.I) else 'unspecified'
        page_url = raw.get('absolute_url', '')
        apply_url = page_url
        internal_id = raw.get('internal_job_id')
        published = raw.get('first_published'); updated = raw.get('updated_at')
    else:
        title = raw.get('text', '')
        # Sections are separate from description; don't lose responsibilities or requirements.
        chunks = [raw.get('openingPlain') or raw.get('opening', ''),
                  raw.get('descriptionPlain') or raw.get('description', ''),
                  raw.get('descriptionBodyPlain') or raw.get('descriptionBody', '')]
        chunks += [str(x.get('text', '')) + '\n' + x.get('content', '') for x in raw.get('lists', [])]
        chunks += [raw.get('additionalPlain') or raw.get('additional', '')]
        description = '\n'.join(dict.fromkeys(plain(x) for x in chunks if x))
        category = raw.get('categories') or {}
        locations = category.get('allLocations') or [category.get('location', '')]
        workplace = raw.get('workplaceType', 'unspecified')
        salary = raw.get('salaryRange')
        page_url, apply_url = raw.get('hostedUrl', ''), raw.get('applyUrl', '')
        internal_id = None; published = None; updated = None
    job = {'key': f'{provider}:{board}:{raw["id"]}', 'provider': provider, 'board': board,
           'employer': source['employer'], 'id': str(raw['id']), 'internal_id': internal_id,
           'title': title, 'description': description, 'locations': locations, 'workplace': workplace,
           'salary': salary, 'page_url': page_url, 'apply_url': apply_url,
           'published_at': published, 'updated_at': updated,
           'created_at': raw.get('createdAt') if provider == 'lever' else None}
    # GH multiple language/office posts can refer to one internal job.
    identity = f'{provider}:{board}:{internal_id}' if internal_id else f'{source["employer"].casefold()}:{title.casefold()}:{sorted(locations)}'
    job['opportunity_key'] = digest(identity)
    return job


TAX_MODEL = 'ru_resident_ndfl_2026_annualized_no_deductions'
TAX_SOURCE = 'https://www.nalog.gov.ru/rn64/news/activities_fts/16639359/'


def estimated_monthly_net(gross_monthly):
    """Annualized standard wage model, not confirmation of personal take-home pay."""
    annual = gross_monthly * 12
    tax = 0.0; previous = 0.0
    for ceiling, rate in [(2400000, .13), (5000000, .15), (20000000, .18), (50000000, .20), (float('inf'), .22)]:
        taxable = max(0, min(annual, ceiling) - previous)
        tax += taxable * rate
        previous = ceiling
        if annual <= ceiling: break
    return round((annual - tax) / 12, 2)


def gross_salary_assessment(job, floor):
    """Compare published RUB fixed GROSS only; never infer GROSS from NET."""
    result={'floor_monthly_rub':floor,'floor_basis':'GROSS','status':'unknown','failure':None,
        'needs_review':False,'net_floor_confirmed':False,'gross_floor_confirmed':False,
        'note':'salary_not_disclosed_or_not_parseable','bands':[]}
    salary=job.get('salary') or {};bands=salary.get('ranges',[salary]) if salary else []
    text=job.get('description','');fixed=[]
    number=r'(\d[\d \u00a0\u202f]*(?:[.,]\d+)?)'
    pattern=r'(?:оклад|фикс(?:ированн\w*(?:\s+(?:часть|доход|зарплат\w*))?)?)\s*[:=—-]?\s*(от\s+|до\s+)?'+number+r'\s*(?:[-–—]\s*'+number+r')?\s*(тыс\.?|тысяч|[kк])?'
    for line in text.splitlines():
        match=re.search(pattern,line,re.I)
        if not match:continue
        factor=1000 if match[4] else 1
        amount=lambda value:float(re.sub(r'[ \u00a0\u202f]','',value).replace(',','.'))*factor
        low=amount(match[2]);high=amount(match[3]) if match[3] else low
        if match[1] and match[1].strip().casefold()=='до':low=None
        elif match[1] and not match[3]:high=None
        gross=True if re.search(r'\bgross\b|до\s+(?:вычета\s+|уплаты\s+)?налог',line,re.I) else False if re.search(r'\bnet\b|на руки|после\s+(?:вычета\s+)?налог',line,re.I) else salary.get('gross')
        period='year' if re.search(r'в год|/год|annual|year',line,re.I) else 'month' if re.search(r'месяц|/мес|month',line,re.I) else salary.get('interval')
        currency='RUB' if re.search(r'₽|руб|\bRUB\b|\bRUR\b',line,re.I) else salary.get('currency')
        fixed.append({'min':low,'max':high,'gross':gross,'interval':period,'currency':currency,'evidence':line})
    package=bool(re.search(r'(?:доход|совокупн|зарплат).{0,55}(?:бонус|прем)|(?:оклад|фикс).{0,20}[+＋]|\bOTE\b',text,re.I))
    if fixed:bands=fixed
    elif package and bands:
        result.update(note='fixed_part_not_disclosed',dispatch_blocker='fixed_part_not_disclosed')
        return result
    for band in bands:
        item={'currency':band.get('currency'),'gross':band.get('gross'),
            'published_interval':band.get('interval'),'monthly_published_min':None,'monthly_published_max':None,
            'gross_min':None,'gross_max':None,'net_min':None,'net_max':None,
            'definitely_below_floor':False,'confirmed_min_meets_floor':False}
        result['bands'].append(item)
        period=str(band.get('interval') or '').casefold()
        factor=12 if 'year' in period or 'год' in period else 1 if 'month' in period or 'месяц' in period else None
        if str(band.get('currency') or '').upper() not in {'RUB','RUR','РУБ'} or factor is None:continue
        for bound in ('min','max'):
            value=band.get(bound)
            if isinstance(value,(int,float)) and not isinstance(value,bool) and value>0:item['monthly_published_'+bound]=value/factor
        low,high=item['monthly_published_min'],item['monthly_published_max']
        if low is not None and high is not None and low>high:continue
        if band.get('gross') is True:
            item.update(gross_min=low,gross_max=high,
                definitely_below_floor=high is not None and high<floor,
                confirmed_min_meets_floor=low is not None and low>=floor)
        elif band.get('gross') is False:item.update(net_min=low,net_max=high)
    if result['bands'] and all(b['definitely_below_floor'] for b in result['bands']):
        result.update(status='below_gross_floor',failure='salary_below_floor',note='published_fixed_GROSS_below_floor')
    elif any(b['confirmed_min_meets_floor'] for b in result['bands']):
        result.update(status='gross_floor_met',gross_floor_confirmed=True,note='published_gross_min_meets_floor_rub')
    elif any(b['gross'] is True and (b['gross_min'] is not None or b['gross_max'] is not None) for b in result['bands']):
        result.update(status='gross_range_requires_negotiation',note='salary_gross_range_requires_negotiation')
    elif any(b['gross'] is False for b in result['bands']):
        result.update(status='net_not_converted_to_gross',note='published_NET_kept_without_GROSS_conversion')
    return result


def salary_assessment(job, floor, basis='NET'):
    if basis=='GROSS':return gross_salary_assessment(job,floor)
    if basis!='NET':raise ValueError('unsupported_salary_floor_basis')
    result = {'floor_monthly_rub': floor, 'floor_basis': 'NET', 'status': 'unknown',
              'failure': None, 'needs_review': True, 'net_floor_confirmed': False,
              'note': 'salary_not_disclosed_or_not_parseable', 'bands': []}
    salary = job.get('salary')
    if not salary:
        bands = []
        for line in job.get('description', '').splitlines():
            if not re.search(r'salary|compensation|зарплат|оклад|оплат', line, re.I): continue
            period = 'month' if re.search(r'month|месяц|/мес', line, re.I) else 'year' if re.search(r'annual|year|в год|/год', line, re.I) else None
            if not period: continue
            match = re.search(r'(\d[\d ]*(?:[.,]\d+)?)\s*(?:-|–|—|до|to)\s*(\d[\d ]*(?:[.,]\d+)?)\s*(тыс\.?|[kк])?\s*(?:₽|руб|RUB|RUR)', line, re.I)
            if not match: continue
            factor = 1000 if match[3] else 1
            gross = True if re.search(r'\bgross\b|до вычета|до уплаты|до налог', line, re.I) else False if re.search(r'\bnet\b|на руки|после вычета|после уплаты', line, re.I) else None
            bands.append({'min': float(match[1].replace(' ', '').replace(',', '.')) * factor,
                          'max': float(match[2].replace(' ', '').replace(',', '.')) * factor,
                          'currency': 'RUB', 'interval': period, 'gross': gross,
                          'evidence': line})
        if not bands: return result
    else: bands = salary.get('ranges', [salary])
    if not bands: return result
    for band in bands:
        currency = (band.get('currency') or '').upper()
        period = str(band.get('interval') or '').lower()
        factor = 12 if 'year' in period or 'год' in period else 1 if 'month' in period or 'месяц' in period else None
        item = {'currency': currency, 'gross': band.get('gross'), 'published_interval': period,
                'monthly_published_min': None, 'monthly_published_max': None,
                'net_min': None, 'net_max': None, 'net_estimate_min': None, 'net_estimate_max': None,
                'definitely_below_floor': False, 'confirmed_min_meets_floor': False}
        result['bands'].append(item)
        if currency not in {'RUB', 'RUR', 'РУБ'}:
            item['unknown_reason'] = 'foreign_salary_no_verified_fx'; continue
        if factor is None:
            item['unknown_reason'] = 'salary_period_not_comparable'; continue
        for field in ('min', 'max'):
            value = band.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                item['monthly_published_' + field] = value / factor
        low, high = item['monthly_published_min'], item['monthly_published_max']
        if low is not None and high is not None and low > high:
            item['unknown_reason'] = 'invalid_salary_range'; continue
        if band.get('gross') is False:
            item.update(net_min=low, net_max=high)
            item['confirmed_min_meets_floor'] = low is not None and low >= floor
            item['definitely_below_floor'] = high is not None and high < floor
        elif band.get('gross') is True:
            item.update(net_estimate_min=estimated_monthly_net(low) if low else None,
                        net_estimate_max=estimated_monthly_net(high) if high else None,
                        tax_model=TAX_MODEL, tax_source=TAX_SOURCE,
                        assumptions=['Russian tax resident, ordinary employment wages',
                                     '12 equal monthly payments, no deductions, bonuses or other income',
                                     'Annual average, not a guaranteed amount for each month'])
            # Even before deductions a ceiling below NET floor cannot meet it.
            item['definitely_below_floor'] = high is not None and high < floor
        else:
            item['unknown_reason'] = 'salary_tax_basis_unknown'
            item['definitely_below_floor'] = high is not None and high < floor
    bands = result['bands']
    if all(b['definitely_below_floor'] for b in bands):
        result.update(status='below_net_floor', failure='salary_below_floor', needs_review=False,
                      note='published_ceiling_below_net_floor_rub')
    elif any(b['confirmed_min_meets_floor'] for b in bands):
        result.update(status='net_floor_met', needs_review=False, net_floor_confirmed=True, note='published_net_min_meets_floor_rub')
    elif all(b['net_estimate_max'] is not None and b['net_estimate_max'] < floor for b in bands):
        result.update(status='estimated_below_net_floor', note='salary_net_below_floor_estimate')
    elif any(b['gross'] is True and b.get('tax_model') for b in bands):
        result.update(status='gross_requires_net_confirmation', note='salary_gross_requires_net_confirmation')
    elif all(b['net_max'] is not None for b in bands):
        result.update(status='net_range_requires_negotiation', note='salary_net_range_requires_negotiation')
    else:
        result['note'] = next((b['unknown_reason'] for b in bands if b.get('unknown_reason')), 'salary_ceiling_not_published')
    return result


def salary_decision(job, floor):
    assessment = salary_assessment(job, floor)
    return assessment['failure'], assessment['note']


def role_signals(job):
    return {'bases':{},'role_level':'configured_profile','management_scope_evidence':[],
            'specialized_domain_evidence':[],'non_digital_product_role':False,
            'excluded_domain':False,'possible_product':True,'potential_research_management':False,
            'research_role':False,'leadership':False}


def hh_quick_filter(raw, candidate, settings):
    # Snippets can establish explicit exclusions, never full candidate fit.
    card = {**raw, '_detail_state': 'summary'}
    job = normalize({'provider': 'hh', 'board': 'public'}, card)
    return hh_card_assessment(job, candidate, settings)


def hh_card_assessment(job,candidate,settings):
    result=filters(job,candidate,settings);match=score(job,candidate)
    if not match['direction_relevant']:
        result['reasons'].append('outside_target_profiles');result['passed']=False
    result.update(priority=max(x['role_score'] for x in match['profiles'].values()),
        role_level='configured_profile',scope='Card-only screening; full text and human review required.')
    return result


def filters(job,candidate,settings):
    title=job['title'];text=job['description'];reject=[];notes=[]
    salary=salary_assessment(job,settings['min_monthly_rub'],settings.get('salary_basis','NET'))
    if salary['failure']:reject.append(salary['failure'])
    notes.append(salary['note'])
    workplace=job.get('workplace','unspecified')
    if workplace not in settings.get('allowed_work_formats',['remote','hybrid','on-site','unspecified']):
        reject.append('work_format_not_allowed')
    cities=settings.get('cities',[])
    if cities and workplace!='remote' and not any(c.casefold() in ' / '.join(job['locations']).casefold() for c in cities):
        reject.append('location_not_allowed')
    for pattern in settings.get('exclude_title_patterns',[]):
        if re.search(pattern,title,re.I):reject.append('excluded_title')
    for pattern in settings.get('exclude_text_patterns',[]):
        if re.search(pattern,title+'\n'+text,re.I):reject.append('excluded_requirement')
    for pattern in settings.get('required_text_patterns',[]):
        if not re.search(pattern,title+'\n'+text,re.I):reject.append('required_condition_unconfirmed')
    if job['employer'].casefold() in {x.casefold() for x in settings.get('excluded_employers',[])}:
        reject.append('excluded_employer')
    return {'passed':not reject,'reasons':sorted(set(reject)),'notes':notes,'salary':salary}


def requirement_gaps(text, supported_caps):
    categories = {
        'career_dates_and_duration': r'\d+.{0,12}(?:years?|лет|года)|стаж|опыт.{0,10}от ',
        'education': r'образован|degree|университет',
        'specific_research_methods': r'интервью|фокус.групп|юзабилити|количествен|качествен|опрос|CJM|JTBD|usability|CustDev|нейрофизиол|EEG|eye.tracking',
        'specific_tools': r'\bSQL\b|\bJira\b|Python|Docker|k8s|RAG|Figma|\bBI\b|Scrum|Kanban',
        'production_and_measured_outcomes': r'production|промышлен|продакшен|внедрен|внедрён|измерим|достижен|P&L|запуск|коммерческ',
        'domain_experience': r'gaming|игров|шутер|антифрод|комплаенс|лояльност|банков|финтех|billing|subscriptions|CDP|RFM',
        'management_scope': r'управлен|руковод|\bteam\b|команд|бюджет|портфел',
    }
    found = [{'category': name, 'status': 'needs_candidate_confirmation',
              'note': 'Not recorded sufficiently in the current fact base; actual experience is unknown.'}
             for name, pattern in categories.items() if re.search(pattern, text, re.I)]
    if not found:
        found = [{'category': 'requirement_details', 'status': 'needs_candidate_confirmation',
                  'note': 'Full requirement has no sufficient confirmation in the local fact base.'}]
    return found


def requirement_assessment(text, facts):
    mandatory = []; optional = []; intersections = []
    for index, (line, kind) in enumerate(description_lines(text)):
        if re.search(r'send your resume|присылайте резюме', line, re.I): continue
        caps = [cap for cap, pattern in CAPABILITIES.items() if re.search(pattern, line)]
        matches = []
        for cap in caps:
            found = [f for f in facts if cap in f.get('capabilities', [])]
            if found:
                fact = found[0]
                evidence = {'capability': cap, 'job_evidence': line, 'fact_id': fact['id'],
                            'candidate_evidence': fact['text'], 'fact_source': fact['source'],
                            'scope': fact.get('scope', 'confirmed_statement_only'),
                            'letter_text': fact.get('letter_text')}
                matches.append(evidence)
                if kind != 'optional': intersections.append(evidence)
        # Full-line coverage is conservative: numbers, tools, domains and arbitrary
        # qualifications remain unconfirmed even when one capability is supported.
        bounded = re.sub(r'[.\s]+$', '', line.strip()).casefold() in {
            'user research and quantitative methods', 'product discovery', 'researchops',
            'research operations', 'backlog', 'crm', 'telephony', 'product analytics',
            'evidence traceability', 'human review', 'synthetic tests',
        }
        fully_supported = bool(bounded and caps and len(matches) == len(caps))
        item = {'id': 'requirement_' + str(index), 'text': line, 'kind': kind,
                'capabilities': caps, 'supported_intersections': matches,
                'unconfirmed_capabilities': [cap for cap in caps if cap not in {m['capability'] for m in matches}],
                'status': 'confirmed' if fully_supported else ('partial' if matches else 'unconfirmed'),
                'needs_review': not fully_supported,
                'constraints_requiring_evidence': [name for name, pattern in {
                    'duration': r'\d+\+?\s*(?:years?|лет|года)',
                    'subscription_billing_payments': r'subscript|billing|payments|подпис|биллинг|платеж',
                    'gaming_creator_marketing': r'games?|gaming|creator|influencer|video game marketing|игров|шутер|гейм',
                    'specific_tools': r'\bJira\b|\bSQL\b|YouTube|Twitch|TikTok|\bKick\b',
                    'production_deployment': r'production|промышленн|продакшен',
                }.items() if re.search(pattern, line, re.I)] if not fully_supported else [],
                'evidence_status': 'confirmed' if fully_supported else ('partially_supported_unknown_remainder' if matches else 'unknown_from_fact_base'),
                'absence_of_fact_is_not_absence_of_experience': True,
                'missing_information': requirement_gaps(line, [m['capability'] for m in matches]) if not fully_supported else [],
                'scope_note': 'Missing evidence is a data gap, not evidence of missing real experience. Each fact supports only its named competence.'}
        if kind == 'mandatory': mandatory.append(item)
        elif kind == 'optional': optional.append(item)
    return mandatory, optional, intersections


def score(job,candidate):
    facts=verified_facts(candidate);mandatory,optional,intersections=requirement_assessment(job['description'],facts)
    language=english_assessment(job['description'],candidate)
    profiles=candidate.get('profiles',{});results={}
    for key in candidate.get('cvs',{}):
        pr=profiles[key];caps=pr.get('capabilities',[])
        relevant=any(re.search(pattern,job['title'],re.I) for pattern in pr['title_patterns'])
        supported=[m for m in intersections if m['capability'] in caps]
        results[key]={'role_score':60 if relevant else 0,'candidate_evidence_score':0,
            'competency_coverage_score':0,'requirements_coverage':dict(Counter(r['status'] for r in mandatory)),
            'role_title_evidence':job['title'],'requirements':mandatory,'supported':supported,
            'unconfirmed':[r['text'] for r in mandatory if r['needs_review']],
            'fit_verified':False,'assessment_scope':'Configured role patterns and sourced evidence; human review required.'}
    if not results:raise ValueError('configure_at_least_one_resume')
    best=max(results,key=lambda key:(results[key]['role_score'],len(results[key]['supported'])))
    gaps=[r for r in mandatory if r['needs_review']]
    enough=len({m['fact_id'] for m in results[best]['supported']})>=2
    relevant=results[best]['role_score']>=60
    return {'profiles':results,'best_profile':best,'direction_relevant':relevant,
        'eligible_profiles':[key for key in results if results[key]['role_score']>=60],
        'role_level':'configured_profile','management_scope_evidence':[],'specialized_domain_evidence':[],
        'fact_base_complete':False,'missing_facts_are_not_negative_evidence':True,'candidate_fit_verified':False,
        'mandatory_requirements':mandatory,'optional_requirements':optional,'unconfirmed_requirements':gaps,
        'requirements_extraction_complete':False,'extracted_requirements_supported':bool(mandatory and not gaps),
        'critical_mismatches':language['critical_mismatches'],'language_risks':language['risks'],
        'further_analysis':'review_with_gaps' if relevant else 'outside_target_profiles',
        'domain_gaps':[],'letter_evidence_sufficient':enough,'assessment_limitations':['Human review required.']}


def channel(job):
    if job['provider'] == 'hh':
        return {'kind': 'hh_read_only', 'recipient': None, 'confirmed': False,
                'url': job['page_url'], 'automatic_form_submission': False, 'reason': 'HH integration is read-only'}
    for line in job['description'].splitlines():
        for address in re.findall(r'[A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', line):
            if re.search(r'privacy|support|dpo|legal|accessib|accommodat', address + ' ' + line, re.I): continue
            if re.search(r'(?:send|submit|email|apply|присыла|отправ|жд[её]м).{0,100}(?:resume|résumé|\bcv\b|резюме)|(?:resume|résumé|\bcv\b|резюме).{0,100}(?:send|submit|email|присыла|отправ|жд[её]м)', line, re.I):
                return {'kind': 'vacancy_email', 'recipient': address, 'confirmed': True,
                        'source_url': job['page_url'], 'evidence': line[:600], 'automatic_form_submission': False}
    return {'kind': 'hosted_form' if job['provider'] == 'lever' and job['apply_url'] else 'career_page',
            'url': job['apply_url'] or job['page_url'], 'confirmed': bool(job['apply_url']),
            'recipient': None, 'automatic_form_submission': False,
            'reason': 'No explicit job-specific resume email; public form URL does not authorize API submission'}


def hh_cv_snapshot(profile, root):
    """Verified HH content, not a historical PDF or a guessed target title."""
    manifest = root / 'cv/current-cv-manifest.json'
    if not manifest.exists(): return None
    entry = json.loads(manifest.read_text()).get('profiles', {}).get(profile, {})
    if entry.get('authoritative_source') != 'authorized_HH_resume_DOM': return None
    source = (root / entry['source_file']).resolve()
    if not source.is_relative_to(root.resolve()): raise ValueError('CV source outside project')
    observed = json.loads(source.read_text())['profiles'][profile]
    version = digest({k: observed[k] for k in ('resume_id', 'title', 'experience', 'skills')})
    if (version != entry.get('hh_version') or observed['resume_id'] != entry.get('hh_resume_id')
            or observed['title'] != entry.get('title_verified')):
        raise ValueError('HH CV snapshot changed; synchronization required')
    return {**observed, 'version': version}


def cv_choice(profile, candidate, root):
    configured = candidate.get('cvs', {}).get(profile)
    if not configured: return {'profile': profile, 'path': None, 'available': False, 'reason': 'final_cv_missing'}
    path = (root / configured).resolve()
    if not path.is_relative_to(root.resolve()): raise ValueError('CV must be inside the project')
    if not path.is_file(): return {'profile': profile, 'path': configured, 'available': False, 'reason': 'final_cv_missing'}
    result = {'profile': profile, 'path': str(path), 'available': True, 'sha256': digest(path.read_bytes())}
    try: hh = hh_cv_snapshot(profile, root)
    except (OSError, KeyError, TypeError, ValueError):
        return {**result, 'available': False, 'reason': 'hh_cv_sync_required'}
    if hh:
        result.update(hh_resume_id=hh['resume_id'], hh_title=hh['title'], hh_version=hh['version'],
                      authoritative_source='authorized_HH_resume_DOM', local_pdf_is_current=False)
    return result


class State:
    def __init__(self, path):
        path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=15); self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS jobs (key TEXT PRIMARY KEY, source TEXT NOT NULL, opportunity TEXT NOT NULL,
          data TEXT NOT NULL, process_hash TEXT, decision TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS applications (id TEXT PRIMARY KEY, job_key TEXT UNIQUE NOT NULL REFERENCES jobs(key),
          opportunity TEXT UNIQUE NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL, artifact_hash TEXT NOT NULL,
          approval_hash TEXT, approval_by TEXT, receipt TEXT);
        CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, time TEXT NOT NULL, job_key TEXT,
          application_id TEXT, stage TEXT NOT NULL, details TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, started TEXT NOT NULL, report TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
    def event(self, stage, job_key=None, app_id=None, **details):
        self.db.execute('INSERT INTO events(time,job_key,application_id,stage,details) VALUES(?,?,?,?,?)',
                        (now(), job_key, app_id, stage, json.dumps(details, ensure_ascii=False)))
    def close(self): self.db.close()


@contextmanager
def exclusive_run(folder):
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / '.run.lock').open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError('another run is active') from None
        try: yield
        finally: fcntl.flock(lock, fcntl.LOCK_UN)


def build_letter(job, match, candidate):
    selected = match['profiles'][match['best_profile']]['supported']
    by_fact = {}
    for entry in selected:
        if entry.get('letter_text') or candidate.get('test_only'):
            by_fact.setdefault(entry['fact_id'], entry)
    priorities = candidate['profiles'][match['best_profile']]['capabilities']
    entries = sorted(by_fact.values(), key=lambda e: priorities.index(e['capability']) if e['capability'] in priorities else len(priorities))
    if not match['letter_evidence_sufficient'] or len(entries) < 2:
        return {'body': '', 'status': 'insufficient_evidence', 'evidence': [],
                'reason': 'At least two independent confirmed facts and competencies are needed.'}
    # Deterministic selection favors distinct employers and minimizes repetition.
    chosen = []; sources = set()
    for entry in entries:
        if entry['fact_source'] not in sources:
            chosen.append(entry); sources.add(entry['fact_source'])
        if len(chosen) == 3: break
    for entry in entries:
        if len(chosen) >= 3: break
        if entry['fact_id'] not in {e['fact_id'] for e in chosen}: chosen.append(entry)
    intro = f'Здравствуйте! Рассматриваю вакансию «{job["title"].strip()}».'
    sentences = [e.get('letter_text') or e['candidate_evidence'] for e in chosen]
    body = intro + '\n\n' + '\n'.join(sentences)
    if candidate.get('name'): body += '\n' + candidate['name']
    return {'body': body + '\n', 'status': 'draft_requires_review', 'language': 'ru', 'voice': candidate.get('voice','neutral'),
            'evidence': [{'fact_id': e['fact_id'], 'source': e['fact_source'],
                          'job_evidence': e['job_evidence'], 'capability': e['capability']} for e in chosen],
            'scope': 'Only the stated intersections; gaps remain in match and block approval.'}


def prepare(state, job, decision, candidate, folder, root):
    existing = state.db.execute('SELECT * FROM applications WHERE job_key=? OR opportunity=?', (job['key'], job['opportunity_key'])).fetchone()
    if existing and (existing['state'] in CLOSED_SEND_STATES or existing['job_key'] != job['key']): return 'existing', existing['id']
    app_id = existing['id'] if existing else uuid.uuid4().hex
    selected_cv = cv_choice(decision['match']['best_profile'], candidate, root)
    send_channel = decision['channel']; blockers = []
    if job['provider'] == 'hh': blockers.append('hh_read_only')
    if not selected_cv['available']: blockers.append('final_cv_missing')
    if not decision['match']['candidate_fit_verified']: blockers.append('professional_facts_not_confirmed')
    if send_channel['kind'] != 'vacancy_email': blockers.append('job_specific_email_not_confirmed')
    sender = candidate.get('email')
    if not sender or (sender.endswith('.invalid') and not candidate.get('test_only')): blockers.append('sender_missing')
    letter = build_letter(job, decision['match'], candidate)
    body = letter['body']
    if not body: blockers.append('insufficient_letter_evidence')
    if decision['match']['unconfirmed_requirements']: blockers.append('mandatory_requirements_need_review')
    if decision['match']['language_risks']: blockers.append('language_requires_review')
    if not candidate.get('test_only') and decision['filter'].get('salary', {}).get('needs_review'): blockers.append('salary_requires_review')
    if not candidate.get('test_only'): blockers.append('human_match_review_required')
    payload = {'job_key': job['key'], 'job_content_hash': digest(job), 'profile': decision['match']['best_profile'], 'cv': selected_cv,
               'channel': send_channel, 'blockers': blockers, 'ready_for_approval': not blockers,
               'letter': letter, 'subject': f'Отклик — {job["title"]}', 'body': body, 'sender': sender, 'test_only': bool(candidate.get('test_only')),
               'message_id': json.loads(existing['payload'])['message_id'] if existing else f'<{app_id}@job-search-agent.invalid>',
               'match': decision['match'], 'filter_notes': decision['filter']['notes']}
    msg = EmailMessage(); msg['Subject'] = payload['subject']; msg['Message-ID'] = payload['message_id']
    msg['From'] = sender or 'unconfigured@example.invalid'
    if send_channel.get('recipient'): msg['To'] = send_channel['recipient']
    msg['X-Job-Agent-Application'] = app_id
    msg['X-Job-Agent-Ready'] = 'yes' if not blockers else 'no'
    msg.set_content(body)
    if selected_cv['available']:
        path = Path(selected_cv['path']); subtype = 'pdf' if path.suffix.lower() == '.pdf' else 'octet-stream'
        msg.add_attachment(path.read_bytes(), maintype='application', subtype=subtype, filename=path.name)
    # Stable artifact bytes across unchanged runs; hash covers email and attachments.
    encoded = msg.as_bytes(policy=policy.SMTP)
    artifact_hash = digest(encoded)
    outbox = folder / 'outbox'; outbox.mkdir(exist_ok=True)
    eml = outbox / (app_id + '.eml'); metadata = outbox / (app_id + '.json')
    if existing and json.loads(existing['payload']) == payload and eml.exists() and digest(eml.read_bytes()) == existing['artifact_hash']:
        return 'existing', app_id
    eml.write_bytes(encoded); metadata.write_text(json.dumps({**payload, 'artifact_sha256': artifact_hash}, ensure_ascii=False, indent=2) + '\n')
    if existing:
        state.db.execute('UPDATE applications SET state=?,payload=?,artifact_hash=?,approval_hash=NULL,approval_by=NULL WHERE id=?',
                         ('prepared', json.dumps(payload, ensure_ascii=False), artifact_hash, app_id))
        state.event('approval_invalidated', job['key'], app_id, reason='draft_or_requirements_changed')
    else:
        state.db.execute('INSERT INTO applications(id,job_key,opportunity,state,payload,artifact_hash) VALUES(?,?,?,?,?,?)',
                         (app_id, job['key'], job['opportunity_key'], 'prepared', json.dumps(payload, ensure_ascii=False), artifact_hash))
    state.event('prepared', job['key'], app_id, blockers=blockers, readiness=not blockers, artifact_hash=artifact_hash)
    return 'updated' if existing else 'created', app_id


def run(config, root=ROOT, http=None, state_dir=None, hh_client=None, from_saved=False):
    config = {**config, 'candidate': load_candidate(config['candidate'], root)}
    folder = Path(state_dir) if state_dir else root / 'var'
    http = http or PublicHTTP()
    with exclusive_run(folder):
        state = State(folder / 'jobs.sqlite3')
        try:
            report = {'run_id': uuid.uuid4().hex, 'started_at': now(), 'source_results': [], 'jobs': [],
                      'counts': {}, 'candidate_verified_facts': len(verified_facts(config['candidate']))}
            if from_saved: report.update(assessment_mode='saved_sqlite_no_network', new_search_performed=False)
            counts = Counter(); process_config = digest({'version': VERSION, 'settings': config['filters'], 'candidate': config['candidate']})
            # File bytes are part of processing version: changed CV invalidates old approval.
            cvs = {p: cv_choice(p, config['candidate'], root) for p in config['candidate'].get('cvs',{})}
            process_config = digest([process_config, cvs])
            for source in config['sources']:
                source_key = source['provider'] + ':' + source['board']
                pause = state.db.execute('SELECT value FROM meta WHERE key=?', ('pause:' + source_key,)).fetchone()
                if not from_saved and pause and datetime.fromisoformat(pause['value']) > datetime.now(timezone.utc):
                    report['source_results'].append({'source': source_key, 'error': 'cooldown', 'until': pause['value']}); continue
                try:
                    if from_saved:
                        raw_jobs = [json.loads(row['data']) for row in state.db.execute('SELECT data FROM jobs WHERE source=? ORDER BY key', (source_key,))]
                        complete = False
                    elif source['provider'] == 'hh' and hh_client is None:
                        try: hh_client = hh_api.HHClient(root)
                        except hh_api.HHError as exc: raise FetchError(exc.code, exc.status) from None
                    if not from_saved:
                        quick = (lambda raw: hh_quick_filter(raw, config['candidate'], config['filters'])) if source['provider'] == 'hh' else None
                        raw_jobs, complete = collect(source, http, hh_client, prefilter=quick)
                except FetchError as exc:
                    report['source_results'].append({'source': source_key, 'error': str(exc), 'status': exc.status})
                    if exc.status in (403, 429):
                        until = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
                        state.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('pause:' + source_key, until))
                    continue
                source_result = {'source': source_key, 'records': len(raw_jobs), 'complete': complete}
                if from_saved: source_result['mode'] = 'saved_sqlite_no_network'
                elif source['provider'] == 'hh':
                    source_result['search'] = getattr(hh_client, 'collection', {})
                    if source_result['search'].get('partial'):
                        source_result.update(error=source_result['search']['stopped_error'], status=source_result['search'].get('stopped_status'))
                report['source_results'].append(source_result)
                seen = set()
                for raw in raw_jobs:
                    try:
                        job = dict(raw) if from_saved else normalize(source, raw)
                        if from_saved and job['provider'] == 'hh':
                            job.setdefault('observation_scope', 'historical_sqlite')
                            if job.get('salary_raw'): job['salary'] = hh_api.normalize_salary(job['salary_raw'])
                            if not job.get('full_description_available'):
                                job['quick_filter'] = hh_card_assessment(job, config['candidate'], config['filters'])
                                job['detail_state'] = 'budget_deferred' if job['quick_filter']['passed'] else 'prefilter_rejected'
                    except (ValueError, TypeError, AttributeError):
                        counts['invalid_records'] += 1; complete = False
                        continue
                    key = job['key']; seen.add(key)
                    counts['fetched'] += 1
                    old = state.db.execute('SELECT * FROM jobs WHERE key=?', (key,)).fetchone()
                    version_hash = digest([job, process_config]); timestamp = old['last_seen'] if from_saved and old else now()
                    if old and old['process_hash'] == version_hash and (old['active'] or from_saved):
                        state.db.execute('UPDATE jobs SET last_seen=?,active=1 WHERE key=?', (timestamp, key))
                        decision = json.loads(old['decision']); counts['unchanged'] += 1
                    else:
                        state.db.execute('''INSERT INTO jobs(key,source,opportunity,data,first_seen,last_seen) VALUES(?,?,?,?,?,?)
                          ON CONFLICT(key) DO UPDATE SET data=excluded.data,last_seen=excluded.last_seen,active=1''',
                          (key, source_key, job['opportunity_key'], json.dumps(job, ensure_ascii=False), timestamp, timestamp))
                        if not old: counts['new_jobs'] += 1; state.event('found', key, source=source_key)
                        result = filters(job, config['candidate'], config['filters'])
                        match = score(job, config['candidate'])
                        match['critical_mismatches'] = sorted(set(match['critical_mismatches'] + result['reasons']))
                        if match['critical_mismatches']: match['further_analysis'] = 'deprioritize_critical_mismatch'
                        decision = {'filter': result, 'match': match, 'channel': channel(job)}
                        if result['passed'] and result['salary']['status'] == 'estimated_below_net_floor':
                            match['further_analysis'] = 'review_salary_first'
                        if not match['direction_relevant']: result['reasons'].append('outside_target_profiles'); result['passed'] = False
                        if job['provider'] == 'hh' and not job['full_description_available']:
                            result['reasons'] = sorted(set(result['reasons'] + ((job.get('quick_filter') or {}).get('reasons', []))))
                            if job['detail_state'] != 'prefilter_rejected': result['reasons'].append('full_description_pending_or_unavailable')
                            result['passed'] = False; match['candidate_fit_verified'] = False
                            match['further_analysis'] = 'description_pending' if job['detail_state'] != 'prefilter_rejected' else 'prefilter_rejected'
                        match['critical_mismatches'] = sorted(set(result['reasons']))
                        state.event('checked', key, passed=result['passed'], reasons=result['reasons'], notes=result['notes'])
                        if result['passed'] and match['candidate_fit_verified']:
                            state.event('matched', key, profile=match['best_profile'], evidence=match['profiles'][match['best_profile']]['supported'])
                        if result['passed']:
                            action, app_id = prepare(state, job, decision, config['candidate'], folder, root)
                            decision['application_id'] = app_id; counts['drafts_' + action] += 1
                        else:
                            app = state.db.execute('SELECT * FROM applications WHERE job_key=?', (key,)).fetchone()
                            if app and app['state'] not in CLOSED_SEND_STATES:
                                state.db.execute('UPDATE applications SET state=?,approval_hash=NULL WHERE id=?', ('blocked', app['id']))
                                state.event('approval_invalidated', key, app['id'], reason='hard_filter_changed')
                        state.db.execute('UPDATE jobs SET process_hash=?,decision=? WHERE key=?', (version_hash, json.dumps(decision, ensure_ascii=False), key))
                    if from_saved and old:
                        state.db.execute('UPDATE jobs SET last_seen=?,active=? WHERE key=?', (old['last_seen'], old['active'], key))
                    if not decision['filter']['reasons'] or decision['filter']['reasons'] == ['outside_target_profiles']:
                        counts['passed_preliminary_filters'] += 1
                    counts['passed_filters' if decision['filter']['passed'] else 'rejected'] += 1
                    if decision['filter']['passed'] and decision['match']['candidate_fit_verified']: counts['verified_matches'] += 1
                    report['jobs'].append({'key': key, 'title': job['title'], 'employer': job['employer'], 'url': job['page_url'],
                                           'locations': job['locations'],
                                           **({k: job[k] for k in ('id', 'published_at', 'salary', 'salary_raw', 'workplace', 'work_formats', 'discovered_profiles', 'source_queries', 'detail_state', 'full_description_available', 'quick_filter', 'observation_scope')} if job['provider'] == 'hh' else {}), **decision})
                if complete:
                    for old in state.db.execute('SELECT key FROM jobs WHERE source=? AND active=1', (source_key,)).fetchall():
                        if old['key'] not in seen:
                            state.db.execute('UPDATE jobs SET active=0 WHERE key=?', (old['key'],))
                            state.db.execute("UPDATE applications SET state='blocked',approval_hash=NULL WHERE job_key=? AND state NOT IN ('sent','confirmed','dispatching','send_uncertain')", (old['key'],))
                            state.event('checked', old['key'], passed=False, reasons=['no_longer_in_complete_public_listing'])
            for name in ('passed_preliminary_filters', 'fetched', 'new_jobs', 'unchanged', 'rejected', 'passed_filters', 'verified_matches', 'drafts_created', 'drafts_updated', 'drafts_existing'):
                counts.setdefault(name, 0)
            report['counts'] = dict(counts)
            report['stage_labels'] = STAGES
            if hh_client is not None or (from_saved and any(s['provider'] == 'hh' for s in config['sources'])):
                report['hh_profiles'] = hh_profile_report(report, hh_client)
            report['rejection_reasons'] = dict(Counter(reason for row in report['jobs'] for reason in row['filter']['reasons']))
            report['requests'] = getattr(http, 'requests', []) + (getattr(hh_client, 'requests', []) if hh_client else [])
            report['applications_total'] = state.db.execute('SELECT count(*) FROM applications').fetchone()[0]
            report['application_states'] = dict(state.db.execute('SELECT state,count(*) FROM applications GROUP BY state').fetchall())
            report['employer_messages_sent'] = sum(report['application_states'].get(x, 0) for x in ('sent', 'confirmed'))
            state.db.execute('INSERT INTO runs VALUES(?,?,?)', (report['run_id'], report['started_at'], json.dumps(report, ensure_ascii=False)))
            state.db.commit()
            (folder / 'latest-run.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
            return report
        finally: state.close()


def hh_profile_report(report,client):
    keys=set(key for job in report['jobs'] for key in job.get('match',{}).get('profiles',{}))
    return {key:{'queries':[q for q in getattr(client,'collection',{}).get('queries',[]) if q['profile']==key],
        'top':[],'full_descriptions':sum(j.get('full_description_available',False) for j in report['jobs'])} for key in sorted(keys)}


def approval_check(state, folder, app_id):
    app = state.db.execute('SELECT * FROM applications WHERE id=?', (app_id,)).fetchone()
    if not app: raise ValueError('unknown application')
    payload = json.loads(app['payload'])
    if payload['blockers']: raise ValueError('not ready: ' + ', '.join(payload['blockers']))
    path = folder / 'outbox' / (app_id + '.eml')
    encoded = path.read_bytes()
    if digest(encoded) != app['artifact_hash']: raise ValueError('email artifact changed; prepare and review again')
    cv = payload['cv']
    if not Path(cv['path']).is_file() or digest(Path(cv['path']).read_bytes()) != cv['sha256']: raise ValueError('CV changed')
    active = state.db.execute('SELECT active FROM jobs WHERE key=?', (app['job_key'],)).fetchone()
    if not active or not active['active']: raise ValueError('vacancy no longer active')
    if payload['channel']['kind'] != 'vacancy_email': raise ValueError('no job-specific application email')
    return app, payload, encoded


def approve(folder, app_id, reviewer, expected_hash=None):
    state = State(folder / 'jobs.sqlite3')
    try:
        state.db.execute('BEGIN IMMEDIATE')
        app, payload, encoded = approval_check(state, folder, app_id)
        if expected_hash is not None and app['artifact_hash'] != expected_hash: raise ValueError('reviewed artifact hash changed')
        if app['state'] not in ('prepared', 'approved'): raise ValueError('application not in reviewable state')
        state.db.execute("UPDATE applications SET state='approved',approval_hash=artifact_hash,approval_by=? WHERE id=?", (reviewer, app_id))
        state.event('approved', app['job_key'], app_id, reviewer=reviewer, artifact_hash=app['artifact_hash'])
        state.db.commit(); return app['artifact_hash']
    finally: state.close()


def smtp_transport(encoded, host, port, sender, recipient, *, local_test=False, user=None, password=None, tls_context=None):
    if local_test:
        if host not in ('127.0.0.1', '::1') or not recipient.endswith('.invalid') or not sender.endswith('.invalid'):
            raise ValueError('self-test permits loopback and synthetic .invalid addresses only')
        server = smtplib.SMTP(host, port, timeout=10)
    else:
        if not host or not user or not password: raise ValueError('SMTP credentials are required for real mail')
        context = tls_context if tls_context is not None else ssl.create_default_context()
        if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
            raise ValueError('SMTP TLS certificate verification required')
        server = smtplib.SMTP_SSL(host, port, timeout=20, context=context)
    with server:
        if not local_test: server.login(user, password)
        refused = server.sendmail(sender, [recipient], encoded)
        if refused: raise ValueError('SMTP recipient refused')
    return {'smtp_accepted_at': now(), 'transport': 'local_test' if local_test else 'smtp_ssl', 'host': host,
            'message_id': BytesParser(policy=policy.default).parsebytes(encoded)['Message-ID'], 'delivery_confirmed': False}


def send_approved(folder, app_id, expected_hash, smtp, *, local_test=False):
    if not local_test:
        from portable_runtime import require
        require(Path(folder).parent,'mail')
    state = State(folder / 'jobs.sqlite3')
    try:
        state.db.execute('BEGIN IMMEDIATE')
        app, payload, encoded = approval_check(state, folder, app_id)
        if payload.get('test_only') and not local_test: raise ValueError('synthetic applications cannot use real SMTP')
        if app['state'] != 'approved' or app['approval_hash'] != expected_hash or app['artifact_hash'] != expected_hash:
            raise ValueError('missing valid approval or already dispatched')
        state.db.execute("UPDATE applications SET state='dispatching' WHERE id=?", (app_id,))
        state.event('dispatching', app['job_key'], app_id, artifact_hash=expected_hash, local_test=local_test)
        state.db.commit()  # Claim before network. Never automatically retry after an ambiguous failure.
        try:
            receipt = smtp_transport(encoded, smtp['host'], smtp['port'], payload['sender'], payload['channel']['recipient'],
                                     local_test=local_test, user=smtp.get('user'), password=smtp.get('password'))
        except Exception as exc:
            state.db.execute("UPDATE applications SET state='send_uncertain' WHERE id=?", (app_id,))
            state.event('send_uncertain', app['job_key'], app_id, error_class=type(exc).__name__, no_automatic_retry=True)
            state.db.commit(); raise
        state.db.execute("UPDATE applications SET state='sent',receipt=? WHERE id=?", (json.dumps(receipt), app_id))
        state.event('sent', app['job_key'], app_id, **receipt)
        state.db.commit(); return receipt
    finally: state.close()


def confirm(folder, app_id, reference):
    if not reference.strip(): raise ValueError('external confirmation reference is required')
    state = State(folder / 'jobs.sqlite3')
    try:
        app = state.db.execute('SELECT * FROM applications WHERE id=?', (app_id,)).fetchone()
        if not app or app['state'] != 'sent': raise ValueError('only actually sent mail can be confirmed')
        state.db.execute("UPDATE applications SET state='confirmed' WHERE id=?", (app_id,))
        state.event('confirmed', app['job_key'], app_id, external_reference=reference)
        state.db.commit()
    finally: state.close()


class TestSMTP(socketserver.StreamRequestHandler):
    """Synthetic loopback sink, never a relay."""
    def handle(self):
        self.wfile.write(b'220 synthetic-test.invalid ESMTP\r\n')
        while True:
            line = self.rfile.readline()
            if not line: break
            verb = line.split(b' ', 1)[0].strip().upper()
            if verb in (b'EHLO', b'HELO'): self.wfile.write(b'250 synthetic-test.invalid\r\n')
            elif verb in (b'MAIL', b'RCPT', b'RSET', b'NOOP'): self.wfile.write(b'250 OK\r\n')
            elif verb == b'DATA':
                self.wfile.write(b'354 End with dot\r\n'); lines = []
                while True:
                    part = self.rfile.readline()
                    if part in (b'.\r\n', b'', b'.\n'): break
                    lines.append(part[1:] if part.startswith(b'..') else part)
                self.server.messages.append(b''.join(lines))
                self.wfile.write(b'250 queued synthetic-only\r\n')
            elif verb == b'QUIT': self.wfile.write(b'221 bye\r\n'); break
            else: self.wfile.write(b'500 unsupported\r\n')


@contextmanager
def smtp_sink():
    server = socketserver.TCPServer(('127.0.0.1', 0), TestSMTP); server.messages = []
    worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
    try: yield server
    finally: server.shutdown(); server.server_close(); worker.join(timeout=2)


def synthetic_config(root):
    raise ValueError('Use portable.py demo: isolated synthetic fixtures only')


def test_email(folder):
    raise ValueError('SMTP self-test disabled; offline demo never opens sockets')


def summary(report):
    result = {key: report[key] for key in ('run_id', 'counts', 'rejection_reasons', 'candidate_verified_facts', 'applications_total', 'application_states', 'employer_messages_sent', 'source_results')}
    result['prepared_candidates'] = [{'title': x['title'], 'profile': x['match']['best_profile'],
                                     'requirements_coverage': x['match']['profiles'][x['match']['best_profile']]['requirements_coverage'],
                                     'confirmed_fact_ids': sorted({e['fact_id'] for e in x['match']['profiles'][x['match']['best_profile']]['supported']}),
                                     'further_analysis': x['match']['further_analysis'],
                                     'unconfirmed_requirements': [r['text'] for r in x['match']['unconfirmed_requirements']],
                                     'application_id': x.get('application_id'), 'channel': x['channel']['kind'],
                                     'flags': x['filter']['notes']} for x in report['jobs'] if x['filter']['passed']]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'pipeline-config.json')
    parser.add_argument('--state-dir', type=Path, default=ROOT / 'var')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('run'); sub.add_parser('reassess'); sub.add_parser('status'); sub.add_parser('test-email')
    ap = sub.add_parser('approve'); ap.add_argument('application'); ap.add_argument('--reviewer', required=True); ap.add_argument('--expected-hash', required=True); ap.add_argument('--acknowledge-reviewed', action='store_true', required=True)
    sp = sub.add_parser('send'); sp.add_argument('application'); sp.add_argument('--approval-hash', required=True); sp.add_argument('--live', action='store_true', required=True)
    cp = sub.add_parser('confirm'); cp.add_argument('application'); cp.add_argument('--receipt-reference', required=True)
    args = parser.parse_args(); folder = args.state_dir.resolve()
    if not folder.is_relative_to(ROOT): parser.error('state directory must remain inside the project')
    if not args.config.resolve().is_relative_to(ROOT): parser.error('config must remain inside the project')
    if args.command in ('run', 'reassess'):
        result = run(json.loads(args.config.read_text()), root=ROOT, state_dir=folder, from_saved=args.command == 'reassess')
        print(json.dumps(summary(result), ensure_ascii=False, indent=2))
        return 1 if any('error' in x for x in result['source_results']) else 0
    if args.command == 'status':
        state = State(folder / 'jobs.sqlite3')
        print(json.dumps({'jobs': state.db.execute('SELECT count(*) FROM jobs').fetchone()[0],
                          'applications': [dict(x) for x in state.db.execute('SELECT id,job_key,state,artifact_hash FROM applications')],
                          'stages': [dict(x) for x in state.db.execute('SELECT stage,count(*) count FROM events GROUP BY stage')]}, ensure_ascii=False, indent=2)); state.close()
    elif args.command == 'test-email': print(json.dumps(test_email(folder), ensure_ascii=False, indent=2))
    elif args.command == 'approve': print(json.dumps({'approval_hash': approve(folder, args.application, args.reviewer, args.expected_hash)}))
    elif args.command == 'send':
        smtp = {'host': os.getenv('JOB_SMTP_HOST'), 'port': int(os.getenv('JOB_SMTP_PORT', '465')),
                'user': os.getenv('JOB_SMTP_USER'), 'password': os.getenv('JOB_SMTP_PASSWORD')}
        if not all(smtp.get(k) for k in ('host', 'user', 'password')): parser.error('real SMTP environment not configured; no dispatch attempted')
        print(json.dumps(send_approved(folder, args.application, args.approval_hash, smtp), indent=2))
    else: confirm(folder, args.application, args.receipt_reference)
    return 0


if __name__ == '__main__':
    try: raise SystemExit(main())
    except (ValueError, FetchError) as exc: raise SystemExit(str(exc)) from None
