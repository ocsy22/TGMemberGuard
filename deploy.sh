#!/usr/bin/env bash
# Public bootstrap. Usage: curl -fsSL RAW_URL | sudo bash -s -- [commit-or-ref]
set -euo pipefail
umask 077
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l
if [[ ${EUID} -ne 0 ]]; then
    echo '请使用 curl ... | sudo bash -s -- 执行部署。' >&2
    exit 1
fi
if [[ ! -d /run/systemd/system ]]; then
    echo '此安装器需要以systemd启动的Ubuntu服务器。' >&2
    exit 1
fi
REF="${1:-main}"
if [[ ! "$REF" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]]; then
    echo '无效的GitHub版本/提交引用。' >&2
    exit 1
fi
if ! command -v curl >/dev/null || ! command -v tar >/dev/null; then
    apt-get update
    apt-get install -y curl ca-certificates tar
fi
WORK_DIR="$(mktemp -d /tmp/tgmemberguard-deploy.XXXXXXXX)"
cleanup() {
    if [[ "$WORK_DIR" == /tmp/tgmemberguard-deploy.* && -d "$WORK_DIR" && ! -L "$WORK_DIR" ]]; then
        rm -rf -- "$WORK_DIR"
    fi
}
trap cleanup EXIT
echo "下载 TGMemberGuard：ocsy22/TGMemberGuard @ $REF"
curl --fail --show-error --silent --location --retry 3 --connect-timeout 15 \
    "https://codeload.github.com/ocsy22/TGMemberGuard/tar.gz/$REF" -o "$WORK_DIR/source.tar.gz"
mkdir "$WORK_DIR/source"
tar --extract --gzip --file "$WORK_DIR/source.tar.gz" --directory "$WORK_DIR/source" --strip-components=1 --no-same-owner
if [[ ! -f "$WORK_DIR/source/install.sh" || ! -f "$WORK_DIR/source/server.py" ]]; then
    echo '下载内容不是有效的TGMemberGuard部署仓库，已停止。' >&2
    exit 1
fi
bash "$WORK_DIR/source/install.sh"
systemctl restart tgmemberguard
systemctl is-active --quiet tgmemberguard
echo
echo '部署完成，服务已启动并设为开机自启。'
echo '使用上方显示的服务器IP后台链接访问，无需SSH隧道。'
echo '查看日志：sudo journalctl -u tgmemberguard -f'
echo '管理员聊天ID：私聊机器人发送 /id，然后在网页“通知与日报”中填写。'
echo '首次打开网页创建管理员账号，然后在频道配置填写Bot Token和频道ID。'
echo '卸载（保留数据）：sudo tgmemberguard uninstall'
echo '彻底删除：sudo tgmemberguard uninstall --purge'
