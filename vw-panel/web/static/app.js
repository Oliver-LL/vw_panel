'use strict';
const $=id=>document.getElementById(id);
const names={overview:'运行概览',vault:'密码库配置',mail:'邮件服务',backup:'备份与恢复',tasks:'任务与日志',settings:'面板设置',remote:'远程存储',restore:'备份与恢复',diagnostics:'诊断与状态',cleanup:'卸载与清理'};
let lastStatus=0;
let proxyLogPreference='';
let proxySourcePreference='auto',autoProxyTarget='',autoInstallMode='caddy';
let proxyEnvironment=null,proxyDetectionLoading=false,proxyDetectionSequence=0,proxyNetworkEdited=false,proxyManualNetwork='';
let csrf='', timer=null, refreshing=false, taskRunning=false, currentTask='', finishedTask='', cfg=null;
let cursor=0,generation='',raw='',consoleRunning=false,hiddenInput=false;
const dirty=new Set();
let authEpoch=0,authPhase='checking',authController=new AbortController();
const activeUploads=new Set();
function sessionChanged(){const error=new Error('');error.name='SessionChanged';return error}
function isSessionTransition(error){return error?.name==='SessionChanged'}
function invalidateAuth(phase){
    setVersionPopover(false);authEpoch++;authController.abort();authController=new AbortController();authPhase=phase;csrf='';
    clearInterval(timer);timer=null;clearTimeout(updateWatch);updateWatch=null;
    for(const xhr of activeUploads)xhr.abort();activeUploads.clear();
    for(const button of document.querySelectorAll('#workspace form button:not([type=button])'))if(!button.closest('#input-form'))button.disabled=false;
    refreshing=false;checkingUpdates=false;lastSubmittedForm=null;proxyEnvironment=null;proxyDetectionLoading=false;proxyDetectionSequence++;proxyNetworkEdited=false;proxyManualNetwork='';proxySourcePreference='auto';autoProxyTarget='';autoInstallMode='caddy';proxyLogPreference='';
    $('npm-credentials').hidden=true;$('npm-login-password').value='';$('npm-login-password').type='password';$('npm-login-email').value='';$('show-npm-credentials').textContent='查看 NPM 管理账号';
    return authEpoch;
}
async function sessionRequest(url,options={}){
    const epoch=authEpoch;
    try{const response=await fetch(url,{...options,signal:options.signal||authController.signal});if(epoch!==authEpoch)throw sessionChanged();return response}
    catch(error){if(epoch!==authEpoch)throw sessionChanged();if(error.name==='AbortError')throw Error('连接超时，请重试。');throw error}
}
function expireSession(){
    const phase=authPhase;
    showLogin(phase==='authenticated'?'登录状态已失效，请重新登录。':phase==='authenticating'?'登录状态未能保持，请确认允许 Cookie，并使用同一个访问地址。':'');
}

