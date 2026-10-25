# QQ Codex Bot

Python 后台服务：QQ 官方 WebSocket → SQLite 持久化队列 → 本机 Codex CLI → QQ 回复。
默认由本机主动连接 QQ，无需公网域名、端口映射或 cpolar；也保留可选 Webhook 模式。
支持 QQ 单聊和群聊；同一私聊用户复用会话，每个群共享一个会话，不同群互不串上下文。

## 环境与安装

需要 Python 3.11+、已安装并登录的 Codex CLI，以及 QQ 机器人的 AppID / AppSecret。

```bash
conda env create -f environment.yml
conda activate qq-codex
codex login
```

也可以使用虚拟环境：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

本机已创建 `.venv`，基于现有 Conda Python 3.12，启用了 `--system-site-packages`，
以便离线复用本机依赖。无需重复安装即可执行下面的启动和测试命令。
源码在 `src/qq_codex_bot/`，测试在 `tests/`，运行数据在 `data/`，日志在 `logs/`。

## 配置

在 `.zshrc` 中导出这两个变量，然后打开新终端或运行 `source ~/.zshrc`：

```bash
export QQ_BOT_APP_ID='你的 AppID'
export QQ_BOT_APP_SECRET='你的 AppSecret'
```

如果 `.zshrc` 已赋值但没有写 `export`，只需再运行：

```bash
export QQ_BOT_APP_ID QQ_BOT_APP_SECRET
```

可选变量见 [.env.example](.env.example)。程序只读取环境变量，不自动读取 `.env`。
默认 `127.0.0.1:8080`，Codex 工作目录为 `data/workspace/`，权限模式为 `approve-for-me`：
使用 `workspace-write` 沙箱，授权请求交由 Codex 自动审核。
新建和续接会话均应用该模式。可通过 `QQ_BOT_CODEX_SANDBOX=read-only` 改为只读模式。
需要指定工作项目时，设置 `QQ_BOT_CODEX_WORKSPACE`；默认权限已允许修改工作目录中的文件。
QQ 凭证不会传入 Codex 子进程。群聊成员共享上下文，也都可使用会话指令。
可通过 `QQ_BOT_ALLOWED_USERS` / `QQ_BOT_ALLOWED_GROUPS` 设置逗号分隔的 OpenID 白名单；
前者在群聊时匹配 `member_openid`，私聊时匹配 `user_openid`，后者匹配 `group_openid`。

## 本机后台启动

默认使用 WebSocket。迁移旧服务时先停止原实例，再启动：

```bash
bash scripts/bot.sh stop
bash scripts/bot.sh start --transport websocket
```

脚本会停止本项目管理的旧 cpolar 隧道，等待 QQ 返回 `READY` 才报告连接成功。
若后台有接入方式选项，选择 WebSocket。是否可用以实际鉴权和消息接收为准。
连接失败时服务保留运行并自动重连，启动命令返回非零，错误详情在日志中。
已有 Codex 会话和数据库直接复用。若未安装新增依赖，请运行 `.venv/bin/python -m pip install -e .`。

```bash
bash scripts/bot.sh start
bash scripts/bot.sh status
curl http://127.0.0.1:8080/health
bash scripts/bot.sh logs
bash scripts/bot.sh stop
```

启动时可指定模型（将 `你的模型名` 替换为账号可用的模型 ID）：

```bash
bash scripts/bot.sh start --model 你的模型名
```

也支持 `--model=你的模型名`。优先级为启动参数 > `QQ_BOT_CODEX_MODEL` 环境变量 >
本机 Codex 默认模型。已运行的服务需要先停止再启动，参数才会生效。
指定模型同时适用于新建和续接会话；重启服务本身不会触发对话。

仅测试 QQ 接入可运行 `bash scripts/bot.sh probe`：它只建立 WSS、鉴权并等待 READY，
随后关闭连接，不启动 Codex、不回复 QQ 消息，也不开放本地监听端口。
运行中的服务可通过 `status` 或 `/health` 查看 `qq.ready`，本地健康与 QQ 已连接是两个状态。

