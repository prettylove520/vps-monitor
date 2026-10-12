#!/usr/bin/env bash
# Root-side updater, started by vps-watch-update.path after the workbench asks for an update.
# Downloads the repository named in /etc/vps-watch.env (UPDATE_REPO), verifies it, installs it and rolls back
# if the new version does not come up healthy. Nothing in the request file is trusted.
set -uo pipefail

APP=${APP:-/opt/vps-watch}
DATA=${DATA:-/var/lib/vps-watch}
BACKUPS=${BACKUPS:-/var/backups/vps-watch}
ENV_FILE=${ENV_FILE:-/etc/vps-watch.env}
SYSTEMCTL=${SYSTEMCTL:-systemctl}
PIP=${PIP:-$APP/.venv/bin/pip}
PYTHON=${PYTHON:-$APP/.venv/bin/python}
HEALTH_URL=${HEALTH_URL:-http://127.0.0.1:18080/healthz}
OWNER=${OWNER:-vpswatch}
UNIT_DIR=${UNIT_DIR:-/etc/systemd/system}
KEEP=5
WORK=''

env_value() { sed -n "s/^$1=//p" "$ENV_FILE" 2>/dev/null | tail -n1; }

set_status() { # state message [from] [to] [percent]
    local tmp="$DATA/.update-status.$$"
    "$PYTHON" - "$tmp" "$1" "$2" "${3:-}" "${4:-}" "${5:-}" <<'PY'
import json, sys, time
path, state, message, old, new, percent = sys.argv[1:7]
info = {'state': state, 'message': message, 'from': old, 'to': new, 'time': time.time()}
if percent:
    info['percent'] = int(percent)
open(path, 'w').write(json.dumps(info, ensure_ascii=False))
PY
    chmod 644 "$tmp" && mv -f "$tmp" "$DATA/update-status.json"
    chown "$OWNER" "$DATA/update-status.json" 2>/dev/null || true
}

current_version() { sed -n "s/^VERSION *= *['\"]\([0-9.]*\)['\"].*/\1/p" "${1:-$APP}/version.py" | head -n1; }

version_newer() { # a b : a > b
    [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -n1)" = "$1" ]
}

fetch_source() { # repo dest
    curl -fsSL --max-time 120 --proto '=https' "https://codeload.github.com/$1/tar.gz/refs/heads/main" -o "$2/src.tar.gz" &&
        mkdir "$2/src" && tar -xzf "$2/src.tar.gz" -C "$2/src" --strip-components=1
}

validate_source() { # dir
    local d=$1 f
    [ -d "$d" ] || return 1
    if [ -n "$(find "$d" -type l -print -quit)" ]; then echo '更新包含符号链接，已拒绝'; return 1; fi
    for f in app.py worker.py store.py version.py requirements.txt install.sh deploy/update.sh static/index.html; do
        [ -f "$d/$f" ] || { echo "更新包缺少 $f"; return 1; }
    done
    "$PYTHON" -I - "$d" <<'PY' || return 1
import py_compile, sys, pathlib, tempfile, os
root = pathlib.Path(sys.argv[1])
with tempfile.TemporaryDirectory() as tmp:
    for i, f in enumerate(sorted(root.glob('*.py')) + sorted(root.glob('merchants/*.py'))):
        py_compile.compile(str(f), cfile=os.path.join(tmp, '%d.pyc' % i), doraise=True)
PY
}

backup() { # version
    local stamp dest
    stamp=$(date +%Y%m%d-%H%M%S)
    dest="$BACKUPS/$stamp-v$1"
    mkdir -p "$dest" && chmod 700 "$BACKUPS" "$dest" || return 1
    tar -C "$APP" --exclude=.venv -czf "$dest/app.tar.gz" . || return 1
    if [ -f "$DATA/watch.db" ]; then
        "$PYTHON" -I - "$DATA/watch.db" "$dest/watch.db" <<'PY' || return 1
import sqlite3, sys
src = sqlite3.connect(sys.argv[1], timeout=30)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
PY
    fi
    BACKUP_DIR=$dest
    local old
    old=$(ls -1d "$BACKUPS"/*/ 2>/dev/null | sort | head -n -$KEEP)
    [ -z "$old" ] || echo "$old" | while read -r d; do rm -rf -- "$d"; done
}

install_source() { # dir
    local d=$1 f
    for f in "$d"/*.py "$d"/requirements.txt; do install -m 644 "$f" "$APP/$(basename "$f")" || return 1; done
    install -m 644 "$d"/static/* "$APP/static/" || return 1
    install -m 644 "$d"/merchants/*.py "$APP/merchants/" || return 1
    install -m 755 "$d/deploy/update.sh" "$APP/update.sh" || return 1
    install -m 644 "$d"/deploy/vps-watch-web.service "$d"/deploy/vps-watch-worker.service \
        "$d"/deploy/vps-watch-update.service "$d"/deploy/vps-watch-update.path "$UNIT_DIR/" 2>/dev/null || true
    "$SYSTEMCTL" daemon-reload || true
}

pip_install() { "$PIP" install -q -r "$APP/requirements.txt"; }

restart_services() { "$SYSTEMCTL" restart vps-watch-web vps-watch-worker; }

healthy() { # expected version
    local i body
    for i in $(seq 1 30); do
        body=$(curl -fsS --max-time 3 "$HEALTH_URL" 2>/dev/null || true)
        if printf '%s' "$body" | grep -q "\"version\": *\"$1\"" &&
            "$SYSTEMCTL" is-active --quiet vps-watch-web && "$SYSTEMCTL" is-active --quiet vps-watch-worker; then
            return 0
        fi
        sleep 2
    done
    return 1
}

rollback() { # backup_dir
    local dir=$1
    "$SYSTEMCTL" stop vps-watch-web vps-watch-worker 2>/dev/null || true
    # remove python files first so a file that only exists in the new version cannot linger
    find "$APP" -maxdepth 2 -path "$APP/.venv" -prune -o -type f \( -name '*.py' -o -name '*.html' -o -name '*.js' -o -name '*.css' \) -print0 | xargs -0 -r rm -f
    tar -C "$APP" -xzf "$dir/app.tar.gz" || return 1
    if [ -f "$dir/watch.db" ]; then
        cp -f "$dir/watch.db" "$DATA/watch.db" && rm -f "$DATA/watch.db-wal" "$DATA/watch.db-shm"
        chown "$OWNER" "$DATA/watch.db" 2>/dev/null || true
        chmod 600 "$DATA/watch.db"
    fi
    "$PIP" install -q -r "$APP/requirements.txt" 2>/dev/null || true
    "$SYSTEMCTL" start vps-watch-web vps-watch-worker
}

cleanup() { [ -z "$WORK" ] || rm -rf -- "$WORK"; }

main() {
    rm -f -- "$DATA/update-request"
    local repo old new
    repo=$(env_value UPDATE_REPO); repo=${repo:-prettylove520/vps-monitor}
    if ! printf '%s' "$repo" | grep -Eq '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'; then
        set_status failed 'UPDATE_REPO 格式不正确' ; return 1
    fi
    old=$(current_version)
    set_status running '正在下载新版本' "$old" '' 10
    WORK=$(mktemp -d) || { set_status failed '无法创建临时目录' "$old"; return 1; }
    trap cleanup EXIT
    if ! fetch_source "$repo" "$WORK"; then set_status failed '下载失败，请检查服务器能否访问 GitHub' "$old"; return 1; fi
    new=$(current_version "$WORK/src")
    if [ -z "$new" ]; then set_status failed '无法识别新版本号' "$old"; return 1; fi
    if ! version_newer "$new" "$old"; then set_status done "已是最新版本 v$old" "$old" "$old" 100; return 0; fi
    set_status running '正在校验新版本' "$old" "$new" 25
    local why
    if ! why=$(validate_source "$WORK/src" 2>&1); then set_status failed "新版本校验未通过：${why:-文件不完整}" "$old" "$new"; return 1; fi
    set_status running '正在备份当前版本和数据' "$old" "$new" 40
    BACKUP_DIR=''
    if ! backup "$old"; then set_status failed '备份失败，未做任何修改' "$old" "$new"; return 1; fi
    set_status running '正在安装新版本' "$old" "$new" 55
    if install_source "$WORK/src" && set_status running '正在安装依赖' "$old" "$new" 70 && pip_install &&
        set_status running '正在重启服务' "$old" "$new" 80 && restart_services &&
        set_status running '正在检查新版本是否正常' "$old" "$new" 90 && healthy "$new"; then
        set_status done "已更新到 v$new" "$old" "$new" 100
        return 0
    fi
    set_status running '新版本没有正常启动，正在退回' "$old" "$new" 90
    if rollback "$BACKUP_DIR" && healthy "$old"; then
        set_status rolled_back "新版本未能启动，已自动退回 v$old，数据完好" "$old" "$new"
    else
        set_status failed "更新失败且退回未完成，请登录服务器查看；备份在 $BACKUP_DIR" "$old" "$new"
    fi
    return 1
}

# Run only when executed directly, so tests can source the functions.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then main "$@"; exit; fi
