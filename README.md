# TGMemberGuard

Ubuntu服务器上的Telegram多频道会员准入管理：账号密码网页后台、来源会员校验、永久黑名单、历史白名单、管理员通知和北京时间零点日报。

## 一键部署

在Ubuntu 22.04/24.04的SSH终端执行：

```bash
curl -fsSL https://raw.githubusercontent.com/ocsy22/TGMemberGuard/main/deploy.sh | sudo bash
```

安装全程无需填写任何项目参数。首次打开网页创建管理员账号，再在频道配置填写Bot Token、来源和多个目标频道ID；默认试运行，可在网页关闭试运行开始正式审核。再次运行可升级，配置和名单保留。Token和密码只在服务器本机输入，不要提交到GitHub。必须停用使用同一Token的其他审核进程。

后台直接通过 `http://服务器IP:8787` 访问，无需SSH隧道。安装器会显示实际链接；首次创建账号请使用带随机密钥的完整链接，创建完成后该链接失效。已有账号直接登录。

安装器在已启用的UFW中放行后台TCP端口；云安全组需允许该端口。账号、频道及通知全部在网页设置；关闭SSH不影响服务。HTTP不加密密码和Token；可自行配置HTTPS反向代理。忘记密码可 `sudo tgmemberguard web-init` 后重启服务。

## 卸载

```bash
sudo tgmemberguard uninstall          # 保留配置和名单，重装可恢复
sudo tgmemberguard uninstall --purge  # 输入 DELETE 后彻底删除配置和数据
```

## 功能

- 网页添加多个目标频道，支持公共或独立来源，无需修改代码。
- 非会员移出并永久封禁；本地名单、任务和审核结果持久化。
- 已核验会员保留历史白名单，更换来源ID不丢失资格；来源异常时未知用户待重试，不因查询错误踢人。
- 支持多个管理员聊天ID，机器人 `/start`、`/id` 可获取本人的聊天ID。
- 入群、审核结果和名单变更通知；每日00:00汇总前一天的入群、成功踢人、新增黑名单及ID明细，默认Asia/Shanghai。
- 通知失败退避重试，日报按日期去重生成，可补发；网页日志、备份下载、名单TXT导入导出。
- systemd开机启动和异常重启；一个进程统一轮询，避免多个频道抢Token。

## 边界

只审核新加入者，不扫描全体历史成员，不替代发卡平台付款核验。历史白名单为长期资格，之后退出来源频道也保留资格。不同目标的名单相互独立；黑名单优先。被第三方主动解封后再次加入，仍可能短暂接触目标内容，再由审核机器人移出。

统计从部署新版后开始；日报基于已接收到并确认记录的事件，不能恢复旧版未记录的历史人数。网络响应丢失时通知重试极少数可能重复送达。完整说明：[部署、升级、迁移、通知与日报](docs/DEPLOYMENT.md)。

## 本地测试

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest test_guard test_server test_activity_web -v
for script in deploy.sh install.sh uninstall.sh tgmemberguard; do bash -n "$script"; done
```

测试使用虚构账号和Telegram接口替身，不连接真实机器人。Linux文件锁及Ubuntu平台测试由CI验证；真实Telegram入群、踢人、通知送达仍需部署后验收。
