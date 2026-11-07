#!/bin/bash

# ==========================================
# Vaultwarden 一键部署脚本
# 版本: v1.1.3
# 适用场景:
# - 首次部署 Vaultwarden 主服务
# - 初始化备份服务与远程存储
# - 查看运行状态、日志、备份诊断
# - 修改配置、恢复数据、彻底卸载
#
# 功能亮点:
# - 自动识别 Caddy / 外部反代 / 宿主机端口映射模式
# - 支持 Docker 网络复用与脚本创建网络的归属识别
# - 支持 ADMIN_TOKEN 离线加密
# - 支持备份服务、Rclone 远程存储与本地自动恢复
# - 支持记忆配置丢失后的逆向自愈恢复
# - 内置环境体检、运行状态看板与常见边界提示
#
# 使用提示:
# - 建议以 root 或具备 Docker 管理权限的用户运行
# - 首次使用前，请先修改下方 SCRIPT_URL 为你托管此脚本的直链地址
# - 推荐在全新服务器或确认无冲突的环境中执行
# - 如需重新部署，请优先使用脚本内的“彻底卸载并清理环境”
# ==========================================
#
# ==========================================
# 【用户配置区】请在此填入你最终托管这个脚本的直链网址
# 例如 Github Gist 的 Raw 链接，或者你自己的域名链接
# ==========================================

SCRIPT_URL="https://gist.githubusercontent.com/Oliver-LL/72ef2f0aa06ce5d4f186bbc67adebada/raw/gistfile1.txt"

# 设置颜色输出
if [[ -t 1 ]] && { [ "${TERM:-}" != "dumb" ] && [ "$(tput colors 2>/dev/null)" -ge 8 ] 2>/dev/null; }; then
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    RED='\033[0;31m'
    BLUE='\033[0;34m'
    CYAN='\033[0;36m'
    BOLD='\033[1m'
    DIM='\033[2m'
    NC='\033[0m'
else
    GREEN='' YELLOW='' RED='' BLUE='' CYAN='' BOLD='' DIM='' NC=''
fi

# Bash 版本检测
if [ -z "$BASH_VERSION" ] || [ "${BASH_VERSINFO[0]}" -lt 4 ]; then
    echo "此脚本需要 Bash 4.0 或更高版本运行。" >&2
    exit 1
fi

VW_SCRIPT_VERSION="v1.1.3"

# ==========================================
# 常量定义 (容器名、默认路径、网络名等)
# ==========================================
readonly VW_CONTAINER_NAME="vaultwarden"
readonly VW_BACKUP_CONTAINER_NAME="vaultwarden-backup"
readonly VW_DEFAULT_DIR="/opt/vaultwarden"
readonly VW_DEFAULT_CADDY_DIR="/opt/caddy"
readonly VW_DEFAULT_NET="vaultwarden_net"
readonly CADDY_DEFAULT_NET="caddy_net"

# ==========================================
# 通用辅助函数
# ==========================================

# 动态宽度分隔线 (上限 50，避免宽终端下拉满整行)
_hr() {
    local w="${COLUMNS:-42}"
    [ "$w" -gt 50 ] && w=50
    printf '%*s\n' "$w" '' | tr ' ' '='
}

# 输入空白裁剪
_trim() {
    local s="$1"
    s="${s#"${s%%[![:space:]]*}"}"
    s="${s%"${s##*[![:space:]]}"}"
    printf '%s' "$s"
}

# 长操作旋转指示器 (用法: long_cmd & _spinner $!)
_spinner() {
    local pid=$1 chars='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏' i=0
    while kill -0 "$pid" 2>/dev/null; do
        printf '\r  %s ' "${chars:i++%${#chars}:1}" >&2
        sleep 0.1
    done
    printf '\r    \r' >&2
}

# 面包屑导航 (嵌套菜单标记当前位置)
_breadcrumb() {
    echo -e "${DIM}📍 ${*}${NC}"
}

# 跨平台获取文件大小 (Linux stat -c / macOS stat -f)
_file_size() {
    local file="$1" size
    if size=$(stat -c '%s' "$file" 2>/dev/null) || size=$(stat -f '%z' "$file" 2>/dev/null); then
        if command -v numfmt >/dev/null 2>&1; then
            numfmt --to=iec --suffix=B "$size" 2>/dev/null || echo "${size}B"
        else
            echo "${size}B"
        fi
    else
        echo "未知大小"
    fi
}

# 跨平台获取文件修改时间 (Linux stat -c / macOS stat -f)
_file_mtime() {
    local file="$1" mtime
    if mtime=$(stat -c '%y' "$file" 2>/dev/null); then
        echo "$mtime" | cut -d'.' -f1
    elif mtime=$(stat -f '%Sm' -t '%Y-%m-%d %H:%M:%S' "$file" 2>/dev/null); then
        echo "$mtime"
    else
        echo "未知时间"
    fi
}

# 安全解压: 优先用 python3 (密码走环境变量，不暴露给 ps)，回退到 unzip -P
_unzip_with_password() {
    local password="$1" src="$2" dest="$3"
    if command -v python3 >/dev/null 2>&1; then
        UNZIP_PASSWORD="$password" python3 -c "
import zipfile, os, sys
with zipfile.ZipFile(sys.argv[1]) as zf:
    zf.extractall(sys.argv[2], pwd=os.environ['UNZIP_PASSWORD'].encode())
" "$src" "$dest" 2>/dev/null && return 0
    fi
    unzip -P "$password" -oq "$src" -d "$dest" >/dev/null 2>&1
}

# 全局临时文件跟踪，用于信号处理时清理
_VW_TMP_FILES=()
_VW_TMP_DIRS=()

_register_tmp_file() {
    _VW_TMP_FILES+=("$1")
}

_register_tmp_dir() {
    _VW_TMP_DIRS+=("$1")
}

_cleanup_tmp_files() {
    local f
    for f in "${_VW_TMP_FILES[@]}"; do
        [ -f "$f" ] && rm -f "$f" 2>/dev/null
    done
    _VW_TMP_FILES=()
    local d
    for d in "${_VW_TMP_DIRS[@]}"; do
        [ -d "$d" ] && rm -rf "$d" 2>/dev/null
    done
    _VW_TMP_DIRS=()
}

_signal_cleanup() {
    echo -e "\n${YELLOW}操作已中断，正在清理临时资源...${NC}"
    _cleanup_tmp_files
    exit 130
}

trap _signal_cleanup INT TERM HUP
trap _cleanup_tmp_files EXIT

# 命令行参数支持
case "${1:-}" in
    -h|--help)
        echo "Vaultwarden 一键部署管理脚本 ${VW_SCRIPT_VERSION}"
        echo ""
        echo "用法: bash vw_2.sh [选项]"
        echo ""
        echo "选项:"
        echo "  -h, --help       显示此帮助信息"
        echo "  -v, --version    显示版本号"
        echo ""
        echo "无参数时进入交互式管理面板。"
        exit 0
        ;;
    -v|--version)
        echo "Vaultwarden Manager ${VW_SCRIPT_VERSION}"
        exit 0
        ;;
esac

# ==========================================
# 🌟 体验升级 5: 宿主机前置体检与依赖安装
# ==========================================
check_dependencies() {
    local _compose_ver="" _cv_tmp _av_tmp

    if ! command -v docker &> /dev/null; then
        echo -e "${YELLOW}⚠️ 探测到系统尚未安装 Docker。${NC}"
        read -p "是否自动为您调用官方脚本安装？(y/n, 默认y): " install_docker
        if [[ "$install_docker" != [nN]* ]]; then
            echo -e "${GREEN}正在调用 Docker 官方脚本，请稍候...${NC}"
            curl -fsSL https://get.docker.com | bash
            echo -e "${GREEN}✅ Docker 环境安装完成！${NC}"
        else
            echo -e "${RED}❌ 缺乏 Docker 环境，脚本无法继续运行。退出。${NC}"
            exit 1
        fi
    fi

    # 并行执行: compose 版本 + Docker API 版本，节省一次 Docker 调用的等待时间
    _cv_tmp=$(mktemp) _av_tmp=$(mktemp)
    docker compose version --short > "$_cv_tmp" 2>/dev/null &
    docker version --format '{{.Server.APIVersion}}' > "$_av_tmp" 2>/dev/null &
    wait
    _compose_ver=$(head -1 "$_cv_tmp" | sed 's/^v//')
    SERVER_API=$(<"$_av_tmp")
    rm -f "$_cv_tmp" "$_av_tmp"

    if [ -z "$_compose_ver" ]; then
        echo -e "${RED}❌ 未检测到 Docker Compose V2，脚本无法继续运行。${NC}"
        echo -e "${YELLOW}👉 请升级 Docker 或手动安装 docker-compose-plugin。${NC}"
        exit 1
    fi

    local _major _minor
    _major="${_compose_ver%%.*}"
    _minor="${_compose_ver#*.}"; _minor="${_minor%%.*}"
    if [ "${_major:-0}" -lt 2 ] || { [ "${_major:-0}" -eq 2 ] && [ "${_minor:-0}" -lt 6 ]; }; then
        echo -e "${YELLOW}⚠️ 当前 Docker Compose 版本 v${_compose_ver} 过低，建议升级到 v2.6+ 以确保所有功能正常。${NC}"
    fi

    if [ -n "$SERVER_API" ]; then export DOCKER_API_VERSION="$SERVER_API"; fi
    return 0
}

# ==========================================
# 🌟 体验升级 6: 失忆症自愈系统 (逆向重建配置)
# ==========================================
CONF_FILE="$HOME/.vw_deploy.conf"
CONF_ALLOWED_KEYS="VW_DIR DOCKER_NET DOCKER_NET_OWNED CADDY_DIR CADDY_EXISTS EXISTING_CADDY_ID PROXY_MODE SKIP_CADDY EXT_PORT UNZIP_INSTALLED_BY_SCRIPT BACKUP_MODE BACKUP_REMOTE_TARGETS"

_upsert_file_var() {
    local file="$1"
    local key="$2"
    local val="$3"
    local tmp_file
    mkdir -p "$(dirname "$file")"
    touch "$file"
    tmp_file=$(mktemp "${file}.XXXXXX")
    _register_tmp_file "$tmp_file"
    awk -v k="$key" -v v="$val" '
        BEGIN { done=0 }
        index($0, k "=") == 1 {
            print k "=" v
            done=1
            next
        }
        { print }
        END {
            if (!done) print k "=" v
        }' "$file" > "$tmp_file" && mv "$tmp_file" "$file"
    chmod 600 "$file" 2>/dev/null || true
}

set_conf_var() {
    _upsert_file_var "$CONF_FILE" "$1" "$2"
}

find_vaultwarden_container_id() {
    docker ps -a --format '{{.ID}}\t{{.Image}}\t{{.Names}}' 2>/dev/null | awk -F '\t' '
        $2 ~ /(^|\/)vaultwarden\/server([:@]|$)/ || $2 ~ /^vaultwarden\/server([:@]|$)/ || $3 == "vaultwarden" {
            print $1
            exit
        }'
}

auto_recover_conf() {
    if [ ! -f "$CONF_FILE" ]; then
        VW_CID=$(find_vaultwarden_container_id)
        if [ -n "$VW_CID" ]; then
            local vaultwarden_networks=""
            local host_binding=""
            local host_ip=""
            local host_port=""
            local caddy_networks=""
            local matched_net=""
            echo -e "${YELLOW}🔍 记忆文件丢失，正在通过存活容器逆向重建记忆配置...${NC}"
            VW_DATA_MNT=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' "$VW_CID" || true)
            if [ -n "$VW_DATA_MNT" ]; then
                VW_DIR=$(dirname "$(dirname "$VW_DATA_MNT")")
                _update_cached_paths
                set_conf_var "VW_DIR" "\"$VW_DIR\""

                vaultwarden_networks=$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$VW_CID" 2>/dev/null | xargs || true)
                host_binding=$(docker inspect -f '{{range $p, $bindings := .HostConfig.PortBindings}}{{if eq $p "80/tcp"}}{{range $bindings}}{{.HostIp}}:{{.HostPort}} {{end}}{{end}}{{end}}' "$VW_CID" 2>/dev/null | awk 'NF { print $1; exit }')
                if [ -n "$host_binding" ]; then
                    host_ip="${host_binding%%:*}"
                    host_port="${host_binding##*:}"
                fi
                
                # 同步找回 Caddy
                CADDY_CID=$(docker ps -a --format '{{.ID}}\t{{.Image}}' 2>/dev/null | awk -F '\t' '$2 ~ /(^|\/)caddy([:@]|$)/ || $2 ~ /^caddy([:@]|$)/ { print $1; exit }')
                if [ -n "$CADDY_CID" ]; then
                    CADDY_FILE_SRC=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/etc/caddy/Caddyfile"}}{{.Source}}{{end}}{{end}}' "$CADDY_CID" || true)
                    if [ -n "$CADDY_FILE_SRC" ]; then
                        CADDY_DIR=$(dirname "$CADDY_FILE_SRC")
                        if [ -f "$CADDY_DIR/docker-compose.yml" ]; then
                            set_conf_var "CADDY_EXISTS" "\"false\""
                        else
                            set_conf_var "CADDY_EXISTS" "\"true\""
                        fi
                        set_conf_var "CADDY_DIR" "\"$CADDY_DIR\""
                        set_conf_var "EXISTING_CADDY_ID" "\"$CADDY_CID\""
                    fi
                fi

                if [ -n "$CADDY_CID" ] && [ -n "$vaultwarden_networks" ]; then
                    caddy_networks=$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$CADDY_CID" 2>/dev/null | xargs || true)
                    for candidate_net in $vaultwarden_networks; do
                        if echo " $caddy_networks " | grep -Fq " $candidate_net "; then
                            matched_net="$candidate_net"
                            break
                        fi
                    done
                    if [ -n "$matched_net" ]; then
                        set_conf_var "DOCKER_NET" "\"$matched_net\""
                        set_conf_var "DOCKER_NET_OWNED" "\"false\""
                        set_conf_var "PROXY_MODE" "\"caddy\""
                    fi
                elif [ -n "$host_port" ] && [ -n "$host_ip" ] && [ "$host_ip" != "0.0.0.0" ] && [ "$host_ip" != "" ]; then
                    set_conf_var "PROXY_MODE" "\"host_ext\""
                    set_conf_var "EXT_PORT" "\"$host_port\""
                    set_conf_var "DOCKER_NET_OWNED" "\"false\""
                elif [ -n "$vaultwarden_networks" ]; then
                    for candidate_net in $vaultwarden_networks; do
                        case "$candidate_net" in
                            bridge|host|none|ingress)
                                continue
                                ;;
                        esac
                        matched_net="$candidate_net"
                        break
                    done
                    if [ -n "$matched_net" ]; then
                        set_conf_var "DOCKER_NET" "\"$matched_net\""
                        set_conf_var "DOCKER_NET_OWNED" "\"false\""
                        set_conf_var "PROXY_MODE" "\"docker_ext\""
                    fi
                fi
                echo -e "${GREEN}✅ 记忆配置找回成功！${NC}"
                sleep 1
            fi
        fi
    fi
    return 0
}

load_conf_vars() {
    local key=""
    local raw_val=""
    local val=""

    [ -f "$CONF_FILE" ] || return 0

    while IFS='=' read -r key raw_val; do
        key="${key%$'\r'}"
        raw_val="${raw_val%$'\r'}"
        case "$key" in
            ""|\#*)
                continue
                ;;
        esac
        [[ " $CONF_ALLOWED_KEYS " == *" $key "* ]] || continue

        val="$raw_val"
        if [[ "$val" == \"*\" && "$val" == *\" ]]; then
            val="${val#\"}"
            val="${val%\"}"
        fi
        printf -v "$key" '%s' "$val"
    done < "$CONF_FILE"

    return 0
}

load_conf_vars

# ==========================================
# 路径缓存系统 (消除每次调用时的子 shell fork)
# 调用 _update_cached_paths 即可刷新所有路径缓存
# ==========================================
_VW_APP_DIR="" _VW_BACKUP_DIR="" _VW_COMPOSE_FILE="" _VW_BACKUP_COMPOSE_FILE=""
_VW_ENV_FILE="" _BACKUP_ENV_FILE="" _VW_DATA_DIR="" _VW_BACKUP_ARCHIVES_DIR=""
_VW_BACKUP_CONFIG_DIR="" _VW_BACKUP_RCLONE_CONF=""

_update_cached_paths() {
    _VW_APP_DIR="$VW_DIR/$VW_CONTAINER_NAME"
    _VW_BACKUP_DIR="$VW_DIR/$VW_BACKUP_CONTAINER_NAME"
    _VW_COMPOSE_FILE="$_VW_APP_DIR/docker-compose.yml"
    _VW_BACKUP_COMPOSE_FILE="$_VW_BACKUP_DIR/docker-compose.yml"
    _VW_ENV_FILE="$_VW_APP_DIR/vaultwarden.env"
    _BACKUP_ENV_FILE="$_VW_BACKUP_DIR/backup.env"
    _VW_DATA_DIR="$_VW_APP_DIR/data"
    _VW_BACKUP_ARCHIVES_DIR="$_VW_BACKUP_DIR/backups"
    _VW_BACKUP_CONFIG_DIR="$_VW_BACKUP_DIR/vw-backup-config"
    _VW_BACKUP_RCLONE_CONF="$_VW_BACKUP_CONFIG_DIR/rclone/rclone.conf"
}

[ -n "$VW_DIR" ] && _update_cached_paths

persist_runtime_state_defaults() {
    [ -f "$CONF_FILE" ] || return 0

    local _pairs="" _tmp
    [ -n "$PROXY_MODE" ] && _pairs="${_pairs}PROXY_MODE=\"$PROXY_MODE\"\n"
    [ -n "$EXT_PORT" ] && _pairs="${_pairs}EXT_PORT=\"$EXT_PORT\"\n"
    [ -n "$DOCKER_NET_OWNED" ] && _pairs="${_pairs}DOCKER_NET_OWNED=\"$DOCKER_NET_OWNED\"\n"
    [ -n "$BACKUP_MODE" ] && _pairs="${_pairs}BACKUP_MODE=\"$BACKUP_MODE\"\n"
    [ -n "$BACKUP_REMOTE_TARGETS" ] && _pairs="${_pairs}BACKUP_REMOTE_TARGETS=\"$BACKUP_REMOTE_TARGETS\"\n"
    [ -z "$_pairs" ] && return 0

    _tmp=$(mktemp "${CONF_FILE}.XXXXXX")
    _register_tmp_file "$_tmp"
    awk -v pairs="$_pairs" '
        BEGIN { n=split(pairs,arr,"\n"); for(i=1;i<=n;i++){ if(arr[i]=="") continue; split(arr[i],kv,"="); keys[kv[1]]=arr[i]; order[++oc]=kv[1] } }
        { for(k in keys){ if(index($0,k"=")==1){ print keys[k]; delete keys[k]; next } }; print }
        END { for(i=1;i<=oc;i++) if(order[i] in keys) print keys[order[i]] }
    ' "$CONF_FILE" > "$_tmp" && mv "$_tmp" "$CONF_FILE"
    chmod 600 "$CONF_FILE" 2>/dev/null || true

    return 0
}

# 兼容旧版本记忆：补齐缺失的 PROXY_MODE 状态
if [ -z "$PROXY_MODE" ]; then
    if [ "$SKIP_CADDY" == "true" ]; then
        PROXY_MODE="host_ext"
        EXT_PORT=${EXT_PORT:-8080}
    else
        PROXY_MODE="caddy"
    fi
fi

# 兼容旧版本记忆：未记录网络归属时，默认按“非脚本创建”处理，避免误删用户网络
if [ -z "$DOCKER_NET_OWNED" ]; then
    DOCKER_NET_OWNED="false"
fi

infer_backup_mode() {
    local backup_compose=""
    local backup_rclone=""

    if [ -n "$VW_DIR" ]; then
        backup_compose="$VW_DIR/vaultwarden-backup/docker-compose.yml"
        backup_rclone="$VW_DIR/vaultwarden-backup/vw-backup-config/rclone/rclone.conf"
    fi

    if service_running "$VW_BACKUP_CONTAINER_NAME"; then
        if [ -n "$backup_rclone" ] && [ -f "$backup_rclone" ]; then
            echo "remote"
        else
            echo "local"
        fi
        return 0
    fi

    if [ -n "$backup_compose" ] && [ -f "$backup_compose" ]; then
        if [ -f "$backup_rclone" ]; then
            echo "remote"
        else
            echo "local"
        fi
        return 0
    fi

    echo "none"
}

infer_backup_state_detail() {
    local backup_dir=""
    local backup_compose=""
    local backup_rclone=""

    if [ -z "$VW_DIR" ]; then
        echo "none"
        return 0
    fi

    backup_dir="$VW_DIR/vaultwarden-backup"
    backup_compose="$backup_dir/docker-compose.yml"
    backup_rclone="$backup_dir/vw-backup-config/rclone/rclone.conf"

    if [ ! -d "$backup_dir" ]; then
        echo "none"
        return 0
    fi

    if [ ! -f "$backup_compose" ]; then
        echo "dir_only"
        return 0
    fi

    if [ ! -f "$backup_rclone" ] || ! backup_remote_configured; then
        echo "config_pending"
        return 0
    fi

    if service_running "$VW_BACKUP_CONTAINER_NAME"; then
        echo "remote_running"
        return 0
    fi

    echo "remote_ready"
}

sync_backup_mode_state() {
    local inferred_mode=""
    inferred_mode=$(infer_backup_mode)
    if [ "$BACKUP_MODE" != "$inferred_mode" ]; then
        BACKUP_MODE="$inferred_mode"
        set_conf_var "BACKUP_MODE" "\"$BACKUP_MODE\""
    fi
}



is_secret_key() {
    local key="$1"
    case "$key" in
        ADMIN_TOKEN|SMTP_PASSWORD|ZIP_PASSWORD|PUSH_INSTALLATION_KEY|MAIL_SMTP_VARIABLES)
            return 0
            ;;
    esac
    return 1
}

print_config_update_message() {
    local key="$1"
    local val="$2"
    if is_secret_key "$key"; then
        echo -e "${GREEN}✅ 变量 [$key] 已更新（敏感值已隐藏）。${NC}"
    else
        echo -e "${GREEN}✅ 变量 [$key] 已更新为: $val${NC}"
    fi
}

hash_admin_token_with_docker() {
    local raw_token="$1"
    local encrypted_token=""
    local salt=""
    local _tmpout

    salt=$(head -c 32 /dev/urandom | base64 | tr -d '\n')
    _tmpout=$(mktemp)
    _register_tmp_file "$_tmpout"

    docker run --rm \
        -e VW_PWD="$raw_token" \
        -e VW_SALT="$salt" \
        alpine:3.20 \
        sh -c 'apk add --no-cache argon2 >/dev/null 2>&1 && printf "%s" "$VW_PWD" | argon2 "$VW_SALT" -e -id -m 16 -t 3 -p 4' \
        > "$_tmpout" 2>/dev/null &
    _spinner $!
    wait $! 2>/dev/null || true

    encrypted_token=$(<"$_tmpout")
    rm -f "$_tmpout"

    if [[ -n "$encrypted_token" && "$encrypted_token" == \$argon2id* ]]; then
        printf '%s\n' "$encrypted_token"
        return 0
    fi

    return 1
}

can_manage_global_shortcut() {
    [ -d "/usr/local/bin" ] && [ -w "/usr/local/bin" ]
}

backup_service_initialized() {
    [ -n "$VW_DIR" ] && [ -f "$_VW_BACKUP_COMPOSE_FILE" ]
}

backup_remote_configured() {
    backup_service_initialized && [ -f "$_VW_BACKUP_RCLONE_CONF" ] && [ "$(count_backup_remote_targets)" -gt 0 ]
}

has_global_shortcut() {
    [ -f "/usr/local/bin/vw" ]
}

print_invalid_choice() {
    echo -e "${RED}❌ 无效的选择，请重新输入。${NC}"
}

print_missing_config_message() {
    local subject="$1"
    local step_no="$2"
    local action_text="$3"

    echo -e "${YELLOW}⚠️ 当前还没有检测到${subject}。${NC}"
    echo -e "${YELLOW}👉 请先执行步骤 ${step_no}，${action_text}。${NC}"
}

compose_up_dir() {
    local dir="$1"
    shift
    (cd "$dir" && docker compose up -d "$@")
}

compose_up_force_recreate_dir() {
    local dir="$1"
    shift
    (cd "$dir" && docker compose up -d --force-recreate "$@")
}

compose_start_service_dir() {
    local dir="$1"
    local service_name="$2"
    (cd "$dir" && docker compose start "$service_name" >/dev/null 2>&1) || true
}

compose_stop_service_dir() {
    local dir="$1"
    local service_name="$2"
    (cd "$dir" && docker compose stop "$service_name" >/dev/null 2>&1) || true
}

compose_down_dir() {
    local dir="$1"
    shift
    (cd "$dir" && docker compose down "$@" 2>/dev/null) || true
}

compose_pull_dir() {
    local dir="$1"
    shift
    (cd "$dir" && docker compose pull "$@")
}

compose_up_remove_orphans_dir() {
    local dir="$1"
    shift
    (cd "$dir" && docker compose up -d --remove-orphans "$@")
}

# ==========================================
# 从各业务函数内部提出的通用辅助 (原逻辑不变)
# ==========================================

# 布尔值交互选择器 (原位于 modify_core_config 内部)
_ask_bool() {
    while true; do
        echo -e "\n${YELLOW}👉 $1${NC}" >&2
        echo -e "   ${GREEN}1) 开启 (true)${NC}" >&2
        echo -e "   ${RED}2) 关闭 (false)${NC}" >&2
        echo -e "   ${YELLOW}q) 取消并返回${NC}" >&2
        read -p "请选择 [1, 2, q]: " choice < /dev/tty
        if [ "$choice" = "1" ]; then echo "true"; break
        elif [ "$choice" = "2" ]; then echo "false"; break
        elif [[ "$choice" == "q" || "$choice" == "Q" ]]; then echo ""; break 
        else echo -e "${RED}❌ 输入无效，请按 1, 2 或 q${NC}" >&2
        fi
    done
}

# SMTP 变量单项更新 (原位于 modify_core_config case 15 内部)
# 返回 0 表示已更新，1 表示跳过
_update_smtp_var() {
    local env_file="$1" k="$2" v="$3"
    if [ -n "$v" ]; then
        set_env_var "$env_file" "$k" "$v"
        print_config_update_message "$k" "$v"
        return 0
    fi
    return 1
}

# 日志查看模式选择 (原位于 view_logs 内部)
_view_container_log() {
    local container_name="$1"
    local container_label="$2"
    echo -e "\n${CYAN}请选择日志查看模式:${NC}"
    echo " 1) 查看最近 100 行日志 (静态)"
    echo " 2) 实时跟踪日志 (Ctrl+C 退出)"
    local log_mode=""
    read -p "请选择 [1-2, 默认 2]: " log_mode
    echo -e "${CYAN}📜 正在查看 ${container_label} 日志${NC}"
    case "${log_mode:-2}" in
        1) docker logs --tail 100 "$container_name" ;;
        *) docker logs --tail 100 -f "$container_name" ;;
    esac
}

# 递归展开嵌套归档包 (原位于 restore_from_local_backup 内部)
_restore_expand_nested_archives() {
    local root="$1"
    local extracted_any=0
    local archive=""
    local pass=0

    while [ "$pass" -lt 5 ]; do
        extracted_any=0
        while IFS= read -r archive; do
            [ -n "$archive" ] || continue

            case "$archive" in
                *.tar|*.tar.gz|*.tgz|*.tar.bz2|*.tbz2|*.tar.xz|*.txz)
                    if tar -xf "$archive" -C "$root" >/dev/null 2>&1; then
                        rm -f "$archive"
                        extracted_any=1
                    fi
                    ;;
                *.zip)
                    if unzip -oq "$archive" -d "$root" >/dev/null 2>&1; then
                        rm -f "$archive"
                        extracted_any=1
                    fi
                    ;;
            esac
        done < <(find "$root" -maxdepth 1 -type f \( -name '*.tar' -o -name '*.tar.gz' -o -name '*.tgz' -o -name '*.tar.bz2' -o -name '*.tbz2' -o -name '*.tar.xz' -o -name '*.txz' -o -name '*.zip' \) -print 2>/dev/null)

        [ "$extracted_any" -eq 1 ] || break
        pass=$((pass + 1))
    done
}

