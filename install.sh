#!/usr/bin/env bash
set -euo pipefail
if [[ ${EUID} -ne 0 ]]; then
    echo '用法：sudo bash install.sh' >&2
    exit 1
fi
if [[ ! -d /run/systemd/system ]]; then
    echo '需要以 systemd 启动的 Ubuntu 服务器，不能在普通容器中安装此服务。' >&2
    exit 1
fi
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
for item in server.py guard.py activity.py webapp.py requirements.txt tgmemberguard tgmemberguard.service templates/login.html templates/dashboard.html static/app.js static/app.css; do
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
install -o root -g root -m 0755 "$ROOT_DIR/tgmemberguard" /usr/local/bin/tgmemberguard
install -o root -g root -m 0644 "$ROOT_DIR/tgmemberguard.service" /etc/systemd/system/tgmemberguard.service
systemctl daemon-reload
systemctl enable tgmemberguard.service
echo '安装完成；配置和数据不会被覆盖。'
echo '网页账号：sudo tgmemberguard web-init；默认监听127.0.0.1:8787。'
if [[ -f /etc/tgmemberguard/config.json ]]; then
    echo '已有配置：sudo tgmemberguard check，然后 sudo systemctl start tgmemberguard'
else
    echo '首次使用：sudo tgmemberguard init'
    echo '然后：sudo tgmemberguard check && sudo systemctl start tgmemberguard'
fi
