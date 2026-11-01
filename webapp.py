"""Authenticated local management UI; writes jobs, never executes shell commands."""
from datetime import datetime,timedelta
import getpass
import hashlib
import hmac
import io
import ipaddress
import json
from pathlib import Path
import re
import secrets
import sqlite3
import tarfile
import threading
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
from flask import Flask,request,session,redirect,url_for,render_template,jsonify,send_file,abort
from werkzeug.security import generate_password_hash,check_password_hash

try:
    from . import server
    from .activity import connect,enqueue_command,Activity
except ImportError:
    import server
    from activity import connect,enqueue_command,Activity


def write_auth(data,username,password,secret=None):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{3,40}',username): raise ValueError('用户名需3–40位字母、数字或 _. -')
    if not 12<=len(password)<=256: raise ValueError('密码长度需12–256位')
    path=Path(data)/'web-auth.json'
    server.secure_write(path,json.dumps(dict(username=username,
        password_hash=generate_password_hash(password,method='pbkdf2:sha256:600000'),
        secret=secret or secrets.token_hex(32))))
    path.chmod(0o600)


def init_auth(data):
    username=input('后台用户名（默认admin）：').strip() or 'admin'
    password=getpass.getpass('后台密码（至少12位，不显示）：')
    if password!=getpass.getpass('再次输入密码：'): raise ValueError('两次密码不一致')
    write_auth(data,username,password)
    print('后台账号已创建/重置。重启服务后用该账号登录。')


def auth_version(auth): return hashlib.sha256(auth['password_hash'].encode()).hexdigest()


