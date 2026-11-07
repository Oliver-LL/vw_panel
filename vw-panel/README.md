# Vaultwarden 网页管理面板 0.3

面向日常管理的表单面板：填写配置、点击部署、查看任务结果。常用功能不再要求输入脚本菜单编号。

## 安装与升级

在 Linux 服务器的交互式 SSH 会话中执行：

```bash
curl -fL --retry 3 https://raw.githubusercontent.com/Oliver-LL/vw_web/main/vw-panel/install-vw-web.sh -o install-vw-web.sh && sudo bash install-vw-web.sh
```

首次安装设置面板端口（默认 8787）与管理员密码（至少 12 字符）。升级执行同一条命令，保留面板密码、端口、域名和原部署数据。新代码推送至公开仓库 `main` 分支后，下载地址才包含新版。

环境：Linux、systemd、Bash 4+、Python 3.9+（需支持 scrypt）、flock。缺少 Python 时安装器尝试使用 apt/dnf/yum 安装。面板不依赖 npm 或 pip，Docker 可在登录后通过概览页安装。

面板默认监听 `0.0.0.0`；访问 `http://服务器IP:端口`。云安全组与服务器防火墙需允许面板端口。公网使用建议配置 HTTPS 反代，面板以 root 身份执行管理操作，应只向管理员开放。

## 从零部署

1. 概览页如提示环境未就绪，点击“安装 Docker”。它使用 Docker 官方安装器，会修改服务器系统软件。
2. 打开“密码库配置”，填写密码库 HTTPS 地址和管理员邮箱。新服务器推荐自动 HTTPS；已有反向代理请选择对应方式。
3. 保存基本配置，设置并加密密码库管理口令，然后点击“部署 / 应用配置”。
4. 在“任务与日志”查看结果。自动 HTTPS 需要先解析域名，并确保 TCP 80 / 443 未被占用且可入站访问。
5. 打开密码库，创建首个账号，然后按需要关闭新用户注册。

三种访问方式：

- 自动 HTTPS：生成独立 Caddy 配置并申请证书。
- 宿主机反代：Vaultwarden 只映射 `127.0.0.1:8080`（端口可选），宿主机 Nginx 指向该地址。
- 容器反代：选择与 NPM 相同的 Docker 网络，目标填 `http://vaultwarden:80`，并配置密码库域名的 HTTPS。

面板域名和密码库域名相互独立。面板设置中允许一个域名，不会自动修改 DNS 或配置反向代理。

## 图形化功能

- 概览：密码库与备份容器状态、最近本机备份、首次部署引导。
- 密码库配置：域名、管理员邮箱、注册、邀请、Send 分享与管理口令。
- 邮件服务：SMTP 地址、端口、加密方式、发件邮箱、账号与授权码。
- 备份保护：频率、保留天数、时区、加密密码、启用 / 暂停、立即备份与本机文件列表。
- WebDAV：填写地址、账号、应用密码和目录，验证连接后切换目标。会创建远程目录；不搬迁或删除原备份。
- 任务：部署、停止、升级、备份、日志读取与失败原因。任务按钮调用独立后端接口，不自动回答 Bash 菜单。
- 面板设置：HTTPS 域名白名单及折叠的高级工具。

密码字段不回填。留空时保留原密码；首次设置备份密码和管理口令至少 12 字符。管理口令通过 Argon2id 哈希后保存，原文不写入任务输出或配置。

已有部署从 `~/.vw_deploy.conf` 读取目录与代理方式，不执行该文件。保存表单保留未修改的环境项、远程配置与其他配置键；修改环境文件前保留一份 `.panel-backup`。现有安装目录和代理拓扑不会通过表单重新生成。现有 Caddy 部署更换域名暂需高级工具同步代理配置。

新建备份默认在本机保存加密归档。已有远程目标在修改备份计划时保持原样。本机副本不能抵御服务器损坏；可连接 WebDAV 远程存储。OneDrive 等需要 OAuth 的云盘、数据恢复和卸载仍在“面板设置 → 高级工具”中完成，尚未改成原生表单。

## 操作与日志

同一时间只执行一个任务或高级控制台，跨进程共享操作锁。原始脚本未加入此锁，切勿在另一 SSH 会话中同时执行部署或恢复。

表单保存与应用分开：保存不会立即重启容器，点击应用才生效。失败会显示真实命令错误；服务部署后检查容器运行状态，DNS 和 HTTPS 是否可用仍以实际访问为准。

后台任务在浏览器关闭后继续。任务状态只保存在当前面板进程，输出以权限 600 的临时文件保存在状态目录内，当前任务替换时清理前一任务输出；面板重启后状态清空，不提供完整操作审计。升级重启面板可能中断任务，应先完成当前操作。任务输出尽量遮蔽已知密码，服务自身输出仍仅供管理员查看。

原脚本高级工具是兼容入口：支持按行输入、回车、中断与隐藏输入，不是完整终端模拟器。

## 面板域名与 SSH 管理

```bash
sudo vw-panel info
sudo vw-panel status
sudo vw-panel logs
sudo vw-panel restart
sudo vw-panel password
sudo vw-panel port 8788
sudo vw-panel domain https://panel.example.com
```

域名需先配置 DNS，再将 Nginx / NPM / Caddy 的 HTTPS 反代指向面板。宿主机反代目标为 `http://127.0.0.1:面板端口`，容器中的 NPM 使用其可访问的宿主机 IP。保留浏览器原始 Host，包括非标准端口：`proxy_set_header Host $http_host;`。

## 本地开发

本目录是面板开发目录，直接修改源码。打包不会覆盖 `vw_web.sh`。

- `web/static/`：原生 HTML、CSS、JavaScript。
- `web/server.py`：登录、会话、CSRF、HTTP 接口与旧控制台。
- `web/manager.py`：配置读取、校验、保留式写入、Docker 任务和锁。
- `web/tty_worker.py`：高级控制台 TTY。
- `installer-header.sh`：系统安装逻辑。
- `build.py`：打包本地代码，校验原始快照，生成单文件安装器。
- `install-vw-web.sh`：发布产物，修改源文件后重新生成并一同提交。
- `vw_original.sh`：原始脚本快照，不修改。
- `vw_web.sh`：兼容分支，保留原业务流程、网页锁与初始化调整，并支持读取表单写入的 dotenv 引号。

原始快照 SHA-256：`756b783a388b087ff48af8db93273543a2ea2ea7009bdcfd50201e671d044aff`。原始来源为 [上游 Gist](https://gist.githubusercontent.com/Oliver-LL/72ef2f0aa06ce5d4f186bbc67adebada/raw/gistfile1.txt)，安装与打包不重新下载上游。

```bash
python3 build.py
python3 -m unittest discover -s tests -v
node --check web/static/app.js
bash -n vw_web.sh
bash install-vw-web.sh --extract /tmp/vw-panel-inspect
```

`--extract` 只解包，不安装或启动服务。内置 SHA-256 校验检测载荷损坏，不是数字签名。

新增测试覆盖密码保留、未知配置保留、文件权限、路径与输入校验、任务互斥、失败结果、WebDAV 验证失败时不切换、认证与 CSRF 和 HEAD 请求。Docker 执行使用模拟命令；浏览器检查使用模拟接口验证页面导航、表单、登录退出、任务入口与移动布局。尚未在真实 VPS 验收新版的 Docker 部署、WebDAV 传输、加密备份或数据恢复。
