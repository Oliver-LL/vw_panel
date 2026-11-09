"""Native configuration, diagnostics, remote setup, recovery and scoped removal."""
import configparser
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from urllib.parse import urlsplit

EXTRA_TASKS = {'info': '访问信息与系统状态', 'doctor': '环境体检', 'backup-diagnose': '备份诊断',
    'proxy-logs': '查看代理日志', 'mail-test': '发送备份测试邮件', 'restore': '恢复备份',
    'remove-services': '卸载服务', 'remote-providers': '读取云盘类型', 'remote-wizard': '配置远程存储',
    'remote-continue': '继续远程存储配置', 'remote-check': '验证远程目标', 'remote-delete': '删除远程配置',
    'remote-about': '远程容量与身份', 'remote-list': '浏览远程备份', 'remote-download': '下载远程备份',
    'upgrade-backup': '升级备份服务'}
BOOL_KEYS = ('WEBSOCKET_ENABLED', 'WEB_VAULT_ENABLED', 'EMERGENCY_ACCESS_ALLOWED', 'ENABLE_DB_WAL')
NUM_KEYS = ('ADMIN_RATELIMIT_MAX_BURST', 'ADMIN_RATELIMIT_SECONDS', 'ADMIN_SESSION_LIFETIME')


def parser_for(path):
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path)
    return parser


def safe_relative(name):
    if not isinstance(name, str) or not name or name.startswith('/') or '\\' in name or any(ord(c) < 32 for c in name) or '..' in Path(name).parts:
        raise ValueError('文件路径不正确。')
    return name