def create_app(config_path,data):
    config_path,data=Path(config_path),Path(data)
    cfg=server.read_config(config_path,allow_unconfigured=True)
    auth_path=data/'web-auth.json'
    def read_auth():
        if not auth_path.exists(): return None
        return json.loads(auth_path.read_text(encoding='utf-8'))
    auth=read_auth()
    app=Flask(__name__,template_folder=str(Path(__file__).parent/'templates'),static_folder=str(Path(__file__).parent/'static'))
    app.secret_key=auth['secret'] if auth else secrets.token_hex(32)
    app.config.update(MAX_CONTENT_LENGTH=4*1024*1024,SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',SESSION_COOKIE_SECURE=cfg['web']['secure_cookie'],
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8))
    if cfg['web']['behind_proxy']:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app=ProxyFix(app.wsgi_app,x_for=1,x_proto=1,x_host=1)
    edit_lock=threading.Lock()

    def csrf():
        if 'csrf' not in session: session['csrf']=secrets.token_hex(32)
        return session['csrf']
    app.jinja_env.globals['csrf_token']=csrf

    @app.before_request
    def protect():
        # Unknown asset routes (e.g. the browser's favicon request) must not
        # clear the login form's session and rotate its CSRF cookie.
        if request.endpoint is None: return
        auth=read_auth()
        if request.endpoint=='static': return
        if not auth:
            try:
                address=ipaddress.ip_address(request.remote_addr or '')
                local=address.is_loopback or bool(getattr(address,'ipv4_mapped',None) and address.ipv4_mapped.is_loopback)
            except ValueError: local=False
            if not local or urlsplit(request.host_url).hostname not in ('localhost','127.0.0.1','::1'):
                return '首次初始化仅允许本机或SSH隧道访问 http://127.0.0.1:8787。',403
            if request.endpoint not in ('setup','static'): return redirect(url_for('setup'))
        elif request.endpoint=='setup':
            if request.method=='POST': return jsonify(error='管理员已经创建，不能重新初始化'),409
            return redirect(url_for('home'))
        if request.method=='POST':
            token=request.headers.get('X-CSRF-Token') or request.form.get('csrf','')
            if not hmac.compare_digest(str(session.get('csrf','missing')),str(token)):
                return jsonify(error='请求校验失败，请刷新页面重试'),403
        if not auth: return
        if request.endpoint!='login' and (session.get('user')!=auth['username'] or session.get('auth_version')!=auth_version(auth)):
            session.clear()
            if request.path.startswith('/api/'): return jsonify(error='请先登录'),401
            return redirect(url_for('login'))

    @app.after_request
    def headers(response):
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Referrer-Policy']='same-origin'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'self'"
        if request.is_secure: response.headers['Strict-Transport-Security']='max-age=31536000'
        return response

    @app.errorhandler(ValueError)
    def value_error(error): return jsonify(error=str(error)),400

    @app.route('/setup',methods=['GET','POST'])
    def setup():
        error=None
        if request.method=='POST':
            with edit_lock:
                if read_auth(): return jsonify(error='初始化已完成，请登录'),409
                username=request.form.get('username','').strip()
                password=request.form.get('password','')
                try:
                    if password!=request.form.get('confirm',''): raise ValueError('两次密码不一致')
                    write_auth(data,username,password)
                except ValueError as e:
                    return render_template('setup.html',error=str(e)),400
                auth=read_auth(); app.secret_key=auth['secret']
                session.clear(); session.permanent=True
                session['user']=auth['username']; session['auth_version']=auth_version(auth); csrf()
                return redirect(url_for('home'))
        return render_template('setup.html',error=error)

    @app.route('/login',methods=['GET','POST'])
    def login():
        error=None
        if request.method=='POST':
            db=sqlite3.connect(data/'web-security.sqlite3',timeout=10)
            db.execute('CREATE TABLE IF NOT EXISTS failures(ip TEXT,at REAL)')
            ip=request.remote_addr or 'unknown'
            try:
                with db: db.execute('DELETE FROM failures WHERE at<?',(time.time()-3600,))
                count=db.execute('SELECT count(*) FROM failures WHERE ip=? AND at>?',(ip,time.time()-900)).fetchone()[0]
                if count>=5:
                    return render_template('login.html',error='失败次数过多，请15分钟后重试'),429
                auth=read_auth()
                password=request.form.get('password','')
                valid=len(password)<=256 and check_password_hash(auth['password_hash'],password)
                if valid and hmac.compare_digest(request.form.get('username','').encode(),auth['username'].encode()):
                    with db: db.execute('DELETE FROM failures WHERE ip=?',(ip,))
                    session.clear(); session.permanent=True
                    session['user']=auth['username']; session['auth_version']=auth_version(auth); csrf()
                    return redirect(url_for('home'))
                with db: db.execute('INSERT INTO failures VALUES (?,?)',(ip,time.time()))
                error='用户名或密码错误'
            finally: db.close()
        return render_template('login.html',error=error)

    @app.get('/')
    def home(): return render_template('dashboard.html',username=read_auth()['username'])

    @app.post('/api/logout')
    def logout(): session.clear(); return jsonify(ok=True)

    @app.get('/api/state')
    def state():
        raw=json.loads(config_path.read_text(encoding='utf-8')); cfg=server.validate_config(raw,allow_unconfigured=True)
        safe={key:cfg[key] for key in ('source_id','dry_run','paused','admin_chat_ids','report_timezone','daily_report','notify_types')}
        safe['targets']=raw['targets']; safe['proxy_configured']=bool(cfg['proxy'])
        safe['setup_pending']=cfg.get('setup_pending',False)
        safe['token_configured']=Path(cfg['token_file']).is_file()
        status={}
        try: status=json.loads((data/'status.json').read_text(encoding='utf-8'))
        except (OSError,ValueError): pass
        db=connect(data)
        try:
            events=[dict(r) for r in db.execute('SELECT target,uid,kind,at FROM results ORDER BY at DESC LIMIT 50')]
            commands=[dict(r) for r in db.execute('SELECT id,kind,state,result,created FROM commands ORDER BY created DESC LIMIT 15')]
            notices=[dict(r) for r in db.execute('SELECT chat,state,attempts,last_error,created FROM outbox ORDER BY created DESC LIMIT 20')]
            reports=[dict(r) for r in db.execute('SELECT day,timezone,text,created FROM reports ORDER BY created DESC LIMIT 7')]
            pending=db.execute("SELECT count(*) FROM outbox WHERE state='pending'").fetchone()[0]
        finally: db.close()
        activity=Activity(data,cfg)
        try: today_report=activity.report(datetime.now(ZoneInfo(cfg['report_timezone'])).date())
        finally: activity.close()
        return jsonify(config=safe,revision=hashlib.sha256(config_path.read_bytes()).hexdigest(),status=status,
            events=events,commands=commands,notices=notices,reports=reports,pending_notices=pending,today_report=today_report,
            backups=sorted(p.name for p in (data/'backups').glob('*') if p.is_dir() and re.fullmatch('[0-9a-f]{32}',p.name)))

    @app.post('/api/config')
    def configure():
        payload=request.get_json(force=True)
        with edit_lock:
            original=config_path.read_bytes()
            if payload.get('revision')!=hashlib.sha256(original).hexdigest():
                return jsonify(error='配置已被修改，请刷新后再保存'),409
            raw=json.loads(original)
            for key in ('source_id','targets','dry_run','paused','admin_chat_ids','report_timezone','daily_report','notify_types'):
                if key in payload: raw[key]=payload[key]
            if payload.get('clear_proxy'): raw['proxy']=''
            elif payload.get('proxy'): raw['proxy']=payload['proxy']
            raw.pop('setup_pending',None)
            server.validate_config(raw)
            token=payload.get('bot_token','')
            if not isinstance(token,str): raise ValueError('Bot Token格式无效')
            token=token.strip()
            if token:
                if ':' not in token or any(c.isspace() for c in token): raise ValueError('Bot Token格式无效')
                token_path=data/'bot.token'
                server.secure_write(token_path,token+'\n')
                token_path.chmod(0o600)
                raw['token_file']=str(token_path)
                raw['token_revision']=secrets.token_hex(8)
            elif not Path(raw.get('token_file','')).is_file():
                raise ValueError('首次配置请填写 Bot Token')
            server.save_config(config_path,raw)
        return jsonify(ok=True,message='配置已保存，审核进程将在当前轮询完成后自动应用')

    @app.post('/api/command')
    def command():
        payload=request.get_json(force=True)
        kind=payload.get('kind')
        if kind not in ('whitelist-add','whitelist-remove','blacklist-remove','test-notice','report-now','backup','check'):
            raise ValueError('不支持的操作')
        clean={}
        if kind in ('whitelist-add','whitelist-remove','blacklist-remove'):
            target=server.chat_id(payload['target']); server.rule_for(server.read_config(config_path),target)
            ids=server.parse_user_ids(str(payload.get('ids','')))
            if len(ids)>10000: raise ValueError('每次最多10000个ID')
            clean=dict(target=target,ids=' '.join(map(str,ids)))
        if kind=='report-now' and payload.get('day'):
            from datetime import date
            clean['day']=str(date.fromisoformat(payload['day']))
        return jsonify(ok=True,id=enqueue_command(data,kind,clean),message='已提交后台执行，请查看操作记录与通知状态')

    @app.get('/api/lists')
    def lists():
        target=server.chat_id(request.args.get('target')); server.rule_for(server.read_config(config_path),target)
        path=server.database_path(data,target)
        db=server.open_database(path)
        try:
            result={table:[r[0] for r in db.execute(f'SELECT uid FROM {table} WHERE target=? ORDER BY uid LIMIT 1000',(target,))] for table in ('whitelist','blacklist')}
        finally: db.close()
        return jsonify(result)

    @app.get('/api/export')
    def export():
        target=server.chat_id(request.args.get('target')); server.rule_for(server.read_config(config_path),target)
        db=server.open_database(server.database_path(data,target))
        try: content=''.join(f'{r[0]}\n' for r in db.execute('SELECT uid FROM whitelist WHERE target=? ORDER BY uid',(target,)))
        finally: db.close()
        return send_file(io.BytesIO(content.encode()),as_attachment=True,download_name=f'whitelist-{target}.txt',mimetype='text/plain')

    @app.get('/api/logs')
    def logs():
        try:
            with (data/'guard.log').open('rb') as f:
                f.seek(0,2); f.seek(max(0,f.tell()-60000)); text=f.read().decode('utf-8',errors='replace')
        except OSError: text='暂无日志'
        return jsonify(text=text)

    @app.get('/api/backup/<ident>')
    def backup_download(ident):
        if not re.fullmatch('[0-9a-f]{32}',ident): abort(404)
        folder=data/'backups'/ident
        if not folder.is_dir(): abort(404)
        buffer=io.BytesIO()
        with tarfile.open(fileobj=buffer,mode='w:gz') as tar:
            for p in folder.iterdir():
                if p.is_file() and not p.is_symlink(): tar.add(p,arcname=p.name)
        buffer.seek(0)
        return send_file(buffer,as_attachment=True,download_name=f'tgmemberguard-backup-{ident}.tar.gz',mimetype='application/gzip')

    @app.post('/api/account')
    def account():
        payload=request.get_json(force=True); auth=read_auth()
        if not check_password_hash(auth['password_hash'],str(payload.get('current_password',''))):
            return jsonify(error='当前密码错误'),400
        write_auth(data,str(payload.get('username',auth['username'])),str(payload.get('password','')),auth['secret'])
        session.clear()
        return jsonify(ok=True,message='账号已更新，请重新登录')

    return app
