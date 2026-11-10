'use strict';
const $=id=>document.getElementById(id);
let pendingDialog=null;
function dismissActionDialog(value=false){
    const pending=pendingDialog;if(!pending)return;pendingDialog=null;
    $('action-dialog').close();document.body.classList.remove('dialog-open');$('action-dialog-input').value='';
    pending.resolve(pending.input?(typeof value==='string'?value:null):value===true);
    if(pending.focus?.isConnected&&pending.focus.checkVisibility())pending.focus.focus({preventScroll:true});
}
function actionDialog(message,options={}){
    if(pendingDialog||authPhase!=='authenticated')return Promise.resolve(options.input?null:false);
    return new Promise(resolve=>{
        pendingDialog={resolve,input:!!options.input,expected:options.expected,focus:document.activeElement};
        const dialog=$('action-dialog'),field=$('action-dialog-input');
        $('action-dialog-title').textContent=options.title||'确认操作';$('action-dialog-message').textContent=message;
        dialog.classList.toggle('is-danger',!!options.danger);$('action-dialog-icon').textContent=options.danger?'!':'↗';
        $('action-dialog-accept').className=options.danger?'danger':'primary';$('action-dialog-accept').textContent=options.accept||'确认';
        $('action-dialog-field').hidden=!options.input;field.disabled=!options.input;field.value='';field.placeholder=options.placeholder||'';field.removeAttribute('aria-invalid');
        $('action-dialog-label').textContent=options.inputLabel||'确认内容';$('action-dialog-error').hidden=true;$('action-dialog-error').textContent='';
        document.body.classList.add('dialog-open');dialog.showModal();
        (options.input?field:options.danger?$('action-dialog-cancel'):$('action-dialog-accept')).focus({preventScroll:true});
    });
}
function confirmDialog(message,options={}){return actionDialog(message,options)}
function promptDialog(message,options={}){return actionDialog(message,{...options,input:true})}
$('action-dialog-cancel').onclick=()=>dismissActionDialog();$('action-dialog-close').onclick=()=>dismissActionDialog();
$('action-dialog').addEventListener('cancel',event=>{event.preventDefault();dismissActionDialog()});
$('action-dialog').addEventListener('close',()=>{if(!$('action-dialog').open)dismissActionDialog()});
$('action-dialog').addEventListener('click',event=>{const box=$('action-dialog').getBoundingClientRect();if(event.target===$('action-dialog')&&(event.clientX<box.left||event.clientX>box.right||event.clientY<box.top||event.clientY>box.bottom))dismissActionDialog()});
document.addEventListener('keydown',event=>{if(event.key!=='Tab'||!pendingDialog||!$('action-dialog').open)return;const items=Array.from($('action-dialog').querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),a[href]')).filter(item=>item.checkVisibility());const first=items[0],last=items[items.length-1];if(!first)return;if(!$('action-dialog').contains(document.activeElement)){event.preventDefault();first.focus()}else if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus()}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus()}},true);
$('action-dialog-input').oninput=()=>{$('action-dialog-input').removeAttribute('aria-invalid');$('action-dialog-error').hidden=true};
$('action-dialog-form').onsubmit=event=>{
    event.preventDefault();if(!pendingDialog)return;
    if(pendingDialog.input){const value=$('action-dialog-input').value.trim();if(!value||(pendingDialog.expected&&value!==pendingDialog.expected)){$('action-dialog-error').textContent=pendingDialog.expected?'请输入 '+pendingDialog.expected+' 以确认。':'请填写确认内容。';$('action-dialog-error').hidden=false;$('action-dialog-input').setAttribute('aria-invalid','true');$('action-dialog-input').focus();return}dismissActionDialog(value)}else dismissActionDialog(true);
};

const names={overview:'运行概览',vault:'密码库配置',mail:'邮件服务',proxy:'域名与反代',backup:'备份设置',targets:'备份目标',archives:'备份文件与恢复',tasks:'任务与日志',settings:'面板设置',remote:'远程存储',restore:'备份与恢复',diagnostics:'诊断与状态',cleanup:'卸载与清理'};
let lastStatus=0;
let proxyLogPreference='';
let proxySourcePreference='auto',autoProxyTarget='',autoInstallMode='caddy';
let proxyEnvironment=null,proxyDetectionLoading=false,proxyDetectionSequence=0,proxyNetworkEdited=false,proxyManualNetwork='';
let csrf='', timer=null, refreshing=false, taskRunning=false, currentTask='', finishedTask='', cfg=null;
let cursor=0,generation='',raw='',consoleRunning=false,hiddenInput=false;
const dirty=new Set();
let tokenState={id:'',phase:'',message:''},tokenStarting=false;
let externalProxyMethod='auto',externalProxyEditing=false;
let authEpoch=0,authPhase='checking',authController=new AbortController();
const activeUploads=new Set();
function sessionChanged(){const error=new Error('');error.name='SessionChanged';return error}
function isSessionTransition(error){return error?.name==='SessionChanged'}
function invalidateAuth(phase){
    dismissActionDialog();
    trackedTask=null;externalProxyMethod='auto';externalProxyEditing=false;$('external-npm-form').reset();delete $('external-npm-form').dataset.filled;$('external-copy-result').textContent='';tokenState={id:'',phase:'',message:''};tokenStarting=false;setVersionPopover(false);authEpoch++;authController.abort();authController=new AbortController();authPhase=phase;csrf='';
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

let noticeTimer=null,trackedTask=null;
function notice(message,error=false,options={}){
    if(error&&isAuthMessage(message)&&authPhase==='authenticated'){notice('正在确认登录状态…',false,{kind:'loading'});reconcileAuthNotice();return}
    if(authPhase!=='authenticated'){message='';error=false}
    clearTimeout(noticeTimer);const box=$('notice');box.replaceChildren();box.hidden=!message;if(!message)return;
    const kind=error?'error':options.kind||(/^(请|有|未|暂|当前)/.test(message)?'info':'success');box.className='toast '+kind;box.setAttribute('role',error?'alert':'status');
    const icon=document.createElement('span');icon.className='toast-icon';icon.setAttribute('aria-hidden','true');icon.textContent=kind==='loading'?'':kind==='error'?'!':kind==='success'?'✓':'i';
    const text=document.createElement('span');text.id='notice-message';text.textContent=message;box.append(icon,text);
    if(options.detail){const detail=document.createElement('button');detail.type='button';detail.className='toast-detail';detail.textContent=options.detailLabel||'查看失败详情';detail.onclick=event=>{event.stopPropagation();if(options.onDetail)options.onDetail();else{closeWorkspaceDialogs();location.hash='tasks';$('task-details').open=true}};box.append(detail)}
    const close=document.createElement('button');close.type='button';close.className='toast-close';close.setAttribute('aria-label','关闭提示');close.textContent='×';close.onclick=()=>notice('');box.append(close);
    if(kind==='success'||kind==='info')noticeTimer=setTimeout(()=>notice(''),5000);
}
function trackTask(job){if(job?.id)trackedTask={id:job.id,action:job.action}}
function taskOutcome(job){
    if(!trackedTask||trackedTask.id!==job.id||job.state==='running')return;
    const action=trackedTask.action||job.action;trackedTask=null;
    if(job.state==='failed'){if(action==='remote-preset-save')presetSubmitted=null;notice((job.error||'操作失败，请查看执行详情。'),true,{detail:true});return}
    if(action==='backup-target-save'){$('target-dialog').close();dirty.delete('target-editor-form')}
    if(action==='backup-target-toggle'||action==='backup-target-save'){notice((cfg?.extras?.targets||[]).some(row=>row.enabled!==false)?'备份目标已更新。':'所有目标已停用，备份投递已暂停。');return}
    if(action==='remote-oauth-start'){notice('授权链接已生成，请打开服务商授权页面继续。',false,{kind:'info'});return}
    if(action==='remote-preset-save'&&presetSubmitted){const form=$('preset-form');if(form&&JSON.stringify(formValues('preset-form'))===presetSubmitted){form.querySelectorAll('input[type=password]').forEach(input=>input.value='');dirty.delete('preset-form');presetTouched=false;$('storage-dialog').close()}presetSubmitted=null}
    if(job.state!=='success'||['logs','backup-logs','proxy-logs'].includes(action)||job.result?.question)return;
    if((['remote-oauth-complete','remote-continue'].includes(action)&&job.result?.done)||action==='restore'){const dialog=$(action==='restore'?'restore-backup-dialog':'storage-dialog');dialog.close()}
    const messages={deploy:'主服务已启动，配置已应用',upgrade:'主服务升级成功','backup-start':'备份服务已启动，配置已应用','backup-stop':'自动备份已暂停','backup-now':'备份任务已完成','upgrade-backup':'备份服务升级成功',stop:'主服务已停止','admin-token':'管理口令已加密保存','remote-save':'WebDAV 连接已保存','remote-r2-save':'Cloudflare R2 存储已保存（只读验证通过）','remote-oauth-complete':'云盘授权完成，存储配置已保存','remote-preset-save':'云存储配置已保存','remote-download':'备份已下载到服务器','existing-npm':'NPM 站点与 HTTPS 已配置','remove-services':'服务卸载成功',restore:'备份恢复成功'};
    notice(messages[action]||'操作成功');
}

function route(){const requested=location.hash.slice(1);const page=requested==='restore'?'archives':requested in names?requested:'overview';if(requested==='restore'){history.replaceState(null,'','#archives');requestAnimationFrame(()=>{if(location.hash!=='#archives')return;const form=$('restore-form');revealGroup(form);form.closest('.config-group')?.scrollIntoView({block:'start'})})};document.querySelectorAll('[data-view]').forEach(v=>v.hidden=v.dataset.view!==page);document.querySelectorAll('[data-page]').forEach(v=>v.classList.toggle('active',v.dataset.page===page));document.querySelectorAll('.nav-group').forEach(group=>{group.open=!!group.querySelector('[data-page="'+page+'"]')});for(const dialog of document.querySelectorAll('dialog.workspace-dialog[open]'))if(dialog.querySelector('[data-view]')?.dataset.view!==page)dialog.close();$('breadcrumb').textContent=names[page];document.title=names[page]+' · Vaultwarden'}
addEventListener('hashchange',()=>{dismissActionDialog();route();notice('')});route();
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
    try{const valid=await verifyCurrentSession(epoch);if(epoch!==authEpoch)return;if(!valid){expireSession();return}if($('notice-message')?.textContent==='正在确认登录状态…')notice('')}
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
    closeWorkspaceDialogs();
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
for(const id of ['core-form','permissions-form','mail-form','backup-form','preset-form','advanced-form','push-form','notifications-form','target-editor-form']){const form=$(id);if(!form)continue;form.addEventListener('input',()=>dirty.add(id));form.addEventListener('change',()=>dirty.add(id))}
function fill(id,values){if(dirty.has(id))return;const form=$(id);for(const [key,value]of Object.entries(values)){const field=form.elements.namedItem(key);if(!field)continue;if(field.type==='checkbox')field.checked=value===true||value==='true';else{if(field.tagName==='SELECT'&&value&&!Array.from(field.options).some(o=>o.value===value)){field.add(new Option('当前设置：'+value,value))}field.value=value}}}
function formatSize(bytes){return bytes>=1048576?(bytes/1048576).toFixed(1)+' MB':(bytes/1024).toFixed(1)+' KB'}
function proxyContext(value){return value?JSON.stringify([value.configured,value.mode,value.network,value.proxy_target,value.proxy?.kind]):''}
function renderVaultAddresses(){
    const raw=cfg?.core?.DOMAIN||'';let url=null;
    try{const candidate=new URL(raw);if(candidate.protocol==='https:'&&!candidate.username&&!candidate.password)url=candidate}catch{}
    $('vault-url').textContent=raw||'完成配置后显示';
    const admin=url?new URL('/admin',url).href:'';
    $('vault-admin-url').textContent=admin||'完成配置后显示';
    for(const [id,value] of [['open-vault',url?.href],['open-vault-admin',admin]]){const link=$(id);link.hidden=!value;if(value)link.href=value;else link.removeAttribute('href')}
    $('open-vault-admin').title=cfg?.admin_set?'使用密码库管理口令登录':'需先设置密码库管理口令';
}
async function loadConfig(){try{const previousContext=proxyContext(cfg);cfg=await api('config');if(previousContext&&previousContext!==proxyContext(cfg)){proxyEnvironment=null;proxyLogPreference='';proxyDetectionSequence++;proxyDetectionLoading=false}const defaults={SIGNUPS_ALLOWED:!cfg.configured,INVITATIONS_ALLOWED:true,SENDS_ALLOWED:false};for(const key of Object.keys(defaults)){if(cfg.core[key]!=='')defaults[key]=cfg.core[key]}fill('core-form',{...cfg.core,...defaults,directory:cfg.directory,mode:cfg.mode,port:cfg.port,network:cfg.network});fill('permissions-form',{...cfg.core,...defaults});fill('mail-form',{SMTP_PORT:'587',SMTP_SECURITY:'starttls',...Object.fromEntries(Object.entries(cfg.core).filter(([,v])=>v!==''))});fill('backup-form',{CRON:'0 3 * * *',BACKUP_KEEP_DAYS:'30',TIMEZONE:'Asia/Shanghai',ZIP_ENABLE:true,ZIP_TYPE:'zip',BACKUP_FILE_SUFFIX:'%Y%m%d-%H%M%S',DISPLAY_NAME:'vaultwarden',...Object.fromEntries(Object.entries(cfg.backup).filter(([,v])=>v!==''))});$('admin-badge').textContent=cfg.admin_set?'已设置 · 留空保留':'尚未设置';$('admin-badge').className='badge'+(cfg.admin_set?' success':'');for(const field of $('core-form').elements){if(['directory','mode','port','network'].includes(field.name))field.disabled=cfg.configured}$('topology-note').hidden=!cfg.configured;const smtp=$('mail-form').elements.SMTP_PASSWORD;smtp.placeholder=cfg.smtp_password_set?'已保存 · 留空保留原值':'填写邮箱授权码';$('backup-form').elements.ZIP_PASSWORD.placeholder=cfg.zip_password_set?'已保存 · 留空保留原值':'首次配置请设置加密密码';$('step-config').classList.toggle('done',cfg.configured);$('step-config').querySelector('b').textContent=cfg.configured?'已保存':'待配置';$('step-token').classList.toggle('done',cfg.admin_set);$('step-token').querySelector('b').textContent=cfg.admin_set?'已保护':'待设置';renderVaultAddresses();renderFiles(cfg.files);renderExtras(cfg);routeHelp();updateGroupSummaries();renderPresentation();if(!proxyEnvironment&&!proxyDetectionLoading)loadProxyEnvironment()}catch(e){if(isSessionTransition(e))return;notice(e.message,true)}}
function renderFiles(files){const box=$('backup-files');box.replaceChildren();if(!files.length){const empty=document.createElement('div');empty.className='empty';const title=document.createElement('strong');title.textContent='还没有本机备份';empty.append(title,document.createTextNode('保存备份计划并启用后，点击“立即备份”创建第一份。'));box.append(empty)}for(const file of files){const row=document.createElement('div');row.className='file-row';for(const [i,value]of [file.name,formatSize(file.size),new Date(file.modified*1000).toLocaleString('zh-CN',{hour12:false})].entries()){const span=document.createElement(i===0?'strong':'span');span.textContent=value;row.append(span)}const actions=document.createElement('div');actions.className='backup-file-actions';const download=document.createElement('a');download.textContent='下载';download.href='/api/backups/download?file='+encodeURIComponent(file.name);actions.append(download);if(/\.(zip|7z)$/i.test(file.name)){const restore=document.createElement('button');restore.type='button';restore.className='ghost';restore.textContent='选择恢复';restore.onclick=()=>{const form=$('restore-form');if(form.elements.file.value!==file.name)form.elements.password.value='';form.elements.file.value=file.name;form.elements.confirmation.value='';revealGroup(form);form.closest('.config-group').scrollIntoView({behavior:'smooth',block:'start'})};actions.append(restore)}row.append(actions);box.append(row)}const latest=files[0];$('latest-backup').textContent=latest?new Date(latest.modified*1000).toLocaleDateString('zh-CN'):'暂无归档';$('latest-detail').textContent=latest?latest.name:'启用自动备份，保护你的数据'}
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
function renderExternalProxyGuide(){
    const core=$('core-form'),mode=core.elements.mode.value,card=$('external-proxy-card');
    $('proxy-current-domain').value=cfg?.core?.DOMAIN||'请先保存密码库的基本信息';
    $('proxy-current-route').textContent=cfg?.proxy?.managed?'反代由面板管理，应用主服务配置时会配置站点与 HTTPS。':mode==='caddy'?'使用 Caddy 自动 HTTPS，应用配置时配置反代与证书。':mode==='host_ext'?'由现有宿主机反代转发到 http://127.0.0.1:'+core.elements.port.value+'，域名规则与证书需在现有反代配置。':'使用已有容器反代。检测到工具和共享网络不代表站点已配置，访问前请确认站点规则与证书。';
    card.hidden=mode!=='docker_ext'||!!cfg?.proxy?.managed;
    if(card.hidden){$('external-npm-form').elements.password.value='';return}
    const target=cfg?.proxy_target||core.elements.PROXY_TARGET.value,network=core.elements.network.value;
    const choices=(proxyEnvironment?.proxies||[]).filter(p=>p.kind==='Nginx Proxy Manager'&&p.running&&p.networks?.includes(network)&&(!target||p.id===target));
    const form=$('external-npm-form'),select=form.elements.target,previous=select.value;
    select.replaceChildren();if(choices.length>1)select.add(new Option('请选择 NPM 实例',''));for(const proxy of choices)select.add(new Option(proxy.name,proxy.id));
    if(choices.some(p=>p.id===previous))select.value=previous;
    const record=cfg?.external_proxy,recorded=!!(record?.recorded&&(!select.value||record.target===select.value));
    const showingRecord=recorded&&!externalProxyEditing;
    $('external-npm-record').hidden=!showingRecord;
    $('external-record-description').textContent=recorded?'上次已为 '+record.domain+' 写入站点与 HTTPS，无需重复填写账号。此记录不代表当前访问已检测正常。':'';
    $('external-record-email').textContent=record?.email?(record.email_source==='npm'?'NPM 已确认的管理邮箱：':'上次使用的 NPM 管理邮箱：')+record.email:'旧版本未记录管理邮箱；仅重新配置时需要填写。';
    if(recorded)$('external-record-open').href='https://'+record.domain;
    $('external-record-cancel').hidden=!recorded||!externalProxyEditing;
    const automatic=choices.length&&externalProxyMethod==='auto';
    $('external-npm-auto').hidden=!automatic||showingRecord;$('external-manual-body').hidden=!!automatic;
    $('external-method-auto').disabled=!choices.length;
    for(const [id,selected] of [['external-method-auto',!!automatic],['external-method-manual',!automatic]]){$(id).className=selected?'primary':'secondary';$(id).setAttribute('aria-pressed',String(selected))}
    $('external-manual-description').textContent=choices.length?'在 NPM 的 Hosts → Proxy Hosts 中点击 Add Proxy Host，按下列参数填写。已有同域名站点时请检查原规则。':'在现有反代中添加下列域名与转发目标；已有同域名规则时，请先检查原配置。';
    $('external-manual-ssl').textContent=choices.length?'开启 Websockets Support。SSL 中选择已有证书或 Request a new SSL Certificate，再开启 Force SSL 和 HTTP/2。申请证书前，将域名解析到本服务器并放行 80 / 443。':'为站点启用 WebSocket 支持和 HTTPS 证书，将域名解析到本服务器；证书申请方式由现有反代管理。';
    if(!form.dataset.filled&&cfg?.configured){if(!form.elements.email.value)form.elements.email.value=record?.email||'';form.dataset.filled='true'}
    let domain=core.elements.DOMAIN.value.trim();try{domain=new URL(domain.includes('://')?domain:'https://'+domain).hostname}catch{domain=''}
    $('external-route-domain').value=domain;
    $('external-route-network').textContent='Scheme 填 http，Forward Hostname / IP 填 vaultwarden，Forward Port 填 80。NPM 与密码库需处于同一 Docker 网络：'+network+'。';
    const running=environment?.containers?.some(row=>row.Names==='vaultwarden'&&row.State==='running');
    form.querySelector('button[type=submit]').disabled=taskRunning||panelUpdating||!cfg?.configured||!running||hasUnsaved('core')||cfg?.application?.core==='pending';
    $('external-npm-ready').textContent=!cfg?.configured?'请先保存主服务配置。':!running?'请先启用主服务，再创建反代站点。':(hasUnsaved('core')||cfg?.application?.core==='pending')?'请先保存并应用主服务修改。':'需要域名解析正确，80 / 443 可用；现有同域名规则不会被覆盖。';
    const selected=choices.find(p=>p.id===select.value),link=$('external-open-npm');link.hidden=!selected?.admin_port||selected.admin_local;
    if(!link.hidden){const url=new URL(location.href);url.protocol='http:';url.port=String(selected.admin_port);url.pathname='/';url.search='';url.hash='';link.href=url.href}
}
$('external-npm-form').onsubmit=async event=>{event.preventDefault();if(!await confirmDialog('将在所选 NPM 创建密码库站点并申请 HTTPS 证书。已有同域名规则会保留并提示。',{title:'配置 NPM 站点',accept:'创建站点'}))return;if(await startTask('existing-npm',formValues('external-npm-form')))$('external-npm-form').elements.password.value=''};
$('external-npm-form').elements.target.onchange=renderExternalProxyGuide;
$('external-record-edit').onclick=()=>{externalProxyMethod='auto';externalProxyEditing=true;const email=cfg?.external_proxy?.email;if(email)$('external-npm-form').elements.email.value=email;renderExternalProxyGuide()};
$('external-record-cancel').onclick=()=>{externalProxyEditing=false;$('external-npm-form').elements.password.value='';renderExternalProxyGuide()};
$('external-method-auto').onclick=()=>{externalProxyMethod='auto';renderExternalProxyGuide()};
$('external-method-manual').onclick=()=>{externalProxyMethod='manual';renderExternalProxyGuide()};
document.querySelectorAll('[data-copy-route]').forEach(button=>button.onclick=async()=>{const field=$(button.dataset.copyRoute);try{if(navigator.clipboard?.writeText)await navigator.clipboard.writeText(field.value);else{field.focus();field.select();if(!document.execCommand('copy'))throw Error()}$('external-copy-result').textContent='已复制。'}catch{$('external-copy-result').textContent='请选中上方内容手动复制。';field.focus();field.select()}});
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
    renderExternalProxyGuide();
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
function normalizeVaultAddress(){const field=$('core-form').elements.DOMAIN;const value=field.value.trim();field.value=value&&!value.includes('://')?(value.startsWith('//')?'https:'+value:'https://'+value):value}
$('core-form').elements.DOMAIN.addEventListener('blur',()=>{const field=$('core-form').elements.DOMAIN,previous=field.value;normalizeVaultAddress();if(previous!==field.value)field.dispatchEvent(new Event('input',{bubbles:true}))});
for(const [id,section]of [['core-form','core'],['permissions-form','permissions'],['mail-form','mail'],['backup-form','backup']]){$(id).onsubmit=async event=>{event.preventDefault();const epoch=authEpoch;const form=event.currentTarget;const button=form.querySelector('button[type=submit]')||form.querySelector('button');button.disabled=true;try{if(id==='core-form')normalizeVaultAddress();const data=formValues(id);const result=await api('config/'+section,data);dirty.delete(id);form.querySelectorAll('input[type=password]').forEach(v=>v.value='');notice(result.message);await loadConfig()}catch(e){if(isSessionTransition(e))return;notice(e.message,true)}finally{if(epoch===authEpoch)button.disabled=false}}}
async function refreshStatus(){try{const status=await api('status');environment=status;lastStatus=Date.now();for(const [name,id]of [['vaultwarden','vault'],['vaultwarden-backup','backup']]){const row=status.containers.find(r=>r.Names===name),active=row&&row.State==='running';$(id+'-state').textContent=!status.available?(status.docker_installed===false?'环境未就绪':'状态未知'):!row?'尚未部署':active?'运行中':'已停止';$(id+'-state').classList.toggle('good',!!active);$(id+'-detail').textContent=row?row.Status:status.message||'完成配置后即可启动';if(id==='vault'){$('step-deploy').classList.toggle('done',!!active);$('step-deploy').querySelector('b').textContent=active?'已运行':'待部署';$('setup-heading').textContent=active?'你的密码库，正在安心运行。':'三步，拥有自己的密码库。'}}$('docker-help').hidden=status.available;renderPresentation()}catch(e){if(isSessionTransition(e))return;environment={unknown:true,available:false,docker_installed:null,containers:[]};for(const id of ['vault','backup']){$(id+'-state').textContent='状态未知';$(id+'-state').classList.remove('good');$(id+'-detail').textContent='暂时无法读取服务状态'}renderPresentation();notice(e.message,true)}}
async function startTask(action,extra={}){const epoch=authEpoch;if(['backup-start','backup-now'].includes(action)&&cfg?.backup_configured&&!(cfg.extras?.targets||[]).some(row=>row.enabled!==false)){notice('没有启用的备份目标，请先在“备份目标”页面启用一个。',true);return false}if(action==='proxy-logs'){if(!$('proxy-log-source').value){notice('请先在诊断与状态中选择反代日志来源。',true);return false}extra={...extra,proxy_id:$('proxy-log-source').value}}if(panelUpdating){notice('面板正在升级，请完成后再操作。',true);return false}if(taskRunning){notice('已有任务正在执行，请等待完成。',false,{kind:'info',detail:true,detailLabel:'查看任务'});return false}if(((action==='deploy'&&hasUnsaved('core'))||(action==='backup-start'&&hasUnsaved('backup')))){notice('有尚未保存的配置，请先保存，再应用。',true);return false}if(action==='backup-start'&&(!cfg?.configured||!cfg?.admin_set)){notice('请先保存主服务配置并设置管理口令。',true);return false}if(action==='backup-start'&&!environment?.containers?.some(row=>row.Names==='vaultwarden'&&row.State==='running')&&!await confirmDialog('将先启动密码库主服务，再启用备份服务。',{title:'启用备份服务',accept:'启动服务'}))return false;const confirmations={'upgrade-backup':'升级会重建备份容器，确认继续？',stop:'停止后客户端将暂时无法同步。确认停止密码库？',upgrade:'升级会下载最新镜像并重建容器，期间服务短暂不可用。建议先完成一次备份，确认继续？','backup-stop':'暂停后将不再按计划自动备份。确认暂停？','install-docker':'将通过 Docker 官方安装器安装 Docker 和 Compose，并修改服务器系统软件。确认安装？'};if(confirmations[action]&&!await confirmDialog(confirmations[action],{title:({'upgrade-backup':'升级备份服务',stop:'停止主服务',upgrade:'升级主服务','backup-stop':'暂停自动备份','install-docker':'安装 Docker'})[action],accept:({'upgrade-backup':'开始升级',stop:'确认停止',upgrade:'开始升级','backup-stop':'确认暂停','install-docker':'确认安装'})[action],danger:action==='stop'}))return false;if(epoch!==authEpoch||authPhase!=='authenticated')return false;if(taskRunning||panelUpdating){notice('已有操作正在执行，请完成后重试。',true);return false}try{const job=await api('task/start',{action,confirmed:true,...extra});trackTask({...job,action});if(!['logs','backup-logs','proxy-logs'].includes(action))notice('操作正在执行，请稍候…',false,{kind:'loading'});else notice('');await poll();if(epoch!==authEpoch)return false;return true}catch(e){if(isSessionTransition(e))return;notice(e.message,true);return false}}
document.querySelectorAll('[data-task]').forEach(button=>button.onclick=()=>startTask(button.dataset.task));
document.querySelectorAll('[data-uninstall]').forEach(button=>button.onclick=()=>{const form=$('remove-form');form.elements.scope.value=button.dataset.uninstall;form.elements.delete_data.value='keep';form.elements.remove_proxy.checked=false;form.dispatchEvent(new Event('change',{bubbles:true}));location.hash='cleanup';revealGroup(form)});
$('log-service').onchange=()=>{$('view-service-logs').dataset.task=$('log-service').value};

function syncTokenJob(job){
    if(!tokenState.id||job.id!==tokenState.id)return;
    tokenState.phase=job.state;
    tokenState.message=job.state==='running'?'正在加密并保存管理口令，请稍候…':job.state==='success'?'管理口令已加密保存。应用密码库配置后生效；原口令不回显，留空保留。':job.state==='failed'?'管理口令保存失败：'+(job.error||'请稍后重试。'):tokenState.message;
}
function renderTokenForm(){
    const form=$('token-form'),busy=tokenStarting||tokenState.phase==='running',saved=!!cfg?.admin_set;
    const editing=!!(form.elements.password.value||form.elements.confirmation.value);
    for(const name of ['password','confirmation']){form.elements[name].required=!saved||editing;form.elements[name].disabled=busy}
    form.elements.password.placeholder=saved?'已设置，填写新口令才修改':'输入新口令';
    form.elements.confirmation.placeholder=saved?'修改时请再次输入新口令':'再次输入以确认';
    const button=form.querySelector('button[type=submit]');button.disabled=busy||taskRunning||panelUpdating;button.textContent=busy?'正在加密保存…':'加密并保存管理口令';
    $('token-feedback').textContent=tokenState.message||(saved?'已设置管理口令。仅保存加密结果，原口令不回显；留空保留，填写新口令可修改。':'尚未设置管理口令，请填写并保存。');
    $('token-feedback').classList.toggle('error',tokenState.phase==='failed');
}
$('token-form').onsubmit=async event=>{
    event.preventDefault();const form=event.currentTarget,epoch=authEpoch;
    const feedback=(message,error=false)=>{tokenState={id:'',phase:error?'failed':'',message};renderTokenForm()};
    if(tokenStarting||tokenState.phase==='running')return;
    if(panelUpdating||taskRunning)return feedback('有任务正在执行，请完成后再保存管理口令。',true);
    const password=form.elements.password.value,confirmation=form.elements.confirmation.value;
    if(!password&&!confirmation&&cfg?.admin_set)return feedback('未修改管理口令，继续保留现有口令。');
    if(password!==confirmation)return feedback('两次输入的管理口令不一致。',true);
    if(password.length<12)return feedback('管理口令至少需要 12 个字符。',true);
    tokenStarting=true;tokenState={id:'',phase:'running',message:'正在加密并保存管理口令，请稍候…'};renderTokenForm();
    try{const job=await api('task/start',{action:'admin-token',confirmed:true,password});if(epoch!==authEpoch)return;form.reset();tokenState.id=job.id;syncTokenJob(job);trackTask({...job,action:'admin-token'});notice('正在加密保存管理口令…',false,{kind:'loading'});await poll()}
    catch(error){if(isSessionTransition(error))return;feedback(error.message,true)}
    finally{if(epoch===authEpoch){tokenStarting=false;renderTokenForm()}}
};
function paintTask(job){syncTokenJob(job);presentationJob=job;taskRunning=job.state==='running';currentTask=job.id;$('task-title').textContent=job.title;$('task-badge').textContent=({idle:'等待操作',running:'执行中',success:'已完成',failed:'执行失败'})[job.state]||job.state;$('task-badge').className='badge '+job.state;$('task-spinner').hidden=!taskRunning;$('task-description').textContent=taskRunning?'任务在后台执行。镜像下载可能需要几分钟，请耐心等待。':job.state==='success'?'操作已完成。配置和服务状态已刷新。':job.state==='failed'?'请根据下面的错误处理后重试。':'从配置页面或概览发起操作。';$('task-output').textContent=plain(job.output||'暂无输出');$('task-error').hidden=!job.error;$('task-error').textContent=job.error||'';if(job.state==='failed'||['logs','backup-logs','proxy-logs'].includes(job.action))$('task-details').open=true;document.querySelectorAll('[data-task]').forEach(b=>b.disabled=taskRunning||panelUpdating);renderPresentation();syncVersionPopover()}
async function poll(){if(refreshing||!csrf)return;const epoch=authEpoch;refreshing=true;try{const job=await api('task');paintTask(job);if(job.result)renderResult(job);if(Date.now()-lastStatus>15000)await refreshStatus();if(epoch!==authEpoch)return;if(job.id&&job.state!=='running'&&finishedTask!==job.id){finishedTask=job.id;await Promise.all([loadConfig(),refreshStatus()])}if(epoch!==authEpoch)return;if(job.state!=='running')taskOutcome(job);if($('advanced').open||consoleRunning)await pollConsole()}catch(e){if(isSessionTransition(e))return;notice(e.message,true)}finally{if(epoch===authEpoch)refreshing=false}}
$('refresh').onclick=async()=>{const epoch=authEpoch;await Promise.all([loadConfig(),refreshStatus(),poll()]);if(epoch!==authEpoch)return;notice('状态已刷新，未保存的表单已保留。')};$('refresh-files').onclick=loadConfig;
async function loadSettings(){try{const settings=await api('settings');settingsSnapshot=settings;$('domains').textContent=settings.origins.length?'已允许访问：'+settings.origins.join('、'):'尚未添加域名，当前可通过 IP 访问。';$('proxy-help').textContent='宿主机反向代理目标：http://127.0.0.1:'+settings.port+'。Nginx 需保留原始 Host（含端口）：proxy_set_header Host $http_host;';renderPresentation()}catch(e){if(isSessionTransition(e))return;notice(e.message,true)}}
$('domain-form').onsubmit=async event=>{event.preventDefault();try{await api('domain',{origin:$('domain').value.trim()});notice('此域名已允许访问面板。DNS 与 HTTPS 是否可用，请通过实际访问确认。');await loadSettings()}catch(e){if(isSessionTransition(e))return;notice(e.message,true)}};
function plain(s){return s.replace(/\x1b\][\s\S]*?(?:\x07|\x1b\\)/g,'').replace(/\x1b\[[0-?]*[ -/]*[@-~]/g,'').replace(/\r\n/g,'\n').split('\n').map(line=>line.split('\r').pop().replace(/[\x00-\x08\x0b-\x1f\x7f]/g,'')).join('\n')}
async function pollConsole(){const state=await api('terminal?cursor='+cursor+'&generation='+generation);if(state.reset)raw='';raw=(raw+state.output).slice(-524288);cursor=state.cursor;generation=state.generation;consoleRunning=state.running;$('output').textContent=plain(raw)||'高级控制台尚未启动';$('console-status').textContent=consoleRunning?'运行中':'未运行';$('terminal-input').disabled=!consoleRunning;$('send').disabled=!consoleRunning;$('terminal-input').type=(!state.echo||hiddenInput)?'password':'text';if($('follow').checked)$('output').scrollTop=$('output').scrollHeight}
$('advanced').ontoggle=()=>{if($('advanced').open&&csrf)pollConsole().catch(e=>{if(!isSessionTransition(e))notice(e.message,true)})};
$('start').onclick=async()=>{try{await api('terminal/start',{});await pollConsole()}catch(e){if(isSessionTransition(e))return;notice(e.message,true)}};
$('stop').onclick=async()=>{if(!consoleRunning||!await confirmDialog('结束控制台会中断当前脚本操作。',{title:'结束高级控制台',accept:'结束控制台',danger:true}))return;try{await api('terminal/stop',{});await pollConsole();await refreshStatus()}catch(e){if(isSessionTransition(e))return;notice(e.message,true)}};
async function send(value){try{await api('terminal/input',{text:value});await pollConsole()}catch(e){if(isSessionTransition(e))return;notice(e.message,true)}}
$('input-form').onsubmit=async event=>{event.preventDefault();const field=$('terminal-input'),value=field.value;field.value='';await send(value+'\n')};$('enter').onclick=()=>send('\n');$('interrupt').onclick=async()=>{if(await confirmDialog('当前脚本操作将被中断。',{title:'中断当前操作',accept:'确认中断',danger:true}))send('\x03')};$('secret').onclick=()=>{hiddenInput=!hiddenInput;$('secret').textContent=hiddenInput?'显示输入':'隐藏输入';pollConsole().catch(e=>{if(!isSessionTransition(e))notice(e.message,true)})};


// Native settings for every original service option.
const extraDefaults={ENABLE_WEBSOCKET:true,LOGIN_RATELIMIT_MAX_BURST:'10',LOGIN_RATELIMIT_SECONDS:'60',WEB_VAULT_ENABLED:true,EMERGENCY_ACCESS_ALLOWED:true,ENABLE_DB_WAL:true,ADMIN_RATELIMIT_MAX_BURST:'5',ADMIN_RATELIMIT_SECONDS:'60',ADMIN_SESSION_LIFETIME:'20',TZ:'Asia/Shanghai'};
let wizardId='',resultSeen='',lastLive=0,remoteBrowse={},presetSubmitted=null,presetTouched=false;
function presetCatalog(){return cfg?.extras?.storage_presets||[]}
function presetById(id){return presetCatalog().find(item=>item.id===id)}
function presetForRemote(remote){
    const type=String(remote.type||'').toLowerCase(),fields=remote.fields||{},provider=String(fields.provider||'').toLowerCase();
    if(type==='s3'){
        if(String(fields.env_auth||'').toLowerCase()==='true'||!fields.access_key_id)return '';
        return provider==='cloudflare'?'r2':'s3';
    }
    if(type==='sftp'&&(fields.key_file||fields.key_pem||fields.key_use_agent))return '';
    if(type==='webdav'&&fields.bearer_token)return '';
    if(['webdav','sftp','ftp','local'].includes(type))return type;
    return '';
}
function renderPresetFields(presetId,values={}){
    const preset=presetById(presetId),box=$('preset-fields');if(!preset||!box)return;
    $('preset-description').textContent=preset.description||'';
    box.replaceChildren();
    const grid=document.createElement('div');grid.className='form-grid';
    const toggles=document.createElement('div');toggles.className='spaced';
    for(const field of preset.fields){
        const value=values[field.key]!==undefined?values[field.key]:field.default;
        if(field.type==='checkbox'){
            const label=document.createElement('label');label.className='toggle';
            const copy=document.createElement('div'),strong=document.createElement('strong'),small=document.createElement('small');
            strong.textContent=field.label;small.textContent=field.help||'';copy.append(strong,small);
            const input=document.createElement('input');input.type='checkbox';input.name=field.key;input.role='switch';
            input.checked=value===true||value==='true';
            const span=document.createElement('span');span.className='switch';
            label.append(copy,input,span);toggles.append(label);continue;
        }
        const label=document.createElement('label');label.className='field';
        const span=document.createElement('span');span.textContent=field.label;
        let input;
        if(field.type==='select'){
            input=document.createElement('select');
            for(const option of field.options||[])input.add(new Option(option.label,option.value));
            if(value!==undefined&&value!==null&&value!==''&&!Array.from(input.options).some(option=>option.value===value))input.add(new Option('当前设置：'+value,value));
        }else{
            input=document.createElement('input');
            input.type=field.type==='password'?'password':field.type==='number'?'number':'text';
            if(field.maxlength)input.maxLength=field.maxlength;
            if(field.min!==undefined)input.min=field.min;
            if(field.max!==undefined)input.max=field.max;
            if(field.pattern)input.pattern=field.pattern;
            if(field.placeholder)input.placeholder=field.placeholder;
            if(field.type==='password')input.autocomplete='new-password';
        }
        input.name=field.key;
        if(field.required)input.required=true;
        if(value!==undefined&&value!==null)input.value=String(value);
        if(field.pattern_message)input.title=field.pattern_message;
        const small=document.createElement('small');small.textContent=field.help||'';
        label.append(span,input,small);grid.append(label);
    }
    if(grid.children.length)box.append(grid);
    if(toggles.children.length)box.append(toggles);
}
function initPresetForm(){
    const form=$('preset-form');if(!form)return;
    const select=form.elements.preset;if(!select||select.options.length)return;
    for(const preset of presetCatalog())select.add(new Option(preset.label,preset.id));
    if(select.options.length)select.value=select.options[0].value;
    select.onchange=()=>renderPresetFields(select.value);
    renderPresetFields(select.value);
}
async function prefillPresetForm(remote){
    const presetId=presetForRemote(remote);if(!presetId)return false;
    const form=$('preset-form');if(!form)return false;
    if(presetTouched&&!await confirmDialog('常用存储表单里有未保存的内容，重新配置会覆盖。确认继续？',{title:'覆盖未保存的存储配置',accept:'继续预填'}))return true;
    initPresetForm();
    const select=form.elements.preset;if(!select||!Array.from(select.options).some(option=>option.value===presetId))return false;
    select.value=presetId;
    const fields=remote.fields||{},values={};
    const target=(cfg?.extras?.targets||[]).find(item=>item.name===remote.name);
    const remembered=String(remote.target||'');
    const directory=String(target?.directory||remembered).replace(/^\/+/,'');
    const parts=directory.split('/').filter(Boolean);
    if(presetId==='r2'||presetId==='s3'){values.bucket=parts[0]||'';values.path=parts.slice(1).join('/')}
    else if(directory)values.directory=String(target?.directory||remembered);
    for(const key of ['provider','region','endpoint','url','vendor','user','host','port','tls'])if(fields[key]!==undefined)values[key]=fields[key];
    if(presetId==='r2'){
        const match=/^https:\/\/([a-f0-9]{32})(\.[a-z0-9-]+)?\.r2\.cloudflarestorage\.com\/?$/i.exec(String(fields.endpoint||'').trim());
        if(match){values.account_id=match[1].toLowerCase();values.jurisdiction=(match[2]||'').replace(/^\./,'')}
        values.no_check_bucket=String(fields.no_check_bucket||'').toLowerCase()==='true';
    }else if(presetId==='s3')values.no_check_bucket=String(fields.no_check_bucket||'').toLowerCase()==='true';
    $('storage-dialog-title').textContent='编辑存储 · '+remote.name;
    form.elements.name.value=remote.name;
    renderPresetFields(presetId,values);
    form.elements.set_target.checked=!!target&&target.enabled!==false;
    dirty.delete('preset-form');
    presetTouched=false;
    showStorageMethod('preset');
    revealGroup(form);
    $('preset-card')?.scrollIntoView({behavior:'smooth',block:'center'});
    const first=form.querySelector('#preset-fields input:not([type=checkbox]),#preset-fields select');
    if(first)first.focus();
    return true;
}
function showStorageMethod(method){
    const common=method!=='wizard';$('preset-form').hidden=!common;$('storage-wizard').hidden=common;
    for(const [id,active]of [['storage-method-preset',common],['storage-method-wizard',!common]]){$(id).className=active?'primary':'secondary';$(id).setAttribute('aria-pressed',String(active))}
}
$('storage-method-preset').onclick=()=>showStorageMethod('preset');$('storage-method-wizard').onclick=()=>showStorageMethod('wizard');
$('new-storage').onclick=async()=>{
    if(wizardId&&!$('wizard-question-form').hidden){openWorkspaceDialog('storage-dialog');return}
    $('storage-dialog-title').textContent='新增存储';
    if(presetTouched&&!await confirmDialog('还有未保存的存储配置，新增会清空这些输入。',{title:'新增存储',accept:'继续新增'}))return;
    initPresetForm();const form=$('preset-form');form.elements.name.value='';renderPresetFields(form.elements.preset.value);form.elements.set_target.checked=true;dirty.delete('preset-form');presetTouched=false;
    showStorageMethod('preset');revealGroup(form);requestAnimationFrame(()=>{form.scrollIntoView({behavior:'smooth',block:'center'});form.elements.name.focus()});
};
function remoteDefaultDirectory(name){
    const extra=cfg?.extras||{},remote=(extra.remotes||[]).find(item=>item.name===name),target=(extra.targets||[]).find(item=>item.name===name);
    return target?.directory||remote?.target||(remote?.type==='local'?'/bitwarden/backup/archive':remote?.type==='s3'?'':'/');
}
function clearRemoteFiles(){ $('remote-files').replaceChildren();$('remote-files-context').textContent='' }
function syncRemoteDirectory(force=false){
    const form=$('remote-browse-form'),input=form.elements.directory,select=form.elements.name,next=remoteDefaultDirectory(select.value);
    if(force||!input.value||input.value===input.dataset.defaultDir){if(input.value!==next)clearRemoteFiles();input.value=next}
    input.dataset.defaultDir=next;input.placeholder=(cfg?.extras?.remotes||[]).find(item=>item.name===select.value)?.type==='s3'?'例如 bucket/VaultBackup':'填写保存目录';
    const submit=form.querySelector('button');submit.disabled=!select.value||taskRunning||panelUpdating;
}
function refreshRemoteBrowser(extra){
    const form=$('remote-browse-form'),select=form.elements.name,previous=select.value;select.replaceChildren();
    for(const remote of extra.remotes||[])select.add(new Option(remote.name+' · '+(remote.fields?.provider==='Cloudflare'?'R2':remote.type),remote.name));
    if(Array.from(select.options).some(option=>option.value===previous))select.value=previous;
    if(previous!==select.value)clearRemoteFiles();syncRemoteDirectory(previous!==select.value);
}
$('remote-browse-form').elements.name.addEventListener('change',()=>{clearRemoteFiles();syncRemoteDirectory(true)});
$('remote-browse-form').elements.directory.addEventListener('input',clearRemoteFiles);
function openRemoteBrowser(name){
    const form=$('remote-browse-form');location.hash='remote';form.elements.name.value=name;clearRemoteFiles();syncRemoteDirectory(true);revealGroup(form);
    requestAnimationFrame(()=>form.scrollIntoView({behavior:'smooth',block:'center'}));
}
function renderStorageList(extra){
    const box=$('remote-list');box.replaceChildren();
    if(!extra.remotes.length){const empty=document.createElement('p');empty.className='empty';empty.textContent='还没有连接存储。点击右上角“新增存储”，选择 WebDAV、R2 或其他云盘。';box.append(empty)}
    for(const remote of extra.remotes){
        const row=document.createElement('article');row.className='storage-row';const copy=document.createElement('div');copy.className='storage-row-copy';
        const head=document.createElement('div');head.className='remote-row-head';const title=document.createElement('strong'),badge=document.createElement('span');title.textContent=remote.name;badge.className='badge';badge.textContent=remote.fields?.provider==='Cloudflare'?'Cloudflare R2':remote.type==='local'?'本机目录':remote.type;head.append(title,badge);
        const path=document.createElement('p');path.className='muted';path.textContent=(extra.targets.some(item=>item.name===remote.name&&item.enabled!==false)?'备份目标 · ':'保存位置 · ')+(remoteDefaultDirectory(remote.name)||'尚未设置目录');copy.append(head,path);
        const actions=document.createElement('div');actions.className='storage-row-actions';
        const browse=document.createElement('button');browse.type='button';browse.className='secondary';browse.textContent='浏览备份';browse.onclick=()=>openRemoteBrowser(remote.name);
        const edit=document.createElement('button');edit.type='button';edit.className='secondary';edit.textContent='重新配置 / 授权';edit.onclick=async()=>{if(await prefillPresetForm(remote))return;showStorageMethod('wizard');const form=$('wizard-start-form');form.elements.name.value=remote.name;if(!Array.from(form.elements.type.options).some(option=>option.value===remote.type))form.elements.type.add(new Option(remote.type,remote.type));form.elements.type.value=remote.type;revealGroup(form);requestAnimationFrame(()=>form.scrollIntoView({behavior:'smooth',block:'center'}))};
        const more=document.createElement('details');more.className='storage-more';const summary=document.createElement('summary');summary.textContent='更多';more.append(summary);
        for(const [action,label]of [['remote-check','验证连接'],['remote-about','容量与身份'],['remote-delete','删除配置']]){const button=document.createElement('button');button.type='button';button.className='ghost'+(action==='remote-delete'?' danger-text':'');button.textContent=label;button.onclick=async()=>{let confirmation='';if(action==='remote-delete'){confirmation=await promptDialog('删除 '+remote.name+' 的连接配置，存储中的备份文件会保留。',{title:'删除存储配置',accept:'删除配置',danger:true,inputLabel:'输入 DELETE 确认',expected:'DELETE',placeholder:'DELETE'})||'';if(confirmation!=='DELETE')return}more.open=false;startTask(action,{name:remote.name,directory:remoteDefaultDirectory(remote.name),confirmation})};more.append(button)}
        const view=document.createElement('button');view.type='button';view.className='secondary';view.textContent='查看配置';view.onclick=()=>{$('storage-config-content').textContent=JSON.stringify(remote.fields,null,2);$('storage-config-dialog-title').textContent=remote.name+' · 存储配置';openWorkspaceDialog('storage-config-dialog')};
        actions.append(browse,edit,view,more);row.append(copy,actions);box.append(row);
    }
}

function renderTargetList(extra){
    const rows=extra.targets||[],box=$('backup-target-list');box.replaceChildren();const active=rows.filter(row=>row.enabled!==false).length,pending=cfg.application?.backup==='pending';
    $('target-enabled-count').textContent=active+' 个启用 / '+rows.length+' 个目标';
    const backupRunning=environment?.containers?.some(row=>row.Names==='vaultwarden-backup'&&row.State==='running');
    $('target-service-hint').textContent=!cfg.backup_configured?'请先在备份设置中保存备份计划。':!active?'没有启用的目标，备份不会投递。':pending?'有已保存的备份配置尚未应用。目标开关会应用当前配置，也可在备份设置中手动应用。':backupRunning?'开关会自动应用当前已保存的备份配置；停用只停止向该位置投递。':'备份服务未运行。启用的目标会在备份服务启动后按计划投递。';
    if(!rows.length){const empty=document.createElement('p');empty.className='empty';empty.textContent='还没有备份目标。保存备份计划后，可添加本机目录或已连接的远程存储。';box.append(empty)}
    for(const [index,target]of rows.entries()){
        const row=document.createElement('div');row.className='storage-row target-list-row';row.dataset.targetId=target.id||'legacy-'+index;
        const copy=document.createElement('div');copy.className='storage-row-copy';const heading=document.createElement('div');heading.className='remote-row-head';const title=document.createElement('strong'),badge=document.createElement('span');title.textContent=target.name;badge.className='badge'+(target.enabled!==false&&!pending?' success':'');badge.textContent=target.enabled===false?'已停用':pending?'启用 · 待应用':'已启用';heading.append(title,badge);const path=document.createElement('p');path.className='muted';const source=(extra.remotes||[]).find(item=>item.name===target.name);path.textContent=(source?.type==='local'?'本机目录':'远程存储')+' · '+target.directory;copy.append(heading,path);
        const actions=document.createElement('div');actions.className='storage-row-actions';
        const toggle=document.createElement('label');toggle.className='toggle target-flag';const text=document.createElement('span');text.textContent=target.enabled===false?'停用':'启用';const input=document.createElement('input');input.type='checkbox';input.setAttribute('role','switch');input.setAttribute('aria-label',target.name+' 启用状态');input.checked=target.enabled!==false;input.dataset.targetControl='';const visual=document.createElement('span');visual.className='switch';input.onchange=async()=>{input.disabled=true;if(!await startTask('backup-target-toggle',{id:target.id,enabled:input.checked}))renderTargetList(cfg.extras)};toggle.append(text,input,visual);
        const edit=document.createElement('button');edit.type='button';edit.className='secondary';edit.dataset.targetControl='';edit.textContent='编辑';edit.onclick=()=>openTargetEditor(target);
        const remove=document.createElement('button');remove.type='button';remove.className='ghost danger-text';remove.dataset.targetControl='';remove.textContent='移除';remove.onclick=async()=>{if(!await confirmDialog('移除 '+target.name+' 的这个备份目标？已有备份文件会保留。',{title:'移除备份目标',accept:'移除目标',danger:true}))return;startTask('backup-target-save',{targets:rows.filter(item=>item!==target)})};actions.append(toggle,edit,remove);row.append(copy,actions);box.append(row);
    }
}
function openTargetEditor(target=null){
    if(!cfg?.backup_configured)return notice('请先在备份设置中保存备份计划。',true);
    const form=$('target-editor-form'),select=form.elements.name;select.replaceChildren(new Option('请选择存储',''));
    for(const remote of cfg.extras.remotes)select.add(new Option(remote.name+' · '+(remote.type==='local'?'本机目录':remote.type),remote.name));
    if(target?.name&&!cfg.extras.remotes.some(remote=>remote.name===target.name)){const missing=new Option(target.name+'（连接未找到）',target.name);missing.disabled=true;select.add(missing)}
    form.elements.target_id.value=target?.id||'';select.value=target?.name||'';form.elements.directory.value=target?.directory||'';form.elements.enabled.checked=target?.enabled!==false;
    $('target-dialog-title').textContent=target?'编辑备份目标':'添加备份目标';dirty.delete('target-editor-form');openWorkspaceDialog('target-dialog');
}
$('new-backup-target').onclick=()=>openTargetEditor();
$('target-editor-form').elements.name.onchange=()=>{const form=$('target-editor-form');form.elements.directory.value=remoteDefaultDirectory(form.elements.name.value)};
$('target-editor-form').onsubmit=async event=>{
    event.preventDefault();const form=event.currentTarget,data=formValues('target-editor-form'),targets=(cfg.extras.targets||[]).map(row=>({...row}));
    data.id=data.target_id;delete data.target_id;
    const index=targets.findIndex(row=>row.id===data.id);if(index>=0)targets[index]=data;else{delete data.id;targets.push(data)}
    form.querySelector('button[type=submit]').disabled=true;
    try{await startTask('backup-target-save',{targets})}finally{form.querySelector('button[type=submit]').disabled=taskRunning}
};
for(const link of document.querySelectorAll('[data-open-targets]'))link.onclick=event=>{event.preventDefault();location.hash='targets'};

function renderExtras(config){const extra=config.extras;if(!extra)return;renderEnvironmentEditors(extra.environment);fill('advanced-form',{...extraDefaults,...Object.fromEntries(Object.entries(extra.advanced).filter(([,v])=>v!==''))});fill('push-form',extra.push);fill('notifications-form',{MAIL_SMTP_ENABLE:false,MAIL_WHEN_SUCCESS:false,MAIL_WHEN_FAILURE:true,MAIL_FORCE_THREAD:false,...extra.notifications,...extra.notification_smtp});renderStorageList(extra);renderTargetList(extra);refreshRemoteBrowser(extra);const restoreSelect=$('restore-form').elements.file,current=restoreSelect.value;restoreSelect.replaceChildren(new Option('选择一份备份',''));for(const file of config.files.filter(f=>/\.(zip|7z)$/i.test(f.name)))restoreSelect.add(new Option(file.name+' · '+formatSize(file.size),file.name));if(Array.from(restoreSelect.options).some(o=>o.value===current))restoreSelect.value=current;initPresetForm();}
for(const [id,section]of [['advanced-form','advanced'],['push-form','push'],['notifications-form','notifications']]){$(id).onsubmit=async event=>{event.preventDefault();const epoch=authEpoch;const form=event.currentTarget,button=form.querySelector('button[type=submit]');button.disabled=true;try{const result=await api('config/'+section,formValues(id));dirty.delete(id);form.querySelectorAll('input[type=password]').forEach(i=>i.value='');notice(result.message);await loadConfig()}catch(e){if(isSessionTransition(e))return;notice(e.message,true)}finally{if(epoch===authEpoch)button.disabled=false}}}
if($('reload-remotes'))$('reload-remotes').onclick=loadConfig;$('load-providers').onclick=()=>{showStorageMethod('wizard');revealGroup($('wizard-start-form'));startTask('remote-providers')};
$('wizard-start-form').onsubmit=async event=>{event.preventDefault();const values=formValues('wizard-start-form');if(cfg?.extras?.remotes.some(r=>r.name===values.name)&&!await confirmDialog('将重新配置 '+values.name+'，向导完成前不会修改原配置。',{title:'重新配置远程存储',accept:'开始配置'}))return;await startTask('remote-wizard',values)};
$('wizard-question-form').onsubmit=async event=>{event.preventDefault();if(browserOAuthWizard)return submitBrowserOAuth();const value=$('wizard-answer').value;if(await startTask('remote-continue',{wizard_id:wizardId,answer:value}))$('wizard-answer').value=''};
if($('preset-form'))$('preset-form').onsubmit=async event=>{event.preventDefault();const values=formValues('preset-form');presetSubmitted=JSON.stringify(values);if(!await startTask('remote-preset-save',values))presetSubmitted=null};
if($('preset-form')){const markPresetDraft=event=>{if(event.target.name!=='preset')presetTouched=true};$('preset-form').addEventListener('input',markPresetDraft);$('preset-form').addEventListener('change',markPresetDraft)}
function renderHelp(box,help){const regex=/https?:\/\/[^\s<>"']+/g;let index=0;for(const match of help.matchAll(regex)){box.append(document.createTextNode(help.slice(index,match.index)));const link=document.createElement('a');link.href=match[0];link.target='_blank';link.rel='noopener noreferrer';link.textContent=match[0];box.append(link);index=match.index+match[0].length}box.append(document.createTextNode(help.slice(index)))}
let browserOAuthWizard='';
function renderBrowserOAuth(result){
    wizardId=result.wizard_id;browserOAuthWizard=wizardId;showStorageMethod('wizard');
    const provider=result.oauth_available?.provider||result.browser_oauth?.provider||'云盘',box=$('wizard-question');box.replaceChildren();
    const heading=document.createElement('h3');heading.textContent='连接 '+provider;box.append(heading);
    const intro=document.createElement('p');intro.className='muted';intro.textContent='不用安装 Rclone，也不用运行命令。面板会替你生成授权链接并保存授权结果。';box.append(intro);
    const steps=document.createElement('ol');steps.className='oauth-steps';
    for(const text of ['打开服务商授权页面，登录并同意访问。','授权后可能显示“无法访问此网站”，这是因为返回地址指向你自己的电脑；请复制地址栏里的完整地址。','回到这里粘贴地址，面板自动完成验证和保存。']){const item=document.createElement('li');item.textContent=text;steps.append(item)}box.append(steps);
    const actions=document.createElement('div');actions.className='button-row spaced';const generate=document.createElement('button');generate.type='button';generate.className=result.browser_oauth?'secondary':'primary';generate.textContent=result.browser_oauth?'重新生成链接':'生成授权链接';generate.onclick=()=>startTask('remote-oauth-start',{wizard_id:wizardId});actions.append(generate);
    if(result.browser_oauth){const link=document.createElement('a');link.className='primary';link.textContent='打开 '+provider+' 授权 ↗';link.href=result.browser_oauth.url;link.target='_blank';link.rel='noopener noreferrer';actions.prepend(link)}box.append(actions);
    if(result.browser_oauth){const label=document.createElement('label');label.className='field spaced';const title=document.createElement('span');title.textContent='粘贴授权后的完整返回地址';const input=document.createElement('input');input.id='oauth-callback';input.type='password';input.autocomplete='off';input.maxLength=20000;input.placeholder='http://127.0.0.1:53682/?state=…&code=…';label.append(title,input);box.append(label);const finish=document.createElement('button');finish.id='oauth-complete';finish.type='button';finish.className='primary spaced';finish.textContent='验证并完成授权';finish.onclick=()=>submitBrowserOAuth();input.onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();submitBrowserOAuth()}};box.append(finish)}
    const manual=document.createElement('details');manual.className='oauth-manual spaced';const summary=document.createElement('summary');summary.textContent='高级：已有 Rclone 授权结果，手动填写';const input=document.createElement('input');input.id='wizard-answer';input.type='password';input.maxLength=60000;input.placeholder='粘贴已有授权结果';input.autocomplete='off';const submit=document.createElement('button');submit.className='secondary spaced';submit.type='button';submit.textContent='使用已有授权结果';submit.onclick=async()=>{if(!input.value)return notice('请先填写授权结果。',true);if(await startTask('remote-continue',{wizard_id:wizardId,answer:input.value}))input.value=''};manual.append(summary,input,submit);box.append(manual);
    $('wizard-next').hidden=true;$('wizard-question-form').hidden=false;revealCurrentGroup($('wizard-question-form'));notice('请在远程存储页面继续云盘授权。',false,{kind:'info'});
}
async function submitBrowserOAuth(){const input=$('oauth-callback');if(!input?.value)return notice('请先粘贴授权后地址栏中的完整返回地址。',true);if(await startTask('remote-oauth-complete',{wizard_id:browserOAuthWizard,callback_url:input.value}))input.value=''}
const wizardChinese={client_id:'应用 ID（可留空，使用默认应用）',client_secret:'应用密钥（可留空）',root_folder_id:'保存到哪个文件夹（留空使用云盘根目录）',service_account_file:'服务账号文件（个人云盘留空）',config_team_drive:'是否使用共享云端硬盘',config_driveid:'选择要连接的云盘',drive_type:'云盘类型',config_refresh_token:'是否重新授权云盘',config_verification_code:'服务商返回的验证码',scope:'允许面板访问哪些文件'};
const wizardHelpChinese={client_id:'普通使用可直接下一步；只有自己创建了 OAuth 应用时才需要填写。',client_secret:'与自定义应用 ID 配套；使用默认应用时留空即可。',scope:'备份需要读取和写入文件。请选择符合需要的权限，随后会在服务商页面再次确认。',root_folder_id:'不填则使用云盘根目录；备份保存的子目录在备份设置中填写。',service_account_file:'个人账号直接留空。服务账号是另一种授权方式，仅在你已准备对应文件时使用。',config_team_drive:'个人 Google Drive 请选择否；团队共享盘请选择是，再选择共享盘。'};

function renderResult(job){if(resultSeen===job.id)return;resultSeen=job.id;const result=job.result;if(result.browser_oauth||(result.question&&result.oauth_available)){renderBrowserOAuth(result);return}if(result.providers){const select=$('wizard-start-form').elements.type,old=select.value;select.replaceChildren();for(const p of result.providers)select.add(new Option(p.description+' ('+p.name+')',p.name));select.value=old;revealCurrentGroup($('wizard-start-form'));notice('全部存储类型已加载。')}if(result.question){browserOAuthWizard='';$('wizard-next').hidden=false;wizardId=result.wizard_id;const option=result.question,box=$('wizard-question');box.replaceChildren();const title=document.createElement('label');title.className='question-label';title.htmlFor='wizard-answer';title.textContent=wizardChinese[option.Name]||({client_id:'客户端 ID（通常可留空）',client_secret:'客户端密钥',config_token:'授权令牌',config_is_local:'使用服务器上的浏览器授权（远程服务器固定为否）',host:'服务器地址',user:'登录账号',pass:'登录密码',port:'端口',vendor:'服务商类型',scope:'授权范围',key_file:'私钥文件路径'})[option.Name]||option.Name;const help=document.createElement('div');help.className='question-help';renderHelp(help,wizardHelpChinese[option.Name]||option.Help||'');let input;if(option.Exclusive&&option.Examples?.length){input=document.createElement('select');for(const item of option.Examples)input.add(new Option(({drive:'全部文件（备份和读取已有文件）','drive.file':'仅本应用创建的文件','drive.readonly':'仅读取（不能用于上传备份）','drive.appfolder':'应用专用文件夹','drive.metadata.readonly':'仅文件信息（不能用于备份）',true:'是',false:'否'})[String(item.Value)]||item.Help+' · '+item.Value,item.Value))}else{input=document.createElement('input');input.type=option.IsPassword||/token|secret|pass/i.test(option.Name)?'password':'text';input.maxLength=60000;if(option.Examples?.length){const hints=document.createElement('p');hints.className='muted';hints.textContent='可选值：'+option.Examples.map(v=>v.Value+' ('+v.Help+')').join('、');box.append(hints)}}input.id='wizard-answer';input.required=!!option.Required;input.value=option.Default===null?'':String(option.Default??'');if(option.Name==='config_is_local'){input.value='false';input.disabled=true}box.append(title,help,input);if(result.error){const error=document.createElement('p');error.textContent=result.error;error.className='danger-text';box.append(error)}$('wizard-question-form').hidden=false;revealCurrentGroup($('wizard-question-form'));notice('请按向导填写下一项。')}if(result.done){browserOAuthWizard='';$('wizard-question-form').hidden=true;notice('存储配置已保存，可验证连接并添加为备份目标。')}if(result.remote_files){
    const origin={...(result.remote_location||remoteBrowse)},form=$('remote-browse-form');
    if(origin.name!==form.elements.name.value||String(origin.directory||'').replace(/^\/+|\/+$/g,'')!==form.elements.directory.value.replace(/^\/+|\/+$/g,''))return;
    const box=$('remote-files');box.replaceChildren();$('remote-files-context').textContent=origin.name+' · '+origin.directory;
    for(const file of result.remote_files){const row=document.createElement('div');row.className='file-row';const name=document.createElement('strong'),size=document.createElement('span'),date=document.createElement('span'),button=document.createElement('button');name.textContent=file.name;size.textContent=formatSize(file.size);date.textContent=file.modified;button.type='button';button.className='secondary';button.textContent='下载到服务器';button.onclick=()=>startTask('remote-download',{...origin,file:file.name});row.append(name,size,date,button);box.append(row)}
    if(!result.remote_files.length){const empty=document.createElement('p');empty.className='empty';empty.textContent='该目录没有 zip / 7z 备份。首次使用时，请先执行一次备份。';box.append(empty)}
}}

$('remote-browse-form').onsubmit=async event=>{event.preventDefault();remoteBrowse=formValues('remote-browse-form');await startTask('remote-list',remoteBrowse)};
$('restore-form').onsubmit=async event=>{event.preventDefault();const values=formValues('restore-form');if(values.confirmation!=='RESTORE')return notice('请输入 RESTORE 确认覆盖恢复。',true);if(!values.file)return notice('请选择备份文件。',true);if(await startTask('restore',values)){$('restore-form').elements.password.value='';$('restore-form').elements.confirmation.value=''}};
function removalDescription(){
    const form=$('remove-form'),scope=form.elements.scope.value,deleteData=form.elements.delete_data.value==='delete';
    const names={backup:'备份服务',core:'密码库主服务',all:'密码库与备份服务'};
    const contents={backup:'本机备份文件及备份配置（含远程存储配置）',core:'密码库数据库、附件及主服务配置',all:'密码库数据库、附件、本机备份文件及所选服务配置'};
    return '卸载'+names[scope]+'。'+(deleteData?'同时永久删除'+contents[scope]+'，无法撤销。':'保留数据和配置，可重新部署使用。')+(scope==='core'?'备份服务也会暂停，其数据保留。':'')+(form.elements.remove_proxy.checked?'同时移除面板安装的反代，会影响其所有站点。':'')+'云盘内文件不会删除，网页面板保持运行。';
}
$('remove-form').onsubmit=async event=>{event.preventDefault();const values=formValues('remove-form');values.delete_data=values.delete_data==='delete';if(!await confirmDialog(removalDescription(),{title:'卸载服务',accept:values.delete_data?'卸载并删除数据':'确认卸载',danger:true}))return;await startTask('remove-services',{...values,confirmation:'DELETE'})};
$('remove-form').onchange=()=>{$('remove-summary').textContent=removalDescription()};
$('remove-summary').textContent=removalDescription();
$('upload-form').onsubmit=event=>{event.preventDefault();const file=$('backup-upload').files[0];if(!file)return;const epoch=authEpoch;const xhr=new XMLHttpRequest();activeUploads.add(xhr);xhr.onloadend=()=>activeUploads.delete(xhr);xhr.open('POST','/api/backup/upload?name='+encodeURIComponent(file.name));xhr.setRequestHeader('Content-Type','application/octet-stream');xhr.setRequestHeader('X-CSRF-Token',csrf);const button=$('upload-form').querySelector('button');button.disabled=true;xhr.upload.onprogress=e=>{if(epoch!==authEpoch)return;$('upload-status').textContent=e.lengthComputable?'上传 '+Math.round(e.loaded/e.total*100)+'%':'上传中…'};xhr.onload=async()=>{if(epoch!==authEpoch)return;button.disabled=false;try{if(xhr.status===401){expireSession();return}const result=JSON.parse(xhr.responseText);if(xhr.status!==200)throw Error(result.error||'上传失败');$('upload-status').textContent='上传完成';await loadConfig();if(epoch!==authEpoch)return;$('restore-form').elements.file.value=result.file;$('restore-form').elements.confirmation.value='';$('import-backup-dialog').close();notice('备份已导入。可在文件列表中选择恢复。')}catch(e){if(isSessionTransition(e))return;notice(e.message,true)}};xhr.onerror=()=>{if(epoch!==authEpoch)return;button.disabled=false;notice('上传连接中断，请重新上传。',true)};xhr.send(file)};
const featureMap=[[1,'生成主服务配置','vault'],[2,'初始化备份服务','backup'],[3,'配置远程存储','remote'],[4,'启动 / 应用主服务','deploy'],[5,'停止主服务','stop'],[6,'启动 / 应用备份','backup-start'],[7,'停止备份服务','backup-stop'],[8,'升级服务镜像','upgrade'],[9,'加密管理口令','vault'],[10,'修改主服务配置','vault'],[11,'修改备份配置','backup'],[12,'立即备份','backup-now'],[13,'访问信息与状态','info'],[14,'服务日志','tasks'],[15,'备份诊断','backup-diagnose'],[16,'恢复备份数据','restore'],[17,'环境体检','doctor'],[18,'卸载与清理','cleanup']];
for(const [id,label,target]of featureMap){const button=document.createElement('button');button.className='feature-link';const number=document.createElement('b');number.textContent=String(id).padStart(2,'0');button.append(number,document.createTextNode(label));button.onclick=()=>target in names?location.hash=target:startTask(target);$('feature-map').append(button)}
setInterval(async()=>{if(!csrf||location.hash!=='#tasks'||!$('live-logs').checked||taskRunning||Date.now()-lastLive<5000)return;lastLive=Date.now();try{await api('task/start',{action:$('log-service').value,...($('log-service').value==='proxy-logs'?{proxy_id:$('proxy-log-source').value}:{})});await poll()}catch(e){if(isSessionTransition(e))return;notice(e.message,true)}},1000);



// Progressive disclosure: keep forms in the same document and preserve their values.
let lastSubmittedForm=null;
function revealCurrentGroup(element){const modal=element?.closest('dialog.workspace-dialog');if(modal&&!modal.open)return;if(!element?.closest('[data-view]')?.hidden)revealGroup(element)}
function revealGroup(element){
    if(!element)return;
    const modal=element.closest('dialog.workspace-dialog');if(modal)openWorkspaceDialog(modal.id);
    const view=element.closest('[data-view]');
    if(view?.hidden)location.hash=view.dataset.view;
    let group=element.closest('details.config-group')||element.querySelector?.('details.config-group');
    while(group){if(group.id==='preset-card')group.hidden=false;group.open=true;group=group.parentElement.closest('details.config-group')}
}
// The catalog distinguishes dedicated form fields from optional upstream settings.
const environmentEditors=new Map();
function renderEnvironmentEditors(catalog){
    for(const scope of ['core','backup']){
        const form=$(scope+'-env-form');
        if(!catalog?.[scope]){form.querySelector('.env-selection').textContent='读取配置目录后即可按需添加。';continue}
        if(dirty.has(form.id))continue;
        const state={specs:new Map(catalog[scope].map(item=>[item.key,item])),rows:new Map(),removed:new Set()};
        environmentEditors.set(scope,state);
        form.querySelector('.env-source').href=catalog.sources[scope].url;
        form.querySelector('.env-rows').replaceChildren();
        for(const spec of state.specs.values())if(spec.present&&(spec.editable||spec.category==='未收录配置'))addEnvironmentRow(scope,spec);
        updateEnvironmentPicker(scope);
    }
}
function environmentChanged(scope){
    const form=$(scope+'-env-form');dirty.add(form.id);
    const group=form.closest('.config-group');if(group)group.dataset.changed='true';
    updateGroupSummaries();renderPresentation();
}
function updateEnvironmentPicker(scope){
    const form=$(scope+'-env-form'),state=environmentEditors.get(scope);if(!state)return;
    const query=form.querySelector('.env-search').value.trim().toLowerCase(),select=form.querySelector('.env-select'),previous=select.value;
    select.replaceChildren();
    for(const spec of state.specs.values()){
        if(state.rows.has(spec.key)||![spec.key,spec.label,spec.category,spec.location,spec.help].join(' ').toLowerCase().includes(query))continue;
        const option=new Option(spec.key+' · '+(spec.editable?spec.label:spec.location),spec.key);select.add(option);
    }
    if(Array.from(select.options).some(option=>option.value===previous))select.value=previous;
    updateEnvironmentSelection(scope);
    form.querySelector('.env-empty').hidden=state.rows.size>0;
}
function updateEnvironmentSelection(scope){
    const form=$(scope+'-env-form'),state=environmentEditors.get(scope),spec=state?.specs.get(form.querySelector('.env-select').value),button=form.querySelector('.env-add');
    button.disabled=!spec||(!spec.editable&&!spec.page)||(spec?.location||'').startsWith('由容器');
    button.textContent=spec&&!spec.editable?'前往设置':'添加';
    form.querySelector('.env-selection').textContent=spec?(spec.editable?spec.help+' 默认值：'+(spec.secret?'请参阅官方说明':spec.default||'由服务决定'):'已有专用入口：'+spec.location+'。'+spec.help):'没有匹配的选项；已添加的配置在下方显示。';
}
function addEnvironmentRow(scope,spec){
    const form=$(scope+'-env-form'),state=environmentEditors.get(scope);if(!state||state.rows.has(spec.key))return;
    const row=document.createElement('div');row.className='env-row';row.dataset.key=spec.key;
    const label=document.createElement('label');label.className='field';
    const heading=document.createElement('span');heading.textContent=spec.label===spec.key?spec.key:spec.label+' · '+spec.key;
    let input;
    if(spec.type==='bool'&&!spec.secret){input=document.createElement('select');for(const value of ['true','false'])input.add(new Option(value==='true'?'开启 · true':'关闭 · false',value));input.value=(spec.value||spec.default||'false').toLowerCase()}
    else{input=document.createElement('input');input.type=spec.secret?'password':spec.type==='number'?'number':'text';input.autocomplete='off';input.spellcheck=false;input.value=spec.secret?'':spec.present?spec.value:(spec.default.includes('${')?'':spec.default);input.placeholder=spec.secret?(spec.present?'已设置 · 留空保留原值':'填写新值，保存后隐藏'):'填写配置值';if(spec.type==='number')input.step='1'}
    input.setAttribute('aria-label',spec.key);input.disabled=!spec.editable;
    const help=document.createElement('small');help.textContent=!spec.editable?spec.help:spec.overridden?'此项被密码库 /admin 中的配置覆盖，请先在 /admin 中核对。':spec.secret?'敏感值不回显；留空保留，点击恢复默认才会移除。':spec.help;
    label.append(heading,input,help);row.append(label);
    if(spec.editable){const remove=document.createElement('button');remove.type='button';remove.className='ghost env-remove';remove.textContent=spec.present?'恢复默认':'移除';remove.setAttribute('aria-label',spec.key+' 恢复默认');remove.onclick=()=>{state.rows.delete(spec.key);if(spec.present)state.removed.add(spec.key);row.remove();updateEnvironmentPicker(scope);environmentChanged(scope)};row.append(remove)}
    input.addEventListener('input',()=>environmentChanged(scope));input.addEventListener('change',()=>environmentChanged(scope));
    state.removed.delete(spec.key);state.rows.set(spec.key,{spec,input});form.querySelector('.env-rows').append(row);
}
for(const scope of ['core','backup']){
    const form=$(scope+'-env-form');
    form.querySelector('.env-search').addEventListener('input',()=>updateEnvironmentPicker(scope));
    form.querySelector('.env-select').addEventListener('change',()=>updateEnvironmentSelection(scope));
    form.querySelector('.env-add').onclick=()=>{
        const state=environmentEditors.get(scope),spec=state?.specs.get(form.querySelector('.env-select').value);if(!spec)return;
        if(!spec.editable){const forms={'基本信息':'core-form','注册与权限':'permissions-form','运行与安全设置':'advanced-form','官方推送':'push-form','管理口令加密设置':'token-form','邮件服务':'mail-form','备份计划':'backup-form','备份邮件通知':'notifications-form','备份目标与保存目录':'backup-target-list'};location.hash=spec.page;const target=$(forms[spec.location]);if(target){revealGroup(target);target.scrollIntoView({behavior:'smooth',block:'center'})}return}
        addEnvironmentRow(scope,spec);updateEnvironmentPicker(scope);environmentChanged(scope);state.rows.get(spec.key).input.focus();
    };
    form.onsubmit=async event=>{
        event.preventDefault();const state=environmentEditors.get(scope);if(!state)return;
        const values=Object.fromEntries(Array.from(state.rows).filter(([,row])=>row.spec.editable).map(([key,row])=>[key,row.input.value]));
        const button=form.querySelector('button[type=submit]'),epoch=authEpoch;button.disabled=true;
        try{const result=await api('config/'+scope+'-env',{values,remove:[...state.removed]});dirty.delete(form.id);form.querySelectorAll('input[type=password]').forEach(input=>input.value='');notice(result.message);await loadConfig()}
        catch(error){if(!isSessionTransition(error))notice(error.message,true)}finally{if(epoch===authEpoch)button.disabled=false}
    };
}

const workspaceNoticeHome=$('notice').parentElement;
function closeWorkspaceDialogs(){for(const dialog of document.querySelectorAll('dialog.workspace-dialog[open]'))dialog.close()}
function openWorkspaceDialog(id){
    if(authPhase!=='authenticated')return;
    const dialog=$(id);if(!dialog)return;
    for(const other of document.querySelectorAll('dialog.workspace-dialog[open]'))if(other!==dialog)other.close();
    const context=dialog.querySelector('[data-view]');if(context?.hidden){location.hash=context.dataset.view;route()}
    const content=dialog.querySelector('.dialog-content-card');if(content){content.hidden=false;content.open=true}
    dialog.append($('notice'));
    if(!dialog.open)dialog.showModal();
}
function mountWorkspaceDialogs(){
    for(const [id,source]of [['storage-dialog',$('preset-card')],['remote-files-dialog',$('remote-browser-card')],['import-backup-dialog',$('upload-form').closest('.config-group')],['restore-backup-dialog',$('restore-form').closest('.config-group')]]){
        source.classList.add('dialog-content-card');source.open=true;$(id).querySelector('.workspace-dialog-body').append(source);
    }
    for(const dialog of document.querySelectorAll('dialog.workspace-dialog')){
        dialog.addEventListener('close',()=>{if(dialog.id==='storage-dialog')$('preset-card').hidden=true;const active=document.querySelector('dialog.workspace-dialog[open]');(active||workspaceNoticeHome).append($('notice'))});
        dialog.addEventListener('click',event=>{if(event.target===dialog){const rect=dialog.getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)dialog.close()}});
    }
    for(const button of document.querySelectorAll('[data-close-workspace]'))button.onclick=()=>$(button.dataset.closeWorkspace).close();
    $('open-import-backup').onclick=()=>openWorkspaceDialog('import-backup-dialog');
}

function groupIcon(title){
    const box=document.createElement('span');box.className='number';box.setAttribute('aria-hidden','true');
    const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('fill','none');svg.setAttribute('stroke','currentColor');svg.setAttribute('stroke-width','1.7');svg.setAttribute('stroke-linecap','round');svg.setAttribute('stroke-linejoin','round');
    const path=document.createElementNS('http://www.w3.org/2000/svg','path');
    path.setAttribute('d',/导入|上传/.test(title)?'M12 16V3m-5 5 5-5 5 5M4 16v5h16v-5':/恢复/.test(title)?'M4 11a8 8 0 1 1 1 6M4 4v7h7':/邮件|SMTP/.test(title)?'M3 5h18v14H3zM3 6l9 7 9-7':/存储|远程|云/.test(title)?'M7 18H5a4 4 0 0 1-.7-7.9 7 7 0 0 1 13.4 0A4 4 0 0 1 19 18h-2M12 13v8m-3-3 3 3 3-3':/文件|备份目标/.test(title)?'M5 3h10l4 4v14H5zM14 3v5h5M8 12h8M8 16h8':/计划/.test(title)?'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18m0 4v6l4 2':'M4 6h16M4 12h16M4 18h16M8 3v6M16 9v6M10 15v6');svg.append(path);box.append(svg);return box;
}
function setupGroups(){
    const defaults={vault:'基本信息',mail:'SMTP 连接',backup:'自动备份计划',remote:'已配置的存储',archives:'本机备份文件',settings:'通过自己的域名访问面板',cleanup:'卸载范围与数据'};
    for(const view of document.querySelectorAll('[data-view]')){
        if(!(view.dataset.view in defaults))continue;
        for(const card of view.querySelectorAll('article.card')){
            const heading=card.querySelector(':scope > .card-heading');
            const titleNode=heading?.querySelector('h2')||card.querySelector('h2');
            const title=titleNode?.textContent||(card.querySelector('#restore-form')?'从备份恢复':card.querySelector('#remove-form')?'卸载范围与数据':'');
            if(!title)continue;
            const group=document.createElement('details');group.className=card.className+' config-group';
            group.dataset.groupTitle=title;group.open=defaults[view.dataset.view]===title;
            if(card.id)group.id=card.id;if(card.hidden)group.hidden=true;
            const summary=document.createElement('summary');summary.className='group-summary';
            if(heading){const oldIcon=heading.querySelector('.number');if(oldIcon&&!/^\d+$/.test(oldIcon.textContent.trim()))oldIcon.replaceWith(groupIcon(title));else if(!oldIcon)heading.prepend(groupIcon(title));summary.append(heading)}else{
                const label=document.createElement('span');label.className='group-label';
                const h2=document.createElement('h2');h2.textContent=title;label.append(groupIcon(title),h2);summary.append(label);titleNode?.remove();
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
        if(title==='基本信息')status=value('DOMAIN')||'待配置';
        else if(title==='注册与权限')status='注册'+(checked('SIGNUPS_ALLOWED')?'开放':'关闭')+' · 邀请'+(checked('INVITATIONS_ALLOWED')?'开放':'关闭');
        else if(title==='管理后台口令')status='';
        else if(title==='运行与安全设置')status='会话 '+value('ADMIN_SESSION_LIFETIME')+' 分钟';
        else if(title==='官方 Push 推送')status=checked('enabled')?'已开启':'未开启';
        else if(title==='SMTP 连接')status=value('SMTP_HOST')||'未配置';
        else if(title==='自动备份计划')status=value('CRON')+' · 保留 '+value('BACKUP_KEEP_DAYS')+' 天';
        else if(title==='备份邮件通知')status=checked('MAIL_SMTP_ENABLE')?'已开启':'未开启';
        else if(title==='远程备份文件')status='选择存储浏览';
        else if(title==='已配置的存储')status=(cfg?.extras?.remotes.length||0)+' 个存储';
        else if(title==='新增或编辑存储')status='添加或重新配置';
        else if(title==='更多存储类型（Rclone 向导）')status='OAuth 与长尾类型';
        else if(title==='备份目标与保存目录')status=(cfg?.extras?.targets.length||0)+' 个目标';
        else if(title==='本机备份文件')status=(cfg?.files.length||0)+' 份文件';
        else if(title==='通过自己的域名访问面板')status=settingsSnapshot?.origins.length?settingsSnapshot.origins.length+' 个允许域名':'按需添加';
        else if(title==='从备份恢复')status='校验后恢复';
        else if(title==='卸载范围与数据')status=value('delete_data')==='delete'?'将删除数据':'保留数据';
        group.querySelector('.group-status').textContent=status;group.querySelector('.group-status').hidden=!status;
        const form=group.closest('form')||group.querySelector('form');
        if(form&&!dirty.has(form.id)&&form.id!=='token-form'&&form.id!=='restore-form')group.dataset.changed='false';
        group.querySelector('.group-changed').hidden=group.dataset.changed!=='true';
    }
}
setupGroups();
mountWorkspaceDialogs();
$('preset-card').addEventListener('toggle',()=>{if(!$('preset-card').open){$('preset-card').hidden=true;$('storage-dialog').close()}});
document.addEventListener('input',event=>{const group=event.target.closest('.config-group');if(group&&!event.target.matches('.env-search,.env-select')){group.dataset.changed='true';updateGroupSummaries()}});
document.addEventListener('change',()=>updateGroupSummaries());
document.addEventListener('submit',event=>{lastSubmittedForm=event.target;updateGroupSummaries()},true);
document.addEventListener('invalid',event=>revealGroup(event.target),true);
document.addEventListener('reset',event=>{const group=event.target.closest('.config-group');if(group)group.dataset.changed='false';setTimeout(updateGroupSummaries,0)},true);


// Render guidance from saved configuration, application records and observed containers.
let environment=null, settingsSnapshot=null, presentationJob=null;
const scopeForms={core:['core-form','permissions-form','mail-form','advanced-form','push-form','core-env-form'],backup:['backup-form','notifications-form','backup-env-form']};
function hasUnsaved(scope){return scopeForms[scope].some(id=>dirty.has(id))}
function viewElement(view,selector){return document.querySelector('[data-view="'+view+'"] '+selector)}
function setupPresentation(){
    viewElement('overview','.setup-card').hidden=true;
    $('latest-backup').textContent='读取中';$('latest-detail').textContent='正在读取本机归档记录';
    for(const view of ['vault','mail']){const bar=viewElement(view,'.apply-bar');bar.dataset.applyScope='core';bar.hidden=true;if(view==='vault')bar.dataset.actionButton='core-service-start'}
    const backupBar=document.createElement('div');backupBar.className='apply-bar';backupBar.dataset.applyScope='backup';backupBar.hidden=true;
    const copy=document.createElement('div');copy.append(document.createElement('strong'),document.createElement('p'));
    backupBar.dataset.actionButton='backup-service-start';backupBar.append(copy);
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
    const button=bar.dataset.actionButton?$(bar.dataset.actionButton):bar.querySelector('button');button.textContent=label;
    if(scope==='backup'&&(!cfg.configured||!cfg.admin_set)){bar.querySelector('strong').textContent='请先完成主服务配置';bar.querySelector('p').textContent='备份依赖密码库。请先保存主服务配置并设置管理口令。';button.textContent='配置主服务';button.disabled=taskRunning;button.onclick=()=>{location.hash='vault'};return}
    if(scope==='backup'&&!active&&!environment?.unknown&&environment?.available&&!environment.containers.some(row=>row.Names==='vaultwarden'&&row.State==='running'))bar.querySelector('p').textContent='启用备份时将先启动密码库主服务，再启动备份服务。';
    button.disabled=taskRunning||unsaved||(!needsToken&&(!environment?.available||environment?.unknown));
    button.title=button.disabled?(unsaved?'请先保存修改':taskRunning?'已有任务正在执行':'请先确认 Docker 和服务状态'):'';
    button.onclick=needsToken?()=>{revealGroup($('token-form'));$('token-form').elements.password.focus()}:()=>startTask(action);
}
function renderPresentation(){
    const pushEnabled=$('push-form').elements.enabled.checked;
    $('push-credentials').hidden=!pushEnabled;
    $('push-form').elements.enabled.setAttribute('aria-expanded',String(pushEnabled));
    const zipEnabled=$('backup-form').elements.ZIP_ENABLE.checked;
    $('backup-zip-fields').hidden=!zipEnabled;$('backup-password-field').hidden=!zipEnabled;
    $('backup-form').elements.ZIP_ENABLE.setAttribute('aria-expanded',String(zipEnabled));
    if(!cfg||!environment)return;
    $('permissions-form').querySelector('button[type=submit]').disabled=taskRunning||!cfg.configured||panelUpdating;
    $('permissions-form').querySelector('.form-footer>span').textContent=cfg.configured?'独立保存权限设置，应用主服务配置后生效。':'请先保存基本信息，再保存注册与权限。';
    for(const scope of ['core','backup'])$(scope+'-env-form').querySelector('button[type=submit]').disabled=taskRunning||panelUpdating||!(scope==='core'?cfg.configured:cfg.backup_configured);
    renderTokenForm();
    renderExternalProxyGuide();
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
    for(const button of document.querySelectorAll('[data-task="backup-start"]'))if(!button.closest('[data-apply-scope]')&&(button.id!=='backup-service-start'||document.querySelector('[data-apply-scope=backup]').hidden))button.textContent=backupPending?'应用配置':backupActive?'应用配置':'启用服务';
    const backupUnknown=!environment.available||environment.unknown;
    $('core-service-status').textContent=backupUnknown?'状态未知':active?'运行中':core?'已停止':cfg.configured?'待启用':'未配置';
    $('core-service-status').classList.toggle('is-running',!!active&&!backupUnknown);
    $('core-service-description').textContent=backupUnknown?'暂时无法确认服务状态，请刷新后重试。':active?'配置修改后保存，再应用到服务。':core?'服务已停止，配置与数据保留。':cfg.configured?'完成配置后即可启用服务。':'先填写并保存下方配置，再启用服务。';
    const coreStart=$('core-service-start');
    if(viewElement('vault','[data-apply-scope]').hidden){coreStart.textContent=active?'应用配置':'启用服务';coreStart.disabled=taskRunning||backupUnknown||!ready||hasUnsaved('core');coreStart.onclick=()=>startTask('deploy')}
    $('core-service').querySelector('[data-task="upgrade"]').disabled=taskRunning||backupUnknown||!core||!ready;

    const prerequisite=!cfg.configured||!cfg.admin_set;
    $('backup-prerequisite').hidden=!prerequisite;
    $('backup-prerequisite').querySelector('span').textContent=!cfg.configured?'请先保存主服务配置，再设置备份计划。':'主服务尚未设置管理口令，完成后才能启用备份。';
    $('backup-form').querySelector('button[type=submit]').disabled=taskRunning||!cfg.configured;
    $('new-backup-target').disabled=taskRunning||panelUpdating||!cfg.backup_configured;
    document.querySelectorAll('[data-target-control]').forEach(button=>button.disabled=taskRunning||panelUpdating);
    const presetForm=$('preset-form');if(presetForm){const presetSubmit=presetForm.querySelector('button[type=submit]');if(presetSubmit)presetSubmit.disabled=taskRunning||panelUpdating;const presetTip=presetForm.elements.set_target.closest('.toggle').querySelector('small');if(presetTip)presetTip.textContent=!cfg?.backup_configured?'备份计划尚未保存；保存配置后可在备份设置中添加目标。':'加入 RCLONE_REMOTE_NAME 目标；不会删除其他目标。'}

    $('backup-service-status').textContent=backupUnknown?'状态未知':backupActive?'运行中':backup?'已暂停':cfg.backup_configured?'待启用':'未配置';
    $('backup-service-status').classList.toggle('is-running',!!backupActive&&!backupUnknown);
    $('backup-service-description').textContent=backupUnknown?'暂时无法确认服务状态，请刷新后重试。':backupActive?'按已保存的计划自动备份，也可以立即创建一份。':backup?'自动备份已暂停，已有配置和备份文件保留。':cfg.backup_configured?'备份计划已保存，启用后开始自动执行。':'先填写并保存下方备份计划，再启用服务。';
    const backupStart=$('backup-service-start');
    backupStart.hidden=false;
    if(document.querySelector('[data-apply-scope=backup]').hidden){backupStart.disabled=taskRunning||backupUnknown||prerequisite||!cfg.backup_configured||hasUnsaved('backup');backupStart.onclick=()=>startTask('backup-start')}
    backupStart.className=backupActive?'secondary':'primary';


    $('backup-service').querySelector('[data-task="upgrade-backup"]').disabled=taskRunning||backupUnknown||!backup;

    for(const button of document.querySelectorAll('[data-task="backup-stop"]')){button.disabled=taskRunning||!backupActive;button.title=backupActive?'暂停计划任务，保留备份文件':'备份服务当前未运行'}
    for(const button of document.querySelectorAll('[data-task="backup-now"]')){button.disabled=taskRunning||!backupActive;button.title=backupActive?'执行一次备份':'请先启动备份服务';const caption=button.querySelector('small');if(caption)caption.textContent=backupActive?'立即生成一份加密备份':'启动备份服务后可手动执行'}
    for(const button of document.querySelectorAll('[data-task="stop"]'))button.disabled=taskRunning||!active;

    document.querySelectorAll('[data-uninstall]').forEach(button=>button.disabled=taskRunning||panelUpdating||(button.dataset.uninstall==='core'?!cfg.configured:!cfg.backup_configured));
    if(settingsSnapshot){const count=settingsSnapshot.origins.length;const heading=$('domain-form').closest('.config-group')?.querySelector('.card-heading');if(heading){heading.querySelector('h2').textContent=count?'允许访问的面板域名':'通过自己的域名访问面板';heading.querySelector('p').textContent=count?'已允许 '+count+' 个域名。DNS 和 HTTPS 反向代理需独立配置与确认。':'如需使用域名，先添加访问地址，再配置 DNS 和 HTTPS 反向代理。'}}
    for(const button of document.querySelectorAll('.action-card[data-task="deploy"]')){
        button.querySelector('strong').textContent=active?'应用密码库配置':core?'启动密码库':'部署密码库';
        button.querySelector('small').textContent=corePending?'有已保存的修改等待加载':active?'服务运行中，可按需重新应用':core?'使用现有配置重新启动':cfg.configured?'使用已保存的配置部署':'先完成密码库配置';
    }
    if(cfg.backup_configured&&!(cfg.extras?.targets||[]).some(row=>row.enabled!==false)){$('backup-service-description').textContent='所有备份目标已停用，请先在“备份目标”页面启用一个。';for(const button of document.querySelectorAll('[data-task="backup-start"],[data-task="backup-now"]')){button.disabled=true;button.title='请先在备份目标页面启用一个目标'}}
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
function upgradeMarker(){try{const value=JSON.parse(sessionStorage.getItem('vwp-upgrade')||'null');if(value&&/^[a-f0-9]{64}$/.test(value.build)&&Date.now()-value.started<900000)return value;sessionStorage.removeItem('vwp-upgrade')}catch{}return null}
function rememberUpgrade(build,version){try{sessionStorage.setItem('vwp-upgrade',JSON.stringify({build,version,started:Date.now()}))}catch{}}
function forgetUpgrade(){try{sessionStorage.removeItem('vwp-upgrade')}catch{}}

function paintPanelUpdate(info){
    clearTimeout(updateWatch);panelUpdateInfo=info;const installed=info.installed||{};const latest=info.latest;
    const marker=upgradeMarker();
    if(marker&&info.progress?.state==='failed'){forgetUpgrade();notice(info.progress.error||'面板升级失败，请查看详情。',true,{detail:true,onDetail:()=>setVersionPopover(true)})}
    else if(marker&&installed.build_id===marker.build){forgetUpgrade();notice('面板已升级到 v'+(installed.version||marker.version||'最新版本'))}
    else if(marker)upgradeTargetBuild=marker.build;

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
        const info=await response.json();if(epoch!==authEpoch)throw sessionChanged();if(!response.ok||!info.installed||typeof info.available!=='boolean')throw Error(info.error||'暂时无法检查更新');if(loadedPanelBuild&&/^[a-f0-9]{64}$/.test(info.installed.build_id||'')&&info.installed.build_id!==loadedPanelBuild&&info.progress?.state!=='failed'){if(!panelReloadPending){panelReloadPending=true;clearTimeout(updateWatch);rememberUpgrade(info.installed.build_id,info.installed.version);location.reload()}return}paintPanelUpdate(info);
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
    const hasInput=dirty.size||Array.from(document.querySelectorAll('#workspace input[type=password],dialog.workspace-dialog input[type=password]')).some(input=>!input.readOnly&&input.value);
    const message=(hasInput?'页面还有未保存输入，重启后将丢失。\n':'')+'确认升级管理面板？升级会重启面板，完成后将自动重新连接；密码库与备份容器保持运行。';
    if(!await confirmDialog(message,{title:'更新管理面板',accept:'立即更新'}))return;
    if(panelUpdating||taskRunning||consoleRunning||authPhase!=='authenticated')return;
    $('upgrade-panel').disabled=true;
    try{const targetBuild=panelUpdateInfo.latest.build_id;const progress=await api('updates/start',{build_id:targetBuild,confirmed:true});upgradeTargetBuild=targetBuild;rememberUpgrade(targetBuild,panelUpdateInfo.latest.version);notice('面板正在升级，完成后将自动刷新…',false,{kind:'loading'});paintPanelUpdate({...panelUpdateInfo,progress});}
    catch(error){if(isSessionTransition(error))return;notice(error.message,true);$('upgrade-panel').disabled=false}
};
setInterval(()=>{if(csrf&&!panelUpdating)checkPanelUpdates(false)},60000);
restoreSession();
