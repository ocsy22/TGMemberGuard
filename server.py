#!/usr/bin/env python3
"""TGMemberGuard 2.1.0: multi-channel enforcement, web management and reports."""
import argparse
from contextlib import contextmanager
import getpass
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import signal
import sqlite3
import sys
import threading
import time
from zoneinfo import ZoneInfo

import guard
from guard import API, ApiError, Engine, atomic_json, open_database, parse_user_ids, import_whitelist, preflight
try:
    from .activity import Activity
except ImportError:
    from activity import Activity

VERSION = '2.1.0'


def chat_id(value):
    if isinstance(value, bool): raise ValueError('频道 ID 必须为负整数')
    if isinstance(value, str) and not value.lstrip('-').isdigit(): raise ValueError('频道 ID 必须为负整数')
    if not isinstance(value, (int, str)): raise ValueError('频道 ID 必须为负整数')
    number = int(value)
    if not -(2**63) < number < 0: raise ValueError('频道 ID 必须为负整数')
    return number


def validate_config(raw):
    if not isinstance(raw, dict): raise ValueError('配置必须是 JSON 对象')
    source = chat_id(raw['source_id'])
    if not isinstance(raw.get('dry_run', True), bool): raise ValueError('dry_run 必须是 true 或 false')
    targets = raw.get('targets', [])
    if not isinstance(targets, list) or not targets: raise ValueError('至少配置一个目标频道')
    seen, rules = set(), []
    for target in targets:
        if not isinstance(target, dict): raise ValueError('targets 的每项必须为对象')
        ident = chat_id(target['id'])
        src = chat_id(target.get('source_id', source))
        if ident == src or ident in seen: raise ValueError('目标ID不能重复，也不能与对应来源相同')
        seen.add(ident)
        rules.append(dict(id=ident, source_id=src, name=str(target.get('name', ident))))
    token_file = raw.get('token_file', '/etc/tgmemberguard/bot.token')
    if not isinstance(token_file, str) or not (Path(token_file).is_absolute() or PurePosixPath(token_file).is_absolute()):
        raise ValueError('token_file 必须是绝对路径')
    proxy = raw.get('proxy', '')
    if not isinstance(proxy, str) or (proxy and not proxy.startswith(('http://', 'https://', 'socks5h://'))):
        raise ValueError('proxy 必须为 HTTP/HTTPS/SOCKS5H 地址或空字符串')
    admins=raw.get('admin_chat_ids',[])
    if not isinstance(admins,list) or any(type(x) is not int or x==0 or abs(x)>=2**63 for x in admins):
        raise ValueError('admin_chat_ids 必须为非零整数聊天ID列表')
    zone=raw.get('report_timezone','Asia/Shanghai')
    try: ZoneInfo(zone)
    except Exception: raise ValueError('日报时区必须是有效IANA时区，例如 Asia/Shanghai') from None
    notify=raw.get('notify_types',['join','result','list'])
    if not isinstance(notify,list) or not set(notify)<= {'join','result','list'}:
        raise ValueError('notify_types 仅支持 join、result、list')
    for flag in ('paused','daily_report'):
        if type(raw.get(flag,flag=='daily_report')) is not bool: raise ValueError(flag+' 必须为布尔值')
    web=raw.get('web',{})
    if not isinstance(web,dict): raise ValueError('web 必须为对象')
    host=web.get('host','127.0.0.1'); port=web.get('port',8787)
    if host not in ('127.0.0.1','0.0.0.0','::1','::') or type(port) is not int or not 1024<=port<=65535:
        raise ValueError('网页监听地址或端口无效')
    for flag in ('enabled','secure_cookie','behind_proxy'):
        if type(web.get(flag,flag=='enabled')) is not bool: raise ValueError('web.'+flag+' 必须为布尔值')
    return dict(source_id=source, dry_run=raw.get('dry_run', True), targets=rules, token_file=token_file, proxy=proxy,
        admin_chat_ids=list(dict.fromkeys(admins)), report_timezone=zone, notify_types=notify,
        daily_report=raw.get('daily_report',True),paused=raw.get('paused',False),
        web=dict(host=host,port=port,enabled=web.get('enabled',True),secure_cookie=web.get('secure_cookie',False),behind_proxy=web.get('behind_proxy',False)))


def read_config(path):
    return validate_config(json.loads(Path(path).read_text(encoding='utf-8-sig')))