# 标准化恢复包目录结构 (原位于 restore_from_local_backup 内部)
_normalize_restore_payload() {
    local root="$1"
    local db_candidate=""
    local db_file=""
    local key_candidate=""
    local key_file=""

    _restore_expand_nested_archives "$root"

    db_candidate=$(find "$root" -maxdepth 1 -type f \( -name 'db.sqlite3' -o -name 'db.*.sqlite3' \) | sort | tail -n 1)
    if [ -n "$db_candidate" ]; then
        if [ "$(basename "$db_candidate")" != "db.sqlite3" ]; then
            cp -f "$db_candidate" "$root/db.sqlite3"
        fi
        while IFS= read -r db_file; do
            [ -n "$db_file" ] || continue
            [ "$(basename "$db_file")" = "db.sqlite3" ] && continue
            rm -f "$db_file"
        done < <(find "$root" -maxdepth 1 -type f -name 'db.*.sqlite3' -print 2>/dev/null)
    fi

    key_candidate=""
    while IFS= read -r key_file; do
        [ -n "$key_file" ] || continue
        key_candidate="$key_file"
        case "$(basename "$key_file")" in
            rsa_key.pem|rsa_key)
                key_candidate="$key_file"
                break
                ;;
        esac
    done < <(find "$root" -maxdepth 1 -type f \( -name 'rsa_key' -o -name 'rsa_key.pem' -o -name 'rsa_key.*' -o -name 'rsakey.*' \) -print 2>/dev/null | sort)

    if [ -n "$key_candidate" ]; then
        case "$(basename "$key_candidate")" in
            *.pem)
                cp -f "$key_candidate" "$root/rsa_key.pem"
                ;;
            *)
                cp -f "$key_candidate" "$root/rsa_key"
                ;;
        esac
    fi

    while IFS= read -r key_file; do
        [ -n "$key_file" ] || continue
        case "$(basename "$key_file")" in
            rsa_key|rsa_key.pem)
                ;;
            *)
                rm -f "$key_file"
                ;;
        esac
    done < <(find "$root" -maxdepth 1 -type f \( -name 'rsa_key.*' -o -name 'rsakey.*' \) -print 2>/dev/null)
}

clear_runtime_route_state() {
    DOCKER_NET=""
    DOCKER_NET_OWNED="false"
    CADDY_DIR=""
    CADDY_EXISTS=""
    EXISTING_CADDY_ID=""
    PROXY_MODE=""
    SKIP_CADDY=""
    EXT_PORT=""
}

persist_runtime_route_state() {
    [ -f "$CONF_FILE" ] || return 0

    set_conf_var "DOCKER_NET" "\"$DOCKER_NET\""
    set_conf_var "DOCKER_NET_OWNED" "\"$DOCKER_NET_OWNED\""
    set_conf_var "CADDY_DIR" "\"$CADDY_DIR\""
    set_conf_var "CADDY_EXISTS" "\"$CADDY_EXISTS\""
    set_conf_var "EXISTING_CADDY_ID" "\"$EXISTING_CADDY_ID\""
    set_conf_var "PROXY_MODE" "\"$PROXY_MODE\""
    set_conf_var "SKIP_CADDY" "\"$SKIP_CADDY\""
    set_conf_var "EXT_PORT" "\"$EXT_PORT\""
}

has_any_managed_deployment() {
    if [ -n "$VW_DIR" ] && {
        [ -d "$_VW_APP_DIR" ] || [ -d "$_VW_BACKUP_DIR" ] || [ -f "$_VW_COMPOSE_FILE" ] || [ -f "$_VW_BACKUP_COMPOSE_FILE" ];
    }; then
        return 0
    fi

    if [ -n "$(find_vaultwarden_container_id)" ]; then
        return 0
    fi

    if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -Fxq "$VW_BACKUP_CONTAINER_NAME"; then
        return 0
    fi

    return 1
}

require_main_service_config() {
    if [ -z "$VW_DIR" ] || [ ! -f "$_VW_COMPOSE_FILE" ]; then
        print_missing_config_message "主服务配置" "1" "生成主服务配置"
        return 1
    fi
    return 0
}

require_backup_service_config() {
    if ! backup_service_initialized; then
        print_missing_config_message "备份服务配置" "2" "初始化备份服务"
        return 1
    fi
    return 0
}

wait_for_vaultwarden_ready() {
    local attempts=0
    local max_attempts="${1:-10}"
    local sleep_seconds="${2:-2}"

    while [ "$attempts" -lt "$max_attempts" ]; do
        if probe_vaultwarden_http; then
            return 0
        fi
        sleep "$sleep_seconds"
        attempts=$((attempts + 1))
    done

    return 1
}

restart_vaultwarden_service_apply() {
    if [ -z "$VW_DIR" ] || [ ! -f "$_VW_COMPOSE_FILE" ]; then
        return 1
    fi

    compose_up_dir "$_VW_APP_DIR"
}

restart_backup_service_apply() {
    if ! backup_service_initialized; then
        return 1
    fi

    compose_up_force_recreate_dir "$_VW_BACKUP_DIR"
}

prompt_restart_vaultwarden_if_running() {
    local prompt_text="${1:-是否立即重启主服务以应用配置？(y/n): }"
    local not_running_message="${2:-${YELLOW}💡 主服务当前未启动。修改已保存，稍后启动主服务即可生效。${NC}}"

    if service_running "$VW_CONTAINER_NAME"; then
        local restart_now=""
        read -p "$prompt_text" restart_now
        if [[ "$restart_now" == [yY]* ]]; then
            echo -e "${YELLOW}正在重启主服务并应用配置...${NC}"
            restart_vaultwarden_service_apply
            return 10
        fi
        return 0
    fi

    echo -e "$not_running_message"
    return 1
}

prompt_restart_backup_if_running() {
    local prompt_text="${1:-是否立即重启备份服务以应用配置？(y/n): }"
    local not_running_message="${2:-${YELLOW}💡 备份服务当前未启动。修改已保存，稍后启动备份服务即可生效。${NC}}"

    if service_running "$VW_BACKUP_CONTAINER_NAME"; then
        local restart_now=""
        read -p "$prompt_text" restart_now
        if [[ "$restart_now" == [yY]* ]]; then
            echo -e "${YELLOW}正在重启备份服务并应用配置...${NC}"
            restart_backup_service_apply
            return 10
        fi
        return 0
    fi

    echo -e "$not_running_message"
    return 1
}

caddy_manages_domain() {
    local domain_host="$1"
    local caddy_file=""

    [ -n "$domain_host" ] || return 1
    [ -n "$CADDY_DIR" ] || return 1

    caddy_file="$CADDY_DIR/Caddyfile"
    [ -f "$caddy_file" ] || return 1

    grep -Fqx "$(vw_caddy_block_begin "$domain_host")" "$caddy_file" 2>/dev/null
}

# ==========================================
# 注入与静默更新全局快捷命令 (自愈修复机制)
# ==========================================
if [ "${VW_WEB_MODE:-0}" != "1" ] && [ -n "$SCRIPT_URL" ] && [[ "$SCRIPT_URL" == http* ]]; then
    if can_manage_global_shortcut; then
        if [ ! -f "/usr/local/bin/vw" ]; then
            echo -e "${YELLOW}正在为你安装全局快捷命令 'vw'...${NC}"
            echo -e "${GREEN}✅ 快捷命令安装完成！以后在任意目录输入 ${NC}vw${GREEN} 即可呼出本管理面板。${NC}"
            sleep 2
        fi
        cat << EOF > /usr/local/bin/vw
#!/bin/bash
bash <(curl -Ls "$SCRIPT_URL")
EOF
        chmod +x /usr/local/bin/vw
    elif [ ! -f "/usr/local/bin/vw" ]; then
        echo -e "${YELLOW}⚠️ 当前用户无权写入 /usr/local/bin，已跳过全局快捷命令安装。${NC}"
    fi
fi

# ==========================================
# 🛡️ 企业级数据强制校验组件
# ==========================================
validate_password() {
    local pwd="$1"
    if [ ${#pwd} -lt 8 ]; then
        echo -e "${RED}❌ 密码太短！必须至少包含 8 个字符。${NC}" >&2; return 1
    elif [[ ! "$pwd" =~ [A-Z] ]]; then
        echo -e "${RED}❌ 强度不足！必须包含至少一个大写字母 (A-Z)。${NC}" >&2; return 1
    elif [[ ! "$pwd" =~ [a-z] ]]; then
        echo -e "${RED}❌ 强度不足！必须包含至少一个小写字母 (a-z)。${NC}" >&2; return 1
    elif [[ ! "$pwd" =~ [0-9] ]]; then
        echo -e "${RED}❌ 强度不足！必须包含至少一个数字 (0-9)。${NC}" >&2; return 1
    elif [[ ! "$pwd" =~ [^a-zA-Z0-9] ]]; then
        echo -e "${RED}❌ 强度不足！必须包含至少一个特殊符号 (如 @, #, $, % 等)。${NC}" >&2; return 1
    fi
    return 0
}

validate_email() {
    local email="$1"
    if [[ ! "$email" =~ ^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$ ]]; then
        echo -e "${RED}❌ 邮箱格式不正确，请重新输入！${NC}" >&2; return 1
    fi
    return 0
}

ensure_unzip_available() {
    if command -v unzip >/dev/null 2>&1; then
        return 0
    fi

    echo -e "${YELLOW}⚠️ 检测到系统尚未安装 unzip，自动解压恢复功能暂时不可用。${NC}"
    read -p "是否现在自动安装 unzip？(y/n, 默认y): " install_unzip
    if [[ "$install_unzip" == [nN]* ]]; then
        echo -e "${RED}❌ 未安装 unzip，无法继续自动恢复流程。${NC}"
        return 1
    fi

    echo -e "${GREEN}正在尝试自动安装 unzip，请稍候...${NC}"
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update >/dev/null 2>&1 && apt-get install -y unzip >/dev/null 2>&1
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y unzip >/dev/null 2>&1
    elif command -v yum >/dev/null 2>&1; then
        yum install -y unzip >/dev/null 2>&1
    elif command -v apk >/dev/null 2>&1; then
        apk add --no-cache unzip >/dev/null 2>&1
    else
        echo -e "${RED}❌ 未识别到受支持的包管理器，请手动安装 unzip 后重试。${NC}"
        return 1
    fi

    if command -v unzip >/dev/null 2>&1; then
        set_conf_var "UNZIP_INSTALLED_BY_SCRIPT" "\"true\""
        echo -e "${GREEN}✅ unzip 安装完成，已继续自动恢复流程。${NC}"
        return 0
    fi

    echo -e "${RED}❌ unzip 安装失败，请手动安装后重试。${NC}"
    return 1
}

remove_unzip_if_managed() {
    if [ "$UNZIP_INSTALLED_BY_SCRIPT" != "true" ]; then
        return 0
    fi

    if ! command -v unzip >/dev/null 2>&1; then
        set_conf_var "UNZIP_INSTALLED_BY_SCRIPT" "\"false\""
        return 0
    fi

    echo -e "${YELLOW}👉 检测到 unzip 曾由本脚本自动安装。${NC}"
    read -p "卸载环境时是否一并卸载 unzip？(y/n, 默认n): " remove_unzip
    if [[ "$remove_unzip" != [yY]* ]]; then
        echo -e "${YELLOW}💡 已保留 unzip，不做处理。${NC}"
        return 0
    fi

    echo -e "${YELLOW}正在尝试卸载 unzip，请稍候...${NC}"
    if command -v apt-get >/dev/null 2>&1; then
        apt-get remove -y unzip >/dev/null 2>&1 || apt-get purge -y unzip >/dev/null 2>&1 || true
    elif command -v dnf >/dev/null 2>&1; then
        dnf remove -y unzip >/dev/null 2>&1 || true
    elif command -v yum >/dev/null 2>&1; then
        yum remove -y unzip >/dev/null 2>&1 || true
    elif command -v apk >/dev/null 2>&1; then
        apk del unzip >/dev/null 2>&1 || true
    else
        echo -e "${YELLOW}⚠️ 未识别到受支持的包管理器，无法自动卸载 unzip。${NC}"
        return 1
    fi

    if command -v unzip >/dev/null 2>&1; then
        echo -e "${YELLOW}⚠️ unzip 卸载未完成，可能仍被系统其他软件依赖。${NC}"
        return 1
    fi

    set_conf_var "UNZIP_INSTALLED_BY_SCRIPT" "\"false\""
    echo -e "${GREEN}✅ unzip 已随本次卸载流程一并移除。${NC}"
    return 0
}

vw_root_dir() {
    echo "$VW_DIR"
}

vw_app_dir() {
    echo "$_VW_APP_DIR"
}

vw_backup_dir() {
    echo "$_VW_BACKUP_DIR"
}

vw_compose_file() {
    echo "$_VW_COMPOSE_FILE"
}

vw_backup_compose_file() {
    echo "$_VW_BACKUP_COMPOSE_FILE"
}

vw_env_file() {
    echo "$_VW_ENV_FILE"
}

backup_env_file() {
    echo "$_BACKUP_ENV_FILE"
}

vw_data_dir() {
    echo "$_VW_DATA_DIR"
}

vw_backup_archives_dir() {
    echo "$_VW_BACKUP_ARCHIVES_DIR"
}

vw_backup_config_dir() {
    echo "$_VW_BACKUP_CONFIG_DIR"
}

vw_backup_rclone_conf() {
    echo "$_VW_BACKUP_RCLONE_CONF"
}

list_rclone_remotes() {
    local conf_file="${1:-$_VW_BACKUP_RCLONE_CONF}"
    [ -f "$conf_file" ] || return 0
    grep '^\[.*\]$' "$conf_file" | tr -d '[]'
}

list_authorized_rclone_remotes() {
    local conf_file="${1:-$_VW_BACKUP_RCLONE_CONF}"

    [ -f "$conf_file" ] || return 0

    # 不能用 token= 判断“可启用 remote”。
    # 很多后端（例如 s3）本来就不会写 token 字段，但依然是可正常使用的有效配置。
    list_rclone_remotes "$conf_file"
}

get_rclone_remote_option() {
    local conf_file="$1"
    local remote_name="$2"
    local option_key="$3"

    [ -f "$conf_file" ] || return 0

    awk -v section="[$remote_name]" -v key="$option_key" '
        $0 == section { in_section=1; next }
        /^\[.*\]$/ && in_section { exit }
        in_section && $0 ~ "^[[:space:]]*" key "[[:space:]]*=" {
            sub(/^[^=]*=[[:space:]]*/, "", $0)
            print
            exit
        }
    ' "$conf_file"
}

rclone_remote_is_s3_compatible() {
    local conf_file="$1"
    local remote_name="$2"
    [ "$(get_rclone_remote_option "$conf_file" "$remote_name" "type")" = "s3" ]
}

rclone_remote_section_exists() {
    local conf_file="$1"
    local remote_name="$2"

    [ -f "$conf_file" ] || return 1
    grep -Fqx "[$remote_name]" "$conf_file"
}

validate_rclone_remote_name() {
    local remote_name="$1"
    [[ "$remote_name" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]
}

remove_rclone_remote_section() {
    local conf_file="$1"
    local remote_name="$2"
    local tmp_file
    tmp_file=$(mktemp "${conf_file}.XXXXXX")
    _register_tmp_file "$tmp_file"

    [ -f "$conf_file" ] || return 0

    awk -v section="[$remote_name]" '
        $0 == section { skip=1; next }
        /^\[.*\]$/ && skip { skip=0 }
        !skip { print }
    ' "$conf_file" > "$tmp_file" && mv "$tmp_file" "$conf_file"
}

upsert_rclone_remote_section() {
    local conf_file="$1"
    local remote_name="$2"
    shift 2

    mkdir -p "$(dirname "$conf_file")"
    touch "$conf_file"
    remove_rclone_remote_section "$conf_file" "$remote_name"

    if [ -s "$conf_file" ]; then
        printf '\n' >> "$conf_file"
    fi

    {
        printf '[%s]\n' "$remote_name"
        while [ "$#" -gt 0 ]; do
            printf '%s\n' "$1"
            shift
        done
    } >> "$conf_file"
}

rclone_remote_display_type() {
    local remote_type="$1"

    case "$remote_type" in
        s3)
            echo "S3 兼容存储"
            ;;
        webdav)
            echo "WebDAV"
            ;;
        sftp)
            echo "SFTP"
            ;;
        ftp)
            echo "FTP"
            ;;
        *)
            echo "$remote_type"
            ;;
    esac
}

prompt_rclone_remote_name_for_manual_config() {
    local conf_file="$1"
    local remote_name=""
    local existing_type=""
    local conflict_choice=""

    while true; do
        echo -e "${YELLOW}💡 远程节点名称就是这份远程存储配置的“代号”。${NC}" >&2
        echo -e "${YELLOW}👉 后续在“选择当前启用的上传目标”时，您会看到这个名字，例如: r2_backup / webdav_nas / sftp_offsite${NC}" >&2
        read -p "请输入远程节点名称 [仅支持字母/数字/._-，直接回车取消]: " remote_name
        [ -n "$remote_name" ] || return 1

        if ! validate_rclone_remote_name "$remote_name"; then
            echo -e "${RED}❌ 远程节点名称格式不合法，请使用字母、数字、下划线、中划线或点号。${NC}" >&2
            continue
        fi

        if ! rclone_remote_section_exists "$conf_file" "$remote_name"; then
            printf '%s\n' "$remote_name"
            return 0
        fi

        existing_type=$(get_rclone_remote_option "$conf_file" "$remote_name" "type")
        existing_type=$(rclone_remote_display_type "$existing_type")
        echo -e "${YELLOW}⚠️ 检测到远程节点 [$remote_name] 已存在。当前类型: ${existing_type:-未知}${NC}" >&2
        echo " 1) 覆盖更新这个远程节点" >&2
        echo " 2) 重新输入一个新名称" >&2
        echo -e "${YELLOW} q) 取消返回${NC}" >&2
        read -p "请选择操作 [1, 2, q]: " conflict_choice
        case "$conflict_choice" in
            1)
                printf '%s\n' "$remote_name"
                return 0
                ;;
            2)
                continue
                ;;
            q|Q)
                return 1
                ;;
            *)
                print_invalid_choice
                ;;
        esac
    done
}

obscure_rclone_password_if_present() {
    local raw_val="$1"
    local obscured=""

    [ -n "$raw_val" ] || return 0

    obscured=$(run_rclone_cli obscure "$raw_val" 2>/dev/null || true)
    if [ -n "$obscured" ]; then
        printf '%s\n' "$obscured"
    else
        printf '%s\n' "$raw_val"
    fi
}

write_manual_rclone_remote_with_preview() {
    local conf_file="$1"
    local remote_name="$2"
    shift 2

    upsert_rclone_remote_section "$conf_file" "$remote_name" "$@"
    echo -e "${GREEN}✅ 远程节点 [$remote_name] 已写入 rclone.conf。${NC}"
    echo -e "${CYAN}当前写入结果（已脱敏）:${NC}"
    show_rclone_config_redacted "$conf_file" "$remote_name" || {
        echo -e "${YELLOW}⚠️ 配置已写入，但暂时无法读取脱敏结果。请稍后通过“查看当前 Rclone 配置（已脱敏）”确认。${NC}"
        return 1
    }
}

configure_manual_s3_remote() {
    local conf_file="$1"
    local remote_name=""
    local provider=""
    local endpoint=""
    local access_key_id=""
    local secret_access_key=""
    local region=""
    local bucket_acl=""
    local no_check_bucket_choice=""
    local no_check_bucket="false"

    remote_name=$(prompt_rclone_remote_name_for_manual_config "$conf_file") || {
        echo -e "${YELLOW}👉 已取消手动配置 S3 远程节点。${NC}"
        return 1
    }

    echo -e "${CYAN}正在配置 S3 兼容远程节点 [$remote_name]${NC}"
    read -p "provider [默认 Other，可填 Cloudflare/AWS/Alibaba/Minio 等]: " provider
    provider=${provider:-Other}
    read -p "endpoint [如 https://xxxx.r2.cloudflarestorage.com，留空按后端默认]: " endpoint
    read -p "access_key_id: " access_key_id
    [ -n "$access_key_id" ] || {
        echo -e "${RED}❌ access_key_id 不能为空。${NC}"
        return 1
    }
    read -s -p "secret_access_key (输入不可见): " secret_access_key ; echo ""
    [ -n "$secret_access_key" ] || {
        echo -e "${RED}❌ secret_access_key 不能为空。${NC}"
        return 1
    }
    read -p "region [可留空]: " region
    read -p "acl [可留空，常见如 private]: " bucket_acl
    read -p "是否启用 no_check_bucket = true？(y/n, 默认n): " no_check_bucket_choice
    if [[ "$no_check_bucket_choice" == [yY]* ]]; then
        no_check_bucket="true"
    fi

    local lines=(
        "type = s3"
        "provider = $provider"
        "access_key_id = $access_key_id"
        "secret_access_key = $secret_access_key"
    )
    [ -n "$endpoint" ] && lines+=("endpoint = $endpoint")
    [ -n "$region" ] && lines+=("region = $region")
    [ -n "$bucket_acl" ] && lines+=("acl = $bucket_acl")
    [ "$no_check_bucket" = "true" ] && lines+=("no_check_bucket = true")

    write_manual_rclone_remote_with_preview "$conf_file" "$remote_name" "${lines[@]}"
}

configure_manual_webdav_remote() {
    local conf_file="$1"
    local remote_name=""
    local url=""
    local vendor=""
    local user_name=""
    local password=""
    local obscured_password=""

    remote_name=$(prompt_rclone_remote_name_for_manual_config "$conf_file") || {
        echo -e "${YELLOW}👉 已取消手动配置 WebDAV 远程节点。${NC}"
        return 1
    }

    echo -e "${CYAN}正在配置 WebDAV 远程节点 [$remote_name]${NC}"
    read -p "WebDAV 地址 URL: " url
    [ -n "$url" ] || {
        echo -e "${RED}❌ URL 不能为空。${NC}"
        return 1
    }
    read -p "vendor [默认 other，可填 nextcloud/owncloud/sharepoint/other]: " vendor
    vendor=${vendor:-other}
    read -p "用户名 [可留空]: " user_name
    read -s -p "密码 (输入不可见，可留空): " password ; echo ""
    obscured_password=$(obscure_rclone_password_if_present "$password")

    local lines=(
        "type = webdav"
        "url = $url"
        "vendor = $vendor"
    )
    [ -n "$user_name" ] && lines+=("user = $user_name")
    [ -n "$obscured_password" ] && lines+=("pass = $obscured_password")

    write_manual_rclone_remote_with_preview "$conf_file" "$remote_name" "${lines[@]}"
}

configure_manual_sftp_remote() {
    local conf_file="$1"
    local remote_name=""
    local host=""
    local port=""
    local user_name=""
    local auth_choice=""
    local password=""
    local obscured_password=""
    local key_file=""

    remote_name=$(prompt_rclone_remote_name_for_manual_config "$conf_file") || {
        echo -e "${YELLOW}👉 已取消手动配置 SFTP 远程节点。${NC}"
        return 1
    }

    echo -e "${CYAN}正在配置 SFTP 远程节点 [$remote_name]${NC}"
    read -p "主机地址 Host: " host
    [ -n "$host" ] || {
        echo -e "${RED}❌ Host 不能为空。${NC}"
        return 1
    }
    read -p "端口 [默认 22]: " port
    port=${port:-22}
    if ! validate_positive_integer "$port"; then
        echo -e "${RED}❌ 端口必须是正整数。${NC}"
        return 1
    fi
    read -p "用户名 User: " user_name
    [ -n "$user_name" ] || {
        echo -e "${RED}❌ User 不能为空。${NC}"
        return 1
    }

    echo "请选择认证方式:"
    echo " 1) 密码"
    echo " 2) 私钥文件"
    read -p "请输入选项 [1, 2]: " auth_choice
    case "$auth_choice" in
        1)
            read -s -p "密码 (输入不可见): " password ; echo ""
            [ -n "$password" ] || {
                echo -e "${RED}❌ 密码不能为空。${NC}"
                return 1
            }
            obscured_password=$(obscure_rclone_password_if_present "$password")
            ;;
        2)
            read -p "私钥文件路径 [如 /root/.ssh/id_rsa]: " key_file
            [ -n "$key_file" ] || {
                echo -e "${RED}❌ 私钥文件路径不能为空。${NC}"
                return 1
            }
            ;;
        *)
            print_invalid_choice
            return 1
            ;;
    esac

    local lines=(
        "type = sftp"
        "host = $host"
        "port = $port"
        "user = $user_name"
    )
    [ -n "$obscured_password" ] && lines+=("pass = $obscured_password")
    [ -n "$key_file" ] && lines+=("key_file = $key_file")

    write_manual_rclone_remote_with_preview "$conf_file" "$remote_name" "${lines[@]}"
}

configure_manual_ftp_remote() {
    local conf_file="$1"
    local remote_name=""
    local host=""
    local port=""
    local user_name=""
    local password=""
    local obscured_password=""
    local tls_choice=""
    local use_tls="false"

    remote_name=$(prompt_rclone_remote_name_for_manual_config "$conf_file") || {
        echo -e "${YELLOW}👉 已取消手动配置 FTP 远程节点。${NC}"
        return 1
    }

    echo -e "${CYAN}正在配置 FTP 远程节点 [$remote_name]${NC}"
    read -p "主机地址 Host: " host
    [ -n "$host" ] || {
        echo -e "${RED}❌ Host 不能为空。${NC}"
        return 1
    }
    read -p "端口 [默认 21]: " port
    port=${port:-21}
    if ! validate_positive_integer "$port"; then
        echo -e "${RED}❌ 端口必须是正整数。${NC}"
        return 1
    fi
    read -p "用户名 User [可留空]: " user_name
    read -s -p "密码 (输入不可见，可留空): " password ; echo ""
    obscured_password=$(obscure_rclone_password_if_present "$password")
    read -p "是否启用 TLS？(y/n, 默认n): " tls_choice
    if [[ "$tls_choice" == [yY]* ]]; then
        use_tls="true"
    fi

    local lines=(
        "type = ftp"
        "host = $host"
        "port = $port"
    )
    [ -n "$user_name" ] && lines+=("user = $user_name")
    [ -n "$obscured_password" ] && lines+=("pass = $obscured_password")
    [ "$use_tls" = "true" ] && lines+=("tls = true")

    write_manual_rclone_remote_with_preview "$conf_file" "$remote_name" "${lines[@]}"
}

configure_manual_rclone_remote() {
    local conf_file="${1:-$_VW_BACKUP_RCLONE_CONF}"
    local type_choice=""

    mkdir -p "$(dirname "$conf_file")"
    touch "$conf_file"

    while true; do
        echo -e "\n${CYAN}手动填写参数并生成远程节点:${NC}"
        echo " 1) S3 兼容存储"
        echo " 2) WebDAV"
        echo " 3) SFTP"
        echo " 4) FTP"
        echo -e "${YELLOW} q) 返回上一级${NC}"
        read -p "请选择远程类型 [1-4, q]: " type_choice

        case "$type_choice" in
            1)
                configure_manual_s3_remote "$conf_file"
                return $?
                ;;
            2)
                configure_manual_webdav_remote "$conf_file"
                return $?
                ;;
            3)
                configure_manual_sftp_remote "$conf_file"
                return $?
                ;;
            4)
                configure_manual_ftp_remote "$conf_file"
                return $?
                ;;
            q|Q)
                echo -e "${YELLOW}👉 已取消手动配置远程节点。${NC}"
                return 1
                ;;
            *)
                print_invalid_choice
                ;;
        esac
    done
}

set_rclone_remote_option() {
    local conf_file="$1"
    local remote_name="$2"
    local option_key="$3"
    local option_val="$4"

    [ -f "$conf_file" ] || return 1
    rclone_remote_section_exists "$conf_file" "$remote_name" || return 1
    run_rclone_cli config update "$remote_name" "$option_key" "$option_val" >/dev/null
}

show_rclone_config_redacted() {
    local conf_file="$1"
    local remote_name="$2"

    [ -f "$conf_file" ] || return 1

    if [ -n "$remote_name" ]; then
        run_rclone_cli config redacted "$remote_name"
    else
        run_rclone_cli config redacted
    fi
}

show_rclone_config_dump() {
    local conf_file="$1"

    [ -f "$conf_file" ] || return 1
    docker run --rm \
        -v "$_VW_BACKUP_CONFIG_DIR:/config" \
        --entrypoint sh \
        ttionya/vaultwarden-backup:latest \
        -ec '
            if command -v jq >/dev/null 2>&1; then
                rclone --config /config/rclone/rclone.conf config dump | jq .
            else
                rclone --config /config/rclone/rclone.conf config dump
            fi
        '
}

