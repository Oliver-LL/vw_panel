# Vaultwarden 网页管理面板

通过浏览器部署和管理 Vaultwarden，配置域名、反向代理和备份，以及查看日志和恢复数据。

## 纯网页版（推荐）

在 Linux 服务器的 SSH 会话中执行，选择安装或升级面板：

```bash
curl -fL --retry 3 https://raw.githubusercontent.com/Oliver-LL/vw_panel/main/vw-panel/install-vw-web-only.sh -o install-vw-web-only.sh && sudo bash install-vw-web-only.sh
```

需要 Linux、systemd 和 Bash 4+。安装完成后，使用终端显示的地址和密码登录。升级可执行同一条命令，保留已有面板设置。

## 网页版 + 脚本版

包含相同的网页功能，并可在终端执行 `sudo vwp original` 打开原版脚本。

```bash
curl -fL --retry 3 https://raw.githubusercontent.com/Oliver-LL/vw_panel/main/vw-panel/install-vw-web.sh -o install-vw-web.sh && sudo bash install-vw-web.sh
```

两种面板都可用 `sudo vwp` 管理。自动更新保留已安装的版本类型。已有面板属于完整版，如需切换，可手动运行对应安装包并选择安装或升级。

## 独立 Bash 脚本

[下载原版 Vaultwarden Bash 脚本](https://raw.githubusercontent.com/Oliver-LL/vw_panel/main/vw-panel/vw_original.sh)

也可在服务器上直接下载并运行，在终端菜单中部署和管理 Vaultwarden：

```bash
curl -fL --retry 3 https://raw.githubusercontent.com/Oliver-LL/vw_panel/main/vw-panel/vw_original.sh -o vw_original.sh && sudo bash vw_original.sh
```

此入口仅用于独立 Bash 脚本，无需安装网页面板。
