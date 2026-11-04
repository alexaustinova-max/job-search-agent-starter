#!/usr/bin/env python3
"""Official HH app-token integration. No applicant OAuth, browser or application writes."""
import argparse
from contextlib import contextmanager
import ctypes
import fcntl
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import warnings
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent
API = 'https://api.hh.ru'
MARKER = b'\n__HH_APP_HTTP_PORTABLE__\n'
SEARCHES = []


class HHError(Exception):
    """Only fixed diagnostic codes; never include server prose, credentials or tokens."""
    def __init__(self, code, status=None):
        super().__init__(code); self.code = code; self.status = status


class Keychain:
    """Native macOS Keychain API: secret bytes never enter process arguments or files."""
    def __init__(self, root):
        if sys.platform != 'darwin': raise HHError('macos_keychain_required')
        self.security = ctypes.CDLL('/System/Library/Frameworks/Security.framework/Security')
        self.cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        self.service = b'Job Search Agent HH API'
        self.account = ('starter-'+__import__('portable_runtime').identity(root)+'-' + hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]).encode()
        ptr = ctypes.c_void_p; uint = ctypes.c_uint32; chars = ctypes.c_char_p
        self.security.SecKeychainFindGenericPassword.argtypes = [ptr, uint, chars, uint, chars, ctypes.POINTER(uint), ctypes.POINTER(ptr), ctypes.POINTER(ptr)]
        self.security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        self.security.SecKeychainAddGenericPassword.argtypes = [ptr, uint, chars, uint, chars, uint, ptr, ctypes.POINTER(ptr)]
        self.security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        self.security.SecKeychainItemModifyAttributesAndData.argtypes = [ptr, ptr, uint, ptr]
        self.security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
        self.security.SecKeychainItemFreeContent.argtypes = [ptr, ptr]
        self.cf.CFRelease.argtypes = [ptr]

    def _find(self):
        length = ctypes.c_uint32(); data = ctypes.c_void_p(); item = ctypes.c_void_p()
        status = self.security.SecKeychainFindGenericPassword(None, len(self.service), self.service, len(self.account), self.account, ctypes.byref(length), ctypes.byref(data), ctypes.byref(item))
        if status == -25300: return None, None  # errSecItemNotFound
        if status != 0: raise HHError('keychain_access_failed')
        try: raw = ctypes.string_at(data, length.value)
        finally: self.security.SecKeychainItemFreeContent(None, data)
        return raw, item

    def load(self):
        raw, item = self._find()
        if raw is None: return {}
        try:
            value = json.loads(raw)
            if not isinstance(value, dict): raise HHError('keychain_record_invalid')
            return value
        except (ValueError, UnicodeError): raise HHError('keychain_record_invalid') from None
        finally: self.cf.CFRelease(item)

    def save(self, value):
        data = json.dumps(value, ensure_ascii=True).encode()
        _, item = self._find()
        if item:
            try: status = self.security.SecKeychainItemModifyAttributesAndData(item, None, len(data), data)
            finally: self.cf.CFRelease(item)
        else:
            status = self.security.SecKeychainAddGenericPassword(None, len(self.service), self.service, len(self.account), self.account, len(data), data, None)
        if status != 0: raise HHError('keychain_write_failed')