class OperationsMixin:
    def emit(self, value):
        with self.mutex:
            self.logfile.write((str(value) + '\n').encode())
            self.logfile.flush()

    def extended_config(self):
        from manager import read_vars
        app, backup = self.paths()
        env, bk = read_vars(app / 'vaultwarden.env'), read_vars(backup / 'backup.env')
        remotes = parser_for(backup / 'vw-backup-config/rclone/rclone.conf')
        targets = []
        index = 0
        while True:
            suffix = '' if index == 0 else '_' + str(index)
            name = bk.get('RCLONE_REMOTE_NAME' + suffix)
            if not name:
                break
            targets.append({'name': name, 'directory': bk.get('RCLONE_REMOTE_DIR' + suffix, '/VaultBackup')})
            index += 1
        smtp = {}
        try:
            tokens = shlex.split(bk.get('MAIL_SMTP_VARIABLES', ''))
            for i, token in enumerate(tokens[:-1]):
                if token == '-S' and '=' in tokens[i+1]:
                    k, v = tokens[i+1].split('=', 1)
                    smtp[k] = v
        except ValueError:
            pass
        uri = urlsplit(smtp.get('smtp', ''))
        return {'advanced': {k: env.get(k, '') for k in BOOL_KEYS + NUM_KEYS + ('TZ',)},
            'push': {'enabled': env.get('PUSH_ENABLED') == 'true', 'id': env.get('PUSH_INSTALLATION_ID', ''),
                     'key_set': bool(env.get('PUSH_INSTALLATION_KEY'))},
            'notifications': {k: bk.get(k, '') for k in ('MAIL_SMTP_ENABLE', 'MAIL_TO', 'MAIL_WHEN_SUCCESS', 'MAIL_WHEN_FAILURE', 'MAIL_FORCE_THREAD')},
            'notification_smtp': {'host': uri.hostname or '', 'port': uri.port or 587,
                'security': 'ssl' if uri.scheme == 'smtps' else 'starttls', 'user': smtp.get('smtp-auth-user', ''),
                'from': smtp.get('from', ''), 'password_set': bool(smtp.get('smtp-auth-password'))},
            'remotes': [{'name': name, 'type': remotes[name].get('type', ''),
                'fields': {k: ('[已隐藏]' if any(word in k.lower() for word in ('pass','token','secret','key','cookie')) else v)
                           for k,v in remotes[name].items()}} for name in remotes.sections()], 'targets': targets}

    def save_extended(self, section, data):
        from manager import read_vars, update_vars, write_private, text, number, secret_text
        app, backup = self.paths()
        env_file, bk_file = app / 'vaultwarden.env', backup / 'backup.env'
        if not env_file.exists():
            raise ValueError('请先保存密码库配置。')
        if section == 'advanced':
            updates = {}
            for key in BOOL_KEYS:
                if key in data:
                    if not isinstance(data[key], bool):
                        raise ValueError('开关值不正确。')
                    updates[key] = str(data[key]).lower()
            for key in NUM_KEYS:
                if key in data:
                    updates[key] = number(data[key], key, 1, 100000)
            timezone = text(data.get('TZ', 'Asia/Shanghai'), '时区')
            if not re.fullmatch(r'[A-Za-z_]+(?:/[A-Za-z0-9_+\-]+)+|UTC', timezone):
                raise ValueError('时区格式不正确。')
            updates['TZ'] = timezone
            update_vars(env_file, updates)
        elif section == 'push':
            enabled = data.get('enabled')
            if not isinstance(enabled, bool):
                raise ValueError('推送开关不正确。')
            if enabled:
                identifier = text(data.get('id', ''), '安装 ID')
                if not re.fullmatch(r'[A-Za-z0-9-]{8,128}', identifier):
                    raise ValueError('请填写服务商提供的安装 ID。')
                updates = {'PUSH_ENABLED': 'true', 'PUSH_INSTALLATION_ID': identifier}
                if data.get('key'):
                    updates['PUSH_INSTALLATION_KEY'] = secret_text(data['key'], '推送密钥')
                elif not read_vars(env_file).get('PUSH_INSTALLATION_KEY'):
                    raise ValueError('首次开启请填写推送密钥。')
                update_vars(env_file, updates)
            else:
                self.remove_env_keys(env_file, ('PUSH_ENABLED','PUSH_INSTALLATION_ID','PUSH_INSTALLATION_KEY'))
        elif section == 'notifications':
            if not bk_file.exists():
                raise ValueError('请先保存备份计划。')
            enabled = data.get('MAIL_SMTP_ENABLE')
            if not isinstance(enabled, bool):
                raise ValueError('通知开关不正确。')
            if not enabled:
                self.remove_env_keys(bk_file, ('MAIL_SMTP_ENABLE','MAIL_TO','MAIL_WHEN_SUCCESS','MAIL_WHEN_FAILURE','MAIL_FORCE_THREAD','MAIL_SMTP_VARIABLES'))
                return
            to = text(data.get('MAIL_TO',''), '收件邮箱')
            sender = text(data.get('from',''), '发件邮箱')
            if not all(re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', v) for v in (to,sender)):
                raise ValueError('请填写正确的收件与发件邮箱。')
            host = text(data.get('host',''), 'SMTP 主机')
            if not re.fullmatch(r'[A-Za-z0-9.-]+',host):
                raise ValueError('SMTP 主机格式不正确。')
            port = number(data.get('port'), 'SMTP 端口', 1, 65535)
            security = data.get('security')
            if security not in ('ssl','starttls','none'):
                raise ValueError('邮件加密方式不正确。')
            old = read_vars(bk_file)
            password = data.get('password','')
            if not password:
                parts = shlex.split(old.get('MAIL_SMTP_VARIABLES',''))
                for token in parts:
                    if token.startswith('smtp-auth-password='):
                        password = token.split('=',1)[1]
            password = secret_text(password, '邮箱授权码')
            user = text(data.get('user',''), '邮箱账号')
            flags = ['-S smtp-use-starttls'] if security=='starttls' else []
            options = {'smtp': ('smtps' if security=='ssl' else 'smtp')+'://'+host+':'+str(port),
                'smtp-auth':'login','smtp-auth-user':user,'smtp-auth-password':password,'from':sender}
            flags += ['-S '+k+'='+shlex.quote(v) for k,v in options.items()]
            updates = {'MAIL_SMTP_ENABLE':'true','MAIL_TO':to,'MAIL_SMTP_VARIABLES':' '.join(flags)}
            for key in ('MAIL_WHEN_SUCCESS','MAIL_WHEN_FAILURE','MAIL_FORCE_THREAD'):
                if not isinstance(data.get(key),bool):
                    raise ValueError('通知开关格式不正确。')
                updates[key] = str(data[key]).lower()
            update_vars(bk_file,updates)
        elif section == 'targets':
            if not bk_file.exists():
                raise ValueError('请先保存备份计划。')
            targets = data.get('targets')
            if not isinstance(targets,list) or not 1<=len(targets)<=20:
                raise ValueError('请选择 1～20 个备份目标。')
            names = parser_for(backup/'vw-backup-config/rclone/rclone.conf').sections()
            updates={}
            for i,item in enumerate(targets):
                if not isinstance(item,dict) or item.get('name') not in names:
                    raise ValueError('请选择已配置的远程存储。')
                directory=text(item.get('directory',''),'远程目录')
                if not directory or '..' in Path(directory).parts:
                    raise ValueError('远程目录不正确。')
                suffix='' if i==0 else '_'+str(i)
                updates['RCLONE_REMOTE_NAME'+suffix]=item['name']
                updates['RCLONE_REMOTE_DIR'+suffix]=directory
            self.remove_env_keys(bk_file,[k for k in read_vars(bk_file) if re.fullmatch(r'RCLONE_REMOTE_(NAME|DIR)(_[0-9]+)?',k)])
            update_vars(bk_file,updates)
        else:
            raise ValueError('未知配置页面。')

    def remove_env_keys(self,path,keys):
        from manager import write_private
        original=path.read_text()
        write_private(path.with_name(path.name+'.panel-backup'),original)
        write_private(path,'\n'.join(line for line in original.splitlines() if line.split('=',1)[0] not in keys)+'\n')

    def prepare_extended(self, action, data):
        from manager import text, secret_text
        payload = dict(data)
        if action in ('restore','remove-services','remote-delete'):
            expected = {'restore':'RESTORE','remove-services':'DELETE','remote-delete':'DELETE'}[action]
            if data.get('confirmation') != expected:
                raise ValueError('请填写 '+expected+' 以确认操作。')
        if action in ('restore','remote-download'):
            payload['file']=safe_relative(data.get('file'))
            if not payload['file'].lower().endswith(('.zip','.7z')):
                raise ValueError('请选择 zip 或 7z 备份。')
        if action == 'restore':
            value=data.get('password','');text(value,'备份密码',1024);payload['password']=value
        if action == 'proxy-logs' and data.get('proxy_id'):
            if not isinstance(data['proxy_id'],str) or not re.fullmatch(r'(?:[a-f0-9]{12,64}|host:(?:nginx|caddy|traefik|haproxy|apache2|httpd))',data['proxy_id']):
                raise ValueError('请选择有效的反代日志来源。')
        if action == 'remove-services':
            if data.get('scope') not in ('core','backup','all') or not isinstance(data.get('delete_data'),bool):
                raise ValueError('请选择卸载范围与数据处理方式。')
            if not isinstance(data.get('remove_proxy', False), bool) or (data.get('remove_proxy') and data.get('scope')=='backup'):
                raise ValueError('卸载备份时不能移除反代。')
        if action.startswith('remote-') and action not in ('remote-providers','remote-continue'):
            name=data.get('name','')
            if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',name):
                raise ValueError('远程名称仅支持字母、数字、下划线和短横线。')
            payload['name']=name
        if action=='remote-wizard':
            kind=text(data.get('type',''),'云盘类型')
            if not re.fullmatch(r'[a-z][a-z0-9_]{0,40}',kind):
                raise ValueError('请选择存储类型。')
            payload['type']=kind
        if action in ('remote-check','remote-list','remote-download'):
            payload['directory']=text(data.get('directory','/VaultBackup'),'远程目录')
            if '..' in Path(payload['directory']).parts:
                raise ValueError('远程目录不正确。')
        return payload

    def compose(self, folder, *args):
        return ['docker','compose','--project-directory',str(folder),'-f',str(folder/'docker-compose.yml'),*args]

    def archive_path(self,name):
        _,backup=self.paths()
        folder=(backup/'backups').resolve()
        path=folder/safe_relative(name)
        if path.is_symlink() or not path.is_file() or folder not in path.resolve().parents:
            raise ValueError('备份文件不存在或路径不正确。')
        return path

    def rclone(self,command,secret=True,config_dir=None):
        _,backup=self.paths()
        folder=config_dir or backup/'vw-backup-config'
        return self._command(['docker','run','--rm','-v',str(folder)+':/config','--entrypoint','rclone',
            'ttionya/vaultwarden-backup:latest',*command],secret=secret)

    def rc(self,path,data,folder):
        # Loopback mode runs entirely inside this disposable container. No RC port is exposed.
        script='payload=$(cat); exec rclone rc --loopback --config /config/rclone/rclone.conf "$1" --json "$payload"'
        raw=self._command(['docker','run','--rm','-i','-v',str(folder)+':/config',
            '--entrypoint','sh','ttionya/vaultwarden-backup:latest','-c',script,'rc',path],
            json.dumps(data),secret=True)
        return json.loads(raw)

    def run_extended(self,action,data):
        from manager import read_vars,write_private,update_vars
        app,backup=self.paths()
        if action in ('info','doctor','backup-diagnose'):
            self.emit('密码库目录：'+str(app))
            self.emit('密码库地址：'+read_vars(app/'vaultwarden.env').get('DOMAIN','未配置'))
            usage=shutil.disk_usage(app if app.exists() else self.state)
            self.emit('磁盘剩余：%.2f GiB'%(usage.free/1024**3))
            self.emit('本机备份数量：'+str(len(self.files())))
            for command in (['docker','version'],['docker','compose','version'],['docker','ps','-a','--format','{{.Names}} | {{.State}} | {{.Status}} | {{.Ports}}']):
                try:self._command(command)
                except (ValueError,OSError) as error:self.emit('检查未通过：'+str(error))
            if action=='backup-diagnose':
                for item in self.extended_config()['targets']:
                    try:
                        raw=self.rclone(['lsf',item['name']+':'+item['directory'],'--max-depth','1'])
                        self.emit('远程 '+item['name']+' 可读取；文件项：'+str(len(raw.splitlines())))
                    except (ValueError,OSError):self.emit('远程 '+item['name']+' 验证失败，请检查授权和目录。')
                self.emit('备份容器日志：')
                self._command(['docker','logs','--tail','80','vaultwarden-backup'])
            elif action=='doctor':
                for port in (80,443):
                    import socket
                    with socket.socket() as sock:
                        self.emit('本机端口 %s：%s'%(port,'有服务响应' if sock.connect_ex(('127.0.0.1',port))==0 else '无服务响应'))
                self.emit('请确认域名 DNS 指向服务器、HTTPS 已签发；端口响应不代表证书有效。')
            return
        if action=='proxy-logs':
            from proxy_environment import detect_environment, log_sources, HOST_SERVICES
            memory=read_vars(self.memory)
            sources=log_sources(detect_environment(),memory,(app/'docker-compose.yml').exists())
            requested=data.get('proxy_id','')
            if requested:sources=[p for p in sources if p['id']==requested]
            if not sources:raise ValueError('未检测到可读取的反代日志。请先部署反代，或重新检测服务器环境。')
            if len(sources)>1:raise ValueError('检测到多个反代，请在诊断页面选择要查看的服务。')
            source=sources[0]
            self.emit('当前日志来源：'+source['name']+'（'+source['kind']+'）')
            if source.get('source')=='host':
                name=source['name']
                if name not in HOST_SERVICES:raise ValueError('不支持的宿主机日志来源。')
                files={'nginx':['/var/log/nginx/error.log','/var/log/nginx/access.log'],
                       'apache2':['/var/log/apache2/error.log'],'httpd':['/var/log/httpd/error_log']}.get(name,[])
                readable=[path for path in files if Path(path).is_file()]
                if readable:
                    for path in readable:self._command(['tail','-n','150',path])
                else:self._command(['journalctl','-u',name,'--no-pager','-n','150'])
            else:
                container=source['id']
                if not re.fullmatch(r'[a-f0-9]{12,64}',container):raise ValueError('反代容器信息无效，请重新检测。')
                self._command(['docker','logs','--tail','150',container])
                if source['name']=='vw-panel-nginx':
                    try:self._command(['docker','logs','--tail','100','vw-panel-certbot'])
                    except ValueError:self.emit('续期服务尚未就绪，请先完成首次证书申请。')
            return
        if action=='upgrade-backup':
            self._command(self.compose(backup,'pull'))
            self._command(self.compose(backup,'up','-d'));return
        if action=='mail-test':
            env=read_vars(backup/'backup.env')
            if env.get('MAIL_SMTP_ENABLE')!='true':raise ValueError('请先启用并保存备份邮件通知。')
            with tempfile.TemporaryDirectory(dir=self.state) as folder:
                file=Path(folder)/'backup.env'
                write_private(file,'\n'.join(k+'='+v for k,v in env.items())+'\n')
                script='printf "%s\\n" "Vaultwarden backup test message." | sh -c "exec s-nail $MAIL_SMTP_VARIABLES -s \\"Vaultwarden Backup Test\\" \\"$MAIL_TO\\""'
                self._command(['docker','run','--rm','--env-file',str(file),'--entrypoint','sh',
                    'ttionya/vaultwarden-backup:latest','-ec',script],secret=True)
            self.emit('测试邮件已发送，请查看收件箱和垃圾邮件。');return
        if action=='remove-services':
            self.remove_services(data);return
        if action=='restore':
            self.restore_archive(data);return
        remote_file=backup/'vw-backup-config/rclone/rclone.conf'
        if action=='remote-providers':
            value=self.rc('config/providers',{},backup/'vw-backup-config')
            self.job['result']={'providers':[{'name':p['Name'],'description':p.get('Description',p['Name'])} for p in value['providers']]};return
        if action in ('remote-wizard','remote-continue'):
            self.remote_question(action,data);return
        parser=parser_for(remote_file)
        name=data['name']
        if name not in parser:raise ValueError('指定的远程配置不存在。')
        if action=='remote-delete':
            if any(t['name']==name for t in self.extended_config()['targets']):
                raise ValueError('此存储仍是备份目标，请先修改备份目标再删除。')
            write_private(remote_file.with_name('rclone.conf.panel-backup'),remote_file.read_text())
            self.rc('config/delete',{'name':name},backup/'vw-backup-config')
            self.emit('远程配置已移除；远程文件未删除。');return
        if action=='remote-about':
            for command in ('about','config userinfo'):
                try:
                    args=['config','userinfo',name+':'] if command=='config userinfo' else ['about',name+':','--json']
                    result=self.rclone(args)
                    self.emit(command+'：'+result)
                except ValueError:self.emit(command+'：服务商不支持该查询或授权已失效。')
            return
        target=name+':'+data['directory'].rstrip('/')
        if action=='remote-check':
            self.rclone(['mkdir',target]);self.rclone(['lsd',target]);self.emit('远程目录可写且可读取。');return
        if action=='remote-list':
            raw=self.rclone(['lsjson',target,'--files-only','--max-depth','1'])
            rows=json.loads(raw)
            self.job['result']={'remote_files':[{'name':r['Path'],'size':r['Size'],'modified':r.get('ModTime','')} for r in rows if r['Path'].lower().endswith(('.zip','.7z'))][:200]};return
        if action=='remote-download':
            safe_relative(data['file'])
            folder=backup/'backups'/'downloaded';folder.mkdir(parents=True,exist_ok=True)
            file=folder/(secrets.token_hex(4)+'-'+Path(data['file']).name)
            self._command(['docker','run','--rm','-v',str(backup/'vw-backup-config')+':/config:ro',
                '-v',str(folder)+':/download','--entrypoint','rclone','ttionya/vaultwarden-backup:latest','copyto',
                target+'/'+data['file'],'/download/'+file.name],secret=True)
            self.emit('已下载到本机：downloaded/'+file.name+'。可在恢复页面选择此文件。');return
        raise ValueError('未知操作。')

    def remote_question(self,action,data):
        from manager import write_private
        _,backup=self.paths()
        base=backup/'vw-backup-config'
        config=base/'rclone/rclone.conf'
        if action=='remote-wizard':
            if not (backup/'backup.env').exists():raise ValueError('请先保存备份计划。')
            old=getattr(self,'remote_wizard',None)
            if old:shutil.rmtree(old['folder'],ignore_errors=True)
            folder=Path(tempfile.mkdtemp(prefix='remote-wizard-',dir=self.state))
            (folder/'rclone').mkdir()
            original=config.read_text() if config.exists() else ''
            write_private(folder/'rclone/rclone.conf',original)
            self.remote_wizard={'id':secrets.token_hex(12),'folder':str(folder),'name':data['name'],'type':data['type'],'original':original,'created':time.time()}
            value=self.rc('config/create',{'name':data['name'],'type':data['type'],'parameters':{'config_is_local':'false'},
                'opt':{'nonInteractive':True,'all':True,'obscure':True}},folder)
        else:
            wizard=getattr(self,'remote_wizard',None)
            if not wizard or wizard['id']!=data.get('wizard_id') or time.time()-wizard['created']>3600:
                raise ValueError('配置向导已过期，请重新开始。')
            value=self.rc('config/update',{'name':wizard['name'],'parameters':{'config_is_local':'false'},
                'opt':{'nonInteractive':True,'continue':True,'state':wizard['state'],
                'result':data.get('answer',''),'obscure':True}},Path(wizard['folder']))
        wizard=self.remote_wizard
        state=value.get('State',value.get('state',''))
        wizard['state']=state
        if state:
            option=value.get('Option',value.get('option',{}))
            # Never echo secret defaults returned by Rclone.
            if option.get('IsPassword') or re.search(r'pass|token|secret|key',option.get('Name',''),re.I):
                option['Default']='';option.pop('Value',None)
            self.job['result']={'wizard_id':wizard['id'],'question':option,'error':value.get('Error','')}
            self.emit('请在远程存储页面继续填写配置问题。')
        else:
            original=config.read_text() if config.exists() else ''
            if original!=wizard['original']:raise ValueError('远程配置已发生变化，请重新开始，避免覆盖其他修改。')
            if config.exists():write_private(config.with_name('rclone.conf.panel-backup'),original)
            write_private(config,(Path(wizard['folder'])/'rclone/rclone.conf').read_text())
            self.job['result']={'wizard_id':wizard['id'],'done':True}
            self.emit('远程存储配置已保存。请验证连接，然后将其选为备份目标。')
            shutil.rmtree(wizard['folder']);self.remote_wizard=None

    def restore_archive(self,data):
        from manager import read_vars
        app,backup=self.paths()
        archive=self.archive_path(data['file'])
        if not (app/'docker-compose.yml').exists():raise ValueError('请先配置主服务。')
        live=app/'data'
        if live.is_symlink():raise ValueError('数据目录是符号链接，无法自动恢复。')
        if shutil.disk_usage(app).free<max(archive.stat().st_size*4,512*1024*1024):
            raise ValueError('磁盘剩余空间不足，无法安全解压和保留旧数据。')
        staging=Path(tempfile.mkdtemp(prefix='.restore-',dir=app))
        old=app/('data.before-restore-'+time.strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(3))
        moved=False
        try:
            listing=self._command(['docker','run','--rm','-i','--network','none','--cap-drop','ALL',
                '-v',str(archive)+':/input/archive:ro','--entrypoint','sh','ttionya/vaultwarden-backup:latest',
                '-c','IFS= read -r password; exec 7z l -slt -p"$password" /input/archive'],data['password']+'\n',secret=True)
            unpacked=sum(int(v) for v in re.findall(r'^Size = ([0-9]+)$',listing,re.M))
            if len(listing)>=4*1024*1024 or unpacked*2+512*1024**2>shutil.disk_usage(app).free:
                raise ValueError('备份解压后的预计大小超过可用空间，原数据未修改。')
            # Only these two directories are mounted. Credentials travel through stdin.
            script='IFS= read -r password; exec 7z x -y -p"$password" -o/output /input/archive'
            self._command(['docker','run','--rm','-i','--network','none','--cap-drop','ALL',
                '-v',str(archive)+':/input/archive:ro','-v',str(staging)+':/output',
                '--entrypoint','sh','ttionya/vaultwarden-backup:latest','-c',script],data['password']+'\n',secret=True)
            if any(p.is_symlink() for p in staging.rglob('*')):raise ValueError('备份包含链接，已拒绝恢复。')
            # Backup images may place attachments/sends/RSA material in nested tar files.
            for nested in list(staging.rglob('*.tar'))+list(staging.rglob('*.tar.gz')):
                if nested.is_symlink():raise ValueError('备份包含不支持的链接。')
                with tarfile.open(nested) as tar:
                    for member in tar:
                        raw=member.name
                        while raw.startswith('./'):raw=raw[2:]
                        if not raw or raw=='.':continue
                        name=safe_relative(raw)
                        target=nested.parent/name
                        if staging.resolve() not in target.resolve().parents:raise ValueError('备份文件超出解压目录。')
                        if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
                            raise ValueError('备份包含不支持的特殊文件。')
                        if member.size+512*1024**2>shutil.disk_usage(app).free:raise ValueError('解压空间不足。')
                        if member.isdir():target.mkdir(parents=True,exist_ok=True)
                        else:
                            target.parent.mkdir(parents=True,exist_ok=True)
                            with tar.extractfile(member) as src, target.open('wb') as dst:shutil.copyfileobj(src,dst)
                nested.unlink()
            if any(p.is_symlink() for p in staging.rglob('*')):raise ValueError('备份包含链接，已拒绝恢复。')
            databases=sorted(list(staging.rglob('db.sqlite3'))+list(staging.rglob('db.*.sqlite3')),key=lambda p:len(p.parts))
            if not databases:raise ValueError('备份内未找到 db.sqlite3，原数据未修改。')
            database=databases[0]
            with sqlite3.connect('file:'+str(database)+'?mode=ro',uri=True) as conn:
                if conn.execute('pragma quick_check').fetchone()[0]!='ok':raise ValueError('备份数据库校验失败。')
            source=database.parent
            if database.name!='db.sqlite3':database.rename(source/'db.sqlite3')
            if not (source/'config.json').exists():
                configs=sorted(source.glob('config.*.json'))
                if configs:configs[-1].rename(source/'config.json')
            restored_size=sum(p.stat().st_size for p in source.rglob('*') if p.is_file())
            if restored_size+512*1024**2>shutil.disk_usage(app).free:raise ValueError('没有足够空间复制恢复数据。')
            running=subprocess.run(['docker','inspect','--format','{{.State.Running}}','vaultwarden-backup'],capture_output=True,text=True,timeout=10)
            backup_was_running=running.returncode==0 and running.stdout.strip()=='true'
            stopped=False; backup_stopped=False; replacing=False
            try:
                if backup_was_running:
                    self._command(['docker','stop','vaultwarden-backup']);backup_stopped=True
                self._command(['docker','stop','vaultwarden']);stopped=True
                if live.exists():live.rename(old);moved=True
                replacing=True
                shutil.copytree(source,live)
                self._command(self.compose(app,'up','-d'))
                result=self._command(['docker','inspect','--format','{{.State.Running}}','vaultwarden'])
                if result.strip()!='true':raise ValueError('恢复后容器未保持运行。')
            except Exception:
                if replacing:
                    self._command(['docker','stop','vaultwarden'])
                    if live.exists():live.rename(app/('data.failed-restore-'+secrets.token_hex(5)))
                    if moved:old.rename(live)
                if stopped:self._command(self.compose(app,'up','-d'))
                raise
            finally:
                if backup_stopped:self._command(['docker','start','vaultwarden-backup'])
            self.emit('恢复完成。恢复前数据保留在：'+str(old) if moved else '恢复完成。')
        finally:
            shutil.rmtree(staging,ignore_errors=True)

    def remove_services(self,data):
        app,backup=self.paths()
        scope=data['scope'];delete=data['delete_data']
        managed_proxy=self.proxy_folder() if data.get('remove_proxy') else None
        if data.get('remove_proxy') and not managed_proxy:raise ValueError('没有面板安装的 NPM 或 Nginx 可移除。')
        # Explicit roots only. Never remove shared Docker networks or other projects.
        root=self.root()
        if not root.is_absolute() or len(root.parts)<3 or root.is_symlink():
            raise ValueError('部署目录不满足安全卸载条件。')
        for folder in ([backup] if scope=='backup' else [backup,app] if scope=='all' else [app]):
            if folder.is_symlink():raise ValueError('服务目录是符号链接，已拒绝自动卸载。')
            if (folder/'docker-compose.yml').exists():self._command(self.compose(folder,'down'))
            if delete and folder.exists():shutil.rmtree(folder)
        if scope=='core' and (backup/'docker-compose.yml').exists():
            self._command(self.compose(backup,'stop'))
        if scope in ('core','all'):
            from manager import read_vars
            memory=read_vars(self.memory)
            caddy=Path(memory.get('CADDY_DIR','/nonexistent'))
            if memory.get('CADDY_EXISTS')=='false' and caddy.is_relative_to(root) and not caddy.is_symlink() and (caddy/'docker-compose.yml').exists():
                # Shared Caddy configuration must be preserved; only a one-site generated file is eligible.
                contents=(caddy/'Caddyfile').read_text() if (caddy/'Caddyfile').exists() else ''
                if contents.count('reverse_proxy')==1 and contents.count('{')==1:
                    self._command(self.compose(caddy,'down'))
                    if delete:shutil.rmtree(caddy)
        if managed_proxy:
            self._command(self.compose(managed_proxy,'down'))
            if delete:
                shutil.rmtree(managed_proxy)
                from manager import update_vars
                update_vars(self.memory,{'PANEL_PROXY_KIND':'','PANEL_PROXY_DIR':''})
        elif self.proxy_folder():
            self.emit('已保留面板安装的反代，避免影响其其他站点。')
        self.emit('服务已卸载。'+('已删除所选服务的数据目录。' if delete else '已保留全部配置、数据库和备份文件，可重新部署。'))
