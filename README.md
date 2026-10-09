# VPS Watch · 补货工作台

自己部署在 VPS 上的补货监控网页（systemd 或 Docker）。一个管理员账号，支持多任务、编辑、暂停/继续、单次检查、过滤、检查日志、Telegram / Bark 通知设置和测试。网页与后台检查进程分开运行，关闭浏览器不会停止任务。

**BERO 套餐支持一次性自动付款（用账户余额）。** 套餐编号、名称、规格、周期、优惠码和金额上限都在任务里配置；自动付款默认关闭，需要在任务的「购买设置」里明确启用。其他商家只发送补货通知。

BERO 是预付费商家：先给账户充值余额（Guthaben），下单时直接从余额扣款，登录状态下没有单独的付款方式步骤。2026-10-09 用已登录账户在有货的 vServer 上核实了完整下单流程，停在最终确认页，没有下单。

## 已支持的接入

| 接入方式 | 当前范围 |
|---|---|
| BERO 套餐页 | 任意 `bero-host.de/server/…` 套餐页上的套餐卡片：填写套餐编号（页面中 `selectPackage(编号)`）、名称、必须出现的规格文字、周期天数和可选优惠码。支持监控、结账预检和一次性自动付款。默认模板为 Anniversary M（109，365 天，DISCORD10，已核实首次 44.10 EUR）。 |
| VMISS 专用适配 | US.LA.TRI.Basic（32），月付、5 CAD。此前页面有 403，任务遇到拒绝访问会暂停。 |
| 通用 HTML 规则 | HTTPS 静态商品页，配置唯一商品区域、身份文字、唯一价格元素、有货/缺货文字。支持明确的 EUR/CAD/USD/GBP/CNY 币种；不支持千分位价格。 |

同一商家不同任务至少间隔 15 秒，所有请求串行执行；大量任务时实际周期会延长。最低任务间隔15秒。网络/解析错误从5分钟起退避，最大1小时（更长 Retry-After 优先）。403、401、识别到的验证页暂停任务。任务修改后默认暂停。补货去重持久化；发送响应丢失或进程在发送后崩溃时仍可能重复通知，不能保证跨网络事务严格一次。

页面标记只表示可能可以订购，不代表库存已锁定。没有明确标记、匹配多个元素、套餐或价格格式变化都会报异常。通用规则匹配缺货文字优先。运行中的检查暂停后可能已有请求在途；暂停不会中断已发送的网络请求。

## 安装（首次）

1. SSH 登录 VPS（Debian 13），执行：

```bash
apt-get update && apt-get install -y git
git clone https://github.com/prettylove520/vps-monitor.git vps-watch
cd vps-watch
bash install.sh
```

2. 也可以在 GitHub 页面下载 ZIP，上传到 VPS 解压后执行 `bash install.sh`。

安装时设置管理员密码（至少12位，输入时不回显）。程序仅监听 VPS 的127.0.0.1:18080，默认不向公网开放管理后台。

3. 在你自己的 Windows 电脑另开 PowerShell，建立 SSH 转发：

```powershell
ssh -L 18080:127.0.0.1:18080 root@你的VPS地址
```

保留这个窗口，然后在电脑浏览器打开 **http://127.0.0.1:18080**，输入管理员密码。
如果 SSH 使用非22端口，在命令中增加 `-p 你的SSH端口`。转发窗口关闭后监控继续运行，只是网页暂时不能访问。

4. 到「通知设置」连接 Bark 或 Telegram，保存并发送测试消息。Telegram 先在机器人发送 `/start`。密钥只填在你的网页里，不要发给他人。
5. 默认已准备一条暂停的 BERO 任务。先点「检查一次」，到日志确认结果，再点「启用」。请停止旧监控服务，避免双份轮询和通知：

```bash
systemctl disable --now bero-monitor
# 如果还运行旧 VMISS 服务，也停止：
systemctl disable --now vmiss-monitor
```

服务不存在时上面两条会提示未找到，不影响新后台。

## 用 Docker 安装

```bash
git clone https://github.com/prettylove520/vps-monitor.git vps-watch
cd vps-watch
docker compose build
docker compose run --rm web python manage.py password   # 设置管理员密码（至少 12 位）
docker compose up -d
```