class CurlTransport:
    def __init__(self): self.requests = []

    def request(self, method, path, *, headers, params=None, form=None):
        if not ((method == 'POST' and path == '/token' and (form or {}).get('grant_type') == 'client_credentials') or
                (method == 'GET' and (path == '/vacancies' or re.fullmatch(r'/vacancies/[0-9]+', path)))):
            raise HHError('endpoint_not_allowed')
        url = API + path + ('?' + urlencode(params, doseq=True) if params else '')
        # The curl configuration, headers and form go through stdin, never argv.
        def quote(value):
            if '\r' in value or '\n' in value: raise HHError('invalid_header_or_form')
            return json.dumps(value, ensure_ascii=True)
        config = [f'url = {quote(url)}', f'request = {quote(method)}', 'silent', 'show-error',
                  'proto = "=https"', 'max-redirs = 0', 'retry = 0',
                  'connect-timeout = 10', 'max-time = 30', 'max-filesize = 10485760',
                  'write-out = "\\n__HH_APP_HTTP_PORTABLE__\\n%{http_code}"']
        config += ['header = ' + quote(k + ': ' + v) for k, v in headers.items()]
        if form is not None: config.append('data = ' + quote(urlencode(form)))
        try:
            response = subprocess.run(['/usr/bin/curl', '-q', '--config', '-'], input=('\n'.join(config) + '\n').encode(), capture_output=True, timeout=35, check=False)
        except (OSError, subprocess.TimeoutExpired): raise HHError('transport_error') from None
        body, sep, status_bytes = response.stdout.rpartition(MARKER)
        status = int(status_bytes) if sep and status_bytes.isdigit() and status_bytes != b'000' else None
        self.requests.append({'method': method, 'path': path, 'status': status, 'bytes': len(body)})
        if response.returncode: raise HHError('transport_error', status)
        try: payload = json.loads(body)
        except (ValueError, UnicodeError): raise HHError('invalid_json', status) from None
        if not isinstance(payload, dict): raise HHError('invalid_schema', status)
        if status != 200:
            code = {400: 'bad_request', 401: 'token_invalid_no_automatic_reissue', 403: 'forbidden_or_captcha', 404: 'vacancy_unavailable', 429: 'rate_limited'}.get(status, 'http_error')
            raise HHError(code, status)
        if payload.get('captcha_url') or any(isinstance(e, dict) and e.get('type') == 'captcha_required' for e in (payload.get('errors') or [])):
            raise HHError('captcha_required', status)
        return payload


def private_dir(root):
    folder = root / 'var' / 'hh'
    if folder.is_symlink(): raise HHError('unsafe_state_path')
    folder.mkdir(parents=True, exist_ok=True, mode=0o700); folder.chmod(0o700)
    return folder