show_rclone_config_view_menu() {
    local conf_file="$1"
    local remotes=()
    local remote_name=""
    local active_targets=()
    local active_target=""
    local active_remote_name=""
    local remote_label=""
    local i=""
    local view_choice=""

    [ -f "$conf_file" ] || {
        echo -e "${RED}❌ 未检测到 rclone.conf，请先完成 Rclone 配置。${NC}"
        return 1
    }

    while IFS= read -r remote_name; do
        [ -n "$remote_name" ] || continue
        remotes+=("$remote_name")
    done < <(list_rclone_remotes "$conf_file")

    while IFS= read -r active_target; do
        [ -n "$active_target" ] || continue
        active_remote_name="${active_target%%:*}"
        active_targets+=("$active_remote_name")
    done < <(list_backup_remote_targets)

    if [ ${#remotes[@]} -eq 0 ]; then
        echo -e "${YELLOW}⚠️ 当前 rclone.conf 中还没有可查看的远程节点。${NC}"
        return 1
    fi

    echo -e "\n${CYAN}Rclone 配置查看:${NC}"
    echo " 1) 查看全部远程配置（已脱敏）"
    for i in "${!remotes[@]}"; do
        remote_label="${remotes[$i]}"
        if echo " ${active_targets[*]} " | grep -Fq " ${remotes[$i]} "; then
            remote_label="${remote_label}（当前启用）"
        fi
        echo " $((i+2))) 查看远程目标 [${remote_label}]（已脱敏）"
    done
    echo " $(( ${#remotes[@]} + 2 ))) 查看 JSON 配置（config dump）"
    echo -e "${YELLOW} q) 返回上一级${NC}"

    read -p "请选择要查看的配置: " view_choice
    case "$view_choice" in
        1)
            show_rclone_config_redacted "$conf_file" || {
                echo -e "${RED}❌ 获取 Rclone 配置失败，请检查 rclone.conf 是否可被容器正常读取。${NC}"
                return 1
            }
            ;;
        q|Q)
            return 0
            ;;
        *)
            if [[ "$view_choice" =~ ^[0-9]+$ ]] && [ "$view_choice" -eq $(( ${#remotes[@]} + 2 )) ]; then
                show_rclone_config_dump "$conf_file" || {
                    echo -e "${RED}❌ 获取 Rclone JSON 配置失败，请检查 rclone.conf 是否可被容器正常读取。${NC}"
                    return 1
                }
                return 0
            fi
            if [[ "$view_choice" =~ ^[0-9]+$ ]] && [ "$view_choice" -ge 2 ] && [ "$view_choice" -le $(( ${#remotes[@]} + 1 )) ]; then
                remote_name="${remotes[$((view_choice-2))]}"
                show_rclone_config_redacted "$conf_file" "$remote_name" || {
                    echo -e "${RED}❌ 获取远程目标 [$remote_name] 的配置失败。${NC}"
                    return 1
                }
            else
                print_invalid_choice
                return 1
            fi
            ;;
    esac

    return 0
}

prompt_enable_s3_no_check_bucket() {
    local conf_file="$1"
    local remote_name="$2"
    local provider=""
    local existing_value=""
    local enable_choice=""

    if ! rclone_remote_is_s3_compatible "$conf_file" "$remote_name"; then
        return 0
    fi

    existing_value=$(get_rclone_remote_option "$conf_file" "$remote_name" "no_check_bucket")
    if [ "$existing_value" = "true" ]; then
        echo -e "${GREEN}✅ 检测到远程目标 [$remote_name] 已配置 no_check_bucket = true，已跳过重复提示。${NC}"
        return 0
    fi

    provider=$(get_rclone_remote_option "$conf_file" "$remote_name" "provider")
    echo -e "${YELLOW}💡 检测到远程目标 [$remote_name] 使用的是 S3 兼容存储。${NC}"
    echo -e "${YELLOW}👉 这类后端通常要求备份路径写成 bucket/path 的形式，而不是单独的 /path。${NC}"
    if [ "$provider" = "Cloudflare" ]; then
        echo -e "${YELLOW}👉 检测到 provider=Cloudflare。Cloudflare R2 遇到 CreateBucket / AccessDenied 时，常常需要设置 no_check_bucket=true。${NC}"
    else
        echo -e "${YELLOW}👉 如果后续出现 CreateBucket / AccessDenied / bucket check 相关报错，通常可以尝试启用 no_check_bucket=true。${NC}"
    fi
    read -p "是否现在自动为 [$remote_name] 写入 no_check_bucket = true？(y/n, 默认n): " enable_choice
    if [[ "$enable_choice" == [yY]* ]]; then
        if set_rclone_remote_option "$conf_file" "$remote_name" "no_check_bucket" "true"; then
            echo -e "${GREEN}✅ 已为远程目标 [$remote_name] 自动写入 no_check_bucket = true${NC}"
        else
            echo -e "${RED}❌ 写入失败：未能在 rclone.conf 中精确定位远程目标 [$remote_name] 的配置段。${NC}"
            echo -e "${YELLOW}👉 当前不会自动追加新 section，请先确认该 remote 仍然存在，再重试。${NC}"
            return 1
        fi
    else
        echo -e "${YELLOW}💡 已跳过自动写入。如后续遇到 bucket 校验类报错，可再手动补上该项。${NC}"
    fi
}

clear_backup_remote_env() {
    local file="$1"
    local tmp_file

    [ -f "$file" ] || return 0
    tmp_file=$(mktemp "${file}.XXXXXX")
    _register_tmp_file "$tmp_file"
    awk -F '=' '
        $1 !~ /^RCLONE_REMOTE_NAME(_[0-9]+)?$/ && $1 !~ /^RCLONE_REMOTE_DIR(_[0-9]+)?$/ { print }' "$file" > "$tmp_file" && mv "$tmp_file" "$file"
}

list_backup_remote_targets() {
    local file="$_BACKUP_ENV_FILE"
    local idx=0
    local name_key="RCLONE_REMOTE_NAME"
    local dir_key="RCLONE_REMOTE_DIR"
    local remote_name=""
    local remote_dir=""

    [ -f "$file" ] || return 0

    while true; do
        remote_name=$(get_env_var "$file" "$name_key")
        remote_dir=$(get_env_var "$file" "$dir_key")

        if [ -z "$remote_name" ] || [ -z "$remote_dir" ]; then
            break
        fi

        printf '%s:%s\n' "$remote_name" "$remote_dir"

        idx=$((idx + 1))
        name_key="RCLONE_REMOTE_NAME_${idx}"
        dir_key="RCLONE_REMOTE_DIR_${idx}"
    done
}

get_backup_remote_target_dir() {
    local remote_name="$1"
    local target=""

    [ -n "$remote_name" ] || return 0

    while IFS= read -r target; do
        [ -n "$target" ] || continue
        if [ "${target%%:*}" = "$remote_name" ]; then
            printf '%s\n' "${target#*:}"
            return 0
        fi
    done < <(list_backup_remote_targets)

    return 0
}

count_backup_remote_targets() {
    list_backup_remote_targets | awk 'NF { count++ } END { print count + 0 }'
}

show_backup_remote_targets_summary() {
    local target=""
    local count=0

    while IFS= read -r target; do
        [ -n "$target" ] || continue
        if [ "$count" -eq 0 ]; then
            echo -e "${CYAN}远程上传目标:${NC}"
        fi
        echo -e " - ${GREEN}${target}${NC}"
        count=$((count + 1))
    done < <(list_backup_remote_targets)

    if [ "$count" -eq 0 ]; then
        echo -e "${YELLOW}⚠️ 当前还没有记录任何远程上传目标。${NC}"
    fi
}

sync_backup_remote_targets_state() {
    local targets=""

    if [ -f "$_BACKUP_ENV_FILE" ]; then
        targets=$(list_backup_remote_targets | paste -sd '|' -)
    fi

    if [ "$BACKUP_REMOTE_TARGETS" != "$targets" ]; then
        BACKUP_REMOTE_TARGETS="$targets"
        set_conf_var "BACKUP_REMOTE_TARGETS" "\"$BACKUP_REMOTE_TARGETS\""
    fi
}

run_rclone_cli() {
    docker run --rm \
        -v "$_VW_BACKUP_CONFIG_DIR:/config" \
        ttionya/vaultwarden-backup:latest \
        rclone --config /config/rclone/rclone.conf "$@"
}

run_rclone_config_ui() {
    docker run --rm -it \
        -v "$_VW_BACKUP_CONFIG_DIR:/config" \
        ttionya/vaultwarden-backup:latest \
        rclone config --config /config/rclone/rclone.conf
}

run_rclone_config_reconnect_ui() {
    local remote_name="$1"

    [ -n "$remote_name" ] || return 1

    docker run --rm -it \
        -v "$_VW_BACKUP_CONFIG_DIR:/config" \
        ttionya/vaultwarden-backup:latest \
        rclone config reconnect --config /config/rclone/rclone.conf "${remote_name}:"
}

rclone_remote_capability_hint() {
    local remote_type="$1"
    local capability="$2"

    case "$capability" in
        userinfo)
            case "$remote_type" in
                onedrive|s3|webdav|ftp|sftp|local|alias|crypt|union)
                    echo "授权身份通常不支持"
                    ;;
                drive|dropbox|box|pcloud)
                    echo "授权身份可尝试"
                    ;;
                *)
                    echo "授权身份按后端决定"
                    ;;
            esac
            ;;
        about)
            case "$remote_type" in
                drive|dropbox|box|b2|onedrive|pcloud)
                    echo "容量配额可尝试"
                    ;;
                s3|webdav|ftp|sftp|local|alias|crypt|union)
                    echo "容量配额通常不支持"
                    ;;
                *)
                    echo "容量配额按后端决定"
                    ;;
            esac
            ;;
        reconnect)
            case "$remote_type" in
                drive|onedrive|dropbox|box|pcloud)
                    echo "常见支持重新授权"
                    ;;
                s3|webdav|ftp|sftp|local|alias|crypt|union)
                    echo "通常无需重新授权"
                    ;;
                *)
                    echo "重连能力按后端决定"
                    ;;
            esac
            ;;
    esac
}

rclone_output_indicates_unsupported_capability() {
    local capability="$1"
    local output="$2"
    local normalized=""

    normalized=$(printf '%s\n' "$output" \
        | sed 's/\x1b\[[0-9;]*[A-Za-z]//g' \
        | tr '[:upper:]' '[:lower:]')

    case "$capability" in
        userinfo)
            case "$normalized" in
                *"support userinfo"*|*"userinfo is not supported"*|*"userinfo not supported"*)
                    return 0
                    ;;
            esac
            ;;
        about)
            case "$normalized" in
                *"support about"*|*"about is not supported"*|*"about not supported"*)
                    return 0
                    ;;
            esac
            ;;
    esac

    return 1
}

print_rclone_unsupported_capability_message() {
    local capability="$1"
    local remote_name="$2"
    local remote_type="$3"

    case "$capability" in
        userinfo)
            echo -e "${YELLOW}⚠️ 远程目标 [$remote_name] 当前不支持查看授权身份。${NC}"
            if [ -n "$remote_type" ]; then
                echo -e "${YELLOW}👉 检测到该 remote 类型为 [$remote_type]，这个后端通常不会返回 UserInfo。${NC}"
            else
                echo -e "${YELLOW}👉 当前后端通常不会返回 UserInfo。${NC}"
            fi
            echo -e "${YELLOW}👉 您仍可继续使用“查看当前 Rclone 配置（已脱敏）”或“重新授权指定远程目标”来排查。${NC}"
            ;;
        about)
            echo -e "${YELLOW}⚠️ 远程目标 [$remote_name] 当前不支持查看容量/配额。${NC}"
            if [ -n "$remote_type" ]; then
                echo -e "${YELLOW}👉 检测到该 remote 类型为 [$remote_type]，这个后端通常不会返回配额信息。${NC}"
            else
                echo -e "${YELLOW}👉 当前后端通常不会返回配额信息。${NC}"
            fi
            ;;
    esac
}

