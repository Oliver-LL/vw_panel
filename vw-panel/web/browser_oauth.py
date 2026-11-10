"""Use rclone's own OAuth exchange; no token or secret enters task results."""
import hmac
from pathlib import Path
import re
import shlex
import socket
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

PROVIDERS={'drive':('Google Drive',{'accounts.google.com'}),'onedrive':('OneDrive',{'login.microsoftonline.com','login.live.com'}),'dropbox':('Dropbox',{'www.dropbox.com','dropbox.com'})}

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def request_local(path):
    opener=build_opener(ProxyHandler({}),NoRedirect())
    try:
        with opener.open('http://127.0.0.1:53682/'+path,timeout=5) as response:return response.status,dict(response.headers)
    except HTTPError as error:
        result=error.code,dict(error.headers);error.close();return result

def arguments(help_text,provider):
    if provider not in PROVIDERS:return None
    match=re.search(r'^\s*rclone authorize\s+([^\r\n]+)',help_text,re.M)
    if not match:return None
    try:args=shlex.split(match[1])
    except ValueError:return None
    if not 1<=len(args)<=3 or args[0]!=provider:return None
    if any(len(arg)>20000 or any(ord(c)<32 for c in arg) for arg in args):return None
    return args

def wizard(manager,wizard_id):
    current=getattr(manager,'remote_wizard',None)
    if not current or not isinstance(wizard_id,str) or current['id']!=wizard_id or time.time()-current['created']>3600:
        raise ValueError('授权向导已过期，请重新开始。')
    return current

def cancel(manager):
    active=getattr(manager,'remote_oauth',None)
    if not active:return
    manager.remote_oauth=None
    try:subprocess.run(['docker','rm','-f',active['container']],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
    except (OSError,subprocess.TimeoutExpired):pass
    process=active['process']
    try:
        if process.poll() is None:process.terminate()
        process.wait(timeout=5)
    except (OSError,subprocess.TimeoutExpired):
        try:process.kill()
        except OSError:pass
    active['output'].close()
    Path(active['path']).unlink(missing_ok=True)

def start(manager,wizard_id):
    from manager import write_private
    current=wizard(manager,wizard_id);args=current.get('oauth_args')
    if not args or args[0] not in PROVIDERS:raise ValueError('此存储暂不支持面板生成授权链接，请使用手动授权入口。')
    cancel(manager)
    # rclone's registered redirect is loopback. Never expose its callback on a public port.
    with socket.socket() as sock:
        try:sock.bind(('127.0.0.1',53682))
        except OSError:raise ValueError('临时授权端口正在使用，请完成已有授权后重试。')
    path=Path(current['folder'])/'oauth-private.log';write_private(path,'');output=path.open('wb')
    container='vwp-oauth-'+__import__('secrets').token_hex(12)
    script=('IFS= read -r backend; IFS= read -r arg1; IFS= read -r arg2; '
            'if [ -n "$arg2" ]; then exec timeout 600 rclone authorize "$backend" "$arg1" "$arg2" --auth-no-open-browser; '
            'elif [ -n "$arg1" ]; then exec timeout 600 rclone authorize "$backend" "$arg1" --auth-no-open-browser; '
            'else exec timeout 600 rclone authorize "$backend" --auth-no-open-browser; fi')
    try:
        process=subprocess.Popen(['docker','run','--rm','--name',container,'--network','host','-i','--entrypoint','sh',
            'ttionya/vaultwarden-backup:latest','-ec',script],stdin=subprocess.PIPE,stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
    except OSError:
        output.close();path.unlink(missing_ok=True);raise ValueError('无法启动授权服务，请确认 Docker 和备份镜像可用。')
    manager.remote_oauth={'wizard_id':current['id'],'provider':args[0],'process':process,'container':container,'output':output,'path':str(path),'expires':time.time()+600}
    try:
        process.stdin.write(('\n'.join(args+['']*(3-len(args)))+'\n').encode());process.stdin.close()
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            output.flush();raw=path.read_bytes()[:1000000].decode('utf-8','replace')
            match=re.search(r'http://127\.0\.0\.1:53682/auth\?state=([A-Za-z0-9_~.%+-]+)',raw)
            if match:
                state=parse_qs(urlsplit(match[0]).query)['state'][0]
                status,headers=request_local('auth?'+urlencode({'state':state}));auth_url=headers.get('Location',headers.get('location',''))
                parsed=urlsplit(auth_url)
                if status not in (302,303,307) or parsed.scheme!='https' or parsed.hostname not in PROVIDERS[args[0]][1] or parsed.username or parsed.password or parsed.port not in (None,443):
                    raise ValueError('授权服务未返回受支持的登录地址，请重新开始。')
                manager.remote_oauth['state']=state
                manager.job['result']={'wizard_id':current['id'],'browser_oauth':{'url':auth_url,'provider':PROVIDERS[args[0]][0],'expires':manager.remote_oauth['expires']}}
                manager.emit('授权链接已生成。请打开服务商页面完成授权，然后返回面板。');return
            if process.poll() is not None:break
            time.sleep(.1)
        raise ValueError('授权服务未能就绪，请确认备份镜像可正常运行后重试。')
    except (OSError,URLError) as error:
        cancel(manager);raise ValueError('生成授权链接失败，请检查服务器网络后重试。') from None
    except Exception:
        cancel(manager);raise

def complete(manager,wizard_id,callback_url):
    current=wizard(manager,wizard_id);active=getattr(manager,'remote_oauth',None)
    if not active or active['wizard_id']!=current['id'] or time.time()>active['expires']:
        cancel(manager);raise ValueError('授权链接已过期，请重新生成。')
    parsed=urlsplit(callback_url)
    try:valid_port=parsed.port==53682
    except ValueError:valid_port=False
    if parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost','localhost.rclone.org') or not valid_port or parsed.path not in ('','/') or parsed.username or parsed.password or parsed.fragment:
        raise ValueError('请复制授权后地址栏中完整的 http://127.0.0.1:53682/ 开头地址。')
    query=parse_qs(parsed.query)
    if any(len(query.get(key,[]))!=1 for key in ('state',)) or not hmac.compare_digest(query['state'][0],active.get('state','')):
        raise ValueError('这不是本次授权的返回地址，请使用刚打开的授权页面重新操作。')
    if query.get('error'):
        cancel(manager);raise ValueError('你取消了云盘授权，可以重新生成链接后再试。')
    if len(query.get('code',[]))!=1 or not query['code'][0] or len(query['code'][0])>16000:
        raise ValueError('返回地址中缺少授权结果，请复制完整地址。')
    try:
        status,_=request_local('?'+urlencode({'state':query['state'][0],'code':query['code'][0]}))
        if status!=200:raise ValueError('授权返回地址校验失败，请重新授权。')
        try:active['process'].wait(timeout=30)
        except subprocess.TimeoutExpired:raise ValueError('授权交换超时，请重新生成链接后再试。')
        raw=Path(active['path']).read_bytes()[:1000000].decode('utf-8','replace')
        match=re.search(r'--->\s*\n(.*?)\n<---End paste',raw,re.S)
        if active['process'].returncode!=0 or not match:raise ValueError('未能取得授权结果，请确认已同意授权，并检查服务器网络。')
        token=match[1].strip()
        if not token or len(token)>60000:raise ValueError('授权结果无法识别，请重新授权。')
        # Feed the result straight into the original isolated wizard; never return it to the browser.
        manager.remote_question('remote-continue',{'wizard_id':current['id'],'answer':token})
    except (OSError,URLError):raise ValueError('授权服务连接失败，请重新生成链接。') from None
    finally:cancel(manager)
