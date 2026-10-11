# VPS Monitor · 补货工作台

自己部署在 VPS 上的 VPS 补货监控网页。发现补货时用 Bark 强提醒、微信或 Telegram 通知你；对已核实的商家（目前是 BERO），还可以用账户余额**自动下单一次**。

账号、密码、余额和推送密钥都只保存在你自己的服务器上，加密存储。

## 功能

- **多商家监控**：BERO 套餐页、**任意 WHMCS 商家**（粘贴下单链接即可）、VMISS 专用模板、通用 HTML 规则（CSS 选择器）。
- **通知**：Bark 重要警告（持续响铃）、微信（PushPlus / Server酱 / 企业微信群机器人）、Telegram，可同时启用，按渠道去重和重试。
- **BERO 自动付款**：补货时立即用余额下单，只下一次；下单前核对套餐、规格、周期、优惠码、价格上限和余额，任何一项不对就停止。
- **补货记录**：记录每次补货的时间和持续时长，通知里附「近 30 天第几次补货」。
- **结账预检**：不下单地走一遍结账流程，启用自动付款后每 6 小时自动检查一次，网站改版能提前发现。
- **安全**：管理员密码 + 可选两步验证（TOTP）；网页只监听本机，配合 SSH 转发或 HTTPS 反向代理访问。
- **部署**：一键脚本（Debian / systemd）或 Docker Compose。

| 接入方式 | 监控 | 自动付款 | 说明 |
|---|---|---|---|
| BERO 套餐页 | ✅ | ✅ | 填套餐编号、名称、规格、周期、可选优惠码。详见 [docs/bero.md](docs/bero.md) |
| WHMCS 商家（通用） | ✅ | — | 粘贴 `…/cart.php?a=add&pid=编号` 下单链接；按 WHMCS 自带的缺货判断，不需要登录。新功能，遇到识别不了的商家欢迎提 Issue |
| VMISS · TRI Basic | ✅ | — | 专用模板 |
| 通用 HTML 规则 | ✅ | — | 无需登录、HTML 里直接有价格和库存文字的商品页 |

## 快速开始

### 方式一：一键脚本（Debian 12 / 13）

```bash
apt-get update && apt-get install -y git
git clone https://github.com/prettylove520/vps-monitor.git vps-watch
cd vps-watch
bash install.sh
```

安装时会让你设置管理员密码（至少 12 位）。

### 方式二：Docker

```bash
git clone https://github.com/prettylove520/vps-monitor.git vps-watch
cd vps-watch
docker compose build
docker compose run --rm web python manage.py password   # 设置管理员密码
docker compose up -d
```

数据保存在 Docker 卷 `vps-watch-data` 里。

### 打开网页

网页只监听 VPS 的 `127.0.0.1:18080`，不直接暴露到公网。两种访问方式：

- **SSH 转发**（最简单）：在自己电脑上运行 `ssh -L 18080:127.0.0.1:18080 root@你的VPS`，保持窗口打开，浏览器访问 http://127.0.0.1:18080 。
- **域名 + HTTPS**（推荐长期使用）：`bash setup-domain.sh 你的域名`，自动配置 nginx 和证书。详见 [docs/deploy.md](docs/deploy.md)。Docker 部署走 HTTPS 时，把 `docker-compose.yml` 里的 `COOKIE_SECURE` 改为 `"1"`。

### 第一次使用

1. **通知设置**：连接 Bark、微信或 Telegram，点「发送测试」确认能收到。
2. **添加任务**：选择接入方式，填好后保存。任务默认暂停，先点「检查一次」看日志结果，再点「启用」。
3. **安全**：建议在「安全」页绑定两步验证。
4. **（可选）BERO 自动付款**：按 [docs/bero.md](docs/bero.md) 验证登录、充值余额、跑一次结账预检，再启用。

## 升级

```bash
cd vps-watch && git pull && bash install.sh          # 一键脚本安装的
cd vps-watch && git pull && docker compose build && docker compose up -d   # Docker
```

数据库、密钥、任务和设置都会保留，旧版本的数据会自动迁移。升级后建议跑一次结账预检。改动见 [CHANGELOG.md](CHANGELOG.md)。

## 常见问题

**任务一直显示「检查异常」或「访问受限」？**
商家网站拒绝了访问（HTTP 403、Cloudflare 验证页）或页面结构变了。访问受限时任务会自动暂停；程序不会绕过验证码。可以调大检查间隔，过一段时间再启用。

**WHMCS 任务怎么找下单链接？**
在商家网站的商品上点「立即订购 / Order Now」，地址栏跳转前的链接形如 `https://商家域名/cart.php?a=add&pid=123`。如果点击后直接进了配置页，看地址栏或页面源代码里的 `pid=` 数字。

**补货通知收到了，但没抢到？**
页面显示有货不代表库存锁定。热门补货往往几秒售罄，建议开 Bark 重要警告，检查间隔设为 15–30 秒。对 BERO 可以启用自动付款。

**自动付款会不会重复扣款？**
不会。发出下单请求前会先写入一条购买记录，之后不论成功还是结果不明都不会再次提交；结果不明时会推送提醒你去商家网站核实。详见 [docs/bero.md](docs/bero.md)。

**手机丢了，两步验证登录不了？**
在服务器上执行 `runuser -u vpswatch -- env WATCH_DATA=/var/lib/vps-watch /opt/vps-watch/.venv/bin/python /opt/vps-watch/manage.py disable-2fa`（Docker：`docker compose run --rm web python manage.py disable-2fa`）。

**忘记管理员密码？**
同上，把 `disable-2fa` 换成 `password`。任务和设置不受影响。

**换 VPS 或想把监控清单分享给别人？**
任务页右上角「导出」得到 JSON 文件，在新的工作台点「导入」即可。文件只有任务设置，不含密码、推送密钥和购买设置；导入后默认暂停。

**会不会偷偷联网上报？**
不会。唯一的额外请求是 worker 每天查一次本项目在 GitHub 上的最新版本号，用来显示「有新版本」。不想要的话，在 `/etc/vps-watch.env`（Docker 在 `docker-compose.yml` 的 `environment`）里加 `UPDATE_CHECK=0` 后重启。

**怎么备份？**
完整备份（含账号和密钥）：停止服务后备份 `/var/lib/vps-watch` 和 `/etc/vps-watch.env`（Docker 备份 `vps-watch-data` 卷）。里面有数据库和加密密钥，请私密保管。

## 文档

- [docs/bero.md](docs/bero.md)：BERO 登录、下单流程、结账预检、自动付款规则
- [docs/notifications.md](docs/notifications.md)：Bark、微信、Telegram 设置与去重规则
- [docs/deploy.md](docs/deploy.md)：域名与 HTTPS、日常管理、备份
- [docs/development.md](docs/development.md)：本地开发、测试、接入新商家

## 免责声明

本项目供个人在自己的服务器上使用自己的商家账户。自动下单会真实扣款，请先用「结账预检」确认流程，并自行承担使用风险。使用前请阅读商家的服务条款，部分商家禁止自动化下单。程序遇到验证码、二步验证或访问限制时会停止，不会绕过；请勿修改代码去绕过商家的安全措施，也不要设置过高的检查频率。

## 许可证

MIT License，见 [LICENSE](LICENSE)。
