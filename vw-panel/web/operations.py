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
from storage_presets import PRESETS, config_lines, normalize, public_presets, target_dir, verify_commands, verify_target

EXTRA_TASKS = {'info': '访问信息与系统状态', 'doctor': '环境体检', 'backup-diagnose': '备份诊断',
    'proxy-logs': '查看代理日志', 'mail-test': '发送备份测试邮件', 'restore': '恢复备份',
    'remove-services': '卸载服务', 'remote-providers': '读取云盘类型', 'remote-wizard': '配置远程存储',
    'remote-continue': '继续远程存储配置','backup-target-save':'保存备份目标','backup-target-toggle':'切换备份目标','remote-oauth-start':'生成云盘授权链接','remote-oauth-complete':'完成云盘授权', 'remote-check': '验证远程目标', 'remote-delete': '删除远程配置',
    'remote-about': '远程容量与身份', 'remote-list': '浏览远程备份', 'remote-download': '下载远程备份', 'remote-file-delete': '删除远程备份',
    'remote-r2-save': '保存 Cloudflare R2 存储', 'remote-preset-save': '保存远程存储预设',
    'upgrade-backup': '升级备份服务', 'existing-npm': '配置现有 NPM 站点'}
BOOL_KEYS = ('ENABLE_WEBSOCKET', 'WEB_VAULT_ENABLED', 'EMERGENCY_ACCESS_ALLOWED', 'ENABLE_DB_WAL')
NUM_KEYS = ('ADMIN_RATELIMIT_MAX_BURST', 'ADMIN_RATELIMIT_SECONDS', 'ADMIN_SESSION_LIFETIME','LOGIN_RATELIMIT_MAX_BURST','LOGIN_RATELIMIT_SECONDS')


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
        preset_targets = self.preset_targets()
        env, bk = read_vars(app / 'vaultwarden.env'), read_vars(backup / 'backup.env')
        remotes = parser_for(backup / 'vw-backup-config/rclone/rclone.conf')
        from backup_targets import read as read_targets
        targets=read_targets(self)
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
        from environment import public_environment, sensitive
        advanced={k:env.get(k,'') for k in BOOL_KEYS+NUM_KEYS+('TZ',)}
        advanced['ENABLE_WEBSOCKET']=env.get('ENABLE_WEBSOCKET',env.get('WEBSOCKET_ENABLED',''))
        return {'environment':public_environment(self,env,bk),'advanced':advanced,
            'push': {'enabled': env.get('PUSH_ENABLED') == 'true', 'id': env.get('PUSH_INSTALLATION_ID', ''),
                     'key_set': bool(env.get('PUSH_INSTALLATION_KEY'))},
            'notifications': {k: bk.get(k, '') for k in ('MAIL_SMTP_ENABLE', 'MAIL_TO', 'MAIL_WHEN_SUCCESS', 'MAIL_WHEN_FAILURE', 'MAIL_FORCE_THREAD')},
            'notification_smtp': {'host': uri.hostname or '', 'port': uri.port or 587,
                'security': 'ssl' if uri.scheme == 'smtps' else 'starttls', 'user': smtp.get('smtp-auth-user', ''),
                'from': smtp.get('from', ''), 'password_set': bool(smtp.get('smtp-auth-password'))},
            'remotes': [{'name': name, 'type': remotes[name].get('type', ''),
                'target': preset_targets.get(name, ''),
                'fields': {k: ('[已隐藏]' if any(word in k.lower() for word in ('pass','token','secret','key','cookie')) or sensitive(k, v) else v)
                           for k,v in remotes[name].items()}} for name in remotes.sections()], 'targets': targets,
            'storage_presets': public_presets()}

    def save_extended(self, section, data):
        from manager import read_vars, update_vars, write_private, text, number, secret_text
        app, backup = self.paths()
        env_file, bk_file = app / 'vaultwarden.env', backup / 'backup.env'
        if not env_file.exists():
            raise ValueError('请先保存密码库配置。')
        if section == 'advanced':
            updates = {}
            if 'WEBSOCKET_ENABLED' in data and 'ENABLE_WEBSOCKET' not in data:data={**data,'ENABLE_WEBSOCKET':data['WEBSOCKET_ENABLED']}
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
            from backup_targets import save
            save(self,data.get('targets'))
        else:
            raise ValueError('未知配置页面。')

    def remove_env_keys(self,path,keys):
        from manager import write_private
        original=path.read_text()
        write_private(path.with_name(path.name+'.panel-backup'),original)
        write_private(path,'\n'.join(line for line in original.splitlines() if line.split('=',1)[0] not in keys)+'\n')

    def remote_config(self,name):
        if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}',name) or name.upper()=='DEFAULT':raise ValueError('请选择有效的存储名称。')
        _,backup=self.paths();file=backup/'vw-backup-config/rclone/rclone.conf'
        if not file.exists():raise ValueError('尚未配置存储连接。')
        if file.stat().st_size>4*1024*1024:raise ValueError('存储配置过大，无法在页面读取。')
        try:parser=parser_for(file)
        except configparser.Error:raise ValueError('存储配置格式无法识别，请检查 Rclone 配置。') from None
        if name not in parser:raise ValueError('该存储已不存在，请刷新后重试。')
        return {'name':name,'fields':dict(parser[name])}

    def prepare_extended(self, action, data):
        from manager import text, secret_text
        payload = dict(data)
        if action in ('backup-target-save','backup-target-toggle'):
            from backup_targets import read,normalize as normalize_targets
            targets=data.get('targets')
            if action=='backup-target-toggle':
                if not isinstance(data.get('enabled'),bool):raise ValueError('目标开关不正确。')
                targets=[dict(row) for row in read(self)];found=False
                for row in targets:
                    if row['id']==data.get('id'):row['enabled']=data['enabled'];found=True
                if not found:raise ValueError('备份目标已变化，请刷新后重试。')
            return {'targets':normalize_targets(self,targets)}
        if action in ('remote-oauth-start','remote-oauth-complete'):
            from browser_oauth import wizard
            current=wizard(self,data.get('wizard_id'))
            return {'wizard_id':current['id'],'callback_url':text(data.get('callback_url',''),'授权返回地址',20000)}
        if action=='existing-npm':
            target=data.get('target','')
            if not isinstance(target,str) or not re.fullmatch(r'[a-f0-9]{12,64}',target):raise ValueError('请选择检测到的 NPM。')
            email=text(data.get('email',''),'NPM 管理邮箱')
            if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email):raise ValueError('请输入有效的 NPM 管理邮箱。')
            return {'target':target,'email':email,'password':secret_text(data.get('password',''),'NPM 管理密码')}

        if action in ('restore','remove-services','remote-delete','remote-file-delete'):
            expected = {'restore':'RESTORE','remove-services':'DELETE','remote-delete':'DELETE','remote-file-delete':'DELETE'}[action]
            if data.get('confirmation') != expected:
                raise ValueError('请填写 '+expected+' 以确认操作。')
        if action in ('restore','remote-download','remote-file-delete'):
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
        if action in ('remote-preset-save','remote-r2-save'):
            preset_id='r2' if action=='remote-r2-save' else data.get('preset','')
            if not isinstance(preset_id,str) or preset_id not in PRESETS:
                raise ValueError('请选择存储类型。')
            name=data.get('name','')
            if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}',name):
                raise ValueError('远程名称仅支持字母、数字、下划线、短横线和点号。')
            if name.upper()=='DEFAULT':raise ValueError('远程名称不能使用 DEFAULT。')
            source=data.get('original_name','')
            if not isinstance(source,str) or source and (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}',source) or source.upper()=='DEFAULT'):
                raise ValueError('原存储名称不正确，请重新打开编辑。')
            values=normalize(preset_id,data)
            if not isinstance(data.get('set_target',False),bool):
                raise ValueError('备份目标选项格式不正确。')
            return {'preset':preset_id,'name':name,'original_name':source,'values':values,'set_target':data.get('set_target',False)}
        if action.startswith('remote-') and action not in ('remote-providers','remote-continue'):
            name=data.get('name','')
            if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}',name):
                raise ValueError('远程名称仅支持字母、数字、下划线、短横线和点号。')
            if name.upper()=='DEFAULT':raise ValueError('远程名称不能使用 DEFAULT。')
            payload['name']=name
        if action=='remote-wizard':
            if not isinstance(data.get('guided',False),bool):raise ValueError('推荐设置选项不正确。')
            if not isinstance(data.get('set_target',False),bool):raise ValueError('备份目标选项不正确。')
            directory=text(data.get('directory',''),'备份保存目录',512)
            if '..' in Path(directory).parts or any(ord(c)<32 for c in directory):raise ValueError('备份保存目录不正确。')
            if data.get('set_target') and not directory:raise ValueError('请填写备份保存目录。')
            payload['directory']=directory
            payload['set_target']=data.get('set_target',False)
            kind=text(data.get('type',''),'云盘类型')
            if not re.fullmatch(r'[a-z][a-z0-9_]{0,40}',kind):
                raise ValueError('请选择存储类型。')
            payload['type']=kind
        if action in ('remote-check','remote-list','remote-download','remote-file-delete'):
            payload['directory']=self.remote_directory(payload['name'],data.get('directory',''))
        return payload

    def remote_directory(self,name,requested=''):
        from manager import read_vars,text
        _,backup=self.paths();parser=parser_for(backup/'vw-backup-config/rclone/rclone.conf')
        if name not in parser:raise ValueError('指定的存储不存在，请刷新后重新选择。')
        directory=text(requested,'远程目录')
        if not directory:
            env=read_vars(backup/'backup.env')
            for index in range(20):
                suffix='' if index==0 else '_'+str(index)
                if env.get('RCLONE_REMOTE_NAME'+suffix)==name:
                    directory=env.get('RCLONE_REMOTE_DIR'+suffix,'');break
            directory=directory or self.preset_targets().get(name,'')
        is_s3=parser[name].get('type','').lower()=='s3'
        if not directory:
            if is_s3:raise ValueError('请填写 Bucket/目录，或先在备份设置中保存此存储的目标目录。')
            directory='/vwp/backups/archive' if parser[name].get('type','')=='local' else '/'
        if '..' in Path(directory).parts or any(ord(c)<32 for c in directory):raise ValueError('远程目录不正确。')
        return directory.lstrip('/') if is_s3 else directory

    def local_backup_mounts(self):
        app,backup=self.paths();mounts=[]
        for path,target in ((backup/'backups','/bitwarden/backup'),(backup/'backups','/vwp/backups'),(app/'data','/bitwarden/data')):
            if path.is_dir():mounts+=['-v',str(path)+':'+target+':ro']
        return mounts

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
        from remote_config import run
        return run(self,path,data,folder)

    def storage_providers(self):
        from types import SimpleNamespace
        import threading
        from remote_config import run
        if not self.storage_provider_lock.acquire(timeout=5):raise ValueError('存储类型列表正在读取，请稍后重试。')
        try:
            cached=self.storage_provider_cache
            if cached and time.time()-cached[0]<3600:return cached[1]
            self.state.mkdir(parents=True,exist_ok=True)
            # Metadata uses an empty temporary config and never changes the active task.
            reader=SimpleNamespace(mutex=threading.RLock(),job={})
            with tempfile.TemporaryDirectory(prefix='storage-types-',dir=self.state) as directory:
                folder=Path(directory);(folder/'rclone').mkdir()
                value=run(reader,'config/providers',{},folder)
            rows=value.get('providers',[])
            if not isinstance(rows,list):raise ValueError('存储类型列表无法识别，请重试。')
            providers=[{'name':row['Name'],'description':row.get('Description') or row['Name']} for row in rows if isinstance(row,dict) and isinstance(row.get('Name'),str) and re.fullmatch(r'[a-z][a-z0-9_]{0,40}',row['Name'])]
            if not providers:raise ValueError('暂时未能读取存储类型，请重试。')
            self.storage_provider_cache=(time.time(),providers)
            return providers
        finally:self.storage_provider_lock.release()

    def preset_targets(self):
        try:
            value=json.loads((self.state/'storage-targets.json').read_text())
            return value if isinstance(value,dict) else {}
        except (OSError,ValueError):
            return {}

    def remember_preset_target(self,name,directory):
        from manager import write_private
        data=self.preset_targets();data[name]=directory
        write_private(self.state/'storage-targets.json',json.dumps(data,ensure_ascii=False,indent=2)+'\n')

    def forget_preset_target(self,name):
        from manager import write_private
        data=self.preset_targets()
        if data.pop(name,None) is not None:
            write_private(self.state/'storage-targets.json',json.dumps(data,ensure_ascii=False,indent=2)+'\n')

    def rclone_probe(self,args,config_file,secrets=(),label='远程存储',hint='',download_dir=None):
        # Trial against a candidate config; surface a redacted rclone error instead of a generic failure.
        from environment import sensitive
        secrets=list(secrets)
        for section in parser_for(config_file).values():
            secrets.extend(value for key,value in section.items() if sensitive(key.upper(),value) or key.lower()=='pass')
        extra_mounts=self.local_backup_mounts()
        if download_dir is not None:extra_mounts+=['-v',str(download_dir)+':/download']
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            try:
                result=subprocess.run(['docker','run','--rm',
                    '-v',str(config_file)+':/config/rclone/rclone.conf:ro',*extra_mounts,
                    '--entrypoint','rclone','ttionya/vaultwarden-backup:latest',*args],
                    stdout=output,stderr=errors,timeout=300)
            except subprocess.TimeoutExpired:
                raise ValueError(label+' 连接超时，请检查服务器网络后重试。')
            output.seek(0);errors.seek(0)
            if result.returncode:
                message=errors.read(8000).decode('utf-8','replace')
                for value in secrets:
                    if value:message=message.replace(value,'[已隐藏]')
                message=re.sub(r'(?i)\b(pass|password|token|secret(?:_access_key)?|access_key_id|key|cookie)\s*=\s*\S+',r'\1 = [已隐藏]',message)
                message=re.sub(r'(?i)([?&](?:token|key|secret|password)=)[^&\s]+',r'\1[已隐藏]',message)
                message=re.sub(r'(://)[^/\s@]+@',r'\1[已隐藏]@',message)
                message=re.sub(r'(?i)(could not parse line:).*',r'\1 [已隐藏]',message)
                message=re.sub(r'\x1b\[[0-9;]*m','',message)
                lines=[line.strip() for line in message.splitlines() if line.strip()]
                if self.logfile:self.emit(label+' 连接验证详情：\n'+'\n'.join(lines))
                if 'SignatureDoesNotMatch' in message:
                    summary='签名校验失败（403 SignatureDoesNotMatch）。请重新填写配对的 Access Key ID 和原始 Secret Access Key，不要填写 API Token 字符串。'
                elif 'InvalidAccessKeyId' in message:
                    summary='Access Key ID 无效，请检查账号和这组访问密钥是否已被撤销。'
                elif 'AccessDenied' in message:
                    summary='访问被拒绝（403 AccessDenied），请检查目标 Bucket 和这组密钥的对象读写权限。'
                elif re.search(r'directory not found|object not found|NoSuchKey',message,re.I):
                    summary='备份目录尚不存在，请先执行一次备份，或检查保存目录。'
                elif 'NoSuchBucket' in message:
                    summary='找不到 Bucket，请确认桶名、所属账号和 Endpoint。'
                else:
                    summary='请求未完成，请查看失败详情。'+((' '+hint) if hint else '')
                raise ValueError(label+' '+summary)
            return output.read().decode('utf-8','replace')

    def run_extended(self,action,data):
        from manager import read_vars,write_private,update_vars
        app,backup=self.paths()
        if action in ('backup-target-save','backup-target-toggle'):
            from backup_targets import apply
            apply(self,data['targets']);return
        if action in ('remote-oauth-start','remote-oauth-complete'):
            import browser_oauth
            if action=='remote-oauth-start':browser_oauth.start(self,data['wizard_id'])
            else:browser_oauth.complete(self,data['wizard_id'],data['callback_url'])
            return
        if action=='existing-npm':
            from existing_npm import ExistingNPM
            ExistingNPM(self,data).configure();return
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
                    if not item.get('enabled',True):continue
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
                if (memory.get('PANEL_PROXY_KIND') == 'nginx' and source['name'] == 'nginx'
                        and memory.get('DOCKER_NET') in source.get('networks', [])):
                    try:self._command(['docker','logs','--tail','100','certbot'])
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
        if action in ('remote-preset-save','remote-r2-save'):
            self.save_preset_remote(data);return
        remote_file=backup/'vw-backup-config/rclone/rclone.conf'
        if action=='remote-providers':
            value=self.rc('config/providers',{},backup/'vw-backup-config')
            self.job['result']={'providers':[{'name':p['Name'],'description':p.get('Description',p['Name'])} for p in value['providers']]};return
        if action in ('remote-wizard','remote-continue'):
            if action=='remote-continue':
                from browser_oauth import cancel
                cancel(self)
            self.remote_question(action,data);return
        parser=parser_for(remote_file)
        name=data['name']
        if name not in parser:raise ValueError('指定的远程配置不存在。')
        if action=='remote-delete':
            if any(t['name']==name for t in self.extended_config()['targets']):
                raise ValueError('此存储仍是备份目标，请先修改备份目标再删除。')
            write_private(remote_file.with_name('rclone.conf.panel-backup'),remote_file.read_text())
            self.rc('config/delete',{'name':name},backup/'vw-backup-config')
            self.forget_preset_target(name)
            self.emit('远程配置已移除；远程文件未删除。');return
        if action=='remote-about':
            available=False
            for command in ('about','config userinfo'):
                try:
                    args=['config','userinfo',name+':'] if command=='config userinfo' else ['about',name+':','--json']
                    result=self.rclone(args)
                    self.emit(command+'：'+result);available=True
                except ValueError:self.emit(command+'：服务商不支持该查询或授权已失效。')
            self.job['result']={'storage_info':{'name':name,'available':available}}
            return
        directory=self.remote_directory(name,data.get('directory',''))
        target=name+':'+directory.rstrip('/')
        if action=='remote-check':
            if parser[name].get('type','')=='s3':target=name+':'+directory.split('/',1)[0]
            self.rclone_probe(['lsf',target,'--max-depth','1'],remote_file,label='存储')
            self.job['result']={'remote_validation':{'name':name,'directory':directory,'readable':True,'write_checked':False}}
            self.emit('存储可读取；本次验证未写入文件。');return
        if action=='remote-list':
            raw=self.rclone_probe(['lsjson',target,'--files-only','--max-depth','1'],remote_file,label='读取备份')
            try:
                rows=json.loads(raw)
                if not isinstance(rows,list):raise ValueError()
                files=[]
                for row in rows:
                    path=row.get('Path','')
                    if row.get('IsDir') or not path.lower().endswith(('.zip','.7z')):continue
                    safe_relative(path)
                    files.append({'name':path,'size':row['Size'],'modified':row.get('ModTime','')})
            except (ValueError,KeyError,TypeError,AttributeError):raise ValueError('存储返回的文件列表无法识别，请查看任务详情。')
            self.job['result']={'remote_files':files[:200],'remote_location':{'name':name,'directory':directory}};return
        if action=='remote-file-delete':
            if data.get('confirmation')!='DELETE':raise ValueError('请先确认删除该备份文件。')
            filename=safe_relative(data.get('file'))
            if not filename.lower().endswith(('.zip','.7z')):raise ValueError('只能删除 ZIP / 7z 备份文件。')
            if parser[name].get('type','').lower()=='local':raise ValueError('此处仅删除远程备份，不处理本机目录。')
            self.rclone_probe(['deletefile',target+'/'+filename],remote_file,label='删除远程备份')
            self.job['result']={'remote_deleted':{'name':name,'directory':directory,'file':filename}}
            self.emit('所选远程备份已删除，已下载到服务器的副本保留。');return
        if action=='remote-download':
            safe_relative(data['file'])
            folder=backup/'backups'/'downloaded';folder.mkdir(parents=True,exist_ok=True,mode=0o700)
            file=folder/(secrets.token_hex(4)+'-'+Path(data['file']).name)
            try:self.rclone_probe(['copyto',target+'/'+data['file'],'/download/'+file.name],remote_file,label='下载备份',download_dir=folder)
            except Exception:
                file.unlink(missing_ok=True);raise
            self.job['result']={'downloaded_file':'downloaded/'+file.name}
            self.emit('备份已下载到服务器，可在本机备份文件中下载或恢复。');return
        raise ValueError('未知操作。')

    def save_preset_remote(self,data):
        from manager import write_private
        preset_id=data['preset'];preset=PRESETS[preset_id];label=preset['label']
        name=data['name'];values=dict(data['values'])
        _,backup=self.paths()
        config=backup/'vw-backup-config/rclone/rclone.conf'
        original=config.read_text() if config.exists() else ''
        parser=parser_for(config)
        source=data.get('original_name') or name
        renamed=source!=name
        if data.get('original_name') and source not in parser:raise ValueError('原存储已不存在，请刷新列表后重新编辑。')
        if renamed and name in parser:raise ValueError('这个存储名称已被使用，请换一个名称。')
        if renamed:
            reference=re.compile(r'(?<![A-Za-z0-9_.-])'+re.escape(source)+r':')
            for section in parser.sections():
                if section!=source and any(reference.search(parser[section].get(key,'')) for key in ('remote','upstreams','remotes')):
                    raise ValueError('存储 '+section+' 引用了原连接，请先调整依赖后再改名。')
        existing=dict(parser[source]) if source in parser else {}
        prior_targets=[dict(item) for item in self.extended_config()['targets']]
        desired_targets=[dict(item,name=name) if renamed and item['name']==source else dict(item) for item in prior_targets]
        if existing and existing.get('type','').lower()!=preset['type'].lower():
            raise ValueError('同名存储已存在且类型不同，请更换存储名称。')
        required_provider=preset.get('requires_provider','')
        if existing and required_provider and existing.get('provider','').lower() not in ('',required_provider.lower()):
            raise ValueError('同名存储已存在且不是 '+required_provider+'，请更换存储名称。')
        if preset_id=='s3' and existing:
            current=existing.get('provider','').lower();selected=values.get('provider','').lower()
            if current and selected and current!=selected:
                raise ValueError('同名存储的服务商不同，请更换存储名称或先删除原配置。')
        if preset_id=='r2' and not values.get('account_id'):
            match=re.fullmatch(r'https://([a-fA-F0-9]{32})(\.[a-z0-9-]+)?\.r2\.cloudflarestorage\.com/?',existing.get('endpoint','').strip())
            if not match:
                raise ValueError('请填写 Cloudflare Account ID，或使用已配置的 R2 存储名称。')
            values['account_id']=match.group(1).lower()
            if not values.get('jurisdiction') and match.group(2):
                values['jurisdiction']=match.group(2).lstrip('.')
        obscured={};secrets=[]
        for field in preset['fields']:
            if not field.get('secret'):continue
            key=field['key'];option=field.get('option',key);value=values.get(key,'')
            if value and not field.get('obscure',False):
                # S3 signs with the original key; Sensitive does not mean IsPassword.
                obscured[key]=value;secrets.append(value)
            elif value:
                try:
                    secret=self._command(['docker','run','--rm','-i','ttionya/vaultwarden-backup:latest',
                        'rclone','obscure','-'],value+'\n',secret=True).strip()
                except ValueError:
                    raise ValueError(label+' Secret 处理失败，请确认备份镜像可正常运行。原配置未修改。')
                if not re.fullmatch(r'[A-Za-z0-9_+/=-]+',secret):
                    raise ValueError(label+' Secret 处理失败，原配置未修改。')
                obscured[key]=secret;secrets += [value,secret]
            else:
                secret=existing.get(option,'')
                if not secret:
                    raise ValueError('首次配置请填写'+field['label']+'。')
                obscured[key]=secret;secrets.append(secret)
        for field in preset['fields']:
            if field.get('secret') or not field.get('reuse_blank'):continue
            key=field['key'];option=field.get('option',key)
            if not values.get(key):
                current=existing.get(option,'')
                if current:
                    if field.get('pattern') and not re.fullmatch(field['pattern'],current):
                        raise ValueError(field['label']+'格式不正确。')
                    values[key]=current
                elif field.get('required'):
                    raise ValueError('首次配置请填写'+field['label']+'。')
        if preset_id in ('r2','s3') and not values.get('access_key_id'):
            raise ValueError('首次配置请填写 Access Key ID。')
        if preset_id=='r2' and not values.get('account_id'):
            raise ValueError('请填写 Cloudflare Account ID。')
        lines,managed=config_lines(preset_id,values,obscured)
        for key,value in existing.items():
            if key in managed or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',key):continue
            if any(ord(c)<32 for c in value):continue
            lines.append('%s = %s'%(key,value))
        block='[%s]\n%s\n'%(name,'\n'.join(lines))
        candidate=re.sub(r'(?ms)^\[%s\]\s*\n.*?(?=^\[|\Z)'%re.escape(source),'',original).strip('\n')
        if candidate:candidate+='\n\n'
        candidate+=block
        directory=target_dir(preset_id,values)
        if data.get('set_target') and (backup/'backup.env').exists():
            targets=desired_targets
            if not any(item['name']==name for item in targets) and len(targets)>=20:
                raise ValueError('备份目标最多 20 个，请先在备份设置中移除一个。')
        with tempfile.TemporaryDirectory(dir=self.state) as folder:
            trial=Path(folder)/'rclone.conf'
            write_private(trial,candidate)
            commands=verify_commands(preset_id,verify_target(preset_id,name,values))
            for command in commands:
                self.rclone_probe(command,trial,secrets=tuple(secrets),label=label,hint=preset.get('verify_hint',''))
            parsed=parser_for(trial)
            if name not in parsed.sections():
                raise ValueError('远程名称无法写入 Rclone 配置，请更换名称。')
            if data.get('set_target') and (backup/'backup.env').exists():
                names=set(parsed.sections())
                for item in desired_targets:
                    if item['name'] not in names:
                        raise ValueError('备份目标 %s 在 Rclone 配置中不存在，请先在备份设置中修复该目标。'%item['name'])
        latest=config.read_text() if config.exists() else ''
        if latest!=original:
            raise ValueError('远程配置在验证期间发生变化，请重新提交，避免覆盖其他修改。')
        env_file=backup/'backup.env'
        env_original=env_file.read_text() if env_file.exists() else None
        if config.exists():write_private(config.with_name('rclone.conf.panel-backup'),original)
        updated=False
        try:
            write_private(config,candidate)
            if env_original is not None and (data.get('set_target') or renamed and any(item['name']==source for item in prior_targets)):
                targets=desired_targets
                if data.get('set_target'):
                    index=next((i for i,item in enumerate(targets) if item['name']==name and item['directory']==directory),None)
                    if index is None:index=next((i for i,item in enumerate(targets) if item['name']==name and item['enabled']),None)
                    entry={'name':name,'directory':directory,'enabled':True}
                    if index is None:targets.append(entry)
                    else:targets[index]={**targets[index],**entry}
                from backup_targets import apply
                def restore_remote():
                    if original:write_private(config,original)
                    else:config.unlink(missing_ok=True)
                apply(self,targets,restore_extra=restore_remote);updated=True
        except Exception:
            try:
                if original:write_private(config,original)
                else:config.unlink(missing_ok=True)
                if env_original is not None:write_private(env_file,env_original)
            except OSError:
                pass
            raise
        try:
            self.remember_preset_target(name,directory)
            if renamed:self.forget_preset_target(source)
        except OSError:
            self.emit('存储已保存，但未能记录目录信息；下次重新配置时需要重新填写 Bucket/目录。')
        verification=('，并已通过 rclone 读取验证。' if preset_id in ('r2','s3') else '，并已通过 rclone 目录验证。') if commands else '。'
        self.emit(label+' 存储已保存'+verification)
        if preset_id=='local':self.emit('本机目录由备份容器挂载，保存时未做远程验证。')
        self.job['result']={'storage_saved':{'name':name,'renamed_from':source if renamed else '', 'references_only':renamed and not data.get('set_target'), 'label':label,'verified':bool(commands),'target_updated':updated,'target_applied':updated and self.application_state()['backup']=='applied'}}
        if updated:
            if self.application_state()['backup']=='applied':self.emit('备份目标已更新并应用：%s:%s。'%(name,directory))
            else:self.emit('备份目标已保存：%s:%s；启动备份服务后按计划投递。'%(name,directory))
        elif data.get('set_target'):
            self.emit('备份计划尚未保存，未写入备份目标；可稍后在备份设置中启用。')
        else:
            self.emit('可到“备份设置”将 %s 选为备份目标。'%name)

    def remote_question(self,action,data):
        from manager import write_private
        _,backup=self.paths()
        base=backup/'vw-backup-config'
        config=base/'rclone/rclone.conf'
        if action=='remote-wizard':
            if not (backup/'backup.env').exists():raise ValueError('请先保存备份计划。')
            from browser_oauth import cancel
            cancel(self)
            old=getattr(self,'remote_wizard',None)
            if old:shutil.rmtree(old['folder'],ignore_errors=True)
            folder=Path(tempfile.mkdtemp(prefix='remote-wizard-',dir=self.state))
            (folder/'rclone').mkdir()
            original=config.read_text() if config.exists() else ''
            write_private(folder/'rclone/rclone.conf',original)
            self.remote_wizard={'id':secrets.token_hex(12),'folder':str(folder),'name':data['name'],'type':data['type'],'original':original,'created':time.time(),'guided':data.get('guided',False)}
            self.remote_wizard.update(directory=data.get('directory',''),set_target=data.get('set_target',False))
            value=self.rc('config/create',{'name':data['name'],'type':data['type'],'parameters':{'config_is_local':'false'},
                'opt':{'nonInteractive':True,'all':not self.remote_wizard['guided'],'obscure':True}},folder)
        else:
            wizard=getattr(self,'remote_wizard',None)
            if not wizard or wizard['id']!=data.get('wizard_id') or time.time()-wizard['created']>3600:
                raise ValueError('配置向导已过期，请重新开始。')
            if wizard.get('choices') and data.get('answer','') not in wizard['choices']:
                raise ValueError('请先选择本步提供的一个选项，再点击下一步。')
            value=self.rc('config/update',{'name':wizard['name'],'parameters':{'config_is_local':'false'},
                'opt':{'nonInteractive':True,'continue':True,'state':wizard['state'],
                'result':data.get('answer',''),'obscure':True}},Path(wizard['folder']))
        if getattr(self,'remote_cancelled_job',None)==self.job.get('id'):raise ValueError('存储配置已取消，可重新开始或更新面板。')
        wizard=self.remote_wizard
        # The guided route skips only optional client overrides and local-browser questions.
        for _ in range(16):
            state=value.get('State',value.get('state',''))
            if not state:break
            option=value.get('Option') or value.get('option') or {}
            if not isinstance(option,dict):raise ValueError('云盘配置返回的问题格式无法识别，请重新开始。')
            key=option.get('Name','')
            if not state or not wizard.get('guided') or wizard['type'] not in ('drive','onedrive','dropbox') or option.get('Required') or key not in ('client_id','client_secret','root_folder_id','service_account_file','config_is_local'):break
            answer='false' if key=='config_is_local' else ''
            value=self.rc('config/update',{'name':wizard['name'],'parameters':{'config_is_local':'false'},'opt':{'nonInteractive':True,'continue':True,'state':state,'result':answer,'obscure':True}},Path(wizard['folder']))
        state=value.get('State',value.get('state',''))
        if value.get('Error') and not state:
            raise ValueError('云盘配置未完成，请检查授权和服务器网络后重新开始。')
        wizard['state']=state
        if state:
            option=value.get('Option') or value.get('option') or {}
            if not isinstance(option,dict) or not option.get('Name'):
                raise ValueError('云盘配置未能返回下一步，请检查授权结果后重新开始。')
            wizard['choices']=[str(item['Value']).lower() if isinstance(item.get('Value'),bool) else str(item.get('Value','')) for item in option.get('Examples',[]) if 'Value' in item] if option.get('Exclusive') else []
            # Never echo secret defaults returned by Rclone.
            if option.get('IsPassword') or re.search(r'pass|token|secret|key',option.get('Name',''),re.I):
                option['Default']='';option.pop('Value',None)
            browser=None
            wizard.pop('oauth_args',None)
            if option.get('Name')=='config_token':
                from browser_oauth import arguments,PROVIDERS
                args=arguments(option.get('Help',''),wizard['type'])
                if args:
                    wizard['oauth_args']=args
                    browser={'provider':PROVIDERS[wizard['type']][0]}
                    option['Help']='点击生成授权链接，在服务商页面同意后，把返回地址粘贴回面板。无需安装 Rclone 或运行命令。'
            self.job['result']={'wizard_id':wizard['id'],'question':option,'error':value.get('Error','')}
            if browser:self.job['result']['oauth_available']=browser
            self.emit('请在远程存储页面继续填写配置问题。')
        else:
            self.emit('云盘配置流程已完成，正在保存存储配置。')
            original=config.read_text() if config.exists() else ''
            if original!=wizard['original']:raise ValueError('远程配置已发生变化，请重新开始，避免覆盖其他修改。')
            candidate=Path(wizard['folder'])/'rclone/rclone.conf'
            parsed=parser_for(candidate)
            if wizard['name'] not in parsed:raise ValueError('未能取得完整存储配置，请重新开始授权。')
            if config.exists():write_private(config.with_name('rclone.conf.panel-backup'),original)
            updated=False;directory=wizard.get('directory','')
            def restore_remote():
                if original:write_private(config,original)
                else:config.unlink(missing_ok=True)
            try:
                write_private(config,candidate.read_text())
                if wizard.get('set_target'):
                    targets=[dict(item) for item in self.extended_config()['targets']]
                    existing=next((item for item in targets if item['name']==wizard['name'] and item['directory']==directory),None)
                    if existing is not None:existing['enabled']=True
                    else:targets.append({'name':wizard['name'],'directory':directory,'enabled':True})
                    from backup_targets import apply
                    apply(self,targets,restore_extra=restore_remote);updated=True
            except Exception:
                restore_remote();raise
            if directory:
                try:self.remember_preset_target(wizard['name'],directory)
                except OSError:self.emit('存储已保存，但浏览目录未能记录；下次浏览时需重新填写目录。')
            self.job['result']={'wizard_id':wizard['id'],'done':True,'storage_saved':{'name':wizard['name'],'label':'存储','directory':directory,'verified':False,'target_updated':updated,'target_applied':updated and self.application_state()['backup']=='applied'}}
            self.emit('存储连接与备份目标已保存，目录：'+directory if updated else '存储连接已保存；已有备份目标保持不变。')
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
