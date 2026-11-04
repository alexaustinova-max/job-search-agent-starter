#!/usr/bin/env python3
"""Export a bounded, local-only AI handoff using saved shortlists and drafts."""
import argparse
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path

import pipeline as p


SHORTLISTS = ('var/hh/net-matching-shortlist.json', 'var/hh/leadership-shortlist.json')


def saved_candidates(root):
    """Preserve existing priority; never scan or score the complete jobs table."""
    candidates = {}
    for name in SHORTLISTS:
        path = root / name
        if path.is_file():
            for job in json.loads(path.read_text())['jobs']:
                candidates.setdefault(str(job['id']), name)
    return candidates


def reserved(payload):
    batch = payload.get('application_batch', {})
    return (batch.get('status') in {'reserve', 'reserved', 'reserve_not_approved'}
            or batch.get('selected_for_submission') is False
            or payload.get('reserved') is True)


def compact_context(job, decision, candidate, root, cv=None):
    """Shared offline evidence bundle; never interprets missing evidence as a refusal."""
    match = decision['match']
    profile = match['best_profile']
    supported = match['profiles'][profile].get('supported', [])
    caps = {entry['capability'] for entry in supported}
    originals = {entry['fact_id'] for entry in supported}
    facts = [f for f in p.verified_facts(candidate) if caps.intersection(f['capabilities'])]
    facts.sort(key=lambda f: (f['id'] in originals, len(caps.intersection(f['capabilities']))), reverse=True)
    facts = facts[:10]
    requirements = [{'id': 'r' + str(i), 'text': line, 'kind': kind}
                    for i, (line, kind) in enumerate(p.description_lines(job['description']))]
    requirement_ids = {r['text']: r['id'] for r in requirements}
    return ({'hh_id': str(job['id']), 'title': job['title'], 'employer': job['employer'],
             'url': job['page_url'], 'recommended_cv': cv or p.cv_choice(profile, candidate, root),
             'saved_role_level': match.get('role_level'),
             'requirements': requirements,
             'fact_links': [{'fact_id': f['id'], 'supports_named_capabilities': sorted(caps.intersection(f['capabilities']))}
                            for f in facts],
             'salary': decision['filter'].get('salary'),
             'risks': {'language': match.get('language_risks', []),
                       'unconfirmed_requirements': [{'requirement_id': requirement_ids.get(r['text']),
                          **({'text': r['text']} if r['text'] not in requirement_ids else {}),
                          'kind': r.get('kind'),
                          'constraints_requiring_evidence': r.get('constraints_requiring_evidence', [])}
                          for r in match.get('unconfirmed_requirements', [])],
                       'fit_verified': False, 'hh_account_history': 'not_checked_by_app_token'},
             'description_sha256': p.digest(job['description'])},
            {f['id']: {k: f[k] for k in ('text', 'source', 'scope', 'letter_text') if k in f} for f in facts})


