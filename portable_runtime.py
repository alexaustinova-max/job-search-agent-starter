"""Installation boundaries and explicit capability switches for the starter."""
import json
from contextlib import closing
import math
from pathlib import Path
import re
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent


def read(root, name):
    return json.loads(inside(root, name).read_text(encoding='utf-8'))


def inside(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError('path_must_be_inside_this_installation')
    return path


def settings(root=ROOT):
    return read(root, 'portable-config.json')


def timezone(root=ROOT):
    value = settings(root)['timezone']
    ZoneInfo(value)
    return value


def identity(root=ROOT):
    value = settings(root).get('installation_id')
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{32}', value):
        raise ValueError('run_portable_init_first')
    return value


def validate_profile(root=ROOT):
    cfg = read(root, 'pipeline-config.hh.json')
    candidate = cfg['candidate']
    cvs = candidate['cvs']
    if not isinstance(cvs, dict) or not 1 <= len(cvs) <= 16:
        raise ValueError('configure_one_or_more_resumes')
    if set(cvs) != set(candidate['profiles']):
        raise ValueError('resume_and_profile_keys_must_match')
    import pipeline as p
    loaded = p.load_candidate(candidate, Path(root))
    if not p.verified_facts(loaded):
        raise ValueError('add_your_verified_career_facts')
    for key, cv in cvs.items():
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', key) or not inside(root, cv).is_file():
            raise ValueError('resume_missing_or_invalid_key')
        pr = candidate['profiles'][key]
        if not pr.get('title_patterns') or not pr.get('capabilities'):
            raise ValueError('configure_role_patterns_and_capabilities')
        if any(cap not in p.CAPABILITIES for cap in pr['capabilities']):
            raise ValueError('unknown_profile_capability')
        for pattern in pr['title_patterns']:
            re.compile(pattern)
    filters = cfg['filters']
    if filters['salary_basis'] not in ('NET', 'GROSS'):
        raise ValueError('salary_basis_must_be_NET_or_GROSS')
    for key in ('min_monthly_rub', 'desired_monthly_rub'):
        value=filters[key]
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:
            raise ValueError('invalid_salary')
    if filters['desired_monthly_rub'] < filters['min_monthly_rub']:
        raise ValueError('desired_salary_below_minimum')
    if not set(filters['allowed_work_formats']) <= {'remote','hybrid','on-site','unspecified'}:
        raise ValueError('invalid_work_format')
    for key in ('exclude_title_patterns','exclude_text_patterns','required_text_patterns'):
        for pattern in filters[key]:re.compile(pattern)
    sources=cfg['sources']
    if len(sources)!=1 or sources[0].get('provider')!='hh':
        raise ValueError('starter_supports_configured_HH_source_only')
    searches=sources[0]['searches']
    if not 1 <= len(searches) <= 16 or len({q['id'] for q in searches})!=len(searches):
        raise ValueError('configure_unique_HH_queries')
    if any(q['profile'] not in cvs or not q['text'].strip() for q in searches):
        raise ValueError('query_must_reference_your_resume_profile')
    policy=settings(root)
    timezone(root)
    for key, low, high in [('max_vacancies',1,90),('max_search_requests',1,30),('max_applications_per_day',1,15)]:
        if type(policy[key]) is not int or not low<=policy[key]<=high:raise ValueError('invalid_'+key)
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',policy['daily_time']):
        raise ValueError('invalid_daily_time')
    return cfg


def require(root, action):
    identity(root)
    cfg=settings(root)
    if action not in ('hh','browser','ai','dispatch','mail'):
        raise ValueError('unknown_capability')
    if cfg.get(action+'_enabled') is not True:
        raise ValueError(action+'_disabled_in_portable_config')
    if cfg.get('profile_confirmed') is not True:
        raise ValueError('confirm_your_own_profile_first')
    validate_profile(root)
    database=inside(root,'var/jobs.sqlite3')
    if not database.is_file():raise ValueError('run_portable_init_first')
    import sqlite3
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as con:
        row=con.execute("SELECT value FROM meta WHERE key='starter:installation-id'").fetchone()
        if not row or json.loads(row[0])!=identity(root):raise ValueError('database_identity_mismatch')
    if action=='ai':
        from llm_prepare import settings as llm_settings
        llm=llm_settings(Path(root))
        if (not llm['enabled'] or not llm.get('spending_approval') or
                not llm.get('pricing_confirmed_by_owner') or llm['max_calls_per_day']<=0):
            raise ValueError('AI_requires_own_key_reviewed_prices_and_explicit_budget')
    if action=='dispatch':
        dispatch=read(root,'dispatch-config.json')
        if dispatch.get('paused'):raise ValueError('all_application_submission_paused_by_user')
        if not dispatch['enabled']:raise ValueError('dispatch_config_disabled')
        if type(dispatch['max_confirmed_per_day']) is not int or not 1<=dispatch['max_confirmed_per_day']<=cfg['max_applications_per_day']:
            raise ValueError('dispatch_limit_exceeds_portable_daily_limit')
        if not cfg.get('browser_enabled') or not cfg.get('hh_enabled'):
            raise ValueError('dispatch_requires_own_HH_and_browser')
    return cfg
