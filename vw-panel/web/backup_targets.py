"""Keep disabled destinations, while exposing only enabled ones to the backup image."""
import configparser
import hashlib
import json
from pathlib import Path
import re
import secrets
import subprocess

SENTINEL='__vwp_disabled_targets__'

def files(manager):
    _,folder=manager.paths();return folder/'backup.env',folder/'panel-targets.json'

def active_from_env(env):
    result=[]
    for index in range(20):
        suffix='' if not index else '_'+str(index);name=env.get('RCLONE_REMOTE_NAME'+suffix)
        if not name:break
        if name==SENTINEL:continue
        result.append({'name':name,'directory':env.get('RCLONE_REMOTE_DIR'+suffix,'/VaultBackup')})
    return result

def read(manager):
    from manager import read_vars
    env_file,meta_file=files(manager);active=active_from_env(read_vars(env_file));meta={}
    try:
        if meta_file.stat().st_size<100000:meta=json.loads(meta_file.read_text())
    except (OSError,ValueError):pass
    if not isinstance(meta,dict):meta={}
    stored=meta.get('targets',[]) if isinstance(meta,dict) else []
    valid=[]
    for row in stored[:20] if isinstance(stored,list) else []:
        if isinstance(row,dict) and all(isinstance(row.get(key),str) for key in ('id','name','directory')) and isinstance(row.get('enabled'),bool):valid.append(row)
    if meta.get('active')==active:return valid
    # An original Bash edit is authoritative for active destinations; keep disabled drafts.
    result=[]
    for row in active:
        old=next((item for item in valid if item['name']==row['name'] and item['directory']==row['directory']),{})
        result.append({**row,'id':old.get('id') or 'legacy-'+hashlib.sha256((row['name']+'\0'+row['directory']).encode()).hexdigest()[:24],'enabled':True})
    result.extend(row for row in valid if not row['enabled'] and not any(item['name']==row['name'] and item['directory']==row['directory'] for item in result))
    return result[:20]

