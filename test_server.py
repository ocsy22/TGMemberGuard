import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    from . import server
except ImportError:
    import server
from guard import ApiError, import_whitelist, open_database


def config(targets=(-1002, -1003), source=-1001, dry=False):
    return server.validate_config(dict(source_id=source, targets=[dict(id=t) for t in targets],
        dry_run=dry, token_file=str(Path(tempfile.gettempdir()).resolve() / 'test-token'), proxy=''))


def event(target, uid, ident):
    return dict(update_id=ident, chat_member=dict(chat=dict(id=target, type='channel'),
        old_chat_member=dict(status='left'), new_chat_member=dict(status='member', user=dict(id=uid, is_bot=False))))


class FakeAPI:
    def __init__(self):
        self.calls, self.updates, self.bad, self.banned = [], [], set(), set()
        self.targets = {-1002, -1003, -1004}
        self.members = {}

    def call(self, method, **args):
        self.calls.append((method,args))
        chat = args.get('chat_id')
        if chat in self.bad: raise ApiError('channel unavailable')
        if method=='getMe': return dict(id=1, username='test')
        if method=='getWebhookInfo': return dict(url='')
        if method=='getChat': return dict(type='channel',title=str(chat))
        if method=='getUpdates':
            out,self.updates = self.updates,[]
            return out
        uid=args.get('user_id')
        if method=='getChatMember':
            if uid==1: return dict(status='administrator',can_restrict_members=True)
            if chat in self.targets:
                return dict(status='kicked' if (chat,uid) in self.banned else 'member')
            return dict(status=self.members.get((chat,uid),'left'))
        if method=='banChatMember':
            assert args['until_date']==0
            self.banned.add((chat,uid)); return True
        if method=='unbanChatMember': self.banned.discard((chat,uid)); return True
        raise AssertionError(method)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.data=Path(self.temp.name)
        self.controllers=[]
        self.api=FakeAPI()

    def tearDown(self):
        for c in self.controllers: c.close()
        self.temp.cleanup()

    def make(self,cfg=None,token='test:token'):
        c=server.Coordinator(cfg or config(),token,self.data,self.api,logging.getLogger('test'))
        self.controllers.append(c); return c

    def test_multi_channel_dispatch_and_single_poller(self):
        c=self.make()
        self.api.updates=[event(-1002,42,10),event(-1003,43,11)]
        self.api.members[(-1001,42)]='member'
        c.cycle(); c.cycle()
        self.assertNotIn((-1002,42),self.api.banned)
        self.assertIn((-1003,43),self.api.banned)
        self.assertEqual([a['offset'] for m,a in self.api.calls if m=='getUpdates'],[0,12])
        self.assertEqual(c.engines[-1002].db.execute('select count(*) from jobs').fetchone()[0],1)
        self.assertEqual(c.engines[-1003].db.execute('select count(*) from jobs').fetchone()[0],1)

    def test_target_failure_does_not_stop_other_target(self):
        c=self.make(); self.api.bad.add(-1002)
        c.dispatch([event(-1002,42,10),event(-1003,43,11)])
        c.cycle()
        self.assertFalse(c.ready[-1002]); self.assertTrue(c.ready[-1003])
        self.assertIn((-1003,43),self.api.banned)
        self.assertEqual(c.engines[-1002].db.execute('select done from jobs').fetchone()[0],0)

    def test_cursor_survives_added_channel(self):
        c=self.make(config((-1002,))); c.dispatch([event(-1002,42,20)]); c.close()
        new=self.make(config((-1002,-1003,-1004)))
        self.assertEqual(new.offset,21)
        new.dispatch([event(-1004,44,21)])
        self.assertEqual(new.engines[-1004].db.execute('select uid from jobs').fetchone()[0],44)

    def test_pending_queue_and_whitelist_survive_source_change(self):
        c=self.make(); c.dispatch([event(-1002,42,10)])
        import_whitelist(c.engines[-1002].db,-1002,-1001,[42])
        c.close()
        new=self.make(config(source=-9999)); self.api.bad.add(-9999)
        new.cycle()
        self.assertFalse(self.api.banned)
        self.assertEqual(new.engines[-1002].db.execute('select done from jobs').fetchone()[0],1)

    def test_source_deleted_unknown_users_not_banned(self):
        c=self.make(); c.dispatch([event(-1002,42,10),event(-1003,43,11)])
        self.api.bad.add(-1001); c.cycle()
        self.assertFalse(self.api.banned)
        for e in c.engines.values():
            self.assertEqual(e.db.execute('select count(*) from blacklist').fetchone()[0],0)

    def test_crash_during_fanout_replays_without_duplicates(self):
        c=self.make(); batch=[event(-1002,42,10),event(-1003,43,11)]
        with patch.object(c.engines[-1003],'ingest',side_effect=RuntimeError('disk error')):
            with self.assertRaises(RuntimeError): c.dispatch(batch)
        self.assertFalse((self.data/'poll.json').exists()); c.close()
        new=self.make(); new.dispatch(batch)
        self.assertEqual(new.offset,12)
        for e in new.engines.values():
            self.assertEqual(e.db.execute('select count(*) from jobs').fetchone()[0],1)

    def test_new_bot_resets_cursor_but_keeps_lists(self):
        c=self.make(); c.dispatch([event(-1002,42,10)])
        import_whitelist(c.engines[-1002].db,-1002,-1001,[42]); c.close()
        new=self.make(token='new:token')
        self.assertEqual(new.offset,0)
        self.assertEqual(new.engines[-1002].db.execute('select count(*) from jobs').fetchone()[0],0)
        self.assertEqual(new.engines[-1002].db.execute('select uid from whitelist').fetchone()[0],42)

    def test_lists_do_not_leak_to_other_channel(self):
        c=self.make()
        import_whitelist(c.engines[-1002].db,-1002,-1001,[42])
        c.dispatch([event(-1003,42,10)]); c.cycle()
        self.assertIn((-1003,42),self.api.banned)

    def test_dry_run_never_bans(self):
        c=self.make(config(dry=True)); c.dispatch([event(-1002,42,10)]); c.cycle()
        self.assertFalse(self.api.banned)

    def test_config_rejects_invalid_rules(self):
        for targets,source in [((-1002,-1002),-1001),((-1001,),-1001),((),-1001),((2,),-1001)]:
            with self.assertRaises(ValueError): config(targets,source)
        bad=dict(source_id=-1,targets=[dict(id=-2)],dry_run='false')
        with self.assertRaises(ValueError): server.validate_config(bad)
        with self.assertRaises(ValueError): server.chat_id(True)
        with self.assertRaises(ValueError): server.chat_id(-1.5)

    def test_individual_source_override(self):
        cfg=server.validate_config(dict(source_id=-1001,targets=[dict(id=-1002),dict(id=-1003,source_id=-2001)]))
        c=self.make(cfg); self.api.members[(-2001,43)]='member'
        c.dispatch([event(-1003,43,10)]); c.cycle()
        self.assertFalse(self.api.banned)

    def test_rate_limit_is_shared(self):
        api=server.RateLimitedAPI(dict(token='test:token'))
        with patch.object(server.API,'call',side_effect=ApiError('rate limit',retry=10)) as call:
            for method in ('getUpdates','getChat'):
                with self.assertRaises(ApiError): api.call(method)
            self.assertEqual(call.call_count,1)

    def test_cli_config_add_source_replace_and_mode(self):
        path=self.data/'config.json'
        raw=dict(source_id=-1001,targets=[dict(id=-1002)],token_file=str(self.data/'bot.token'),dry_run=True)
        server.save_config(path,raw)
        base=['--config',str(path),'--data-dir',str(self.data)]
        with patch.object(server.guard,'DATA',self.data):
            server.main(base+['target-add','-1003'])
            server.main(base+['source-set','-2001'])
            server.main(base+['mode','live'])
        cfg=server.read_config(path)
        self.assertEqual([r['source_id'] for r in cfg['targets']],[-2001,-2001])
        self.assertFalse(cfg['dry_run'])

    def test_windows_database_migration(self):
        old=self.data/'windows.sqlite3'
        db=open_database(old)
        import_whitelist(db,-1002,-1001,[42])
        with db: db.execute('insert into blacklist values (-1002,43,0)')
        db.close()
        args=type('Args',(),dict(command='migrate-windows',target=-1002,file=str(old)))()
        server.database_command(args,config(),self.data)
        new=open_database(server.database_path(self.data,-1002))
        self.assertEqual(new.execute('select uid from whitelist').fetchone()[0],42)
        self.assertEqual(new.execute('select uid from blacklist').fetchone()[0],43)
        new.close()

    @unittest.skipUnless(os.name=='posix','Requires Linux fcntl')
    def test_linux_lock(self):
        with server.worker_lock(self.data):
            with self.assertRaises(RuntimeError):
                with server.worker_lock(self.data): pass


if __name__=='__main__': unittest.main(verbosity=2)