select_rclone_remote() {
    local conf_file="${1:-$_VW_BACKUP_RCLONE_CONF}"
    local prompt_text="${2:-请选择远程目标编号: }"
    local capability_kind="${3:-}"
    local remotes=()
    local remote_name=""
    local remote_type=""
    local active_targets=()
    local active_target=""
    local active_remote_name=""
    local remote_label=""
    local capability_hint=""
    local choice=""
    local i=""

    [ -f "$conf_file" ] || {
        echo -e "${RED}❌ 未检测到 rclone.conf，请先完成 Rclone 配置。${NC}" >&2
        return 1
    }

    while IFS= read -r remote_name; do
        [ -n "$remote_name" ] || continue
        remotes+=("$remote_name")
    done < <(list_authorized_rclone_remotes "$conf_file")

    if [ ${#remotes[@]} -eq 0 ]; then
        echo -e "${YELLOW}⚠️ 当前还没有可选择的远程目标。${NC}" >&2
        return 1
    fi

    while IFS= read -r active_target; do
        [ -n "$active_target" ] || continue
        active_remote_name="${active_target%%:*}"
        active_targets+=("$active_remote_name")
    done < <(list_backup_remote_targets)

    echo -e "\n${CYAN}可选择的远程目标:${NC}" >&2
    for i in "${!remotes[@]}"; do
        remote_label="${remotes[$i]}"
        if echo " ${active_targets[*]} " | grep -Fq " ${remotes[$i]} "; then
            remote_label="${remote_label}（当前启用）"
        fi
        if [ -n "$capability_kind" ]; then
            remote_type=$(get_rclone_remote_option "$conf_file" "${remotes[$i]}" "type")
            capability_hint=$(rclone_remote_capability_hint "$remote_type" "$capability_kind")
            if [ -n "$capability_hint" ]; then
                remote_label="${remote_label} - ${capability_hint}"
            fi
        fi
        echo " $((i+1))) ${remote_label}" >&2
    done

    while true; do
        read -p "$prompt_text" choice < /dev/tty
        [ -n "$choice" ] || return 1
        if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "${#remotes[@]}" ]; then
            printf '%s\n' "${remotes[$((choice-1))]}"
            return 0
        fi
        echo -e "${RED}❌ 输入无效，请输入上方编号，或直接回车取消。${NC}" >&2
    done
}

show_rclone_remote_userinfo() {
    local conf_file="${1:-$_VW_BACKUP_RCLONE_CONF}"
    local remote_name=""
    local remote_type=""
    local output=""
    local status=0

    remote_name=$(select_rclone_remote "$conf_file" "请选择要查看授权身份的远程目标 [直接回车取消]: " "userinfo") || {
        echo -e "${YELLOW}👉 已取消查看授权身份。${NC}"
        return 1
    }

    remote_type=$(get_rclone_remote_option "$conf_file" "$remote_name" "type")
    echo -e "\n${CYAN}远程目标 [$remote_name] 的授权身份:${NC}"
    output=$(run_rclone_cli config userinfo "${remote_name}:" --json 2>&1) || status=$?
    if rclone_output_indicates_unsupported_capability "userinfo" "$output"; then
        print_rclone_unsupported_capability_message "userinfo" "$remote_name" "$remote_type"
        return 1
    fi
    if [ "$status" -ne 0 ]; then
        echo -e "${RED}❌ 获取远程目标 [$remote_name] 的授权身份失败。${NC}"
        [ -n "$output" ] && echo "$output"
        return 1
    fi

    printf '%s\n' "$output"

    return 0
}

show_rclone_remote_about() {
    local conf_file="${1:-$_VW_BACKUP_RCLONE_CONF}"
    local remote_name=""
    local remote_type=""
    local output=""
    local status=0

    remote_name=$(select_rclone_remote "$conf_file" "请选择要查看容量/配额的远程目标 [直接回车取消]: " "about") || {
        echo -e "${YELLOW}👉 已取消查看容量/配额。${NC}"
        return 1
    }

    remote_type=$(get_rclone_remote_option "$conf_file" "$remote_name" "type")
    echo -e "\n${CYAN}远程目标 [$remote_name] 的容量/配额:${NC}"
    output=$(run_rclone_cli about "${remote_name}:" --json 2>&1) || status=$?
    if rclone_output_indicates_unsupported_capability "about" "$output"; then
        print_rclone_unsupported_capability_message "about" "$remote_name" "$remote_type"
        return 1
    fi
    if [ "$status" -ne 0 ]; then
        echo -e "${RED}❌ 获取远程目标 [$remote_name] 的容量/配额失败。${NC}"
        [ -n "$output" ] && echo "$output"
        return 1
    fi

    printf '%s\n' "$output"

    return 0
}

reconnect_rclone_remote() {
    local conf_file="${1:-$_VW_BACKUP_RCLONE_CONF}"
    local remote_name=""

    remote_name=$(select_rclone_remote "$conf_file" "请选择要重新授权的远程目标 [直接回车取消]: " "reconnect") || {
        echo -e "${YELLOW}👉 已取消重新授权。${NC}"
        return 1
    }

    echo -e "${YELLOW}💡 即将重新授权远程目标 [$remote_name]。${NC}"
    echo -e "${YELLOW}👉 如该后端使用 OAuth，稍后请按照官方提示完成重新登录或粘贴授权结果。${NC}"
    if ! run_rclone_config_reconnect_ui "$remote_name"; then
        echo -e "${RED}❌ 远程目标 [$remote_name] 重新授权失败。${NC}"
        echo -e "${YELLOW}👉 请检查该后端是否支持 reconnect，或稍后进入 Rclone 官方配置界面重试。${NC}"
        return 1
    fi

    echo -e "${GREEN}✅ 远程目标 [$remote_name] 已完成重新授权。${NC}"
    return 0
}

validate_backup_remote_target_path() {
    local remote_name="$1"
    local target_dir="$2"
    validate_backup_remote_targets_batch "${remote_name}:${target_dir}"
}

validate_backup_remote_targets_batch() {
    local target=""
    local remote_name=""
    local target_dir=""
    local output=""
    local status=0
    local docker_args=()

    [ "$#" -gt 0 ] || return 0

    for target in "$@"; do
        remote_name="${target%%:*}"
        target_dir="${target#*:}"
        docker_args+=("$remote_name" "$target_dir")
    done

    output=$(docker run --rm \
        -v "$_VW_BACKUP_CONFIG_DIR:/config" \
        --entrypoint sh \
        ttionya/vaultwarden-backup:latest \
        -ec '
            while [ "$#" -gt 0 ]; do
                remote_name="$1"
                target_dir="$2"
                shift 2

                echo "[check] ${remote_name}:${target_dir}"
                rclone mkdir --config /config/rclone/rclone.conf "${remote_name}:${target_dir}"
                rclone lsd --config /config/rclone/rclone.conf "${remote_name}:${target_dir}" >/dev/null
            done
        ' sh "${docker_args[@]}" 2>&1) || status=$?

    if [ "$status" -ne 0 ]; then
        echo -e "${RED}❌ 远程目标预检查失败。${NC}"
        [ -n "$output" ] && echo "$output"
        if printf '%s\n' "$output" | grep -Eiq 'CreateBucket|bucket|ListObjectsV2|AccessDenied'; then
            echo -e "${YELLOW}💡 检测到这像是 S3 兼容存储的 bucket 校验类报错。${NC}"
            echo -e "${YELLOW}👉 请优先确认远程路径是否写成 bucket/path。${NC}"
            echo -e "${YELLOW}👉 如果是 Cloudflare R2 或其他受限 S3 兼容后端，可在对应 remote 下启用:${NC}"
            echo -e "${GREEN}   no_check_bucket = true${NC}"
        fi
        return 1
    fi

    return 0
}

show_rclone_config_entry_menu() {
    local authorized_count=0

    authorized_count=$(list_authorized_rclone_remotes | awk 'NF { count++ } END { print count + 0 }')

    echo ""
    _breadcrumb "主菜单 > Rclone 配置"
    echo -e "${CYAN}Rclone 配置入口:${NC}"
    echo " 1) 管理远程节点"
    if [ "$authorized_count" -gt 0 ]; then
        echo " 2) 选择当前启用的上传目标"
    fi
    echo " 3) 查看当前 Rclone 配置（已脱敏）"
    if [ "$authorized_count" -gt 0 ]; then
        echo " 4) 查看远程目标能力信息"
    fi
    echo -e "${YELLOW} q) 返回主菜单${NC}"
}

show_rclone_remote_management_menu() {
    echo ""
    _breadcrumb "主菜单 > Rclone 配置 > 远程节点管理"
    echo -e "${CYAN}远程节点管理:${NC}"
    echo " 1) 官方交互式管理（新增/编辑/删除）"
    echo " 2) 手动填写参数并生成/更新远程节点"
    echo " 3) 重新授权指定远程目标（按后端决定）"
    echo -e "${YELLOW} q) 返回上一级${NC}"
}

show_rclone_capability_menu() {
    echo ""
    _breadcrumb "主菜单 > Rclone 配置 > 远程目标能力"
    echo -e "${CYAN}远程目标能力信息:${NC}"
    echo " 1) 查看授权身份（部分后端支持）"
    echo " 2) 查看容量/配额（部分后端支持）"
    echo -e "${YELLOW} q) 返回上一级${NC}"
}

configure_backup_remote_targets() {
    local conf_file="$_VW_BACKUP_RCLONE_CONF"
    local remotes=()
    local selected_indexes=()
    local selected_targets=()
    local remote_choice=""
    local target_dir=""
    local i=""
    local idx=""
    local remote_name=""
    local pair=""
    local invalid_choice=""
    local persisted_count=0
    local desired_targets=""
    local validate_remote_choice=""
    local previous_target_dir=""

    [ -f "$conf_file" ] || {
        echo -e "${RED}❌ 未检测到 rclone.conf，请先完成 Rclone 配置。${NC}"
        return 1
    }

    while IFS= read -r remote_name; do
        [ -n "$remote_name" ] || continue
        remotes+=("$remote_name")
    done < <(list_authorized_rclone_remotes "$conf_file")

    if [ ${#remotes[@]} -eq 0 ]; then
        echo -e "${RED}❌ 当前未检测到任何可用的 Rclone 远程节点。${NC}"
        return 1
    fi

    echo -e "\n${CYAN}已检测到以下可用远程目标:${NC}"
    for i in "${!remotes[@]}"; do
        echo " $((i+1))) ${remotes[$i]}"
    done

    if [ -n "$BACKUP_REMOTE_TARGETS" ]; then
        echo -e "${YELLOW}💡 当前已启用的远程目标如下：${NC}"
        show_backup_remote_targets_summary
    fi

    while true; do
        read -p "请选择要启用的远程目标编号，可多选 (例: 1,2 或 1 2，直接回车取消): " remote_choice
        [ -n "$remote_choice" ] || return 2

        remote_choice=$(printf '%s' "$remote_choice" | tr ',' ' ')
        selected_indexes=()
        invalid_choice="false"

        for idx in $remote_choice; do
            if [[ ! "$idx" =~ ^[0-9]+$ ]] || [ "$idx" -lt 1 ] || [ "$idx" -gt "${#remotes[@]}" ]; then
                invalid_choice="true"
                break
            fi
            if echo " ${selected_indexes[*]} " | grep -Fq " $idx "; then
                invalid_choice="true"
                break
            fi
            selected_indexes+=("$idx")
        done

        if [ "$invalid_choice" = "false" ] && [ ${#selected_indexes[@]} -gt 0 ]; then
            break
        fi

        echo -e "${RED}❌ 选择无效，请输入有效编号，并用逗号或空格分隔多个目标。${NC}"
    done

    selected_targets=()
    for idx in "${selected_indexes[@]}"; do
        remote_name="${remotes[$((idx-1))]}"
        previous_target_dir=$(get_backup_remote_target_dir "$remote_name")
        if [ -n "$previous_target_dir" ]; then
            read -p "远程目标 [$remote_name] 的备份目录 [回车用默认 /VaultBackup，输入 l 沿用上次: $previous_target_dir]: " target_dir
            case "$target_dir" in
                l|L)
                    target_dir="$previous_target_dir"
                    ;;
                "")
                    target_dir="/VaultBackup"
                    ;;
            esac
        else
            read -p "远程目标 [$remote_name] 的备份目录 [默认 /VaultBackup]: " target_dir
            target_dir=${target_dir:-/VaultBackup}
        fi
        if ! prompt_enable_s3_no_check_bucket "$conf_file" "$remote_name"; then
            echo -e "${YELLOW}👉 已取消本次远程目标更新，请先确认 rclone.conf 配置后再重试。${NC}"
            return 1
        fi
        selected_targets+=("${remote_name}:${target_dir}")
    done

    desired_targets=$(printf '%s\n' "${selected_targets[@]}" | paste -sd '|' -)
    if [ "$desired_targets" = "$BACKUP_REMOTE_TARGETS" ]; then
        echo -e "${YELLOW}💡 当前启用的远程目标没有变化，已跳过重复校验。${NC}"
        show_backup_remote_targets_summary
        return 0
    fi

    echo -e "${YELLOW}💡 远程目标预检查会逐个测试可写性，速度较慢，并且可能会在网盘中预先创建空目录。${NC}"
    read -p "是否现在执行远程目标预检查？(y/n, 默认n): " validate_remote_choice
    if [[ "$validate_remote_choice" == [yY]* ]]; then
        echo -e "${YELLOW}正在批量校验 ${#selected_targets[@]} 个远程目标，请稍候...${NC}"
        if ! validate_backup_remote_targets_batch "${selected_targets[@]}"; then
            echo -e "${YELLOW}👉 已取消本次远程目标更新，请先修正上面的远端路径问题后重试。${NC}"
            return 1
        fi
    else
        echo -e "${YELLOW}💡 已跳过远程目标预检查。本次不会额外在网盘中创建校验目录；首次实际备份时会再验证上传是否成功。${NC}"
    fi

    clear_backup_remote_env "$_BACKUP_ENV_FILE"

    for i in "${!selected_targets[@]}"; do
        pair="${selected_targets[$i]}"
        remote_name="${pair%%:*}"
        target_dir="${pair#*:}"

        if [ "$i" -eq 0 ]; then
            set_env_var "$_BACKUP_ENV_FILE" "RCLONE_REMOTE_NAME" "$remote_name"
            set_env_var "$_BACKUP_ENV_FILE" "RCLONE_REMOTE_DIR" "$target_dir"
        else
            set_env_var "$_BACKUP_ENV_FILE" "RCLONE_REMOTE_NAME_${i}" "$remote_name"
            set_env_var "$_BACKUP_ENV_FILE" "RCLONE_REMOTE_DIR_${i}" "$target_dir"
        fi
    done

    persisted_count=$(count_backup_remote_targets)
    if [ "$persisted_count" -ne "${#selected_targets[@]}" ]; then
        echo -e "${RED}❌ 远程目标写入校验失败：预期写入 ${#selected_targets[@]} 个，实际仅检测到 ${persisted_count} 个。${NC}"
        echo -e "${YELLOW}👉 当前不会继续沿用这次结果，请检查 backup.env 写入情况。${NC}"
        return 1
    fi

    BACKUP_REMOTE_TARGETS="$desired_targets"
    set_conf_var "BACKUP_REMOTE_TARGETS" "\"$BACKUP_REMOTE_TARGETS\""
    BACKUP_MODE="remote"
    set_conf_var "BACKUP_MODE" "\"$BACKUP_MODE\""

    echo -e "${GREEN}✅ 远程上传目标已更新，共启用 ${#selected_targets[@]} 个目标。${NC}"
    show_backup_remote_targets_summary
    return 0
}

run_official_restore_flow() {
    local selected_zip=""
    local selected_name=""
    local restore_confirm=""

    if [ -z "$VW_DIR" ] || [ ! -d "$_VW_APP_DIR" ]; then
        echo -e "${RED}❌ 未找到 Vaultwarden 主服务目录，无法执行官方高级恢复。${NC}"
        return 1
    fi

    if ! selected_zip=$(select_local_backup_zip); then
        echo -e "${YELLOW}👉 已取消官方高级恢复。${NC}"
        return 1
    fi

    selected_name=$(basename "$selected_zip")
    echo -e "${YELLOW}⚠️ 官方高级恢复将直接调用 vaultwarden-backup 镜像内建 restore 命令。${NC}"
    echo -e "${YELLOW}⚠️ 该流程会直接覆盖当前数据目录内容，适合排障或需要严格跟随上游恢复逻辑时使用。${NC}"
    read -p "是否继续恢复备份包 [$selected_name]？(y/n, 默认n): " restore_confirm
    if [[ "$restore_confirm" != [yY]* ]]; then
        echo -e "${YELLOW}👉 已取消官方高级恢复。${NC}"
        return 1
    fi

    echo -e "${YELLOW}正在停止 Vaultwarden 主服务...${NC}"
    compose_stop_service_dir "$_VW_APP_DIR" "$VW_CONTAINER_NAME"

    echo -e "${YELLOW}正在调用官方 restore 引擎，请根据提示输入备份密码...${NC}"
    if ! docker run --rm -it \
        --mount type=bind,source="$_VW_DATA_DIR",target=/bitwarden/data/ \
        --mount type=bind,source="$_VW_BACKUP_ARCHIVES_DIR",target=/bitwarden/restore/ \
        ttionya/vaultwarden-backup:latest restore \
        --zip-file "$selected_name"; then
        echo -e "${RED}❌ 官方高级恢复执行失败，请检查备份包、密码和日志输出。${NC}"
        return 1
    fi

    echo -e "${YELLOW}正在尝试重新启动 Vaultwarden...${NC}"
    compose_start_service_dir "$_VW_APP_DIR" "$VW_CONTAINER_NAME"
    probe_vaultwarden_http || restart_vaultwarden_service_apply >/dev/null 2>&1 || true

    if probe_vaultwarden_http; then
        echo -e "${GREEN}✅ 官方高级恢复已完成，Vaultwarden 已重新拉起并通过探活。${NC}"
    else
        echo -e "${YELLOW}⚠️ 官方高级恢复已完成，但服务尚未通过探活，建议立即查看日志确认状态。${NC}"
    fi

    return 0
}

cleanup_generated_files() {
    rm -f "$CONF_FILE" /usr/local/bin/vw 2>/dev/null || true
    if [ -n "$VW_DIR" ]; then
        rm -f \
            "$_VW_COMPOSE_FILE" \
            "$_VW_BACKUP_COMPOSE_FILE" \
            "$_VW_ENV_FILE" \
            "$_BACKUP_ENV_FILE" \
            2>/dev/null || true
    fi
    VW_DIR=""
    DOCKER_NET=""
    DOCKER_NET_OWNED=""
    CADDY_DIR=""
    CADDY_EXISTS=""
    EXISTING_CADDY_ID=""
    PROXY_MODE=""
    SKIP_CADDY=""
    EXT_PORT=""
    UNZIP_INSTALLED_BY_SCRIPT=""
    BACKUP_MODE="none"
    BACKUP_REMOTE_TARGETS=""
}

prompt_remove_empty_parent_dir() {
    local removed_root="$1"
    local parent_dir=""
    local grandparent_dir=""
    local remove_parent=""

    [ -n "$removed_root" ] || return 0

    parent_dir=$(dirname "$removed_root")
    grandparent_dir=$(dirname "$parent_dir")

    [ -n "$parent_dir" ] || return 0
    [ "$parent_dir" != "/" ] || return 0
    [ "$grandparent_dir" != "/" ] || return 0
    [ -d "$parent_dir" ] || return 0
    [ -z "$(find "$parent_dir" -mindepth 1 -maxdepth 1 2>/dev/null)" ] || return 0

    echo -e "${YELLOW}💡 检测到上级目录 [$parent_dir] 当前也已经为空。${NC}"
    read -p "是否继续删除这个空目录？(y/n, 默认n): " remove_parent
    if [[ "$remove_parent" == [yY]* ]]; then
        rmdir "$parent_dir" 2>/dev/null || true
        if [ ! -d "$parent_dir" ]; then
            echo -e "${GREEN}✅ 空上级目录已删除: $parent_dir${NC}"
        else
            echo -e "${YELLOW}⚠️ 已尝试删除 [$parent_dir]，但当前未成功，可能目录已被其他文件重新占用。${NC}"
        fi
    else
        echo -e "${YELLOW}💡 已保留空上级目录: $parent_dir${NC}"
    fi
}

extract_domain_host() {
    local raw="$1"
    raw="${raw#http://}"
    raw="${raw#https://}"
    raw="${raw%%/*}"
    echo "$raw"
}

set_env_var() {
    _upsert_file_var "$1" "$2" "$3"
}

# 公网 IP 缓存机制，减少重复网络请求
_CACHED_PUBLIC_IPV4=""
_CACHED_IPV4_TS=0

get_public_ipv4() {
    local now
    now=$(date +%s)
    if [ -n "$_CACHED_PUBLIC_IPV4" ] && [ $((now - _CACHED_IPV4_TS)) -lt 300 ]; then
        echo "$_CACHED_PUBLIC_IPV4"
        return
    fi
    local ip=""
    if command -v curl >/dev/null 2>&1; then
        ip=$(curl -4 -fsSL --max-time 5 https://api.ipify.org 2>/dev/null || true)
        [ -n "$ip" ] || ip=$(curl -4 -fsSL --max-time 5 https://ifconfig.me 2>/dev/null || true)
    fi
    if [ -z "$ip" ] && command -v wget >/dev/null 2>&1; then
        ip=$(wget -4 -qO- --timeout=5 https://api.ipify.org 2>/dev/null || true)
        [ -n "$ip" ] || ip=$(wget -4 -qO- --timeout=5 https://ifconfig.me 2>/dev/null || true)
    fi
    _CACHED_PUBLIC_IPV4="$ip"
    _CACHED_IPV4_TS="$now"
    echo "$ip"
}

resolve_domain_ipv4() {
    local host="$1"
    if command -v dig >/dev/null 2>&1; then
        dig +short A "$host" 2>/dev/null | awk 'NF { print; exit }'
    elif command -v getent >/dev/null 2>&1; then
        getent ahostsv4 "$host" 2>/dev/null | awk 'NF { print $1; exit }'
    elif command -v host >/dev/null 2>&1; then
        host "$host" 2>/dev/null | awk '/has address/ { print $4; exit }'
    fi
}

validate_domain_input() {
    local domain="$1"
    domain=$(extract_domain_host "$domain")
    [[ "$domain" =~ ^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]]
}

validate_positive_integer() {
    local val="$1"
    [[ "$val" =~ ^[1-9][0-9]*$ ]]
}

validate_non_negative_integer() {
    local val="$1"
    [[ "$val" =~ ^[0-9]+$ ]]
}

validate_timezone_input() {
    local tz="$1"
    [[ "$tz" =~ ^[A-Za-z0-9._+-]+(/[A-Za-z0-9._+-]+)+$ ]]
}

validate_docker_network_name() {
    local name="$1"
    [[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]
}

validate_cron_expression() {
    local expr="$1"
    [[ "$expr" =~ ^[0-9*/,-]+[[:space:]][0-9*/,-]+[[:space:]][0-9*/,-]+[[:space:]][0-9*/,-]+[[:space:]][0-9*/,-]+$ ]]
}

check_disk_space() {
    local target_dir="$1"
    local min_mb="${2:-500}"
    local avail_mb=""
    [ -d "$target_dir" ] || target_dir=$(dirname "$target_dir")
    avail_mb=$(df -m "$target_dir" 2>/dev/null | awk 'NR==2 {print $4}')
    if [ -n "$avail_mb" ] && [ "$avail_mb" -lt "$min_mb" ] 2>/dev/null; then
        echo -e "${YELLOW}⚠️ 磁盘空间预检提示：当前可用 ${avail_mb}MB，建议至少保留 ${min_mb}MB。${NC}"
        echo -e "${YELLOW}👉 空间不足可能导致操作失败或数据损坏，请注意清理。${NC}"
    fi
    return 0
}

check_domain_dns_status() {
    local raw_domain="$1"
    local domain=""
    local resolved_ip=""
    local public_ip=""

    domain=$(extract_domain_host "$raw_domain")
    [ -n "$domain" ] || return 2

    resolved_ip=$(resolve_domain_ipv4 "$domain")
    public_ip=$(get_public_ipv4)

    if [ -z "$resolved_ip" ]; then
        echo -e "${YELLOW}⚠️ DNS 检查：暂时还无法解析域名 [$domain]。${NC}"
        echo -e "${YELLOW}👉 请先确认该域名的 A 记录已经添加，并且 DNS 解析已经生效。${NC}"
        return 2
    fi

    if [ -z "$public_ip" ]; then
        echo -e "${YELLOW}⚠️ DNS 检查：域名 [$domain] 当前解析到 [$resolved_ip]。${NC}"
        echo -e "${YELLOW}👉 但脚本暂时无法识别本机公网 IP，请您手动确认该解析是否指向当前服务器。${NC}"
        return 2
    fi

    if [ "$resolved_ip" = "$public_ip" ]; then
        echo -e "${GREEN}✅ DNS 检查通过：[$domain] 当前解析 [$resolved_ip] 与本机公网 IP 一致。${NC}"
        return 0
    fi

    echo -e "${RED}⚠️ DNS 检查未通过：[$domain] 当前解析到 [$resolved_ip]，但本机公网 IP 是 [$public_ip]。${NC}"
    echo -e "${YELLOW}👉 这说明域名目前还没有指向这台服务器。${NC}"
    echo -e "${YELLOW}👉 如果您接下来要申请 SSL 或通过外部反代访问，请先把域名解析修正到当前服务器。${NC}"
    return 1
}

confirm_domain_dns_or_abort() {
    local raw_domain="$1"
    local dns_status=0
    local continue_anyway=""

    check_domain_dns_status "$raw_domain"
    dns_status=$?

    case "$dns_status" in
        0)
            return 0
            ;;
        1)
            echo -e "${RED}❌ 当前域名还没有正确指向这台服务器。${NC}"
            echo -e "${YELLOW}👉 继续部署虽然不会影响配置文件生成，但后续访问、外部反代和 SSL 申请大概率会出问题。${NC}"
            read -p "是否确认忽略 DNS 异常并继续部署？(y/n, 默认n): " continue_anyway
            [[ "$continue_anyway" == [yY]* ]]
            return
            ;;
        *)
            echo -e "${YELLOW}⚠️ 当前还无法完整确认域名是否已经正确指向这台服务器。${NC}"
            echo -e "${YELLOW}👉 如果您现在继续，后续访问和 SSL 申请仍然可能失败。${NC}"
            read -p "是否在接受该风险的前提下继续部署？(y/n, 默认n): " continue_anyway
            [[ "$continue_anyway" == [yY]* ]]
            return
            ;;
    esac
}

get_env_var() {
    local file="$1"
    local key="$2"
    # Form-managed dotenv values use literal quoting. Decode without executing them.
    local reader="$(dirname -- "${BASH_SOURCE[0]}")/web/manager.py"
    if [ -f "$reader" ]; then
        python3 "$reader" "$file" "$key"
        return $?
    fi
    if [ -f "$file" ]; then
        awk -F '=' -v k="$key" '$1 == k { sub(/^[^=]*=/, "", $0); print $0; exit }' "$file"
    fi
}

delete_env_var() {
    local file="$1"
    local key="$2"
    local tmp_file
    [ -f "$file" ] || return 0
    tmp_file=$(mktemp "${file}.XXXXXX")
    _register_tmp_file "$tmp_file"
    awk -F '=' -v k="$key" '$1 != k { print }' "$file" > "$tmp_file" && mv "$tmp_file" "$file"
}

find_caddy_container_id() {
    local _cid=""

    # 快速路径: 优先从统一缓存中按名称/镜像匹配 (零 Docker 调用)
    if [ $(( SECONDS - _DOCKER_CACHE_SEC )) -lt 5 ]; then
        _cid=$(printf '%s\n' "$_DOCKER_CACHE" | awk -F '\t' '
            $2 == "caddy" { print $1; exit }
            $3 ~ /(^|\/)caddy([:@]|$)/ { print $1; exit }')
        if [ -n "$_cid" ]; then echo "$_cid"; return 0; fi
    else
        _cid=$(docker ps -a --format '{{.ID}}\t{{.Image}}\t{{.Names}}' 2>/dev/null | awk -F '\t' '
            $3 == "caddy" { print $1; exit }
            $2 ~ /(^|\/)caddy([:@]|$)/ { print $1; exit }')
        if [ -n "$_cid" ]; then echo "$_cid"; return 0; fi
    fi

    # 慢速路径: 名称/镜像都匹配不到时，逐个容器检查 Caddyfile 挂载点
    if [ -n "$CADDY_DIR" ]; then
        local matched_id=""
        matched_id=$(docker ps -a --format '{{.ID}}' 2>/dev/null | while read -r cid; do
            [ -n "$cid" ] || continue
            docker inspect -f '{{range .Mounts}}{{if eq .Destination "/etc/caddy/Caddyfile"}}{{.Source}}{{end}}{{end}}' "$cid" 2>/dev/null
        done | awk -v dir="$CADDY_DIR" 'index($0, dir "/") == 1 || $0 == dir "/Caddyfile" { print NR }' | head -n 1)
        if [ -n "$matched_id" ]; then
            docker ps -a --format '{{.ID}}' 2>/dev/null | sed -n "${matched_id}p"
            return 0
        fi
    fi
}

load_caddy_runtime() {
    if [ -n "$EXISTING_CADDY_ID" ]; then
        # 优先用缓存验证容器是否存在，避免 docker inspect 调用
        if [ $(( SECONDS - _DOCKER_CACHE_SEC )) -lt 5 ]; then
            if ! printf '%s\n' "$_DOCKER_CACHE" | grep -q "^${EXISTING_CADDY_ID}"; then
                EXISTING_CADDY_ID=""
            fi
        elif ! docker inspect "$EXISTING_CADDY_ID" >/dev/null 2>&1; then
            EXISTING_CADDY_ID=""
        fi
    fi

    if [ -z "$EXISTING_CADDY_ID" ]; then
        EXISTING_CADDY_ID=$(find_caddy_container_id)
    fi

    if [ -n "$EXISTING_CADDY_ID" ] && [ -z "$CADDY_DIR" ]; then
        local caddy_file_src=""
        caddy_file_src=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/etc/caddy/Caddyfile"}}{{.Source}}{{end}}{{end}}' "$EXISTING_CADDY_ID" 2>/dev/null || true)
        if [ -n "$caddy_file_src" ]; then
            CADDY_DIR=$(dirname "$caddy_file_src")
        fi
    fi
}

vw_caddy_block_begin() {
    local host="$1"
    echo "# BEGIN VW_MANAGED $host"
}

vw_caddy_block_end() {
    local host="$1"
    echo "# END VW_MANAGED $host"
}

remove_caddy_managed_block() {
    local host="$1"
    local caddy_file="$CADDY_DIR/Caddyfile"
    local tmp_file
    [ -f "$caddy_file" ] || return 0
    tmp_file=$(mktemp "${caddy_file}.XXXXXX")
    _register_tmp_file "$tmp_file"
    awk -v begin="$(vw_caddy_block_begin "$host")" -v end="$(vw_caddy_block_end "$host")" '
        $0 == begin { skip=1; next }
        $0 == end { skip=0; next }
        skip != 1 { print }' "$caddy_file" > "$tmp_file" && mv "$tmp_file" "$caddy_file"
}

write_caddy_managed_block() {
    local host="$1"
    local caddy_file="$CADDY_DIR/Caddyfile"
    mkdir -p "$CADDY_DIR"
    touch "$caddy_file"
    remove_caddy_managed_block "$host"
    cat <<EOF >> "$caddy_file"
$(vw_caddy_block_begin "$host")
$host {
    reverse_proxy vaultwarden:80
}
$(vw_caddy_block_end "$host")

EOF
}

count_caddy_managed_blocks() {
    local caddy_file="$CADDY_DIR/Caddyfile"
    [ -f "$caddy_file" ] || { echo 0; return 0; }
    grep -c '^# BEGIN VW_MANAGED ' "$caddy_file" 2>/dev/null || true
}

reload_caddy_runtime() {
    load_caddy_runtime
    if [ -n "$EXISTING_CADDY_ID" ]; then
        docker exec "$EXISTING_CADDY_ID" caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1
    else
        return 1
    fi
}

filter_selectable_networks() {
    docker network ls --format '{{.Name}}' 2>/dev/null | awk '
        $0 != "bridge" && $0 != "host" && $0 != "none" && $0 != "ingress" { print }'
}

# 统一 Docker 容器缓存: 一次 docker ps -a 同时服务于 service_running + find_caddy_container_id
# 格式: ID\tNames\tImage\tState  (State: running/exited/created/paused/...)
_DOCKER_CACHE=""
_DOCKER_CACHE_SEC=-10

_refresh_docker_cache() {
    _DOCKER_CACHE=$(docker ps -a --format '{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.State}}' 2>/dev/null || true)
    _DOCKER_CACHE_SEC=$SECONDS
}

service_running() {
    local name="$1"
    if [ $(( SECONDS - _DOCKER_CACHE_SEC )) -ge 5 ]; then
        _refresh_docker_cache
    fi
    printf '%s\n' "$_DOCKER_CACHE" | awk -F '\t' -v n="$name" '$2 == n && $4 == "running" { found=1; exit } END { exit !found }'
}

http_probe_url() {
    local url="$1"
    if command -v curl >/dev/null 2>&1; then
        curl -fsS --max-time 5 "$url" >/dev/null 2>&1 && return 0
    fi
    if command -v wget >/dev/null 2>&1; then
        wget -qO- --timeout=5 "$url" >/dev/null 2>&1 && return 0
    fi
    return 1
}

get_container_ipv4() {
    local name="$1"
    docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$name" 2>/dev/null | awk 'NF { print $1; exit }'
}

probe_vaultwarden_http() {
    local probe_ip=""

    if ! service_running "$VW_CONTAINER_NAME"; then
        return 1
    fi

    if [ "$PROXY_MODE" == "host_ext" ] && [ -n "$EXT_PORT" ] && [ "$EXT_PORT" != "none" ]; then
        http_probe_url "http://127.0.0.1:$EXT_PORT/alive" && return 0
        http_probe_url "http://127.0.0.1:$EXT_PORT/" && return 0
    fi

    probe_ip=$(get_container_ipv4 "vaultwarden")
    if [ -n "$probe_ip" ]; then
        http_probe_url "http://$probe_ip:80/alive" && return 0
        http_probe_url "http://$probe_ip:80/" && return 0
    fi

    return 1
}

latest_backup_zip() {
    [ -n "$VW_DIR" ] || return 0
    [ -d "$_VW_BACKUP_ARCHIVES_DIR" ] || return 0
    find "$_VW_BACKUP_ARCHIVES_DIR" -maxdepth 1 -type f -name '*.zip' -print 2>/dev/null | sort | tail -n 1
}

show_backup_remote_delivery_status() {
    local latest_zip_path="$1"
    local latest_zip_name=""
    local target=""
    local remote_name=""
    local target_dir=""
    local remote_spec=""
    local output=""
    local status=0
    local zip_entries=""
    local zip_count=0

    if ! backup_remote_configured; then
        return 0
    fi

    if [ -z "$latest_zip_path" ] || [ ! -f "$latest_zip_path" ]; then
        echo -e "${YELLOW}⚠️ 远端验收已跳过：当前还没有检测到本地最新备份文件。${NC}"
        return 1
    fi

    latest_zip_name=$(basename "$latest_zip_path")
    echo -e "${CYAN}☁️ 远端上传验收结果${NC}"

    while IFS= read -r target; do
        [ -n "$target" ] || continue
        remote_name="${target%%:*}"
        target_dir="${target#*:}"
        remote_spec="${remote_name}:${target_dir}"
        status=0

        output=$(docker run --rm \
            -v "$_VW_BACKUP_CONFIG_DIR:/config" \
            --entrypoint sh \
            ttionya/vaultwarden-backup:latest \
            -ec 'rclone lsf --files-only --config /config/rclone/rclone.conf "$1"' \
            sh "$remote_spec" 2>&1) || status=$?

        if [ "$status" -ne 0 ]; then
            echo -e "${RED}❌ [${remote_spec}] 读取失败，暂时无法确认上传结果。${NC}"
            [ -n "$output" ] && echo "$output"
            continue
        fi

        if printf '%s\n' "$output" | grep -Fxq "$latest_zip_name"; then
            echo -e "${GREEN}✅ [${remote_spec}] 已检测到最新备份文件: ${latest_zip_name}${NC}"
            continue
        fi

        zip_entries=$(printf '%s\n' "$output" | grep -E '\.zip$' || true)
        if [ -n "$zip_entries" ]; then
            zip_count=$(printf '%s\n' "$zip_entries" | awk 'NF { count++ } END { print count + 0 }')
            echo -e "${YELLOW}⚠️ [${remote_spec}] 目录可访问，但未发现本次最新备份 [${latest_zip_name}]。${NC}"
            echo -e "${YELLOW}💡 当前目录中检测到 ${zip_count} 个其他 zip 文件。${NC}"
        else
            echo -e "${YELLOW}⚠️ [${remote_spec}] 目录可访问，但当前未发现任何 zip 文件。${NC}"
        fi
    done < <(list_backup_remote_targets)

    return 0
}

list_backup_archive_entries() {
    local archive="$1"
    if [ -z "$archive" ] || [ ! -f "$archive" ]; then
        return 1
    fi
    if command -v unzip >/dev/null 2>&1; then
        unzip -Z1 "$archive" 2>/dev/null
        return $?
    fi
    if command -v zipinfo >/dev/null 2>&1; then
        zipinfo -1 "$archive" 2>/dev/null
        return $?
    fi
    return 1
}

list_local_backup_zips() {
    [ -n "$VW_DIR" ] || return 0
    [ -d "$_VW_BACKUP_ARCHIVES_DIR" ] || return 0
    find "$_VW_BACKUP_ARCHIVES_DIR" -maxdepth 1 -type f -name '*.zip' -print 2>/dev/null | sort -r
}

format_backup_zip_label() {
    local archive="$1"
    local file_name=""
    local file_size=""
    local file_mtime=""

    file_name=$(basename "$archive")
    file_size=$(_file_size "$archive")
    file_mtime=$(_file_mtime "$archive")

    echo "${file_name} | ${file_mtime} | ${file_size}"
}

select_local_backup_zip() {
    local backups=()
    local choice=""
    mapfile -t backups < <(list_local_backup_zips)

    if [ ${#backups[@]} -eq 0 ]; then
        echo -e "${YELLOW}⚠️ 本地备份目录中未发现可用的 .zip 备份文件。${NC}" >&2
        return 1
    fi

    echo -e "\n${CYAN}可用的本地备份包:${NC}" >&2
    for i in "${!backups[@]}"; do
        echo " $((i+1))) $(format_backup_zip_label "${backups[$i]}")" >&2
    done

    while true; do
        read -p "请选择要恢复的备份编号 [1-${#backups[@]}，直接回车取消]: " choice < /dev/tty
        if [ -z "$choice" ]; then
            return 1
        fi
        if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "${#backups[@]}" ]; then
            echo "${backups[$((choice-1))]}"
            return 0
        fi
        echo -e "${RED}❌ 输入无效，请重新选择。${NC}" >&2
    done
}

restore_from_local_backup() {
    local selected_zip=""
    local restore_tmp=""
    local restore_root=""
    local backup_password=""
    local backup_data_dir=""
    local restore_ts=""

    if [ -z "$VW_DIR" ] || [ ! -d "$_VW_APP_DIR" ]; then
        echo -e "${RED}❌ 未找到 Vaultwarden 主服务目录，无法执行本地恢复。${NC}"
        return 1
    fi

    check_disk_space "$_VW_APP_DIR" 500

    if ! ensure_unzip_available; then
        return 1
    fi

    if ! selected_zip=$(select_local_backup_zip); then
        echo -e "${YELLOW}👉 已取消自动恢复。${NC}"
        return 1
    fi
    if [ -z "$selected_zip" ]; then
        echo -e "${YELLOW}👉 已取消自动恢复。${NC}"
        return 1
    fi

    read -s -p "请输入该备份包的解压密码 (直接回车取消): " backup_password ; echo ""
    if [ -z "$backup_password" ]; then
        echo -e "${YELLOW}👉 已取消自动恢复。${NC}"
        return 1
    fi

    restore_ts=$(date +%Y%m%d_%H%M%S)
    restore_tmp="$VW_DIR/.restore_tmp_$restore_ts"
    mkdir -p "$restore_tmp"
    _register_tmp_dir "$restore_tmp"

    if ! _unzip_with_password "$backup_password" "$selected_zip" "$restore_tmp"; then
        rm -rf "$restore_tmp" 2>/dev/null || true
        echo -e "${RED}❌ 备份包解压失败，请检查压缩包密码是否正确。${NC}"
        return 1
    fi

    restore_root="$restore_tmp"
    if [ "$(find "$restore_tmp" -mindepth 1 -maxdepth 1 | wc -l | tr -d ' ')" -eq 1 ]; then
        local only_entry=""
        only_entry=$(find "$restore_tmp" -mindepth 1 -maxdepth 1 | head -n 1)
        if [ -d "$only_entry" ]; then
            restore_root="$only_entry"
        fi
    fi

    _normalize_restore_payload "$restore_root"

    if ! find "$restore_root" -maxdepth 1 -type f \( -name 'db.sqlite3' -o -name 'db.*.sqlite3' \) | grep -q .; then
        rm -rf "$restore_tmp" 2>/dev/null || true
        echo -e "${RED}❌ 解压后未发现数据库主文件，已取消自动恢复。${NC}"
        return 1
    fi

    echo -e "${YELLOW}⚠️ 即将停止 Vaultwarden，并用 [$(basename "$selected_zip")] 覆盖恢复数据。${NC}"
    read -p "是否继续自动恢复？(y/n, 默认n): " restore_confirm
    if [[ "$restore_confirm" != [yY]* ]]; then
        rm -rf "$restore_tmp" 2>/dev/null || true
        echo -e "${YELLOW}👉 已取消自动恢复。${NC}"
        return 1
    fi

    compose_stop_service_dir "$_VW_APP_DIR" "$VW_CONTAINER_NAME"

    if [ -d "$_VW_DATA_DIR" ]; then
        backup_data_dir="$_VW_APP_DIR/data.bak_$restore_ts"
        mv "$_VW_DATA_DIR" "$backup_data_dir"
        echo -e "${GREEN}✅ 旧 data 目录已备份到: ${backup_data_dir}${NC}"
    fi

    mkdir -p "$_VW_DATA_DIR"
    cp -a "$restore_root"/. "$_VW_DATA_DIR"/
    rm -rf "$restore_tmp" 2>/dev/null || true

    echo -e "${GREEN}✅ 备份文件已解压并覆盖到数据目录。${NC}"
    echo -e "${YELLOW}正在尝试重新启动 Vaultwarden...${NC}"
    compose_start_service_dir "$_VW_APP_DIR" "$VW_CONTAINER_NAME"
    probe_vaultwarden_http || restart_vaultwarden_service_apply >/dev/null 2>&1 || true

    if probe_vaultwarden_http; then
        echo -e "${GREEN}🎉 自动恢复完成，Vaultwarden 已重新拉起并通过探活。${NC}"
    else
        echo -e "${YELLOW}⚠️ 自动恢复已完成，但服务尚未通过探活，建议立即查看日志确认状态。${NC}"
    fi

    if [ -n "$backup_data_dir" ] && [ -d "$backup_data_dir" ]; then
        read -p "是否保留旧 data 备份目录 [$backup_data_dir]？(y/n, 默认y): " keep_old_data
        if [[ "$keep_old_data" == [nN]* ]]; then
            rm -rf "$backup_data_dir"
            echo -e "${GREEN}✅ 旧 data 备份目录已按您的选择删除。${NC}"
        else
            echo -e "${YELLOW}💡 已保留旧 data 备份目录，方便您后续手动回滚。${NC}"
        fi
    fi

    return 0
}

show_backup_diagnostics() {
    local latest_zip=""
    local entries=""

    sync_backup_remote_targets_state

    if ! backup_service_initialized; then
        echo -e "${YELLOW}⚠️ 备份诊断暂不可用：备份服务尚未初始化。${NC}"
        echo -e "${YELLOW}👉 请先执行步骤 2，生成备份服务基础配置。${NC}"
        return 1
    fi

    if ! backup_remote_configured; then
        echo -e "${YELLOW}⚠️ 备份诊断暂不可用：远程存储尚未配置完成。${NC}"
        echo -e "${YELLOW}👉 请先执行步骤 3，完成 Rclone 远程存储配置。${NC}"
        return 1
    fi

    if [ ! -d "$_VW_BACKUP_ARCHIVES_DIR" ]; then
        echo -e "${YELLOW}⚠️ 备份诊断：当前还没有发现本地备份目录。${NC}"
        return 1
    fi

    latest_zip=$(latest_backup_zip)
    if [ -z "$latest_zip" ]; then
        echo -e "${YELLOW}⚠️ 备份诊断：当前还没有发现任何 .zip 备份文件。${NC}"
        echo -e "${YELLOW}👉 您可以先执行一次备份，或将历史备份包放入 $_VW_BACKUP_ARCHIVES_DIR/。${NC}"
        return 1
    fi

    echo -e "${CYAN}📦 备份包诊断摘要${NC}"
    show_backup_remote_targets_summary
    echo -e "${GREEN}最近备份文件:${NC} ${latest_zip}"
    entries=$(list_backup_archive_entries "$latest_zip" || true)
    if [ -z "$entries" ]; then
        echo -e "${YELLOW}⚠️ 当前环境未安装 unzip/zipinfo，暂时无法展开查看压缩包内容。${NC}"
        return 0
    fi

    echo "$entries" | grep -Eq '(^|/)db\.[0-9]{8}\.sqlite3$|(^|/)db\.sqlite3$' \
        && echo -e "${GREEN}✅ 已检测到数据库主文件。${NC}" \
        || echo -e "${RED}⚠️ 未检测到数据库主文件，请优先排查该备份包是否可恢复。${NC}"

    echo "$entries" | grep -Eq '(^|/)rsa_key(\.pem|$)' \
        && echo -e "${GREEN}✅ 已检测到密钥文件。${NC}" \
        || echo -e "${YELLOW}⚠️ 未检测到 rsa_key，请确认当前实例是否确实使用了对应密钥文件。${NC}"

    if echo "$entries" | grep -Eq '(^|/)attachments(/|$)'; then
        echo -e "${GREEN}✅ 已检测到 attachments 附件目录。${NC}"
    else
        echo -e "${YELLOW}💡 压缩包中未发现 attachments 目录；如果您还没有附件文件，这通常是正常的。${NC}"
    fi

    if echo "$entries" | grep -Eq '(^|/)db\.sqlite3-(wal|shm)$'; then
        echo -e "${YELLOW}💡 压缩包中包含 SQLite WAL/SHM 临时文件。${NC}"
    else
        echo -e "${YELLOW}💡 压缩包中未包含 db.sqlite3-wal / db.sqlite3-shm；这通常是正常的，备份镜像一般只保留可恢复的主库文件。${NC}"
    fi

    if echo "$entries" | grep -Eq '(^|/)tmp(/|$)'; then
        echo -e "${YELLOW}💡 压缩包中包含 tmp 目录。${NC}"
    else
        echo -e "${YELLOW}💡 压缩包中未包含 tmp 目录；这通常也是正常的，tmp 多为运行时临时数据。${NC}"
    fi

    return 0
}

show_runtime_health() {
    local unhealthy=0
    if service_running "$VW_CONTAINER_NAME"; then
        echo -e "${GREEN}✅ Vaultwarden 容器运行中${NC}"
    else
        echo -e "${RED}❌ Vaultwarden 容器未运行${NC}"
        unhealthy=1
    fi

    if [ "$BACKUP_MODE" != "none" ]; then
        if service_running "$VW_BACKUP_CONTAINER_NAME"; then
            echo -e "${GREEN}✅ Backup 容器运行中${NC}"
        else
            echo -e "${YELLOW}⚠️ Backup 容器未运行${NC}"
            unhealthy=1
        fi
    else
        echo -e "${YELLOW}💡 Backup 服务尚未启用${NC}"
    fi

    if probe_vaultwarden_http; then
        echo -e "${GREEN}✅ Vaultwarden HTTP 探活通过${NC}"
    else
        echo -e "${YELLOW}⚠️ Vaultwarden HTTP 探活未通过${NC}"
        unhealthy=1
    fi

    if [ "$PROXY_MODE" == "caddy" ]; then
        load_caddy_runtime
        if [ -n "$EXISTING_CADDY_ID" ] && docker ps -q | grep -Fxq "$EXISTING_CADDY_ID"; then
            echo -e "${GREEN}✅ Caddy 容器运行中${NC}"
        else
            echo -e "${RED}❌ Caddy 容器未运行${NC}"
            unhealthy=1
        fi
    fi

    [ "$unhealthy" -eq 0 ] && return 0 || return 1
}

show_dashboard_summary() {
    local backup_state=""
    local backup_state_detail=""
    local proxy_state=""
    local security_state=""
    local current_token=""
    local current_domain_host=""
    local remote_target_count=0

    _refresh_docker_cache
    sync_backup_mode_state
    sync_backup_remote_targets_state

    if service_running "$VW_CONTAINER_NAME"; then
        echo -e "  ▶ 核心服务:  ${GREEN}🟢 运行中${NC}"
    elif [ -n "$VW_DIR" ] && [ -f "$_VW_COMPOSE_FILE" ]; then
        echo -e "  ▶ 核心服务:  ${YELLOW}🟡 已配置，未启动${NC}"
    else
        echo -e "  ▶ 核心服务:  ${RED}🔴 未部署${NC}"
    fi

    backup_state_detail=$(infer_backup_state_detail)
    case "$backup_state_detail" in
        remote_running)
            remote_target_count=$(count_backup_remote_targets)
            backup_state="${GREEN}🟢 备份服务运行中（远程上传已启用，目标 ${remote_target_count} 个）${NC}"
            ;;
        remote_ready)
            remote_target_count=$(count_backup_remote_targets)
            backup_state="${YELLOW}🟡 备份服务已初始化（远程上传已启用，目标 ${remote_target_count} 个，未启动）${NC}"
            ;;
        config_pending)
            backup_state="${YELLOW}🟡 备份服务已初始化，待完成远程存储配置${NC}"
            ;;
        dir_only)
            backup_state="${YELLOW}🟡 备份目录已存在，尚未初始化服务${NC}"
            ;;
        *)
            backup_state="${RED}🔴 未启用${NC}"
            ;;
    esac
    echo -e "  ▶ 备份状态:  ${backup_state}"

    current_domain_host=$(extract_domain_host "$(get_env_var "$_VW_ENV_FILE" "DOMAIN")")
    if [ "$PROXY_MODE" == "docker_ext" ]; then
        proxy_state="${YELLOW}🟡 外部容器反代${NC}"
    elif [ "$PROXY_MODE" == "host_ext" ]; then
        proxy_state="${YELLOW}🟡 宿主机反代${NC}"
    elif [ "$PROXY_MODE" == "caddy" ]; then
        load_caddy_runtime
        if [ -n "$EXISTING_CADDY_ID" ] && caddy_manages_domain "$current_domain_host"; then
            proxy_state="${GREEN}🟢 Caddy 已接管${NC}"
        elif [ -n "$VW_DIR" ] && [ -f "$_VW_COMPOSE_FILE" ]; then
            proxy_state="${YELLOW}🟡 已配置，待启动${NC}"
        else
            proxy_state="${RED}🔴 未配置${NC}"
        fi
    elif [ -n "$VW_DIR" ] && [ -f "$_VW_COMPOSE_FILE" ]; then
        proxy_state="${YELLOW}🟡 已配置，待启动${NC}"
    else
        proxy_state="${RED}🔴 未配置${NC}"
    fi
    echo -e "  ▶ 代理状态:  ${proxy_state}"

    current_token=$(get_env_var "$_VW_ENV_FILE" "ADMIN_TOKEN")
    if [[ "$current_token" == \$\$argon2* ]]; then
        security_state="${GREEN}🟢 ADMIN_TOKEN 已加密${NC}"
    elif [ -n "$current_token" ]; then
        security_state="${YELLOW}🟡 ADMIN_TOKEN 未加密${NC}"
    else
        security_state="${RED}🔴 未设置${NC}"
    fi
    echo -e "  ▶ 安全状态:  ${security_state}"
}

show_environment_doctor() {
    local current_domain=""
    local current_domain_host=""
    local domain_ip=""
    local public_ip=""

    sync_backup_remote_targets_state

    echo -e "\n${CYAN}$(_hr)${NC}"
    echo -e "${CYAN}           环境体检与状态检查             ${NC}"
    echo -e "${CYAN}$(_hr)${NC}"

    if command -v docker >/dev/null 2>&1; then
        echo -e "${GREEN}✅ Docker 命令可用${NC}"
    else
        echo -e "${RED}❌ Docker 命令不可用${NC}"
    fi

    if docker compose version >/dev/null 2>&1; then
        echo -e "${GREEN}✅ Docker Compose V2 可用${NC}"
    else
        echo -e "${RED}❌ Docker Compose V2 不可用${NC}"
    fi

    if [ -f "$CONF_FILE" ]; then
        echo -e "${GREEN}✅ 记忆配置文件存在: ${CONF_FILE}${NC}"
    else
        echo -e "${YELLOW}⚠️ 当前未发现记忆配置文件: ${CONF_FILE}${NC}"
    fi

    if [ -n "$VW_DIR" ]; then
        echo -e "${GREEN}✅ 项目根目录: ${VW_DIR}${NC}"
    else
        echo -e "${YELLOW}⚠️ 当前还没有记录项目根目录${NC}"
    fi

    if [ -f "$_VW_COMPOSE_FILE" ]; then
        echo -e "${GREEN}✅ 主服务配置文件存在${NC}"
    else
        echo -e "${YELLOW}⚠️ 主服务配置文件不存在${NC}"
    fi

    if [ -n "$DOCKER_NET" ]; then
        if docker network inspect "$DOCKER_NET" >/dev/null 2>&1; then
            if [ "$DOCKER_NET_OWNED" = "true" ]; then
                echo -e "${GREEN}✅ Docker 网络存在: ${DOCKER_NET}（脚本创建）${NC}"
            else
                echo -e "${GREEN}✅ Docker 网络存在: ${DOCKER_NET}（外部/共享）${NC}"
            fi
        else
            echo -e "${YELLOW}⚠️ 已记录 Docker 网络 [$DOCKER_NET]，但当前不存在${NC}"
        fi
    else
        echo -e "${YELLOW}⚠️ 当前还没有记录 Docker 网络${NC}"
    fi

    if [ -n "$CADDY_DIR" ]; then
        if [ -f "$CADDY_DIR/Caddyfile" ]; then
            echo -e "${GREEN}✅ Caddyfile 存在: $CADDY_DIR/Caddyfile${NC}"
        else
            echo -e "${YELLOW}⚠️ 已记录 Caddy 目录，但未发现 Caddyfile: $CADDY_DIR${NC}"
        fi
    fi

    current_domain=$(get_env_var "$_VW_ENV_FILE" "DOMAIN")
    if [ -n "$current_domain" ]; then
        current_domain_host=$(extract_domain_host "$current_domain")
        domain_ip=$(resolve_domain_ipv4 "$current_domain_host")
        public_ip=$(get_public_ipv4)
        echo -e "${GREEN}✅ 当前记录访问域名: ${current_domain}${NC}"
        echo -e "${YELLOW}💡 域名解析: ${domain_ip:-未知} / 本机公网 IP: ${public_ip:-未知}${NC}"
    else
        echo -e "${YELLOW}⚠️ 当前还没有记录访问域名${NC}"
    fi

    echo -e ""
    show_backup_remote_targets_summary
    echo -e ""
    show_runtime_health || true
    echo -e ""
    show_backup_diagnostics || true
}

# ==========================================
# 🌟 体验升级 1: 实时状态看板 Dashboard
# ==========================================
show_menu() {
    echo ""
    echo ""
    echo -e "${GREEN}$(_hr)${NC}"
    echo -e "       Vaultwarden 一键部署脚本 ${VW_SCRIPT_VERSION}"
    echo -e "${GREEN}$(_hr)${NC}"
    echo -e "${CYAN}${BOLD}实时状态看板:${NC}"
    show_dashboard_summary
    echo -e "${GREEN}$(_hr)${NC}"
    echo -e "${CYAN}【首次部署】${NC}"
    echo -e " 1) 🏗️  生成主服务配置"
    echo -e " 2) 🗂️  初始化备份服务"
    echo -e " 3) ☁️  配置 Rclone 远程存储（启动备份前必做）"
    echo -e ""
    echo -e "${CYAN}【服务控制】${NC}"
    echo -e " 4) 🚀 启动或重启主服务"
    echo -e " 5) ⏸️  停止主服务"
    echo -e " 6) 🧰 启动或重启备份服务"
    echo -e " 7) ⏹️  停止备份服务"
    echo -e ""
    echo -e "${CYAN}【安全与备份】${NC}"
    echo -e " 8) 🔄 升级到最新版本"
    echo -e " 9) 🛡️  一键加密 ADMIN_TOKEN"
    echo -e "10) ⚙️  修改主服务配置"
    echo -e "11) 🗂️  修改备份服务配置"
    echo -e "12) 🧪 立即执行一次备份"
    echo -e ""
    echo -e "${CYAN}【运行与排错】${NC}"
    echo -e "13) 🌐 查看访问信息与系统状态"
    echo -e "14) 📜 查看服务日志"
    echo -e "15) 🔎 查看备份诊断"
    echo -e "16) 🚑 恢复备份数据"
    echo -e "17) 🩺 环境体检"
    echo -e ""
    echo -e "${CYAN}【危险操作】${NC}"
    echo -e " ${RED}18) 彻底卸载并清理环境${NC}"
    echo -e " q) 退出管家"
}

# 🌟 体验升级 2: 操作停顿缓冲
pause_point() {
    echo ""
    read -n 1 -s -r -p "👉 操作结束，按任意键返回主菜单..."
    echo ""
}

# 1. 生成配置 (含神级网络解耦逻辑)
generate_configs() {
    echo -e "${GREEN}===> 步骤 1: 生成主服务配置 <===${NC}"
    
    if [ -z "$VW_DIR" ]; then
        read -p "📂 请输入项目安装根目录 [默认: $VW_DEFAULT_DIR]: " input_vw
        input_vw=$(_trim "${input_vw:-}")
        VW_DIR=${input_vw:-$VW_DEFAULT_DIR}
        _update_cached_paths
        set_conf_var "VW_DIR" "\"$VW_DIR\""
    fi
    
    if [ -f "$_VW_COMPOSE_FILE" ]; then
        echo -e "${YELLOW}⚠️ 探测拦截：检测到当前根目录 [$VW_DIR] 已存在主服务部署配置！${NC}"
        echo -e "${GREEN}👉 提示 1：如需修改域名、密码、邮箱等变量，请在主菜单选择【10】修改主服务配置。${NC}"
        echo -e "${RED}👉 提示 2：如需推倒重来，请先在主菜单选择【18】彻底卸载并清理环境。${NC}"
        return
    fi

    # 首次重新生成前，清掉可能残留的旧路由状态，避免半残配置把旧网络/代理模式带入新部署
    if [ -f "$CONF_FILE" ]; then
        clear_runtime_route_state
        persist_runtime_route_state
        load_conf_vars
    fi
    
    echo -e "${BLUE}项目根目录: $VW_DIR${NC}"
    echo -e "${BLUE}主服务目录: $_VW_APP_DIR${NC}"
    echo -e "${BLUE}备份服务目录: $_VW_BACKUP_DIR${NC}"

    # ==========================================
    # 🎯 终极解耦：网络与反代流派雷达探测
    # ==========================================
    PROXY_MODE="caddy"
    EXT_PORT="none"
    SKIP_CADDY="false"
    DOCKER_NET_OWNED="false"

    if [ -z "$CADDY_DIR" ]; then
        EXISTING_CADDY_ID=$(find_caddy_container_id)
        
        if [ -n "$EXISTING_CADDY_ID" ]; then
            CADDY_NAME=$(docker inspect -f '{{.Name}}' "$EXISTING_CADDY_ID" | sed 's|/||')
            echo -e "${YELLOW}🔍 智能探测：发现运行中的内置 Caddy (名称: $CADDY_NAME)！${NC}"
            
            CADDY_FILE_SRC=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/etc/caddy/Caddyfile"}}{{.Source}}{{end}}{{end}}' "$EXISTING_CADDY_ID" || true)
            if [ -n "$CADDY_FILE_SRC" ]; then
                CADDY_DIR=$(dirname "$CADDY_FILE_SRC")
                echo -e "${GREEN}✅ 成功定位到该 Caddy 的配置目录: $CADDY_DIR${NC}"
            else
                read -p "⚠️ 无法自动定位 Caddyfile，请输入其所在目录: " CADDY_DIR
            fi
            set_conf_var "CADDY_EXISTS" "\"true\""
            set_conf_var "CADDY_DIR" "\"$CADDY_DIR\""
            set_conf_var "EXISTING_CADDY_ID" "\"$EXISTING_CADDY_ID\""
        else
            # 探测 80/443 是否被占用
            PORT_HOG_ID=$(docker ps --format '{{.ID}} {{.Ports}}' | grep -E '0\.0\.0\.0:(80|443)->|:::(80|443)->' | awk '{print $1}' | head -n 1 || true)
            
            if [ -n "$PORT_HOG_ID" ]; then
                HOG_NAME=$(docker inspect -f '{{.Name}}' "$PORT_HOG_ID" | sed 's|/||')
                HOG_NET=$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$PORT_HOG_ID" | awk '{print $1}' || true)
                
                echo -e "\n${RED}⚠️ 检测到 80/443 端口已被容器 [${HOG_NAME:-未知服务}] 占用。${NC}"
                echo -e "${YELLOW}👉 这通常表示当前服务器已经有反向代理服务在接管入口流量。${NC}"
                echo -e "${GREEN}👉 接下来脚本会跳过内置 Caddy，改由您现有的外部反代工具来接管 Vaultwarden。${NC}"
                
                # 如果对方是 Docker 容器，且有独立网络（非 host），启动同化策略
                if [ -n "$HOG_NET" ] && [ "$HOG_NET" != "host" ]; then
                    echo -e "\n${CYAN}💡 网络接入建议:${NC}"
                    echo -e "检测到该容器运行在 Docker 网络 [${GREEN}$HOG_NET${NC}] 中。"
                    echo -e "这很像是一个容器化反代面板使用中的网络（例如 1Panel、Nginx Proxy Manager）。"
                    echo -e "推荐直接让 Vaultwarden 加入这个网络，这样反代可以走容器内网通信，配置更简单，也更稳定。"
                    echo -e "--------------------------------------------------------"
                    read -p "是否让 Vaultwarden 直接加入该网络？(y/n, 默认y): " auto_join
                    
                    if [[ "$auto_join" != [nN]* ]]; then
                        DOCKER_NET="$HOG_NET"
                        PROXY_MODE="docker_ext"
                        SKIP_CADDY="true"
                        DOCKER_NET_OWNED="false"
                        echo -e "${GREEN}✅ 已接入目标网络。${NC}"
                        echo -e "${GREEN}👉 稍后您只需要在反代面板里把目标地址写成: http://vaultwarden:80${NC}"
                    else
                        PROXY_MODE="host_ext"
                        SKIP_CADDY="true"
                        read -p "请输入映射到宿主机的本地安全端口 (默认 8080): " host_port
                        EXT_PORT=${host_port:-8080}
                        echo -e "${GREEN}✅ 已切换为宿主机本地端口映射模式。${NC}"
                        echo -e "${GREEN}👉 稍后您可以在反代工具中把目标地址写成: http://127.0.0.1:$EXT_PORT${NC}"
                    fi
                else
                    # 对方是宿主机 Nginx 或者是 host 模式
                    echo -e "\n${YELLOW}💡 当前没有识别到可直接复用的容器网络。${NC}"
                    echo -e "${YELLOW}👉 脚本将改用宿主机本地端口映射模式，方便您后续通过宿主机反代工具接入。${NC}"
                    PROXY_MODE="host_ext"
                    SKIP_CADDY="true"
                    read -p "请输入映射到宿主机的本地安全端口 (默认 8080): " host_port
                    EXT_PORT=${host_port:-8080}
                fi
            else
                echo -e "${YELLOW}🔍 当前未发现 80/443 端口冲突。${NC}"
                echo -e "${GREEN}👉 接下来脚本将使用内置 Caddy，帮您完成一套更省心的自动反代方案。${NC}"
                read -p "📂 请输入全新的 Caddy 安装目录 [默认: $VW_DEFAULT_CADDY_DIR]: " input_caddy
                input_caddy=$(_trim "${input_caddy:-}")
                CADDY_DIR=${input_caddy:-$VW_DEFAULT_CADDY_DIR}
                set_conf_var "CADDY_EXISTS" "\"false\""
                set_conf_var "CADDY_DIR" "\"$CADDY_DIR\""
            fi
        fi
    fi
    
    # 持久化存储路由流派
    set_conf_var "PROXY_MODE" "\"$PROXY_MODE\""
    set_conf_var "EXT_PORT" "\"$EXT_PORT\""
    set_conf_var "SKIP_CADDY" "\"$SKIP_CADDY\""
    if [ -n "$DOCKER_NET" ]; then set_conf_var "DOCKER_NET" "\"$DOCKER_NET\""; fi
    set_conf_var "DOCKER_NET_OWNED" "\"$DOCKER_NET_OWNED\""
    load_conf_vars

    # 如果没有走同化策略，常规选取/创建网络
    if [ "$PROXY_MODE" != "docker_ext" ] && [ -z "$DOCKER_NET" ]; then
        local net_choice=""
        local default_network="$VW_DEFAULT_NET"

        mapfile -t NETWORKS < <(filter_selectable_networks)

        # 内置 Caddy 模式且无有意义的已有网络时，自动创建 Caddy 公共网络，无需用户选择
        if [ "$PROXY_MODE" == "caddy" ] && [ "$CADDY_EXISTS" = "false" ]; then
            local _has_custom_net=false
            for _net in "${NETWORKS[@]}"; do
                if [ "$_net" != "$default_network" ] && [ "$_net" != "$CADDY_DEFAULT_NET" ]; then
                    _has_custom_net=true
                    break
                fi
            done

            if [ "$_has_custom_net" = false ]; then
                net_choice="$CADDY_DEFAULT_NET"
                echo -e "\n${GREEN}🔗 内置 Caddy 模式：自动创建公共网络 [$net_choice]，Caddy 与 Vaultwarden 及其他需要反代的容器均可加入此网络。${NC}"
            fi
        fi

        # 存在有意义的自定义网络，或非 Caddy 自动模式时，交互选择
        if [ -z "$net_choice" ]; then
            if [ ${#NETWORKS[@]} -eq 0 ]; then
                NETWORKS=("$default_network")
            fi

            while true; do
                echo -e "\n请选择 Vaultwarden 专属的 Docker 网络："
                for i in "${!NETWORKS[@]}"; do echo "  $((i+1))) ${NETWORKS[$i]}"; done
                read -p "输入编号、直接输入新网络名创建，或回车使用默认值 [$default_network]: " NET_INPUT

                if [ -z "$NET_INPUT" ]; then
                    net_choice="$default_network"
                    break
                fi

                if [[ "$NET_INPUT" =~ ^[0-9]+$ ]]; then
                    if [ "$NET_INPUT" -ge 1 ] && [ "$NET_INPUT" -le "${#NETWORKS[@]}" ]; then
                        net_choice="${NETWORKS[$((NET_INPUT-1))]}"
                        break
                    fi
                    echo -e "${RED}❌ 编号超出范围，请重新输入。${NC}"
                    continue
                fi

                if ! validate_docker_network_name "$NET_INPUT"; then
                    echo -e "${RED}❌ 网络名格式不合法，请使用字母、数字、下划线、中划线或点号。${NC}"
                    continue
                fi

                net_choice="$NET_INPUT"
                break
            done
        fi

        DOCKER_NET="$net_choice"
        if docker network inspect "$DOCKER_NET" >/dev/null 2>&1; then
            DOCKER_NET_OWNED="false"
        else
            docker network create "$DOCKER_NET" >/dev/null 2>&1
            DOCKER_NET_OWNED="true"
        fi

        set_conf_var "DOCKER_NET" "\"$DOCKER_NET\""
        set_conf_var "DOCKER_NET_OWNED" "\"$DOCKER_NET_OWNED\""
    fi

    # 【域名接入提示：Cloudflare/CDN】
    echo -e "\n${CYAN}🌐 站点接入向导${NC}"
    echo -e "${CYAN}步骤 1/3：配置访问域名${NC}"
    echo -e "${YELLOW}💡 如果您的域名托管在 Cloudflare 等 CDN / DNS 平台上，请先把代理模式临时关闭（例如 Cloudflare 的橙色云朵）。${NC}"
    echo -e "${YELLOW}👉 否则在首次部署阶段，可能会影响证书申请，或导致站点出现 ERR_TOO_MANY_REDIRECTS 重定向循环。${NC}"
    echo -e "${YELLOW}👉 等部署确认正常后，再按需重新开启代理模式。${NC}"
    while true; do
        read -p "请输入准备绑定到 Vaultwarden 的域名 (如 bbb.aaa.com): " DOMAIN
        DOMAIN=$(_trim "$DOMAIN")
        if validate_domain_input "$DOMAIN"; then
            break
        fi
        echo -e "${RED}❌ 域名格式不正确，请重新输入一个有效域名。${NC}"
    done
    if ! confirm_domain_dns_or_abort "$DOMAIN"; then
        echo -e "${YELLOW}👉 已取消本次主服务配置生成，请先修正 DNS 后再重试。${NC}"
        return 1
    fi
    
    echo -e "\n${CYAN}步骤 2/3：配置管理员邮箱${NC}"
    echo -e "${YELLOW}💡 这里填写一个可用邮箱，后续可用于系统通知等配置。${NC}"
    while true; do
        read -p "请输入管理员邮箱: " SERVER_ADMIN_EMAIL
        SERVER_ADMIN_EMAIL=$(_trim "$SERVER_ADMIN_EMAIL")
        if validate_email "$SERVER_ADMIN_EMAIL"; then break; fi
    done
    
    echo -e "\n${CYAN}步骤 3/3：设置管理后台密码${NC}"
    echo -e "${CYAN}🛡️ 密码要求：至少 8 位，并同时包含大写字母、小写字母、数字和特殊符号。${NC}"
    while true; do
        read -s -p "请输入 ADMIN_TOKEN（输入不可见）: " RAW_TOKEN ; echo ""
        if validate_password "$RAW_TOKEN"; then
            read -s -p "请再次输入确认 ADMIN_TOKEN: " RAW_TOKEN_CONFIRM ; echo ""
            if [ "$RAW_TOKEN" == "$RAW_TOKEN_CONFIRM" ]; then
                echo -e "${GREEN}✅ 管理后台密码校验通过。${NC}"
                break
            else
                echo -e "${RED}❌ 两次输入的密码不一致！${NC}"
            fi
        fi
    done

    local signups_allowed="false"
    echo -e "\n${CYAN}👤 首次初始化注册策略${NC}"
    echo -e "${YELLOW}💡 如果您准备先创建第一个账号再关闭注册，可以临时开启。${NC}"
    read -p "首次部署是否临时开启新用户注册？(y/n, 默认 y): " signup_choice
    if [[ "$signup_choice" != [nN]* ]]; then
        signups_allowed="true"
    fi

    # 生成配置前确认摘要
    echo -e "\n${CYAN}$(_hr)${NC}"
    echo -e "${CYAN}     📋 配置确认摘要${NC}"
    echo -e "${CYAN}$(_hr)${NC}"
    echo -e "  ▶ 访问域名:     ${GREEN}https://$DOMAIN${NC}"
    echo -e "  ▶ 管理员邮箱:   ${GREEN}$SERVER_ADMIN_EMAIL${NC}"
    echo -e "  ▶ ADMIN_TOKEN:  ${GREEN}[已加密保护]${NC}"
    echo -e "  ▶ 代理模式:     ${GREEN}$PROXY_MODE${NC}"
    if [ -n "$DOCKER_NET" ]; then
        echo -e "  ▶ Docker 网络:  ${GREEN}$DOCKER_NET${NC}"
    fi
    if [ "$PROXY_MODE" == "host_ext" ] && [ -n "$EXT_PORT" ] && [ "$EXT_PORT" != "none" ]; then
        echo -e "  ▶ 映射端口:     ${GREEN}127.0.0.1:$EXT_PORT${NC}"
    fi
    echo -e "  ▶ 注册策略:     ${GREEN}$( [ "$signups_allowed" == "true" ] && echo "临时开启" || echo "已关闭" )${NC}"
    echo -e "${CYAN}$(_hr)${NC}"
    local confirm_generate=""
    read -p "确认生成以上配置？(y/n, 默认 y): " confirm_generate
    if [[ "$confirm_generate" == [nN]* ]]; then
        echo -e "${YELLOW}👉 已取消配置生成，返回主菜单。${NC}"
        return 1
    fi
    
    mkdir -p "$_VW_DATA_DIR"
    BACKUP_MODE="none"
    set_conf_var "BACKUP_MODE" "\"$BACKUP_MODE\""

    echo -e "${YELLOW}正在离线加密 ADMIN_TOKEN，请稍候...${NC}"
    ENCRYPTED_TOKEN=$(hash_admin_token_with_docker "$RAW_TOKEN" || true)
    if [[ -z "$ENCRYPTED_TOKEN" || "$ENCRYPTED_TOKEN" != \$argon2id* ]]; then
        echo -e "${RED}❌ ADMIN_TOKEN 加密失败，已取消本次部署，避免将明文密码写入配置文件。${NC}"
        echo -e "${YELLOW}👉 请检查 Docker 网络是否正常，然后重新执行步骤 1。${NC}"
        return 1
    fi
    ESCAPED_TOKEN=$(echo "$ENCRYPTED_TOKEN" | sed 's/\$/\$\$/g')

    cat <<EOF > "$_VW_ENV_FILE"
TZ=Asia/Shanghai
DOMAIN=https://$DOMAIN
ADMIN_TOKEN=$ESCAPED_TOKEN
SERVER_ADMIN_EMAIL=$SERVER_ADMIN_EMAIL
SENDS_ALLOWED=false
SIGNUPS_ALLOWED=$signups_allowed
INVITATIONS_ALLOWED=true
WEBSOCKET_ENABLED=true
WEB_VAULT_ENABLED=true
EMERGENCY_ACCESS_ALLOWED=true
ENABLE_DB_WAL=true
ADMIN_RATELIMIT_MAX_BURST=5
ADMIN_RATELIMIT_SECONDS=60
ADMIN_SESSION_LIFETIME=20
EOF
    chmod 600 "$_VW_ENV_FILE" 2>/dev/null || true

    # ==========================================
    # 🎯 动态解耦生成 YAML (绝不删一行注释)
    # ==========================================
    cat <<EOF > "$_VW_COMPOSE_FILE"
services:
  vaultwarden:
    image: vaultwarden/server:latest
    container_name: vaultwarden
    restart: unless-stopped
EOF

    # 只有宿主机映射派，才会在 YAML 里写明 ports，精准解耦
    if [ "$PROXY_MODE" == "host_ext" ] && [ -n "$EXT_PORT" ] && [ "$EXT_PORT" != "none" ]; then
        echo "    ports:" >> "$_VW_COMPOSE_FILE"
        echo "      - \"127.0.0.1:$EXT_PORT:80\"" >> "$_VW_COMPOSE_FILE"
    fi

    cat <<EOF >> "$_VW_COMPOSE_FILE"
    env_file:
      - ./vaultwarden.env
    volumes:
      - ./data:/data
    networks:
      - $DOCKER_NET

networks:
  $DOCKER_NET:
    external: true
EOF

    # 只有 Caddy 派才去管 Caddy 的配置文件
    if [ "$PROXY_MODE" == "caddy" ]; then
        mkdir -p "$CADDY_DIR/data" "$CADDY_DIR/config"
        write_caddy_managed_block "$(extract_domain_host "$DOMAIN")"

        if [ "$CADDY_EXISTS" = "false" ]; then
            cat <<EOF > "$CADDY_DIR/docker-compose.yml"
services:
  caddy:
    image: caddy:latest
    container_name: caddy
    restart: unless-stopped
    ports: ["80:80", "443:443"]
    volumes: ["./Caddyfile:/etc/caddy/Caddyfile", "./data:/data", "./config:/config"]
    networks: ["$DOCKER_NET"]
networks:
  $DOCKER_NET:
    external: true
EOF
        fi
    fi
    echo -e "${GREEN}✅ 主服务配置已生成完成，当前尚未启动容器。${NC}"
    echo -e "${YELLOW}💡 如需启用备份服务，请继续执行步骤 2 初始化备份服务。${NC}"
    if [ "$signups_allowed" == "true" ]; then
        echo -e "${YELLOW}💡 当前已临时开启注册。完成首个账号创建后，建议尽快在主服务配置中关闭。${NC}"
    fi
}

# 2. 备份服务配置
setup_backup_service() {
    local zip_pw=""
    local zip_pw_confirm=""
    local keep_days=""
    local cron_expr=""
    local backup_timezone=""

    if ! require_main_service_config; then
        return 1
    fi

    if [ -f "$_VW_BACKUP_COMPOSE_FILE" ]; then
        echo -e "${YELLOW}⚠️ 备份服务配置已存在。${NC}"
        echo -e "${GREEN}👉 如需补配远程存储，请继续执行步骤 3。${NC}"
        return 0
    fi

    echo -e "${GREEN}===> 步骤 2: 初始化备份服务 <===${NC}"
    echo -e "${CYAN}备份配置向导${NC}"
    echo -e "${YELLOW}👉 这一阶段会先准备本地备份参数。完成后，还需要继续执行步骤 3 配置远程存储，之后才能正式启动备份服务。${NC}"

    echo -e "\n${CYAN}步骤 1/4：设置备份压缩包密码${NC}"
    echo -e "${YELLOW}💡 这个密码会用于加密生成的备份压缩包，恢复时也需要用到，请妥善保管。${NC}"
    while true; do
        read -s -p "请输入备份压缩包密码（输入不可见）: " zip_pw ; echo ""
        if validate_password "$zip_pw"; then
            read -s -p "请再次输入确认备份密码: " zip_pw_confirm ; echo ""
            if [ "$zip_pw" == "$zip_pw_confirm" ]; then
                echo -e "${GREEN}✅ 备份压缩包密码校验通过。${NC}"
                break
            else
                echo -e "${RED}❌ 两次输入的密码不一致！${NC}"
            fi
        fi
    done

    echo -e "\n${CYAN}步骤 2/4：设置备份保留天数${NC}"
    echo -e "${YELLOW}💡 超过保留天数的旧备份会按策略清理。默认保留 30 天。${NC}"
    read -p "请输入备份保留天数 [默认 30]: " keep_days
    keep_days=${keep_days:-30}
    if ! validate_non_negative_integer "$keep_days"; then
        echo -e "${RED}❌ 备份保留天数必须是非负整数。${NC}"
        return 1
    fi

    echo -e "\n${CYAN}步骤 3/4：设置备份执行时间${NC}"
    echo -e "${YELLOW}💡 请选择备份执行频率，或自定义 CRON 表达式。${NC}"
    echo " 1) 每天凌晨 3 点 (0 3 * * *)  [默认]"
    echo " 2) 每 12 小时 (0 */12 * * *)"
    echo " 3) 每 6 小时 (0 */6 * * *)"
    echo " 4) 自定义 CRON 表达式"
    local cron_choice=""
    read -p "请选择 [1-4, 默认 1]: " cron_choice
    case "${cron_choice:-1}" in
        1) cron_expr="0 3 * * *" ;;
        2) cron_expr="0 */12 * * *" ;;
        3) cron_expr="0 */6 * * *" ;;
        4)
            while true; do
                read -p "请输入自定义 CRON 表达式: " cron_expr
                if [ -z "$cron_expr" ]; then
                    cron_expr="0 3 * * *"
                    echo -e "${YELLOW}💡 未输入，已使用默认值: $cron_expr${NC}"
                    break
                fi
                if validate_cron_expression "$cron_expr"; then
                    break
                fi
                echo -e "${RED}❌ CRON 表达式格式不正确，请使用标准 5 段格式（如 0 3 * * *）。${NC}"
            done
            ;;
        *) cron_expr="0 3 * * *" ;;
    esac
    echo -e "${GREEN}✅ 备份执行计划: $cron_expr${NC}"

    echo -e "\n${CYAN}步骤 4/4：设置备份服务时区${NC}"
    echo -e "${YELLOW}💡 建议与您的服务器使用同一时区，默认是 Asia/Shanghai。${NC}"
    read -p "请输入备份服务时区 [默认 Asia/Shanghai]: " backup_timezone
    backup_timezone=${backup_timezone:-Asia/Shanghai}
    if ! validate_timezone_input "$backup_timezone"; then
        echo -e "${RED}❌ 时区格式不正确，请使用类似 Asia/Shanghai 的格式。${NC}"
        return 1
    fi

    mkdir -p "$_VW_BACKUP_ARCHIVES_DIR" "$_VW_BACKUP_CONFIG_DIR/rclone"

    cat <<EOF > "$_BACKUP_ENV_FILE"
RCLONE_REMOTE_NAME=BitwardenBackup
RCLONE_REMOTE_DIR=/VaultBackup
CRON=$cron_expr
ZIP_PASSWORD=$zip_pw
BACKUP_KEEP_DAYS=$keep_days
TIMEZONE=$backup_timezone
EOF
    chmod 600 "$_BACKUP_ENV_FILE" 2>/dev/null || true

    cat <<EOF > "$_VW_BACKUP_COMPOSE_FILE"
services:
  backup:
    image: ttionya/vaultwarden-backup:latest
    container_name: vaultwarden-backup
    restart: always
    env_file:
      - ./backup.env
    volumes:
      - ../vaultwarden/data:/bitwarden/data:ro
      - ./backups:/bitwarden/backup
      - ./vw-backup-config:/config
    networks:
      - $DOCKER_NET

networks:
  $DOCKER_NET:
    external: true
EOF

    BACKUP_MODE="local"
    BACKUP_REMOTE_TARGETS=""
    set_conf_var "BACKUP_MODE" "\"$BACKUP_MODE\""
    set_conf_var "BACKUP_REMOTE_TARGETS" "\"$BACKUP_REMOTE_TARGETS\""
    echo -e "${GREEN}✅ 备份服务基础配置已生成完成。${NC}"
    echo -e "${YELLOW}👉 下一步请继续执行步骤 3，完成 Rclone 远程存储配置。${NC}"
    echo -e "${YELLOW}👉 等远程存储配置完成后，再回来启动备份服务。${NC}"
}

# 3. Rclone 配置
configure_rclone() {
    local CONF_FILE_RC=""
    local entry_choice=""
    local submenu_choice=""
    local authorized_count=0
    local restart_result=0
    local configure_targets_status=0

    if ! require_backup_service_config; then
        return
    fi
    echo -e "${GREEN}===> 步骤 3: 配置 Rclone <===${NC}"
    echo -e "${CYAN}远程存储接入向导${NC}"
    echo -e "${YELLOW}👉 这里支持管理远程节点、调整当前启用目标，以及查看/重连部分后端支持的官方能力。${NC}"
    echo -e "${YELLOW}👉 其中“授权身份”“容量/配额”“重新授权”是否可用，取决于具体 remote 的后端类型。${NC}"
    echo -e "${YELLOW}👉 进入具体功能后，远程目标列表会按后端类型显示“大概率支持 / 通常不支持 / 通常无需重连”等提示。${NC}"

    while true; do
        CONF_FILE_RC="$_VW_BACKUP_RCLONE_CONF"
        authorized_count=$(list_authorized_rclone_remotes "$CONF_FILE_RC" | awk 'NF { count++ } END { print count + 0 }')

        if [ ! -f "$CONF_FILE_RC" ] || [ "$authorized_count" -eq 0 ]; then
            echo -e "${YELLOW}💡 当前还没有可直接启用的远程节点，先进入 Rclone 官方交互界面。${NC}"
            echo -e "${YELLOW}💡 完成新增或修改后，请在 Rclone 界面输入 q 退出；返回后会回到当前配置入口。${NC}"
            sleep 2
            run_rclone_config_ui
            CONF_FILE_RC="$_VW_BACKUP_RCLONE_CONF"
            if [ ! -f "$CONF_FILE_RC" ]; then
                echo -e "${RED}❌ 未检测到 rclone.conf，配置结果未落盘，请重新执行步骤 2。${NC}"
                return 1
            fi
            continue
        fi

        show_rclone_config_entry_menu
        if [ "$authorized_count" -gt 0 ]; then
            read -p "请选择操作 [1, 2, 3, 4, q]: " entry_choice
        else
            read -p "请选择操作 [1, 3, q]: " entry_choice
        fi
        case "$entry_choice" in
            1)
                while true; do
                    show_rclone_remote_management_menu
                    read -p "请选择操作 [1, 2, 3, q]: " submenu_choice
                    case "$submenu_choice" in
                        1)
                            echo -e "${YELLOW}💡 完成远程节点调整后，请在 Rclone 界面输入 q 退出；返回后会回到当前配置入口菜单。${NC}"
                            run_rclone_config_ui
                            CONF_FILE_RC="$_VW_BACKUP_RCLONE_CONF"
                            if [ ! -f "$CONF_FILE_RC" ]; then
                                echo -e "${RED}❌ 未检测到 rclone.conf，配置结果未落盘，请重新执行步骤 2。${NC}"
                                return 1
                            fi
                            break
                            ;;
                        2)
                            configure_manual_rclone_remote "$CONF_FILE_RC" || true
                            CONF_FILE_RC="$_VW_BACKUP_RCLONE_CONF"
                            if [ ! -f "$CONF_FILE_RC" ]; then
                                echo -e "${RED}❌ 未检测到 rclone.conf，手动写入结果未落盘，请稍后重试。${NC}"
                                return 1
                            fi
                            break
                            ;;
                        3)
                            reconnect_rclone_remote "$CONF_FILE_RC" || true
                            CONF_FILE_RC="$_VW_BACKUP_RCLONE_CONF"
                            if [ ! -f "$CONF_FILE_RC" ]; then
                                echo -e "${RED}❌ 未检测到 rclone.conf，重新授权结果未落盘，请稍后重试。${NC}"
                                return 1
                            fi
                            break
                            ;;
                        q|Q)
                            break
                            ;;
                        *)
                            print_invalid_choice
                            ;;
                    esac
                done
                continue
                ;;
            2)
                if [ "$authorized_count" -eq 0 ]; then
                    print_invalid_choice
                    continue
                fi
                if ! configure_backup_remote_targets; then
                    configure_targets_status=$?
                    if [ "$configure_targets_status" -eq 2 ]; then
                        echo -e "${YELLOW}👉 已取消本次远程目标选择，正在返回 Rclone 配置入口。${NC}"
                        continue
                    fi
                    echo -e "${YELLOW}👉 远程目标启用列表未更新，当前保留原有配置。${NC}"
                    return 1
                fi
                break
                ;;
            3)
                show_rclone_config_view_menu "$CONF_FILE_RC" || true
                continue
                ;;
            4)
                if [ "$authorized_count" -eq 0 ]; then
                    print_invalid_choice
                    continue
                fi
                while true; do
                    show_rclone_capability_menu
                    read -p "请选择操作 [1, 2, q]: " submenu_choice
                    case "$submenu_choice" in
                        1)
                            show_rclone_remote_userinfo "$CONF_FILE_RC" || true
                            break
                            ;;
                        2)
                            show_rclone_remote_about "$CONF_FILE_RC" || true
                            break
                            ;;
                        q|Q)
                            break
                            ;;
                        *)
                            print_invalid_choice
                            ;;
                    esac
                done
                continue
                ;;
            q|Q)
                return 1
                ;;
            *)
                print_invalid_choice
                ;;
        esac
    done

    sync_backup_remote_targets_state
    echo -e "${GREEN}✅ Rclone 配置校验通过，已同步远程目标列表。${NC}"

    if service_running "$VW_BACKUP_CONTAINER_NAME"; then
        echo -e "${YELLOW}💡 检测到备份服务正在运行。为了让新的网盘目标立即生效，需要重建一次备份容器。${NC}"
        prompt_restart_backup_if_running "是否现在立即重建备份服务并应用新的远程目标？(y/n): "
        restart_result=$?
        if [ "$restart_result" -eq 10 ]; then
            echo -e "${GREEN}🎉 备份服务已按新远程目标重建完成，后续手动备份会直接使用新网盘。${NC}"
        else
            echo -e "${YELLOW}💡 新远程目标已经写入配置；下次执行步骤 6 启动/重启备份服务时会自动生效。${NC}"
        fi
    else
        echo -e "${YELLOW}👉 现在可以返回主菜单，执行步骤 6 启动备份服务。${NC}"
    fi
}