def prepare_batch(root=p.ROOT, config=None, state_dir=None, limit=5, output=None):
    root = Path(root).resolve()
    folder = Path(state_dir or root / 'var').resolve()
    output = Path(output or folder / 'application-batch-ai.json').resolve()
    if not 1 <= limit <= 5:
        raise ValueError('limit must be between 1 and 5')
    if not folder.is_relative_to(root) or not output.is_relative_to(root):
        raise ValueError('state and output must remain inside the project')
    if not (folder / 'jobs.sqlite3').is_file():
        raise ValueError('existing SQLite database is required')
    config = config or json.loads((root / 'pipeline-config.hh.json').read_text())
    candidate = p.load_candidate(config['candidate'], root)
    facts = p.verified_facts(candidate)
    floor = config.get('filters', {}).get('min_monthly_rub', 0)
    candidates = saved_candidates(root)
    if not candidates:
        raise ValueError('no saved shortlist; no search will be performed')
    excluded = Counter()
    jobs, fact_export, seen = [], {}, set()
    with p.exclusive_run(folder):
        state = p.State(folder / 'jobs.sqlite3')
        try:
            for vid, shortlist in candidates.items():
                if len(jobs) >= limit:
                    break
                row = state.db.execute('SELECT * FROM jobs WHERE key=?', ('hh:public:' + vid,)).fetchone()
                if not row:
                    excluded['not_in_sqlite'] += 1
                    continue
                apps = state.db.execute('SELECT * FROM applications WHERE job_key=? OR opportunity=?',
                                        (row['key'], row['opportunity'])).fetchall()
                if any(a['state'] in p.CLOSED_SEND_STATES for a in apps):
                    excluded['sent_or_uncertain'] += 1
                    continue
                if any(reserved(json.loads(a['payload'])) for a in apps):
                    excluded['reserved'] += 1
                    continue
                if state.db.execute("SELECT 1 FROM events e LEFT JOIN jobs j ON j.key=e.job_key "
                                    "WHERE (e.job_key=? OR j.opportunity=?) "
                                    "AND e.stage IN ('sent','confirmed','dispatching','send_uncertain') LIMIT 1",
                                    (row['key'], row['opportunity'])).fetchone():
                    excluded['delivery_history'] += 1
                    continue
                if row['opportunity'] in seen or any(a['job_key'] != row['key'] for a in apps):
                    excluded['duplicate'] += 1
                    continue
                job = json.loads(row['data'])
                decision = json.loads(row['decision'] or '{}')
                match = decision.get('match', {})
                profile = match.get('best_profile')
                saved_profile = match.get('profiles', {}).get(profile, {})
                if (not row['active'] or not job.get('full_description_available')
                        or not decision.get('filter', {}).get('passed')
                        or match.get('critical_mismatches')
                        or match.get('role_level') == 'outside_target'
                        or saved_profile.get('role_score', 0) < 60):
                    excluded['inactive_incomplete_or_mismatch'] += 1
                    continue
                salary = p.salary_assessment(job, floor,config.get('filters',{}).get('salary_basis','NET'))
                if salary['failure'] or salary['status'] == 'estimated_below_net_floor':
                    excluded['salary_below_gross_floor' if config.get('filters',{}).get('salary_basis')=='GROSS' else 'salary_below_net_floor'] += 1
                    continue
                if not match.get('letter_evidence_sufficient'):
                    excluded['insufficient_saved_evidence'] += 1
                    continue
                cv = p.cv_choice(profile, candidate, root)
                if not cv['available']:
                    excluded['cv_missing'] += 1
                    continue
                supported = saved_profile.get('supported', [])
                caps = {entry['capability'] for entry in supported}
                relevant = [f for f in facts if caps.intersection(f['capabilities'])]
                original = {entry['fact_id'] for entry in supported}
                relevant.sort(key=lambda f: (f['id'] in original, len(caps.intersection(f['capabilities']))), reverse=True)
                relevant = relevant[:10]
                if len(relevant) < 2:
                    excluded['insufficient_current_evidence'] += 1
                    continue
                # Reuse the existing draft preparation; no matching or network call.
                draft_decision = deepcopy(decision)
                draft_decision['filter']['salary'] = salary
                action, app_id = p.prepare(state, job, draft_decision, candidate, folder, root)
                for fact in relevant:
                    fact_export[fact['id']] = {k: fact[k] for k in ('text', 'source', 'scope') if k in fact}
                requirements = [{'text': line, 'kind': kind} for line, kind in p.description_lines(job['description'])
                                if kind in {'mandatory', 'optional', 'context'}]
                jobs.append({'hh_id': vid, 'title': job['title'], 'employer': job['employer'],
                             'url': job['page_url'], 'shortlist': shortlist,
                             'application_id': app_id, 'draft_action': action,
                             'recommended_cv': cv, 'saved_role_level': match.get('role_level'),
                             'requirements': requirements,
                             'fact_links': [{'fact_id': f['id'], 'supports_named_capabilities': sorted(caps.intersection(f['capabilities']))}
                                            for f in relevant],
                             'salary': salary,
                             'risks': {'language': match.get('language_risks', []),
                                       'unconfirmed_requirements': [r['text'] for r in match.get('unconfirmed_requirements', [])],
                                       'next_review': match.get('further_analysis'),
                                       'fit_verified': False, 'hh_live_status': 'not_checked_offline',
                                       'hh_account_history': 'not_checked_offline'},
                             'saved_at': row['last_seen'], 'description_sha256': p.digest(job['description'])})
                seen.add(row['opportunity'])
            state.db.commit()
        finally:
            state.close()
    base = json.loads((root / config['candidate']['facts_file']).read_text()) if config['candidate'].get('facts_file') else {}
    packet = {'scope': 'Offline AI handoff; saved shortlist priority; no search, rescoring, LLM, API or submission.',
              'generated_at': p.now(), 'salary_floor_net_rub_monthly': floor,
              'candidate': {'english_level': candidate.get('english_level'),
                            'career_facts_sha256': p.digest(base), 'limitations': base.get('limitations', [])},
              'instructions': ['Evaluate this entire packet and write concise factual letters only after review.',
                               'Fact links support named competencies only, not entire requirements, tenure or outcomes.',
                               'Missing evidence is unknown experience; do not invent facts or answers.',
                               'Requirement extraction is not exhaustive. Full descriptions remain in SQLite.',
                               'HH availability, account duplicates and forms require checking before a single user approval.',
                               'Prepared drafts are not approved or sent.'],
              'facts': fact_export, 'vacancies': jobs,
              'summary': {'vacancies': len(jobs), 'shortlist_candidates': len(candidates),
                          'excluded': dict(excluded), 'rescored': 0, 'network_requests': 0, 'applications_sent': 0}}
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + '.tmp')
    temporary.write_text(json.dumps(packet, ensure_ascii=False, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.replace(output)
    return {'output': str(output), **packet['summary']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=5)
    args = parser.parse_args()
    print(json.dumps(prepare_batch(limit=args.limit), ensure_ascii=False))


if __name__ == '__main__':
    main()
