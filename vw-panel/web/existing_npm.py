"""Configure one Vaultwarden route on an existing, locally detected NPM."""
import ipaddress
import json
import re
from urllib.parse import urlsplit
from pathlib import Path
from managed_proxy import ManagedProxyMixin, NPMAPIError

class ExistingNPM(ManagedProxyMixin):
    def __init__(self, manager, data):
        from manager import read_vars
        from proxy_environment import detect_environment
        self.manager=manager
        memory=read_vars(manager.memory)
        app,_=manager.paths()
        self.domain=urlsplit(read_vars(app/'vaultwarden.env').get('DOMAIN','')).hostname
        if memory.get('PROXY_MODE')!='docker_ext' or memory.get('PANEL_PROXY_KIND') or not self.domain:
            raise ValueError('请先保存使用现有容器反代的密码库配置。')
        if manager.application_state().get('core')=='pending':raise ValueError('请先应用主服务配置，再创建 NPM 站点。')
        self.target=data['target'];network=memory.get('DOCKER_NET','')
        if not re.fullmatch(r'[a-f0-9]{12,64}',self.target):raise ValueError('请选择有效的 NPM 实例。')
        if urlsplit(read_vars(app/'vaultwarden.env').get('DOMAIN','')).port not in (None,443):raise ValueError('自动配置现有 NPM 使用 HTTPS 443，请使用不带自定义端口的地址。')
        if memory.get('PANEL_PROXY_TARGET') and self.target!=memory['PANEL_PROXY_TARGET']:
            raise ValueError('所选 NPM 与已保存的反代不一致，请重新检测。')
        choices=detect_environment().get('proxies',[])
        choice=next((p for p in choices if p.get('id')==self.target and p.get('kind')=='Nginx Proxy Manager' and p.get('running') and network in p.get('networks',[])),None)
        if not choice:raise ValueError('未找到所选 NPM 或共享网络已变化，请重新检测。')
        # Inspect only network metadata; never read the NPM environment or mounted secrets.
        raw=manager._command(['docker','inspect','--format','{{json .NetworkSettings.Networks}}',self.target],secret=True)
        try:
            address=json.loads(raw)[network]['IPAddress']
            ip=ipaddress.ip_address(address)
            if ip.version!=4 or not ip.is_private or ip.is_loopback or ip.is_unspecified or ip.is_link_local:raise ValueError()
        except (ValueError,KeyError,TypeError):raise ValueError('无法获取 NPM 的本机 Docker 网络地址。') from None
        self.base='http://%s:81/api/'%ip
        if manager._command(['docker','inspect','--format','{{.State.Running}}','vaultwarden']).strip()!='true':
            raise ValueError('请先启用密码库主服务，再创建 NPM 站点。')
        try:
            main_networks=json.loads(manager._command(['docker','inspect','--format','{{json .NetworkSettings.Networks}}','vaultwarden'],secret=True))
            if not isinstance(main_networks,dict) or network not in main_networks:raise ValueError()
        except (ValueError,TypeError):raise ValueError('密码库尚未接入所选 NPM 的网络，请重新应用主服务配置。') from None
        self.email=data['email'];self.password=data['password']
        self.folder=manager.state/'existing-npm'/self.target
        self.folder.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.network=network

    def proxy_folder(self):return self.folder
    def proxy_settings(self):return {'domain':self.domain}
    def _npm_api_base(self):return self.base
    def emit(self,message):self.manager.emit(message)
    def _npm_token(self):
        if not getattr(self,'_version_checked',False):
            self._version_checked=True;self._legacy_certificate=False
            try:info=self._npm_request('')
            except NPMAPIError as error:
                if error.status!=404:raise
                info={}
            version=info.get('version',{}) if isinstance(info,dict) else {}
            if isinstance(version,dict) and all(isinstance(version.get(key),int) and not isinstance(version.get(key),bool) for key in ('major','minor')):
                self._legacy_certificate=(version['major'],version['minor'])<(2,14)
                self.emit('检测到 NPM %s.%s，已选择对应证书参数。'%(version['major'],version['minor']))
        self.emit('正在使用现有 NPM 账号登录…')
        result=self._npm_request('tokens',{'identity':self.email,'secret':self.password})
        if not isinstance(result,dict) or not isinstance(result.get('token'),str) or not result['token']:
            raise ValueError('NPM 登录未返回有效授权，请检查 NPM 账号与接口版本。')
        token=result['token'];self.email_source='login'
        try:
            profile=self._npm_request('users/me',token=token)
            email=profile.get('email','') if isinstance(profile,dict) else ''
            if isinstance(email,str) and re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email):
                self.email=email;self.email_source='npm'
                self.emit('已通过授权读取 NPM 当前账号邮箱；密码不回传。')
        except ValueError:
            self.emit('NPM 用户资料接口暂不可用，保留本次已登录邮箱。')
        return token

    def _npm_request(self,path,data=None,token=None,method=None,timeout=15):
        if path!='nginx/certificates' or data is None:return super()._npm_request(path,data,token,method,timeout)
        def payload(legacy):
            result={**data,'meta':dict(data.get('meta',{}))}
            result['meta'].pop('letsencrypt_email',None);result['meta'].pop('letsencrypt_agree',None)
            if legacy:result['meta'].update(letsencrypt_email=self.email,letsencrypt_agree=True)
            return result
        legacy=getattr(self,'_legacy_certificate',False)
        try:return super()._npm_request(path,payload(legacy),token,method,timeout)
        except NPMAPIError as error:
            # Retry once only for an explicit schema rejection, never an ACME/network failure.
            if (legacy and error.compat=='legacy-rejected') or (not legacy and error.compat=='legacy-required'):
                self.emit('NPM 证书接口参数有差异，按接口校验结果切换一次兼容格式。')
                self._legacy_certificate=not legacy
                return super()._npm_request(path,payload(not legacy),token,method,timeout)
            raise

    def configure(self):
        state=self.folder/'route-state.json'
        if state.exists():
            saved=json.loads(state.read_text())
            # Never reuse ownership across changed networks or domains.
            if saved.get('network') not in (None,self.network) or saved.get('domain')!=self.domain:
                raise ValueError('该 NPM 已记录其他密码库路由，请在 NPM 手动确认现有规则后配置。')
            hosts=self._npm_request('nginx/proxy-hosts',token=self._npm_token())
            host=next((h for h in hosts if h.get('id')==saved.get('host_id')),None) if isinstance(hosts,list) else None
            if host and (host.get('domain_names')!=[self.domain] or host.get('forward_host')!='vaultwarden' or host.get('forward_port')!=80):
                raise ValueError('已记录站点在 NPM 中被修改，请手动确认，面板不会覆盖。')
        try:
            self.configure_npm()
            from manager import write_private
            saved=json.loads(state.read_text());saved['network']=self.network;saved['email']=self.email;saved['email_source']=getattr(self,'email_source','login')
            write_private(state,json.dumps(saved))
            write_private(self.manager.state/'existing-npm'/'last-target.json',json.dumps({'target':self.target}))
            self.emit('已在现有 NPM 写入密码库站点与 HTTPS。请实际打开密码库验证访问。')
        finally:
            self.password=''