# 4. 启动主服务
start_core_service() {
    if ! require_main_service_config; then return; fi
    echo -e "${GREEN}===> 步骤 4: 启动或重启主服务 <===${NC}"
    echo -e "${CYAN}服务启动向导${NC}"
    echo -e "${YELLOW}👉 接下来会启动 Vaultwarden 主服务，并在需要时同步处理代理服务。${NC}"
    check_disk_space "$_VW_APP_DIR" 500
    cd "$_VW_APP_DIR" || { echo -e "${RED}❌ 主服务目录不存在: $_VW_APP_DIR${NC}"; return 1; }
    CURRENT_DOMAIN=$(get_env_var "$_VW_ENV_FILE" "DOMAIN")
    if [ -n "$CURRENT_DOMAIN" ]; then
        confirm_domain_dns_or_abort "$CURRENT_DOMAIN" || true
    fi
    
    echo -e "${YELLOW}正在启动 Vaultwarden 主服务...${NC}"
    compose_up_dir "$_VW_APP_DIR"

    if [ "$PROXY_MODE" == "caddy" ]; then
        echo -e "${YELLOW}正在同步处理内置 Caddy 代理服务...${NC}"
        if [ "$CADDY_EXISTS" = "false" ]; then
            compose_up_dir "$CADDY_DIR"
        fi
        reload_caddy_runtime || true
    fi
    echo -e "${YELLOW}正在等待 Vaultwarden 完成启动探活，请稍候...${NC}"
    if wait_for_vaultwarden_ready 12 2 && show_runtime_health; then
        echo -e "${GREEN}🎉 主服务已启动，最新配置已加载。${NC}"
        echo -e "${YELLOW}👉 您现在可以继续查看访问信息，或进入后续功能配置。${NC}"
    else
        echo -e "${YELLOW}⚠️ 启动命令已经执行，但在当前等待窗口内仍未完全就绪。${NC}"
        echo -e "${YELLOW}👉 建议先查看主服务日志，确认容器状态与启动输出。${NC}"
    fi
}

