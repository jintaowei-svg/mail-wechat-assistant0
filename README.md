# AI 重要邮件微信助手

Python 3.11+，仅用标准库。**本项目是完整的个人部署起点，不是已连接到你的邮箱的在线服务。**

- 定时只读检查 **QQ 邮箱、网易 163、西安交通大学学生邮箱（Coremail）**，每个邮箱独立认证和增量 UID 游标。
- QQ / 163 使用各自的邮箱**授权码**；西交学生邮箱使用**客户端专用密码**（不是普通网页密码、NetID 密码）。
- 网易 163 兼容 RFC 2971 IMAP `ID` 握手，减少 `Unsafe Login` 问题。
- 个人微信通过 **Server酱 Turbo** 推送；紧急邮件合并即时推送，其他重要邮件每天汇总一次。
- 预设学校专属关键词：选课、考试、成绩、奖学金、学籍、毕业答辩、论文提交、教务通知等。
- 可选连接**你自己的 Ollama** 做中文 AI 摘要。不配置 AI 时使用本地规则；默认不把原邮件发给外部 AI。
- `SQLite` 本地保存摘要、待推送队列和已检查 UID；重复启动不会重复扫描已处理邮件。
- 仅使用 IMAP `BODY.PEEK[]` + `SELECT readonly`；**不自动回信、不修改已读状态、也不删除邮件**。

> 安全提示：Server酱收到你选择推送的邮件标题、发件人和简要摘要；第三方推送服务可以接触这些提醒内容。机密学校/研究邮件建议自行调整推送内容。首次启动会只扫描最近 24 小时内的邮件（最多每账号 150 封），用于避免历史邮件刷屏。

## 1. 三个邮箱的正确 IMAP 参数

| 邮箱 | IMAP 主机 | 端口 | 登录用户 | 登录密钥 |
|---|---|---:|---|---|
| QQ | `imap.qq.com` | 993/SSL | `你的QQ号@qq.com` | QQ 邮箱 IMAP 授权码 |
| 163 | `imap.163.com` | 993/SSL | `你@163.com` | 网易邮箱客户端授权码 |
| 西安交通大学学生 | `stu.xjtu.edu.cn` | 993/SSL | `你@stu.xjtu.edu.cn` | **学校邮箱客户端专用密码（16 位）** |

**西交官方校验来源（建议优先以学校官网最新通知为准）：**

1. 学生邮箱入口：<https://stu.xjtu.edu.cn/>
2. 学校网络信息中心常见问题：<https://nic.xjtu.edu.cn/info/1466/7409.htm>，明确学生邮箱收发服务器是 `stu.xjtu.edu.cn`，IMAP SSL 端口 `993`。
3. 网络信息中心 2024-06-25 公告：<https://oa.xjtu.edu.cn/zxgg_infonew.jsp?processInsId=%5BVZR%5BU>，说明学生邮箱从 2024 年 6 月底起强制启用双因素认证：网页端短信验证；第三方客户端须使用 **16 位客户端专用密码**。
4. 官方教学材料还出现 `imap.stu.xjtu.edu.cn` 作为收信地址：<https://agent.xjtu.edu.cn/__local/D/90/8F/1268C6A08721241C62B86B08C46_CA0B1049_3F6C0E.pdf>。本项目默认采用网信中心 FAQ 和 2024 公告使用的 `stu.xjtu.edu.cn`；如果学校提供了不同的服务器，以管理员当前通知为准。

**如何生成西交客户端专用密码：**

1. 用浏览器打开 <https://stu.xjtu.edu.cn/>，按学校当前网页版要求登录、完成额外验证。
2. 找到 **设置 → 安全设置 → 客户端安全登录 → 生成客户端专用密码**。
3. 将生成的专用密码复制到你自己的本机 `.env` 中的 `XJTU_MAIL_APP_PASSWORD`（若界面分组显示，填写时去掉组间空格）。**不要将密码发到聊天里，也不要用统一身份认证 NetID 密码。**
4. 完成后用 `--check-auth` 验证是否能以只读方式访问 INBOX。

QQ：在 <https://mail.qq.com/> 登录 → 设置/账号与安全 → IMAP/SMTP → 开启服务 → 获取授权码。163：在 <https://mail.163.com/> 登录 → 设置 → POP3/SMTP/IMAP → 开启 IMAP 服务 → 获取客户端授权码。具体菜单以邮箱当前网页为准。

## 2. 首次配置三邮箱（推荐）

在本机安装 Python 3.11+，解压 ZIP，终端进入项目目录：

```bash
cd mail_wechat_assistant_qq_163_xjtu
python setup.py
```

初始化程序将依次询问：