def normalize(manager,targets):
    from manager import text
    if not isinstance(targets,list) or len(targets)>20:raise ValueError('最多可配置 20 个备份目标。')
    env_file,_=files(manager)
    if not env_file.exists():raise ValueError('请先保存备份计划。')
    parser=configparser.ConfigParser(interpolation=None);parser.read(env_file.parent/'vw-backup-config/rclone/rclone.conf')
    previous=read(manager);result=[];ids=set();destinations=set()
    for row in targets:
        if not isinstance(row,dict):raise ValueError('备份目标格式不正确。')
        name=text(row.get('name',''),'存储名称',64);directory=text(row.get('directory',''),'保存目录',512);enabled=row.get('enabled',True)
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}',name) or not isinstance(enabled,bool):raise ValueError('请选择有效的存储和启用状态。')
        if enabled and name not in parser:raise ValueError('启用前请先配置对应的存储连接。')
        if not directory or '..' in Path(directory).parts:raise ValueError('请填写有效的保存目录。')
        if name in parser and parser[name].get('type','')=='s3' and not directory.strip('/'):raise ValueError('S3/R2 保存目录必须包含 Bucket。')
        old=next((item for item in previous if item['name']==name and item['directory']==directory),{})
        identity=row.get('id') or old.get('id') or secrets.token_hex(12)
        if not isinstance(identity,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',identity) or identity in ids:raise ValueError('目标标识重复或不正确。')
        if (name,directory) in destinations:raise ValueError('同一存储与目录不能重复添加。')
        ids.add(identity);destinations.add((name,directory));result.append({'id':identity,'name':name,'directory':directory,'enabled':enabled})
    return result

def save(manager,targets,paused=None):
    from manager import read_vars,write_private
    targets=normalize(manager,targets);env_file,meta_file=files(manager);original=env_file.read_text();meta_original=meta_file.read_text() if meta_file.exists() else None
    prior={}
    try:prior=json.loads(meta_original or '{}')
    except ValueError:pass
    if not isinstance(prior,dict):prior={}
    active=[{'name':row['name'],'directory':row['directory']} for row in targets if row['enabled']]
    lines=[line for line in original.splitlines() if not re.fullmatch(r'RCLONE_REMOTE_(NAME|DIR)(_[0-9]+)?|VW_PANEL_TARGETS_DISABLED',line.split('=',1)[0])]
    values={}
    for index,row in enumerate(active):
        suffix='' if not index else '_'+str(index);values['RCLONE_REMOTE_NAME'+suffix]=row['name'];values['RCLONE_REMOTE_DIR'+suffix]=row['directory']
    if not active:values={'RCLONE_REMOTE_NAME':SENTINEL,'RCLONE_REMOTE_DIR':'/','VW_PANEL_TARGETS_DISABLED':'true'}
    for key,value in values.items():lines.append(key+"='"+value.replace('\\','\\\\').replace("'","\\'")+"'")
    try:
        rendered='\n'.join(lines)+'\n'
        if rendered!=original:
            write_private(env_file.with_name('backup.env.panel-backup'),original)
            write_private(env_file,rendered)
        write_private(meta_file,json.dumps({'targets':targets,'active':active,'paused_for_targets':prior.get('paused_for_targets',False) if paused is None else paused},ensure_ascii=False)+'\n')
    except Exception:
        write_private(env_file,original)
        if meta_original is None:meta_file.unlink(missing_ok=True)
        else:write_private(meta_file,meta_original)
        raise
    return targets

def running(name):
    try:result=subprocess.run(['docker','inspect','--format','{{.State.Running}}',name],capture_output=True,text=True,timeout=10)
    except (OSError,subprocess.TimeoutExpired):raise ValueError('无法确认备份服务状态，目标未修改，请检查 Docker 后重试。')
    if result.returncode:
        if re.search(r'no such (?:object|container)',result.stderr,re.I):return False
        raise ValueError('无法确认备份服务状态，目标未修改，请检查 Docker 后重试。')
    if result.stdout.strip() not in ('true','false'):raise ValueError('服务状态无法识别，目标未修改。')
    return result.stdout.strip()=='true'

def apply(manager,targets,restore_extra=None):
    from manager import write_private
    targets=normalize(manager,targets);env_file,meta_file=files(manager);original=env_file.read_text();meta_original=meta_file.read_text() if meta_file.exists() else None
    was_running=running('vaultwarden-backup');paused=False
    try:paused=json.loads(meta_original or '{}').get('paused_for_targets',False)
    except ValueError:pass
    resume=paused and any(row['enabled'] for row in targets) and running('vaultwarden')
    save(manager,targets,paused=paused)
    try:
        if not any(row['enabled'] for row in targets):
            if was_running:manager._command(manager.compose(env_file.parent,'stop'))
            if was_running and running('vaultwarden-backup'):raise ValueError('未能暂停备份服务，目标设置已恢复。')
            save(manager,targets,paused=was_running or paused)
            manager.note_applied('backup');manager.emit('所有目标已停用，备份服务已暂停；配置和已有备份保留。')
        elif was_running or resume:
            manager._command(manager.compose(env_file.parent,'up','-d','--force-recreate'))
            if not running('vaultwarden-backup'):raise ValueError('备份服务未能运行，目标设置已恢复。')
            save(manager,targets,paused=False);manager.note_applied('backup');manager.emit('备份目标已保存并应用，正在运行的服务已加载最新目标。')
        else:
            manager.emit('备份目标已保存。备份服务尚未运行，请在备份设置中启用服务。')
    except Exception:
        write_private(env_file,original)
        if meta_original is None:meta_file.unlink(missing_ok=True)
        else:write_private(meta_file,meta_original)
        restored=True
        if restore_extra:
            try:restore_extra()
            except OSError:
                restored=False;manager.emit('存储配置未能恢复，备份服务将暂停，请检查配置文件权限。')
        if was_running:
            try:manager._command(manager.compose(env_file.parent,*(['up','-d','--force-recreate'] if restored else ['stop'])))
            except Exception:manager.emit('旧目标配置已恢复，但备份服务未能恢复运行，请检查备份日志。')
        raise

def clear_pause(manager):
    from manager import write_private
    _,path=files(manager)
    if not path.exists():return
    try:value=json.loads(path.read_text())
    except ValueError:return
    if isinstance(value,dict) and value.get('paused_for_targets'):
        value['paused_for_targets']=False;write_private(path,json.dumps(value,ensure_ascii=False)+'\n')