# 5. 停止主服务
stop_core_service() {
    if ! require_main_service_config; then
        return 1
    fi

    echo -e "${GREEN}===> 步骤 5: 停止主服务 <===${NC}"
    if ! service_running "$VW_CONTAINER_NAME"; then
        echo -e "${YELLOW}💡 Vaultwarden 主服务当前未运行，无需停止。${NC}"
        return 0
    fi

    cd "$_VW_APP_DIR" || { echo -e "${RED}❌ 主服务目录不存在${NC}"; return 1; }
    compose_stop_service_dir "$_VW_APP_DIR" "$VW_CONTAINER_NAME"

    if service_running "$VW_CONTAINER_NAME"; then
        echo -e "${YELLOW}⚠️ 已执行停止命令，但 Vaultwarden 容器仍在运行，请查看日志进一步确认。${NC}"
        return 1
    fi

    echo -e "${GREEN}✅ Vaultwarden 主服务已停止。${NC}"
}

# 6. 启动备份服务
start_backup_service() {
    if ! require_backup_service_config; then
        return 1
    fi

    if ! backup_remote_configured; then
        echo -e "${RED}❌ 备份服务尚未完成远程存储配置，当前不能直接启动。${NC}"
        echo -e "${YELLOW}👉 请先执行步骤 3 完成 Rclone 配置，再回来启动备份服务。${NC}"
        return 1
    fi

    check_disk_space "$_VW_APP_DIR" 500

    if ! service_running "$VW_CONTAINER_NAME"; then
        echo -e "${YELLOW}💡 检测到主服务当前未运行，脚本会先帮您启动主服务。${NC}"
        start_core_service
    fi

    echo -e "${GREEN}===> 步骤 6: 启动或重启备份服务 <===${NC}"
    echo -e "${CYAN}备份服务启动向导${NC}"
    echo -e "${YELLOW}👉 接下来会重建备份容器，并确保最新的远程存储配置已经装载。${NC}"
    cd "$_VW_BACKUP_DIR" || { echo -e "${RED}❌ 备份服务目录不存在${NC}"; return 1; }
    compose_up_force_recreate_dir "$_VW_BACKUP_DIR"
    sleep 3
    sync_backup_mode_state
    if service_running "$VW_BACKUP_CONTAINER_NAME"; then
        echo -e "${GREEN}🎉 备份服务已启动。${NC}"
        echo -e "${YELLOW}👉 您现在可以执行一次手动备份，或进入备份诊断查看最新状态。${NC}"
    else
        echo -e "${RED}❌ 备份服务启动失败，容器当前未稳定运行。${NC}"
        echo -e "${YELLOW}👉 请先查看备份容器日志，重点确认 Rclone 配置、远程授权和环境变量是否正确。${NC}"
        return 1
    fi
}

# 7. 停止备份服务
stop_backup_service() {
    if ! require_backup_service_config; then
        return 1
    fi

    echo -e "${GREEN}===> 步骤 7: 停止备份服务 <===${NC}"
    if ! service_running "$VW_BACKUP_CONTAINER_NAME"; then
        echo -e "${YELLOW}💡 备份服务当前未运行，无需停止。${NC}"
        return 0
    fi

    cd "$_VW_BACKUP_DIR" || { echo -e "${RED}❌ 备份服务目录不存在${NC}"; return 1; }
    compose_stop_service_dir "$_VW_BACKUP_DIR" "backup"
    sync_backup_mode_state

    if service_running "$VW_BACKUP_CONTAINER_NAME"; then
        echo -e "${YELLOW}⚠️ 已执行停止命令，但备份容器仍在运行，请查看日志进一步确认。${NC}"
        return 1
    fi

    echo -e "${GREEN}✅ 备份服务已停止。${NC}"
}

# 🌟 体验升级 3: 一键平滑升级
update_services() {
    if ! require_main_service_config; then return; fi
    echo -e "${GREEN}===> 步骤 8: 升级到最新版本 <===${NC}"
    check_disk_space "$_VW_APP_DIR" 1000
    cd "$_VW_APP_DIR" || { echo -e "${RED}❌ 主服务目录不存在${NC}"; return 1; }
    CURRENT_DOMAIN=$(get_env_var "$_VW_ENV_FILE" "DOMAIN")
    if [ -n "$CURRENT_DOMAIN" ]; then
        confirm_domain_dns_or_abort "$CURRENT_DOMAIN" || true
    fi
    
    echo -e "${YELLOW}正在拉取最新镜像...（该过程不会中断当前业务）${NC}"
    compose_pull_dir "$_VW_APP_DIR" > /dev/null 2>&1 &
    _spinner $!
    wait $! 2>/dev/null || true
    echo -e "${YELLOW}正在重启容器并应用新版本...${NC}"
    compose_up_remove_orphans_dir "$_VW_APP_DIR"
    
    if [ -f "$_VW_BACKUP_COMPOSE_FILE" ]; then
        compose_pull_dir "$_VW_BACKUP_DIR" > /dev/null 2>&1 &
        _spinner $!
        wait $! 2>/dev/null || true
        compose_up_remove_orphans_dir "$_VW_BACKUP_DIR" 2>/dev/null || true
    fi

    if [ "$PROXY_MODE" == "caddy" ] && [ -n "$CADDY_DIR" ]; then
        compose_pull_dir "$CADDY_DIR" > /dev/null 2>&1 &
        _spinner $!
        wait $! 2>/dev/null || true
        compose_up_remove_orphans_dir "$CADDY_DIR" 2>/dev/null || true
        reload_caddy_runtime || true
    fi
    
    echo -e "${YELLOW}正在清理废弃的旧版本镜像以释放空间...${NC}"
    docker image prune -f 2>/dev/null || true
    if show_runtime_health; then
        echo -e "${GREEN}🎉 升级完成！您的私有云已是最新版本！${NC}"
    else
        echo -e "${YELLOW}⚠️ 升级命令已完成，但部分组件未完全就绪，请查看日志。${NC}"
    fi
}

# 5. 离线加密 TOKEN
encrypt_token() {
    if ! require_main_service_config; then return; fi
    echo -e "${GREEN}===> 步骤 9: 安全强化 - 离线加密 ADMIN_TOKEN <===${NC}"
    cd "$_VW_APP_DIR" || { echo -e "${RED}❌ 主服务目录不存在${NC}"; return 1; }

    CURRENT_TOKEN=$(get_env_var "$_VW_ENV_FILE" "ADMIN_TOKEN")
    
    if [ -z "$CURRENT_TOKEN" ]; then
        echo -e "${RED}❌ 无法在配置中找到 ADMIN_TOKEN。${NC}"
        return
    fi

    if [[ "$CURRENT_TOKEN" == \$\$argon2* ]]; then
        echo -e "${GREEN}✅ ADMIN_TOKEN 已经是 Argon2 密文状态，无需重复加密。${NC}"
        return
    fi

    echo -e "${YELLOW}正在提取明文密码并通过无感安全沙箱生成高强度 Argon2 哈希...${NC}"
    
    ENCRYPTED_TOKEN=$(hash_admin_token_with_docker "$CURRENT_TOKEN" || true)
            
    if [[ -n "$ENCRYPTED_TOKEN" && "$ENCRYPTED_TOKEN" == \$argon2id* ]]; then
        ESCAPED_TOKEN=$(echo "$ENCRYPTED_TOKEN" | sed 's/\$/\$\$/g')
        set_env_var "$_VW_ENV_FILE" "ADMIN_TOKEN" "$ESCAPED_TOKEN"
        echo -e "${GREEN}✅ Token 自动离线加密成功！已安全覆盖原本的明文记录。${NC}"
    else
        echo -e "${RED}⚠️ 沙箱加密失败，回退至手动输入模式...${NC}"
        if ! docker ps --format '{{.Names}}' | grep -Fxq "$VW_CONTAINER_NAME"; then
            echo -e "${YELLOW}⚠️ 官方加密工具需要 Vaultwarden 处于运行状态，正在为您临时唤醒容器...${NC}"
            compose_up_dir "$_VW_APP_DIR" "$VW_CONTAINER_NAME"
            sleep 3
        fi
        
        echo -e "${YELLOW}===================================================================${NC}"
        echo -e "${GREEN} 请在稍后出现的 ${NC}Password:${GREEN} 提示后，手动盲打您的管理密码（输入两次）。${NC}"
        echo -e "${YELLOW}===================================================================${NC}"
        
        docker exec -it "$VW_CONTAINER_NAME" sh -c '/vaultwarden hash | tee /tmp/hash_output.txt'
        
        if docker exec "$VW_CONTAINER_NAME" test -f /tmp/hash_output.txt 2>/dev/null; then
            ENCRYPTED_TOKEN=$(docker exec "$VW_CONTAINER_NAME" cat /tmp/hash_output.txt | grep 'ADMIN_TOKEN=' | cut -d"'" -f2 | tr -d '\r')
            docker exec "$VW_CONTAINER_NAME" rm -f /tmp/hash_output.txt 2>/dev/null || true
        fi
        
        if [[ -n "$ENCRYPTED_TOKEN" ]]; then
            ESCAPED_TOKEN=$(echo "$ENCRYPTED_TOKEN" | sed 's/\$/\$\$/g')
            set_env_var "$_VW_ENV_FILE" "ADMIN_TOKEN" "$ESCAPED_TOKEN"
            echo -e "${GREEN}✅ Token 官方交互式加密成功！${NC}"
        else
            echo -e "${RED}❌ 提取失败，操作已取消。${NC}"
            return 1
        fi
    fi

    prompt_restart_vaultwarden_if_running \
        "是否立即重启主服务以使加密 Token 生效？(y/n): " \
        "${YELLOW}💡 当前容器未运行。稍后启动主服务时会自动应用密文 Token。${NC}"
    local restart_result=$?
    if [ "$restart_result" -eq 10 ]; then
        echo -e "${GREEN}🎉 重启完成，加密 Token 已全面接管！${NC}"
    fi
}

