# 部署、域名与日常管理

## 用域名直接访问（推荐）

域名的 A 记录解析到 VPS 的 IPv4。若有 AAAA 记录，必须指向这台 VPS 可访问的 IPv6；没有 IPv6 则不要设置 AAAA。VPS 防火墙和云服务商安全组需允许 TCP 80、443；不需要开放 18080。

安装网页后，在解压目录执行：

```bash
bash setup-domain.sh watch.example.com
```

把 `watch.example.com` 换成你自己的域名。可选第二个参数为证书联系邮箱：`bash setup-domain.sh watch.example.com 你的邮箱`；不填邮箱也可以申请证书。

首次安装可一步完成安装与域名配置：`bash install.sh watch.example.com`。

脚本会安装 nginx 和 certbot，为该域名建立独立配置、申请 HTTPS 证书、设置 HTTP 跳转 HTTPS、启用安全 Cookie 和自动续期，并重启网页服务。证书签发前不会通过 HTTP 暴露登录表单。若证书申请失败，回退该脚本负责的网站配置，提示检查 DNS 和端口。其他 nginx 站点配置保留。

之后直接在浏览器打开 `https://你的域名`，不需要 SSH 转发。管理员密码和已保存任务保留。公网访问仍需登录。

如果已有网站占用 80/443 且不是 nginx，先调整现有入口配置；不要同时运行多个入口服务器占用同一端口。若使用 CDN 代理，证书验证路径需要正常通往 VPS；首次配置可先用直接 DNS 解析。

域名配置脚本做本地语法与配置生成检查，真实证书签发及 VPS 上的 nginx 联调仍需部署时验证。

### 手动配置方式

将自己的域名解析到 VPS 后，安装 nginx 和 certbot；检查已有站点配置避免端口冲突。将 `deploy/nginx.conf.example` 复制到 nginx 的站点配置目录，并把 `watch.example.com` 替换为实际域名。

```bash
apt-get install -y nginx certbot python3-certbot-nginx
# 按你现有 nginx 结构启用配置后：
nginx -t
systemctl reload nginx
certbot --nginx -d 你的域名
```

证书成功、已启用 HTTP 到 HTTPS 跳转后，将 `/etc/vps-watch.env` 中的 `COOKIE_SECURE=0` 改成 `COOKIE_SECURE=1`，执行 `systemctl restart vps-watch-web`，使用 `https://你的域名` 登录。不要直接把18080开放到公网。SSH转发走SSH加密；公网应使用HTTPS。

## 日常管理

```bash
systemctl status vps-watch-web vps-watch-worker --no-pager
journalctl -u vps-watch-worker -n 50 --no-pager
systemctl restart vps-watch-web vps-watch-worker
```

重置密码（不会改变任务）：

```bash
runuser -u vpswatch -- env WATCH_DATA=/var/lib/vps-watch /opt/vps-watch/.venv/bin/python /opt/vps-watch/manage.py password
```

重置密码后若要撤销原有登录会话，可在停服后备份并移除 `/var/lib/vps-watch/session.key` 再启动网页服务；不要移除 encryption.key，否则已有 Telegram Token 无法解密。

网页一键更新：`install.sh` 会安装一个 root 身份的更新程序（`vps-watch-update.path` 监听 `/var/lib/vps-watch/update-request`，触发 `/opt/vps-watch/update.sh`）。网页进程本身仍是无权限的 `vpswatch` 用户，只能创建这个请求文件，且需要重新输入管理员密码；请求文件的内容不会被采用，更新来源只取 `/etc/vps-watch.env` 里的 `UPDATE_REPO`（默认 `prettylove520/vps-monitor`）的 `main` 分支。更新前备份到 `/var/backups/vps-watch`（保留最近 5 份，含数据库），校验失败或新版本不能启动会自动退回。日志：`journalctl -u vps-watch-update`。不想要这个功能：`systemctl disable --now vps-watch-update.path` 并删除 `/etc/vps-watch.env` 里的 `UPDATE_APPLY=1`。Docker 部署不提供网页更新。

升级：备份后上传新版本，运行安装脚本；数据库和密钥保留，既有任务保留。安装脚本不会主动停掉旧的独立 BERO/VMISS 服务。

环境变量（写在 `/etc/vps-watch.env`，Docker 写在 `docker-compose.yml` 的 `environment`）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `COOKIE_SECURE` | `1` | 通过 HTTPS 访问时保持 `1`；只用 SSH 转发访问时设为 `0` |
| `UPDATE_CHECK` | `1` | 设为 `0` 关闭每天一次的新版本检查 |
| `UPDATE_REPO` | `prettylove520/vps-monitor` | 用自己的 fork 时改成 `你的用户名/仓库名` |

只想迁移任务、不迁移账号：在任务页点「导出」，新工作台点「导入」。

备份：先停止两个新服务，完整备份 `/var/lib/vps-watch` 和 `/etc/vps-watch.env`，再启动服务。目录包含数据库和密钥，应私密保管；恢复时保持目录属主 vpswatch、权限700。Telegram Token在数据库中加密，具有该数据目录权限的服务器管理员仍可解密。
