# TGMemberGuard 2.1.0 · Ubuntu 网页管理、管理员通知与零点日报

## 新增：网页后台和通知

后台支持账号密码登录、频道添加/移除、来源配置、试运行/正式审核、暂停/恢复审核、黑白名单、日志、备份下载、管理员通知和日报设置。网页中保存的频道及通知配置，在当前轮询完成后自动应用，不需要手动重启。暂停仅停止审核处置，仍接收入群事件、保留待审任务，网页与通知继续工作。

### 从2.0升级

上传新版部署包并解压，在包目录执行：

```bash
sudo bash install.sh
sudo tgmemberguard web-init
sudo systemctl start tgmemberguard
```

安装器保留原配置、名单和队列。web-init交互创建后台账号（默认用户名可选admin，密码至少12位），没有预设密码。忘记密码时再次运行web-init并重启服务。密码保存为带随机盐的PBKDF2哈希；网页登录有CSRF防护、登录失败限速、会话失效和安全响应头。

首次部署需要先完成下方 init 创建Bot配置，再执行web-init。同一个Token仍只能保留一台服务器/一个审核进程。

### 打开网页

默认仅监听服务器的 `127.0.0.1:8787`。从自己的电脑通过SSH隧道访问，无需域名，也不需要开放后台端口：

```bash
ssh -N -L 8787:127.0.0.1:8787 用户名@服务器IP
```

保持该SSH窗口打开，在电脑浏览器访问 **http://127.0.0.1:8787**，用web-init创建的账号登录。关闭隧道只影响浏览器访问，服务器上的审核和日报继续运行。

需要通过域名直接访问时，使用Nginx等HTTPS反向代理到127.0.0.1:8787，参考包内nginx.example.conf。配置好HTTPS后，在运行配置的web对象中设置 behind_proxy=true、secure_cookie=true，再完整重启服务。只能在后端端口仅由可信反向代理访问时启用behind_proxy。不要将明文HTTP密码后台直接暴露公网。

如确需调整监听地址/端口，可用 `sudo tgmemberguard web-bind 127.0.0.1 --port 8787` 后执行 `sudo systemctl restart tgmemberguard`。网页监听和Cookie选项需要完整重启；普通频道及通知配置可自动应用。

### 管理员聊天ID与即时通知

1. 启动服务后，先私聊你的机器人发送 `/start` 或 `/id`，机器人会回复你本人的聊天ID。私聊指令仅回复自己的ID，不提供频道名单或管理权限。
2. 在网页“通知与日报”填写管理员聊天ID，支持多个个人或群聊ID。发往群聊时，先把机器人加入该群并确保可发消息。
3. 勾选“入群待审核”“审核结果”“名单变更”中的所需类型，保存配置，再点击“发送测试通知”。
4. 查看通知状态：pending为待发/重试，sent为Telegram接口已确认发送，cancelled为收件人已从配置移除。管理员未先start、封禁机器人或群权限不足时会显示失败原因并持续退避重试。

自动核验、永久封禁以及网页/CLI手动调整名单都会记录相应变更通知。历史数据迁移不追溯生成旧版的全部通知。相同入群或审核事件重放不会重复建立通知任务。私聊ID回复每个聊天30秒内最多生成一条任务，避免重复点击刷屏。

### 每天00:00日报

默认时区为 **Asia/Shanghai（北京时间）**。跨过本地00:00后，在下一个审核循环汇总**刚结束的前一天**，例如11月1日00:00发送10月31日的报告。没有会员入群的日子也生成零值报告。网络请求、限流或停机可能使实际发送延后，恢复后会补生成/补发；日报在通知队列中优先发送。

报告包含：

- 每个频道及合计的入群人次、去重人数；不统计机器人和管理员直接加入。
- 实际成功记录的踢人次数；试运行不计入实际踢人。
- 新增黑名单次数、用户ID明细和当前黑名单数量。

首次配置管理员并启用日报时，从当天开始安排自动日报；可在网页选择指定日期补发已有统计。报告任务按“时区+日期”唯一保存，重启不会重复生成同一天的自动报告。切换时区后，从新时区的当天开始安排，历史报告保留。

统计从2.1部署后接收的事件开始，无法重建旧版未记录的历史人数。迟到的Telegram更新可能晚于当天报告生成，可恢复后手动补发该日完整报告。Telegram接口不提供发送幂等键：极少数“服务端已发送成功但响应丢失/本地确认前进程中断”的情况，重试可能导致重复送达；发送状态不能证明管理员本人已读。

### 数据与操作记录

活动台账、通知队列、日报及网页操作任务存放在 `/var/lib/tgmemberguard/activity.sqlite3`。执行中的手动操作如遇进程中断，会标为uncertain并要求核对，不盲目重放可能已完成的解封操作。备份包含名单、队列和统计台账，不包含Bot Token或后台密码；恢复/迁移后可重新web-init创建账号。

