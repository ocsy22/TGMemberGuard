#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l
if [[ ${EUID} -ne 0 ]]; then
    echo '用法：sudo bash install.sh' >&2
    exit 1
fi
if [[ ! -d /run/systemd/system ]]; then
    echo '需要以 systemd 启动的 Ubuntu 服务器，不能在普通容器中安装此服务。' >&2
    exit 1
fi
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
for item in server.py guard.py activity.py webapp.py requirements.txt tgmemberguard tgmemberguard.service uninstall.sh templates/setup.html templates/login.html templates/dashboard.html static/app.js static/app.css; do
    [[ -f "$ROOT_DIR/$item" ]] || { echo "部署包缺少 $item" >&2; exit 1; }
done
apt-get update
apt-get install -y python3 python3-venv ca-certificates tzdata
if ! id tgmemberguard >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /var/lib/tgmemberguard --no-create-home --shell /usr/sbin/nologin tgmemberguard
fi
if systemctl cat tgmemberguard.service >/dev/null 2>&1; then
    systemctl stop tgmemberguard.service
fi
install -d -o root -g root -m 0755 /opt/tgmemberguard
install -d -o root -g tgmemberguard -m 2750 /etc/tgmemberguard
install -d -o tgmemberguard -g tgmemberguard -m 0700 /var/lib/tgmemberguard
for item in server.py guard.py activity.py webapp.py requirements.txt; do
    install -o root -g root -m 0644 "$ROOT_DIR/$item" "/opt/tgmemberguard/$item"
done
for folder in templates static; do
    install -d -o root -g root -m 0755 "/opt/tgmemberguard/$folder"
    for item in "$ROOT_DIR/$folder/"*; do
        install -o root -g root -m 0644 "$item" "/opt/tgmemberguard/$folder/$(basename -- "$item")"
    done
done
for item in README.md config.example.json; do
    if [[ -f "$ROOT_DIR/$item" ]]; then install -o root -g root -m 0644 "$ROOT_DIR/$item" "/opt/tgmemberguard/$item"; fi
done
python3 -m venv /opt/tgmemberguard/.venv
/opt/tgmemberguard/.venv/bin/python -m pip install --disable-pip-version-check -r /opt/tgmemberguard/requirements.txt
# deploy.sh uses umask 077. The root-owned environment contains code, not
# credentials, and must be readable/traversable by the service account.
# Also repairs environments created by earlier installers with that umask.
chmod -R a+rX -- /opt/tgmemberguard/.venv
(
    cd /opt/tgmemberguard
    runuser -u tgmemberguard -- /opt/tgmemberguard/.venv/bin/python -c 'import server, activity, webapp, waitress'
)
install -o root -g root -m 0755 "$ROOT_DIR/tgmemberguard" /usr/local/bin/tgmemberguard
install -o root -g root -m 0644 "$ROOT_DIR/tgmemberguard.service" /etc/systemd/system/tgmemberguard.service
install -o root -g root -m 0755 "$ROOT_DIR/uninstall.sh" /opt/tgmemberguard/uninstall.sh
systemctl daemon-reload
systemctl enable tgmemberguard.service
systemctl start tgmemberguard.service
echo '安装完成；打开 http://127.0.0.1:8787 创建账号并配置机器人。已有配置及名单保留。'
