"""Scoped environment editing backed by the upstream variable catalog."""
import json
from pathlib import Path
import re

CATALOG=json.loads(Path(__file__).with_name('env_catalog.json').read_text())
CORE_LOCATIONS={**{k:('基本信息','vault') for k in ('DOMAIN','SERVER_ADMIN_EMAIL')},**{k:('注册与权限','vault') for k in ('SIGNUPS_ALLOWED','INVITATIONS_ALLOWED','SENDS_ALLOWED')},**{k:('运行与安全设置','vault') for k in ('ENABLE_WEBSOCKET','WEBSOCKET_ENABLED','WEB_VAULT_ENABLED','EMERGENCY_ACCESS_ALLOWED','ENABLE_DB_WAL','ADMIN_RATELIMIT_MAX_BURST','ADMIN_RATELIMIT_SECONDS','ADMIN_SESSION_LIFETIME','LOGIN_RATELIMIT_MAX_BURST','LOGIN_RATELIMIT_SECONDS','TZ')},**{k:('官方推送','vault') for k in ('PUSH_ENABLED','PUSH_INSTALLATION_ID','PUSH_INSTALLATION_KEY')},**{k:('邮件服务','mail') for k in ('SMTP_HOST','SMTP_PORT','SMTP_SECURITY','SMTP_FROM','SMTP_USERNAME','SMTP_PASSWORD')},'ADMIN_TOKEN':('管理口令加密设置','vault'),**{k:('由容器部署管理','vault') for k in ('DATA_FOLDER','ROCKET_PORT','ROCKET_ADDRESS','ROCKET_TLS')}}
BACKUP_LOCATIONS={**{k:('备份计划','backup') for k in ('CRON','TIMEZONE','BACKUP_KEEP_DAYS','ZIP_PASSWORD','ZIP_ENABLE','ZIP_TYPE','BACKUP_FILE_SUFFIX','DISPLAY_NAME','BACKUP_FILE_DATE','BACKUP_FILE_DATE_SUFFIX')},**{k:('备份邮件通知','backup') for k in ('MAIL_SMTP_ENABLE','MAIL_SMTP_VARIABLES','MAIL_TO','MAIL_WHEN_SUCCESS','MAIL_WHEN_FAILURE','MAIL_FORCE_THREAD')},**{k:('远程存储的备份目标','remote') for k in ('RCLONE_REMOTE_NAME','RCLONE_REMOTE_DIR')},'DATA_DIR':('由容器挂载管理','backup')}

def sensitive(key,value=''):
    # Flags, counters and filenames describe credentials but do not contain them.
    if key in ('RSA_KEY_FILENAME','DATA_RSAKEY','SSO_MASTER_PASSWORD_POLICY') or any(item['key']==key and item['type'] in ('bool','number') for scope in ('core','backup') for item in CATALOG[scope]):return False
    return bool(re.search(r'PASSWORD|TOKEN|SECRET|KEY|CREDENTIAL|SMTP_VARIABLES',key) or key in ('DATABASE_URL','RCLONE_GLOBAL_FLAG') or key.startswith('PING_') or re.search(r'://[^/\s]*@|[?&](?:token|key|secret|password)=',str(value),re.I))

def definitions(scope):
    locations=CORE_LOCATIONS if scope=='core' else BACKUP_LOCATIONS
    return {item['key']:{**item,'location':locations.get(item['key'],('',''))[0],'page':locations.get(item['key'],('',''))[1],'editable':item['key'] not in locations,'secret':sensitive(item['key'])} for item in CATALOG[scope]}

def public_environment(manager,core,backup):
    result={'sources':CATALOG['sources']}
    app,_=manager.paths();overrides=set()
    try:
        path=app/'data/config.json'
        if path.stat().st_size<1024*1024:
            value=json.loads(path.read_text())
            if isinstance(value,dict):overrides={str(key).upper() for key in value}
    except (OSError,ValueError):pass
    for scope,current in [('core',core),('backup',backup)]:
        specs=definitions(scope);items=[]
        for key,spec in specs.items():
            secret=spec['secret'] or sensitive(key,current.get(key,''))
            items.append({**spec,'secret':secret,'present':key in current,'default':'' if secret else spec['default'],'value':'' if secret else current.get(key,''),'overridden':scope=='core' and key in overrides})
        for key in current.keys()-specs.keys():
            items.append({'key':key,'label':key,'category':'未收录配置','editable':False,'secret':True,'value':'','present':True,'default':'','type':'text','location':'已有配置保留；请先核对原项目说明','page':'','help':'未在本次官方文档快照中收录，不回显值。','overridden':False})
        result[scope]=items
    return result

def save_environment(manager,scope,data):
    from manager import read_vars,update_vars,write_private,text
    app,backup=manager.paths();file=(app/'vaultwarden.env') if scope=='core' else (backup/'backup.env')
    if not file.exists():raise ValueError('请先保存'+('基本信息。' if scope=='core' else '备份计划。'))
    specs=definitions(scope);values=data.get('values',{});remove=data.get('remove',[])
    if not isinstance(values,dict) or not isinstance(remove,list) or len(values)+len(remove)>200:raise ValueError('环境变量格式不正确。')
    if any(not isinstance(key,str) for key in [*values,*remove]):raise ValueError('环境变量名称不正确。')
    if set(values)&set(remove):raise ValueError('不能同时修改和移除同一变量。')
    for key in [*values,*remove]:
        if key not in specs or not specs[key]['editable']:raise ValueError(key+' 请使用专用设置或部署配置修改。')
    current=read_vars(file);updates={}
    for key,value in values.items():
        spec=specs[key]
        if isinstance(value,bool):value=str(value).lower()
        text(value,key,8192)
        if (spec['secret'] or sensitive(key,current.get(key,''))) and not value:
            if current.get(key):continue
            raise ValueError(key+' 请填写新值，或移除此项以使用默认配置。')
        if spec['type']=='bool':
            if value.lower() not in ('true','false'):raise ValueError(key+' 请选择 true 或 false。')
            value=value.lower()
        elif spec['type']=='number':
            if not re.fullmatch(r'-?\d{1,18}',value):raise ValueError(key+' 必须为整数。')
            if str(spec['default']).isdigit() and int(value)<0:raise ValueError(key+' 不能为负数。')
        updates[key]=value
    # Validate everything first; then write a single private env file and preserve other keys/comments.
    if not remove:update_vars(file,updates);return
    original=file.read_text();write_private(file.with_name(file.name+'.panel-backup'),original)
    lines=[line for line in original.splitlines() if line.split('=',1)[0] not in set(remove)|set(updates)]
    for key,value in updates.items():lines.append(key+"='"+str(value).replace('\\','\\\\').replace("'","\\'")+"'")
    write_private(file,'\n'.join(lines)+'\n')