def public_route(manager,memory,core):
    """Return last completed route metadata, never login credentials or live-health claims."""
    try:
        if memory.get('PANEL_PROXY_KIND') or memory.get('PROXY_MODE')!='docker_ext':return {}
        root=manager.state/'existing-npm'
        target=memory.get('PANEL_PROXY_TARGET','')
        if not target:
            if (root/'last-target.json').exists():target=json.loads((root/'last-target.json').read_text()).get('target','')
            else:
                matches=[]
                for candidate in list(root.glob('*/route-state.json'))[:32]:
                    if not re.fullmatch(r'[a-f0-9]{12,64}',candidate.parent.name):continue
                    try:previous=json.loads(candidate.read_text())
                    except (OSError,ValueError):continue
                    if previous.get('domain')==urlsplit(core.get('DOMAIN','')).hostname and previous.get('network')==memory.get('DOCKER_NET') and isinstance(previous.get('host_id'),int):matches.append(candidate.parent.name)
                if len(matches)==1:target=matches[0]
        if not isinstance(target,str) or not re.fullmatch(r'[a-f0-9]{12,64}',target):return {}
        state=json.loads((root/target/'route-state.json').read_text())
        domain=urlsplit(core.get('DOMAIN','')).hostname
        if state.get('domain')!=domain or state.get('network')!=memory.get('DOCKER_NET') or not isinstance(state.get('host_id'),int):return {}
        return {'recorded':True,'target':target,'domain':domain,'email':state.get('email','') if isinstance(state.get('email',''),str) else '','email_source':'npm' if state.get('email_source')=='npm' else 'login'}
    except (OSError,ValueError,TypeError,AttributeError):return {}
