# Vaultwarden 网页管理面板

在 Linux 服务器的交互式 SSH 会话中执行：

```bash
curl -fL --retry 3 https://raw.githubusercontent.com/Oliver-LL/vw_web/main/vw-panel/install-vw-web.sh -o install-vw-web.sh && sudo bash install-vw-web.sh
```

需要 Linux、systemd 和 Bash 4+；缺少 Python 时安装器会尝试安装。按提示设置端口和管理员密码后，通过服务器 IP 访问面板。安装器默认监听 `0.0.0.0`，需自行配置安全组、防火墙和 HTTPS 反代。

升级也执行同一条命令，保留管理员密码、面板端口和域名设置。升级会重启面板，请先完成正在运行的控制台操作。

## 0.3 · 表单管理面板

密码库、注册权限、邮件与备份改为原生表单；部署、升级和备份通过后台任务执行，显示真实结果。支持 WebDAV 连接验证，保留旧部署与未修改的密码。原脚本控制台默认折叠，仅用于高级维护。

[功能、使用方法与开发说明](vw-panel/README.md)