@contextmanager
def locked(root):
    folder = private_dir(root)
    fd = os.open(folder / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as handle:
        try: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise HHError('hh_operation_already_running') from None
        try: yield
        finally: fcntl.flock(handle, fcntl.LOCK_UN)


def write_report(root, name, value):
    path = private_dir(root) / name
    if path.is_symlink(): raise HHError('unsafe_state_path')
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as handle: json.dump(value, handle, ensure_ascii=False, indent=2); handle.write('\n')
    os.replace(temporary, path)


class HHClient:
    def __init__(self, root=ROOT, store=None, transport=None):
        self.root = root; __import__('portable_runtime').require(root,'hh'); self.store = store if store is not None else Keychain(root)
        self.transport = transport if transport is not None else CurlTransport()
        self.details = {}; self.requests = self.transport.requests

    def headers(self, record, authenticated=True):
        email = record.get('contact_email', '')
        if not re.fullmatch(r'[A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', email):
            raise HHError('contact_email_missing_or_invalid')
        headers = {'User-Agent': f'JobSearchAgent/1.0 ({email})', 'Accept': 'application/json'}
        if authenticated:
            token = record.get('access_token')
            if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9._~+/=-]+', token): raise HHError('run_local_setup_first')
            headers['Authorization'] = 'Bearer ' + token
        return headers

    def obtain_token_once(self):
        record = self.store.load()
        if record.get('access_token'): return 'reused'
        if not record.get('client_id') or not record.get('client_secret'): raise HHError('run_local_setup_first')
        if record.get('token_request_pending'): raise HHError('token_request_uncertain_check_dev_portal')
        if time.time() - record.get('token_request_at', 0) < 300: raise HHError('token_request_five_minute_guard')
        headers = self.headers(record, authenticated=False)
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
        record.update(token_request_pending=True, token_request_at=time.time()); self.store.save(record)
        try:
            response = self.transport.request('POST', '/token', headers=headers,
                                              form={'grant_type': 'client_credentials', 'client_id': record['client_id'], 'client_secret': record['client_secret']})
        except HHError as exc:
            if exc.status in (400, 403):
                record['token_request_pending'] = False; self.store.save(record)
            raise
        token = response.get('access_token')
        if response.get('token_type', '').lower() != 'bearer' or not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9._~+/=-]+', token):
            raise HHError('unexpected_token_response')
        # App tokens have no expiry/refresh flow. An invalid token stops GETs.
        record.update(access_token=token, token_request_pending=False, token_obtained_at=time.time())
        self.store.save(record); return 'issued_once'

    def get(self, path, params=None):
        record = self.store.load()
        if record.get('blocked_until', 0) > time.time(): raise HHError('hh_cooldown_active')
        if isinstance(self.transport, CurlTransport):
            time.sleep(max(0, 0.25 - (time.monotonic() - getattr(self, '_last_get', 0))))
            self._last_get = time.monotonic()
        try: return self.transport.request('GET', path, headers=self.headers(record), params=params)
        except HHError as exc:
            if exc.status in (401, 403, 429) or exc.code == 'captcha_required':
                record['blocked_until'] = time.time() + 21600; self.store.save(record)
            raise

    def search(self, text, per_page=5, page=0, *, bounded_search=False):
        if not isinstance(text, str) or not text.strip(): raise HHError('empty_search')
        if not isinstance(per_page, int) or not 1 <= per_page <= (20 if bounded_search else 5) or not isinstance(page, int) or page < 0 or (page + 1) * per_page > 2000:
            raise HHError('search_bounds_exceeded')
        data = self.get('/vacancies', {'text': text, 'search_field': 'name', 'page': page, 'per_page': per_page, 'order_by': 'publication_time'})
        items = data.get('items')
        if not isinstance(items, list) or len(items) > per_page or not all(isinstance(x, dict) and re.fullmatch(r'[0-9]+', str(x.get('id', ''))) for x in items):
            raise HHError('invalid_search_schema')
        return data

    def detail(self, vacancy_id):
        vacancy_id = str(vacancy_id)
        if not re.fullmatch(r'[0-9]+', vacancy_id): raise HHError('invalid_vacancy_id')
        if vacancy_id not in self.details:
            item = self.get('/vacancies/' + vacancy_id)
            if str(item.get('id')) != vacancy_id or not isinstance(item.get('description'), str) or not item['description'].strip():
                raise HHError('full_description_unavailable')
            self.details[vacancy_id] = item
        return self.details[vacancy_id]

    def collect(self, source, prefilter=None):
        with locked(self.root):
            return self._collect(source, prefilter)

    def _collect(self, source, prefilter):
        if not self.store.load().get('control_success'): raise HHError('successful_control_required')
        searches = source.get('searches', [])
        ac_control=False
        expected_profiles={q['profile'] for q in searches}
        if not 1<=len(searches)<=16 or not expected_profiles:
            raise HHError('configure_searches_for_your_resumes')
        pages, per_page, detail_limit = source.get('max_pages', 1), source.get('per_page', 5), source.get('detail_limit', 15)
        if not 1 <= pages <= 2 or not 1 <= per_page <= 20 or not 1 <= detail_limit <= 90:
            raise HHError('collection_budget_exceeded')
        if ac_control and (pages > 1 or per_page > 10 or detail_limit > 20):
            raise HHError('ac_control_budget_exceeded')
        policy=__import__('portable_runtime').settings(self.root)
        detail_limit=min(detail_limit,policy['max_vacancies'])
        request_count=0
        jobs = {}; self.details = {}
        cache = self.root / 'var/hh/search-cache.json'
        if source.get('use_recent_details') and cache.is_file() and time.time() - cache.stat().st_mtime < 900:
            cached = json.loads(cache.read_text())
            self.details = {str(x['id']): public_fields(x) for x in cached.get('items', []) if x.get('_detail_state') == 'full'}
        self.collection = {'control_profiles': sorted(expected_profiles), 'queries': [], 'max_pages': pages, 'per_page': per_page,
                           'detail_limit': detail_limit, 'detail_requests': 0, 'processed_details': 0, 'cached_details_used': 0, 'partial': False}
        # Independent query/page calls, one union by official HH ID.
        for search in searches:
            if request_count>=policy['max_search_requests'] or len(jobs)>=policy['max_vacancies']:break
            q = {'id': search.get('id', search['profile']), 'profile': search['profile'],
                 'text': search['text'], 'found': None, 'pages_fetched': 0, 'items_received': 0}
            for page in range(pages):
                if request_count>=policy['max_search_requests'] or len(jobs)>=policy['max_vacancies']:break
                request_count+=1
                data = self.search(search['text'], per_page, page, bounded_search=True)
                if page == 0: q['found'] = data.get('found')
                q['pages_fetched'] += 1; q['items_received'] += len(data['items'])
                for item in data['items']:
                    if len(jobs)>=policy['max_vacancies']:break
                    vacancy_id = str(item['id'])
                    if vacancy_id not in jobs:
                        jobs[vacancy_id] = {**public_fields(item), '_search_profiles': [], '_search_queries': [], '_detail_state': 'summary', '_observation_scope': 'current_ac_control' if ac_control else 'current_full_search'}
                    job = jobs[vacancy_id]
                    job['_search_profiles'] = sorted(set(job['_search_profiles'] + [search['profile']]))
                    job['_search_queries'].append({'id': q['id'], 'profile': search['profile'], 'text': search['text'], 'page': page})
                if not data['items'] or page + 1 >= data.get('pages', 1): break
            self.collection['queries'].append(q)
            if isinstance(self.transport, CurlTransport): print(json.dumps({'hh_query': q['id'], 'profile': q['profile'], 'found': q['found'],
                              'received': q['items_received'], 'pages': q['pages_fetched']}, ensure_ascii=False), flush=True)
        eligible = []
        for item in jobs.values():
            quick = prefilter(item) if prefilter else {'passed': True, 'reasons': [], 'priority': 0}
            item['_quick_filter'] = quick
            if quick['passed']: eligible.append(item)
            else: item['_detail_state'] = 'prefilter_rejected'
        self.collection['unique_cards'] = len(jobs)
        self.collection['quick_passed'] = len(eligible)
        # Round-robin directions so broad product queries cannot consume all details.
        queues = {profile: sorted([x for x in eligible if profile in x['_search_profiles']],
                                  key=lambda x: (-x['_quick_filter'].get('priority', 0), x['id'])) for profile in sorted(expected_profiles)}
        selected = []; selected_ids = set()
        while len(selected) < min(detail_limit, len(eligible)):
            progress = False
            for profile in sorted(expected_profiles):
                while queues[profile] and queues[profile][0]['id'] in selected_ids: queues[profile].pop(0)
                if queues[profile] and len(selected) < detail_limit:
                    item = queues[profile].pop(0); selected.append(item); selected_ids.add(item['id']); progress = True
            if not progress: break
        for item in eligible:
            if item['id'] not in selected_ids: item['_detail_state'] = 'budget_deferred'
        for item in selected:
            self.collection['processed_details'] += 1
            if item['id'] in self.details: self.collection['cached_details_used'] += 1
            else: self.collection['detail_requests'] += 1
            try:
                detail = public_fields(self.detail(item['id']))
                item.update(detail); item['_detail_state'] = 'full'
            except HHError as exc:
                if exc.status == 404:
                    item['_detail_state'] = 'unavailable'; continue
                # Stop the run on auth/captcha/rate/transport failure; retain cards and history.
                item['_detail_state'] = 'detail_failed'
                self.collection.update(partial=True, stopped_error=exc.code, stopped_status=exc.status)
                break
            if isinstance(self.transport, CurlTransport) and self.collection['processed_details'] % 10 == 0:
                print(json.dumps({'hh_details_loaded': self.collection['detail_requests'], 'budget': detail_limit}), flush=True)
        for item in eligible:
            if item['_detail_state'] == 'summary': item['_detail_state'] = 'budget_deferred'
        self.collection['full_descriptions'] = sum(x['_detail_state'] == 'full' for x in jobs.values())
        write_report(self.root, 'ac-control-cache.json' if ac_control else 'search-cache.json', {'items': list(jobs.values()), 'collection': self.collection})
        return list(jobs.values()), False


