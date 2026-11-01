from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

try:
    from . import activity,webapp,server
    from .test_server import FakeAPI,config,event
except ImportError:
    import activity,webapp,server
    from test_server import FakeAPI,config,event
from guard import ApiError


class NotifyAPI(FakeAPI):
    fail_send=False
    def call(self,method,**args):
        if method=='sendMessage':
            self.calls.append((method,args))
            if self.fail_send: raise ApiError('Forbidden: start the bot first',code=403)
            return dict(message_id=123)
        return super().call(method,**args)


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.data=Path(self.temp.name)
        self.cfg=config(); self.cfg['admin_chat_ids']=[123456]
        self.a=activity.Activity(self.data,self.cfg)
    def tearDown(self): self.a.close(); self.temp.cleanup()

    def test_join_replay_notifies_once(self):
        batch=[event(-1002,42,10)]
        self.a.record_joins(batch,'bot'); self.a.record_joins(batch,'bot')
        self.assertEqual(self.a.db.execute('select count(*) from joins').fetchone()[0],1)
        self.assertEqual(self.a.db.execute('select count(*) from outbox').fetchone()[0],1)

    def test_status_write_retries_temporary_reader_conflict(self):
        import guard
        path=self.data/'status.json'
        original=guard.os.replace
        count=[0]
        def replace(src,dest):
            count[0]+=1
            if count[0]==1: raise PermissionError('reader temporarily holds file')
            return original(src,dest)
        with patch.object(guard.os,'replace',side_effect=replace): guard.atomic_json(path,dict(state='running'))
        self.assertEqual(json.loads(path.read_text())['state'],'running')
        self.assertEqual(count[0],2)

    def test_private_id_reply_is_durable_and_does_not_expose_lists(self):
        update=dict(update_id=30,message=dict(chat=dict(id=7654321,type='private'),text='/id'))
        self.a.record_joins([update],'bot'); self.a.record_joins([update],'bot')
        row=self.a.db.execute('select chat,text from outbox').fetchone()
        self.assertEqual(row['chat'],7654321)
        self.assertIn('7654321',row['text']); self.assertNotIn('123456',row['text'])
        api=NotifyAPI(); self.a.pump(api)
        self.assertEqual(self.a.db.execute('select state from outbox').fetchone()[0],'sent')

    def test_result_and_blacklist_are_synced_once(self):
        api=NotifyAPI(); c=server.Coordinator(self.cfg,'test:token',self.data,api)
        try:
            c.dispatch([event(-1002,42,10)]); c.cycle(); c.activity.sync(c.engines[-1002])
            self.assertEqual(self.a.db.execute('select count(*) from results').fetchone()[0],1)
            self.assertEqual(self.a.db.execute('select count(*) from changes').fetchone()[0],1)
            self.assertEqual(self.a.db.execute('select count(*) from outbox').fetchone()[0],3)
        finally: c.close()

    def test_midnight_and_restart_generate_one_report(self):
        before=datetime.fromisoformat('2026-10-31T23:59:59+08:00').timestamp()
        after=datetime.fromisoformat('2026-11-01T00:00:01+08:00').timestamp()
        self.a.schedule(before); self.a.schedule(after); self.a.close()
        self.a=activity.Activity(self.data,self.cfg); self.a.schedule(after)
        self.assertEqual(self.a.db.execute('select day from reports').fetchone()[0],'2026-10-31')
        self.assertEqual(self.a.db.execute('select count(*) from reports').fetchone()[0],1)
        self.assertEqual(self.a.db.execute('select count(*) from outbox').fetchone()[0],1)

    def test_calendar_report_counts_success_not_dry_run(self):
        stamp=datetime.fromisoformat('2026-10-31T23:59:59+08:00').timestamp()
        with self.a.db:
            self.a.db.executemany('insert into joins values (?,?,?,?)',[('a',-1002,42,stamp),('b',-1002,42,stamp+2)])
            self.a.db.executemany('insert into results values (?,?,?,?,?,?)',[
                ('a',-1002,42,'ban_nonmember',stamp,0),('b',-1002,43,'dry_nonmember',stamp,1)])
            self.a.db.execute('insert into changes values (?,?,?,?,?)',('a',-1002,42,'blacklist_add',stamp))
        text=self.a.report('2026-10-31')
        self.assertIn('入群：1 人次',text); self.assertIn('实际踢人：1 次',text)
        self.assertIn('新增黑名单ID：42',text)

    def test_failed_notification_is_durable_and_retried(self):
        with self.a.db: self.a.enqueue('test','hello')
        api=NotifyAPI(); api.fail_send=True; self.a.pump(api)
        row=self.a.db.execute('select state,attempts from outbox').fetchone()
        self.assertEqual(tuple(row),('pending',1))
        self.a.close(); self.a=activity.Activity(self.data,self.cfg)
        with self.a.db: self.a.db.execute('update outbox set due=0')
        api.fail_send=False; self.a.pump(api)
        self.assertEqual(self.a.db.execute('select state from outbox').fetchone()[0],'sent')

    def test_removed_recipient_is_cancelled(self):
        with self.a.db: self.a.enqueue('test','hello')
        self.a.cfg=dict(self.cfg,admin_chat_ids=[]); api=NotifyAPI(); self.a.pump(api)
        self.assertEqual(self.a.db.execute('select state from outbox').fetchone()[0],'cancelled')
        self.assertFalse(api.calls)

    def test_long_reports_split_and_catch_up(self):
        with self.a.db: self.a.enqueue('test','a'*5000)
        self.assertEqual(self.a.db.execute('select count(*) from outbox').fetchone()[0],3)
        self.assertLessEqual(self.a.db.execute('select max(length(text)) from outbox').fetchone()[0],1900)
        self.a.schedule(datetime.fromisoformat('2026-10-28T12:00:00+08:00').timestamp())
        self.a.schedule(datetime.fromisoformat('2026-10-31T12:00:00+08:00').timestamp())
        self.assertEqual(self.a.db.execute('select count(*) from reports').fetchone()[0],3)

    def test_manual_web_command_and_failure_do_not_break_enforcement(self):
        api=NotifyAPI(); api.fail_send=True; c=server.Coordinator(self.cfg,'test:token',self.data,api)
        try:
            ident=activity.enqueue_command(self.data,'whitelist-add',dict(target=-1002,ids='42'))
            c.dispatch([event(-1003,43,11)]); c.cycle()
            self.assertIn((-1003,43),api.banned)
            self.assertEqual(c.engines[-1002].db.execute('select uid from whitelist').fetchone()[0],42)
            self.assertEqual(self.a.db.execute('select state from commands where id=?',(ident,)).fetchone()[0],'done')
        finally: c.close()

    def test_paused_mode_keeps_incoming_events_without_bans(self):
        cfg=dict(self.cfg,paused=True); api=NotifyAPI(); c=server.Coordinator(cfg,'test:token',self.data,api)
        try:
            c.dispatch([event(-1002,42,10)]); c.cycle()
            self.assertFalse(api.banned)
            self.assertEqual(c.engines[-1002].db.execute('select done from jobs').fetchone()[0],0)
        finally: c.close()