- QQ 邮箱地址 + QQ IMAP 授权码；
- 163 邮箱地址 + 163 授权码；
- 西交学生邮箱完整地址 + **学校客户端专用密码**；
- Server酱 Turbo SendKey（可先跳过，稍后填写）。

如只想先启用一个邮箱，其他地址留空；可随后编辑 `config.json` 追加/启用。密码输入采用 `getpass`，不会回显，保存在本机 `.env`，不进入 `config.json`。`setup.py` 默认拒绝覆盖现有配置。

### 已经配好了 QQ + 163？只追加西交邮箱

保持已有 `.env` 和 `config.json` 不变，把它们放到新项目目录，运行：

```bash
python setup.py --add-xjtu
```

该命令**只增加/更新学校邮箱**，不会清空你的 QQ/163 凭据、已有通知设置或自定义重要关键词。不要用 `--force`，它会重建全部配置。

也可以自己复制 `config.example.json` 到 `config.json`，复制 `.env.example` 到 `.env`，编辑其中 `enabled`、`username` 和相关授权码；未启用的邮箱不读取。

## 3. 先验证，再开启自动检查

```bash
python main.py --demo          # 本地模拟 3 封邮件，不联网、不使用真实密码
python main.py --doctor        # 离线检查三个邮箱和微信配置是否完整
python main.py --check-auth    # 逐个测试 IMAP 登录+只读打开 INBOX，不下载邮件正文
python main.py --test-push     # 真实发一条微信测试通知，可能占用免费额度
python main.py --dry-run       # 读取并打印判断结果，不持久化/不推送
python main.py --once          # 正式检查一轮
python main.py --loop          # 按配置间隔持续监控，Ctrl+C 退出
```

第一次排查建议按 `--doctor → --check-auth → --test-push → --dry-run → --once` 的顺序运行。

注意：`--dry-run` 会在本地终端输出邮件主题，不建议把输出日志上传到任何公开网站。

## 4. 微信推送

到 <https://sct.ftqq.com/> 用微信扫码登录，按照网站流程完成微信通知通道配置，复制以 `SCT` 开头的 **Server酱 Turbo SendKey** 到 `.env`：

```dotenv
NOTIFIER=serverchan
SERVERCHAN_SENDKEY=SCT你的SendKey
TIMEZONE=Asia/Shanghai
POLL_SECONDS=300
DIGEST_TIME=19:00
DAILY_PUSH_LIMIT=5
```

通常每 **5 分钟**扫描一次：紧急邮件随扫描合并推送，普通重要邮件每天 **19:00** 后汇总。这个时间是你为提醒设置的时区 `TIMEZONE`，与服务器部署在哪个国家无关。

Server酱 Turbo 官方当前免费额度是每天 **5 条**：<https://sct.ftqq.com/docs/getting-started/faq/>。项目默认最多 4 条紧急提醒 + 1 条日报，并保留本地待推送队列。额度耗尽时某些紧急消息可能次日才能到达，**不应当作为必须实时送达的安全报警渠道**。Server酱网页中微信端的消息展示方式依通道/套餐而变，有时推送页只显示标题，详情需打开查看。

如果需要 **企业微信群** 而不是个人微信，支持 `NOTIFIER=wecom` + `WECOM_WEBHOOK=...`，详见 `mailcore.py`。不要把 Webhook 发给别人。

## 5. AI 和校园邮件分级规则

`config.json` 的西交邮箱条目自带 `rules`，会与全局 `rules` **合并**，因此：

- 学校邮箱：`选课截止`、`考试时间变更`、`答辩截止` 等视作紧急；`奖学金`、`学籍`、`选课`、`教务处`、`论文送审` 等视作重要；
- QQ/163：不受校内关键词影响，仍按常规合同、账单、截止日期和 VIP 等分类；
- 营销优惠/退订等尽量过滤；规则不是百分百准确，需用 `--dry-run` 调整。

如果用本机 Ollama（可选），在 `.env` 中加：

```dotenv
AI_BASE_URL=http://localhost:11434/v1
AI_MODEL=qwen2.5:7b
AI_API_KEY=
```

需要先安装 Ollama 并拉取该模型。**开启非本地 AI API 后，邮件主题、发件人及部分正文可能离开你自己的机器**，学校科研、个人隐私内容不建议未经确认就转发到第三方模型。

AI 请求失败、响应正文不是文本、模型返回的 JSON 不是对象，或摘要/建议/截止时间字段不是字符串时，会回退到本地规则分类，继续处理后续邮件；不会把 `null` 或数组直接当成摘要。缺失或空白的文本字段沿用规则结果，规则判定为紧急的邮件不会被 AI 降级。失败日志只记录异常类型，不打印原始响应、邮件正文或 API 密钥。