function notice(message,error=false){if(error&&isAuthMessage(message)&&authPhase==='authenticated'){$('notice').textContent='正在确认登录状态…';$('notice').hidden=false;$('notice').classList.remove('error');reconcileAuthNotice();return}if(authPhase!=='authenticated'){message='';error=false}$('notice').textContent=message;$('notice').hidden=!message;$('notice').classList.toggle('error',error)}
function route(){const requested=location.hash.slice(1);const page=requested==='restore'?'backup':requested in names?requested:'overview';if(requested==='restore'){history.replaceState(null,'','#backup');requestAnimationFrame(()=>{if(location.hash!=='#backup')return;const form=$('restore-form');revealGroup(form);form.closest('.config-group')?.scrollIntoView({block:'start'})})};document.querySelectorAll('[data-view]').forEach(v=>v.hidden=v.dataset.view!==page);document.querySelectorAll('[data-page]').forEach(v=>v.classList.toggle('active',v.dataset.page===page));$('breadcrumb').textContent=names[page];document.title=names[page]+' · Vaultwarden'}
addEventListener('hashchange',()=>{route();notice('')});route();
let authRecheck=null;
function isAuthMessage(message){return typeof message==='string'&&/^请先登录[。.!！]?$/.test(message.trim())}
async function verifyCurrentSession(epoch=authEpoch){
    if(epoch!==authEpoch)throw sessionChanged();
    if(authRecheck?.epoch===epoch)return authRecheck.promise;
    const request={epoch};
    request.promise=(async()=>{
        const response=await sessionRequest('/api/session',{credentials:'same-origin'});
        if(epoch!==authEpoch)throw sessionChanged();
        if(response.status===401)return null;
        const value=await response.json();if(epoch!==authEpoch)throw sessionChanged();
        if(isAuthMessage(value.error))return null;
        if(!response.ok||typeof value.csrf!=='string'||!value.csrf)throw Error('暂时无法确认登录状态，请稍后重试。');
        csrf=value.csrf;return value;
    })().finally(()=>{if(authRecheck===request)authRecheck=null});
    authRecheck=request;return request.promise;
}
async function reconcileAuthNotice(){
    const epoch=authEpoch;
    try{const valid=await verifyCurrentSession(epoch);if(epoch!==authEpoch)return;if(!valid){expireSession();return}if($('notice').textContent==='正在确认登录状态…')notice('')}
    catch(error){if(isSessionTransition(error)||epoch!==authEpoch)return;notice('暂时无法确认登录状态，请稍后重试。',true)}
}
async function api(path,data,retried=false){
    const epoch=authEpoch;const options={credentials:'same-origin',headers:{}};
    if(data!==undefined){options.method='POST';options.headers={'Content-Type':'application/json','X-CSRF-Token':csrf};options.body=JSON.stringify(data)}
    let response;try{response=await sessionRequest('/api/'+path,options)}catch(error){if(panelUpdating&&data===undefined&&authPhase==='authenticated')throw sessionChanged();throw error}
    if(epoch!==authEpoch)throw sessionChanged();
    let value=null;
    if(response.status!==401||path==='login'){
        try{value=await response.json()}catch(error){if(epoch!==authEpoch)throw sessionChanged();throw Error('服务器没有返回有效结果，请检查连接。')}
    }
    if(epoch!==authEpoch)throw sessionChanged();
    if(path!=='login'&&(response.status===401||isAuthMessage(value?.error))){
        if(authPhase==='authenticated'&&path!=='session'){
            const valid=await verifyCurrentSession(epoch);if(epoch!==authEpoch)throw sessionChanged();
            if(valid){
                if(data===undefined&&!retried)return api(path,undefined,true);
                throw Error('当前登录有效，但此请求未能完成，请重试。');
            }
        }
        expireSession();throw sessionChanged();
    }
    if(!response.ok){if(lastSubmittedForm&&(path.startsWith('config/')||path==='task/start'))revealGroup(lastSubmittedForm.querySelector(':invalid')||lastSubmittedForm);throw Error(value?.error||'操作失败')}
    return value;
}
function showLogin(message=''){
    invalidateAuth('anonymous');$('auth-loading').hidden=true;$('workspace').hidden=true;$('login').hidden=false;
    document.querySelectorAll('input[type=password]').forEach(v=>v.value='');$('login-error').textContent=typeof message==='string'?message:'';
    $('login-form').querySelector('button').disabled=false;$('login-form').querySelector('button').textContent='登录管理中心 →';
    raw='';cursor=0;generation='';$('output').textContent='';$('task-output').textContent='';dirty.clear();notice('');
}
async function enter(session,epoch=authEpoch){
    if(epoch!==authEpoch)throw sessionChanged();
    if(typeof session?.csrf!=='string'||!session.csrf)throw Error('登录状态不完整，请重新连接。');
    csrf=session.csrf;authPhase='authenticated';$('password').value='';$('login-error').textContent='';notice('');
    $('workspace').hidden=false;$('login').hidden=true;$('auth-loading').hidden=true;$('server-address').textContent=location.host;route();
    await Promise.all([loadConfig(),refreshStatus(),loadSettings(),poll()]);
    if(epoch!==authEpoch||!csrf)return;
    if(!timer)timer=setInterval(poll,1500);checkPanelUpdates(false);
}
async function restoreSession(){
    const epoch=invalidateAuth('checking');$('login').hidden=true;$('workspace').hidden=true;$('auth-loading').hidden=false;
    $('auth-spinner').hidden=false;$('auth-retry').hidden=true;$('auth-loading-title').textContent='正在连接管理面板';$('auth-loading-description').textContent='正在确认登录状态，请稍候。';
    const timeout=setTimeout(()=>{if(epoch===authEpoch)authController.abort()},15000);
    try{const session=await api('session');clearTimeout(timeout);await enter(session,epoch)}
    catch(error){if(isSessionTransition(error)||epoch!==authEpoch)return;authPhase='unavailable';$('auth-loading').hidden=false;$('auth-spinner').hidden=true;$('auth-retry').hidden=false;$('auth-loading-title').textContent='暂时无法连接面板';$('auth-loading-description').textContent='服务器可能正在重启或网络暂时不可用，请稍后重新连接。'}
    finally{clearTimeout(timeout)}
}
$('auth-retry').onclick=restoreSession;
$('login-form').onsubmit=async event=>{
    event.preventDefault();if(authPhase==='authenticating')return;
    const password=$('password').value;const epoch=invalidateAuth('authenticating');
    const button=$('login-form').querySelector('button');button.disabled=true;button.textContent='正在登录…';$('login-error').textContent='';
    try{await api('login',{password});const verified=await api('session');await enter(verified,epoch)}
    catch(error){if(isSessionTransition(error)||epoch!==authEpoch)return;authPhase='anonymous';$('login-error').textContent=error.name==='TypeError'?'暂时无法连接面板，请稍后重试。':error.message||'暂时无法登录，请稍后重试。'}
    finally{if(epoch===authEpoch){button.disabled=false;button.textContent='登录管理中心 →'}}
};
$('logout').onclick=async()=>{try{await api('logout',{});showLogin()}catch(error){if(isSessionTransition(error))return;notice(error.message,true)}};
for(const id of ['core-form','mail-form','backup-form','remote-form','advanced-form','push-form','notifications-form','targets-form']){$(id).addEventListener('input',()=>dirty.add(id));$(id).addEventListener('change',()=>dirty.add(id))}
function fill(id,values){if(dirty.has(id))return;const form=$(id);for(const [key,value]of Object.entries(values)){const field=form.elements.namedItem(key);if(!field)continue;if(field.type==='checkbox')field.checked=value===true||value==='true';else{if(field.tagName==='SELECT'&&value&&!Array.from(field.options).some(o=>o.value===value)){field.add(new Option('当前设置：'+value,value))}field.value=value}}}
function formatSize(bytes){return bytes>=1048576?(bytes/1048576).toFixed(1)+' MB':(bytes/1024).toFixed(1)+' KB'}
function proxyContext(value){return value?JSON.stringify([value.configured,value.mode,value.network,value.proxy_target,value.proxy?.kind]):''}
async function loadConfig(){try{const previousContext=proxyContext(cfg);cfg=await api('config');if(previousContext&&previousContext!==proxyContext(cfg)){proxyEnvironment=null;proxyLogPreference='';proxyDetectionSequence++;proxyDetectionLoading=false}const defaults={SIGNUPS_ALLOWED:!cfg.configured,INVITATIONS_ALLOWED:true,SENDS_ALLOWED:false};for(const key of Object.keys(defaults)){if(cfg.core[key]!=='')defaults[key]=cfg.core[key]}fill('core-form',{...cfg.core,...defaults,directory:cfg.directory,mode:cfg.mode,port:cfg.port,network:cfg.network});fill('mail-form',{SMTP_PORT:'587',SMTP_SECURITY:'starttls',...Object.fromEntries(Object.entries(cfg.core).filter(([,v])=>v!==''))});fill('backup-form',{CRON:'0 3 * * *',BACKUP_KEEP_DAYS:'30',TIMEZONE:'Asia/Shanghai',...Object.fromEntries(Object.entries(cfg.backup).filter(([,v])=>v!==''))});$('admin-badge').textContent=cfg.admin_set?'已设置 · 留空保留':'尚未设置';$('admin-badge').className='badge'+(cfg.admin_set?' success':'');for(const field of $('core-form').elements){if(['directory','mode','port','network'].includes(field.name))field.disabled=cfg.configured}$('topology-note').hidden=!cfg.configured;const smtp=$('mail-form').elements.SMTP_PASSWORD;smtp.placeholder=cfg.smtp_password_set?'已保存 · 留空保留原值':'填写邮箱授权码';$('backup-form').elements.ZIP_PASSWORD.placeholder=cfg.zip_password_set?'已保存 · 留空保留原值':'首次配置请设置加密密码';$('step-config').classList.toggle('done',cfg.configured);$('step-config').querySelector('b').textContent=cfg.configured?'已保存':'待配置';$('step-token').classList.toggle('done',cfg.admin_set);$('step-token').querySelector('b').textContent=cfg.admin_set?'已保护':'待设置';const url=cfg.core.DOMAIN;$('vault-url').textContent=url||'完成配置后显示';$('open-vault').hidden=!url;if(url&&/^https:\/\//.test(url))$('open-vault').href=url;if(cfg.remote){const remote=cfg.remote;const webdav=remote.type==='webdav';$('remote-current').textContent=remote.type==='local'?'当前目标：本机加密备份。连接 WebDAV 后，新的备份将发送到远程目录。':remote.name?'当前目标：'+remote.name+' · '+remote.directory:'尚未连接远程存储。请先保存备份计划，再连接 WebDAV。';if(webdav)fill('remote-form',{url:remote.url,user:remote.user,directory:remote.directory})}renderFiles(cfg.files);renderExtras(cfg);routeHelp();updateGroupSummaries();renderPresentation();if(!proxyEnvironment&&!proxyDetectionLoading)loadProxyEnvironment()}catch(e){if(isSessionTransition(e))return;notice(e.message,true)}}
function renderFiles(files){const box=$('backup-files');box.replaceChildren();if(!files.length){const empty=document.createElement('div');empty.className='empty';const title=document.createElement('strong');title.textContent='还没有本机备份';empty.append(title,document.createTextNode('保存备份计划并启用后，点击“立即备份”创建第一份。'));box.append(empty)}for(const file of files){const row=document.createElement('div');row.className='file-row';for(const [i,value]of [file.name,formatSize(file.size),new Date(file.modified*1000).toLocaleString('zh-CN',{hour12:false})].entries()){const span=document.createElement(i===0?'strong':'span');span.textContent=value;row.append(span)}const actions=document.createElement('div');actions.className='backup-file-actions';const download=document.createElement('a');download.textContent='下载';download.href='/api/backups/download?file='+encodeURIComponent(file.name);actions.append(download);if(/\.(zip|7z)$/i.test(file.name)){const restore=document.createElement('button');restore.type='button';restore.className='ghost';restore.textContent='选择恢复';restore.onclick=()=>{const form=$('restore-form');form.elements.file.value=file.name;form.elements.confirmation.value='';revealGroup(form);form.closest('.config-group').scrollIntoView({behavior:'smooth',block:'start'})};actions.append(restore)}row.append(actions);box.append(row)}const latest=files[0];$('latest-backup').textContent=latest?new Date(latest.modified*1000).toLocaleDateString('zh-CN'):'暂无归档';$('latest-detail').textContent=latest?latest.name:'启用自动备份，保护你的数据'}
function proxyNetworkChoices(){
    const choices=new Map();
    const target=$('core-form').elements.PROXY_TARGET.value;
    for(const proxy of proxyEnvironment?.proxies||[]){
        if(!proxy.running||target&&proxy.id!==target)continue;
        for(const network of proxy.networks||[]){if(!choices.has(network))choices.set(network,[]);choices.get(network).push(proxy.name+'（'+proxy.kind+'）')}
    }
    return choices;
}
function paintProxyEnvironment(){
    const form=$('core-form'),mode=form.elements.mode.value,network=form.elements.network,choices=proxyNetworkChoices(),select=$('proxy-choice');
    $('proxy-environment').hidden=!!cfg?.configured||proxySourcePreference==='auto';
    $('detect-proxy').disabled=proxyDetectionLoading;$('detect-proxy').textContent=proxyDetectionLoading?'正在检测…':'重新检测';
    select.replaceChildren(new Option('请选择反代使用的网络',''));
    for(const [name,proxies] of choices)select.add(new Option(proxies.join('、')+' · '+name,name));
    $('proxy-choice-field').hidden=mode!=='docker_ext'||!!cfg?.configured||!choices.size;
    select.disabled=!!cfg?.configured||proxyDetectionLoading;
    if(!cfg?.configured&&mode==='docker_ext'&&!proxyNetworkEdited){
        if(!network.value||network.value==='vaultwarden_net'||network.value===network.dataset.detected){
            network.value=proxyEnvironment?.complete&&choices.size===1?[...choices.keys()][0]:'';
            network.dataset.detected=network.value;
        }
    }
    select.value=choices.has(network.value)?network.value:'';
    let message='点击“重新检测”读取服务器反代和网络。';
    if(proxyDetectionLoading)message='正在检测服务器的反代、Docker 网络和 80 / 443 端口…';
    else if(proxyEnvironment){
        const env=proxyEnvironment,descriptions=(env.proxies||[]).map(p=>p.name+'（'+p.kind+(p.running?'':'，已停止')+'）');
        if(!env.available||!env.complete)message=env.message||'检测未完成，请重试或手动填写已确认的网络。';
        else if(descriptions.length)message='检测到 '+descriptions.join('、')+'。'+(choices.size===1?'可自动使用网络 '+[...choices.keys()][0]+'。':choices.size>1?'存在多个网络，请选择使用哪一个。':'没有可直接接入的自定义网络，请先为反代配置网络。');
        else message='未检测到可识别的代理容器。'+(env.busy_ports?.length?'80 / 443 中已有端口被占用，请确认宿主机反代或其他占用服务。':'新服务器可选择安装 Caddy、NPM 或 Nginx。如有未识别到的反代，也可手动填写其网络。');
        if(env.busy_ports?.length)message+=' 已占用端口：'+env.busy_ports.join(' / ')+'。';
        else if(!env.ports_checked)message+=' 端口占用情况未能完整确认。';
    }
    $('proxy-detection-message').textContent=message;
}
async function loadProxyEnvironment(){
    const epoch=authEpoch,sequence=++proxyDetectionSequence;proxyDetectionLoading=true;routeHelp();
    try{const env=await api('proxy/environment');if(epoch!==authEpoch||sequence!==proxyDetectionSequence)return;proxyEnvironment=env}
    catch(error){if(isSessionTransition(error)||epoch!==authEpoch)return;proxyEnvironment={available:false,complete:false,proxies:[],message:'无法完成反代检测，请重试；也可手动填写已确认的网络。'}}
    finally{if(epoch===authEpoch&&sequence===proxyDetectionSequence){proxyDetectionLoading=false;routeHelp()}}
}
const proxyModes={install:[['caddy','Caddy · 自动 HTTPS，配置简单'],['npm','Nginx Proxy Manager · 可视化管理'],['nginx','Nginx · 自动配置 HTTPS 与续期']],existing:[['docker_ext','已有容器反代（NPM / Caddy 等）'],['host_ext','已有宿主机反代（Nginx / Caddy 等）']]};
function proxySource(mode){return ['caddy','npm','nginx'].includes(mode)?'install':'existing'}
function syncProxyModes(source,selected){
    const select=$('core-form').elements.mode,options=proxyModes[source];
    if(Array.from(select.options).map(o=>o.value).join(',')!==options.map(o=>o[0]).join(',')){select.replaceChildren(...options.map(([value,label])=>new Option(label,value)))}
    select.value=options.some(o=>o[0]===selected)?selected:options[0][0];
    $('proxy-source').disabled=!!cfg?.configured;
    $('proxy-mode-label').textContent=cfg?.configured?'当前反代配置':source==='install'?'安装的反代':'已有反代类型';
    $('proxy-mode-help').textContent=cfg?.configured?'沿用已保存的反代配置。':source==='install'?'安装和 HTTPS 配置由面板任务完成。':'已自动带出接入类型，也可以手动调整。';
}
function resolveSetupChoice(){
    const env=proxyEnvironment,recommend=env?.recommendation;
    if(autoProxyTarget){const proxy=env?.proxies?.find(p=>p.id===autoProxyTarget&&p.running);if(proxy)return {state:'existing',mode:proxy.source==='host'?'host_ext':'docker_ext',target:proxy.id,name:proxy.name,kind:proxy.kind};autoProxyTarget=''}
    return recommend||{state:'unknown',reason:proxyDetectionLoading?'正在检测服务器环境…':'尚未完成检测，请重新检测或手动指定。'};
}
function paintAutoSetup(choice){
    const source=$('proxy-source'),saved=source.querySelector('option[value=configured]');
    if(cfg?.configured&&proxySource(cfg.mode)==='install'){
        const labels={caddy:'Caddy 自动 HTTPS',npm:'NPM（面板管理）',nginx:'Nginx（面板管理）'};
        const option=saved||new Option('', 'configured');option.textContent='当前配置：'+(labels[cfg.mode]||cfg.mode);if(!saved)source.add(option);
        source.value='configured';
    }else{
        saved?.remove();
        source.value=cfg?.configured?'existing':proxySourcePreference==='auto'?(choice.state==='existing'||choice.state==='choose'?'existing':choice.state==='install'?'install':'auto'):proxySourcePreference;
    }
    source.disabled=!!cfg?.configured;
    const active=!cfg?.configured&&proxySourcePreference==='auto';
    const needsChoice=choice.state==='choose',unresolved=choice.state==='unknown';
    $('proxy-auto-status').hidden=!active||proxyDetectionLoading||(!needsChoice&&!unresolved);
    $('auto-proxy-target-field').hidden=!active||(!needsChoice&&!(choice.state==='existing'&&(proxyEnvironment?.proxies?.length||0)>1));
    const existingOption=source.querySelector('option[value=existing]'),installOption=source.querySelector('option[value=install]');
    const kind=choice.kind==='Nginx Proxy Manager'?'NPM':choice.kind;
    existingOption.textContent=active&&choice.state==='existing'?'使用已有反代（检测到 '+kind+'）':active&&needsChoice?'使用已有反代（检测到多个或需确认）':'使用已有反代';
    installOption.textContent=active&&choice.state==='install'?'安装新的反代（未检测到反代）':'安装新的反代';
    source.title=active&&choice.state==='existing'?'检测到 '+choice.kind+' · '+choice.name:source.selectedOptions[0]?.textContent||'';
    let help=cfg?.configured?'沿用当前已保存的配置。':proxyDetectionLoading?'正在确认服务器环境…':active&&choice.state==='existing'?'已自动选中，可按需要调整接入类型。':active&&choice.state==='install'?'可在下方选择 Caddy、NPM 或 Nginx。':active?'请根据检测结果确认，或手动选择接入方式。':'已使用你的手动选择。';
    if(proxyEnvironment?.docker_installed===false)help+=' 请先在运行概览安装 Docker。';
    $('proxy-source-help').textContent=help;
    $('auto-detect-proxy').disabled=proxyDetectionLoading;$('auto-detect-proxy').textContent=proxyDetectionLoading?'检测中…':'重新检测';$('refresh-proxy-logs').disabled=proxyDetectionLoading;
    if(!active)return;
    const title=$('proxy-auto-title'),reason=$('proxy-auto-reason'),select=$('auto-proxy-target');
    select.replaceChildren(new Option('请选择检测到的反代',''));
    for(const proxy of proxyEnvironment?.proxies||[]){const option=new Option(proxy.name+'（'+proxy.kind+(proxy.running?'':'，已停止')+'）',proxy.id);option.disabled=!proxy.running||(proxy.source!=='host'&&!proxy.networks?.length);select.add(option)}
    select.value=choice.target||'';
    $('auto-proxy-target-field').hidden=choice.state!=='choose'&&!(choice.state==='existing'&&(proxyEnvironment?.proxies?.length||0)>1);
    if(proxyDetectionLoading){title.textContent='正在检测服务器环境…';reason.textContent='正在识别已有反代和入口端口，无需先判断服务器是否安装过反代。'}
    else if(choice.state==='existing'){title.textContent='已自动选择：使用 '+choice.kind;reason.textContent='检测到 '+choice.name+'，将接入现有反代，不会重复安装反代。'+(choice.mode==='docker_ext'?'面板会读取它的 Docker 网络。':'使用本机转发端口接入现有服务。')}
    else if(choice.state==='install'){title.textContent='入口空闲，已选择新装反代';reason.textContent='推荐配置简单的 Caddy，也可以在下方改选 NPM 或 Nginx。'}
    else{title.textContent=choice.state==='choose'?'请选择检测到的反代':'环境暂未确认';reason.textContent=choice.reason||'请重新检测，或在服务器环境中切换到手动选择。'}
    if(proxyEnvironment?.docker_installed===false)reason.textContent+=' Vaultwarden 还需要 Docker，请先在运行概览安装。'
}
function paintProxyLogSources(){
    const select=$('proxy-log-source');if(!select)return;
    const sources=proxyEnvironment?.log_sources||proxyEnvironment?.proxies||[];
    select.replaceChildren(new Option(sources.length?'请选择日志来源':proxyDetectionLoading?'正在检测反代…':'未检测到可读取的反代',''));
    for(const source of sources)select.add(new Option(source.name+'（'+source.kind+(source.running?'':'，已停止')+'）',source.id));
    const target=sources.find(p=>p.id===proxyLogPreference)||sources.find(p=>p.id===cfg?.proxy_target)||sources.find(p=>p.id===$('core-form').elements.PROXY_TARGET.value)||(sources.length===1?sources[0]:null);
    select.value=target?.id||'';select.disabled=proxyDetectionLoading||!sources.length;
    for(const card of document.querySelectorAll('[data-task="proxy-logs"]')){const title=card.querySelector('strong');if(title)title.textContent=target?target.kind+' 日志':'反代日志';const detail=card.querySelector('small');if(detail)detail.textContent=target?'日志来源：'+target.name:'先检测并选择实际使用的服务'}
    $('proxy-log-hint').textContent=target?'将读取 '+target.name+' 的日志。':sources.length>1?'检测到多个反代，请选择实际使用的服务。':'尚未部署或未能识别反代。可重新检测服务器环境。';
}
function renderManagedProxy(){
    const proxy=cfg?.proxy||{},npm=proxy.kind==='npm',managed=!!proxy.managed;
    $('managed-proxy-info').hidden=!managed;$('npm-access').hidden=!npm;
    $('remove-proxy-field').hidden=!managed||$('remove-form').elements.scope.value==='backup';
    if(npm){
        const url=new URL(location.href);url.protocol='http:';url.port=String(proxy.admin_port);url.pathname='/';url.search='';url.hash='';$('open-npm').href=url.href;
        $('managed-proxy-description').textContent='NPM 由面板安装，管理邮箱为 '+proxy.email+'，管理端口为 '+proxy.admin_port+'。部署完成后可打开管理界面；若无法访问，请检查该端口是否放行。';
    }else if(managed)$('managed-proxy-description').textContent='Nginx 与证书续期服务由面板管理；修改域名后保存并应用配置，证书申请失败会恢复之前的 Nginx 配置。';
}
function routeHelp(){
    const form=$('core-form');let mode=form.elements.mode.value;
    const choice=resolveSetupChoice();
    if(!cfg?.configured&&proxySourcePreference==='auto'){
        if(choice.state==='install'){syncProxyModes('install',autoInstallMode);mode=form.elements.mode.value;form.elements.PROXY_TARGET.value=''}
        else if(choice.state==='existing'){syncProxyModes('existing',choice.mode);mode=form.elements.mode.value;form.elements.PROXY_TARGET.value=choice.target||''}
        else{form.elements.mode.replaceChildren(new Option(proxyDetectionLoading?'正在检测…':'请先确认服务器环境',''));mode='';form.elements.PROXY_TARGET.value='';$('proxy-source').value='auto';$('proxy-source').disabled=false;$('proxy-mode-label').textContent='检测结果'}
    }else{syncProxyModes(proxySource(mode),mode);mode=form.elements.mode.value;if(!cfg?.configured)form.elements.PROXY_TARGET.value=''}
    form.elements.mode.required=true;form.elements.mode.closest('label').hidden=false;
    form.elements.mode.setCustomValidity(!cfg?.configured&&proxySourcePreference==='auto'&&!mode?'请先完成服务器环境检测，或手动选择接入方式。':'');
    paintAutoSetup(choice);paintProxyLogSources();
    $('npm-port-field').hidden=mode!=='npm';$('npm-password-field').hidden=mode!=='npm';
    form.elements.NPM_ADMIN_PORT.disabled=mode!=='npm'||!!cfg?.configured;form.elements.NPM_ADMIN_PASSWORD.disabled=mode!=='npm';
    form.elements.SERVER_ADMIN_EMAIL.required=['npm','nginx'].includes(mode);
    $('npm-password-label').textContent=cfg?.configured?'NPM 当前管理密码':'NPM 管理密码';
    $('npm-password-help').textContent=cfg?.configured?'留空保留。若在 NPM 改过密码，请在这里填写当前密码，以便面板继续自动配置站点。':'留空自动生成随机密码；保存后可在此页面查看管理账号。';
    if(cfg?.proxy?.admin_port)form.elements.NPM_ADMIN_PORT.value=cfg.proxy.admin_port;
    renderManagedProxy();
    form.elements.port.closest('label').hidden=mode!=='host_ext';form.elements.network.closest('label').hidden=mode!=='docker_ext';
    form.elements.network.required=mode==='docker_ext'&&!cfg?.configured;
    if(!cfg?.configured){if(mode!=='docker_ext')form.elements.network.value=cfg?.network||'vaultwarden_net';else if(proxyNetworkEdited)form.elements.network.value=proxyManualNetwork}
    paintProxyEnvironment();
    if(!cfg?.configured&&mode==='npm'){$('route-help').textContent='新装 NPM：自动启动代理、创建管理账号、配置密码库站点并申请 HTTPS；无需再手动新增转发规则。需要域名已解析到本机，80 / 443 和管理端口可用。';return}
    if(!cfg?.configured&&mode==='nginx'){$('route-help').textContent='新装 Nginx：自动生成反代配置、申请 HTTPS 并启用证书续期；无需手写 Nginx 配置。需要域名已解析到本机，80 / 443 端口可用。';return}
    if(cfg?.configured&&['npm','nginx'].includes(mode)){$('route-help').textContent=mode==='npm'?'当前由面板安装的 NPM 提供 HTTPS；保存并应用配置会自动更新密码库站点。':'当前由面板安装的 Nginx 提供 HTTPS，证书定时续期。保存后应用配置以生效。';return}
    if(cfg?.configured){$('route-help').textContent=mode==='caddy'?'当前使用 Caddy 自动 HTTPS，代理目标为 vaultwarden:80。更换域名后保存并应用配置。':mode==='host_ext'?'当前使用宿主机反向代理，转发目标为 http://127.0.0.1:'+form.elements.port.value+'，域名和证书由现有反代管理。':'当前通过 Docker 网络 '+form.elements.network.value+' 接入容器反代，转发目标为 http://vaultwarden:80，域名和证书由现有反代管理。';return}
    $('route-help').textContent=mode==='caddy'?'无需预装反代：部署时自动启动 Caddy 容器并申请 HTTPS 证书。请将域名解析到本服务器并放行 80 / 443；若端口已有服务，请选择已有反代方式。':mode==='host_ext'?'使用服务器上已经运行的 Nginx、Caddy 等反代，转发到 http://127.0.0.1:'+form.elements.port.value+'，域名和 HTTPS 由现有反代配置。此选项不会安装宿主机反代。':'使用已运行的 NPM、Caddy、Traefik 等代理容器，Vaultwarden 加入所选网络；在现有反代中添加密码库域名，目标填写 http://vaultwarden:80 并配置 HTTPS。此选项不会安装 NPM，也不会改写已有反代配置。';
}
$('proxy-source').onchange=()=>{proxySourcePreference=$('proxy-source').value;if(proxySourcePreference!=='auto')syncProxyModes(proxySourcePreference);dirty.add('core-form');routeHelp();if((proxySourcePreference==='auto'||!proxyEnvironment)&&!proxyDetectionLoading)loadProxyEnvironment()};
$('core-form').elements.mode.onchange=()=>{if(!cfg?.configured){proxySourcePreference=proxySource($('core-form').elements.mode.value);if(proxySourcePreference==='install')autoInstallMode=$('core-form').elements.mode.value}routeHelp();if(!cfg?.configured&&!proxyEnvironment&&!proxyDetectionLoading)loadProxyEnvironment()};
$('core-form').elements.port.oninput=routeHelp;
$('core-form').elements.network.oninput=()=>{proxyNetworkEdited=true;proxyManualNetwork=$('core-form').elements.network.value;routeHelp()};
$('proxy-choice').onchange=()=>{if(!$('proxy-choice').value)return;$('core-form').elements.network.value=$('proxy-choice').value;proxyNetworkEdited=true;proxyManualNetwork=$('proxy-choice').value;dirty.add('core-form');routeHelp()};
$('proxy-log-source').onchange=()=>{proxyLogPreference=$('proxy-log-source').value;paintProxyLogSources()};$('refresh-proxy-logs').onclick=loadProxyEnvironment;
$('detect-proxy').onclick=loadProxyEnvironment;$('auto-detect-proxy').onclick=loadProxyEnvironment;
$('auto-proxy-target').onchange=()=>{autoProxyTarget=$('auto-proxy-target').value;proxyNetworkEdited=false;proxyManualNetwork='';dirty.add('core-form');routeHelp()};
$('remove-form').elements.scope.addEventListener('change',()=>{if($('remove-form').elements.scope.value==='backup')$('remove-form').elements.remove_proxy.checked=false;renderManagedProxy()});
$('show-npm-credentials').onclick=async()=>{
    if(!$('npm-credentials').hidden){$('npm-credentials').hidden=true;$('npm-login-password').value='';$('show-npm-credentials').textContent='查看 NPM 管理账号';return}
    try{const credentials=await api('proxy/credentials',{});$('npm-login-email').value=credentials.email;$('npm-login-password').value=credentials.password;$('npm-login-password').type='text';$('npm-credentials').hidden=false;$('show-npm-credentials').textContent='隐藏 NPM 管理账号'}catch(error){if(!isSessionTransition(error))notice(error.message,true)}
};

function formValues(id){const values={};for(const field of $(id).elements){if(!field.name)continue;values[field.name]=field.type==='checkbox'?field.checked:field.value}return values}
for(const [id,section]of [['core-form','core'],['mail-form','mail'],['backup-form','backup']]){$(id).onsubmit=async event=>{event.preventDefault();const epoch=authEpoch;const form=event.currentTarget;const button=form.querySelector('button[type=submit]')||form.querySelector('button');button.disabled=true;try{const data=formValues(id);const result=await api('config/'+section,data);dirty.delete(id);form.querySelectorAll('input[type=password]').forEach(v=>v.value='');notice(result.message);await loadConfig()}catch(e){if(isSessionTransition(e))return;notice(e.message,true)}finally{if(epoch===authEpoch)button.disabled=false}}}
async function refreshStatus(){try{const status=await api('status');environment=status;lastStatus=Date.now();for(const [name,id]of [['vaultwarden','vault'],['vaultwarden-backup','backup']]){const row=status.containers.find(r=>r.Names===name),active=row&&row.State==='running';$(id+'-state').textContent=!status.available?(status.docker_installed===false?'环境未就绪':'状态未知'):!row?'尚未部署':active?'运行中':'已停止';$(id+'-state').classList.toggle('good',!!active);$(id+'-detail').textContent=row?row.Status:status.message||'完成配置后即可启动';if(id==='vault'){$('step-deploy').classList.toggle('done',!!active);$('step-deploy').querySelector('b').textContent=active?'已运行':'待部署';$('setup-heading').textContent=active?'你的密码库，正在安心运行。':'三步，拥有自己的密码库。'}}$('docker-help').hidden=status.available;renderPresentation()}catch(e){if(isSessionTransition(e))return;environment={unknown:true,available:false,docker_installed:null,containers:[]};for(const id of ['vault','backup']){$(id+'-state').textContent='状态未知';$(id+'-state').classList.remove('good');$(id+'-detail').textContent='暂时无法读取服务状态'}renderPresentation();notice(e.message,true)}}
async function startTask(action,extra={}){const epoch=authEpoch;if(action==='proxy-logs'){if(!$('proxy-log-source').value){location.hash='diagnostics';revealGroup($('proxy-log-source'));notice('请先选择实际检测到的反代日志来源。');return false}extra={...extra,proxy_id:$('proxy-log-source').value}}if(panelUpdating){notice('面板正在升级，请完成后再操作。',true);return false}if(taskRunning){location.hash='tasks';notice('已有任务正在执行，请等待完成。');return false}if(((action==='deploy'&&hasUnsaved('core'))||(action==='backup-start'&&hasUnsaved('backup')))){notice('有尚未保存的配置，请先保存，再应用。',true);return false}const confirmations={'upgrade-backup':'升级会重建备份容器，确认继续？',stop:'停止后客户端将暂时无法同步。确认停止密码库？',upgrade:'升级会下载最新镜像并重建容器，期间服务短暂不可用。建议先完成一次备份，确认继续？','backup-stop':'暂停后将不再按计划自动备份。确认暂停？','install-docker':'将通过 Docker 官方安装器安装 Docker 和 Compose，并修改服务器系统软件。确认安装？'};if(confirmations[action]&&!confirm(confirmations[action]))return false;try{await api('task/start',{action,confirmed:true,...extra});location.hash='tasks';notice('任务已开始，可以在此查看执行结果。');await poll();if(epoch!==authEpoch)return false;return true}catch(e){if(isSessionTransition(e))return;notice(e.message,true);return false}}
document.querySelectorAll('[data-task]').forEach(button=>button.onclick=()=>startTask(button.dataset.task));
$('log-service').onchange=()=>{$('view-service-logs').dataset.task=$('log-service').value};
$('remote-form').onsubmit=async event=>{event.preventDefault();const form=event.currentTarget;if(!confirm('将验证 WebDAV 连接并切换备份目标。原文件保留，配置完成后需应用备份服务。确认继续？'))return;if(await startTask('remote-save',formValues('remote-form'))){dirty.delete('remote-form');form.elements.password.value=''}};
$('token-form').onsubmit=async event=>{event.preventDefault();const form=event.currentTarget;if(form.elements.password.value!==form.elements.confirmation.value)return notice('两次输入的管理口令不一致。',true);if(form.elements.password.value.length<12)return notice('管理口令至少需要 12 个字符。',true);if(await startTask('admin-token',{password:form.elements.password.value}))form.reset()};
function paintTask(job){presentationJob=job;taskRunning=job.state==='running';currentTask=job.id;$('task-title').textContent=job.title;$('task-badge').textContent=({idle:'等待操作',running:'执行中',success:'已完成',failed:'执行失败'})[job.state]||job.state;$('task-badge').className='badge '+job.state;$('task-spinner').hidden=!taskRunning;$('task-description').textContent=taskRunning?'任务在后台执行。镜像下载可能需要几分钟，请耐心等待。':job.state==='success'?'操作已完成。配置和服务状态已刷新。':job.state==='failed'?'请根据下面的错误处理后重试。':'从配置页面或概览发起操作。';$('task-output').textContent=plain(job.output||'暂无输出');$('task-error').hidden=!job.error;$('task-error').textContent=job.error||'';if(job.state==='failed'||['logs','backup-logs','proxy-logs'].includes(job.action))$('task-details').open=true;document.querySelectorAll('[data-task]').forEach(b=>b.disabled=taskRunning||panelUpdating);renderPresentation();syncVersionPopover()}
async function poll(){if(refreshing||!csrf)return;const epoch=authEpoch;refreshing=true;try{const job=await api('task');paintTask(job);if(job.result)renderResult(job);if(Date.now()-lastStatus>15000)await refreshStatus();if(epoch!==authEpoch)return;if(job.id&&job.state!=='running'&&finishedTask!==job.id){finishedTask=job.id;await Promise.all([loadConfig(),refreshStatus()])}if(epoch!==authEpoch)return;if($('advanced').open||consoleRunning)await pollConsole()}catch(e){if(isSessionTransition(e))return;notice(e.message,true)}finally{if(epoch===authEpoch)refreshing=false}}
$('refresh').onclick=async()=>{const epoch=authEpoch;await Promise.all([loadConfig(),refreshStatus(),poll()]);if(epoch!==authEpoch)return;notice('状态已刷新，未保存的表单已保留。')};$('refresh-files').onclick=loadConfig;
async function loadSettings(){try{const settings=await api('settings');settingsSnapshot=settings;$('domains').textContent=settings.origins.length?'已允许访问：'+settings.origins.join('、'):'尚未添加域名，当前可通过 IP 访问。';$('proxy-help').textContent='宿主机反向代理目标：http://127.0.0.1:'+settings.port+'。Nginx 需保留原始 Host（含端口）：proxy_set_header Host $http_host;';renderPresentation()}catch(e){if(isSessionTransition(e))return;notice(e.message,true)}}
$('domain-form').onsubmit=async event=>{event.preventDefault();try{await api('domain',{origin:$('domain').value.trim()});notice('此域名已允许访问面板。DNS 与 HTTPS 是否可用，请通过实际访问确认。');await loadSettings()}catch(e){if(isSessionTransition(e))return;notice(e.message,true)}};
function plain(s){return s.replace(/\x1b\][\s\S]*?(?:\x07|\x1b\\)/g,'').replace(/\x1b\[[0-?]*[ -/]*[@-~]/g,'').replace(/\r\n/g,'\n').split('\n').map(line=>line.split('\r').pop().replace(/[\x00-\x08\x0b-\x1f\x7f]/g,'')).join('\n')}
async function pollConsole(){const state=await api('terminal?cursor='+cursor+'&generation='+generation);if(state.reset)raw='';raw=(raw+state.output).slice(-524288);cursor=state.cursor;generation=state.generation;consoleRunning=state.running;$('output').textContent=plain(raw)||'高级控制台尚未启动';$('console-status').textContent=consoleRunning?'运行中':'未运行';$('terminal-input').disabled=!consoleRunning;$('send').disabled=!consoleRunning;$('terminal-input').type=(!state.echo||hiddenInput)?'password':'text';if($('follow').checked)$('output').scrollTop=$('output').scrollHeight}
$('advanced').ontoggle=()=>{if($('advanced').open&&csrf)pollConsole().catch(e=>{if(!isSessionTransition(e))notice(e.message,true)})};
$('start').onclick=async()=>{try{await api('terminal/start',{});await pollConsole()}catch(e){if(isSessionTransition(e))return;notice(e.message,true)}};
$('stop').onclick=async()=>{if(!consoleRunning||!confirm('结束会中断当前脚本操作，确认继续？'))return;try{await api('terminal/stop',{});await pollConsole();await refreshStatus()}catch(e){if(isSessionTransition(e))return;notice(e.message,true)}};
async function send(value){try{await api('terminal/input',{text:value});await pollConsole()}catch(e){if(isSessionTransition(e))return;notice(e.message,true)}}
$('input-form').onsubmit=async event=>{event.preventDefault();const field=$('terminal-input'),value=field.value;field.value='';await send(value+'\n')};$('enter').onclick=()=>send('\n');$('interrupt').onclick=()=>{if(confirm('确认中断当前高级操作？'))send('\x03')};$('secret').onclick=()=>{hiddenInput=!hiddenInput;$('secret').textContent=hiddenInput?'显示输入':'隐藏输入';pollConsole().catch(e=>{if(!isSessionTransition(e))notice(e.message,true)})};


// Native settings for every original service option.
const extraDefaults={WEBSOCKET_ENABLED:true,WEB_VAULT_ENABLED:true,EMERGENCY_ACCESS_ALLOWED:true,ENABLE_DB_WAL:true,ADMIN_RATELIMIT_MAX_BURST:'5',ADMIN_RATELIMIT_SECONDS:'60',ADMIN_SESSION_LIFETIME:'20',TZ:'Asia/Shanghai'};
let wizardId='',resultSeen='',lastLive=0,remoteBrowse={};
function renderExtras(config){const extra=config.extras;if(!extra)return;fill('advanced-form',{...extraDefaults,...Object.fromEntries(Object.entries(extra.advanced).filter(([,v])=>v!==''))});fill('push-form',extra.push);fill('notifications-form',{MAIL_SMTP_ENABLE:false,MAIL_WHEN_SUCCESS:false,MAIL_WHEN_FAILURE:true,MAIL_FORCE_THREAD:false,...extra.notifications,...extra.notification_smtp});const box=$('remote-list');box.replaceChildren();if(!extra.remotes.length){const empty=document.createElement('p');empty.className='empty';empty.textContent='还没有配置存储。下方可以新增，也可以在备份页面快速连接 WebDAV。';box.append(empty)}for(const remote of extra.remotes){const row=document.createElement('article');row.className='remote-row';const heading=document.createElement('div');heading.className='remote-row-head';const strong=document.createElement('strong');strong.textContent=remote.name;const badge=document.createElement('span');badge.className='badge';badge.textContent=remote.type;heading.append(strong,badge);row.append(heading);const toolbar=document.createElement('div');toolbar.className='button-row';for(const [action,label]of [['remote-check','验证连接'],['remote-about','容量与身份'],['reauthorize','重新配置 / 授权'],['remote-delete','删除配置']]){const button=document.createElement('button');button.className='secondary';button.textContent=label;button.onclick=()=>{if(action==='reauthorize'){const form=$('wizard-start-form');form.elements.name.value=remote.name;if(!Array.from(form.elements.type.options).some(o=>o.value===remote.type))form.elements.type.add(new Option(remote.type,remote.type));form.elements.type.value=remote.type;revealGroup(form);form.scrollIntoView({behavior:'smooth'});return}let confirmation='';if(action==='remote-delete'){confirmation=prompt('删除配置不会删除远程文件。输入 DELETE 确认删除 '+remote.name)||'';if(confirmation!=='DELETE')return}const target=extra.targets.find(v=>v.name===remote.name);startTask(action,{name:remote.name,directory:target?target.directory:'/VaultBackup',confirmation})};toolbar.append(button)}row.append(toolbar);const details=document.createElement('details'),summary=document.createElement('summary'),pre=document.createElement('pre');summary.textContent='查看脱敏配置';pre.textContent=JSON.stringify(remote.fields,null,2);details.append(summary,pre);row.append(details);box.append(row)}if(!dirty.has('targets-form')){$('target-rows').replaceChildren();for(const target of extra.targets)addTarget(target)}const select=$('remote-browse-form').elements.name,previous=select.value;select.replaceChildren();for(const remote of extra.remotes)select.add(new Option(remote.name+' · '+remote.type,remote.name));if(extra.remotes.some(r=>r.name===previous))select.value=previous;const restoreSelect=$('restore-form').elements.file,current=restoreSelect.value;restoreSelect.replaceChildren(new Option('选择一份备份',''));for(const file of config.files.filter(f=>/\.(zip|7z)$/i.test(f.name)))restoreSelect.add(new Option(file.name+' · '+formatSize(file.size),file.name));if(Array.from(restoreSelect.options).some(o=>o.value===current))restoreSelect.value=current;}
for(const [id,section]of [['advanced-form','advanced'],['push-form','push'],['notifications-form','notifications']]){$(id).onsubmit=async event=>{event.preventDefault();const epoch=authEpoch;const form=event.currentTarget,button=form.querySelector('button[type=submit]');button.disabled=true;try{const result=await api('config/'+section,formValues(id));dirty.delete(id);form.querySelectorAll('input[type=password]').forEach(i=>i.value='');notice(result.message);await loadConfig()}catch(e){if(isSessionTransition(e))return;notice(e.message,true)}finally{if(epoch===authEpoch)button.disabled=false}}}
function addTarget(target={}){const row=document.createElement('div');row.className='target-row';const select=document.createElement('select');select.setAttribute('aria-label','备份目标存储');for(const remote of cfg?.extras?.remotes||[])select.add(new Option(remote.name,remote.name));if(target.name)select.value=target.name;const directory=document.createElement('input');directory.value=target.directory||'/VaultBackup';directory.placeholder='远程目录';directory.setAttribute('aria-label','备份保存目录');const remove=document.createElement('button');remove.type='button';remove.className='ghost danger-text';remove.textContent='移除';remove.onclick=()=>{row.remove();dirty.add('targets-form')};row.append(select,directory,remove);$('target-rows').append(row)}
$('add-target').onclick=()=>{addTarget();dirty.add('targets-form')};$('targets-form').onsubmit=async event=>{event.preventDefault();const targets=Array.from($('target-rows').children).map(row=>({name:row.querySelector('select').value,directory:row.querySelector('input').value}));try{await api('config/targets',{targets});dirty.delete('targets-form');notice('备份目标已保存，请应用备份配置。');await loadConfig()}catch(e){if(isSessionTransition(e))return;notice(e.message,true)}};
$('reload-remotes').onclick=loadConfig;$('load-providers').onclick=()=>startTask('remote-providers');
$('wizard-start-form').onsubmit=async event=>{event.preventDefault();const values=formValues('wizard-start-form');if(cfg?.extras?.remotes.some(r=>r.name===values.name)&&!confirm('将重新配置 '+values.name+'，向导完成前不会修改原配置。确认继续？'))return;await startTask('remote-wizard',values)};
$('wizard-question-form').onsubmit=async event=>{event.preventDefault();const value=$('wizard-answer').value;if(await startTask('remote-continue',{wizard_id:wizardId,answer:value}))$('wizard-answer').value=''};
function renderHelp(box,help){const regex=/https?:\/\/[^\s<>"']+/g;let index=0;for(const match of help.matchAll(regex)){box.append(document.createTextNode(help.slice(index,match.index)));const link=document.createElement('a');link.href=match[0];link.target='_blank';link.rel='noopener noreferrer';link.textContent=match[0];box.append(link);index=match.index+match[0].length}box.append(document.createTextNode(help.slice(index)))}
function renderResult(job){if(resultSeen===job.id)return;resultSeen=job.id;const result=job.result;if(result.providers){const select=$('wizard-start-form').elements.type,old=select.value;select.replaceChildren();for(const p of result.providers)select.add(new Option(p.description+' ('+p.name+')',p.name));select.value=old;revealGroup($('wizard-start-form'));location.hash='remote';notice('全部存储类型已加载。')}if(result.question){wizardId=result.wizard_id;const option=result.question,box=$('wizard-question');box.replaceChildren();const title=document.createElement('label');title.className='question-label';title.htmlFor='wizard-answer';title.textContent=({client_id:'客户端 ID（通常可留空）',client_secret:'客户端密钥',config_token:'授权令牌',config_is_local:'使用服务器上的浏览器授权（远程服务器固定为否）',host:'服务器地址',user:'登录账号',pass:'登录密码',port:'端口',vendor:'服务商类型',scope:'授权范围',key_file:'私钥文件路径'})[option.Name]||option.Name;const help=document.createElement('div');help.className='question-help';renderHelp(help,option.Help||'');let input;if(option.Exclusive&&option.Examples?.length){input=document.createElement('select');for(const item of option.Examples)input.add(new Option(item.Help+' · '+item.Value,item.Value))}else{input=document.createElement('input');input.type=option.IsPassword||/token|secret|pass/i.test(option.Name)?'password':'text';input.maxLength=60000;if(option.Examples?.length){const hints=document.createElement('p');hints.className='muted';hints.textContent='可选值：'+option.Examples.map(v=>v.Value+' ('+v.Help+')').join('、');box.append(hints)}}input.id='wizard-answer';input.required=!!option.Required;input.value=option.Default===null?'':String(option.Default??'');if(option.Name==='config_is_local'){input.value='false';input.disabled=true}box.append(title,help,input);if(result.error){const error=document.createElement('p');error.textContent=result.error;error.className='danger-text';box.append(error)}$('wizard-question-form').hidden=false;revealGroup($('wizard-question-form'));location.hash='remote';notice('请按向导填写下一项。')}if(result.done){$('wizard-question-form').hidden=true;location.hash='remote';notice('存储配置已保存，可验证连接并添加为备份目标。')}if(result.remote_files){const box=$('remote-files');box.replaceChildren();for(const file of result.remote_files){const row=document.createElement('div');row.className='file-row';const name=document.createElement('strong');name.textContent=file.name;const size=document.createElement('span');size.textContent=formatSize(file.size);const date=document.createElement('span');date.textContent=file.modified;const button=document.createElement('button');button.className='ghost';button.textContent='下载到本机';button.onclick=()=>startTask('remote-download',{...remoteBrowse,file:file.name});row.append(name,size,date,button);box.append(row)}if(!result.remote_files.length)box.textContent='该目录没有 zip / 7z 备份。';location.hash='remote'}}
$('remote-browse-form').onsubmit=async event=>{event.preventDefault();remoteBrowse=formValues('remote-browse-form');await startTask('remote-list',remoteBrowse)};
$('restore-form').onsubmit=async event=>{event.preventDefault();const values=formValues('restore-form');if(values.confirmation!=='RESTORE')return notice('请输入 RESTORE 确认覆盖恢复。',true);if(!values.file)return notice('请选择备份文件。',true);if(await startTask('restore',values)){$('restore-form').elements.password.value='';$('restore-form').elements.confirmation.value=''}};
$('remove-form').onsubmit=async event=>{event.preventDefault();const values=formValues('remove-form');if(values.confirmation!=='DELETE')return notice('请输入 DELETE 确认卸载。',true);if(!confirm((values.remove_proxy?'将同时移除反代并影响其所有站点。\n':'')+(values.delete_data?'将永久删除所选服务的数据和配置。确认继续？':'将移除所选服务容器，保留配置和数据。确认继续？')))return;if(await startTask('remove-services',values))$('remove-form').elements.confirmation.value=''};
$('remove-form').onchange=()=>{$('remove-summary').textContent=$('remove-form').elements.delete_data.checked?'已选择永久删除数据。密码库范围会删除数据库和附件；备份范围会删除本机备份与远程配置。云盘内文件不会删除。':'仅移除容器，配置与数据保留。不会卸载 Docker 或删除其他应用的网络。'};
$('upload-form').onsubmit=event=>{event.preventDefault();const file=$('backup-upload').files[0];if(!file)return;const epoch=authEpoch;const xhr=new XMLHttpRequest();activeUploads.add(xhr);xhr.onloadend=()=>activeUploads.delete(xhr);xhr.open('POST','/api/backup/upload?name='+encodeURIComponent(file.name));xhr.setRequestHeader('Content-Type','application/octet-stream');xhr.setRequestHeader('X-CSRF-Token',csrf);const button=$('upload-form').querySelector('button');button.disabled=true;xhr.upload.onprogress=e=>{if(epoch!==authEpoch)return;$('upload-status').textContent=e.lengthComputable?'上传 '+Math.round(e.loaded/e.total*100)+'%':'上传中…'};xhr.onload=async()=>{if(epoch!==authEpoch)return;button.disabled=false;try{if(xhr.status===401){expireSession();return}const result=JSON.parse(xhr.responseText);if(xhr.status!==200)throw Error(result.error||'上传失败');$('upload-status').textContent='上传完成';await loadConfig();if(epoch!==authEpoch)return;$('restore-form').elements.file.value=result.file;$('restore-form').elements.confirmation.value='';notice('已上传，可在下方选择恢复。')}catch(e){if(isSessionTransition(e))return;notice(e.message,true)}};xhr.onerror=()=>{if(epoch!==authEpoch)return;button.disabled=false;notice('上传连接中断，请重新上传。',true)};xhr.send(file)};
const featureMap=[[1,'生成主服务配置','vault'],[2,'初始化备份服务','backup'],[3,'配置远程存储','remote'],[4,'启动 / 应用主服务','deploy'],[5,'停止主服务','stop'],[6,'启动 / 应用备份','backup-start'],[7,'停止备份服务','backup-stop'],[8,'升级服务镜像','upgrade'],[9,'加密管理口令','vault'],[10,'修改主服务配置','vault'],[11,'修改备份配置','backup'],[12,'立即备份','backup-now'],[13,'访问信息与状态','info'],[14,'服务日志','tasks'],[15,'备份诊断','backup-diagnose'],[16,'恢复备份数据','restore'],[17,'环境体检','doctor'],[18,'卸载与清理','cleanup']];
for(const [id,label,target]of featureMap){const button=document.createElement('button');button.className='feature-link';const number=document.createElement('b');number.textContent=String(id).padStart(2,'0');button.append(number,document.createTextNode(label));button.onclick=()=>target in names?location.hash=target:startTask(target);$('feature-map').append(button)}
setInterval(async()=>{if(!csrf||location.hash!=='#tasks'||!$('live-logs').checked||taskRunning||Date.now()-lastLive<5000)return;lastLive=Date.now();try{await api('task/start',{action:$('log-service').value,...($('log-service').value==='proxy-logs'?{proxy_id:$('proxy-log-source').value}:{})});await poll()}catch(e){if(isSessionTransition(e))return;notice(e.message,true)}},1000);



// Progressive disclosure: keep forms in the same document and preserve their values.
let lastSubmittedForm=null;
function revealGroup(element){
    if(!element)return;
    const view=element.closest('[data-view]');
    if(view?.hidden)location.hash=view.dataset.view;
    let group=element.closest('details.config-group')||element.querySelector?.('details.config-group');
    while(group){group.open=true;group=group.parentElement.closest('details.config-group')}
}
function setupGroups(){
    const defaults={vault:'基本信息与权限',mail:'SMTP 连接',backup:'自动备份计划',remote:'已配置的存储',settings:'通过自己的域名访问面板',cleanup:'卸载范围与数据'};
    for(const view of document.querySelectorAll('[data-view]')){
        if(!(view.dataset.view in defaults))continue;
        for(const card of view.querySelectorAll('article.card')){
            const heading=card.querySelector(':scope > .card-heading');
            const titleNode=heading?.querySelector('h2')||card.querySelector('h2');
            const title=titleNode?.textContent||(card.querySelector('#restore-form')?'从备份恢复':card.querySelector('#remove-form')?'卸载范围与数据':'');
            if(!title)continue;
            const group=document.createElement('details');group.className=card.className+' config-group';
            group.dataset.groupTitle=title;group.open=defaults[view.dataset.view]===title;
            if(card.id)group.id=card.id;
            const summary=document.createElement('summary');summary.className='group-summary';
            if(heading){summary.append(heading)}else{
                const label=document.createElement('span');label.className='group-label';
                const h2=document.createElement('h2');h2.textContent=title;label.append(h2);summary.append(label);titleNode?.remove();
            }
            const meta=document.createElement('span');meta.className='group-meta';
            const status=document.createElement('span');status.className='group-status';
            const changed=document.createElement('span');changed.className='group-changed';changed.textContent='未保存';changed.hidden=true;
            const toggle=document.createElement('span');toggle.className='group-toggle';toggle.setAttribute('aria-hidden','true');
            meta.append(status,changed,toggle);const badge=heading?.querySelector('.badge');if(badge)meta.prepend(badge);summary.append(meta);
            const body=document.createElement('div');body.className='group-body';
            while(card.firstChild)body.append(card.firstChild);
            group.append(summary,body);card.replaceWith(group);
        }
    }
}
function updateGroupSummaries(){
    for(const group of document.querySelectorAll('.config-group')){
        const value=name=>group.querySelector('[name="'+name+'"]')?.value||'';
        const checked=name=>!!group.querySelector('[name="'+name+'"]')?.checked;
        const title=group.dataset.groupTitle;let status='按需配置';
        if(title==='基本信息与权限')status=value('DOMAIN')||'待配置';
        else if(title==='注册与权限')status='注册'+(checked('SIGNUPS_ALLOWED')?'开放':'关闭')+' · 邀请'+(checked('INVITATIONS_ALLOWED')?'开放':'关闭');
        else if(title==='管理后台口令')status='';
        else if(title==='运行与安全设置')status='会话 '+value('ADMIN_SESSION_LIFETIME')+' 分钟';
        else if(title==='官方 Push 推送')status=checked('enabled')?'已开启':'未开启';
        else if(title==='SMTP 连接')status=value('SMTP_HOST')||'未配置';
        else if(title==='自动备份计划')status=value('CRON')+' · 保留 '+value('BACKUP_KEEP_DAYS')+' 天';
        else if(title==='备份邮件通知')status=checked('MAIL_SMTP_ENABLE')?'已开启':'未开启';
        else if(title==='远程备份 · WebDAV')status=cfg?.remote?.type==='webdav'?'已配置':'按需连接';
        else if(title==='已配置的存储')status=(cfg?.extras?.remotes.length||0)+' 个存储';
        else if(title==='自动备份发送到哪里')status=(cfg?.extras?.targets.length||0)+' 个目标';
        else if(title==='本机备份文件')status=(cfg?.files.length||0)+' 份文件';
        else if(title==='新增 / 重新配置存储')status='添加或重新授权';
        else if(title==='通过自己的域名访问面板')status=settingsSnapshot?.origins.length?settingsSnapshot.origins.length+' 个允许域名':'按需添加';
        else if(title==='从备份恢复')status='校验后恢复';
        else if(title==='卸载范围与数据')status=checked('delete_data')?'将删除数据':'保留数据';
        group.querySelector('.group-status').textContent=status;group.querySelector('.group-status').hidden=!status;
        const form=group.closest('form')||group.querySelector('form');
        if(form&&!dirty.has(form.id)&&form.id!=='token-form'&&form.id!=='restore-form')group.dataset.changed='false';
        group.querySelector('.group-changed').hidden=group.dataset.changed!=='true';
    }
}
setupGroups();
document.addEventListener('input',event=>{const group=event.target.closest('.config-group');if(group){group.dataset.changed='true';updateGroupSummaries()}});
document.addEventListener('change',()=>updateGroupSummaries());
document.addEventListener('submit',event=>{lastSubmittedForm=event.target;updateGroupSummaries()},true);
document.addEventListener('invalid',event=>revealGroup(event.target),true);
document.addEventListener('reset',event=>{const group=event.target.closest('.config-group');if(group)group.dataset.changed='false';setTimeout(updateGroupSummaries,0)},true);


// Render guidance from saved configuration, application records and observed containers.
let environment=null, settingsSnapshot=null, presentationJob=null;
const scopeForms={core:['core-form','mail-form','advanced-form','push-form'],backup:['backup-form','notifications-form','targets-form','remote-form']};
function hasUnsaved(scope){return scopeForms[scope].some(id=>dirty.has(id))}
function viewElement(view,selector){return document.querySelector('[data-view="'+view+'"] '+selector)}
function setupPresentation(){
    viewElement('overview','.setup-card').hidden=true;
    $('latest-backup').textContent='读取中';$('latest-detail').textContent='正在读取本机归档记录';
    for(const view of ['vault','mail']){const bar=viewElement(view,'.apply-bar');bar.dataset.applyScope='core';bar.hidden=true}
    const backupBar=document.createElement('div');backupBar.className='apply-bar';backupBar.dataset.applyScope='backup';backupBar.hidden=true;
    const copy=document.createElement('div');copy.append(document.createElement('strong'),document.createElement('p'));
    const button=document.createElement('button');button.className='primary';button.dataset.task='backup-start';button.onclick=()=>startTask('backup-start');backupBar.append(copy,button);
    $('backup-service-guidance').append(backupBar);
    const pending=document.createElement('div');pending.id='pending-overview';pending.className='callout';pending.hidden=true;
    const text=document.createElement('div');const title=document.createElement('strong');title.textContent='有已保存的配置等待应用';const description=document.createElement('p');description.id='pending-overview-detail';text.append(title,description);
    const links=document.createElement('div');links.className='button-row';for(const [scope,view,label]of [['core','vault','查看密码库配置'],['backup','backup','查看备份配置']]){const a=document.createElement('a');a.className='secondary';a.href='#'+view;a.dataset.pendingScope=scope;a.textContent=label;links.append(a)}pending.append(text,links);viewElement('overview','.setup-card').after(pending);
}
function applyPresentation(bar,scope,row){
    const configured=scope==='core'?cfg.configured:cfg.backup_configured;
    const pending=cfg.application?.[scope]==='pending';
    const active=row?.State==='running';const unsaved=hasUnsaved(scope);
    const action=scope==='core'?'deploy':'backup-start';
    const applying=presentationJob?.state==='running'&&([action,scope==='core'?'upgrade':'upgrade-backup'].includes(presentationJob.action));
    const failed=presentationJob?.state==='failed'&&[action,scope==='core'?'upgrade':'upgrade-backup'].includes(presentationJob.action);
    let title='',description='',label='',needsToken=false;
    if(applying){title=scope==='core'?'正在应用密码库配置':'正在应用备份配置';description='任务正在后台执行，完成后会自动更新状态。';label='执行中…'}
    else if(unsaved){title='有尚未保存的修改';description='先保存修改，再应用到'+(scope==='core'?'密码库':'备份服务')+'。';label='请先保存配置'}
    else if(!configured){bar.hidden=true;return}
    else if(scope==='core'&&!cfg.admin_set){title='还需设置管理口令';description='基本配置已保存，请先设置并加密管理后台口令。';label='设置管理口令';needsToken=true}
    else if(pending){title=failed?'上次应用未成功，配置仍待应用':'有新配置待应用';description='保存的设置尚未通过面板重新加载。应用时服务可能短暂中断。';label=active?'应用新配置':row?'启动并应用配置':'部署并应用配置'}
    else if(environment?.unknown||!environment?.available){bar.hidden=true;return}
    else if(!row){title=scope==='core'?'配置已就绪，可以部署密码库':'备份计划已保存，尚未启用';description=scope==='core'?'将下载所需镜像并启动服务。':'启动备份服务后，才会按计划执行备份。';label=scope==='core'?'部署密码库':'启用自动备份'}
    else if(!active){title=scope==='core'?'密码库服务已停止':'自动备份已暂停';description='配置和数据仍保留，可以在需要时重新启动。';label=scope==='core'?'启动密码库':'启动备份服务'}
    else{bar.hidden=true;return}
    bar.hidden=false;bar.querySelector('strong').textContent=title;bar.querySelector('p').textContent=description;
    const button=bar.querySelector('button');button.textContent=label;
    button.disabled=taskRunning||unsaved||(!needsToken&&(!environment?.available||environment?.unknown));
    button.title=button.disabled?(unsaved?'请先保存修改':taskRunning?'已有任务正在执行':'请先确认 Docker 和服务状态'):'';
    button.onclick=needsToken?()=>{revealGroup($('token-form'));$('token-form').elements.password.focus()}:()=>startTask(action);
}
function renderPresentation(){
    if(!cfg||!environment)return;
    const core=environment.containers.find(row=>row.Names==='vaultwarden');const backup=environment.containers.find(row=>row.Names==='vaultwarden-backup');
    const active=core?.State==='running',backupActive=backup?.State==='running';
    const ready=cfg.configured&&cfg.admin_set;
    const setup=viewElement('overview','.setup-card');setup.hidden=!!(!environment.available||environment.unknown||(core&&ready));
    if(!setup.hidden){
        const heading=$('setup-heading'),description=setup.querySelector('.setup-copy>p'),link=setup.querySelector('a.primary');
        const pill=setup.querySelector('.pill');pill.textContent=core?'部署配置':'下一步';
        if(!cfg.configured){heading.textContent=core?'检测到已有密码库，请先确认配置':'先填写密码库的基本信息';description.textContent=core?'服务已经存在，请确认部署目录与访问方式，再继续管理。':'填写域名与访问方式，保存后继续设置管理口令。';link.textContent='填写基本信息 →'}
        else if(!cfg.admin_set){heading.textContent='基本配置已保存，接着设置管理口令';description.textContent='管理口令用于保护密码库的 /admin 后台。';link.textContent='设置管理口令 →'}
        else{heading.textContent='配置已就绪，可以部署密码库';description.textContent='启动后即可通过已配置的地址访问。';link.textContent='前往部署 →'}
        link.onclick=()=>{if(cfg.configured&&!cfg.admin_set)revealGroup($('token-form'))};
    }
    const corePending=cfg.application?.core==='pending',backupPending=cfg.application?.backup==='pending';
    $('pending-overview').hidden=!corePending&&!backupPending;
    $('pending-overview-detail').textContent=[corePending?'密码库设置':'',backupPending?'备份设置':''].filter(Boolean).join('、')+'已保存，需要应用后由服务加载。';
    document.querySelectorAll('[data-pending-scope]').forEach(link=>link.hidden=link.dataset.pendingScope==='core'?!corePending:!backupPending);
    for(const bar of document.querySelectorAll('[data-apply-scope]'))applyPresentation(bar,bar.dataset.applyScope,bar.dataset.applyScope==='core'?core:backup);
    const docker=$('docker-help'),dockerTitle=docker.querySelector('strong'),dockerDescription=docker.querySelector('p'),install=docker.querySelector('button');
    docker.hidden=environment.available||environment.unknown;
    install.hidden=environment.docker_installed!==false;
    if(!docker.hidden){dockerTitle.textContent=environment.docker_installed===false?'服务器尚未安装 Docker':'暂时无法连接 Docker';dockerDescription.textContent=environment.docker_installed===false?'安装 Docker 后即可继续部署密码库。':'Docker 已存在，请检查服务是否启动、访问权限或稍后刷新，无需重复安装。'}
    const noFiles=!cfg.files.length;
    if(noFiles){
        $('latest-detail').textContent=backupActive?'备份服务运行中；本机暂无归档，可查看远程目标':cfg.backup_configured?'备份计划已保存；本机暂无归档':'配置备份计划后可创建第一份归档';
        const empty=$('backup-files').querySelector('.empty');if(empty){const strong=document.createElement('strong');strong.textContent='本机暂无备份归档';const message=backupActive?'备份服务已运行，可手动备份或查看远程目录。':!cfg.backup_configured?'先保存备份计划，再启用备份服务。':backup?'备份服务已暂停，可重新启用或从远程存储下载备份。':'备份计划已保存，启用备份服务后可创建第一份归档。';empty.replaceChildren(strong,document.createTextNode(message))}
    }
    const vaultSubtitle=viewElement('vault','.page-title>.muted');vaultSubtitle.textContent=!cfg.configured?'填写基本信息、设置管理口令后即可部署。':!cfg.admin_set?'基本配置已保存，还需设置管理口令。':corePending?'有已保存的配置等待应用。':core?'管理域名、访问权限和运行参数；修改后保存并应用。':'配置已就绪，可以部署或重新启动密码库。';
    const smtp=!!cfg.core.SMTP_HOST;viewElement('mail','.page-title h1').textContent=smtp?'邮件服务':'连接邮件服务';viewElement('mail','.page-title>.muted').textContent=smtp?'已填写 SMTP 配置，可在下方修改。是否能实际投递取决于邮箱服务商设置。':'填写邮箱服务商提供的 SMTP 信息，用于邀请、邮箱验证和密码提示。';
    viewElement('mail','.form-footer>span').textContent=hasUnsaved('core')?'修改尚未保存':corePending?'已保存，等待应用到密码库':smtp?'修改设置后保存，再应用到密码库':'填写后保存，邮件配置由密码库服务加载';
    const backupGroup=$('backup-form').querySelector('.config-group .card-heading p');if(backupGroup)backupGroup.textContent=cfg.backup_configured?'管理备份频率、保留策略与加密密码；已有远程目标保持不变。':'新建备份计划默认保存到本机，也可以添加远程目标。';
    $('backup-form').querySelector('.form-footer>span').textContent=hasUnsaved('backup')?'修改尚未保存':backupPending?'计划已保存，等待应用到备份服务':cfg.backup_configured?'修改计划后保存，再应用备份配置':'先填写备份计划与加密密码';
    for(const button of document.querySelectorAll('[data-task="backup-start"]'))if(!button.closest('[data-apply-scope]'))button.textContent=backupPending?'应用备份配置':backupActive?'重新应用备份配置':'启用备份服务';
    const backupUnknown=!environment.available||environment.unknown;
    $('backup-service-status').textContent=backupUnknown?'状态未知':backupActive?'运行中':backup?'已暂停':cfg.backup_configured?'待启用':'未配置';
    $('backup-service-status').classList.toggle('is-running',!!backupActive&&!backupUnknown);
    $('backup-service-description').textContent=backupUnknown?'暂时无法确认服务状态，请刷新后重试。':backupActive?'按已保存的计划自动备份，也可以立即创建一份。':backup?'自动备份已暂停，已有配置和备份文件保留。':cfg.backup_configured?'备份计划已保存，启用后开始自动执行。':'先填写并保存下方备份计划，再启用服务。';
    const backupStart=$('backup-service-start');
    backupStart.hidden=!document.querySelector('[data-apply-scope="backup"]').hidden;
    backupStart.disabled=taskRunning||backupUnknown||!cfg.backup_configured||hasUnsaved('backup');
    backupStart.className=backupActive?'secondary':'primary';
    const backupNow=$('backup-service').querySelector('[data-task="backup-now"]');backupNow.className=backupActive?'primary':'secondary';backupNow.hidden=!backup;
    $('backup-service').querySelector('.backup-maintenance').hidden=!backup;
    $('backup-service').querySelector('[data-task="upgrade-backup"]').disabled=taskRunning||backupUnknown||!backup;

    for(const button of document.querySelectorAll('[data-task="backup-stop"]')){button.disabled=taskRunning||!backupActive;button.title=backupActive?'暂停计划任务，保留备份文件':'备份服务当前未运行'}
    for(const button of document.querySelectorAll('[data-task="backup-now"]')){button.disabled=taskRunning||!backupActive;button.title=backupActive?'执行一次备份':'请先启动备份服务';const caption=button.querySelector('small');if(caption)caption.textContent=backupActive?'立即生成一份加密备份':'启动备份服务后可手动执行'}
    for(const button of document.querySelectorAll('[data-task="stop"]'))button.disabled=taskRunning||!active;
    if(settingsSnapshot){const count=settingsSnapshot.origins.length;const heading=$('domain-form').closest('.config-group')?.querySelector('.card-heading');if(heading){heading.querySelector('h2').textContent=count?'允许访问的面板域名':'通过自己的域名访问面板';heading.querySelector('p').textContent=count?'已允许 '+count+' 个域名。DNS 和 HTTPS 反向代理需独立配置与确认。':'如需使用域名，先添加访问地址，再配置 DNS 和 HTTPS 反向代理。'}}
    for(const button of document.querySelectorAll('.action-card[data-task="deploy"]')){
        button.querySelector('strong').textContent=active?'应用密码库配置':core?'启动密码库':'部署密码库';
        button.querySelector('small').textContent=corePending?'有已保存的修改等待加载':active?'服务运行中，可按需重新应用':core?'使用现有配置重新启动':cfg.configured?'使用已保存的配置部署':'先完成密码库配置';
    }
    updateGroupSummaries();
    if(panelUpdating)document.querySelectorAll('[data-task]').forEach(button=>button.disabled=true);
    if(presentationJob?.state==='running')$('task-description').textContent=['deploy','upgrade','upgrade-backup','install-docker'].includes(presentationJob.action)?'正在准备并应用服务，镜像下载可能需要几分钟。':'任务正在服务器后台执行，完成后会自动刷新结果。';
}
setupPresentation();
document.addEventListener('input',()=>renderPresentation());
document.addEventListener('change',()=>renderPresentation());


let panelUpdating=false,checkingUpdates=false,panelUpdateInfo=null,updateWatch=null;
let loadedPanelBuild='',upgradeTargetBuild='',panelReloadPending=false;
let versionPopoverOpen=false,updateCheckError='';
function paintPanelUpdate(info){
    clearTimeout(updateWatch);panelUpdateInfo=info;const installed=info.installed||{};const latest=info.latest;
    $('panel-version').textContent=installed.version||'未知版本';
    $('check-updates').title='当前构建 '+(installed.build_id||'未记录').slice(0,8)+' · 点击检查更新';
    if(!loadedPanelBuild&&/^[a-f0-9]{64}$/.test(installed.build_id||''))loadedPanelBuild=installed.build_id;
    if(info.progress?.state==='failed'||installed.build_id===upgradeTargetBuild)upgradeTargetBuild='';
    panelUpdating=info.progress?.state==='upgrading'||!!upgradeTargetBuild;
    $('panel-update').className='panel-update';
    $('update-dot').hidden=!info.available;$('upgrade-panel').hidden=!info.available||panelUpdating;
    $('upgrade-panel').disabled=taskRunning||consoleRunning||panelUpdating;
    $('upgrade-panel').title=taskRunning||consoleRunning?'请先完成当前任务或结束高级控制台':'';
    if(panelUpdating){$('update-message').textContent='面板升级中，完成后将自动刷新。'}
    else if(info.progress?.state==='failed'){$('panel-update').classList.add('error');$('update-message').textContent=info.progress.error||'升级失败，请重试。'}
    else if(info.error){$('panel-update').classList.add('error');$('update-message').textContent=info.error}
    else if(info.available&&latest){$('panel-update').classList.add('available');$('update-message').textContent='可升级到 '+latest.version+' · 构建 '+latest.build_id.slice(0,8)}
    else{$('update-message').textContent='已是最新构建 · 点击版本号可重新检查'}
    if(panelUpdating){clearTimeout(updateWatch);updateWatch=setTimeout(()=>checkPanelUpdates(false),3000)}
    syncVersionPopover();renderPresentation();
}
async function checkPanelUpdates(force=false){
    if(!csrf||checkingUpdates)return;
    const epoch=authEpoch,parentSignal=authController.signal,controller=new AbortController();
    const cancel=()=>controller.abort();parentSignal.addEventListener('abort',cancel,{once:true});
    let timedOut=false;const timeout=setTimeout(()=>{timedOut=true;controller.abort()},20000);
    checkingUpdates=true;updateCheckError='';syncVersionPopover();
    try{
        let response=await sessionRequest('/api/updates'+(force?'?force=1':''),{credentials:'same-origin',signal:controller.signal});
        if(epoch!==authEpoch)throw sessionChanged();
        if(response.status===401){if(panelUpdating){location.reload();return}const valid=await verifyCurrentSession(epoch);if(epoch!==authEpoch)throw sessionChanged();if(!valid){expireSession();return}response=await sessionRequest('/api/updates'+(force?'?force=1':''),{credentials:'same-origin',signal:controller.signal});if(epoch!==authEpoch)throw sessionChanged();if(response.status===401)throw Error('更新检查暂不可用，请稍后重试。')}
        const info=await response.json();if(epoch!==authEpoch)throw sessionChanged();if(!response.ok||!info.installed||typeof info.available!=='boolean')throw Error(info.error||'暂时无法检查更新');if(loadedPanelBuild&&/^[a-f0-9]{64}$/.test(info.installed.build_id||'')&&info.installed.build_id!==loadedPanelBuild&&info.progress?.state!=='failed'){if(!panelReloadPending){panelReloadPending=true;clearTimeout(updateWatch);location.reload()}return}paintPanelUpdate(info);
    }catch(error){if(isSessionTransition(error)||epoch!==authEpoch)return;updateCheckError=timedOut?'检查超时，请点击右上角刷新重试。':'连接失败，请点击右上角刷新重试。';$('panel-update').className='panel-update error';$('update-message').textContent=panelUpdating?'面板暂时离线，正在等待重新连接…':'检查失败，点击版本号重试';$('upgrade-panel').hidden=true;if(panelUpdating){clearTimeout(updateWatch);updateWatch=setTimeout(()=>checkPanelUpdates(false),3000)}}
    finally{clearTimeout(timeout);parentSignal.removeEventListener('abort',cancel);if(epoch===authEpoch){checkingUpdates=false;syncVersionPopover()}}
}
function positionVersionPopover(){
    if(!versionPopoverOpen)return;
    const popup=$('panel-update'),anchor=$('check-updates').getBoundingClientRect(),box=popup.getBoundingClientRect();
    const left=Math.max(16,Math.min(anchor.left,innerWidth-box.width-16));
    let top=anchor.top-box.height-10;if(top<16)top=anchor.bottom+10;
    top=Math.max(16,Math.min(top,innerHeight-box.height-16));
    popup.style.left=left+'px';popup.style.top=top+'px';
}
function setVersionPopover(open,focusBack=false){
    versionPopoverOpen=open;$('version-menu').open=open;
    document.body.classList.toggle('version-popover-open',open);
    if(open){syncVersionPopover();requestAnimationFrame(positionVersionPopover)}
    else if(focusBack)$('check-updates').focus({preventScroll:true});
}
function syncVersionPopover(){
    const info=panelUpdateInfo,latest=info?.latest,failed=info?.progress?.state==='failed'&&info?.available,error=updateCheckError||info?.error;
    $('popover-current').textContent='v'+(info?.installed?.version||$('panel-version').textContent);
    $('popover-latest').textContent=checkingUpdates?'检查中…':latest&&!error?'v'+latest.version:error?'暂不可用':'等待检查';
    $('refresh-updates').disabled=checkingUpdates||panelUpdating;$('refresh-updates').classList.toggle('is-checking',checkingUpdates);
    $('refresh-updates').setAttribute('aria-busy',String(checkingUpdates));
    const available=!!(info?.available&&!error),title=$('popover-status-title');
    $('check-updates').classList.toggle('has-update',available);
    title.textContent=panelUpdating?'正在更新面板':checkingUpdates?'正在检查更新':failed?'上次更新未完成':error?'暂时无法检查更新':available?'有新版本可用！':info?'已是最新版本':'检查面板更新';
    if(panelUpdating)$('update-message').textContent='更新完成后自动刷新并回到面板，密码库服务保持运行。';
    else if(failed)$('update-message').textContent=info.progress.error||'可重新检查后重试。';
    else if(checkingUpdates)$('update-message').textContent='正在获取最新版本信息，请稍候。';
    else if(error)$('update-message').textContent=error;
    else if(!panelUpdating&&!error&&latest)$('update-message').textContent=available?(latest.version===info.installed.version?'包含新的修复，可立即更新。':'v'+latest.version):'当前面板无需更新。';
    $('upgrade-panel').hidden=!available||panelUpdating;
    $('upgrade-panel').textContent=panelUpdating?'正在更新…':'↓ 立即更新';
    $('upgrade-panel').disabled=checkingUpdates||panelUpdating||taskRunning||consoleRunning;
    if(versionPopoverOpen)requestAnimationFrame(positionVersionPopover);
}
$('version-menu').addEventListener('toggle',()=>{const open=$('version-menu').open;if(open===versionPopoverOpen)return;setVersionPopover(open);if(open&&!panelUpdating)checkPanelUpdates(true)});
$('refresh-updates').onclick=()=>checkPanelUpdates(true);
document.addEventListener('click',event=>{if(versionPopoverOpen&&!$('panel-update').contains(event.target)&&!$('check-updates').contains(event.target))setVersionPopover(false)});
document.addEventListener('keydown',event=>{if(event.key==='Escape'&&versionPopoverOpen){event.preventDefault();setVersionPopover(false,true)}});
addEventListener('resize',positionVersionPopover);addEventListener('scroll',positionVersionPopover,{capture:true,passive:true});
$('upgrade-panel').onclick=async()=>{
    if(!panelUpdateInfo?.available||!panelUpdateInfo.latest||checkingUpdates||panelUpdating)return;
    if(taskRunning||consoleRunning)return notice('请先完成当前任务或结束高级控制台。',true);
    const hasInput=dirty.size||Array.from(document.querySelectorAll('#workspace input[type=password]')).some(input=>!input.readOnly&&input.value);
    const message=(hasInput?'页面还有未保存输入，重启后将丢失。\n':'')+'确认升级管理面板？升级会重启面板，完成后将自动重新连接；密码库与备份容器保持运行。';
    if(!confirm(message))return;
    $('upgrade-panel').disabled=true;
    try{const targetBuild=panelUpdateInfo.latest.build_id;const progress=await api('updates/start',{build_id:targetBuild,confirmed:true});upgradeTargetBuild=targetBuild;paintPanelUpdate({...panelUpdateInfo,progress});}
    catch(error){if(isSessionTransition(error))return;notice(error.message,true);$('upgrade-panel').disabled=false}
};
setInterval(()=>{if(csrf&&!panelUpdating)checkPanelUpdates(false)},60000);
restoreSession();