# 10. 修改主服务配置
modify_core_config() {
    if ! require_main_service_config; then
        return
    fi

    while true; do
        # 读取当前配置用于显示
        local _vw_env="$_VW_ENV_FILE"
        local _cur_domain _cur_token _cur_email _cur_signups _cur_invitations _cur_sends
        local _cur_ws _cur_webvault _cur_emergency _cur_wal _cur_rl_burst _cur_rl_sec _cur_session _cur_tz
        _cur_domain=$(get_env_var "$_vw_env" "DOMAIN" 2>/dev/null)
        _cur_token=$(get_env_var "$_vw_env" "ADMIN_TOKEN" 2>/dev/null)
        _cur_email=$(get_env_var "$_vw_env" "SERVER_ADMIN_EMAIL" 2>/dev/null)
        _cur_signups=$(get_env_var "$_vw_env" "SIGNUPS_ALLOWED" 2>/dev/null)
        _cur_invitations=$(get_env_var "$_vw_env" "INVITATIONS_ALLOWED" 2>/dev/null)
        _cur_sends=$(get_env_var "$_vw_env" "SENDS_ALLOWED" 2>/dev/null)
        _cur_ws=$(get_env_var "$_vw_env" "WEBSOCKET_ENABLED" 2>/dev/null)
        _cur_webvault=$(get_env_var "$_vw_env" "WEB_VAULT_ENABLED" 2>/dev/null)
        _cur_emergency=$(get_env_var "$_vw_env" "EMERGENCY_ACCESS_ALLOWED" 2>/dev/null)
        _cur_wal=$(get_env_var "$_vw_env" "ENABLE_DB_WAL" 2>/dev/null)
        _cur_rl_burst=$(get_env_var "$_vw_env" "ADMIN_RATELIMIT_MAX_BURST" 2>/dev/null)
        _cur_rl_sec=$(get_env_var "$_vw_env" "ADMIN_RATELIMIT_SECONDS" 2>/dev/null)
        _cur_session=$(get_env_var "$_vw_env" "ADMIN_SESSION_LIFETIME" 2>/dev/null)
        _cur_tz=$(get_env_var "$_vw_env" "TZ" 2>/dev/null)
        # 敏感字段特殊显示
        local _token_display="未设置"
        if [ -n "$_cur_token" ]; then
            if [[ "$_cur_token" == \$\$argon2* ]] || [[ "$_cur_token" == \$argon2* ]]; then
                _token_display="已加密"
            else
                _token_display="已设置(明文)"
            fi
        fi

        echo ""
        _breadcrumb "主菜单 > 主服务配置"
        echo -e "${CYAN}$(_hr)${NC}"
        echo -e "${CYAN}        Vaultwarden 主服务配置面板        ${NC}"
        echo -e "${CYAN}$(_hr)${NC}"
        echo -e " 1) DOMAIN (访问域名)                      ${YELLOW}[当前: ${_cur_domain:-未设置}]${NC}"
        echo -e " 2) ADMIN_TOKEN (管理后台密码 - 离线安全哈希) ${YELLOW}[当前: ${_token_display}]${NC}"
        echo -e " 3) SERVER_ADMIN_EMAIL (管理员邮箱)          ${YELLOW}[当前: ${_cur_email:-未设置}]${NC}"
        echo -e " 4) SIGNUPS_ALLOWED (是否允许新用户注册)     ${YELLOW}[当前: ${_cur_signups:-未设置}]${NC}"
        echo -e " 5) INVITATIONS_ALLOWED (是否允许邀请注册)   ${YELLOW}[当前: ${_cur_invitations:-未设置}]${NC}"
        echo -e " 6) SENDS_ALLOWED (开启 Send 文本/文件分享)  ${YELLOW}[当前: ${_cur_sends:-未设置}]${NC}"
        echo -e " 7) WEBSOCKET_ENABLED (开启 WebSocket)       ${YELLOW}[当前: ${_cur_ws:-未设置}]${NC}"
        echo -e " 8) WEB_VAULT_ENABLED (开启网页端访问)       ${YELLOW}[当前: ${_cur_webvault:-未设置}]${NC}"
        echo -e " 9) EMERGENCY_ACCESS_ALLOWED (开启紧急访问)  ${YELLOW}[当前: ${_cur_emergency:-未设置}]${NC}"
        echo -e "10) ENABLE_DB_WAL (开启数据库 WAL)           ${YELLOW}[当前: ${_cur_wal:-未设置}]${NC}"
        echo -e "11) ADMIN_RATELIMIT_MAX_BURST (防爆破-最大突发)${YELLOW}[当前: ${_cur_rl_burst:-未设置}]${NC}"
        echo -e "12) ADMIN_RATELIMIT_SECONDS (防爆破-检测周期) ${YELLOW}[当前: ${_cur_rl_sec:-未设置}]${NC}"
        echo -e "13) ADMIN_SESSION_LIFETIME (登录有效时间-分钟)${YELLOW}[当前: ${_cur_session:-未设置}]${NC}"
        echo -e "14) TIMEZONE (主服务时区)                    ${YELLOW}[当前: ${_cur_tz:-未设置}]${NC}"
        echo -e "${GREEN}15) 📧 配置 SMTP 邮件服务 (组合向导)${NC}"
        echo -e "${GREEN}16) 📱 配置官方 Push 推送${NC}"
        echo -e "${YELLOW} q) 返回主菜单${NC}"
        read -p "请输入要修改的配置项编号: " conf_choice

        if [[ "$conf_choice" == "q" || "$conf_choice" == "Q" ]]; then break; fi

        local key=""
        local val=""
        local is_token=false
        local target_file=""
        local CADDY_NEEDS_RELOAD=false 

        case $conf_choice in
            1) 
               key="DOMAIN"
               target_file="$_VW_ENV_FILE"
               read -p "输入新域名 (例: vw.abc.com, 直接回车取消): " val
               val=$(_trim "$val")
               if [ -n "$val" ]; then
                   if [[ ! "$val" == http* ]]; then val="https://$val"; fi
                   validate_domain_input "$val" || val=""
               fi
               ;;
            2) key="ADMIN_TOKEN"; target_file="$_VW_ENV_FILE"; is_token=true ;;
            3) 
               key="SERVER_ADMIN_EMAIL"
               target_file="$_VW_ENV_FILE"
               read -p "输入新邮箱 (直接回车取消): " val
               val=$(_trim "$val")
               if [ -n "$val" ]; then validate_email "$val" || val=""; fi
               ;;
            4) key="SIGNUPS_ALLOWED"; target_file="$_VW_ENV_FILE"; val=$(_ask_bool "是否允许新用户注册?") ;;
            5) key="INVITATIONS_ALLOWED"; target_file="$_VW_ENV_FILE"; val=$(_ask_bool "是否允许邀请注册?") ;;
            6) key="SENDS_ALLOWED"; target_file="$_VW_ENV_FILE"; val=$(_ask_bool "是否开启 Send 文本/文件分享功能?") ;;
            7) key="WEBSOCKET_ENABLED"; target_file="$_VW_ENV_FILE"; val=$(_ask_bool "是否开启 WebSocket 实时同步?") ;;
            8) key="WEB_VAULT_ENABLED"; target_file="$_VW_ENV_FILE"; val=$(_ask_bool "是否允许访问 Web 网页端?") ;;
            9) key="EMERGENCY_ACCESS_ALLOWED"; target_file="$_VW_ENV_FILE"; val=$(_ask_bool "是否开启紧急访问功能?") ;;
            10) key="ENABLE_DB_WAL"; target_file="$_VW_ENV_FILE"; val=$(_ask_bool "是否开启 SQLite WAL 性能优化?") ;;
            11) key="ADMIN_RATELIMIT_MAX_BURST"; target_file="$_VW_ENV_FILE"; read -p "最大突发次数 (直接回车取消): " val ;;
            12) key="ADMIN_RATELIMIT_SECONDS"; target_file="$_VW_ENV_FILE"; read -p "检测周期秒数 (直接回车取消): " val ;;
            13) key="ADMIN_SESSION_LIFETIME"; target_file="$_VW_ENV_FILE"; read -p "登录有效时间/分钟 (直接回车取消): " val ;;
            14) key="TIMEZONE"; read -p "主服务时区 例 Asia/Shanghai (直接回车取消): " val ;;
            15)
                echo -e "\n${CYAN}📧 SMTP 邮件服务配置向导${NC}"
                read -p "SMTP_HOST (如 smtp.qq.com, 回车跳过): " smtp_host
                read -p "SMTP_FROM (发件人邮箱, 回车跳过): " smtp_from
                read -p "SMTP_PORT (通常为 465 或 587, 回车跳过): " smtp_port
                read -p "SMTP_SECURITY (通常为 force_tls 或 starttls, 回车跳过): " smtp_security
                read -p "SMTP_USERNAME (邮箱账号, 回车跳过): " smtp_username
                read -s -p "SMTP_PASSWORD (授权码/密码, 不可见, 回车跳过): " smtp_password ; echo ""

                local smtp_updated=false
                local _vw_env_smtp="$_VW_ENV_FILE"
                _update_smtp_var "$_vw_env_smtp" "SMTP_HOST" "$smtp_host" && smtp_updated=true
                _update_smtp_var "$_vw_env_smtp" "SMTP_FROM" "$smtp_from" && smtp_updated=true
                if [ -n "$smtp_port" ] && ! validate_positive_integer "$smtp_port"; then
                    echo -e "${RED}❌ SMTP_PORT 必须是正整数，已跳过本次 SMTP_PORT 修改。${NC}"
                    smtp_port=""
                fi
                _update_smtp_var "$_vw_env_smtp" "SMTP_PORT" "$smtp_port" && smtp_updated=true
                _update_smtp_var "$_vw_env_smtp" "SMTP_SECURITY" "$smtp_security" && smtp_updated=true
                _update_smtp_var "$_vw_env_smtp" "SMTP_USERNAME" "$smtp_username" && smtp_updated=true
                _update_smtp_var "$_vw_env_smtp" "SMTP_PASSWORD" "$smtp_password" && smtp_updated=true

                if [ "$smtp_updated" = true ]; then
                    prompt_restart_vaultwarden_if_running "是否立即重启主服务以应用 SMTP 配置？(y/n): "
                    local restart_result=$?
                    if [ "$restart_result" -eq 10 ]; then
                        echo -e "${GREEN}🎉 主服务重启完成，SMTP 配置已生效。${NC}"
                        sleep 1
                    fi
                else
                    echo -e "${YELLOW}👉 未修改任何 SMTP 配置。${NC}"
                fi
                continue
                ;;
            16)
                setup_push
                continue
                ;;
            *) echo -e "${RED}无效选择！${NC}"; continue ;;
        esac

        if [ "$key" == "DOMAIN" ] && [ -z "$val" ]; then
            echo -e "${YELLOW}👉 域名格式无效或已取消修改。${NC}"
            continue
        fi

        if [ "$key" == "ADMIN_RATELIMIT_MAX_BURST" ] && [ -n "$val" ] && ! validate_positive_integer "$val"; then
            echo -e "${RED}❌ [ADMIN_RATELIMIT_MAX_BURST] 必须是正整数。${NC}"
            continue
        fi
        if [ "$key" == "ADMIN_RATELIMIT_SECONDS" ] && [ -n "$val" ] && ! validate_positive_integer "$val"; then
            echo -e "${RED}❌ [ADMIN_RATELIMIT_SECONDS] 必须是正整数。${NC}"
            continue
        fi
        if [ "$key" == "ADMIN_SESSION_LIFETIME" ] && [ -n "$val" ] && ! validate_positive_integer "$val"; then
            echo -e "${RED}❌ [ADMIN_SESSION_LIFETIME] 必须是正整数。${NC}"
            continue
        fi
        if [ "$key" == "TIMEZONE" ] && [ -n "$val" ] && ! validate_timezone_input "$val"; then
            echo -e "${RED}❌ [TIMEZONE] 格式不正确，请使用类似 Asia/Shanghai 的格式。${NC}"
            continue
        fi

        if [ "$is_token" = true ]; then
            read -p "⚠️ 即将重置管理后台密码，是否继续？(y/n, 默认y): " pwd_continue
            if [[ "$pwd_continue" == [nN]* ]]; then
                echo -e "${YELLOW}👉 已取消对密码的修改。${NC}"
                continue
            fi
            
            while true; do
                echo -e "\n${CYAN}🛡️ 企业级要求：8位以上 + 大写 + 小写 + 数字 + 特殊符号${NC}"
                read -s -p "输入新后台密码 (直接回车取消): " RAW_TOKEN ; echo ""
                if [ -z "$RAW_TOKEN" ]; then val=""; break; fi
                
                if validate_password "$RAW_TOKEN"; then
                    read -s -p "再次输入确认新密码: " RAW_TOKEN_CONFIRM ; echo ""
                    if [ "$RAW_TOKEN" == "$RAW_TOKEN_CONFIRM" ]; then
                        echo -e "${YELLOW}正在通过无感沙箱生成哈希...${NC}"
                        ENCRYPTED_TOKEN=$(hash_admin_token_with_docker "$RAW_TOKEN" || true)
                        
                        if [[ -n "$ENCRYPTED_TOKEN" && "$ENCRYPTED_TOKEN" == \$argon2id* ]]; then
                            val=$(echo "$ENCRYPTED_TOKEN" | sed 's/\$/\$\$/g')
                            echo -e "${GREEN}✅ 新密码离线加密完成！${NC}"
                            break
                        else
                            echo -e "${RED}⚠️ 自动沙箱失败。请确保主服务已运行，我们将借用官方工具...${NC}"
                            if ! docker ps --format '{{.Names}}' | grep -Fxq "$VW_CONTAINER_NAME"; then
                                echo -e "${RED}❌ 严重错误：修改后台密码必须借助【运行中】的 Vaultwarden 容器！${NC}"
                                echo -e "${YELLOW}👉 请先执行主菜单的启动确保服务运行。${NC}"
                                val=""; break
                            fi
                            
                            docker exec -it "$VW_CONTAINER_NAME" sh -c '/vaultwarden hash | tee /tmp/hash_output.txt'
                            if docker exec "$VW_CONTAINER_NAME" test -f /tmp/hash_output.txt 2>/dev/null; then
                                ENCRYPTED_TOKEN=$(docker exec "$VW_CONTAINER_NAME" cat /tmp/hash_output.txt | grep 'ADMIN_TOKEN=' | cut -d"'" -f2 | tr -d '\r')
                                docker exec "$VW_CONTAINER_NAME" rm -f /tmp/hash_output.txt 2>/dev/null || true
                                val=$(echo "$ENCRYPTED_TOKEN" | sed 's/\$/\$\$/g')
                                break
                            else
                                echo -e "${RED}❌ 密码提取失败，操作已取消。${NC}"
                                val=""; break
                            fi
                        fi
                    else
                        echo -e "${RED}❌ 两次输入不一致！${NC}"
                    fi
                fi
            done
        fi

        if [ -z "$val" ]; then 
            echo -e "${YELLOW}👉 已取消对 [$key] 的修改。${NC}"
            continue 
        fi

        if [ "$key" == "DOMAIN" ]; then
            OLD_DOMAIN_FULL=$(get_env_var "$_VW_ENV_FILE" "DOMAIN")
            OLD_DOMAIN=$(extract_domain_host "$OLD_DOMAIN_FULL")
            NEW_DOMAIN=$(extract_domain_host "$val")
            set_env_var "$target_file" "$key" "$val"

            if [ "$PROXY_MODE" == "docker_ext" ]; then
                echo -e "${YELLOW}💡 当前服务处于【纯内网容器桥接模式】。${NC}"
                echo -e "${GREEN}👉 核心配置已更新。请务必前往您的外部反代面板 (如 1Panel/NPM)，将反代域名同步修改为: ${NEW_DOMAIN}，目标地址保持 http://vaultwarden:80 不变。${NC}"
            elif [ "$PROXY_MODE" == "host_ext" ]; then
                echo -e "${YELLOW}💡 当前服务处于【宿主机本地映射模式】。${NC}"
                echo -e "${GREEN}👉 核心配置已更新。请务必前往您的宿主机反代工具 (如 宝塔/Nginx)，将反代域名同步修改为: ${NEW_DOMAIN}，目标地址保持 http://127.0.0.1:$EXT_PORT 不变。${NC}"
            elif [ "$PROXY_MODE" == "caddy" ]; then
                load_caddy_runtime
                if [ -n "$CADDY_DIR" ]; then
                    if [ -n "$OLD_DOMAIN" ] && [ "$OLD_DOMAIN" != "$NEW_DOMAIN" ]; then
                        remove_caddy_managed_block "$OLD_DOMAIN"
                    fi
                    write_caddy_managed_block "$NEW_DOMAIN"
                    echo -e "${GREEN}✅ Caddy 受控反代规则已同步更新 (${OLD_DOMAIN:-无} -> $NEW_DOMAIN)${NC}"
                    CADDY_NEEDS_RELOAD=true
                else
                    echo -e "${RED}⚠️ 未能自动定位到 Caddyfile。请手动前往修改！${NC}"
                fi
            fi
        elif [ "$key" == "TIMEZONE" ]; then
            set_env_var "$_VW_ENV_FILE" "TZ" "$val"
        else
            set_env_var "$target_file" "$key" "$val"
        fi
        
        if is_secret_key "$key"; then
            echo -e "${GREEN}✅ 变量 [$key] 已在配置文件中成功更新（敏感值已隐藏）。${NC}"
        else
            echo -e "${GREEN}✅ 变量 [$key] 已在配置文件中成功更新为: $val${NC}"
        fi
        
        prompt_restart_vaultwarden_if_running "是否立即重启主服务以应用最新配置？(y/n): "
        local restart_result=$?
        if [ "$restart_result" -eq 10 ]; then
            if [ "$CADDY_NEEDS_RELOAD" = true ]; then
                echo -e "${YELLOW}正在热重载内置 Caddy 反向代理...${NC}"
                reload_caddy_runtime || true
            fi
            echo -e "${GREEN}🎉 主服务重启完成，新配置已生效。${NC}"
            sleep 1
        fi
    done
}

# 11. 修改备份服务配置
modify_backup_config() {
    local key=""
    local val=""
    local restart_backup=""
    local mail_state_label=""

    if ! require_backup_service_config; then
        return
    fi

    while true; do
        if backup_mail_enabled; then
            mail_state_label="已启用"
        else
            mail_state_label="未启用"
        fi
        echo ""
        _breadcrumb "主菜单 > 备份服务配置"
        echo -e "${CYAN}$(_hr)${NC}"
        echo -e "${CYAN}        Vaultwarden 备份服务配置面板      ${NC}"
        echo -e "${CYAN}$(_hr)${NC}"
        # 读取当前备份配置用于显示
        local _bk_env="$_BACKUP_ENV_FILE"
        local _cur_zip_pw _cur_keep_days _cur_bk_tz _cur_cron
        _cur_zip_pw=$(get_env_var "$_bk_env" "ZIP_PASSWORD" 2>/dev/null)
        _cur_keep_days=$(get_env_var "$_bk_env" "BACKUP_KEEP_DAYS" 2>/dev/null)
        _cur_bk_tz=$(get_env_var "$_bk_env" "TIMEZONE" 2>/dev/null)
        _cur_cron=$(get_env_var "$_bk_env" "CRON" 2>/dev/null)
        local _zip_pw_display="未设置"
        [ -n "$_cur_zip_pw" ] && _zip_pw_display="已设置"
        echo -e " 1) ZIP_PASSWORD (备份压缩包密码)      ${YELLOW}[当前: ${_zip_pw_display}]${NC}"
        echo -e " 2) BACKUP_KEEP_DAYS (备份保留天数)    ${YELLOW}[当前: ${_cur_keep_days:-未设置}]${NC}"
        echo -e " 3) TIMEZONE (备份服务时区)            ${YELLOW}[当前: ${_cur_bk_tz:-未设置}]${NC}"
        echo " 4) REMOTES (重新配置远程上传目标)"
        echo " 5) MAIL 通知（当前: ${mail_state_label}）"
        if [ -n "$_cur_cron" ]; then
            echo -e "    ${YELLOW}📅 当前备份计划: ${_cur_cron}${NC}"
        fi
        echo -e "${YELLOW} q) 返回主菜单${NC}"
        read -p "请输入要修改的配置项编号: " conf_choice

        if [[ "$conf_choice" == "q" || "$conf_choice" == "Q" ]]; then break; fi

        key=""
        val=""

        case $conf_choice in
            1)
                key="ZIP_PASSWORD"
                read -s -p "新备份压缩包密码 (盲打，直接回车取消): " val ; echo ""
                if [ -n "$val" ]; then validate_password "$val" || val=""; fi
                ;;
            2)
                key="BACKUP_KEEP_DAYS"
                read -p "备份保留天数 (直接回车取消): " val
                ;;
            3)
                key="TIMEZONE"
                read -p "备份服务时区 例 Asia/Shanghai (直接回车取消): " val
                ;;
            4)
                if configure_backup_remote_targets; then
                    prompt_restart_backup_if_running "是否立即重启备份服务以应用最新远程目标配置？(y/n): "
                    local restart_result=$?
                    if [ "$restart_result" -eq 10 ]; then
                        echo -e "${GREEN}🎉 备份服务重启完成，远程上传目标配置已生效。${NC}"
                        sleep 1
                    fi
                else
                    echo -e "${YELLOW}👉 已取消远程上传目标重配置。${NC}"
                fi
                continue
                ;;
            5)
                configure_backup_mail_notifications
                continue
                ;;
            *)
                print_invalid_choice
                continue
                ;;
        esac

        if [ -z "$val" ]; then
            echo -e "${YELLOW}👉 已取消对 [$key] 的修改。${NC}"
            continue
        fi

        if [ "$key" == "BACKUP_KEEP_DAYS" ] && ! validate_non_negative_integer "$val"; then
            echo -e "${RED}❌ [BACKUP_KEEP_DAYS] 必须是非负整数。${NC}"
            continue
        fi
        if [ "$key" == "TIMEZONE" ] && ! validate_timezone_input "$val"; then
            echo -e "${RED}❌ [TIMEZONE] 格式不正确，请使用类似 Asia/Shanghai 的格式。${NC}"
            continue
        fi

        set_env_var "$_BACKUP_ENV_FILE" "$key" "$val"
        if is_secret_key "$key"; then
            echo -e "${GREEN}✅ 变量 [$key] 已在备份配置中成功更新（敏感值已隐藏）。${NC}"
        else
            echo -e "${GREEN}✅ 变量 [$key] 已在备份配置中成功更新为: $val${NC}"
        fi

        prompt_restart_backup_if_running "是否立即重启备份服务以应用最新配置？(y/n): "
        local restart_result=$?
        if [ "$restart_result" -eq 10 ]; then
            echo -e "${GREEN}🎉 备份服务重启完成，新配置已生效。${NC}"
            sleep 1
        fi
    done
}

# 7. 官方 Push 推送管理
setup_push() {
    if ! require_main_service_config; then return; fi
    while true; do
        echo -e "\n${CYAN}$(_hr)${NC}"
        echo -e "${CYAN}        官方 Push 推送管理面板 (多端同步)       ${NC}"
        echo -e "${CYAN}$(_hr)${NC}"
        if service_running "$VW_CONTAINER_NAME"; then
            echo -e "主服务运行状态: ${GREEN}✅ 运行中${NC}"
        else
            echo -e "主服务运行状态: ${RED}❌ 未启动${NC}"
        fi
        if [ "$(get_env_var "$_VW_ENV_FILE" "PUSH_ENABLED")" == "true" ]; then
            echo -e "Push 功能状态:  ${GREEN}✅ 已开启${NC}"
        else
            echo -e "Push 功能状态:  ${YELLOW}⚠️ 未开启${NC}"
        fi
        echo -e "\n 1) ➕ 新增或修改 Push 凭证"
        echo -e " 2) 🗑️  一键关闭并删除 Push 配置"
        echo -e "${YELLOW} q) 返回主菜单${NC}"
        read -p "请选择操作 [1, 2, q]: " push_choice

        case $push_choice in
            1)
                read -p "请输入 PUSH_INSTALLATION_ID (直接回车取消): " PUSH_ID
                if [ -z "$PUSH_ID" ]; then continue; fi
                read -s -p "请输入 PUSH_INSTALLATION_KEY (直接回车取消): " PUSH_KEY ; echo ""
                if [ -z "$PUSH_KEY" ]; then continue; fi
                
                cd "$_VW_APP_DIR" || { echo -e "${RED}❌ 主服务目录不存在${NC}"; continue; }
                set_env_var "$_VW_ENV_FILE" "PUSH_ENABLED" "true"
                set_env_var "$_VW_ENV_FILE" "PUSH_INSTALLATION_ID" "$PUSH_ID"
                set_env_var "$_VW_ENV_FILE" "PUSH_INSTALLATION_KEY" "$PUSH_KEY"
                
                echo -e "${GREEN}✅ 凭证已注入配置文件（敏感值已隐藏）。${NC}"
                if service_running "$VW_CONTAINER_NAME"; then
                    echo -e "${YELLOW}正在热重启 Vaultwarden...${NC}"
                    restart_vaultwarden_service_apply
                    echo -e "${GREEN}🎉 Push 推送服务已激活！${NC}"
                fi
                ;;
            2)
                cd "$_VW_APP_DIR" || { echo -e "${RED}❌ 主服务目录不存在${NC}"; continue; }
                if [ -n "$(get_env_var "$_VW_ENV_FILE" "PUSH_ENABLED")" ]; then
                    delete_env_var "$_VW_ENV_FILE" "PUSH_ENABLED"
                    delete_env_var "$_VW_ENV_FILE" "PUSH_INSTALLATION_ID"
                    delete_env_var "$_VW_ENV_FILE" "PUSH_INSTALLATION_KEY"
                    echo -e "${GREEN}✅ 已彻底从配置文件中抹除 Push 数据。${NC}"
                    if service_running "$VW_CONTAINER_NAME"; then
                        restart_vaultwarden_service_apply
                    fi
                    echo -e "${GREEN}🎉 Push 功能已成功关闭！${NC}"
                else
                    echo -e "${YELLOW}👉 Push 功能当前未开启，无需删除。${NC}"
                fi
                ;;
            q|Q) break ;;
            *) print_invalid_choice ;;
        esac
    done
}

backup_mail_enabled() {
    [ "$(get_env_var "$_BACKUP_ENV_FILE" "MAIL_SMTP_ENABLE")" = "true" ]
}

escape_shell_single_quotes() {
    printf "%s" "$1" | sed "s/'/'\"'\"'/g"
}

build_backup_mail_smtp_variables() {
    local smtp_host="$1"
    local smtp_port="$2"
    local smtp_security="$3"
    local smtp_user="$4"
    local smtp_password="$5"
    local smtp_from="$6"
    local smtp_uri=""
    local smtp_uri_escaped=""
    local smtp_user_escaped=""
    local smtp_password_escaped=""
    local smtp_from_escaped=""
    local parts=()
    local i=""

    case "$smtp_security" in
        ssl)
            smtp_uri="smtps://${smtp_host}:${smtp_port}"
            ;;
        starttls|none)
            smtp_uri="smtp://${smtp_host}:${smtp_port}"
            ;;
        *)
            return 1
            ;;
    esac

    smtp_uri_escaped=$(escape_shell_single_quotes "$smtp_uri")
    smtp_user_escaped=$(escape_shell_single_quotes "$smtp_user")
    smtp_password_escaped=$(escape_shell_single_quotes "$smtp_password")
    smtp_from_escaped=$(escape_shell_single_quotes "$smtp_from")

    if [ "$smtp_security" = "starttls" ]; then
        parts+=("-S smtp-use-starttls")
    fi
    parts+=("-S smtp='${smtp_uri_escaped}'")
    parts+=("-S smtp-auth=login")
    parts+=("-S smtp-auth-user='${smtp_user_escaped}'")
    parts+=("-S smtp-auth-password='${smtp_password_escaped}'")
    parts+=("-S from='${smtp_from_escaped}'")

    for i in "${!parts[@]}"; do
        if [ "$i" -gt 0 ]; then
            printf ' '
        fi
        printf '%s' "${parts[$i]}"
    done
    printf '\n'
}

delete_backup_mail_config() {
    local key=""
    for key in \
        MAIL_SMTP_ENABLE \
        MAIL_TO \
        MAIL_WHEN_SUCCESS \
        MAIL_WHEN_FAILURE \
        MAIL_FORCE_THREAD \
        MAIL_SMTP_VARIABLES; do
        delete_env_var "$_BACKUP_ENV_FILE" "$key"
    done
}

