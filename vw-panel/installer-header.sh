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
    exit 0
fi
[[ $# -eq 0 ]] || { echo '未知参数，请使用 --help。'; exit 1; }
[[ "$(uname -s)" == Linux ]] || { echo '请把安装文件上传到 Linux 服务器执行。'; exit 1; }
[[ "$EUID" -eq 0 ]] || { echo '请执行 sudo bash install-vw-web.sh'; exit 1; }
(( BASH_VERSINFO[0] >= 4 )) || { echo '需要 Bash 4 或更高版本。'; exit 1; }
command -v systemctl >/dev/null && [[ -d /run/systemd/system ]] || { echo '需要使用 systemd 的 Linux 服务器（例如 Debian 12）。'; exit 1; }
[[ -r /dev/tty && -w /dev/tty ]] || { echo '请在交互式 SSH 会话中执行。'; exit 1; }
exec 3<>/dev/tty
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
if [[ -f /var/lib/vw-panel/panel.json ]]; then
    panel_port="$(python3 -c 'import json;print(json.load(open("/var/lib/vw-panel/panel.json"))["port"])')"
    echo "检测到已有面板，保留密码、端口 $panel_port 和域名配置，更新程序文件。"
else
    while true; do
        read -r -p '面板访问端口 [默认 8787]: ' panel_port <&3
        panel_port="${panel_port:-8787}"
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
trap 'rm -f "$archive"; if [[ -n "${stage:-}" && -d "$stage" ]]; then rm -rf -- "$stage"; fi' EXIT
extract_payload "$stage" "$archive"
chmod -R go-w "$stage"
if [[ ! -f /var/lib/vw-panel/credentials.json ]]; then
    # getpass reads /dev/tty; works when installer is launched with process substitution.
    until python3 "$stage/web/server.py" configure --state-dir /var/lib/vw-panel --port "$panel_port" --host 0.0.0.0; do
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
cat > /usr/local/bin/vw-panel <<'CLI'
#!/usr/bin/env bash
set -euo pipefail
case "${1:-info}" in
    password)
        [[ "$EUID" -eq 0 ]] || { echo '请使用 sudo vw-panel password'; exit 1; }
        python3 /opt/vw-panel/current/web/server.py reset-password --state-dir /var/lib/vw-panel
        systemctl restart vw-panel
        ;;
    domain)
        [[ "$EUID" -eq 0 ]] || { echo '请使用 sudo vw-panel domain https://面板域名'; exit 1; }
        [[ -n "${2:-}" ]] || { echo '用法：sudo vw-panel domain https://vw-panel.example.com'; exit 1; }
        python3 /opt/vw-panel/current/web/server.py domain --state-dir /var/lib/vw-panel --origin "$2"
        systemctl restart vw-panel
        ;;
    port)
        [[ "$EUID" -eq 0 ]] || { echo '请使用 sudo vw-panel port 新端口'; exit 1; }
        python3 - "${2:-}" <<'PY'
import json,socket,sys,os
p='/var/lib/vw-panel/panel.json'
c=json.load(open(p))
try:
    port=int(sys.argv[1]); assert 1024 <= port <= 65535
    if port != c['port']:
        with socket.socket() as s:s.bind((c['host'],port))
except (ValueError,AssertionError,OSError):
    raise SystemExit('端口必须在 1024～65535 之间且未被占用。')
c['port']=port
fd=os.open(p+'.new',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as f:json.dump(c,f)
os.replace(p+'.new',p)
print('端口已更新为',port)
PY
        systemctl restart vw-panel
        ;;
    restart|stop|start|status) systemctl "$1" vw-panel ;;
    logs) journalctl -u vw-panel -f ;;
    info)
        echo 'Vaultwarden 网页管理面板'
        if [[ "$EUID" -eq 0 ]]; then
            python3 - <<'PY'
import json
c=json.load(open('/var/lib/vw-panel/panel.json'))
print('IP 访问：http://服务器IP:'+str(c['port']))
for origin in c.get('origins',[]):print('域名访问：'+origin)
PY
        fi
        echo '命令：sudo vw-panel status | logs | password | port 8787 | domain https://面板域名'
        ;;
    *) echo '命令：info、status、logs、password、port、domain、restart、stop、start'; exit 1 ;;
esac
CLI
chmod 755 /usr/local/bin/vw-panel
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
local_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo '================ 安装完成 ================'
echo "网页地址：http://${local_ip:-服务器IP}:$panel_port"
echo "公网访问：http://你的服务器公网IP:$panel_port"
echo '使用刚才设置的管理员密码登录。'
echo "若无法访问，请在云安全组及服务器防火墙放行 TCP $panel_port。"
echo '后续绑定域名：网页 → 面板设置，或 sudo vw-panel domain https://你的面板域名'
echo '面板已设置开机启动；原有密码库和原脚本没有被改动。'
exit 0
