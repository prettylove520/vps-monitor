#!/usr/bin/env bash
set -euo pipefail
if [ "$(id -u)" != 0 ]; then echo '请使用 sudo bash install.sh'; exit 1; fi
cd -- "$(dirname -- "$0")"
apt-get update
apt-get install -y python3 python3-venv ca-certificates
if ! id vpswatch >/dev/null 2>&1; then useradd --system --home /var/lib/vps-watch --shell /usr/sbin/nologin vpswatch; fi
install -d -m 755 /opt/vps-watch /opt/vps-watch/static /opt/vps-watch/merchants
install -d -m 700 -o vpswatch -g vpswatch /var/lib/vps-watch
if systemctl is-active --quiet vps-watch-web; then systemctl stop vps-watch-web; fi
if systemctl is-active --quiet vps-watch-worker; then systemctl stop vps-watch-worker; fi
for f in app.py store.py network.py adapters.py notifications.py purchases.py accounts.py checkout.py worker.py manage.py verify-web.py requirements.txt; do install -m 644 "$f" "/opt/vps-watch/$f"; done
install -m 644 static/* /opt/vps-watch/static/
install -m 644 merchants/*.py /opt/vps-watch/merchants/
python3 -m venv /opt/vps-watch/.venv
/opt/vps-watch/.venv/bin/pip install -r /opt/vps-watch/requirements.txt
if [ ! -f /etc/vps-watch.env ]; then
    printf 'WATCH_DATA=/var/lib/vps-watch\nCOOKIE_SECURE=0\n' > /etc/vps-watch.env
    chown root:vpswatch /etc/vps-watch.env
    chmod 640 /etc/vps-watch.env
fi
if ! runuser -u vpswatch -- env WATCH_DATA=/var/lib/vps-watch /opt/vps-watch/.venv/bin/python -c "import sys; sys.path.insert(0,'/opt/vps-watch'); from store import setting; sys.exit(0 if setting('admin_password') else 1)"; then
    runuser -u vpswatch -- env WATCH_DATA=/var/lib/vps-watch /opt/vps-watch/.venv/bin/python /opt/vps-watch/manage.py password
fi
runuser -u vpswatch -- env WATCH_DATA=/var/lib/vps-watch /opt/vps-watch/.venv/bin/python /opt/vps-watch/manage.py seed
install -m 644 deploy/vps-watch-web.service deploy/vps-watch-worker.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now vps-watch-web vps-watch-worker
/opt/vps-watch/.venv/bin/python /opt/vps-watch/verify-web.py
if [ -n "${1:-}" ]; then
    bash setup-domain.sh "$1" "${2:-}"
else
    echo '安装完成。网页仅监听 127.0.0.1:18080。'
    echo '使用域名：bash setup-domain.sh 你的域名'
    echo '或建立SSH转发后打开 http://127.0.0.1:18080 。任务默认暂停。'
fi