class WebTests(unittest.TestCase):
    password='test-only-password-1234'
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.data=Path(self.temp.name)
        self.path=self.data/'runtime-config.json'
        self.raw=dict(source_id=-1001,targets=[dict(id=-1002,name='测试频道')],dry_run=True,token_file=str(self.data/'secret.token'))
        (self.data/'secret.token').write_text('NEVER-EXPOSE-THIS:TOKEN')
        server.save_config(self.path,self.raw)
        webapp.write_auth(self.data,'admin',self.password)
        self.app=webapp.create_app(self.path,self.data); self.app.testing=True
        self.client=self.app.test_client()
    def tearDown(self): self.temp.cleanup()
    def csrf(self):
        with self.client.session_transaction() as session: return session['csrf']
    def login(self,password=None):
        self.client.get('/login')
        return self.client.post('/login',data=dict(username='admin',password=password or self.password,csrf=self.csrf()))
    def post(self,path,payload):
        return self.client.post(path,json=payload,headers={'X-CSRF-Token':self.csrf()})

    def test_login_required_and_no_default_password(self):
        self.assertEqual(self.client.get('/api/state').status_code,401)
        self.assertEqual(self.login().status_code,302)
        response=self.client.get('/api/state')
        self.assertEqual(response.status_code,200)
        self.assertNotIn('NEVER-EXPOSE',response.text)
        self.assertNotIn('password_hash',response.text)
        self.assertIn('no-store',response.headers['Cache-Control'])

    def test_missing_favicon_does_not_invalidate_login_csrf(self):
        self.client.get('/login'); token=self.csrf()
        self.assertEqual(self.client.get('/favicon.ico').status_code,404)
        self.assertEqual(self.csrf(),token)
        response=self.client.post('/login',data=dict(username='admin',password=self.password,csrf=token))
        self.assertEqual(response.status_code,302)

    def test_csrf_and_stale_config_protection(self):
        self.login()
        self.assertEqual(self.client.post('/api/config',json={}).status_code,403)
        self.assertEqual(self.post('/api/config',dict(revision='old',paused=True)).status_code,409)

    def test_configuration_saved_without_secret_path_injection(self):
        self.login(); rev=self.client.get('/api/state').json['revision']
        r=self.post('/api/config',dict(revision=rev,paused=True,admin_chat_ids=[123456],token_file='/etc/shadow'))
        self.assertEqual(r.status_code,200)
        cfg=server.read_config(self.path)
        self.assertTrue(cfg['paused']); self.assertEqual(cfg['admin_chat_ids'],[123456])
        self.assertEqual(cfg['token_file'],self.raw['token_file'])

    def test_invalid_configuration_does_not_replace_file(self):
        self.login(); old=self.path.read_bytes(); rev=hashlib.sha256(old).hexdigest()
        r=self.post('/api/config',dict(revision=rev,source_id=1))
        self.assertEqual(r.status_code,400); self.assertEqual(self.path.read_bytes(),old)

    def test_command_validation_and_authentication(self):
        self.login()
        self.assertEqual(self.post('/api/command',dict(kind='shell',command='anything')).status_code,400)
        self.assertEqual(self.post('/api/command',dict(kind='whitelist-add',target=-999,ids='42')).status_code,400)
        self.assertEqual(self.post('/api/command',dict(kind='whitelist-add',target=-1002,ids='42 43')).status_code,200)

    def test_login_rate_limit_persists(self):
        for _ in range(5): self.assertEqual(self.login('wrong-password').status_code,200)
        self.assertEqual(self.login('wrong-password').status_code,429)

    def test_password_change_revokes_old_session(self):
        self.login()
        r=self.post('/api/account',dict(current_password=self.password,username='admin',password='changed-test-password-1234'))
        self.assertEqual(r.status_code,200)
        self.assertEqual(self.client.get('/api/state').status_code,401)
        self.assertEqual(self.login('changed-test-password-1234').status_code,302)

    def test_admin_password_is_hashed_and_headers_protect_page(self):
        self.assertNotIn(self.password,(self.data/'web-auth.json').read_text())
        self.login(); page=self.client.get('/')
        self.assertEqual(page.status_code,200)
        self.assertIn('frame-ancestors',page.headers['Content-Security-Policy'])
        self.assertEqual(page.headers['X-Frame-Options'],'DENY')


if __name__=='__main__': unittest.main(verbosity=2)