用于 Ubuntu 22.04 / 24.04 有 systemd 的服务器，Python 3.10+。不需要桌面、EXE、域名、HTTPS证书或公网入站端口。需要能持续访问 Telegram API；安装时需要 apt 和 PyPI 下载依赖。通过原发卡流程加入目标频道后，本程序负责核验身份、记录名单和移出非会员，不验证订单、不替代发卡机器人。

## 首次部署

先在 Windows 上停止原审核服务。同一个 Bot Token 不能同时由 Windows 和 Ubuntu 长轮询运行。

上传 `TGMemberGuard-v2.1.0-Ubuntu.tar.gz` 到服务器，执行：

```bash
tar -xzf TGMemberGuard-v2.1.0-Ubuntu.tar.gz
cd TGMemberGuard-v2.1.0-Ubuntu
sudo bash install.sh
sudo tgmemberguard init
sudo tgmemberguard check
sudo tgmemberguard web-init
sudo systemctl start tgmemberguard
sudo systemctl status tgmemberguard --no-pager
sudo journalctl -u tgmemberguard -f
```

`init` 会询问 Token（输入不显示）、来源频道1 ID、多个目标频道ID。可一次填写频道2、3、4、5，空格或逗号分隔。只在服务器本机输入 Token，不要发到聊天。初始化默认试运行；先用会员和非会员测试日志，确认判断无误后切正式模式：

```bash
sudo tgmemberguard mode live
sudo systemctl restart tgmemberguard
```

机器人在每个来源及目标频道中均需为管理员，目标频道还需要封禁/移除订阅者权限。普通群需升级为超级群；频道可直接使用。安装器已启用开机启动；异常退出由 systemd 重启，网络错误由程序重试。关闭SSH不影响运行。安装服务和出现 `active` 不代表已通过真实Telegram审核，仍须测试账号加入确认。

## 后续添加频道：不修改脚本

例如新增频道3，先把机器人加入该频道并授予管理员、封禁权限，再执行（把示例ID替换为实际数字ID）：

```bash
sudo tgmemberguard target-add -1003333333333 --name '会员频道3'
sudo tgmemberguard check
sudo systemctl restart tgmemberguard
```

继续添加频道4、5，用同一命令换ID即可。已有频道配置及名单保留。配置保存后须重启生效；新增目标只接收后续入群事件，不补扫新增之前的全体成员。

默认所有目标共用来源频道1。需要独立规则时也支持：

```bash
sudo tgmemberguard target-add -1005555555555 --source -1006666666666 --name '独立项目'
sudo systemctl restart tgmemberguard
```

停止审核某个目标（不会踢人或清除名单）：

```bash
sudo tgmemberguard target-remove -1005555555555
sudo systemctl restart tgmemberguard
```

至少保留一个目标；停止全部审核请 `sudo systemctl stop tgmemberguard`。

## 配置文件

首次配置位于 `/etc/tgmemberguard/config.json`，第一次启动后复制为 `/var/lib/tgmemberguard/runtime-config.json`，之后网页和CLI统一管理该运行配置。手动编辑时请修改runtime-config.json；原/etc/config.json仅作为首次启动引导。Token 独立存放在 `/etc/tgmemberguard/bot.token`，不向网页回显，也不通过命令行参数传递。修改Bot Token后应完整重启服务。

```json
{
  "source_id": -1001111111111,
  "targets": [
    {"id": -1002222222222, "name": "会员频道2"},
    {"id": -1003333333333, "name": "会员频道3"},
    {"id": -1004444444444, "name": "会员频道4"}
  ],
  "dry_run": false,
  "token_file": "/etc/tgmemberguard/bot.token",
  "proxy": ""
}
```

如需代理，将 proxy 设置为服务器能访问的 `http://127.0.0.1:7890` 或 `socks5h://127.0.0.1:1080`，重启生效。不要把Windows机器上的127.0.0.1地址当作服务器代理。

## 名单和历史资格

规则延续Windows 1.2：管理员/机器人跳过 → 当前目标的黑名单 → 当前目标的历史白名单 → 当前来源实时核验。非会员永久封禁并保存本地黑名单。第三方主动解封、再次放入时，收到入群事件后再次封禁（仍可能短暂接触频道内容）。成功核验的账号写入本地白名单，换来源ID或重启不清除；以后主动退出来源也保留历史资格，撤销需要手工移除白名单。

**每个目标的黑白名单相互独立**，频道2的名单不会自动给频道3资格。需要把可信历史名单用于新频道时，请导出后有选择地导入对应目标。发卡平台仍负责付款资格。

来源失效、无法访问、管理员权限丢失、网络异常时，未知账号等待核验，不新增封禁；历史白名单继续保留，已确认的黑名单仍执行。只审核新加入的人，不扫描或批量清理已有成员。旧版从未保存的会员ID不能自动恢复，需要提前补录。

名单修改、迁移和备份均须先停止服务，完成后再启动：

```bash
sudo systemctl stop tgmemberguard
sudo tgmemberguard lists --target -1002222222222
sudo tgmemberguard whitelist-export --target -1002222222222 --file /var/lib/tgmemberguard/members.txt
sudo tgmemberguard whitelist-import --target -1003333333333 --file /var/lib/tgmemberguard/members.txt
sudo tgmemberguard whitelist-remove --target -1002222222222 --user 123456789
sudo tgmemberguard blacklist-remove --target -1002222222222 --user 987654321
sudo systemctl start tgmemberguard
```

