# Vaultwarden 网页管理面板

在 Linux 服务器的交互式 SSH 会话中执行：

```bash
curl -fL --retry 3 https://raw.githubusercontent.com/Oliver-LL/vw_web/main/vw-panel/install-vw-web.sh -o install-vw-web.sh && sudo bash install-vw-web.sh
```

需要 Linux、systemd 和 Bash 4+；缺少 Python 时安装器会尝试安装。按提示设置端口和管理员密码后，通过服务器 IP 访问面板。安装器默认监听 `0.0.0.0`，需自行配置安全组、防火墙和 HTTPS 反代。

升级也执行同一条命令，保留管理员密码、面板端口和域名设置。升级会重启面板，请先完成正在运行的控制台操作。

## 本地开发

- `vw_2.sh`：原始脚本，保留不改。
- `vw-panel/`：面板代码与开发说明，直接修改本地源文件。
- `vw-panel/install-vw-web.sh`：单文件安装包，修改代码后执行 `python3 vw-panel/build.py` 更新，再提交发布。

详细功能、反代设置和验证方式见 [面板说明](vw-panel/README.md)。

以上下载地址在代码推送至 GitHub 的 `main` 分支后生效。公开下载需要仓库保持公开。