def public_fields(item):
    names = {'id', 'name', 'employer', 'area', 'salary', 'salary_range', 'schedule', 'work_format',
             'alternate_url', 'description', 'published_at', 'created_at', 'snippet', 'experience', 'languages', 'key_skills'}
    result = {k: v for k, v in item.items() if k in names}
    if isinstance(result.get('employer'), dict):
        result['employer'] = {k: v for k, v in result['employer'].items() if k in ('id', 'name', 'alternate_url')}
    return result


class ReplayClient:
    """Local replay for dedup checks: no Keychain access and no network."""
    def __init__(self, root, cache_name='search-cache.json'):
        if cache_name not in ('search-cache.json','ac-control-cache.json','leadership-merged-cache.json'): raise HHError('invalid_cache_name')
        data = json.loads((root / 'var/hh' / cache_name).read_text())
        self.items = data['items']; self.collection = data['collection']; self.requests = []
    def collect(self, source, prefilter=None):
        return self.items, False


def prepare_config(root):
    return 'pipeline-config.hh.json'


def control(client):
    record = client.store.load()
    path = client.root / 'var' / 'hh' / 'control-result.json'
    if record.get('control_attempted'):
        if path.is_file(): return json.loads(path.read_text())
        raise HHError('control_already_attempted_no_automatic_retry')
    record['control_attempted'] = True; client.store.save(record)
    result = {'application_request': 'user-configured', 'read_only': True, 'search_limit': 5,
              'checked_at_unix': time.time(), 'success': False, 'search_requests': 1}
    try:
        config=json.loads((client.root/'pipeline-config.hh.json').read_text())
        query=next(s for s in config['sources'] if s['provider']=='hh')['searches'][0]['text']
        data = client.search(query)
        result.update(search_http_status=200, items_count=len(data['items']), found=data.get('found'),
                      page=data.get('page'), pages=data.get('pages'), fields=sorted(data.keys()),
                      item_fields=sorted(data['items'][0].keys()) if data['items'] else [],
                      jobs=[{'id': x['id'], 'name': x.get('name'), 'employer': (x.get('employer') or {}).get('name'), 'url': x.get('alternate_url')} for x in data['items']])
        if not data['items']: raise HHError('search_empty_description_not_checked')
        item = client.detail(data['items'][0]['id'])
        result.update(detail_http_status=200, detail_fields=sorted(item.keys()),
                      description_chars=len(item['description']), full_description_available=True, success=True)
        # Gate pipeline configuration on a proven search AND full detail response.
        result['prepared_config'] = prepare_config(client.root)
        record = client.store.load(); record['control_success'] = True; client.store.save(record)
    except HHError as exc:
        result.update(error=exc.code, failed_http_status=exc.status, decision='stop_no_automatic_retry')
    result['requests'] = list(client.requests)
    write_report(client.root, 'control-result.json', result)
    return result