TXT支持空格、换行、逗号分隔的正整数用户ID。导入黑名单ID会跳过，不自动解封。解除黑名单需要访问Telegram，成功解封后才删除本地记录；仅在Telegram手动解封不会清除本地黑名单。白名单删除不立即踢现有成员，下次加入重新核验。

上述管理命令以专用低权限服务用户操作数据。如从SSH上传名单到root目录，请先把文件放到服务用户可读的位置：

```bash
sudo install -o tgmemberguard -g tgmemberguard -m 600 /tmp/members.txt /var/lib/tgmemberguard/members-import.txt
```

## 从 Windows 迁移

1. 停止Windows审核服务，在1.2界面“白名单 / 备份”中备份数据库。不要直接复制正在使用且带WAL的数据库主文件。
2. 上传备份到Ubuntu；Ubuntu重新运行init输入原Token和ID。Windows的DPAPI加密配置不能直接用于Ubuntu。
3. 在Ubuntu停止服务，把数据库放到服务用户可读目录，然后导入对应目标：

```bash
sudo systemctl stop tgmemberguard
sudo install -o tgmemberguard -g tgmemberguard -m 600 /tmp/windows-backup.sqlite3 /var/lib/tgmemberguard/windows-backup.sqlite3
sudo tgmemberguard migrate-windows --target -1002222222222 --file /var/lib/tgmemberguard/windows-backup.sqlite3
sudo tgmemberguard check
sudo systemctl start tgmemberguard
```

会合并当前目标的历史白名单、黑名单，黑名单优先；不会导入Windows的旧任务或轮询进度。该命令也可合并本Linux版备份中某个目标的名单。相同Token不能在两个服务器上并行运行。

## 来源频道1更换

```bash
sudo systemctl stop tgmemberguard
sudo tgmemberguard source-set -1007777777777
sudo tgmemberguard check
sudo systemctl start tgmemberguard
```

`source-set` 修改公共默认来源；配置中显式指定 source_id 的目标保持独立来源。历史白名单不清除，待核验任务也保留并用新来源再核验。来源已失效时，服务依然能运行目标端名单保护，check会提示来源异常，未知用户待重试。

## 数据、备份和运维

- 程序：`/opt/tgmemberguard`；配置：`/etc/tgmemberguard`。
- 名单和队列：`/var/lib/tgmemberguard/channels/目标ID.sqlite3`。
- 唯一轮询进度：`/var/lib/tgmemberguard/poll.json`；心跳：`status.json`。
- 本地轮转日志：`/var/lib/tgmemberguard/guard.log`，同时输出至journal。

```bash
sudo tgmemberguard status
sudo journalctl -u tgmemberguard -n 100 --no-pager
sudo systemctl restart tgmemberguard
sudo systemctl stop tgmemberguard
sudo systemctl disable tgmemberguard
```

备份所有目标的数据，输出目录必须是新的目录：

```bash
sudo systemctl stop tgmemberguard
sudo tgmemberguard backup --output /var/lib/tgmemberguard/backups/manual-001
sudo systemctl start tgmemberguard
```

备份包含各目标的名单和队列、轮询进度、无Token配置，不包含bot.token。请另行安全保存Token；不要把名单/数据库公开上传。迁移至另一台服务器时可先init配置，再用migrate-windows逐目标合并备份中的名单。不要在已有运行数据库上直接覆盖SQLite/WAL文件。

升级：在新部署包目录再次运行 `sudo bash install.sh`，安装器会先停止旧服务，保留配置和数据；完成后check并重新启动。移除旧频道的配置不会删除对应数据库，重新添加该ID会继续使用原名单。

## 验证与限制

一个进程统一长轮询；每个目标独立队列；所有队列提交后再推进全局进度。中途崩溃会重放事件并去重。更换Token重置轮询队列但保留名单。某个目标缺少权限会保留其任务并重试，其余目标继续处理；HTTP请求依次执行，慢请求可能暂时增加其他频道延迟。退出系统d服务时最多允许180秒等待当前请求结束。

Telegram未消费的更新保留时间有限（约24小时），长期停机可能漏事件，恢复连接不等于能扫描全部历史成员。没有权限或源不可用时，程序优先避免误踢，可能暂时保留未核验者；请通过status和journal检查积压。

开发机为Windows，自动化测试和Shell语法检查结果见 `verification.json`。真实Ubuntu安装、systemd重启及真实Telegram多频道审核，需在你的服务器上完成验收。本包不会自动连接或修改你尚未提供的服务器。

部署后可在包目录运行：`/opt/tgmemberguard/.venv/bin/python -m unittest test_guard test_server test_activity_web -v`。Linux上会额外执行文件锁测试。

参考：[systemd服务和Restart配置](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)、[Telegram Bot API](https://core.telegram.org/bots/api)。
