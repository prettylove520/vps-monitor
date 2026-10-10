# 开发与接入新商家

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

当前共 97 项后端测试。新增的自动付款测试按 2026-10-09 实际核实的页面结构（`tests/fixtures/bero-check109.html`、`bero-money.html`）回放完整下单流程，覆盖：调用顺序与签名快照不变、缺货不占用购买记录、余额不足、确认页价格或规格变化、下单表单或条款控件变化、条款未生效时释放记录、下单响应丢失时标记不确定且绝不重发、未启用时禁止勾选条款和下单、补货时先下单后提醒、购买任务跳过访问间隔、启用需要输入金额确认和已验证登录、清除记录需要输入「已核实」。商家交互全部使用模拟页面，测试中没有向 BERO 发送真实请求。真实补货时的下单响应（跳转或感谢页）还没有在真实环境中出现过，程序以余额变化作为成功依据。当前环境未执行 Debian 13 上的安装脚本和 systemd 服务联调。

端口说明：工作台使用 18080。安装脚本通过 /healthz 确认程序身份，域名脚本先验证本机 HTTPS 再报告成功。

## 模块一览

| 文件 | 作用 |
|---|---|
| `app.py` | 网页和 API |
| `worker.py` | 后台检查循环、补货通知、自动下单排队 |
| `adapters.py` / `merchants/` | 任务校验和各商家的页面识别、结账流程 |
| `checkout.py` / `purchases.py` | 结账预检与自动下单队列、一次性购买保护 |
| `stock.py` | 补货记录（库存状态变化） |
| `notifications.py` | Bark、微信、Telegram |
| `updates.py` | 每天检查 GitHub 最新 Release |
| `accounts.py` / `twofactor.py` | BERO 登录会话、管理员两步验证 |

## 发布新版本

1. 改 `version.py` 和 `CHANGELOG.md`，提交并确认 GitHub Actions 是绿色。
2. 打标签并推送：`git tag v0.4.0 && git push origin v0.4.0`。
3. 在 GitHub 上用这个标签建一个 Release（标题写版本号，正文贴 CHANGELOG 对应段落）。各用户的工作台第二天就会看到「有新版本」提示。
