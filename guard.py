"""TG Member Guard: durable post-join membership enforcement."""
import ctypes
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
from pathlib import Path
import sqlite3
import threading
import time

import requests

DATA = Path(os.environ.get('PROGRAMDATA', str(Path.home()))) / 'TGMemberGuard'


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_name(f'.{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        for attempt in range(8):
            try:
                os.replace(temp, path)
                break
            except PermissionError:
                # Windows readers can temporarily deny replacement of an open
                # status file. Keep the old valid snapshot and retry briefly.
                if attempt == 7: raise
                time.sleep(0.01 * (attempt + 1))
    finally:
        temp.unlink(missing_ok=True)


def setup_log():
    DATA.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger('guard')
    log.setLevel(logging.INFO)
    if not log.handlers:
        handler = RotatingFileHandler(DATA / 'guard.log', maxBytes=2_000_000, backupCount=5, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        log.addHandler(handler)
    return log


def save_config(token, source, target, dry_run, proxy=''):
    import win32crypt
    if not token.strip() or ':' not in token:
        raise ValueError('请填写有效的 Bot Token')
    source, target = int(source), int(target)
    if source >= 0 or target >= 0 or source == target:
        raise ValueError('两个群 ID 必须是不同的负数')
    if proxy and not proxy.startswith(('http://', 'https://', 'socks5h://')):
        raise ValueError('代理应以 http://、https:// 或 socks5h:// 开头')
    DATA.mkdir(parents=True, exist_ok=True)
    encrypted = win32crypt.CryptProtectData(token.strip().encode(), 'TGMemberGuard', None, None, None, 4)
    atomic_json(DATA / 'config.json', dict(token=encrypted.hex(), source=source, target=target,
                                         dry_run=bool(dry_run), proxy=proxy.strip()))


def load_config():
    import win32crypt
    cfg = json.loads((DATA / 'config.json').read_text(encoding='utf-8'))
    cfg['token'] = win32crypt.CryptUnprotectData(bytes.fromhex(cfg['token']), None, None, None, 0)[1].decode()
    return cfg


class ApiError(Exception):
    def __init__(self, description, retry=0, code=0):
        super().__init__(description)
        self.retry, self.code = retry, code


class API:
    def __init__(self, cfg):
        self.url = 'https://api.telegram.org/bot' + cfg['token'] + '/'
        self.session = requests.Session()
        self.timeouts = (10, 40)
        if cfg.get('proxy'):
            self.session.proxies.update(http=cfg['proxy'], https=cfg['proxy'])

    def call(self, method, **kwargs):
        try:
            r = self.session.post(self.url + method, json=kwargs, timeout=self.timeouts)
            body = r.json()
        except (requests.RequestException, ValueError):
            # Never log request exceptions: they can include the Bot Token URL.
            raise ApiError('Telegram 连接失败或返回异常，请检查网络/代理') from None
        if not isinstance(body, dict):
            raise ApiError('Telegram 返回格式异常')
        if not body.get('ok'):
            description = str(body.get('description', 'Telegram API error'))
            token = self.url.split('/bot', 1)[1].rstrip('/')
            raise ApiError(description.replace(token, '[REDACTED]'),
                           body.get('parameters', {}).get('retry_after', 0), body.get('error_code', 0))
        return body['result']


def is_member(member):
    return member.get('status') in ('creator', 'administrator', 'member') or (
        member.get('status') == 'restricted' and member.get('is_member') is True)


def preflight(cfg, api, require_source=True):
    me = api.call('getMe')
    webhook = api.call('getWebhookInfo')
    if webhook.get('url'):
        raise ApiError('此 Token 已配置 Webhook，请使用独立的新机器人；程序不会删除现有 Webhook')
    names = []
    for key in (('source', 'target') if require_source else ('target',)):
        chat = api.call('getChat', chat_id=cfg[key])
        if chat['type'] not in ('supergroup', 'channel'):
            raise ApiError(('来源1' if key == 'source' else '目标2') + '必须是频道或超级群，请核对 ID；普通群需先升级')
        member = api.call('getChatMember', chat_id=cfg[key], user_id=me['id'])
        if member['status'] not in ('administrator', 'creator'):
            raise ApiError(('来源频道/群1' if key == 'source' else '目标频道/群2') + '中机器人必须为管理员')
        if key == 'target' and member['status'] != 'creator' and not member.get('can_restrict_members'):
            raise ApiError('目标频道/群2中机器人缺少「封禁用户 / 移除订阅者」权限（can_restrict_members）')
        names.append(chat.get('title', str(cfg[key])))
    return (f"@{me.get('username', me['id'])}；来源1：{names[0]}；目标2：{names[1]}" if require_source
            else f"@{me.get('username', me['id'])}；目标2：{names[0]}；来源1按审核任务检查，异常时保护未知用户")


def open_database(path=None):
    db = sqlite3.connect(str(path or DATA / 'queue.sqlite3'))
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS jobs (
          id INTEGER PRIMARY KEY, uid INTEGER NOT NULL, attempts INTEGER DEFAULT 0,
          due REAL DEFAULT 0, done INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS blacklist (
          target INTEGER NOT NULL, uid INTEGER NOT NULL, created REAL NOT NULL,
          PRIMARY KEY(target,uid));
        CREATE TABLE IF NOT EXISTS whitelist (
          target INTEGER NOT NULL, uid INTEGER NOT NULL, source INTEGER NOT NULL,
          verified_at REAL NOT NULL, origin TEXT NOT NULL,
          PRIMARY KEY(target,uid));
        CREATE TABLE IF NOT EXISTS audit (
          event_key TEXT PRIMARY KEY, update_id INTEGER, target INTEGER, uid INTEGER,
          kind TEXT, at REAL, dry_run INTEGER);
        CREATE TABLE IF NOT EXISTS list_changes (
          event_key TEXT PRIMARY KEY, target INTEGER, uid INTEGER, kind TEXT, at REAL);
    ''')
    return db


def parse_user_ids(text):
    parts = [p for p in re.split(r'[\s,，;；]+', text.strip()) if p]
    if not parts or any(not p.isascii() or not p.isdecimal() or not 0 < int(p) < 2**63 for p in parts):
        raise ValueError('请仅填写正整数用户ID，用换行、空格或逗号分隔；不接受用户名或群ID')
    return sorted(set(map(int, parts)))


def import_whitelist(db, target, source, ids):
    added = blocked = 0
    with db:
        for uid in ids:
            if db.execute('SELECT 1 FROM blacklist WHERE target=? AND uid=?', (target, uid)).fetchone():
                blocked += 1
                continue
            added += db.execute('INSERT OR IGNORE INTO whitelist VALUES (?,?,?,?,?)',
                                (target, uid, source, time.time(), 'manual')).rowcount
    return added, blocked


class Engine:
    def __init__(self, cfg, db_path=None, api=None, log=None, preserve_pending=False):
        self.cfg = cfg
        self.api = api or API(cfg)
        self.log = log or setup_log()
        self.stop = threading.Event()
        self.db = open_database(db_path)
        self.bot_id = None
        self.source_error = ''
        self.audit_namespace = hashlib.sha256(cfg['token'].encode()).hexdigest()
        scope = hashlib.sha256(f"{cfg['token']}:{cfg['source']}:{cfg['target']}:{cfg['dry_run']}".encode()).hexdigest()
        old = self.db.execute("SELECT value FROM meta WHERE key='scope'").fetchone()
        if old and old[0] != scope and not preserve_pending:
            self.db.execute('DELETE FROM jobs')
            self.db.execute("DELETE FROM meta WHERE key='offset'")
        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('scope',?)", (scope,))
        self.db.commit()

    def source_health(self):
        try:
            if self.bot_id is None:
                self.bot_id = self.api.call('getMe')['id']
            chat = self.api.call('getChat', chat_id=self.cfg['source'])
            member = self.api.call('getChatMember', chat_id=self.cfg['source'], user_id=self.bot_id)
            if chat.get('type') not in ('channel', 'supergroup') or member.get('status') not in ('administrator', 'creator'):
                raise ApiError('来源1不可用或审核机器人已失去管理员权限')
            self.source_error = ''
        except ApiError as e:
            self.source_error = '来源1异常保护：' + str(e)
            raise

    def ingest(self, updates):
        with self.db:
            for update in updates:
                cm = update.get('chat_member', {})
                if cm.get('chat', {}).get('id') == self.cfg['target']:
                    new = cm.get('new_chat_member', {})
                    user = new.get('user', {})
                    if is_member(new) and not is_member(cm.get('old_chat_member', {})):
                        if not user.get('is_bot') and new.get('status') not in ('creator', 'administrator'):
                            self.db.execute('INSERT OR IGNORE INTO jobs(id,uid) VALUES (?,?)',
                                            (update['update_id'], user['id']))
                self.db.execute("INSERT OR REPLACE INTO meta VALUES ('offset',?)", (str(update['update_id'] + 1),))

    def process(self, limit=20):
        rows = self.db.execute('SELECT id,uid,attempts FROM jobs WHERE done=0 AND due<=? ORDER BY id LIMIT ?', (time.time(), limit)).fetchall()
        for job, uid, attempts in rows:
            if self.stop.is_set():
                return
            try:
                kind = 'skip'
                event_key = f'{self.audit_namespace}:{job}'
                # Recheck current target state, protecting people promoted since the event.
                target = self.api.call('getChatMember', chat_id=self.cfg['target'], user_id=uid)
                if target['status'] in ('creator', 'administrator') or not is_member(target):
                    result = '已离群或为管理员，跳过'
                elif self.db.execute('SELECT 1 FROM blacklist WHERE target=? AND uid=?', (self.cfg['target'], uid)).fetchone():
                    if self.cfg['dry_run']:
                        kind = 'dry_blacklist'
                        result = '试运行：命中本地黑名单，正式模式将永久封禁'
                    else:
                        self.api.call('banChatMember', chat_id=self.cfg['target'], user_id=uid, until_date=0)
                        result = '命中本地黑名单，已再次移出并永久封禁'
                        kind = 'ban_blacklist'
                elif self.db.execute('SELECT 1 FROM whitelist WHERE target=? AND uid=?', (self.cfg['target'], uid)).fetchone():
                    kind = 'keep_whitelist'
                    result = '命中历史会员白名单，保留（更换来源1不影响资格）'
                else:
                    self.source_health()
                    source = self.api.call('getChatMember', chat_id=self.cfg['source'], user_id=uid)
                    if is_member(source):
                        with self.db:
                            inserted = self.db.execute('INSERT OR IGNORE INTO whitelist VALUES (?,?,?,?,?)',
                                            (self.cfg['target'], uid, self.cfg['source'], time.time(), 'verified')).rowcount
                            if inserted:
                                self.db.execute('INSERT OR IGNORE INTO list_changes VALUES (?,?,?,?,?)',
                                    (event_key, self.cfg['target'], uid, 'whitelist_add', time.time()))
                        kind = 'keep_verified'
                        result = '来源1会员，保留并记录本地白名单'
                    elif source.get('status') in ('left', 'kicked') or (source.get('status') == 'restricted' and source.get('is_member') is False):
                        # Check source health again before any negative decision is persisted.
                        self.source_health()
                        if self.cfg['dry_run']:
                            kind = 'dry_nonmember'
                            result = '试运行：非群1会员，正式模式将移出'
                        else:
                            with self.db:
                                inserted = self.db.execute('INSERT OR IGNORE INTO blacklist VALUES (?,?,?)',
                                                (self.cfg['target'], uid, time.time())).rowcount
                                if inserted:
                                    self.db.execute('INSERT OR IGNORE INTO list_changes VALUES (?,?,?,?,?)',
                                        (event_key, self.cfg['target'], uid, 'blacklist_add', time.time()))
                            # Permanent Telegram-side ban survives restarts and blocks invite reentry.
                            self.api.call('banChatMember', chat_id=self.cfg['target'], user_id=uid,
                                          until_date=0)
                            result = '非来源1会员，已移出并永久加入本地及目标2黑名单（需在管理界面解封）'
                            kind = 'ban_nonmember'
                    else:
                        raise ApiError('未知成员状态，暂停处置并重试')
                self.log.info('用户 %s：%s', uid, result)
                with self.db:
                    self.db.execute('INSERT OR IGNORE INTO audit VALUES (?,?,?,?,?,?,?)',
                        (event_key, job, self.cfg['target'], uid, kind, time.time(), int(self.cfg['dry_run'])))
                    self.db.execute('UPDATE jobs SET done=1 WHERE id=?', (job,))
            except ApiError as e:
                delay = max(min(300, 5 * 2 ** min(attempts, 6)), e.retry)
                with self.db:
                    self.db.execute('UPDATE jobs SET attempts=attempts+1,due=? WHERE id=?', (time.time()+delay, job))
                self.log.warning('用户 %s 审核失败，%s秒后重试：%s', uid, delay, e)
                if e.retry:
                    break

    def status(self, state, detail=''):
        pending = self.db.execute('SELECT count(*) FROM jobs WHERE done=0').fetchone()[0]
        atomic_json(DATA / 'status.json', dict(time=time.time(), state=state, detail=detail,
                   pending=pending, dry_run=self.cfg['dry_run'], pid=os.getpid()))

    def run(self):
        try:
            while not self.stop.is_set():
                try:
                    self.status('检查权限')
                    info = preflight(self.cfg, self.api, require_source=False)
                    self.log.info('审核启动：%s；试运行=%s', info, self.cfg['dry_run'])
                    break
                except ApiError as e:
                    self.log.warning('启动检查失败：%s', e)
                    self.status('等待恢复', str(e))
                    self.stop.wait(max(15, e.retry))
            while not self.stop.is_set():
                try:
                    self.process()
                    self.status('来源异常保护' if self.source_error else '运行中', self.source_error)
                    row = self.db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()
                    updates = self.api.call('getUpdates', offset=int(row[0]) if row else 0,
                                            timeout=10, allowed_updates=['chat_member', 'my_chat_member'])
                    self.ingest(updates)
                    with self.db:
                        self.db.execute('DELETE FROM jobs WHERE done=1 AND id < (SELECT coalesce(max(id),0)-100000 FROM jobs)')
                except ApiError as e:
                    self.log.warning('连接中断：%s', e)
                    self.status('等待恢复', str(e))
                    self.stop.wait(max(5, e.retry))
        finally:
            self.status('已停止')
            self.db.close()


def acquire_worker():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel.CreateMutexW(None, False, 'Global\\TGMemberGuardWorker')
    if not handle or ctypes.get_last_error() == 183:
        raise RuntimeError('审核进程已运行，请勿同时启动多个实例')
    return handle
