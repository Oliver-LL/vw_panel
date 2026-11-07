#!/usr/bin/env bash
set -euo pipefail
panel_root=/opt/vw-panel
panel_state=/var/lib/vw-panel
need_root() { [[ "$EUID" -eq 0 ]] || { echo '请使用 sudo vwp。'; exit 1; }; }
menu() {
    while true; do
        echo; echo '========== Vaultwarden 管理菜单 =========='
        echo ' 1) 面板地址与信息'; echo ' 2) 查看面板状态'; echo ' 3) 启动面板'; echo ' 4) 停止面板'
        echo ' 5) 重启面板'; echo ' 6) 查看面板日志'; echo ' 7) 重设面板密码（回车随机）'
        echo ' 8) 修改面板端口（回车随机）'; echo ' 9) 设置面板域名'
        echo '10) 进入 Bash 部署菜单（共享网页配置，推荐）'; echo '11) 进入原始 Bash 快照（未修改）'
        echo '12) 卸载网页面板（保留密码库与备份）'; echo '13) 检查面板更新'; echo '14) 升级网页面板'; echo ' 0) 退出'
        read -r -p '请选择 [0-14]: ' option || return
        case "$option" in
            1) "$0" info;; 2) "$0" status || true;; 3) "$0" start;; 4) "$0" stop;; 5) "$0" restart;;
            6) "$0" logs;; 7) "$0" password;; 8) read -r -p '新端口 [回车随机]: ' value; "$0" port "$value";;
            9) read -r -p '面板 HTTPS 地址: ' value; "$0" domain "$value";;
            10) "$0" bash;; 11) "$0" original;; 12) "$0" uninstall; return;; 13) "$0" check-update || true;; 14) "$0" update || true;; 0|q|Q) return;; *) echo '请选择正确的编号。';;
        esac
    done
}
case "${1:-menu}" in
    original|bash|password|port|domain|uninstall|restart|stop|start)
        need_root
        if [[ -f "$panel_root/current/web/updater.py" ]]; then
            python3 "$panel_root/current/web/updater.py" busy --state-dir "$panel_state"
        fi;;
esac
case "${1:-menu}" in
    menu) need_root; menu;;
    original)
        need_root
        export HOME=/root
        echo '原始快照未修改：能识别部署目录和容器，但网页配置的引号格式可能导致部分状态显示或操作不准确。'
        echo '推荐使用 vwp bash 进入共享网页配置的兼容菜单。'
        exec 9>"$panel_state/operation.lock"; flock -n 9 || { echo '有网页任务正在运行。'; exit 1; }
        read -r -p '输入 y 进入原始脚本 [默认取消]: ' confirm
        [[ "$confirm" == y || "$confirm" == Y ]] || exit 0
        exec bash "$panel_root/current/vw_original.sh";;
    bash)
        need_root
        export HOME=/root
        export VW_WEB_MODE=1 VW_PANEL_LOCK="$panel_state/operation.lock"
        exec bash "$panel_root/current/vw_web.sh";;
    password)
        need_root
        exec 9>"$panel_state/operation.lock"; flock -n 9 || { echo '有任务正在执行，请结束后再修改。'; exit 1; }
        python3 "$panel_root/current/web/server.py" reset-password --state-dir "$panel_state"
        systemctl restart vw-panel;;
    port)
        need_root
        exec 9>"$panel_state/operation.lock"; flock -n 9 || { echo '有任务正在执行，请结束后再修改。'; exit 1; }
        python3 - "${2:-}" "$panel_state/panel.json" <<'PY'
import json,socket,sys,os,secrets
p=sys.argv[2]; c=json.load(open(p))
try:
    if sys.argv[1]:
        port=int(sys.argv[1]); assert 1024 <= port <= 65535
        if port != c['port']:
            with socket.socket() as s:s.bind((c['host'],port))
    else:
        with socket.socket() as s:
            s.bind((c['host'],0));port=s.getsockname()[1]
except (ValueError,AssertionError,OSError):
    raise SystemExit('端口需在 1024～65535 之间且未被占用。')
c['port']=port
path=p+'.'+secrets.token_hex(8)
fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as f:json.dump(c,f)
os.replace(path,p)
print('面板端口：',port,'；请同步防火墙与反向代理。')
PY
        systemctl restart vw-panel;;
    domain)
        need_root; [[ -n "${2:-}" ]] || { echo '用法：sudo vwp domain https://面板域名'; exit 1; }
        exec 9>"$panel_state/operation.lock"; flock -n 9 || { echo '有任务正在执行，请结束后再修改。'; exit 1; }
        python3 "$panel_root/current/web/server.py" domain --state-dir "$panel_state" --origin "$2"
        systemctl restart vw-panel;;
    uninstall)
        need_root
        echo '仅卸载网页面板：服务、程序与终端菜单。密码库、Caddy、备份和 Docker 均保留。'
        read -r -p '输入 UNINSTALL 确认卸载 [默认取消]: ' confirm
        [[ "$confirm" == UNINSTALL ]] || { echo '已取消。'; exit 0; }
        read -r -p '是否同时删除面板密码和域名设置？[y/N]: ' purge
        exec 8>"$panel_state/install.lock"; flock -n 8 || { echo '安装器正在运行，请稍后操作。'; exit 1; }
        exec 9>"$panel_state/operation.lock"; flock -n 9 || { echo '网页任务正在运行，请结束后再卸载。'; exit 1; }
        systemctl disable --now vw-panel
        rm -f /etc/systemd/system/vw-panel.service
        systemctl daemon-reload
        rm -rf -- /opt/vw-panel
        rm -f /usr/local/bin/vwp
        if [[ "$purge" == y || "$purge" == Y ]]; then rm -rf -- /var/lib/vw-panel; fi
        echo '网页面板已卸载。密码库和备份保留。';;
    check-update|update) need_root; python3 "$panel_root/current/web/updater.py" "${1/check-update/check}" --state-dir "$panel_state";;
    restart|stop|start|status) need_root; systemctl "$1" vw-panel;;
    logs) need_root; journalctl -u vw-panel -f;;
    info)
        need_root
        python3 - "$panel_state/panel.json" <<'PY'
import json,sys,socket
c=json.load(open(sys.argv[1]))
print('面板端口：',c['port'])
print('访问地址：http://服务器公网IP:'+str(c['port']))
for origin in c.get('origins',[]):print('域名访问：'+origin)
print('密码仅在安装或重设时显示，忘记后可执行 sudo vwp password。')
PY
        echo '管理菜单：sudo vwp | 原始脚本：sudo vwp original | 卸载面板：sudo vwp uninstall';;
    *) echo '命令：menu、info、status、start、stop、restart、logs、password、port、domain、original、bash、check-update、update、uninstall'; exit 1;;
esac
