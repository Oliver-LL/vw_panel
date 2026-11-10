"""Bounded, private Rclone config calls that can be cancelled independently."""
import json
import secrets
import shutil
import subprocess
import tempfile

TIMEOUT=60
ACTIONS=('remote-wizard','remote-continue','remote-oauth-complete')

def run(manager,path,data,folder):
    job_id=manager.job.get('id')
    name='vwp-config-'+secrets.token_hex(10)
    script='payload=$(cat); exec rclone rc --loopback --config /config/rclone/rclone.conf "$1" --json "$payload"'
    args=['docker','run','--rm','--name',name,'-i','-v',str(folder)+':/config',
          '--entrypoint','sh','ttionya/vaultwarden-backup:latest','-c',script,'rc',path]
    process=None
    try:
        with tempfile.TemporaryFile() as output,tempfile.TemporaryFile() as errors:
            with manager.mutex:
                if job_id and getattr(manager,'remote_cancelled_job',None)==job_id:raise ValueError('存储配置已取消，可重新开始或更新面板。')
                process=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=output,stderr=errors)
                manager.remote_config_call={'job_id':job_id,'name':name,'process':process}
            try:process.communicate(json.dumps(data).encode(),timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                process.kill();process.communicate(timeout=5)
                raise ValueError('云盘配置请求超过 60 秒未响应，任务已结束。请检查服务器是否能连接云盘服务商，再重新开始；现在可更新面板。')
            if job_id and getattr(manager,'remote_cancelled_job',None)==job_id:raise ValueError('存储配置已取消，可重新开始或更新面板。')
            if process.returncode:raise ValueError('云盘配置请求失败，请检查服务器网络与云盘授权后重试。凭据未写入日志。')
            output.seek(0)
            try:
                value=json.loads(output.read(4*1024*1024).decode())
                if not isinstance(value,dict):raise ValueError()
            except (ValueError,UnicodeError):raise ValueError('云盘配置返回了无法识别的结果，请重新开始。')
            return value
    except OSError:raise ValueError('无法启动云盘配置，请检查 Docker 服务后重试。')
    finally:
        with manager.mutex:
            if getattr(manager,'remote_config_call',{}).get('name')==name:manager.remote_config_call={}
        if process is not None:
            try:subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
            except (OSError,subprocess.TimeoutExpired):pass

def cancel(manager,job_id):
    running=False
    with manager.mutex:
        job=manager.job
        if not job_id or job.get('id')!=job_id:raise ValueError('任务已变化，请刷新后重试。')
        if job.get('state')=='running':
            if job.get('action') not in ACTIONS:raise ValueError('这里只能取消存储配置，不能中断其他操作。')
            if job.get('result',{}).get('done'):raise ValueError('配置已保存，无需取消。')
            manager.remote_cancelled_job=job_id
            job['cancelled']=True
            call=getattr(manager,'remote_config_call',{})
            process=call.get('process') if call.get('job_id')==job_id else None
            if process is not None:
                try:process.terminate()
                except OSError:pass
            running=True
    if running:
        from browser_oauth import cancel as cancel_oauth
        cancel_oauth(manager)
        return {'ok':True,'message':'正在取消存储配置，任务结束后即可更新面板。'}
    # An idle wizard owns only its temporary candidate config, not the live file.
    with manager.exclusive():
        from browser_oauth import cancel as cancel_oauth
        cancel_oauth(manager)
        wizard=getattr(manager,'remote_wizard',None)
        if wizard:shutil.rmtree(wizard['folder'],ignore_errors=True)
        manager.remote_wizard=None
        with manager.mutex:
            if manager.job.get('id')==job_id:
                manager.job['result']={'wizard_cancelled':True}
                manager.job['cancelled']=True
    return {'ok':True,'message':'存储配置已结束，现有存储配置保留。'}
