#!/usr/bin/env bash
set -euo pipefail
# Self-contained installer. No GitHub checkout, npm, pip or Docker required for panel startup.
payload_sha='@PAYLOAD_SHA256@'
extract_payload() {
    local target="$1" archive="$2"
    awk 'found { print } /^__VW_PANEL_PAYLOAD__$/ { found=1 }' "$0" | base64 -d > "$archive"
    printf '%s  %s\n' "$payload_sha" "$archive" | sha256sum -c - >/dev/null
    tar -xzf "$archive" -C "$target"
}
if [[ "${1:-}" == --extract ]]; then
    [[ $# -eq 2 ]] || { echo '用法：bash install-vw-web.sh --extract 目标目录'; exit 1; }
    mkdir -p "$2"
    archive="$(mktemp)"
    trap 'rm -f "$archive"' EXIT
    extract_payload "$2" "$archive"
    echo "文件已解包到：$2（未安装或启动服务）"
    exit 0
fi
if [[ "${1:-}" == --help ]]; then
    echo '用法：sudo bash install-vw-web.sh'
    echo '      bash install-vw-web.sh --extract 目标目录'
    echo '选项：--install 安装/升级；--original 原始脚本；--menu 管理菜单；--uninstall 卸载面板'
    exit 0
fi
[[ $# -eq 0 || ( $# -eq 1 && ( "$1" == --upgrade || "$1" == --install || "$1" == --original || "$1" == --menu || "$1" == --uninstall ) ) ]] || { echo '未知参数，请使用 --help。'; exit 1; }
[[ "$(uname -s)" == Linux ]] || { echo '请把安装文件上传到 Linux 服务器执行。'; exit 1; }
[[ "$EUID" -eq 0 ]] || { echo '请执行 sudo bash install-vw-web.sh'; exit 1; }
(( BASH_VERSINFO[0] >= 4 )) || { echo '需要 Bash 4 或更高版本。'; exit 1; }
command -v systemctl >/dev/null && [[ -d /run/systemd/system ]] || { echo '需要使用 systemd 的 Linux 服务器（例如 Debian 12）。'; exit 1; }
if [[ "${1:-}" == --upgrade ]]; then
    [[ -f /var/lib/vw-panel/panel.json && -f /var/lib/vw-panel/credentials.json ]] || { echo '后台升级只适用于已经安装的面板。'; exit 1; }
    exec 3</dev/null
else
    [[ -r /dev/tty && -w /dev/tty ]] || { echo '请在交互式 SSH 会话中执行。'; exit 1; }
    exec 3<>/dev/tty
fi
choice=1
case "${1:-}" in
    --original) choice=2;; --menu) choice=3;; --uninstall) choice=4;;
    --install|--upgrade) choice=1;;
    '')
        echo '========== Vaultwarden 安装与管理 =========='
        echo ' 1) 安装 / 升级网页面板'
        echo ' 2) 进入原始 Bash 部署脚本'
        echo ' 3) 已安装面板的管理菜单'
        echo ' 4) 卸载网页面板（保留密码库和备份）'
        echo ' 0) 退出'
        read -r -p '请选择 [默认 1]: ' choice <&3
        choice="${choice:-1}";;
esac
case "$choice" in
    0|q|Q) exit 0;;
    2)
        original_tmp="$(mktemp -d)"; original_archive="$(mktemp)"
        trap 'rm -rf -- "$original_tmp"; rm -f -- "$original_archive"' EXIT
        extract_payload "$original_tmp" "$original_archive"
        echo '原始脚本不受网页任务锁保护，请勿与网页同时部署或恢复。'
        bash "$original_tmp/vw_original.sh"; exit $?;;
    3|4)
        [[ -x /usr/local/bin/vwp ]] || { echo '尚未安装网页面板。'; exit 1; }
        if [[ "$choice" == 4 ]]; then exec /usr/local/bin/vwp uninstall; fi
        exec /usr/local/bin/vwp menu;;
    1) ;; *) echo '无效的菜单选项。'; exit 1;;
esac
if ! command -v python3 >/dev/null; then
    echo '正在安装 Python 3…'
    if command -v apt-get >/dev/null; then apt-get update; apt-get install -y python3;
    elif command -v dnf >/dev/null; then dnf install -y python3;
    elif command -v yum >/dev/null; then yum install -y python3;
    else echo '请先安装 Python 3.9+ 后重新执行。'; exit 1; fi
fi
python3 -c 'import sys,hashlib,fcntl,pty; assert sys.version_info >= (3,9), "需要 Python 3.9+"; assert hasattr(hashlib,"scrypt"), "Python 需支持 scrypt"'
command -v flock >/dev/null || { echo '缺少 flock，请先安装 util-linux。'; exit 1; }
mkdir -p /opt/vw-panel/releases /var/lib/vw-panel
chmod 700 /var/lib/vw-panel
exec 8>/var/lib/vw-panel/install.lock
flock -n 8 || { echo '另一个安装器正在运行。'; exit 1; }
exec 7>/var/lib/vw-panel/operation.lock
flock -n 7 || { echo '有网页任务正在执行，请完成后再升级。'; exit 1; }
if [[ -f /var/lib/vw-panel/panel.json ]]; then
    panel_port="$(python3 -c 'import json;print(json.load(open("/var/lib/vw-panel/panel.json"))["port"])')"
    echo "检测到已有面板，保留密码、端口 $panel_port 和域名配置，更新程序文件。"
