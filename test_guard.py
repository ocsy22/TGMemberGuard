import logging
from pathlib import Path
import tempfile
import unittest
from guard import Engine, ApiError, is_member, preflight, parse_user_ids, import_whitelist


class FakeAPI:
    def __init__(self, source='member', target='member', fail=False):
        self.source, self.target, self.fail = source, target, fail
        self.calls = []

    def call(self, method, **args):
        self.calls.append((method, args))
        if method == 'getMe': return dict(id=1, username='test')
        if method == 'getChat':
            if self.fail: raise ApiError('source unavailable')
            return dict(type='channel', title='test')
        if method == 'getChatMember':
            if args['user_id'] == 1: return dict(status='administrator')
            if args['chat_id'] == -1001 and self.fail:
                raise ApiError('timeout')
            status = self.source if args['chat_id'] == -1001 else self.target
            return {'status': status}
        if method == 'banChatMember':
            return True
        raise AssertionError(method)


def event(uid=42, ident=10, old='left', new='member', chat=-1002, bot=False, kind='supergroup'):
    return dict(update_id=ident, chat_member=dict(chat=dict(id=chat, type=kind),
        old_chat_member=dict(status=old), new_chat_member=dict(status=new, user=dict(id=uid, is_bot=bot))))


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = dict(token='fake:token', source=-1001, target=-1002, dry_run=False)
        self.engines = []

    def tearDown(self):
        for engine in self.engines:
            engine.db.close()
        self.tmp.cleanup()

    def make(self, api=None):
        e = Engine(self.cfg, Path(self.tmp.name)/'queue.db', api or FakeAPI(), logging.getLogger('test'))
        self.engines.append(e)
        return e

    def test_member_retained(self):
        e = self.make(); e.ingest([event()]); e.process()
        self.assertFalse(any(c[0]=='banChatMember' for c in e.api.calls))
        self.assertEqual(e.db.execute('select done from jobs').fetchone()[0], 1)

    def test_nonmember_banned_permanently(self):
        e = self.make(FakeAPI(source='left')); e.ingest([event()]); e.process()
        bans = [c for c in e.api.calls if c[0]=='banChatMember']
        self.assertEqual(len(bans), 1)
        self.assertEqual(bans[0][1]['until_date'], 0)
        self.assertFalse(any(c[0]=='unbanChatMember' for c in e.api.calls))

    def test_dry_run(self):
        self.cfg['dry_run'] = True
        e = self.make(FakeAPI(source='kicked')); e.ingest([event()]); e.process()
        self.assertFalse(any(c[0]=='banChatMember' for c in e.api.calls))

    def test_network_error_is_not_nonmember(self):
        e = self.make(FakeAPI(fail=True)); e.ingest([event()]); e.process()
        self.assertFalse(any(c[0]=='banChatMember' for c in e.api.calls))
        self.assertEqual(e.db.execute('select done,attempts from jobs').fetchone(), (0,1))

    def test_duplicate_update_does_not_duplicate_kick(self):
        e = self.make(FakeAPI(source='left')); e.ingest([event(), event()]); e.process(); e.process()
        self.assertEqual(sum(c[0]=='banChatMember' for c in e.api.calls), 1)

    def test_unrelated_and_existing_member_changes_ignored(self):
        e = self.make(); e.ingest([event(chat=-9), event(ident=11, old='member'), event(ident=12, bot=True)])
        self.assertEqual(e.db.execute('select count(*) from jobs').fetchone()[0],0)

    def test_promoted_member_protected(self):
        e = self.make(FakeAPI(source='left', target='administrator')); e.ingest([event()]); e.process()
        self.assertFalse(any(c[0]=='banChatMember' for c in e.api.calls))

    def test_queue_survives_restart(self):
        e = self.make(); e.ingest([event()]); other = self.make(FakeAPI(source='left')); other.process()
        self.assertEqual(sum(c[0]=='banChatMember' for c in other.api.calls),1)
        self.assertEqual(other.db.execute("select value from meta where key='offset'").fetchone()[0], '11')

    def test_config_change_drops_stale_jobs(self):
        e = self.make(); e.ingest([event()]); self.cfg['target'] = -999
        other = self.make()
        self.assertEqual(other.db.execute('select count(*) from jobs').fetchone()[0],0)

    def test_restricted_membership(self):
        self.assertTrue(is_member(dict(status='restricted', is_member=True)))
        self.assertFalse(is_member(dict(status='restricted', is_member=False)))

    def test_unknown_status_retried(self):
        e = self.make(FakeAPI(source='unknown')); e.ingest([event()]); e.process()
        self.assertEqual(e.db.execute('select done from jobs').fetchone()[0],0)

    def test_no_join_request_approval(self):
        e = self.make(); e.ingest([dict(update_id=4,chat_join_request=dict(chat=dict(id=-1002), from_user=dict(id=42)))])
        e.process(); self.assertEqual(e.api.calls, [])

    def test_channel_member_retained(self):
        e = self.make(); e.ingest([event(kind='channel')]); e.process()
        self.assertEqual(e.db.execute('select done from jobs').fetchone()[0], 1)
        self.assertFalse(any(c[0]=='banChatMember' for c in e.api.calls))

    def test_channel_nonmember_removed(self):
        e = self.make(FakeAPI(source='left')); e.ingest([event(kind='channel')]); e.process()
        bans = [c for c in e.api.calls if c[0]=='banChatMember']
        self.assertEqual(len(bans), 1)
        self.assertEqual(bans[0][1]['chat_id'], self.cfg['target'])
        self.assertEqual(bans[0][1]['until_date'], 0)

    def test_blacklist_survives_restart_and_external_unban(self):
        e = self.make(FakeAPI(source='left')); e.ingest([event()]); e.process()
        self.assertEqual(e.db.execute('select uid from blacklist').fetchone()[0],42)
        other = self.make(FakeAPI(source='member'))
        other.ingest([event(ident=11)]); other.process()
        self.assertEqual(sum(c[0]=='banChatMember' for c in other.api.calls),1)
        self.assertFalse(any(c[0]=='getChatMember' and c[1]['chat_id']==-1001 for c in other.api.calls))

    def test_dry_run_does_not_blacklist(self):
        self.cfg['dry_run'] = True
        e = self.make(FakeAPI(source='left')); e.ingest([event()]); e.process()
        self.assertEqual(e.db.execute('select count(*) from blacklist').fetchone()[0],0)

    def test_blacklisted_admin_still_protected(self):
        e = self.make(FakeAPI(source='left')); e.ingest([event()]); e.process()
        other = self.make(FakeAPI(target='administrator'))
        other.ingest([event(ident=11)]); other.process()
        self.assertFalse(any(c[0]=='banChatMember' for c in other.api.calls))

    def test_preflight_channel_and_supergroup_combinations(self):
        class CheckAPI:
            def __init__(self, source_type, target_type, restrict=True):
                self.kinds = {-1001: source_type, -1002: target_type}
                self.restrict = restrict
            def call(self, method, **args):
                if method == 'getMe': return dict(id=1, username='test')
                if method == 'getWebhookInfo': return dict(url='')
                if method == 'getChat': return dict(type=self.kinds[args['chat_id']], title='test')
                if method == 'getChatMember': return dict(status='administrator', can_restrict_members=self.restrict)
                raise AssertionError(method)
        for source in ('channel', 'supergroup'):
            for target in ('channel', 'supergroup'):
                with self.subTest(source=source, target=target):
                    self.assertIn('@test', preflight(self.cfg, CheckAPI(source, target)))
        with self.assertRaises(ApiError):
            preflight(self.cfg, CheckAPI('channel', 'group'))
        with self.assertRaises(ApiError):
            preflight(self.cfg, CheckAPI('channel', 'channel', restrict=False))

    def test_verified_whitelist_survives_source_replacement_and_outage(self):
        e = self.make(); e.ingest([event()]); e.process()
        self.assertEqual(e.db.execute('select source,origin from whitelist').fetchone(),(-1001,'verified'))
        self.cfg['source'] = -2001
        other = self.make(FakeAPI(fail=True)); other.ingest([event(ident=12)]); other.process()
        self.assertFalse(any(c[0]=='banChatMember' for c in other.api.calls))
        self.assertFalse(any(c[0]=='getChat' for c in other.api.calls))
        self.assertEqual(other.db.execute('select done from jobs').fetchone()[0],1)

    def test_outage_never_adds_blacklist(self):
        e = self.make(FakeAPI(source='left',fail=True)); e.ingest([event()]); e.process()
        self.assertEqual(e.db.execute('select count(*) from blacklist').fetchone()[0],0)
        self.assertIn('来源1异常保护',e.source_error)
        self.assertFalse(any(c[0]=='banChatMember' for c in e.api.calls))

    def test_source_loses_admin_privileges(self):
        class LostAdmin(FakeAPI):
            def call(self, method, **args):
                if method=='getChatMember' and args['user_id']==1: return dict(status='left')
                return super().call(method, **args)
        e = self.make(LostAdmin(source='left')); e.ingest([event()]); e.process()
        self.assertEqual(e.db.execute('select count(*) from blacklist').fetchone()[0],0)

    def test_source_disappears_before_negative_decision(self):
        class Disappearing(FakeAPI):
            checks = 0
            def call(self, method, **args):
                if method=='getChat':
                    self.checks += 1
                    if self.checks > 1: raise ApiError('source disappeared')
                return super().call(method, **args)
        e = self.make(Disappearing(source='left')); e.ingest([event()]); e.process()
        self.assertEqual(e.db.execute('select count(*) from blacklist').fetchone()[0],0)
        self.assertFalse(any(c[0]=='banChatMember' for c in e.api.calls))

    def test_whitelist_isolated_by_target(self):
        e = self.make(); e.ingest([event()]); e.process()
        self.cfg['target'] = -999
        other = self.make(FakeAPI(fail=True)); other.ingest([event(ident=12,chat=-999)]); other.process()
        self.assertEqual(other.db.execute('select done from jobs').fetchone()[0],0)

    def test_blacklist_takes_precedence_over_whitelist(self):
        e = self.make()
        import_whitelist(e.db,-1002,-1001,[42])
        with e.db: e.db.execute('insert into blacklist values (-1002,42,0)')
        e.ingest([event()]); e.process()
        self.assertTrue(any(c[0]=='banChatMember' for c in e.api.calls))

    def test_import_deduplicates_and_skips_blacklist(self):
        e = self.make()
        with e.db: e.db.execute('insert into blacklist values (-1002,43,0)')
        self.assertEqual(import_whitelist(e.db,-1002,-1001,[42,42,43]),(1,1))
        self.assertEqual(parse_user_ids('42，43\n42;44'),[42,43,44])
        for text in ('-1001','@user','0','42 abc',''):
            with self.assertRaises(ValueError): parse_user_ids(text)

    def test_degraded_startup_does_not_require_source(self):
        class TargetOnly(FakeAPI):
            def call(self, method, **args):
                if method=='getWebhookInfo': return dict(url='')
                if args.get('chat_id') == -1001: raise ApiError('source deleted')
                if method=='getChatMember': return dict(status='administrator',can_restrict_members=True)
                return super().call(method,**args)
        api=TargetOnly()
        self.assertIn('目标2',preflight(self.cfg,api,require_source=False))
        with self.assertRaises(ApiError): preflight(self.cfg,api)


if __name__ == '__main__':
    unittest.main(verbosity=2)