首次联调先在 QQ 私聊发送 `/help`，确认机器人能回复指令列表；再发送普通文字，验证 Codex 回复。
群聊测试需要 @机器人。测试环境还需在 QQ 后台正确配置测试成员/测试群。

启动脚本默认使用 `.venv/bin/python`。使用 Conda 环境时先设置：

```bash
export QQ_BOT_PYTHON="$(command -v python)"
```

后台模式使用 `nohup`，终端关闭后继续运行；不提供开机自启。避免让电脑休眠。
每次启动会将旧日志归档到 `logs/bot.<时间>.<进程号>.log`，当前日志为 `logs/bot.log`。
`bash scripts/bot.sh logs` 只显示最近一次启动的记录并持续跟踪，避免混入历史启动错误。
同一数据目录只允许一个服务进程，不能开启多个 uvicorn workers。
前台调试可运行 `.venv/bin/python -m qq_codex_bot`。

## 可选 Webhook 模式

只有显式选择 Webhook 时才需要下面的公网回调配置：

```bash
bash scripts/bot.sh start --transport webhook
```

Webhook 模式启动脚本会同时启动本地服务和 cpolar，验证公网地址确实转发到
当前服务后，打印完整的 `https://.../qq/webhook` 地址。隧道失败时返回非零状态并提示
查看 `logs/cpolar.log`；本地服务保留运行，不将其误报为 QQ 已连接。
`stop` 同时停止本项目管理的 bot 和 cpolar，不操作其他 cpolar 服务。
重复执行 `start` 会复用本项目已运行的 cpolar，避免不必要地重建随机域名。
已有外部隧道或反向代理时，使用 `bash scripts/bot.sh start --transport webhook --no-tunnel`。

**推荐使用已预留的固定二级子域名**，避免随机域名变化导致 QQ 回调失效：

```bash
export QQ_BOT_CPOLAR_SUBDOMAIN="你的预留名称"
export QQ_BOT_CPOLAR_REGION="预留时选择的区域"
bash scripts/bot.sh start --transport webhook --model 你的模型名
```

`SUBDOMAIN` 填预留名称，不带协议或域名后缀；`REGION` 必须与预留区域一致，
例如 `cn_vip`。它不会自动申请或购买域名，需先在 cpolar 账户预留。
也可以把这些值写进项目根目录的 `cpolar.env`（本地文件，已加入 `.gitignore`），
隧道脚本会自动读取，无需重复 export；显式导出的环境变量优先。
若使用自己的域名，则设置 `QQ_BOT_CPOLAR_HOSTNAME=bot.example.com`，并按 cpolar 要求
完成自定义域名预留和 DNS；不能同时设置 `SUBDOMAIN` 和 `HOSTNAME`。

未指定固定域名时使用随机 HTTPS 地址。脚本在启动检查时将地址保存至 `data/callback_url`，
若与上次不同，明确提示更新 QQ 后台。脚本不会自动修改 QQ 后台的回调配置。
若域名在运行期间变化，也需要重新检查并更新配置；当前未实现持续的域名变化监控。

若尚未登录 cpolar，先按你的 cpolar 账户配置 authtoken。
需要单独调试隧道时可运行 `bash scripts/tunnel.sh`，不要与已管理的隧道重复启动。

在 QQ 开放平台的机器人开发设置中选择 Webhook，填写此地址，订阅：

- `C2C_MESSAGE_CREATE`：QQ 私聊消息。
- `GROUP_AT_MESSAGE_CREATE`：群内 @机器人消息。
- `GROUP_MESSAGE_CREATE`：开启“接收所有消息”后，可选订阅群内全量消息。