def read_token(cfg):
    token = Path(cfg['token_file']).read_text(encoding='utf-8').strip()
    if not token or ':' not in token or any(c.isspace() for c in token):
        raise ValueError('Token 文件为空或格式错误，请运行 init 配置')
    return token


def secure_write(path, text):
    path = Path(path)
    temp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o640)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        if os.name == 'posix' and os.geteuid() == 0:
            os.chown(temp, -1, path.parent.stat().st_gid)
        os.chmod(temp, 0o640)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def save_config(path, cfg):
    validate_config(cfg)
    secure_write(path, json.dumps(cfg, ensure_ascii=False, indent=2) + '\n')


@contextmanager
def worker_lock(data):
    import fcntl
    data.mkdir(parents=True, exist_ok=True)
    with (data / 'run.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('后台正在运行，请先 sudo systemctl stop tgmemberguard') from None
        try: yield
        finally: fcntl.flock(handle, fcntl.LOCK_UN)


def database_path(data, target):
    folder = data / 'channels'
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f'{chat_id(target)}.sqlite3'


class ChannelLog(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        return f"[目标 {self.extra['target']}] {msg}", kwargs


class RateLimitedAPI(API):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.not_before = 0

    def call(self, method, **kwargs):
        remaining = self.not_before - time.monotonic()
        if remaining > 0:
            raise ApiError('Telegram 限流，等待后继续', retry=int(remaining) + 1, code=429)
        try: return super().call(method, **kwargs)
        except ApiError as e:
            if e.retry: self.not_before = time.monotonic() + e.retry
            raise


class Coordinator:
    def __init__(self, cfg, token, data, api=None, log=None):
        self.cfg, self.data = cfg, Path(data)
        self.data.mkdir(parents=True, exist_ok=True)
        self.api = api or RateLimitedAPI(dict(token=token, proxy=cfg['proxy']))
        if isinstance(self.api, API): self.api.timeouts = (5, 20)
        self.log = log or logging.getLogger('server')
        self.stop = threading.Event()
        self.fingerprint = hashlib.sha256(token.encode()).hexdigest()
        self.offset = 0
        self.engines, self.ready, self.next_check = {}, {}, {}
        self.activity=Activity(self.data,cfg)
        self.activity.schedule()
        self.config_path=None
        self.config_digest=None
        self.reload=False
        state = self.data / 'poll.json'
        previous = json.loads(state.read_text(encoding='utf-8')) if state.exists() else None
        if previous and previous['bot'] == self.fingerprint:
            self.offset = int(previous['offset'])
        try:
            for rule in cfg['targets']:
                target = rule['id']
                engine_cfg = dict(token=token, source=rule['source_id'], target=target, dry_run=cfg['dry_run'])
                engine = Engine(engine_cfg, database_path(self.data, target), self.api,
                                ChannelLog(self.log, dict(target=target)), preserve_pending=True)
                self.engines[target] = engine
                self.ready[target] = False
                self.next_check[target] = 0
                if previous and previous['bot'] != self.fingerprint:
                    with engine.db:
                        engine.db.execute('DELETE FROM jobs')
                        engine.db.execute("DELETE FROM meta WHERE key='offset'")
        except Exception:
            self.close()
            raise

    def dispatch(self, updates):
        # Each engine commits before global cursor advances. A crash in this loop
        # replays updates; INSERT OR IGNORE and completed jobs make replay safe.
        for engine in self.engines.values(): engine.ingest(updates)
        self.activity.record_joins(updates,self.fingerprint)
        if updates:
            offset = max(self.offset, max(u['update_id'] for u in updates) + 1)
            atomic_json(self.data / 'poll.json', dict(bot=self.fingerprint, offset=offset))
            self.offset = offset

    def cycle(self):
        self.process_commands()
        for target, engine in self.engines.items():
            if self.stop.is_set(): return
            if time.time() >= self.next_check[target]:
                try:
                    preflight(engine.cfg, self.api, require_source=False)
                    self.ready[target] = True
                    self.next_check[target] = time.time() + 60
                except ApiError as e:
                    self.ready[target] = False
                    self.next_check[target] = time.time() + max(15, e.retry)
                    engine.log.warning('目标权限/连接检查失败，将重试：%s', e)
                    if e.retry:
                        self.stop.wait(e.retry)
                        return
            if self.ready[target] and not self.cfg.get('paused'): engine.process(limit=5)
            self.activity.sync(engine)
        self.status('审核已暂停' if self.cfg.get('paused') else '运行中')
        due = not self.cfg.get('paused') and any(self.ready[target] and engine.db.execute('SELECT 1 FROM jobs WHERE done=0 AND due<=? LIMIT 1',
                  (time.time(),)).fetchone() for target,engine in self.engines.items())
        notice_due=self.activity.db.execute("SELECT 1 FROM outbox WHERE state='pending' AND due<=? LIMIT 1",(time.time(),)).fetchone()
        updates = self.api.call('getUpdates', offset=self.offset, timeout=0 if due else (1 if notice_due else 10),
                                allowed_updates=['chat_member', 'my_chat_member','message'])
        self.dispatch(updates)
        self.activity.schedule()
        self.activity.pump(self.api)
        if due: self.stop.wait(0.2)

    def status(self, state, detail=''):
        channels = []
        for target, engine in self.engines.items():
            pending = engine.db.execute('SELECT count(*) FROM jobs WHERE done=0').fetchone()[0]
            white = engine.db.execute('SELECT count(*) FROM whitelist WHERE target=?', (target,)).fetchone()[0]
            black = engine.db.execute('SELECT count(*) FROM blacklist WHERE target=?', (target,)).fetchone()[0]
            channels.append(dict(target=target, source=engine.cfg['source'], ready=self.ready[target],
                                 pending=pending, whitelist=white, blacklist=black, source_error=engine.source_error))
        atomic_json(self.data / 'status.json', dict(version=VERSION, time=time.time(), pid=os.getpid(),
                   state=state, detail=detail, dry_run=self.cfg['dry_run'], targets=channels))

    def run(self):
        self.log.info('服务器审核启动，目标频道数量=%s；试运行=%s', len(self.engines), self.cfg['dry_run'])
        try:
            while not self.stop.is_set():
                if self.config_path and hashlib.sha256(self.config_path.read_bytes()).hexdigest()!=self.config_digest:
                    self.reload=True
                    break
                try: self.cycle()
                except ApiError as e:
                    self.log.warning('连接失败，将自动重试：%s', e)
                    self.status('等待恢复', str(e))
                    self.stop.wait(max(5, e.retry))
        finally:
            try: self.status('已停止')
            finally: self.close()

    def close(self):
        for engine in self.engines.values():
            engine.stop.set()
            engine.db.close()
        self.engines.clear()
        if self.activity:
            self.activity.close()
            self.activity=None

    def process_commands(self):
        # A previously running command after a crash has an uncertain external
        # outcome. Do not automatically repeat an administrator's manual action.
        with self.activity.db:
            self.activity.db.execute("UPDATE commands SET state='uncertain',result='进程中断，操作结果待核对，请检查名单后再提交' WHERE state='running'")
        rows=self.activity.db.execute("SELECT * FROM commands WHERE state='pending' ORDER BY created LIMIT 10").fetchall()
        for row in rows:
            with self.activity.db:
                self.activity.db.execute("UPDATE commands SET state='running' WHERE id=?",(row['id'],))
            try:
                payload=json.loads(row['payload']); kind=row['kind']
                if kind=='test-notice':
                    if not self.cfg.get('admin_chat_ids'): raise ValueError('请先配置管理员聊天ID，并先私聊机器人发送 /start')
                    with self.activity.db: self.activity.enqueue('test:'+row['id'],'TGMemberGuard 管理员通知测试成功。')
                    result='已加入通知队列；请查看发送记录确认送达'
                elif kind=='report-now':
                    if not self.cfg.get('admin_chat_ids'): raise ValueError('请先配置管理员聊天ID')
                    from datetime import datetime,timedelta
                    day=payload.get('day') or str(datetime.now(ZoneInfo(self.cfg.get('report_timezone','Asia/Shanghai'))).date()-timedelta(days=1))
                    text=self.activity.report(day)
                    with self.activity.db: self.activity.enqueue('manual-report:'+row['id'],text)
                    result='报告已生成并加入通知队列'
                elif kind=='backup':
                    output=self.data/'backups'/row['id']
                    args=argparse.Namespace(command='backup',output=str(output))
                    database_command(args,self.cfg,self.data)
                    result='备份完成：'+row['id']
                elif kind=='check':
                    lines=[]
                    for engine in self.engines.values():
                        try: lines.append(preflight(engine.cfg,self.api))
                        except ApiError as e: lines.append(f"目标 {engine.cfg['target']}：{e}")
                    result='\n'.join(lines)
                else:
                    target=chat_id(payload['target']); rule_for(self.cfg,target)
                    ids=parse_user_ids(str(payload.get('ids','')))
                    if len(ids)>10000: raise ValueError('每次最多管理10000个用户ID')
                    engine=self.engines[target]; db=engine.db
                    if kind=='whitelist-add':
                        existing={r[0] for r in db.execute('SELECT uid FROM whitelist WHERE target=?',(target,))}
                        added,blocked=import_whitelist(db,target,engine.cfg['source'],ids)
                        for uid in ids:
                            if uid not in existing and db.execute('SELECT 1 FROM whitelist WHERE target=? AND uid=?',(target,uid)).fetchone():
                                self.activity.manual_change(target,uid,'whitelist_add',f'{row["id"]}:{uid}')
                        result=f'新增白名单 {added}；跳过黑名单 {blocked}'
                    elif kind=='whitelist-remove':
                        count=0
                        for uid in ids:
                            with db: changed=db.execute('DELETE FROM whitelist WHERE target=? AND uid=?',(target,uid)).rowcount
                            count+=changed
                            if changed: self.activity.manual_change(target,uid,'whitelist_remove',f'{row["id"]}:{uid}')
                        result=f'移除白名单 {count}；不踢现有成员'
                    elif kind=='blacklist-remove':
                        count=0
                        for uid in ids:
                            if not db.execute('SELECT 1 FROM blacklist WHERE target=? AND uid=?',(target,uid)).fetchone(): continue
                            self.api.call('unbanChatMember',chat_id=target,user_id=uid,only_if_banned=True)
                            with db:
                                db.execute('DELETE FROM blacklist WHERE target=? AND uid=?',(target,uid))
                                db.execute('UPDATE jobs SET done=1 WHERE uid=? AND done=0',(uid,))
                            self.activity.manual_change(target,uid,'blacklist_remove',f'{row["id"]}:{uid}'); count+=1
                        result=f'已解除 {count} 个用户的本地及Telegram封禁'
                    else: raise ValueError('未知管理操作')
                state='done'
            except Exception as e:
                result=str(e)[:500]; state='failed'
            with self.activity.db:
                self.activity.db.execute('UPDATE commands SET state=?,result=?,finished=? WHERE id=?',(state,result,time.time(),row['id']))


def rule_for(cfg, target):
    for rule in cfg['targets']:
        if rule['id'] == target: return rule
    raise ValueError('该目标ID未配置，请先 target-add')


def database_command(args, cfg, data):
    if args.command == 'backup':
        output = Path(args.output)
        output.mkdir(parents=True, exist_ok=False)
        for path in sorted((data / 'channels').glob('*.sqlite3')):
            src = sqlite3.connect(path)
            dest = sqlite3.connect(output / path.name)
            try: src.backup(dest)
            finally: src.close(); dest.close()
        activity_path=data/'activity.sqlite3'
        if activity_path.exists():
            src=sqlite3.connect(activity_path); dest=sqlite3.connect(output/'activity.sqlite3')
            try: src.backup(dest)
            finally: src.close(); dest.close()
        if (data / 'poll.json').exists():
            (output / 'poll.json').write_bytes((data / 'poll.json').read_bytes())
        (output / 'config-no-token.json').write_text(json.dumps(cfg, indent=2), encoding='utf-8')
        print('备份完成（不含 Token）：', output)
        return
    rule = rule_for(cfg, args.target)
    db = open_database(database_path(data, args.target))
    activity=Activity(data,cfg)
    try:
        cmd = args.command
        if cmd == 'whitelist-import':
            ids = parse_user_ids(Path(args.file).read_text(encoding='utf-8-sig'))
            existing={r[0] for r in db.execute('SELECT uid FROM whitelist WHERE target=?',(args.target,))}
            added, blocked = import_whitelist(db, args.target, rule['source_id'], ids)
            for uid in ids:
                if uid not in existing and db.execute('SELECT 1 FROM whitelist WHERE target=? AND uid=?',(args.target,uid)).fetchone():
                    activity.manual_change(args.target,uid,'whitelist_add')
            print(f'新增白名单 {added}；跳过黑名单 {blocked}')
        elif cmd == 'whitelist-export':
            rows = db.execute('SELECT uid FROM whitelist WHERE target=? ORDER BY uid', (args.target,)).fetchall()
            Path(args.file).write_text(''.join(f'{r[0]}\n' for r in rows), encoding='utf-8')
            print('已导出', len(rows), '人')
        elif cmd == 'whitelist-remove':
            uid = parse_user_ids(args.user)[0]
            with db: changed=db.execute('DELETE FROM whitelist WHERE target=? AND uid=?', (args.target, uid)).rowcount
            if changed: activity.manual_change(args.target,uid,'whitelist_remove')
            print('已移除白名单；不踢现有成员，下次加入重新核验')
        elif cmd == 'blacklist-remove':
            uid = parse_user_ids(args.user)[0]
            if not db.execute('SELECT 1 FROM blacklist WHERE target=? AND uid=?', (args.target, uid)).fetchone():
                raise ValueError('用户不在本地黑名单中')
            API(dict(token=read_token(cfg), proxy=cfg['proxy'])).call('unbanChatMember',
                chat_id=args.target, user_id=uid, only_if_banned=True)
            with db:
                db.execute('DELETE FROM blacklist WHERE target=? AND uid=?', (args.target, uid))
                db.execute('UPDATE jobs SET done=1 WHERE uid=? AND done=0', (uid,))
            activity.manual_change(args.target,uid,'blacklist_remove')
            print('已解除本地和 Telegram 封禁')
        elif cmd == 'lists':
            for table in ('whitelist', 'blacklist'):
                print(table, [r[0] for r in db.execute(f'SELECT uid FROM {table} WHERE target=? ORDER BY uid', (args.target,))])
        elif cmd == 'migrate-windows':
            source = Path(args.file).resolve()
            if not source.is_file(): raise ValueError('迁移数据库不存在')
            if source == database_path(data, args.target).resolve(): raise ValueError('不能从当前数据库向自身迁移')
            old = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
            try:
                tables = {r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {'whitelist', 'blacklist'} <= tables: raise ValueError('不是1.2版黑白名单数据库')
                whites = old.execute('SELECT target,uid,source,verified_at,origin FROM whitelist WHERE target=?', (args.target,)).fetchall()
                blacks = old.execute('SELECT target,uid,created FROM blacklist WHERE target=?', (args.target,)).fetchall()
                with db:
                    db.executemany('INSERT OR IGNORE INTO whitelist VALUES (?,?,?,?,?)', whites)
                    db.executemany('INSERT OR IGNORE INTO blacklist VALUES (?,?,?)', blacks)
                print(f'已迁移当前目标：白名单 {len(whites)} 条，黑名单 {len(blacks)} 条；黑名单优先')
            finally: old.close()
    finally:
        db.close()
        activity.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='TGMemberGuard Ubuntu 服务器管理 2.1.0')
    parser.add_argument('--config', default='/etc/tgmemberguard/config.json')
    parser.add_argument('--data-dir', default='/var/lib/tgmemberguard')
    sub = parser.add_subparsers(dest='command', required=True)
    for cmd in ('run', 'check', 'status', 'init','web-init'): sub.add_parser(cmd)
    bind=sub.add_parser('web-bind'); bind.add_argument('host',choices=['127.0.0.1','0.0.0.0','::1','::']); bind.add_argument('--port',type=int,default=8787)
    add = sub.add_parser('target-add'); add.add_argument('id', type=chat_id); add.add_argument('--source', type=chat_id); add.add_argument('--name', default='')
    remove = sub.add_parser('target-remove'); remove.add_argument('id', type=chat_id)
    src = sub.add_parser('source-set'); src.add_argument('id', type=chat_id)
    mode = sub.add_parser('mode'); mode.add_argument('value', choices=['live', 'dry-run'])
    for cmd in ('whitelist-import', 'whitelist-export', 'migrate-windows'):
        p = sub.add_parser(cmd); p.add_argument('--target', type=chat_id, required=True); p.add_argument('--file', required=True)
    for cmd in ('whitelist-remove', 'blacklist-remove'):
        p = sub.add_parser(cmd); p.add_argument('--target', type=chat_id, required=True); p.add_argument('--user', required=True)
    lists = sub.add_parser('lists'); lists.add_argument('--target', type=chat_id, required=True)
    backup = sub.add_parser('backup'); backup.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    data, config = Path(args.data_dir).resolve(), Path(args.config).resolve()
    guard.DATA = data
    data.mkdir(parents=True, exist_ok=True)
    runtime_config=data/'runtime-config.json'
    if runtime_config.exists(): config=runtime_config
    if args.command=='web-init':
        try: from .webapp import init_auth
        except ImportError: from webapp import init_auth
        init_auth(data)
        return 0
    if args.command == 'init':
        if config.exists(): raise ValueError('配置已存在；请用管理命令修改，避免覆盖 Token')
        token = getpass.getpass('输入 Bot Token（不会显示）：').strip()
        if not token or ':' not in token or any(c.isspace() for c in token): raise ValueError('Token 格式无效')
        source = chat_id(input('来源频道1 ID：').strip())
        raw_targets = input('目标频道ID（逗号或空格分隔）：').replace('，', ',').replace(',', ' ').split()
        cfg = dict(source_id=source, targets=[dict(id=chat_id(x)) for x in raw_targets],
                   dry_run=True, token_file=str(config.parent / 'bot.token'), proxy='')
        validate_config(cfg)
        config.parent.mkdir(parents=True, exist_ok=True)
        secure_write(cfg['token_file'], token + '\n')
        save_config(config, cfg)
        print('配置已创建，默认为试运行。请运行 check 后启动服务。')
        return
    cfg = read_config(config)
    if args.command in ('target-add', 'target-remove', 'source-set', 'mode','web-bind'):
        # Keep unspecified per-target source inherited from the default when saved.
        raw = json.loads(config.read_text(encoding='utf-8'))
        if args.command == 'target-add':
            rule = dict(id=args.id, name=args.name or str(args.id))
            if args.source is not None: rule['source_id'] = args.source
            raw['targets'].append(rule)
        elif args.command == 'target-remove':
            rule_for(cfg,args.id)
            raw['targets'] = [r for r in raw['targets'] if chat_id(r['id']) != args.id]
        elif args.command == 'source-set': raw['source_id'] = args.id
        elif args.command=='web-bind':
            raw.setdefault('web',{}).update(host=args.host,port=args.port)
        else: raw['dry_run'] = args.value != 'live'
        save_config(config, raw)
        print('配置已保存；执行 sudo systemctl restart tgmemberguard 生效。名单数据保留。')
    elif args.command == 'check':
        api = API(dict(token=read_token(cfg), proxy=cfg['proxy']))
        failed = False
        for rule in cfg['targets']:
            try:
                print(preflight(dict(source=rule['source_id'], target=rule['id']), api))
            except ApiError as e:
                print(f"目标 {rule['id']}：检查失败：{e}"); failed = True
        if failed: return 1
    elif args.command == 'status':
        st = json.loads((data / 'status.json').read_text(encoding='utf-8'))
        st['heartbeat_stale'] = time.time() - st['time'] > 120
        print(json.dumps(st, ensure_ascii=False, indent=2))
    elif args.command == 'run':
        with worker_lock(data):
            log = guard.setup_log()
            console = logging.StreamHandler()
            console.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
            log.addHandler(console)
            if not runtime_config.exists():
                secure_write(runtime_config,config.read_text(encoding='utf-8'))
            config=runtime_config
            shutdown=threading.Event()
            holder=[None]
            def stop(signum,frame):
                shutdown.set()
                if holder[0]: holder[0].stop.set()
            for sig in (signal.SIGTERM,signal.SIGINT): signal.signal(sig,stop)
            web_server=None
            cfg=read_config(config)
            if cfg['web']['enabled']:
                try: from .webapp import create_app
                except ImportError: from webapp import create_app
                from waitress import create_server
                web_server=create_server(create_app(config,data),host=cfg['web']['host'],port=cfg['web']['port'],
                    threads=4,ident='TGMemberGuard',max_request_body_size=4*1024*1024)
                threading.Thread(target=web_server.run,daemon=True).start()
                log.info('网页管理已启动：%s:%s',cfg['web']['host'],cfg['web']['port'])
            try:
                while not shutdown.is_set():
                    snapshot=config.read_bytes()
                    cfg=validate_config(json.loads(snapshot.decode('utf-8-sig')))
                    controller=Coordinator(cfg,read_token(cfg),data,log=log)
                    holder[0]=controller
                    controller.config_path=config
                    controller.config_digest=hashlib.sha256(snapshot).hexdigest()
                    if shutdown.is_set(): controller.stop.set()
                    controller.run()
                    if not controller.reload: break
                    log.info('正在应用新的网页/文件配置')
            finally:
                if web_server: web_server.close()
    else:
        with worker_lock(data): database_command(args, cfg, data)
    return 0


if __name__ == '__main__':
    try: sys.exit(main())
    except Exception as e:
        print(f'操作失败：{e}', file=sys.stderr)
        sys.exit(1)
