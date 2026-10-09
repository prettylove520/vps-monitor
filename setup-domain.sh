#!/usr/bin/env bash
# VPS Watch domain setup. Existing unrelated nginx sites are preserved.
set -euo pipefail
if [ "$(id -u)" != 0 ]; then echo '请使用 sudo bash setup-domain.sh 域名'; exit 1; fi
DOMAIN="${1:-}"
CERT_EMAIL="${2:-}"
HTTPS_PORT="${VPS_WATCH_HTTPS_PORT:-443}"
if ! [[ "$HTTPS_PORT" =~ ^[0-9]{1,5}$ ]]; then
    echo 'HTTPS 端口格式不正确。'; exit 1
fi
if [ "$HTTPS_PORT" != 443 ] && { [ "$HTTPS_PORT" -lt 1024 ] || [ "$HTTPS_PORT" -gt 65535 ]; }; then
    echo 'HTTPS 端口必须为443，或1024–65535。'; exit 1
fi
if [ "$HTTPS_PORT" = 443 ]; then
    TLS_LISTEN=$'listen 443 ssl;\n    listen [::]:443 ssl;'
else
    TLS_LISTEN="listen 127.0.0.1:${HTTPS_PORT} ssl;"
fi
if [ -z "$DOMAIN" ]; then read -r -p '请输入完整域名（例如 watch.example.com）：' DOMAIN; fi
DOMAIN="$(printf '%s' "$DOMAIN" | tr '[:upper:]' '[:lower:]')"
python3 - "$DOMAIN" <<'PY'
import re,sys
s=sys.argv[1]
if len(s)>253 or '.' not in s or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',x) for x in s.split('.')) or s.split('.')[-1].isdigit():
    sys.exit('域名格式不正确。只填写域名，不含 https://、端口或路径。')
PY
if [ ! -f /etc/vps-watch.env ] || [ ! -x /opt/vps-watch/.venv/bin/gunicorn ]; then
    echo '请先运行 bash install.sh 安装网页，再配置域名。'; exit 1
fi
if ! systemctl is-active --quiet vps-watch-web; then
    systemctl start vps-watch-web
fi
/opt/vps-watch/.venv/bin/python /opt/vps-watch/verify-web.py
apt-get update
apt-get install -y nginx certbot curl
SITE="/etc/nginx/sites-available/vps-watch-${DOMAIN}.conf"
LINK="/etc/nginx/sites-enabled/vps-watch-${DOMAIN}.conf"
if [ -e "$SITE" ] && ! head -n 1 "$SITE" | grep -qx '# Managed by VPS Watch'; then
    echo "配置文件已存在且不属于本程序：$SITE，请人工检查。"; exit 1
fi
if [ -e "$LINK" ] || [ -L "$LINK" ]; then
    if [ "$(readlink -f "$LINK")" != "$SITE" ]; then
        echo "同名启用配置已存在且指向其他文件：$LINK"; exit 1
    fi
fi
BACKUP="$(mktemp)"
HAD_SITE=0
HAD_LINK=0
if [ -f "$SITE" ]; then cp -- "$SITE" "$BACKUP"; HAD_SITE=1; fi
if [ -L "$LINK" ]; then HAD_LINK=1; fi
cleanup() { rm -f -- "$BACKUP"; }
rollback() {
    if [ "$HAD_SITE" = 1 ]; then cp -- "$BACKUP" "$SITE"; else rm -f -- "$SITE"; fi
    if [ "$HAD_LINK" = 0 ]; then rm -f -- "$LINK"; fi
    nginx -t && systemctl reload nginx || true
}
trap cleanup EXIT
install -d -m 755 /var/lib/vps-watch-acme/.well-known/acme-challenge
# During issuance, HTTP exposes only ACME files, never the login form.
cat > "$SITE" <<EOF
# Managed by VPS Watch
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};
    location ^~ /.well-known/acme-challenge/ { root /var/lib/vps-watch-acme; }
    location / { return 503; }
}
EOF
ln -sfn -- "$SITE" "$LINK"
if ! nginx -t; then rollback; exit 1; fi
if ! systemctl enable --now nginx || ! systemctl reload nginx; then rollback; exit 1; fi
CERT_ARGS=(certonly --webroot -w /var/lib/vps-watch-acme -d "$DOMAIN" --cert-name "$DOMAIN" --non-interactive --agree-tos)
if [ -n "$CERT_EMAIL" ]; then CERT_ARGS+=(--email "$CERT_EMAIL"); else CERT_ARGS+=(--register-unsafely-without-email); fi
if ! certbot "${CERT_ARGS[@]}"; then
    rollback
    echo '证书申请失败。请检查域名的 A/AAAA 解析、VPS 防火墙/安全组的 80 端口，以及已有网站配置。'; exit 1
fi
cat > "$SITE" <<EOF
# Managed by VPS Watch
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};
    location ^~ /.well-known/acme-challenge/ { root /var/lib/vps-watch-acme; }
    location / { return 301 https://${DOMAIN}\$request_uri; }
}
server {
    ${TLS_LISTEN}
    server_name ${DOMAIN};
    ssl_certificate /etc/letsencrypt/live/${DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${DOMAIN}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    client_max_body_size 32k;
    location / {
        proxy_pass http://127.0.0.1:18080;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 60s;
    }
}
EOF
if ! nginx -t; then rollback; exit 1; fi
install -d -m 755 /etc/letsencrypt/renewal-hooks/deploy
cat > /etc/letsencrypt/renewal-hooks/deploy/vps-watch-reload-nginx <<'EOF'
#!/bin/sh
nginx -t && systemctl reload nginx
EOF
chmod 755 /etc/letsencrypt/renewal-hooks/deploy/vps-watch-reload-nginx
if ! systemctl reload nginx; then rollback; exit 1; fi
if grep -q '^COOKIE_SECURE=' /etc/vps-watch.env; then
    sed -i 's/^COOKIE_SECURE=.*/COOKIE_SECURE=1/' /etc/vps-watch.env
else
    printf '\nCOOKIE_SECURE=1\n' >> /etc/vps-watch.env
fi
systemctl restart vps-watch-web
/opt/vps-watch/.venv/bin/python /opt/vps-watch/verify-web.py
systemctl enable --now certbot.timer
if ! curl --fail --silent --show-error --max-time 10 --resolve "${DOMAIN}:${HTTPS_PORT}:127.0.0.1" "https://${DOMAIN}:${HTTPS_PORT}/healthz" | python3 -c "import json,sys; sys.exit(0 if json.load(sys.stdin).get('application')=='vps-watch' else 1)"; then
    echo '配置已写入，但本机 HTTPS 身份检查失败。请检查 HTTPS 端口监听和现有入口配置。'; exit 1
fi
echo "本机 HTTPS 检查通过，请打开 https://${DOMAIN} 并用原来的管理员密码登录。"
echo 'HTTP 将跳转到 HTTPS。证书自动续期，18080 仍仅监听本机；公网可达性需另行确认。'