程序自动处理 `op=13` 地址验证；普通事件使用原始请求体和 Ed25519 签名验真。
只有订阅 @消息时，群内普通消息不会送到程序。不要给回调地址加 Basic Auth。
QQ API 访问权限、沙箱成员/测试群、出口 IP 白名单等按平台后台要求配置。
如果日志出现 QQ API 错误码，请对照官方文档检查；程序不会记录密钥或完整消息正文。
云服务器反向代理配置示例在 `docs/nginx.conf.example`。

## 会话指令

| 消息 | 行为 |
| --- | --- |
| 普通文字或图片 | 首次新建 Codex 会话，之后用该会话的 thread ID 续接 |
| `/new` | 新建并切换至全新 Codex 会话，旧记录保留 |
| `/new 你的问题` | 在新会话中直接处理问题 |
| `/reset` | 清除当前会话映射，下一条文字建立新会话 |
| `/session` | 查看当前本地会话 ID 和 Codex thread ID |
| `/status` | 查看本聊天最早的排队或处理中任务 |
| `/result` | 领取本聊天最近一次已保存的 Codex 任务结果 |
| `/all_message` | 切换本群的全量消息接收开关 |
| `/all_message on` / `/all_message off` | 明确开启或关闭本群的全量消息接收 |
| `/all_message status` | 查看本群的全量消息接收状态 |
| `/help` | 显示指令 |

`/reset` 不删除 Codex 的历史文件，`/new` 不复制旧上下文。
两条指令都按消息到达顺序执行，等待前一轮完成，不会中途取消正在运行的任务。
这里的 session 是 Codex 对话，与 QQ 网关连接的 session 不同。
Codex 会话文件由本机 Codex 保存，SQLite 保存聊天映射和消息处理状态；重启后继续复用。
群聊发给 Codex 的消息带发送者 OpenID，方便区分不同成员。
`/status`、`/result`、`/session`、`/help` 和 `/all_message` 使用独立控制队列，长任务运行时也能响应。
`/result` 不会再次调用 Codex；最近结果可能属于上一条任务，以返回的任务编号为准。

全量消息开关按机器人和群分别保存，重启、`/new` 和 `/reset` 后仍保留；默认开启以延续原有行为。
关闭后，未 @ 当前机器人的群消息会在入队前被丢弃，不会调用 Codex，也不会处理其中的指令；
尚未开始执行的普通群消息也会被跳过，已经运行的任务不会被取消。私聊不受影响。
关闭状态下，如需恢复或查询，请 **@ 当前机器人**发送 `/all_message on` 或 `/all_message status`。
该开关只控制本地触发条件；开启全量接收仍需要 QQ 平台提供 `GROUP_MESSAGE_CREATE` 权限。