def read_secret(prompt):
    # getpass normally falls back to echoed input if terminal echo cannot be
    # disabled. Abort before that fallback to keep secret entry fail-closed.
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        try: return getpass.getpass(prompt).strip()
        except getpass.GetPassWarning: raise HHError('secure_input_unavailable') from None


def local_setup(client):
    if not sys.stdin.isatty() or not sys.stdout.isatty(): raise HHError('setup_requires_your_local_terminal')
    record = client.store.load()
    if not record.get('client_id') or not record.get('client_secret'):
        print('Локальная настройка HH, заявка PORTABLE. Секреты не записываются в файлы и не выводятся.')
        client_id = input('Client ID: ').strip()
        secret = read_secret('Client Secret (ввод скрыт): ')
        email = input('Контактный email разработчика для User-Agent: ').strip()
        previous = read_secret('Если токен приложения уже выдавался, вставьте его из dev.hh.ru/admin; иначе Enter (ввод скрыт): ')
        if not client_id or not secret: raise HHError('credentials_empty')
        new = {'client_id': client_id, 'client_secret': secret, 'contact_email': email}
        if previous: new['access_token'] = previous
        client.headers(new, authenticated=bool(previous))  # Validate before persisting.
        client.store.save(new)
    record = client.store.load()
    if record.get('token_request_pending') and not record.get('access_token'):
        token = read_secret('Ответ выдачи токена не подтвержден. Вставьте действующий токен из dev.hh.ru/admin (ввод скрыт): ')
        if not token: raise HHError('token_request_uncertain_check_dev_portal')
        record.update(access_token=token, token_request_pending=False)
        client.headers(record); client.store.save(record)
    outcome = client.obtain_token_once()
    print('Токен сохранен в macOS Keychain.' if outcome == 'issued_once' else 'Используется сохраненный токен приложения.')
    return control(client)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['setup', 'check', 'status', 'search', 'replay'])
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == 'status':
        path = ROOT / 'var' / 'hh' / 'control-result.json'
        print(path.read_text() if path.is_file() else json.dumps({'configured_control': False, 'next_action': 'run_local_setup'}))
        return 0
    if args.command in ('search', 'replay'):
        import pipeline
        config = json.loads((ROOT / 'pipeline-config.hh.json').read_text())
        config['sources'] = [s for s in config['sources'] if s['provider'] == 'hh']
        client = ReplayClient(ROOT) if args.command == 'replay' else HHClient(ROOT)
        result = pipeline.run(config, root=ROOT, hh_client=client)
        filename = 'search-repeat.json' if args.command == 'replay' else 'search-result.json'
        write_report(ROOT, filename, result)
        print(json.dumps({'run_id': result['run_id'], 'counts': result['counts'], 'hh_profiles': {p: {k:v for k,v in d.items() if k not in ('queries','top')} for p,d in result.get('hh_profiles',{}).items()}, 'top': {p:[{'id':j['id'],'title':j['title'],'employer':j['employer'],'url':j['url']} for j in d['top']] for p,d in result.get('hh_profiles',{}).items()}}, ensure_ascii=False, indent=2))
        return 1 if any('error' in x for x in result['source_results']) else 0
    with locked(ROOT):
        client = HHClient()
        result = local_setup(client) if args.command == 'setup' else control(client)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get('success') else 1