## 6. Docker 自动部署（NAS / VPS / 常开主机）

先在宿主机完成 `python setup.py` 或 `python setup.py --add-xjtu` 并确认 `.env`、`config.json` 已生成，然后：

```bash
docker compose up -d --build

docker compose ps
docker compose logs --tail=80 -f

# 检查心跳：三个邮箱是否成功扫描
docker compose exec mail-assistant python main.py --status

# 停止（SQLite 卷保留）
docker compose down
```

`docker-compose.yml` 包含：

- `restart: unless-stopped` 自动随 Docker/主机重启恢复；
- Python 进程内定时轮询（默认 300 秒），不用另装 cron；
- IMAP 强制 SSL 且验证服务器证书，不跳过 TLS；
- 镜像中非 root 用户、只读根文件系统、禁用额外权限；
- SQLite 数据存放在 Docker 命名卷 `mail_state`，容器升级时保留；
- 健康检查、认证失败退避（默认至少 1 小时），避免连续错误密码被邮箱封禁。

注意：`docker compose down -v` **会删除状态数据卷**，除非你确定要重新扫描/清空通知队列，否则不要加 `-v`。若你把原项目升级到新目录且希望避免重复扫描，要先备份并迁移原 SQLite；不同项目文件夹默认创建不同的命名卷，**仅复制 `.env` / `config.json` 不会迁移历史游标**。

## 7. 常见连接错误

| 现象 | 排查方式 |
|---|---|
| QQ `LOGIN failed` | IMAP 是否开启；是否使用授权码；账户地址是否完整 |
| 163 `Unsafe Login` | 确认授权码；此项目自动发出 IMAP `ID` 握手；检查网易账户安全登录控制 |
| 西交认证失败 | **客户端专用密码**是否已经生成；不要填网页登录密码/NetID；学校要求双因素认证 |
| 西交连接超时 | 在部署主机检查 `stu.xjtu.edu.cn:993` 是否可达；确认校园网/网关/防火墙策略；不要关闭 TLS 校验 |
| 推送成功但微信没看到 | 检查 Server酱通知通道、微信服务号、免费额度以及网页推送日志 |
| 邮件分级不正确 | 编辑 `config.json` 中全局或学校邮箱条目的 `rules`，再通过 `--dry-run` 检查 |

若学校当前安全策略已经更新或客户端被禁用，请以西安交大网络信息中心最新说明为准：<https://nic.xjtu.edu.cn/>。

## 8. 隐私、限制和测试

- `config.json` 和 `.env` 已加到 `.gitignore`；**不能把真实授权码、SendKey、邮件正文发到聊天、提交 GitHub 或截图分享**。
- 邮件只读扫描，但本地 SQLite 会保存重要邮件的标题、简短摘要、发件人和待办建议。持久化目录应只允许本机管理员/容器用户读取。Docker/宿主机管理员可查看传入容器的环境变量，生产环境可改用专门的密钥管理系统。
- 暂时不含附件分析、不支持 OCR、没有 AI 自动发信。它不是严格的“一次且仅一次”消息投递系统：极端情况下可能重复提醒。
- 本项目做了单元测试和本地模拟，**没有连接你的真实邮箱、没有使用真实 SendKey，也没有做网络端到端登录/推送测试**。

运行测试：

```bash
python -m unittest discover -s tests -v
python -m py_compile main.py providers.py mailcore.py setup.py
```

新增的 AI 回退测试使用模拟响应，覆盖异常格式、网络错误、紧急优先级和日志隐私；邮箱扫描回归验证异常 AI 响应不会阻塞后续邮件。GitHub Actions 在 Ubuntu / Windows、Python 3.11 / 3.13 上运行离线测试、语法检查和本地演示，不需要邮箱密码、SendKey 或 AI 服务。`.env` 的 `0600` 权限断言仅在 POSIX 系统运行，Windows 文件权限不使用该位掩码判断。

### 可参考的开源项目

- <https://github.com/elie222/inbox-zero> — AI 邮件规则/智能分类；
- <https://github.com/emailops/emailops> — 多账户收件箱、本地 AI；
- <https://github.com/mjs/imapclient> — 更成熟的 IMAP Python 客户端；
- <https://github.com/activepieces/activepieces> — 可视化自动化工作流；
- <https://github.com/n8n-io/n8n> — 可用于低代码 IMAP 触发的工作流系统，注意许可证要求。

本项目是针对该需求独立编写的起步代码，不是上述仓库的代码搬运；引用/重用第三方代码前请检查许可证。
