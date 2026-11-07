# Vaultwarden 网页管理面板 0.2

## 本地开发

本目录是网页面板的唯一开发目录。直接编辑：

- `web/static/`：页面、样式与前端交互。
- `web/server.py`、`web/tty_worker.py`：HTTP 后端与交互控制台。
- `vw_web.sh`：网页脚本分支，直接维护；打包不会重新生成或覆盖它。
- `installer-header.sh`：安装器逻辑。

上级目录的 `vw_2.sh` 保留原样。本目录的 `vw_original.sh` 是相同内容的独立打包快照，为安装包审阅与校验保留，不作为开发入口。
`install-vw-web.sh` 是发布产物，请修改源文件后运行 `python3 build.py` 更新，不直接编辑其内嵌载荷。
构建只读取本地文件，不下载或执行上游脚本。

## 现在怎么用

只需要 `install-vw-web.sh` 一个文件。它已经内置网页、后端和脚本，不需要手动拼项目，不需要 npm、pip 或事先安装 Docker。

1. 在 Linux 服务器的交互式 SSH 会话中执行：

   ```bash
   curl -fL --retry 3 https://raw.githubusercontent.com/Oliver-LL/vw_web/main/vw-panel/install-vw-web.sh -o install-vw-web.sh && sudo bash install-vw-web.sh
   ```

2. 命令从 `Oliver-LL/vw_web` 仓库的 `main` 分支下载完整安装包，无需手动上传。
3. 按提示设置面板端口（默认 8787）和管理员密码（至少 12 个字符）。
4. 打开安装结束时显示的 `http://服务器IP:端口`，输入刚才的密码登录。
5. 在功能中心选择生成配置、部署、备份等，按照下方交互控制台的提示继续。

面板默认监听 `0.0.0.0`。如果 IP 无法访问，在云安全组和服务器防火墙中放行所选 TCP 端口。安装器不擅自修改你的防火墙。

适用环境：Linux + systemd、Bash 4+、Python 3.9+。Debian 12 适用。Python 缺失时安装器会尝试通过 apt/dnf/yum 安装；缺少 flock 时需安装 util-linux。Mac 仅用于编辑代码，安装器需要在 Linux VPS 上执行。

## 域名可选，后续再配

先使用 IP 登录，在“面板设置”中填写 `https://vw-panel.example.com` 并保存。也可以通过 SSH 配置：

```bash
sudo vw-panel domain https://vw-panel.example.com
```

然后：

1. 将该域名 DNS 指向服务器。
2. 在 NPM / Nginx / Caddy 中配置 HTTPS 反代。
3. 宿主机 Nginx 的目标通常为 `http://127.0.0.1:8787`。
4. **容器中的 NPM** 使用其能访问的宿主机 IP 和面板端口。容器里的 `127.0.0.1` 指向 NPM 自己，并非宿主机。
5. 保留浏览器原始 Host（含非标准端口）。Nginx 可设置 `proxy_set_header Host $http_host;`，避免被默认上游 Host 覆盖。
6. 配置后打开域名登录。域名和 IP 的登录 cookie 分开，需要重新登录。

面板域名与密码库域名是两个地址。面板支持 IP HTTP 登录，不代表 Vaultwarden Web Vault 也适合以公网 IP HTTP 使用；密码库应配置 HTTPS 域名。

IP 的 HTTP 访问没有传输加密。需要在公网输入密码和远程存储凭据时，建议先配置 HTTPS 反代。面板服务以 root 身份执行管理脚本，请仅向管理员开放。这里没有给原来的密码库修改注册、加密或访问策略。

## 网页功能

- 管理员密码登录、scrypt 密码哈希、登录限速、HttpOnly / SameSite cookie、CSRF 校验。
- 主服务与备份状态。
- 原脚本 18 个菜单入口。
- 多级菜单、密码隐藏输入、实时输出、刷新后的输出重连。
- 同时只运行一个控制台，避免网页内并发部署。
- 配置域名白名单；HTTPS 域名使用 Secure cookie。
- 浏览器关闭或退出登录后，进行中的脚本继续运行；面板重启会终止控制台。

**本版配置通过网页中的交互向导完成，尚未把所有配置项改成专用表单。** 点击功能后在下方输入选项和配置；需要停留在当前控制台继续操作时，直接按脚本菜单操作，不要重复启动新控制台。