网页只监听本机 `127.0.0.1:18080`，用 SSH 转发访问，或在前面放一个 HTTPS 反向代理（`deploy/nginx.conf.example`）。走 HTTPS 时把 `docker-compose.yml` 里的 `COOKIE_SECURE` 改为 `"1"`。数据（数据库和加密密钥）保存在 Docker 卷 `vps-watch-data` 里，升级用 `git pull && docker compose build && docker compose up -d`。

## Bark 强提醒

网页「通知设置」可启用 Bark，分别填写「服务器地址」和「设备 Key」。官方服务器默认 `https://api.day.app`，Key 从 Bark App 获取，只填写 Key。支持公网 HTTPS/443 的自建服务，不支持内网或自建服务的路径前缀。旧版本保存的完整推送地址会自动拆分显示，旧 Key 继续使用；Key 栏留空保留现有密钥，更换服务器时必须填写新 Key。

默认补货提醒使用「重要警告」（critical）、音量8/10、alarm铃声和持续响铃30秒，可在网页调整，也可改为时效性或普通通知。监控异常使用普通通知，不附加30秒持续响铃。铃声名称可使用 Bark 内置或已导入手机的自定义铃声。0音量会静音，仅重要警告模式应用音量参数。

先在 iPhone 的系统设置允许 Bark 通知与「重要警告」，然后保存设置、点击「测试强提醒」确认实际效果。测试会使用当前已保存的设置，可能响铃30秒。服务器接收推送并不保证手机即时收到、响铃或你一定能买到；网络、系统权限和设备状态仍会影响通知。参数依据 [Bark 官方 API V2](https://github.com/Finb/bark-server/blob/master/docs/API_V2.md) 与官方客户端实现。

Bark和Telegram可以只启用一个，也可以同时启用。两渠道独立发送；一条补货通知某渠道失败时，记录成功渠道，只在后续库存检查仍有货时重试失败渠道（至少60秒间隔）。成功记录存入数据库，重启或暂停后继续仍可去重；只有再次确认售罄/超过上限或编辑任务时才开始新一轮。新增渠道不会重发已完成的补货提醒。异常通知失败只记录日志，不补发已过去的异常。发送响应丢失或进程在发送后、记录前崩溃仍可能重复，无法保证严格一次送达。

设备 Key 像Telegram Token一样加密保存在数据库；配置接口返回服务器地址和是否已保存 Key，不返回完整密钥地址或 Key。删除encryption.key会让两种渠道的凭证无法解密。服务器管理员仍可读取解密密钥。Bark正文使用HTTPS发送给所配置的推送服务，本版本没有配置Bark消息端到端加密。

## BERO 账户与登录会话

「商家账户」页面填写 BERO 登录邮箱和密码，保存后点击「验证登录」。凭证只输入到自己的 HTTPS 管理后台，不要发到聊天里。程序从 [BERO 官方登录页](https://bero-host.de/login) 取得当前表单和 CSRF 参数，用网站的邮箱/密码表单正常登录，并勾选其「保持登录」选项。此操作不创建订单、不扣款。当前仅适配邮箱密码登录，未适配 Google 等第三方登录及二步验证交互；遇到验证码、二步验证、403 或限流会停止，不会绕过验证。

密码不能仅保存哈希，因为正常重新登录需要原始凭证。密码和 Cookie 会话均使用 VPS 上的密钥加密存入数据库，配置接口不返回密码或 Cookie。更换邮箱/密码时清除旧会话并要求重新验证；只修改自动重新登录开关则保留会话。点击「删除凭证和会话」只删除本程序保存的内容，不注销商家网站的其他设备。

正常登录通过后，程序保存 Cookie，包括商家返回的持久登录 Cookie；不修改 Cookie 的到期时间。服务重启后读取保存的会话，优先复用，无需再次输入密码。后台每30分钟检查一次已验证会话，网站不再认可会话时，只有启用了「会话过期后自动重新登录」才会用保存的凭证正常登录；不开启则通知你到后台点击验证登录。一次登录失败、验证码或二步验证会停止自动尝试，避免重复提交错误密码。手动重试登录失败/安全验证至少间隔5分钟；修改凭证可重新验证。网络问题会退避检查。

无法保证永久免登录或在某个补货瞬间一定保持登录：商家可能撤销会话、修改流程、限制访问或要求二步验证。状态「已验证登录」仅代表上次验证时网站认可会话。最终下单执行器尚未接入，结账预检每次会重新确认会话，必要时按已保存的自动重新登录开关正常登录，并保存刷新后的 Cookie。真正的账户登录与预检需要在 VPS 上使用自己的账户运行；本地不会索取你的密码或 Cookie。

## BERO 下单流程（已核实）

| 步骤 | 页面 | 程序动作 |
|---|---|---|
| 500 | 选择套餐 | `selectPackage(109)` |
| 1000 | 配置 | `saveIndividualConfig`，选 365 天 |
| 1100 | 概览 | `addCode` 填 DISCORD10，核对规格、含税价 €44.10、续费 €49 |
| 1400 | 确认（CHECK） | NEXT → `jumpToStep(1400)`，再次核对规格和价格 |
| — | 条款 | `toggleAgbConfirmation`（复选框） |
| 1500 | 下单 | `<form wire:submit="jumpToStep(1500)">` ORDER NOW / JETZT BESTELLEN，从余额扣款 |

如果 NEXT 进入的是 1200（登录）或 1300（付款），说明会话或余额不对，程序停止，不下单。

## 结账预检（不下单）

「结账预检 → 运行预检」会走到 1400 确认页并读取余额，**不勾选条款、不下单**。缺货时停在概览页（NEXT 禁用），这是正常现象。报告不含邮箱、密码、Cookie、CSRF 或网页原文。

启用自动付款后，后台每 6 小时自动跑一次预检（健康检查）。如果网站结构变化、优惠码失效或余额不足，会推送普通通知，让你在补货前发现问题。

## 自动付款（一次性）

前提条件：

1. 「商家账户」已验证 BERO 登录，并建议开启「会话过期后自动重新登录」。
2. BERO 账户余额 ≥ 你设定的上限（在 bero-host.de/customer/money 充值）。
3. 任务「购买设置」中填写含税总额上限，付款方式选「BERO 账户余额」并保存。
4. 在同一对话框的「自动付款」区域再输入一次上限金额确认，点击「启用自动付款」。
5. 启用任务的监控。

补货时的流程：公开页显示可能有货 → **立即**排队自动下单（不等 15 秒的访问间隔，也不先发补货提醒）→ 读取余额 → 选择套餐 → 365 天 → DISCORD10 → 核对规格和价格（概览价和确认页价都必须 ≤ €44.10 且一致）→ 核对余额 ≥ 价格 → 写入购买记录 → 勾选条款 → 下单 → 重新读取余额，用扣款金额确认订单成功。

安全规则：

- **只下单一次。** 发送下单请求前会先在数据库写入唯一购买记录；之后无论成功或结果不明，都不会再次提交。结果不明时推送「下单结果不确定」，需要你登录 BERO 核实。
- 下单请求发出前的任何失败（缺货、价格或优惠不符、余额不足、页面结构变化、验证码、登录失效）都不会下单，并释放购买记录。若公开页仍显示有货，后台会在 15–30 秒后重试。
- 下单成功或结果不明后，自动付款会自动关闭，任务暂停。
- 保存购买设置、编辑任务都会关闭自动付款，需要重新启用。
- 想再买一台：先在 BERO 核实上一单，然后在「购买设置」里输入「已核实」清除购买记录，再重新启用。

结果通过 Bark（重要警告）和 Telegram 推送：已自动下单 / 结果不确定 / 余额不足 / 价格超限 / 优惠码未生效 / 流程变化。

限制：BERO 改版、要求二步验证或验证码、会话被撤销时，程序会停止而不是绕过，届时需要人工下单。补货通常很快售罄，程序只能尽快尝试，不能保证一定抢到。

## 升级

用 git 安装的：`cd vps-watch && git pull && bash install.sh`。用压缩包安装的：上传新包到 `/root/vps-watch.zip`，执行：

```bash
cd /root
unzip -o vps-watch.zip
cd vps-watch
bash install.sh
```

已配置的域名、管理员密码、任务和通知密钥继续使用，升级无需再次配置域名。安装会短暂重启工作台的两个服务；建议事先备份 `/var/lib/vps-watch` 和 `/etc/vps-watch.env`。升级完成后刷新网页，先运行一次「结账预检」确认能读到余额，再按上面的「自动付款」步骤启用。已保存的 BERO 会话继续复用；升级本身不会启用自动付款。

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

升级：备份后上传新版本，运行安装脚本；数据库和密钥保留，既有任务保留。安装脚本不会主动停掉旧的独立 BERO/VMISS 服务。

备份：先停止两个新服务，完整备份 `/var/lib/vps-watch` 和 `/etc/vps-watch.env`，再启动服务。目录包含数据库和密钥，应私密保管；恢复时保持目录属主 vpswatch、权限700。Telegram Token在数据库中加密，具有该数据目录权限的服务器管理员仍可解密。

## 后续接入新商家

普通静态页面可在网页直接用「通用 HTML 规则」添加。专用适配放在 `merchants/` 目录，每个商家一个文件，并在 `merchants/__init__.py` 注册：

| 必须提供 | 作用 |
|---|---|
| `NAME`、`LABEL`、`PRESETS`、`SUPPORTS_CHECKOUT` | 名称、显示名、任务模板、是否支持结账 |
| `validate(result, data)` | 校验并补充该商家特有的任务字段 |
| `inspect(soup, cfg)` | 返回 `(status, Decimal 价格, 说明)`，status 为 available / out_of_stock |

支持结账的商家还要提供 `MERCHANT`、`HOST`、`ORDERS_URL`、`CURRENCY`、`ERRORS`、`account_ready()`、`session()`、`preflight(client, cfg, policy)` 和 `purchase(client, cfg, policy, task_id)`，并且只有在真实网站上核实过流程后，才能加入 `purchases.VERIFIED_CHECKOUT_ADAPTERS`。可以参考 `merchants/bero.py`。新的适配应测试正确套餐、错误价格/周期、售罄、页面异常与可能有货等情况。

通用规则不执行网站 JavaScript、不提交表单、不使用 Cookie、不绕过验证码。公开抓取只支持 HTTPS/443；每次解析域名检查所有地址并把连接固定到验证过的公网 IP，禁止内网/回环访问和跨域跳转。后台不是任意网址代理。

## 本地开发与验证

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export WATCH_DATA="$PWD/data"
export COOKIE_SECURE=0
.venv/bin/python manage.py password
.venv/bin/python manage.py seed
.venv/bin/python app.py
# 另一个终端（Linux）
.venv/bin/python worker.py
```

网页开发入口仅适合本机测试；生产部署使用安装包中的 gunicorn 服务。

运行测试：`python -m unittest discover -s tests -v`。测试使用独立临时数据库和模拟网络/通知，不发送真实消息。浏览器 WebMCP 若可用，只暴露已登录用户的只读任务列表；不支持时不影响功能。

交付前已通过 84 项后端测试（含登录后英文界面、页面上出现重复/嵌套套餐卡片、账户下单进度停在后续步骤时自动返回、其他 BERO 套餐配置、旧版数据自动迁移的测试）。新增的自动付款测试按 2026-10-09 实际核实的页面结构（`tests/fixtures/bero-check109.html`、`bero-money.html`）回放完整下单流程，覆盖：调用顺序与签名快照不变、缺货不占用购买记录、余额不足、确认页价格或规格变化、下单表单或条款控件变化、条款未生效时释放记录、下单响应丢失时标记不确定且绝不重发、未启用时禁止勾选条款和下单、补货时先下单后提醒、购买任务跳过访问间隔、启用需要输入金额确认和已验证登录、清除记录需要输入「已核实」。商家交互全部使用模拟页面，测试中没有向 BERO 发送真实请求。真实补货时的下单响应（跳转或感谢页）还没有在真实环境中出现过，程序以余额变化作为成功依据。当前环境未执行 Debian 13 上的安装脚本和 systemd 服务联调。

端口说明：工作台使用 18080。安装脚本通过 /healthz 确认程序身份，域名脚本先验证本机 HTTPS 再报告成功。

## 免责声明

本项目供个人在自己的服务器上使用自己的商家账户。自动下单会真实扣款，请先用「结账预检」确认流程，并自行承担使用风险。使用前请阅读商家的服务条款，部分商家禁止自动化下单。程序遇到验证码、二步验证或访问限制时会停止，不会绕过；请勿修改代码去绕过商家的安全措施，也不要设置过高的检查频率。

## 许可证

MIT License，见 [LICENSE](LICENSE)。
