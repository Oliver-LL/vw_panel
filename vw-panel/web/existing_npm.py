"""Configure one Vaultwarden route on an existing, locally detected NPM."""
import ipaddress
import json
import re
from urllib.parse import urlsplit
from pathlib import Path
from managed_proxy import ManagedProxyMixin

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
        result=self._npm_request('tokens',{'identity':self.email,'secret':self.password})
        if not isinstance(result,dict) or not isinstance(result.get('token'),str) or not result['token']:
            raise ValueError('NPM 登录失败，请检查管理邮箱、密码及账号权限。')
        return result['token']

    def _npm_request(self,path,data=None,token=None,method=None,timeout=15):
        if path=='nginx/certificates' and data is not None:
            data={**data,'meta':{**data.get('meta',{}),'letsencrypt_email':self.email,'letsencrypt_agree':True}}
        return super()._npm_request(path,data,token,method,timeout)

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
            saved=json.loads(state.read_text());saved['network']=self.network
            write_private(state,json.dumps(saved))
            self.emit('已在现有 NPM 写入密码库站点与 HTTPS。请实际打开密码库验证访问。')
        finally:
            self.password=''
