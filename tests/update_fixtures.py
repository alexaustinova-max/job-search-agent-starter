"""Synthetic installation shared by update regression tests; external actions blocked."""
import json
import test_portable as base
import pipeline as p
import daily_prepare as daily
import portable as cli
import llm_prepare as llm
from unittest.mock import MagicMock

TEXT='В вымышленной компании я анализирую продуктовые метрики и готовлю материалы для команды. Исследую потребности рынка и проверяю исходные предположения по данным.'
class Fixture:
    policy=base.PortableTests.policy
    candidate=base.PortableTests.candidate
    def setUp(self):
        base.PortableTests.setUp(self)
        self.state=p.State(self.root/'var/jobs.sqlite3');self.addCleanup(self.state.close)
    def add_job(self,vid='900000001'):
        job=base.PortableTests.job(self);job.update(id=vid,key='hh:public:'+vid,page_url='https://hh.ru/vacancy/'+vid)
        match=p.score(job,self.candidate())
        decision={'match':match,'filter':{'passed':True,'notes':[]},'channel':p.channel(job)}
        self.state.db.execute('INSERT INTO jobs(key,source,opportunity,data,decision,first_seen,last_seen) VALUES(?,?,?,?,?,?,?)',
            (job['key'],'hh:public',vid,json.dumps(job),json.dumps(decision),p.now(),p.now()));self.state.db.commit()
        return job
    def configure_ai(self):
        cli.save(self.root,'llm-config.json',{**llm.settings(self.root),'enabled':True,'spending_approval':'offline synthetic consent',
            'max_cost_usd_per_day':1.,'max_cost_usd_per_month':20.,'max_calls_per_day':20,'max_output_tokens':4096})
    def application(self,job,status):
        self.state.db.execute('INSERT INTO applications(id,job_key,opportunity,state,payload,artifact_hash) VALUES(?,?,?,?,?,?)',
            (job['id'],job['key'],job['id'],status,'{}','synthetic'));self.state.db.commit()