else
    while true; do
        read -r -p '面板访问端口 [回车随机选择空闲端口]: ' panel_port <&3
        if [[ -z "$panel_port" ]]; then
            panel_port="$(python3 -c 'import socket;s=socket.socket();s.bind(("0.0.0.0",0));print(s.getsockname()[1]);s.close()')"
        fi
        if python3 - "$panel_port" <<'PY'
import socket,sys
try:
    port=int(sys.argv[1]); assert 1024 <= port <= 65535
    with socket.socket() as s: s.bind(('0.0.0.0',port))
except (ValueError,AssertionError,OSError):
    raise SystemExit('请输入 1024～65535 之间未被占用的端口。')
PY
        then break; fi
    done
fi
stage="$(mktemp -d /opt/vw-panel/releases/.stage.XXXXXXXX)"
archive="$(mktemp)"
bootstrap_secret="$(mktemp /var/lib/vw-panel/.bootstrap.XXXXXXXX)"
chmod 600 "$bootstrap_secret"
trap 'rm -f "$archive" "$bootstrap_secret"; if [[ -n "${stage:-}" && -d "$stage" ]]; then rm -rf -- "$stage"; fi' EXIT
extract_payload "$stage" "$archive"
chmod -R go-w "$stage"
if [[ ! -f /var/lib/vw-panel/credentials.json ]]; then
    # getpass reads /dev/tty; works when installer is launched with process substitution.
    until python3 "$stage/web/server.py" configure --state-dir /var/lib/vw-panel --port "$panel_port" --host 0.0.0.0 --bootstrap-secret-file "$bootstrap_secret"; do
        echo '请重新设置管理员密码。'
    done
fi
release="/opt/vw-panel/releases/$(date -u +%Y%m%dT%H%M%SZ)-$$"
mv "$stage" "$release"
stage=''
previous="$(readlink /opt/vw-panel/current 2>/dev/null || true)"
ln -s "$release" /opt/vw-panel/.current-new
mv -Tf /opt/vw-panel/.current-new /opt/vw-panel/current
python_path="$(command -v python3)"
cat > /etc/systemd/system/vw-panel.service <<EOF
[Unit]
Description=Vaultwarden Web Manager
After=network.target

[Service]
Type=simple
User=root
Environment=HOME=/root
WorkingDirectory=/opt/vw-panel/current
ExecStart=$python_path /opt/vw-panel/current/web/server.py serve --state-dir /var/lib/vw-panel
Restart=on-failure
RestartSec=3
UMask=0077
KillMode=control-group
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable vw-panel >/dev/null
systemctl restart vw-panel
healthy=false
for attempt in {1..15}; do
    if python3 - "$panel_port" <<'PY' 2>/dev/null
import sys,urllib.request
with urllib.request.urlopen('http://127.0.0.1:'+sys.argv[1]+'/',timeout=1) as r:
    assert r.status==200 and b'Vaultwarden' in r.read()
PY
    then healthy=true; break; fi
    sleep 1
done
if [[ "$healthy" != true ]]; then
    echo '面板启动检查失败，请执行 journalctl -u vw-panel -n 80 查看原因。'
    if [[ -n "$previous" && -d "$previous" ]]; then
        ln -s "$previous" /opt/vw-panel/.current-rollback
        mv -Tf /opt/vw-panel/.current-rollback /opt/vw-panel/current
        systemctl restart vw-panel || true
        echo '已切回上一版本程序文件。'
    fi
    exit 1
fi
install -m 755 "$release/panel-cli.sh" /usr/local/bin/vwp
# Remove only the earlier command installed by this project; never touch `vw`.
if [[ -f /usr/local/bin/vw-panel ]] && grep -Eq 'panel_root=/opt/vw-panel|/opt/vw-panel/current/web/server.py' /usr/local/bin/vw-panel; then
    rm -f /usr/local/bin/vw-panel
fi
local_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo '================ 安装完成 ================'
echo "网页地址：http://${local_ip:-服务器IP}:$panel_port"
echo "公网访问：http://你的服务器公网IP:$panel_port"
if [[ -s "$bootstrap_secret" ]]; then
    printf '管理员密码：'; cat "$bootstrap_secret"; echo
    echo '请保存此密码，安装完成后不会在服务器保存密码原文。'
else
    echo '升级保留原面板密码；忘记后执行 sudo vwp password。'
fi
rm -f "$bootstrap_secret"
echo "面板端口：$panel_port"
echo '终端管理菜单：sudo vwp'
echo '原始 Bash 脚本：sudo vwp original'
echo '卸载网页面板：sudo vwp uninstall（保留密码库与备份）'
echo "若无法访问，请在云安全组及服务器防火墙放行 TCP $panel_port。"
echo '后续绑定域名：网页 → 面板设置，或 sudo vwp domain https://你的面板域名'
echo '面板已设置开机启动；原有密码库和原脚本没有被改动。'
exit 0
