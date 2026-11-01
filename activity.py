"""Durable audit ledger, administrator outbox, command queue and daily reports."""
from datetime import datetime, date, time as daytime, timedelta
import json
from pathlib import Path
import sqlite3
import time
import uuid
from zoneinfo import ZoneInfo
from guard import ApiError, is_member

KINDS = {'keep_verified':'来源会员，保留并记录白名单','keep_whitelist':'历史白名单，保留',
    'ban_nonmember':'非会员，已移出并永久封禁','ban_blacklist':'黑名单再次进入，已移出并永久封禁',
    'dry_nonmember':'试运行：非会员，本次未踢人','dry_blacklist':'试运行：黑名单，本次未踢人',
    'skip':'已离开或为管理员，跳过','blacklist_add':'新增黑名单','whitelist_add':'新增白名单',
    'whitelist_remove':'移除白名单','blacklist_remove':'解除黑名单'}


def connect(data):
    db = sqlite3.connect(Path(data)/'activity.sqlite3', timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript('''
      CREATE TABLE IF NOT EXISTS joins (event_key TEXT PRIMARY KEY,target INTEGER,uid INTEGER,at REAL);
      CREATE TABLE IF NOT EXISTS results (event_key TEXT PRIMARY KEY,target INTEGER,uid INTEGER,kind TEXT,at REAL,dry_run INTEGER);
      CREATE TABLE IF NOT EXISTS changes (event_key TEXT PRIMARY KEY,target INTEGER,uid INTEGER,kind TEXT,at REAL);
      CREATE TABLE IF NOT EXISTS cursors (name TEXT PRIMARY KEY,value TEXT);
      CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY,chat INTEGER,text TEXT,state TEXT DEFAULT 'pending',
        attempts INTEGER DEFAULT 0,due REAL DEFAULT 0,last_error TEXT DEFAULT '',created REAL,sent REAL);
      CREATE TABLE IF NOT EXISTS reports (id TEXT PRIMARY KEY,day TEXT,timezone TEXT,text TEXT,created REAL);
      CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY,kind TEXT,payload TEXT,state TEXT DEFAULT 'pending',
        result TEXT DEFAULT '',created REAL,finished REAL);
    ''')
    return db


def enqueue_command(data, kind, payload):
    ident=uuid.uuid4().hex
    db=connect(data)
    try:
        with db: db.execute('INSERT INTO commands(id,kind,payload,created) VALUES (?,?,?,?)',
                           (ident,kind,json.dumps(payload),time.time()))
    finally: db.close()
    return ident


class Activity:
    def __init__(self,data,cfg):
        self.data,self.cfg=Path(data),cfg
        self.db=connect(data)
        self.cooldown={}

    def close(self): self.db.close()

    def name(self,target):
        return next((r['name'] for r in self.cfg['targets'] if r['id']==target),str(target))

    def notice(self,key,text,category='result'):
        if category not in self.cfg.get('notify_types',['join','result','list']): return
        self.enqueue(key,text)

    def enqueue(self,key,text):
        for chat in self.cfg.get('admin_chat_ids',[]):
            for i,start in enumerate(range(0,len(text),1900)):
                self.db.execute('INSERT OR IGNORE INTO outbox(id,chat,text,created) VALUES (?,?,?,?)',
                                (f'{key}:{chat}:{i}',chat,text[start:start+1900],time.time()))

    def record_joins(self,updates,namespace):
        targets={r['id'] for r in self.cfg['targets']}
        with self.db:
            for update in updates:
                message=update.get('message',{})
                chat=message.get('chat',{})
                command=str(message.get('text','')).split(' ',1)[0].split('@',1)[0]
                if chat.get('type')=='private' and command in ('/start','/id') and type(chat.get('id')) is int:
                    recent=self.db.execute("SELECT 1 FROM outbox WHERE chat=? AND id LIKE 'reply:%' AND created>? LIMIT 1",(chat['id'],time.time()-30)).fetchone()
                    if not recent:
                        self.db.execute('INSERT OR IGNORE INTO outbox(id,chat,text,created) VALUES (?,?,?,?)',
                            (f"reply:{namespace}:{update['update_id']}:{chat['id']}",chat['id'],
                             f"你的聊天 ID：{chat['id']}\n频道主可将此ID填入网页后台的管理员通知设置。",time.time()))
                cm=update.get('chat_member',{})
                target=cm.get('chat',{}).get('id')
                new=cm.get('new_chat_member',{}); user=new.get('user',{})
                if target not in targets or user.get('is_bot') or new.get('status') in ('creator','administrator'): continue
                if not is_member(new) or is_member(cm.get('old_chat_member',{})): continue
                key=f"{namespace}:{update['update_id']}:{target}"
                stamp=float(cm.get('date',time.time()))
                added=self.db.execute('INSERT OR IGNORE INTO joins VALUES (?,?,?,?)',(key,target,user['id'],stamp)).rowcount
                if added:
                    who=str(user.get('first_name','')).replace('\n',' ')[:80]
                    self.notice('join:'+key,f'入群待审核\n频道：{self.name(target)} ({target})\n用户ID：{user["id"]} {who}', 'join')

    def sync(self,engine):
        for table,local in (('audit','results'),('list_changes','changes')):
            cursor_key=f"{engine.cfg['target']}:{table}"
            row=self.db.execute('SELECT value FROM cursors WHERE name=?',(cursor_key,)).fetchone()
            cursor=int(row[0]) if row else 0
            rows=engine.db.execute(f'SELECT rowid,* FROM {table} WHERE rowid>? ORDER BY rowid LIMIT 500',(cursor,)).fetchall()
            with self.db:
                for values in rows:
                    if table=='audit':
                        index,key,update,target,uid,kind,at,dry=values
                        key=f'{key}:{target}'
                        added=self.db.execute('INSERT OR IGNORE INTO results VALUES (?,?,?,?,?,?)',
                                              (key,target,uid,kind,at,dry)).rowcount
                        if added:
                            self.notice('result:'+key,f'审核结果\n频道：{self.name(target)} ({target})\n用户ID：{uid}\n结果：{KINDS.get(kind,kind)}')
                    else:
                        index,key,target,uid,kind,at=values
                        key=f'{key}:{target}'
                        added=self.db.execute('INSERT OR IGNORE INTO changes VALUES (?,?,?,?,?)',(key,target,uid,kind,at)).rowcount
                        if added:
                            self.notice('list:'+key,f'名单变更\n频道：{self.name(target)} ({target})\n用户ID：{uid}\n操作：{KINDS.get(kind,kind)}','list')
                if rows: self.db.execute('INSERT OR REPLACE INTO cursors VALUES (?,?)',(cursor_key,str(rows[-1][0])))

    def manual_change(self,target,uid,kind,key=None):
        key=key or uuid.uuid4().hex
        with self.db:
            added=self.db.execute('INSERT OR IGNORE INTO changes VALUES (?,?,?,?,?)',(key,target,uid,kind,time.time())).rowcount
            if added: self.notice('list:'+key,f'管理员操作\n频道：{self.name(target)} ({target})\n用户ID：{uid}\n操作：{KINDS.get(kind,kind)}','list')

    def report(self,day,timezone=None):
        timezone=timezone or self.cfg.get('report_timezone','Asia/Shanghai')
        zone=ZoneInfo(timezone); day=date.fromisoformat(str(day))
        start=datetime.combine(day,daytime.min,zone).timestamp()
        end=datetime.combine(day+timedelta(days=1),daytime.min,zone).timestamp()
        counts={}
        for table,column,label in [('joins','COUNT(*)','joins'),('results',"SUM(CASE WHEN kind IN ('ban_nonmember','ban_blacklist') THEN 1 ELSE 0 END)",'bans'),
                                   ('changes',"SUM(CASE WHEN kind='blacklist_add' THEN 1 ELSE 0 END)",'black')]:
            for r in self.db.execute(f'SELECT target,{column} AS n FROM {table} WHERE at>=? AND at<? GROUP BY target',(start,end)):
                counts.setdefault(r['target'],dict(joins=0,bans=0,black=0))[label]=int(r['n'] or 0)
        for rule in self.cfg['targets']: counts.setdefault(rule['id'],dict(joins=0,bans=0,black=0))
        total={k:sum(v[k] for v in counts.values()) for k in ('joins','bans','black')}
        unique=self.db.execute('SELECT COUNT(DISTINCT uid) FROM joins WHERE at>=? AND at<?',(start,end)).fetchone()[0]
        text=[f'TG会员审核日报 · {day}',f'时区：{timezone}，00:00–24:00',
              f'入群：{total["joins"]} 人次（去重 {unique} 人）',f'实际踢人：{total["bans"]} 次',f'新增黑名单：{total["black"]} 次']
        for target,c in sorted(counts.items()):
            black_total=0
            path=self.data/'channels'/f'{target}.sqlite3'
            if path.exists():
                db=sqlite3.connect(path)
                try: black_total=db.execute('SELECT count(*) FROM blacklist WHERE target=?',(target,)).fetchone()[0]
                finally: db.close()
            text += ['',f'{self.name(target)} ({target})',f'入群 {c["joins"]} | 踢人 {c["bans"]} | 新增黑名单 {c["black"]} | 当前黑名单 {black_total}']
            ids=[str(r[0]) for r in self.db.execute("SELECT DISTINCT uid FROM changes WHERE target=? AND kind='blacklist_add' AND at>=? AND at<? ORDER BY uid",(target,start,end))]
            if ids: text.append('新增黑名单ID：'+', '.join(ids))
        text.append('\n统计基于本版本部署后已接收的事件；试运行不计入实际踢人。')
        return '\n'.join(text)

    def schedule(self,now=None):
        if not self.cfg.get('admin_chat_ids') or not self.cfg.get('daily_report',True): return
        zone=self.cfg.get('report_timezone','Asia/Shanghai')
        today=datetime.fromtimestamp(time.time() if now is None else now,ZoneInfo(zone)).date()
        cursor_key='daily:'+zone
        row=self.db.execute('SELECT value FROM cursors WHERE name=?',(cursor_key,)).fetchone()
        next_day=date.fromisoformat(row[0]) if row else today
        with self.db:
            for _ in range(7):
                if next_day>=today: break
                ident=f'{zone}:{next_day}'
                text=self.report(next_day,zone)
                added=self.db.execute('INSERT OR IGNORE INTO reports VALUES (?,?,?,?,?)',
                    (ident,str(next_day),zone,text,time.time())).rowcount
                if added: self.enqueue('daily:'+ident,text)
                next_day+=timedelta(days=1)
            self.db.execute('INSERT OR REPLACE INTO cursors VALUES (?,?)',(cursor_key,str(next_day)))

    def pump(self,api,limit=2):
        rows=self.db.execute("SELECT * FROM outbox WHERE state='pending' AND due<=? ORDER BY CASE WHEN id LIKE 'daily:%' THEN 0 ELSE 1 END,created,id LIMIT 50",(time.time(),)).fetchall()
        delivered=0
        for row in rows:
            if row['chat'] not in self.cfg.get('admin_chat_ids',[]) and not row['id'].startswith('reply:'):
                with self.db: self.db.execute("UPDATE outbox SET state='cancelled' WHERE id=?",(row['id'],))
                continue
            if self.cooldown.get(row['chat'],0)>time.time(): continue
            try:
                api.call('sendMessage',chat_id=row['chat'],text=row['text'],link_preview_options=dict(is_disabled=True))
                with self.db: self.db.execute("UPDATE outbox SET state='sent',sent=?,attempts=attempts+1,last_error='' WHERE id=?",(time.time(),row['id']))
                self.cooldown[row['chat']]=time.time()+(3.2 if row['chat']<0 else 1.2)
            except ApiError as e:
                delay=max(min(3600,5*2**min(row['attempts'],10)),e.retry)
                with self.db:
                    self.db.execute('UPDATE outbox SET attempts=attempts+1,due=?,last_error=? WHERE id=?',
                                    (time.time()+delay,str(e)[:500],row['id']))
                if e.retry: break
            delivered+=1
            if delivered>=limit: break
