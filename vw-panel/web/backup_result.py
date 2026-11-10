"""Keep retained archives outside the upstream image's disposable work directory."""
import configparser
import json
from pathlib import Path
import re

WORK='/bitwarden/backup'
STORE='/vwp/backups'

def retained_directory(value):
    if value==WORK or value.startswith(WORK+'/'):return STORE+value[len(WORK):]
    return value

def prepare_storage(manager):
    from manager import read_vars,update_vars,write_private
    _,folder=manager.paths();compose=folder/'docker-compose.yml'
    if not compose.exists():return
    original=compose.read_text()
    # Only change a short volume's container destination, preserving source paths and other settings.
    revised=re.sub(r'(?<=:)/bitwarden/backup(?=[:\"\x27\s])',STORE,original)
    if WORK in revised:
        raise ValueError('备份使用了自定义临时目录挂载，请将持久目录改挂载到 /vwp/backups，再重启并应用备份服务。')
    env_file=folder/'backup.env';meta_file=folder/'panel-targets.json'
    parser=configparser.ConfigParser(interpolation=None);parser.read(folder/'vw-backup-config/rclone/rclone.conf')
    local={name for name in parser.sections() if parser[name].get('type','').lower()=='local'}
    env=read_vars(env_file);changes={}
    for index in range(20):
        suffix='' if index==0 else '_'+str(index)
        if env.get('RCLONE_REMOTE_NAME'+suffix) in local:
            key='RCLONE_REMOTE_DIR'+suffix;old=env.get(key,'');new=retained_directory(old)
            if old!=new:changes[key]=new
    meta=None
    if meta_file.exists():
        meta=json.loads(meta_file.read_text())
        for row in meta.get('targets',[])+meta.get('active',[]):
            if row.get('name') in local:row['directory']=retained_directory(row.get('directory',''))
    paths=[compose,env_file,meta_file];snapshots={p:p.read_bytes() if p.exists() else None for p in paths}
    try:
        if revised!=original:
            write_private(compose.with_name('docker-compose.yml.panel-backup'),original)
            write_private(compose,revised)
        if changes:update_vars(env_file,changes)
        if meta is not None:write_private(meta_file,json.dumps(meta,ensure_ascii=False)+'\n')
    except Exception:
        for path,old in snapshots.items():
            if old is None:path.unlink(missing_ok=True)
            else:write_private(path,old.decode())
        raise

def run_now(manager):
    from backup_targets import active_from_env
    _,folder=manager.paths()
    raw=manager._command(['docker','inspect','--format','{"env":{{json .Config.Env}},"mounts":{{json .Mounts}}}','vaultwarden-backup'],secret=True)
    try:
        runtime=json.loads(raw)
        mounts=runtime['mounts'];values=runtime['env']
        if not isinstance(mounts,list) or not isinstance(values,list):raise ValueError()
        env=dict(item.split('=',1) for item in values if isinstance(item,str) and '=' in item)
    except (ValueError,KeyError,TypeError):raise ValueError('无法确认备份服务实际使用的目录，请刷新服务状态后重试。')
    if any(isinstance(row,dict) and (row.get('Destination')==WORK or str(row.get('Destination','')).startswith(WORK+'/')) for row in mounts):
        raise ValueError('备份服务仍将本机文件挂载在临时清理目录。请到备份设置点击“重启并应用”修复后再备份；已有文件保留。')
    targets=active_from_env(env)
    parser=configparser.ConfigParser(interpolation=None);parser.read(folder/'vw-backup-config/rclone/rclone.conf')
    destinations=[]
    for target in targets:
        local=target['name'] in parser and parser[target['name']].get('type','').lower()=='local'
        directory=target['directory']
        if local and retained_directory(directory)!=directory:
            raise ValueError('本地目标仍指向临时清理目录，请在备份设置重启并应用后再备份。')
        destinations.append(dict(target,local=local))
    before={row['name']:(row['size'],row['modified']) for row in manager.files()}
    manager._command(['docker','exec','vaultwarden-backup','/app/backup.sh'])
    after=manager.files()
    added=[row['name'] for row in after if before.get(row['name'])!=(row['size'],row['modified'])]
    manager.job['result']={'backup_result':{'destinations':destinations,'local_files':added,'local_total':len(after)}}
    for target in destinations:manager.emit('本次备份目标：'+target['name']+' · '+target['directory'])
    manager.emit('本机列表新增或更新 %s 个文件；远程文件请从对应存储浏览。'%len(added))
