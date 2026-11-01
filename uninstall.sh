#!/usr/bin/env bash
set -euo pipefail
PURGE=0
YES=0
DRY=0
for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=1 ;;
        --yes) YES=1 ;;
        --dry-run) DRY=1 ;;
        --help|-h) echo 'sudo tgmemberguard uninstall [--purge] [--yes] [--dry-run]'; exit 0 ;;
        *) echo "未知参数：$arg" >&2; exit 1 ;;
    esac
done
paths=(/opt/tgmemberguard /usr/local/bin/tgmemberguard /etc/systemd/system/tgmemberguard.service)
if [[ $PURGE -eq 1 ]]; then paths+=(/etc/tgmemberguard /var/lib/tgmemberguard); fi
if [[ $DRY -eq 1 ]]; then printf '将删除：%s\n' "${paths[@]}"; exit 0; fi
if [[ $EUID -ne 0 ]]; then echo '请使用sudo执行卸载。' >&2; exit 1; fi
if [[ $PURGE -eq 1 && $YES -ne 1 ]]; then
    echo '将永久删除配置、Bot Token、后台账号及全部黑白名单。输入 DELETE 确认：' >/dev/tty
    read -r answer </dev/tty
    [[ "$answer" == DELETE ]] || { echo '已取消。'; exit 1; }
fi
systemctl stop tgmemberguard.service || true
systemctl disable tgmemberguard.service || true
if systemctl is-active --quiet tgmemberguard.service; then
    echo '服务仍在运行，停止卸载。' >&2; exit 1
fi
# Fixed absolute paths only. rm removes symlinks themselves; never follows them.
for path in "${paths[@]}"; do rm -rf --one-file-system -- "$path"; done
systemctl daemon-reload
systemctl reset-failed tgmemberguard.service 2>/dev/null || true
if [[ $PURGE -eq 1 ]]; then echo '已卸载并删除配置及名单数据。'; else echo '已卸载；配置及名单保留，重新安装可继续使用。'; fi