def normalize_salary(salary):
    """Preserve the published tax basis; absent gross is unknown, never NET."""
    if salary:
        mode = (salary.get('mode') or {}).get('id', '')
        return {'min': salary.get('from'), 'max': salary.get('to'),
                  'currency': salary.get('currency'), 'gross': salary.get('gross'),
                  'interval': {'MONTH': 'month', 'YEAR': 'year'}.get(mode, 'unknown')}
    return None


def normalize_job(source, raw, plain, digest):
    """Map a full official HH vacancy to the existing job shape; no schema changes."""
    vacancy_id = str(raw.get('id', ''))
    if not re.fullmatch(r'[0-9]+', vacancy_id) or (not raw.get('description') and '_detail_state' not in raw):
        raise ValueError('HH requires ID and full description, not search snippets')
    employer = raw.get('employer') or {}
    salary = normalize_salary(raw.get('salary_range') or raw.get('salary'))
    formats = {x.get('id') for x in (raw.get('work_format') or []) if isinstance(x, dict)}
    schedule = (raw.get('schedule') or {}).get('id')
    workplace = ('remote' if 'REMOTE' in formats or (not formats and schedule == 'remote')
                 else 'hybrid' if 'HYBRID' in formats else 'on-site' if formats == {'ON_SITE'} else 'unspecified')
    return {'key': 'hh:public:' + vacancy_id, 'provider': 'hh', 'board': 'public',
            'employer': employer.get('name', 'Не указан'), 'id': vacancy_id, 'internal_id': None,
            'title': raw.get('name', ''), 'description': plain(raw.get('description') or '\n'.join(str(x or '') for x in (raw.get('snippet') or {}).values())),
            'locations': [(raw.get('area') or {}).get('name', '')], 'workplace': workplace,
            'salary': salary, 'page_url': raw.get('alternate_url') or 'https://hh.ru/vacancy/' + vacancy_id,
            'apply_url': '', 'published_at': raw.get('published_at'), 'updated_at': None,
            'created_at': raw.get('created_at'), 'opportunity_key': digest('hh:vacancy:' + vacancy_id),
            'discovered_profiles': sorted(set(raw.get('_search_profiles', []))), 'read_only': True,
            'source_queries': raw.get('_search_queries', []), 'detail_state': raw.get('_detail_state', 'full'),
            'full_description_available': raw.get('_detail_state', 'full') == 'full',
            'quick_filter': raw.get('_quick_filter'), 'observation_scope': raw.get('_observation_scope', 'previous_full_search'), 'salary_raw': raw.get('salary_range') or raw.get('salary'),
            'work_formats': raw.get('work_format') or [], 'employer_id': employer.get('id')}


if __name__ == '__main__':
    try: raise SystemExit(main())
    except HHError as exc:
        print(json.dumps({'error': exc.code, 'http_status': exc.status, 'decision': 'stop'}, ensure_ascii=False)); raise SystemExit(1)
    except (KeyboardInterrupt, EOFError): raise SystemExit('Настройка прервана; секреты не выводились.') from None
    except Exception: raise SystemExit('Локальная ошибка; остановка без вывода чувствительных данных.') from None