test_backup_mail_notification() {
    local test_mail_to=""
    local test_output=""
    local test_status=0

    if ! require_backup_service_config; then
        return 1
    fi

    if ! backup_mail_enabled; then
        echo -e "${YELLOW}⚠️ 当前还没有启用备份 Mail 通知。${NC}"
        echo -e "${YELLOW}👉 请先完成 Mail 通知配置，再执行测试发送。${NC}"
        return 1
    fi

    test_mail_to=$(get_env_var "$_BACKUP_ENV_FILE" "MAIL_TO")
    if [ -z "$test_mail_to" ]; then
        echo -e "${RED}❌ 当前未检测到收件邮箱 [MAIL_TO]，无法发送测试邮件。${NC}"
        return 1
    fi

    echo -e "${YELLOW}正在向 [$test_mail_to] 发送测试邮件，请稍候...${NC}"
    test_output=$(docker run --rm \
        --env-file "$_BACKUP_ENV_FILE" \
        --entrypoint sh \
        ttionya/vaultwarden-backup:latest \
        -ec '
            [ "$MAIL_SMTP_ENABLE" = "true" ] || {
                echo "MAIL_SMTP_ENABLE 未开启"
                exit 1
            }
            [ -n "$MAIL_TO" ] || {
                echo "MAIL_TO 未设置"
                exit 1
            }
            [ -n "$MAIL_SMTP_VARIABLES" ] || {
                echo "MAIL_SMTP_VARIABLES 未设置"
                exit 1
            }

            subject="Vaultwarden Backup Test Mail"
            body="This is a test message from vaultwarden-backup."
            printf "%s\n" "$body" | sh -c "exec s-nail $MAIL_SMTP_VARIABLES -s \"$subject\" \"$MAIL_TO\""
        ' 2>&1) || test_status=$?

    if [ "$test_status" -ne 0 ]; then
        echo -e "${RED}❌ 测试邮件发送失败。${NC}"
        [ -n "$test_output" ] && echo "$test_output"
        return 1
    fi

    echo -e "${GREEN}✅ 测试邮件已发送，请检查收件箱（以及垃圾邮件箱）。${NC}"
    return 0
}

configure_backup_mail_notifications() {
    local mail_choice=""
    local mail_enable=""
    local mail_to=""
    local mail_when_success=""
    local mail_when_failure=""
    local mail_force_thread=""
    local smtp_host=""
    local smtp_port=""
    local smtp_from=""
    local smtp_user=""
    local smtp_password=""
    local smtp_security=""
    local smtp_security_choice=""
    local mail_smtp_variables=""

    if ! require_backup_service_config; then
        return 1
    fi

    while true; do
        echo ""
        _breadcrumb "主菜单 > 备份服务配置 > Mail 通知"
        echo -e "${CYAN}$(_hr)${NC}"
        echo -e "${CYAN}      备份 Mail 通知管理面板             ${NC}"
        echo -e "${CYAN}$(_hr)${NC}"
        if backup_mail_enabled; then
            echo -e "当前状态: ${GREEN}✅ 已启用${NC}"
            echo -e "收件邮箱: ${GREEN}$(get_env_var "$_BACKUP_ENV_FILE" "MAIL_TO")${NC}"
        else
            echo -e "当前状态: ${YELLOW}⚠️ 未启用${NC}"
        fi
        echo " 1) 配置/修改 Mail 通知"
        echo " 2) 发送测试邮件"
        echo " 3) 关闭并清理 Mail 通知"
        echo -e "${YELLOW} q) 返回上一级${NC}"
        read -p "请选择操作 [1, 2, 3, q]: " mail_choice

        case "$mail_choice" in
            1)
                echo -e "\n${CYAN}Mail 通知配置向导${NC}"
                echo -e "${YELLOW}💡 如果您使用 Gmail，推荐填写方式如下：${NC}"
                echo -e "${YELLOW}👉 SMTP_HOST = smtp.gmail.com${NC}"
                echo -e "${YELLOW}👉 SMTP_PORT = 587${NC}"
                echo -e "${YELLOW}👉 发件邮箱 / 登录用户名 = 您的 Gmail 地址${NC}"
                echo -e "${YELLOW}👉 登录密码 = Google 应用专用密码（不是邮箱登录密码）${NC}"
                echo -e "${YELLOW}👉 加密方式 = STARTTLS${NC}"
                read -p "是否启用备份 Mail 通知？(y/n, 默认y): " mail_enable
                if [[ "$mail_enable" == [nN]* ]]; then
                    echo -e "${YELLOW}👉 已取消本次 Mail 通知配置。${NC}"
                    continue
                fi

                while true; do
                    read -p "收件邮箱 MAIL_TO [用于接收通知，可填 yourname@gmail.com]: " mail_to
                    if validate_email "$mail_to"; then
                        break
                    fi
                done

                read -p "备份成功时是否发送通知？(y/n, 默认y): " mail_when_success
                if [[ "$mail_when_success" == [nN]* ]]; then
                    mail_when_success="false"
                else
                    mail_when_success="true"
                fi

                read -p "备份失败时是否发送通知？(y/n, 默认y): " mail_when_failure
                if [[ "$mail_when_failure" == [nN]* ]]; then
                    mail_when_failure="false"
                else
                    mail_when_failure="true"
                fi

                read -p "是否强制将通知归入同一邮件线程？(y/n, 默认y，Gmail 推荐 y): " mail_force_thread
                if [[ "$mail_force_thread" == [nN]* ]]; then
                    mail_force_thread="false"
                else
                    mail_force_thread="true"
                fi

                echo -e "\n${CYAN}SMTP 连接信息${NC}"
                read -p "SMTP_HOST (如 smtp.gmail.com / smtp.qq.com): " smtp_host
                [ -n "$smtp_host" ] || {
                    echo -e "${RED}❌ SMTP_HOST 不能为空。${NC}"
                    continue
                }

                read -p "SMTP_PORT (常见 587=STARTTLS, 465=SSL/TLS): " smtp_port
                if ! validate_positive_integer "$smtp_port"; then
                    echo -e "${RED}❌ SMTP_PORT 必须是正整数。${NC}"
                    continue
                fi

                while true; do
                    read -p "发件邮箱 From [Gmail 通常填您自己的 Gmail 地址]: " smtp_from
                    if validate_email "$smtp_from"; then
                        break
                    fi
                done

                read -p "SMTP 登录用户名 [默认与发件邮箱相同；Gmail 通常也是完整邮箱地址]: " smtp_user
                smtp_user=${smtp_user:-$smtp_from}

                read -s -p "SMTP 登录密码 / 授权码 (输入不可见；Gmail 请填应用专用密码): " smtp_password ; echo ""
                if [ -z "$smtp_password" ]; then
                    echo -e "${RED}❌ SMTP 密码不能为空。${NC}"
                    continue
                fi

                echo "请选择 SMTP 加密方式:"
                echo " 1) STARTTLS (常见 587，Gmail 推荐)"
                echo " 2) SSL/TLS (常见 465)"
                echo " 3) 无加密"
                read -p "请输入选项 [1, 2, 3]: " smtp_security_choice
                case "$smtp_security_choice" in
                    1) smtp_security="starttls" ;;
                    2) smtp_security="ssl" ;;
                    3) smtp_security="none" ;;
                    *)
                        print_invalid_choice
                        continue
                        ;;
                esac

                mail_smtp_variables=$(build_backup_mail_smtp_variables \
                    "$smtp_host" \
                    "$smtp_port" \
                    "$smtp_security" \
                    "$smtp_user" \
                    "$smtp_password" \
                    "$smtp_from") || {
                    echo -e "${RED}❌ 构建 MAIL_SMTP_VARIABLES 失败，请检查输入后重试。${NC}"
                    continue
                }

                set_env_var "$_BACKUP_ENV_FILE" "MAIL_SMTP_ENABLE" "true"
                set_env_var "$_BACKUP_ENV_FILE" "MAIL_TO" "$mail_to"
                set_env_var "$_BACKUP_ENV_FILE" "MAIL_WHEN_SUCCESS" "$mail_when_success"
                set_env_var "$_BACKUP_ENV_FILE" "MAIL_WHEN_FAILURE" "$mail_when_failure"
                set_env_var "$_BACKUP_ENV_FILE" "MAIL_FORCE_THREAD" "$mail_force_thread"
                set_env_var "$_BACKUP_ENV_FILE" "MAIL_SMTP_VARIABLES" "$mail_smtp_variables"

                echo -e "${GREEN}✅ Mail 通知配置已写入备份配置文件。${NC}"
                echo -e "${GREEN}✅ 收件邮箱: ${mail_to}${NC}"
                echo -e "${GREEN}✅ 成功通知: ${mail_when_success}${NC}"
                echo -e "${GREEN}✅ 失败通知: ${mail_when_failure}${NC}"
                echo -e "${GREEN}✅ 线程归并: ${mail_force_thread}${NC}"

                prompt_restart_backup_if_running "是否立即重启备份服务以应用 Mail 通知配置？(y/n): "
                local restart_result=$?
                if [ "$restart_result" -eq 10 ]; then
                    echo -e "${GREEN}🎉 备份服务重启完成，Mail 通知配置已生效。${NC}"
                fi
                ;;
            2)
                test_backup_mail_notification || true
                ;;
            3)
                if backup_mail_enabled; then
                    delete_backup_mail_config
                    echo -e "${GREEN}✅ 备份 Mail 通知配置已清理。${NC}"
                    prompt_restart_backup_if_running "是否立即重启备份服务以关闭 Mail 通知？(y/n): "
                    local restart_result=$?
                    if [ "$restart_result" -eq 10 ]; then
                        echo -e "${GREEN}🎉 备份服务重启完成，Mail 通知已关闭。${NC}"
                    fi
                else
                    echo -e "${YELLOW}👉 当前尚未启用 Mail 通知，无需清理。${NC}"
                fi
                ;;
            q|Q)
                return 0
                ;;
            *)
                print_invalid_choice
                ;;
        esac
    done
}

# 8. 校验备份
test_backup() {
    local latest_zip_after=""

    if ! backup_service_initialized; then
        echo -e "${YELLOW}⚠️ 当前还没有检测到备份服务基础配置。${NC}"
        echo -e "${YELLOW}👉 请先执行步骤 2，初始化备份服务。${NC}"
        return 1
    fi
    if ! backup_remote_configured; then
        echo -e "${YELLOW}⚠️ 当前还没有完成远程存储配置。${NC}"
        echo -e "${YELLOW}👉 请先执行步骤 3，完成 Rclone 远程存储配置。${NC}"
        return 1
    fi
    if [ "$(docker ps -q -f "name=$VW_BACKUP_CONTAINER_NAME")" ]; then
        echo -e "${CYAN}🧪 手动备份执行中${NC}"
        echo -e "${YELLOW}👉 接下来会立即触发一次备份任务，并在完成后给出最新备份诊断。${NC}"
        if ! docker exec -it "$VW_BACKUP_CONTAINER_NAME" /app/backup.sh; then
            echo -e "${RED}❌ 手动备份执行失败，请先查看备份容器日志。${NC}"
            latest_zip_after=$(latest_backup_zip)
            [ -n "$latest_zip_after" ] && show_backup_remote_delivery_status "$latest_zip_after" || true
            return 1
        fi
        latest_zip_after=$(latest_backup_zip)
        show_backup_remote_delivery_status "$latest_zip_after" || true
        show_backup_diagnostics || true
    else
        echo -e "${YELLOW}⚠️ 当前备份服务还没有运行。${NC}"
        echo -e "${YELLOW}👉 请先执行步骤 6 启动备份服务，然后再回来手动执行一次备份。${NC}"
    fi
}

# 🌟 体验升级 4: 实时运行日志查看器
view_logs() {
    echo ""
    _breadcrumb "主菜单 > 服务日志"
    echo -e "${CYAN}$(_hr)${NC}"
    echo -e "${CYAN}      实时运行日志探测器 (按 Ctrl+C 退出)     ${NC}"
    echo -e "${CYAN}$(_hr)${NC}"
    echo " 1) 📜 查看 Vaultwarden 核心日志"
    echo " 2) 📜 查看 Backup 备份服务日志"
    echo " 3) 📜 查看 Caddy 代理服务日志"
    echo -e "${YELLOW} q) 返回主菜单${NC}"
    read -p "请选择要查看的日志 [1-3, q]: " log_choice
    case $log_choice in
        1)
           if service_running "$VW_CONTAINER_NAME"; then
               _view_container_log "$VW_CONTAINER_NAME" "Vaultwarden 主服务"
           elif [ -f "$_VW_COMPOSE_FILE" ]; then
               echo -e "${YELLOW}⚠️ 当前主服务配置已存在，但 Vaultwarden 还没有运行。${NC}"
               echo -e "${YELLOW}👉 请先执行步骤 4 启动主服务，然后再查看日志。${NC}"
           else
               print_missing_config_message "主服务配置" "1" "生成主服务配置"
           fi
           ;;
        2)
           if ! backup_service_initialized; then
               print_missing_config_message "备份服务配置" "2" "初始化备份服务"
           elif ! backup_remote_configured; then
               echo -e "${YELLOW}⚠️ 当前备份服务还没有完成远程存储配置。${NC}"
               echo -e "${YELLOW}👉 请先执行步骤 3 完成 Rclone 配置。${NC}"
           elif service_running "$VW_BACKUP_CONTAINER_NAME"; then
               _view_container_log "$VW_BACKUP_CONTAINER_NAME" "备份服务"
           else
               echo -e "${YELLOW}⚠️ 当前备份服务配置已存在，但容器还没有运行。${NC}"
               echo -e "${YELLOW}👉 请先执行步骤 6 启动备份服务，然后再查看日志。${NC}"
           fi
           ;;
        3) 
           _CADDY_ID=$(find_caddy_container_id)
           if [ -n "$_CADDY_ID" ]; then
               _view_container_log "$_CADDY_ID" "Caddy 代理"
           elif [ "$PROXY_MODE" == "docker_ext" ] || [ "$PROXY_MODE" == "host_ext" ]; then
               echo -e "${YELLOW}⚠️ 当前并不是 Caddy 代理模式，因此这里没有可查看的 Caddy 日志。${NC}"
           else
               echo -e "${YELLOW}⚠️ 当前没有检测到运行中的 Caddy 容器。${NC}"
           fi
           ;;
        q|Q) return ;;
        *) print_invalid_choice ;;
    esac
}

# 10. 查看访问地址 (专属流派保姆级向导)
show_info() {
    echo -e "\n${CYAN}🌐 访问信息与状态摘要${NC}"

    if [ -z "$VW_DIR" ] || [ ! -f "$_VW_COMPOSE_FILE" ]; then
        print_missing_config_message "主服务配置" "1" "生成主服务配置"
        echo -e "${YELLOW}👉 完成后，这里会显示访问域名、管理后台地址和运行状态。${NC}"
        echo -e ""
        return
    fi

    CURRENT_DOMAIN=$(get_env_var "$_VW_ENV_FILE" "DOMAIN")
    echo -e "${YELLOW}目标访问域名:${NC} ${GREEN}$CURRENT_DOMAIN${NC}"
    CURRENT_DOMAIN_HOST=$(extract_domain_host "$CURRENT_DOMAIN")
    CURRENT_DOMAIN_IP=$(resolve_domain_ipv4 "$CURRENT_DOMAIN_HOST")
    CURRENT_PUBLIC_IP=$(get_public_ipv4)
    
    # 针对不同反代模式给予保姆级连接向导
    if [ "$PROXY_MODE" == "docker_ext" ]; then
        echo -e "\n${CYAN}🛠️ 外部反代配置指南:${NC}"
        echo -e "👉 当前运行模式：纯内网容器桥接。"
        echo -e "👉 请在您的容器化反代面板（如 1Panel / NPM）中，将目标地址填写为：${GREEN}http://vaultwarden:80${NC}"
        echo -e "👉 请确认反代面板与 Vaultwarden 处于同一个 Docker 网络 [${GREEN}$DOCKER_NET${NC}]。"
        echo -e "👉 域名当前解析: ${GREEN}${CURRENT_DOMAIN_IP:-未知}${NC} / 本机公网 IP: ${GREEN}${CURRENT_PUBLIC_IP:-未知}${NC}"
    elif [ "$PROXY_MODE" == "host_ext" ]; then
        echo -e "\n${CYAN}🛠️ 外部反代配置指南:${NC}"
        echo -e "👉 当前运行模式：宿主机本地端口映射。"
        echo -e "👉 请在宿主机反代工具（如 宝塔 / Nginx）中，将目标地址填写为：${GREEN}http://127.0.0.1:$EXT_PORT${NC}"
        echo -e "👉 域名当前解析: ${GREEN}${CURRENT_DOMAIN_IP:-未知}${NC} / 本机公网 IP: ${GREEN}${CURRENT_PUBLIC_IP:-未知}${NC}"
    else
        echo -e "${YELLOW}管理后台地址:${NC} ${GREEN}$CURRENT_DOMAIN/admin${NC}"
    fi
    
    TOKEN_CHECK=$(get_env_var "$_VW_ENV_FILE" "ADMIN_TOKEN")
    if [[ "$TOKEN_CHECK" == \$\$argon2* ]]; then
        echo -e "${YELLOW}管理后台状态:${NC} ${GREEN}[密码已安全离线加密]${NC}"
    else
        echo -e "${YELLOW}管理后台状态:${NC} ${RED}[当前仍为明文配置，建议执行步骤 9 加密]${NC}"
    fi

    echo -e ""
    show_backup_diagnostics || true
    echo -e ""
}

# 【闭环补全】：11. 灾难恢复指南 (Restore)
restore_guide() {
    local latest_zip=""
    local restore_mode=""

    echo -e "\n${RED}$(_hr)${NC}"
    echo -e "${RED}       🚑 灾难恢复 (Restore) 抢救指南       ${NC}"
    echo -e "${RED}$(_hr)${NC}"
    echo -e "${YELLOW}如果您遇到了数据库损坏或数据丢失，请按照以下步骤使用云端备份还原：${NC}\n"

    if [ -z "$VW_DIR" ] || [ ! -d "$_VW_APP_DIR" ]; then
        echo -e "${YELLOW}⚠️ 当前还没有检测到可恢复的 Vaultwarden 本地部署目录。${NC}"
        echo -e "${YELLOW}👉 请先完成主服务部署；如果您是准备手动恢复历史数据，也请先执行步骤 1 生成主服务目录结构。${NC}"
        echo -e "${CYAN}====================================================${NC}"
        return
    fi

    echo -e "${CYAN}恢复前快速体检:${NC}"
    show_backup_diagnostics || true
    echo ""
    echo -e "${CYAN}备份文件放置目录:${NC}"
    echo -e "${GREEN}$_VW_BACKUP_ARCHIVES_DIR/${NC}"
    echo -e "${YELLOW}👉 如果您要使用本地自动恢复，请先把备份 .zip 上传到这个目录。${NC}"
    echo ""

    latest_zip=$(latest_backup_zip)
    if [ -n "$latest_zip" ]; then
        echo -e "${GREEN}✅ 已检测到本地备份包。您现在可以直接选择两种恢复方式：${NC}"
        echo -e "${GREEN}   1) 本地自动恢复${NC}"
        echo -e "${GREEN}   2) 官方高级恢复（调用上游 restore 引擎）${NC}"
        echo ""
        while true; do
            echo -e "${CYAN}恢复方式选择:${NC}"
            echo -e " 1) 本地自动恢复（推荐）"
            echo -e " 2) 官方高级恢复（严格跟随上游 restore 逻辑）"
            echo -e " q) 跳过自动恢复助手，继续查看手动恢复说明"
            read -p "请选择恢复方式 [1, 2, q]: " restore_mode

            case "$restore_mode" in
                1)
                    if ! restore_from_local_backup; then
                        echo -e "${YELLOW}👉 本地自动恢复流程已结束，您仍可参考下方手动恢复步骤。${NC}"
                        echo ""
                    fi
                    echo -e "${CYAN}====================================================${NC}"
                    return
                    ;;
                2)
                    if ! run_official_restore_flow; then
                        echo -e "${YELLOW}👉 官方高级恢复流程已结束，您仍可参考下方手动恢复步骤。${NC}"
                        echo ""
                    fi
                    echo -e "${CYAN}====================================================${NC}"
                    return
                    ;;
                q|Q)
                    break
                    ;;
                *)
                    print_invalid_choice
                    ;;
            esac
        done
        echo ""
    elif [ -n "$VW_DIR" ] && [ -d "$_VW_BACKUP_ARCHIVES_DIR" ]; then
        echo -e "${YELLOW}⚠️ 当前备份目录中还没有可用的 .zip 备份文件。${NC}"
        echo -e "${YELLOW}👉 请先把备份包上传到 $_VW_BACKUP_ARCHIVES_DIR/，再进入本地自动恢复流程。${NC}"
        echo ""
    fi
    
    echo -e " ${CYAN}步骤 1${NC}: 从您的云盘 (如 WebDAV/OSS) 中下载最近的一份 ${GREEN}.zip${NC} 备份文件到本地电脑。"
    echo -e "          ${YELLOW}如果要走本机自动恢复，请把它上传到：$_VW_BACKUP_ARCHIVES_DIR/${NC}"
    echo -e " ${CYAN}步骤 2${NC}: 解压该 ${GREEN}.zip${NC} 文件 (解压密码为您在部署时设置的【备份压缩包密码】)。"
    echo -e " ${CYAN}步骤 3${NC}: 登录本服务器，在终端执行以下命令暂时停止 Vaultwarden 核心服务："
    echo -e "          ${GREEN}cd $_VW_APP_DIR && docker compose stop vaultwarden${NC}"
    echo -e " ${CYAN}步骤 4${NC}: 将解压后得到的 ${GREEN}db.sqlite3${NC}、${GREEN}rsa_key*${NC}、${GREEN}attachments${NC} 文件夹等，"
    echo -e "          完整覆盖上传到服务器的以下数据目录："
    echo -e "          ${GREEN}$_VW_DATA_DIR/${NC}"
    echo -e "          ${YELLOW}(注意：覆盖前建议将旧的 data 目录改名备份，以防万一)${NC}"
    echo -e " ${CYAN}步骤 5${NC}: 数据覆盖完毕后，重新启动 Vaultwarden 容器："
    echo -e "          ${GREEN}cd $_VW_APP_DIR && docker compose start vaultwarden${NC}\n"
    echo -e "${GREEN}🎉 启动后，刷新网页端，您的金库数据即刻重生！${NC}"
    echo -e "${CYAN}====================================================${NC}"
}

# 12. 彻底卸载 (包含 Caddy 智能连带销毁)
uninstall_backup_service() {
    if [ -z "$VW_DIR" ] || [ ! -d "$_VW_BACKUP_DIR" ]; then
        echo -e "${YELLOW}⚠️ 未发现备份服务目录，无需卸载。${NC}"
        BACKUP_MODE="none"
        set_conf_var "BACKUP_MODE" "\"$BACKUP_MODE\""
        return 2
    fi

    if [ -f "$_VW_BACKUP_COMPOSE_FILE" ]; then
        compose_down_dir "$_VW_BACKUP_DIR" -v
    fi
    cd / && rm -rf "$_VW_BACKUP_DIR"
    BACKUP_MODE="none"
    set_conf_var "BACKUP_MODE" "\"$BACKUP_MODE\""
    echo -e "${GREEN}✅ 备份服务、配置与本地备份目录已清理。${NC}"
    return 0
}

uninstall_core_service() {
    local domain=""
    local caddy_backup_file=""
    local keep_caddy_backup=""

    if [ -z "$VW_DIR" ] || [ ! -d "$_VW_APP_DIR" ]; then
        echo -e "${YELLOW}⚠️ 未发现主服务目录，无需卸载。${NC}"
        return 2
    fi

    if [ -f "$_VW_BACKUP_COMPOSE_FILE" ]; then
        echo -e "${YELLOW}💡 检测到备份服务已启用。卸载主服务前会先停止备份容器，避免空转重启。${NC}"
        compose_down_dir "$_VW_BACKUP_DIR"
    fi

    domain=$(extract_domain_host "$(get_env_var "$_VW_ENV_FILE" "DOMAIN")")
    if [ -f "$_VW_COMPOSE_FILE" ]; then
        compose_down_dir "$_VW_APP_DIR" -v
    fi

    if [ -n "$domain" ] && [ "$PROXY_MODE" == "caddy" ] && [ -n "$CADDY_DIR" ] && [ -d "$CADDY_DIR" ]; then
        CADDY_FILE="$CADDY_DIR/Caddyfile"
        if [ -f "$CADDY_FILE" ]; then
            caddy_backup_file="${CADDY_FILE}.bak_$(date +%s)"
            cp "$CADDY_FILE" "$caddy_backup_file"

            remove_caddy_managed_block "$domain"

            if [ "$(count_caddy_managed_blocks)" -eq 0 ] && [ "$CADDY_EXISTS" = "false" ] && [ -f "$CADDY_DIR/docker-compose.yml" ]; then
                echo -e "${YELLOW}🔍 检测到内置 Caddy 当前已无任何有效配置站点。${NC}"
                echo -e "${YELLOW}正在连同 Caddy 容器一并彻底物理销毁...${NC}"
                compose_down_dir "$CADDY_DIR" -v
                load_caddy_runtime
                [ -n "$EXISTING_CADDY_ID" ] && docker rm -f "$EXISTING_CADDY_ID" 2>/dev/null || true
                cd / && rm -rf "$CADDY_DIR"
                echo -e "${GREEN}✅ 随附的 Caddy 代理服务已被无痕销毁。${NC}"
            else
                reload_caddy_runtime || true
                echo -e "${GREEN}✅ 已从 Caddy 中安全移除 Vaultwarden 规则，保留了您的其他配置，并已热重载。${NC}"
                if [ -n "$caddy_backup_file" ] && [ -f "$caddy_backup_file" ]; then
                    read -p "是否保留 Caddy 变更前备份文件 [$caddy_backup_file]？(y/n, 默认y): " keep_caddy_backup
                    if [[ "$keep_caddy_backup" == [nN]* ]]; then
                        rm -f "$caddy_backup_file"
                        echo -e "${GREEN}✅ 已删除本次生成的 Caddy 备份文件。${NC}"
                    else
                        echo -e "${YELLOW}💡 已保留 Caddy 备份文件，方便您后续回滚。${NC}"
                    fi
                fi
            fi
        fi
    fi

    rm -f "$_VW_COMPOSE_FILE" "$_VW_ENV_FILE" 2>/dev/null || true
    cd / && rm -rf "$_VW_APP_DIR"
    echo -e "${GREEN}✅ Vaultwarden 主服务与数据已清除。${NC}"
    return 0
}

uninstall_services() {
    local backup_uninstall_result=2
    local core_uninstall_result=2
    local performed_cleanup=false
    local had_shortcut=false
    local uninstall_choice=""
    local removed_project_root=""

    echo -e "${RED}$(_hr)${NC}"
    echo -e "${RED} 警告：此操作将彻底清理环境与数据！${NC}"
    echo -e "${RED}$(_hr)${NC}"

    while true; do
        backup_uninstall_result=2
        core_uninstall_result=2
        performed_cleanup=false
        removed_project_root=""

        echo -e "${CYAN}请选择卸载范围:${NC}"
        echo -e " 1) 仅卸载备份服务"
        echo -e " 2) 仅卸载主服务"
        echo -e " ${RED}3) 全部卸载${NC}"
        echo -e " q) 取消返回"
        read -p "请选择操作 [1-3, q]: " uninstall_choice

        if [[ "$uninstall_choice" == "q" || "$uninstall_choice" == "Q" ]]; then
            return
        fi

        had_shortcut=false
        has_global_shortcut && had_shortcut=true

        if ! has_any_managed_deployment; then
            if [[ "$uninstall_choice" == "3" ]]; then
                cleanup_generated_files
                remove_unzip_if_managed || true
                if [ "$had_shortcut" = true ]; then
                    echo -e "${GREEN}✅ 当前未发现已部署环境，已为您清理快捷命令与残留记录。${NC}"
                else
                    echo -e "${YELLOW}⚠️ 当前未发现可卸载的主服务或备份服务。${NC}"
                fi
                exit 0
            fi

            case $uninstall_choice in
                1)
                    echo -e "${YELLOW}⚠️ 当前未发现可卸载的备份服务。${NC}"
                    ;;
                2)
                    echo -e "${YELLOW}⚠️ 当前未发现可卸载的主服务。${NC}"
                    ;;
                *)
                    print_invalid_choice
                    ;;
            esac
            echo ""
            continue
        fi

        case $uninstall_choice in
            1)
                if uninstall_backup_service; then
                    backup_uninstall_result=0
                else
                    backup_uninstall_result=$?
                fi
                if [ "$backup_uninstall_result" -eq 0 ]; then
                    performed_cleanup=true
                fi
                ;;
            2)
                if uninstall_core_service; then
                    core_uninstall_result=0
                else
                    core_uninstall_result=$?
                fi
                if [ "$core_uninstall_result" -eq 0 ]; then
                    performed_cleanup=true
                fi
                ;;
            3)
                if uninstall_backup_service; then
                    backup_uninstall_result=0
                else
                    backup_uninstall_result=$?
                fi
                if uninstall_core_service; then
                    core_uninstall_result=0
                else
                    core_uninstall_result=$?
                fi
                rm -f "$_VW_BACKUP_COMPOSE_FILE" "$_BACKUP_ENV_FILE" 2>/dev/null || true
                if [ -d "$VW_DIR" ] && [ -z "$(find "$VW_DIR" -mindepth 1 -maxdepth 1 2>/dev/null)" ]; then
                    removed_project_root="$VW_DIR"
                    cd / && rm -rf "$VW_DIR"
                fi
                if [ "$backup_uninstall_result" -eq 0 ] || [ "$core_uninstall_result" -eq 0 ]; then
                    performed_cleanup=true
                    echo -e "${GREEN}✅ 整个项目根目录已完成清理。${NC}"
                fi
                ;;
            *)
                print_invalid_choice
                echo ""
                continue
                ;;
        esac

        if [ "$performed_cleanup" = false ] && [[ "$uninstall_choice" != "3" ]]; then
            case $uninstall_choice in
                1)
                    echo -e "${YELLOW}⚠️ 当前未发现可卸载的备份服务。${NC}"
                    ;;
                2)
                    echo -e "${YELLOW}⚠️ 当前未发现可卸载的主服务。${NC}"
                    ;;
            esac
            echo ""
            continue
        fi

        if [[ "$uninstall_choice" == "3" ]]; then
            # 仅清理脚本确认创建过的网络，避免误删用户已有网络
            if [ -n "$DOCKER_NET" ]; then
                if [ "$DOCKER_NET_OWNED" = "true" ]; then
                    echo -e "${YELLOW}正在尝试清理脚本创建的 Docker 网络 [$DOCKER_NET]...${NC}"
                    if docker network rm "$DOCKER_NET" >/dev/null 2>&1; then
                        echo -e "${GREEN}✅ 脚本创建的 Docker 网络已安全清理。${NC}"
                    else
                        echo -e "${YELLOW}⚠️ 已执行网络清理，但 [$DOCKER_NET] 当前未能删除，可能仍被其他容器占用。${NC}"
                    fi
                else
                    echo -e "${GREEN}✅ 网络 [$DOCKER_NET] 不属于脚本创建的专属网络，已跳过清理。${NC}"
                fi
            fi

            cleanup_generated_files
            remove_unzip_if_managed || true
            docker image prune -f 2>/dev/null || true
            prompt_remove_empty_parent_dir "$removed_project_root"
        elif [[ "$uninstall_choice" == "1" ]]; then
            BACKUP_MODE="none"
            set_conf_var "BACKUP_MODE" "\"$BACKUP_MODE\""
        fi

        case $uninstall_choice in
            1)
                echo -e "${GREEN}🎉 备份服务卸载完成，您可以继续在当前卸载菜单中执行其他操作。${NC}"
                echo ""
                ;;
            2)
                echo -e "${GREEN}🎉 主服务卸载完成，您可以继续在当前卸载菜单中执行其他操作。${NC}"
                echo ""
                ;;
            3)
                echo -e "${GREEN}🎉 卸载清理完全完成！系统已恢复如初，快捷命令已拔除。${NC}"
                exit 0
                ;;
        esac
    done
}

# Web branch startup; original script is kept byte-for-byte separately.
if [ -n "${VW_PANEL_LOCK:-}" ]; then
    exec 9>"$VW_PANEL_LOCK"
    flock -n 9 || { echo "另一个网页或 SSH 网页分支控制台正在运行。"; exit 1; }
fi
check_dependencies
auto_recover_conf
load_conf_vars
[ -n "$VW_DIR" ] && _update_cached_paths
[ -z "$BACKUP_MODE" ] && BACKUP_MODE=$(infer_backup_mode)
persist_runtime_state_defaults

# 主循环
while true; do
    show_menu
    read -p "请选择操作 [1-18, q]: " choice
    case $choice in
        1)  generate_configs || true ; pause_point ;;
        2)  setup_backup_service || true ; pause_point ;;
        3)  configure_rclone || true ;;
        4)  start_core_service || true ; pause_point ;;
        5)  stop_core_service || true ; pause_point ;;
        6)  start_backup_service || true ; pause_point ;;
        7)  stop_backup_service || true ; pause_point ;;
        8)  update_services || true ; pause_point ;;
        9)  encrypt_token || true ; pause_point ;;
        10) modify_core_config || true ;;
        11) modify_backup_config || true ;;
        12) test_backup || true ; pause_point ;;
        13) show_info || true ; pause_point ;;
        14) view_logs || true ;;
        15) show_backup_diagnostics || true ; pause_point ;;
        16) restore_guide || true ; pause_point ;;
        17) show_environment_doctor || true ; pause_point ;;
        18) uninstall_services ;;
        q|Q) echo -e "${GREEN}退出管家面板。再见！${NC}"; exit 0 ;;
        *) echo -e "${RED}❌ 无效的选择，请重新输入。${NC}" ; sleep 1 ;;
    esac
done