这是脚本交互桥接，并非完整终端模拟器：输出按文本显示，支持发送一行、仅回车和 Ctrl+C；不支持全屏终端编辑器、鼠标操作或任意 Shell 命令接口。Rclone OAuth 仍按原脚本提示完成授权。脚本原有的业务限制、远程存储能力与错误提示仍然适用。

密码输入时脚本关闭 TTY 回显，网页根据回显状态隐藏输入。遇到敏感输入也可以先点击“手动隐藏输入”。已有脚本如主动打印敏感配置，仍会出现在管理员控制台内。输出仅保存在面板进程内存，最多约 512 Ki 字符，服务重启后不保留，不写入审计文件。面板有单管理员密码，不提供多用户角色和操作审计。

**网页分支**可通过共享文件锁阻止多个分支控制台同时操作，但无法阻止你在另一个 SSH 中运行未改动的原脚本。不要同时用原脚本和网页执行部署、恢复、更新或卸载。

## 常用 SSH 命令

```bash
sudo vw-panel info
sudo vw-panel status
sudo vw-panel logs
sudo vw-panel restart
sudo vw-panel password
sudo vw-panel port 8788
sudo vw-panel domain https://vw-panel.example.com
```

修改端口或域名后，反代目标、安全组及防火墙规则需要自行同步。`domain` 和 `password` 命令会重启面板，使已有登录失效，并终止正在进行的控制台；请在空闲时执行。网页保存域名不需要重启。

升级面板时重新运行新版单文件安装器；保留密码、端口和域名设置，生成新的程序版本目录。升级会重启面板，请先完成当前控制台操作。

## 文件与原脚本

- `/opt/vw-panel/current`：当前程序版本链接。
- `/opt/vw-panel/releases/`：程序版本目录。
- `/var/lib/vw-panel/`：面板设置、密码哈希和互斥锁。
- `/etc/systemd/system/vw-panel.service`：开机启动服务。
- `/usr/local/bin/vw-panel`：面板维护命令。
- `vw_original.sh`：从用户提供的 Gist 获取、逐字节保留的脚本快照。
- `vw_web.sh`：最初基于该快照生成、现直接维护的网页分支。

上游地址：
https://gist.githubusercontent.com/Oliver-LL/72ef2f0aa06ce5d4f186bbc67adebada/raw/gistfile1.txt

本次获取的快照自报版本为 `v1.1.3`，SHA-256：

```text
756b783a388b087ff48af8db93273543a2ea2ea7009bdcfd50201e671d044aff
```

网页分支只禁用网页启动时覆盖全局 `vw` 快捷命令的逻辑，并调整初始化顺序、加入网页分支互斥锁。保留原脚本其余业务流程。安装器不会覆盖服务器上已有的 `vw_2.sh`，也不会在安装面板时启动或卸载 Vaultwarden。

安装面板不访问 Gist 下载新脚本：快照已内置，避免每次安装时上游内容变化。原脚本自身的 Docker 镜像下载、Docker 官方安装和备份服务等仍可能需要联网。

## 打包与验证

```bash
python3 build.py
python3 -m unittest discover -s tests -v
node --check web/static/app.js
bash -n vw_web.sh
bash install-vw-web.sh --extract /tmp/vw-panel-inspect
```

`--extract` 仅解包，不安装或启动服务；也可以据此审阅内置代码。内置 SHA-256 检查检测载荷损坏，不是数字签名或远端来源认证。

测试覆盖：登录与 CSRF、登录限速、IP 免域名访问、域名与 cookie、18 个快捷入口、控制台互斥与密码回显、原脚本菜单启动、载荷校验和单文件安装/升级。

安装集成测试将路径映射到临时目录并模拟 systemd，实际启动 HTTP 后端、完成 IP 登录，验证升级保留设置。Docker 测试使用桩命令。**尚未在真实 VPS 上执行 Docker 部署、云盘授权、生产数据恢复或真实 systemd 安装；这些仍需要部署验收。**

发布仓库为 `https://github.com/Oliver-LL/vw_web`。代码推送到公开仓库的 `main` 分支后，本文中的下载命令生效。后续更新本地代码后，先运行 `python3 build.py`，再将源代码和安装包一并提交、推送。GitHub Desktop 使用 HTTPS 登录，无需修改公司 SSH 配置。