程序以 QQ 的 [`GROUP_AT_MESSAGE_CREATE` 事件](https://github.com/tencent-connect/bot-docs/blob/main/docs/develop/api-v2/server-inter/message/send-receive/event.md#群聊机器人)
识别 @ 当前机器人的消息，也支持在全量事件的正文和 `mentions` 中匹配机器人的身份。
WebSocket 会从 `READY.user` 获取机器人 ID；若平台只推送全量事件，且其中使用的 OpenID 不在
`READY.user` 中，或使用 Webhook 接收全量消息，请设置 `QQ_BOT_SELF_ID` 为 @ 当前机器人时
`<@OpenID>` 中的 OpenID。@ 其他成员或其他机器人不会触发关闭状态下的本机器人。

## 图片输入

群聊和私聊均支持直接上传图片，也支持文字与图片一起发送。纯图片消息会附带默认的看图提示，
有文字时保留用户的问题，并继续使用当前 Codex 会话；`/new` 也可同时附带图片。
图片遵守本群的 `/all_message` 开关及用户、群白名单，未获准的消息不会下载图片或调用 Codex。

QQ 事件中的图片会先保存到工作目录的 `.qq-inbox/` 下，再按
[Codex 官方图片输入方式](https://learn.chatgpt.com/docs/cli/reference)通过 `--image` 附加到本轮输入，
新会话和 `exec resume` 均支持。每条消息最多读取 3 张图片，单张不超过 20 MiB，
支持 PNG、JPEG、WebP，以及 GIF 首帧（转换为 PNG，不分析整段动画）。
附件缺少 MIME 或声明为通用文件时，也会根据图片文件名或 URL 路径识别；下载后仍检查文件签名。
下载失败、链接过期或格式不支持时会明确提示重新上传。
语音和其他文件暂不作为模型输入；引用消息中的图片只有在 QQ 同时提供图片附件时才能读取。

关闭群全量触发时，未点名的已授权群友附图会与最近文字一起缓存，下一次非命令唤醒时，
优先保留当前消息图片，再在三张总上限内附入最新缓存图片；过期记录不会附入。
缓存仍是进程内暂存，重启会清空；收到 `/status` 等命令不会消耗它。
同一发送者在同一聊天里明确叫昵称或 @ 本机器人后，可在 60 秒内补发一次图片，
这一次附图可独立唤醒；机器人发送者同样适用，其他机器人消息仍按原规则过滤。
只有 `<faceType=...>` 等表情占位而没有图片附件时，会向模型注明未取得图像，不根据标签猜图。
日志仅记录附件数、识别图片数和接收提示数，不记录图片链接或签名参数。

## 回复格式与附件

默认使用 QQ 的 Markdown 消息格式发送 Codex 最终回复。Markdown 的显示范围由 QQ 客户端和
机器人权限决定；若 QQ 明确拒绝 Markdown 权限，程序会提示并降为纯文本。
需要始终使用纯文本时，设置 `QQ_BOT_MESSAGE_FORMAT=plain` 并重启。

图片和文件会作为实际 QQ 附件发送，无需公开本机文件或启用 cpolar。
可以直接让 Codex“生成一张 PNG 图并发送给我”或“把结果保存成 CSV 并发给我”。
输出约定位于 Codex 工作目录的 `AGENTS.md`，默认是 `data/workspace/AGENTS.md`，
由 Codex 原生加载；bot 不再向每条用户消息重复拼接规则，新建和续接均使用此方式。
服务启动时，若配置的工作目录没有 `AGENTS.md`，会按随包提供的
[模板](src/qq_codex_bot/templates/AGENTS.md) 创建；已有文件保持原样，可手动将模板中的交付规则合并进去。
若目录中还有 `AGENTS.override.md`，Codex 优先读取它，需要将相应规则写进该覆盖文件。
Codex 在每次 CLI 启动时发现规则文件，具体加载方式见
[OpenAI 官方 AGENTS.md 文档](https://learn.chatgpt.com/docs/agent-configuration/agents-md)。

交付文件时，成品保存在配置的工作目录内，最终回复用本地 Markdown 链接引用，
例如 `![图表](<绝对路径/chart.png>)` 或 `[报告](<绝对路径/report.csv>)`。
代码块中的示例链接不会触发上传，工作目录外的文件不会自动发送。

每次最多发送 3 个附件，更多文件可让 Codex 打包为 ZIP。PNG/JPEG 按图片发送，其他格式按文件发送；
文件类型、大小和私聊/群聊发送权限仍受 QQ 平台限制，平台拒绝时会显示错误码。
单附件的程序大小上限默认 50 MiB，可通过 `QQ_BOT_MAX_ATTACHMENT_MB` 调整为 1–200 MiB。
本地小文件直接上传，超过 8 MiB 使用分片上传；都由本机主动请求 QQ，无需入站公网连接。

生成的附件会保存快照到 `data/artifacts/`（可用 `QQ_BOT_DATA_DIR` 改变数据目录）。
长任务超过原消息回复窗口后，使用 `/result` 领取保存的文字和附件；发送重试不会重新执行 Codex，
也不会读取被后续任务改写的原始文件。平台限制解决后，亦可用 `/result` 重试附件发送。

Codex 正常结束但没有最终文本、返回空字符串或全空白时，任务正常完成，机器人保持静默，
不会发送空消息、错误提示或完成占位语。会话照常保留；`/result` 领取该空结果时也不发送消息。

## 可靠性与边界

- 收到消息先写入 SQLite，再更新 WebSocket 恢复序号或确认 Webhook 回调。按消息 ID 去重；
  同群/同私聊串行，不同聊天默认并行 2 个。
- WebSocket 实现 QQ 协议心跳与 ACK 检测、指数退避重连、Resume 恢复，以及无效会话重新鉴权。
  接收事件与 Codex 执行解耦，长任务不会阻塞心跳。默认只在本地回环地址监听健康检查，
  WebSocket 模式禁用 `/qq/webhook` 路由，不使用固定域名和 cpolar。
- Codex 通过 `exec --json` 创建会话、`exec resume <thread_id>` 续接，不调用 shell 拼接用户输入。
- Codex 默认不限时（`QQ_BOT_CODEX_TIMEOUT=0`），不再受 240 秒上限限制。
  如需自行限制，可设为任意正整数秒数，例如 `QQ_BOT_CODEX_TIMEOUT=3600`。
  已导出旧值 `240` 时需改为 `0` 并重启服务，才会取消限制。
- 为兼容官方文档中不同的私聊时效描述，自动被动回复仍统一按约 5 分钟窗口保守处理。
  窗口过期不会终止或跳过已接收的 Codex 任务；结果保存到 SQLite，发送 `/result` 即可领取。
  程序不会在窗口过期后主动推送；服务停止或崩溃仍会中断正在运行的任务。
- QQ 网络/服务端失败会有限重试已保存的回复，不重复调用 Codex。
  发送结果不确定时沿用相同 `msg_id + msg_seq`，避免重复回复。
- 服务崩溃后不自动重放已开始的 Codex 任务，避免重复执行动作；保留已取得的 thread ID，
  尝试提示用户重新发送。尚未开始的任务恢复处理，超出回复窗口的结果也会保存。
- 输入支持文字及 QQ 直接提供的图片附件，语音和其他文件尚未传给 Codex。
  输出支持 Markdown、图片和文件。文字超过 4000 字符或 12000 UTF-8 字节会截断，
  原始结果保留在数据库与 Codex，可让 Codex 将长文本保存为文件发送。
- 本地数据库包含消息与回复，应和 Codex 数据一起妥善保管。不同会话共享所配置的工作目录。

## 开发与验证

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q src
bash -n scripts/bot.sh scripts/tunnel.sh
# 安装开发依赖后可执行：ruff check src tests
```

测试使用模拟 QQ HTTP 接口和模拟 Codex 可执行程序，不发送真实 QQ 消息，也不消耗模型调用。
覆盖官方公钥与地址验证签名向量、请求防篡改、会话复用/隔离、指令切换、
数据库重启恢复、消息去重、发送重试、进程超时回收和凭证过滤，
以及 WebSocket 鉴权、心跳、恢复序号、重连与消息到回复的模拟端到端流程。
另覆盖 Markdown 权限降级、图片/文件上传、分片上传、附件快照与发送进度恢复，
以及主动空回复、空结果领取和重启后的会话续接。
Python 使用四空格缩进、`snake_case` 命名；新增行为请补充有意义的回归测试。

## 参考文档

- [QQ 启动接入](https://bot.q.qq.com/wiki/develop/api-v2/)
- [QQ Webhook 事件与地址验证](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/event-emit.html)
- [QQ 签名算法](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/sign.html)
- [QQ API 调用指南](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/api-call-guide.html)
- [QQ 消息收发规则](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html)
- [腾讯 QQ Bot Agent SDK：消息与附件接口](https://github.com/tencent-connect/qqbot-agent-sdk)
- [OpenAI Codex 非交互模式与会话续接](https://learn.chatgpt.com/docs/non-interactive-mode)
