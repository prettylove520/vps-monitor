#!/usr/bin/env bash
# Turn an SSR node (e.g. a US residential exit) into a local SOCKS5 proxy on 127.0.0.1:1080 for VPS Monitor.
# Run as root on the monitor server:  bash ssr-socks.sh
# It installs the open-source mihomo client, writes its config (root-only), and runs it as a systemd service.
set -euo pipefail
if [ "$(id -u)" != 0 ]; then echo '请使用 root 运行'; exit 1; fi
PORT=${SOCKS_PORT:-1080}
CONF=/etc/mihomo
apt-get install -y curl gzip python3 ca-certificates >/dev/null

if [ -n "${SSR_LINK:-}" ]; then LINK=$SSR_LINK; else read -r -s -p '粘贴 SSR 链接（ssr://…，输入时不显示）：' LINK; echo; fi

install -d -m 700 "$CONF"
printf '%s' "$LINK" | PORT=$PORT python3 -c '
import base64, json, os, sys, urllib.parse

def b64(text):
    text = text.strip().replace("-", "+").replace("_", "/")
    return base64.b64decode(text + "=" * (-len(text) % 4)).decode("utf-8", "replace")

link = sys.stdin.read().strip()
if not link.startswith("ssr://"):
    sys.exit("不是 ssr:// 开头的链接")
body = b64(link[6:])
main, _, query = body.partition("/?")
host, port, protocol, method, obfs, password = main.rsplit(":", 5)
params = urllib.parse.parse_qs(query)
get = lambda k: b64(params[k][0]) if k in params else ""
q = json.dumps  # JSON strings are valid YAML scalars
config = "\n".join([
    "socks-port: %s" % os.environ["PORT"],
    "bind-address: 127.0.0.1",
    "allow-lan: false",
    "mode: rule",
    "log-level: warning",
    "ipv6: false",
    "proxies:",
    "  - name: exit",
    "    type: ssr",
    "    server: %s" % q(host),
    "    port: %d" % int(port),
    "    cipher: %s" % q(method),
    "    password: %s" % q(b64(password)),
    "    obfs: %s" % q(obfs),
    "    protocol: %s" % q(protocol),
    "    obfs-param: %s" % q(get("obfsparam")),
    "    protocol-param: %s" % q(get("protoparam")),
    "    udp: false",
    "rules:",
    "  - MATCH,exit",
    "",
])
open("/etc/mihomo/config.yaml", "w").write(config)
print("节点：%s:%s  加密：%s  协议：%s  混淆：%s" % (host, port, method, protocol, obfs))
'
chmod 600 "$CONF/config.yaml"

if [ ! -x /usr/local/bin/mihomo ]; then
    case "$(uname -m)" in x86_64) A=amd64 ;; aarch64) A=arm64 ;; *) echo "不支持的 CPU：$(uname -m)"; exit 1 ;; esac
    URL=$(curl -fsSL https://api.github.com/repos/MetaCubeX/mihomo/releases/latest | A=$A python3 -c '
import json, os, re, sys
assets = [a["browser_download_url"] for a in json.load(sys.stdin)["assets"]]
a = os.environ["A"]
for pattern in (r"mihomo-linux-%s-v\d+\.\d+\.\d+\.gz$", r"mihomo-linux-%s-compatible-v\d+\.\d+\.\d+\.gz$"):
    for u in assets:
        if re.search(pattern % a, u):
            print(u); sys.exit()
sys.exit("没有找到适合的 mihomo 安装包")
')
    echo "下载 $URL"
    curl -fsSL "$URL" | gunzip > /usr/local/bin/mihomo
    chmod 755 /usr/local/bin/mihomo
fi
/usr/local/bin/mihomo -t -d "$CONF" >/dev/null

cat > /etc/systemd/system/ssr-socks.service <<UNIT
[Unit]
Description=Local SOCKS5 (127.0.0.1:$PORT) through the SSR exit, for VPS Monitor
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/local/bin/mihomo -d $CONF
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable ssr-socks >/dev/null 2>&1
systemctl restart ssr-socks
sleep 4

echo '--- 检查 ---'
if ! systemctl is-active --quiet ssr-socks; then echo '服务没有启动，查看：journalctl -u ssr-socks -n 30 --no-pager'; exit 1; fi
IP=$(curl --socks5-hostname 127.0.0.1:$PORT -s --max-time 15 https://api.ipify.org || true)
echo "出口 IP：${IP:-获取失败（节点连不上或密码/加密方式不对）}"
CODE=$(curl --socks5-hostname 127.0.0.1:$PORT -s -o /dev/null -w '%{http_code}' --max-time 20 -A 'Mozilla/5.0' https://app.vmiss.com/store/us-los-angeles-tri || true)
echo "VMISS 返回：${CODE:-000}（200 表示可以用）"
echo "监控任务的「代理」栏填：socks5://127.0.0.1:$PORT"
