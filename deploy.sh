#!/usr/bin/env bash
# Public bootstrap. Usage: curl -fsSL RAW_URL | sudo bash -s -- [commit-or-ref]
set -euo pipefail
umask 077
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
FIRST_INSTALL=0
if [[ ! -f /etc/tgmemberguard/config.json && ! -f /var/lib/tgmemberguard/runtime-config.json ]]; then
    FIRST_INSTALL=1
    if [[ ! -r /dev/tty ]]; then
        echo '缺少交互终端。请通过SSH登录后运行：sudo tgmemberguard init && sudo tgmemberguard web-init' >&2
        exit 1
    fi
    tgmemberguard init </dev/tty
fi
if [[ ! -s /var/lib/tgmemberguard/web-auth.json ]]; then
    tgmemberguard web-init </dev/tty
fi
if [[ "$FIRST_INSTALL" -eq 1 ]]; then
    printf '现在启用正式踢人审核？输入 y 确认，直接回车保持试运行 [y/N]：' >/dev/tty
    read -r LIVE </dev/tty
    if [[ "$LIVE" == y || "$LIVE" == Y ]]; then
        tgmemberguard mode live
    fi
fi
if ! tgmemberguard check; then
    echo '部分Telegram权限或来源检查未通过。程序仍会启动并按保护规则重试，请在后台检查。' >&2
fi
systemctl restart tgmemberguard
systemctl is-active --quiet tgmemberguard
echo
echo '部署完成，服务已启动并设为开机自启。'
echo '网页默认地址：http://127.0.0.1:8787 （服务器本机）'
echo '电脑访问：ssh -N -L 8787:127.0.0.1:8787 用户名@服务器IP'
echo '然后在电脑浏览器打开：http://127.0.0.1:8787'
echo '查看日志：sudo journalctl -u tgmemberguard -f'
echo '管理员聊天ID：私聊机器人发送 /id，然后在网页“通知与日报”中填写。'
