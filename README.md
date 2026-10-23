# Dayuan Lite

企业微信远端「大渊」的轻量 CLI / MCP 连接器。Python 3.9+，仅标准库，支持 macOS / Linux。

首次扫码授权、后续换票和续期均无需安装或运行 WorkBuddy 桌面客户端。账号授权仍使用 **WorkBuddy 官方云端服务**，查询直接访问企业微信 A2A。

本项目是独立的第三方连接器，与腾讯、企业微信、WorkBuddy 无隶属关系。所用接口不是已承诺稳定的公开 API；可用性取决于实际账号、企业授权与上游服务。

非官方项目，仅供学习与研究参考；请仅在获得授权的范围内使用，使用风险由使用者自行承担。代码采用 [MIT License](LICENSE)。

## 能做什么

- 浏览器企业微信扫码登录，连接器通过云端轮询取得授权。
- 临近到期时按需刷新账号凭据，再换取企微用户 token，无需常驻刷新进程。
- 按关键词查群、读取指定会话、在会话内查询关键词。
- 用同一份用户授权，自动换票后调用企微 CLI 的文档搜索/正文、会议、通讯录、日程、邮件正文、微盘元数据及普通/智能表格只读接口。
- 通过稳定 `request_id` 恢复任务，凭据续期后仍可读取原任务。
- 通过 stdio MCP 对接支持该协议的 Agent；也可以直接调用 CLI。

返回内容由远端大渊生成，可能是摘要。它不是原始聊天记录导出接口，不保证逐字原文、消息数量或完整性。

## 统一安装与 Agent 接入

推荐从审阅过的检出安装；先准备 Python 3.9+ 与 uv（或 pipx）。需要企微业务读取时再加 Node.js 18+ / npm：

```bash
git clone https://github.com/lighters/dayuan-lite.git
cd dayuan-lite
sh scripts/install.sh --with-wecom-cli
# 已有兼容官方 CLI，或只用群查询时：sh scripts/install.sh
# 同时安装到所用 Agent 的 skills 目录（显式指定，不自动修改客户端）：
# sh scripts/install.sh --skill-dir /absolute/path/to/client/skills
```

入口使用已有 uv/pipx 重装当前检出（同版本源码变化也生效）；`--with-wecom-cli` 明确安装已验版本 `@wecom/cli@1.3.4`。不自动安装包管理器，不启动登录，不改变既有 Bot 配置。已安装同名 Skill 时拒绝覆盖，需先比较本地修改。安装脚本和官方 CLI 的 npm 安装需要网络；Python 运行时仍仅标准库。

```bash
dayuan doctor                         # 本地能力、状态和恢复指引；不联网、不启动 CLI
dayuan login --open --wait 300
dayuan doctor --online                # 真实换票及 CLI 版本检查，不查询业务资源
dayuan setup                          # 输出当前解释器和状态目录对应的 MCP 配置
# 已安装后单独复制随包 Skill：
dayuan setup --skill-dir /absolute/path/to/client/skills
```

`setup` 输出的 `mcpServers` 条目可以直接复制到所用客户端；它不自动改写客户端配置。使用自定义状态目录时给 `setup` 加同一 `--state-dir`，生成绝对路径；保持安装环境存在。随 wheel/sdist 分发的 [dayuan-readonly Skill](skills/dayuan-readonly/SKILL.md) 提供业务路由、异步任务、追问和跨领域汇总流程；也可用客户端支持的技能安装器选择本仓库的该目录。

`doctor` 将已实现白名单、CLI 存在性、版本兼容性、授权状态和 `business_permissions_verified` 分开报告。`--online` 在 30 秒共享预算内按需刷新账号并强制交换企微令牌，版本探测最多 10 秒；底层 HTTP 仍是 I/O 超时。成功取票不代表任意资源可读，诊断始终保留 `business_permissions_verified:false`。它不会调用业务查询、发送消息或下载文件。诊断结果使用退出码 0 返回报告；读取其中 `auth_error`、`wecom_cli.error` 判断失败。

授权错误返回 `recovery.commands` / `instructions`，命令保留当前状态目录。首次授权用 `login`；待扫码流程用 `finish`；明确企微授权缺失用 `wecom-login`；`refresh_unknown` 不重放旧刷新令牌，按指引重新扫码。网络问题或锁等待不要求删凭据；账号/企业变化应核对身份并使用独立目录。此路径不延长 Bot 七天权限，不承诺永久免扫码。

## 其他安装方式

仅依赖 Python 3.9+ 标准库，三种方式任选：

```bash
# 安装成 dayuan 命令
pipx install git+https://github.com/lighters/dayuan-lite.git

# 不安装，每次直接运行
uvx --from git+https://github.com/lighters/dayuan-lite.git dayuan --help

# 克隆后直接运行脚本，下文的 dayuan 换成 python3 dayuan.py 即可
git clone https://github.com/lighters/dayuan-lite.git && cd dayuan-lite
python3 dayuan.py --help
```

所有功能都在一个入口里：`dayuan login|finish|renew|status|doctor|setup|wecom-login|read|reply|result|wecom|list|gc|serve`。旧的 `python3 workbuddy_auth.py login|finish|renew|status|wecom-login` 仍然可用，作为兼容层调用同一实现。

### 状态目录

授权和任务回执默认保存在 `~/.config/dayuan-lite`（如设置了绝对路径的 `XDG_CONFIG_HOME`，则为 `$XDG_CONFIG_HOME/dayuan-lite`）。优先级：`--state-dir` > 环境变量 `DAYUAN_STATE_DIR` > 默认目录。`--state-dir` 写在子命令前后都可以，`dayuan status` 会显示实际使用的目录。

**从 0.2.x 升级**：旧版默认目录是仓库内的 `.private`。如果它里面有授权、而新默认目录还没有，连接器会在标准错误和 `status` 的 `migration_hint` 里提示，但**不会自动移动凭据**。可以二选一：

```bash
# 继续使用旧目录
export DAYUAN_STATE_DIR=/path/to/dayuan-lite/.private

# 或者确认没有 dayuan / MCP 进程在运行后，自行移动
mkdir -p ~/.config && mv /path/to/dayuan-lite/.private ~/.config/dayuan-lite
```

## 快速开始

```bash
dayuan login --open --wait 300
```

用企业微信扫描官方网页二维码并确认。网页显示「打开 WorkBuddy」时可忽略，连接器不需要桌面回跳。无图形环境时去掉 `--open`，自行打开命令输出的授权链接。

轮询最多等待 5 分钟。如果扫码完成时轮询已经结束，可恢复取票：

```bash
dayuan finish --wait 60
dayuan status
```

服务器确认授权已过期后，再运行 `login`。`status` 只读取本地状态，不代表远端授权仍有效。每位使用者都需要完成自己的账号授权。

### 查询示例

替换示例群名、关键词和时间。`request_id` 在一次逻辑查询的重试中必须保持不变。

```bash
dayuan read --wait 60 --json '{"request_id":"find-project-001","operation":"search_groups","query":"项目讨论","limit":5}'

dayuan read --wait 60 --json '{"request_id":"read-project-001","operation":"read_chat","chat":"项目讨论群","since":"2026-10-15T00:00:00+08:00","until":"2026-10-15T12:00:00+08:00","limit":3}'

dayuan read --wait 60 --json '{"request_id":"search-project-001","operation":"search_messages","chat":"项目讨论群","query":"会议安排","limit":5}'

# 等待结束仍未完成时，用同一 ID 继续等待
dayuan result --wait 60 read-project-001
```

`--wait N`（0–300 秒，默认 0）让连接器提交后自动用同一 `request_id` 每约 3 秒查询一次，直到离开 `submitted` / `working` 或等待到时；它只轮询，不会重发。等待预算从首次提交/首次结果查询返回后开始；后续轮询的网络超时、授权请求和授权锁共享剩余预算，到时不再发起下一次轮询。首次请求耗时不计入 N；底层网络超时是 I/O 超时，不是整个进程的强制终止时间。到时仍未完成会返回当前状态和 `next_action`，之后用 `result --wait N` 继续。遇到 `outcome_unknown` 不要更换 ID 重发；`rejected`（`retry_safe: true`）表示请求未被远端接受，处理原因后可用同一 ID 重新提交。同一 ID 正在提交时，并发调用最多等待 10 秒；对方已拿到任务则直接返回该任务的当前状态（不会再提交一次），仍未完成提交才返回 `REQUEST_BUSY`，稍后用原 ID 重试。不同查询应使用不同 ID。时间范围必须成对且带时区，`limit` 为 1–50；这是摘要请求约束，不是原始消息数量保证。

## 大渊澄清追问

仅在原群查询返回 `input_required` 时，取得澄清答案后继续原任务：

```bash
dayuan reply --wait 60 --json '{"parent_request_id":"read-project-001","request_id":"reply-project-001","answer":"选择第一个候选群，时间范围沿用原查询"}'
dayuan result reply-project-001 --wait 60
```

`parent_request_id` 始终为最初群查询 ID；每轮回复用一个新的稳定 `request_id`，重试时保持该 ID 和答案不变。答案为 1–2000 字符的单行文本；只用于澄清原有只读查询，不开放其他业务操作。连接器先获取原任务最新状态，仅在 `input_required` 时携带原 `taskId` / `contextId` 提交；完成态、`auth_required` 和账号变化均拒绝。后续可按回复 ID 查结果；再次澄清仍引用最初父 ID。

父任务锁与回复锁串行化提交；回复只落盘哈希，不存答案原文。发出前保存回执和独立回复屏障，普通结果轮询不会覆盖屏障。同 ID 不同答案报冲突。确定未被接受的 `rejected` 可用相同 ID 重试；`outcome_unknown` / 崩溃中的 `dispatching` 不重放，并阻止换回复 ID。可以查询父任务，但父任务状态不能证明该回复已接受；需要人工核对后结束处理，不提供自动清除屏障。正常接受的回复不阻止之后的新一轮澄清。

这是客户端对只读澄清的约束，上游仍没有已证明的强制只读权限。真实多轮兼容性见 [验收记录](docs/VALIDATION.md)。

## 企微 CLI 用户授权只读入口

此功能另外需要本机官方 `wecom-cli >= 1.3.4`（已联机验证 1.3.4）。Python 部分仍只用标准库，A2A 消息查询不需要安装企微 CLI。

```bash
npm install -g @wecom/cli@1.3.4
wecom-cli --version
# 使用前面的 dayuan login 完成用户扫码；不需要 wecom-cli auth init

dayuan wecom doc-search --json '{"keywords":["项目","方案"],"limit":5}'
# 从上述 data.docs 获取普通文档的 docid，传给正文读取：
dayuan wecom doc-read --json '{"docid":"<搜索返回的docid>","content_type":"text"}'

dayuan wecom meeting-list --json '{"begin_time":"2026-10-12 00:00:00","end_time":"2026-10-15 23:59:59","limit":5}'
# 从上述列表获取会议标识：
dayuan wecom meeting-get --json '{"meeting_ids":[{"meeting_id":"<列表返回的meeting_id>"}]}'
```

每次调用先校验操作与参数、检查 CLI 版本，再调用云端授权模块检查缓存；临近到期时自动刷新账号/重新换取企微用户令牌。令牌只通过该次子进程的环境传递，不输出、不进入命令参数；连接器使用临时空配置目录，移除继承的 CLI 令牌、额外请求头及日志配置，不会回退到已有 Bot 凭据，也不会改动原有 Bot 配置。明确的令牌失效码 `853004` / `853005` 会触发一次重新换票和只读重试，其他失败直接返回机器码。

可用 `--wecom-cli /absolute/path/to/wecom-cli` 指定受信任的官方可执行文件；CLI 和 MCP 也支持本机环境变量 `DAYUAN_WECOM_CLI`。这是程序路径，不是凭据。不要将任意脚本或上游返回的路径配置为 CLI。`dayuan status` 的 `wecom_cli.found` 只检查该程序是否能找到，不启动它、不校验版本。

| 操作 | 参数与限制 |
| --- | --- |
| `doc-search` | `keywords` 非空数组，最多 10 个词、每词最多 200 字符；`search_scope` 可为 `title` / `content` / `title_content`；`doc_types` 支持 `doc` / `sheet` / `smartsheet`，默认 `["doc"]`；正文操作按文档类型选择 |
| `doc-read` | `docid` 必填；`content_type` 为 `text`（默认）或 `markdown` |
| `meeting-list` | `begin_time` / `end_time` 均必填，`YYYY-MM-DD HH:mm:ss`，按企微接口时间解释；区间最多 31 天 |
| `meeting-get` | `meeting_ids` 对象数组，1–10 个；对象含 `meeting_id`，周期单场可附 `sub_meeting_id` |
| `contact-search` | `keywords` 必填，1–10 个非空词，每词最多 200 字符；不支持 `limit` / `cursor` |
| `calendar-list` | `begin_time` / `end_time` 均必填，格式同会议，区间最多 31 天；不支持 `limit` / `cursor` |
| `calendar-get` | `schedule_ids` 非空字符串数组，最多 10 个 |
| `mail-search` | 成对时间窗必填、最多 31 天；可附 `keywords`（1–10 个词）、`sender`、`receiver`；支持分页 |
| `mail-get` | `mail_ids` 非空字符串数组，最多 10 个；读取正文及附件元数据，不下载附件、不标记已读 |
| `disk-list` | 最近查看的文件列表，支持分页 |
| `disk-search` | 至少提供 `keywords`、`file_types`、`search_type` 之一；`search_type` 为 `all` / `file` / `folder` / `space`；可附 `sort_by`（`best_match` / `modify_time` / `file_size`）、`sort_order`（`asc` / `desc`），支持分页 |
| `disk-get` | `file_id` 必填，仅文件元数据，不下载文件 |
| `sheet-get` | `docid` 必填，返回普通表格的子表元数据 |
| `sheet-read` | `docid`、`range` 必填；`sheet` 为子表 ID 或名称，单子表可省略；大写 A1 单格/矩形，如 `A1:C3`，最多 1000 格，不允许整表读取 |
| `smartsheet-sheets` | `docid` 必填，列出智能表格子表 |
| `smartsheet-fields` | `docid` 必填，`sheet_id` / `sheet_title` 必须二选一；字段列表支持分页 |
| `smartsheet-records` | 参数同字段列表；返回每条记录的所有列，不接受 `field_titles` / `sql` / 任意过滤表达式 |
| `smartsheet-query` | `docid`、`sheet_title`、`field_titles`（1–20 列）必填，`limit` 默认 10、1–50；只生成单条 SELECT 投影，不接受 SQL、表达式或任意排序；标题不能含反引号或反斜杠 |

支持分页的操作为 `doc-search`、`meeting-list`、`mail-search`、`disk-list`、`disk-search`、`smartsheet-fields`、`smartsheet-records`：`limit` 默认 10、1–50，`cursor` 必须为非空字符串，首页省略；不会自动翻页。需要更多数据时使用返回游标再次请求，保留覆盖范围说明。JSON 可用 `--json -` 从标准输入读取；不接受任何透传 CLI 参数或写操作。

返回 `{operation, source:"wecom_cli_user_token", read_only:true, data, limitation}`，业务数据在 `data`。CLI 可能将较长正文写入临时文件，连接器只读取该临时目录内的普通文件，将内容放回 `content`（已有该字段时放入 `file_content`）后删除临时目录；不会返回已经失效的本地路径。输入最多 64KB、结果最多 2MiB，超限报错。CLI 调用总预算 60 秒，MCP 30 秒，包含版本检查、授权和子进程执行；超时终止整个 CLI 进程组。输出已完整读到、仅等待退出码时，保留剩余预算且至少给 2 秒退出宽限，不丢弃已读结果。底层云端 HTTP 的 I/O 超时仍受操作系统行为影响。

这是用户凭据的替代接入路径，没有延长 Bot 的七天权限。用户授权被撤销、refresh token 到期或刷新结果不确定时，仍需根据错误重新扫码；不能承诺永不重新授权。不含智能文档正文、会议转写/录制、微盘下载、邮件附件下载或任何写接口。

新增领域示例（ID/标题均从上一条列表结果获取，不使用示例占位值请求真实资源）：

```bash
dayuan wecom contact-search --json '{"keywords":["张三"]}'
dayuan wecom calendar-list --json '{"begin_time":"2026-10-22 00:00:00","end_time":"2026-10-23 00:00:00"}'
dayuan wecom mail-search --json '{"begin_time":"2026-10-20 00:00:00","end_time":"2026-10-23 00:00:00","limit":3}'
dayuan wecom disk-search --json '{"file_types":["sheet","smartsheet"],"limit":3}'
dayuan wecom sheet-read --json '{"docid":"<docid>","sheet":"<子表ID>","range":"A1:C3"}'
dayuan wecom smartsheet-query --json '{"docid":"<docid>","sheet_title":"<子表标题>","field_titles":["<字段标题>"],"limit":1}'
```

微盘类型过滤支持 `doc` / `sheet` / `ppt` / `collect` / `mind` / `flow` / `smartsheet` / `smartpage` / `journal` / `pdf` / `offline_word` / `offline_excel` / `offline_ppt` / `offline_pdf` / `image` / `videoaudio` / `design`；搜索命中不表示支持该类型正文读取。关键词和类型数组均非空、最多 10 项。日程、邮件时间按企微接口时间解释，不接收带时区的 ISO 字符串。

`mail-get` 的单封失败保留在 `data.mail_list[].errcode`，调用方应逐封判断，不能把批次返回成功当成每封成功。表格 `not_viewable_cells`、`truncated` 等可见性/截断信息原样保留。智能表格查询统一将 `data.values` 返回为对象数组，每项包含 `rows`，上游提供的 `columns` 原样保留，兼容 CLI 返回对象或 JSON 字符串的两种编码。智能表格的顶层 JSON 临时文件会在同一目录/大小限制下解析；业务列名 `file_path` 仅作为数据，不触发文件读取。所有列表都只证明本次响应覆盖范围，不能当作全量同步。

常见错误：`WECOM_CLI_NOT_FOUND` / `WECOM_CLI_VERSION_UNSUPPORTED` 表示需要安装或指定新版 CLI；`LOGIN_REQUIRED*` / `WECOM_AUTH_REQUIRED_OPEN_WECOM_LOGIN` 按原登录流程处理；`WECOM_CLI_TIMEOUT` 保留只读查询条件后稍后重试；`WECOM_CLI_ERROR_<数字>` 是上游业务错误码，原始错误正文不会透出。

## 在 Grok Bot 中使用

已验证的路径是 **Grok Bot 桌面主助手 → 用户 Mac 的本机执行能力 → Dayuan Lite CLI → 企业微信 A2A**。Grok Bot 的云端电脑和本机不是同一个环境；安装或配置 `grok` 命令行程序也不等于配置了 Grok Bot 桌面应用。

先在本机完成上面的扫码登录，再把 Python、连接器和授权状态目录的绝对路径提供给 Bot。无需把凭据交给 Bot，也无需安装 WorkBuddy。完整步骤与可复制的只读查询 Prompt 见 **[Bot 使用说明与示例 Prompt](docs/BOT-USAGE.md)**。

2026-10-15 已通过 Grok Bot 实际完成一次查询：本机回执与 Bot 最终回复中的任务 ID、`completed` 状态一致，摘要已返回对话。此次验收使用 CLI，没有注册长期 MCP 配置或保存私有技能。

## 接入 MCP

**MCP 的状态目录必须与登录命令一致。** 登录和 MCP 都用默认目录时不需要额外配置；用了自定义目录时，在 `args` 里加 `--state-dir`，或在宿主配置里设置 `DAYUAN_STATE_DIR`。

用 pipx 安装后（宿主的 PATH 可能不含 `~/.local/bin`，建议写 `command -v dayuan` 输出的绝对路径）：

```json
{"mcpServers": {"dayuan-lite": {"command": "/absolute/path/to/dayuan", "args": ["serve"]}}}
```

用 uvx（不安装）：

```json
{"mcpServers": {"dayuan-lite": {"command": "uvx", "args": ["--from", "git+https://github.com/lighters/dayuan-lite.git", "dayuan", "serve"]}}}
```

克隆仓库直接运行时参考 [mcp-config.example.json](mcp-config.example.json)，把路径替换为实际绝对路径；Python 必须是 3.9+，宿主无法解析 `python3` 时写它的绝对路径。手动启动服务：

```bash
dayuan serve
```

| 工具 | 功能 |
| --- | --- |
| `dayuan_capabilities` | 本地能力和授权恢复指引；`online:true` 才换票和探测 CLI 版本 |
| `dayuan_read_reply` | 在原只读任务中回答 `input_required` 澄清；独立稳定回复 ID |
| `dayuan_status` | 本地状态，不发远端请求 |
| `dayuan_wecom_read` | 文档/会议/通讯录/日程/邮件正文/微盘元数据/表格同步只读接口，参数为 `operation` 和 `parameters`，每次自动检查用户授权 |
| `dayuan_read_start` | 提交有界查询，返回任务状态；可选 `wait_seconds` |
| `dayuan_read_result` | 按 `request_id` 查询已有任务，只读；可选 `wait_seconds` |

群查询、结果查询及追问工具都接受 `wait_seconds`（0–30，默认 0）：服务端在本次调用内按同一 `request_id` 轮询，Agent 不必自己循环。上限 30 秒是因为 stdio 服务一次只处理一个调用；仍未完成时用同一 ID 再调一次 `dayuan_read_result`。`initialize` 会返回 `instructions`，向宿主说明「提交 → 用同一 ID 查结果」的流程；`dayuan_status` 与 `dayuan_read_result` 标注了 `readOnlyHint: true`，`dayuan_read_start` / `dayuan_read_reply` 因会提交远端任务消息和创建本地回执而不标只读，但标注了 `idempotentHint: true`（同一 ID 只恢复原任务）。

企微读取工具 `dayuan_wecom_read` 与 A2A 任务不同：同步返回 `data`，无需 `request_id`，也不用 `dayuan_read_result`。宿主的环境需能找到新版 CLI；必要时在 MCP 配置的 `env` 中设置 `DAYUAN_WECOM_CLI` 为绝对路径。该工具标注 `readOnlyHint:true`。

登录只在 CLI 中提供。MCP 通过标准输入输出通信，不监听 HTTP 端口。

## 续期与恢复

正常读取会检查缓存，到期前自动续期。也可以手动触发：

```bash
dayuan renew
```

如果出现 `WECOM_AUTH_REQUIRED_OPEN_WECOM_LOGIN`，在完成账号登录的同一浏览器中授权同一企业微信账号：

```bash
dayuan wecom-login --open
# 页面成功后
dayuan renew
```

`result` 查询到任务进入 `completed` / `failed` / `cancelled` / `rejected` 后，连接器会把整理后的结果（`text`、`task_id`、`limitation` 等字段，不含凭据和上游原始响应）写进该 `request_id` 的本地回执；之后对同一 ID 的 `result` / `dayuan_read_result` 直接返回这份副本，不再向企业微信 A2A 查询任务；授权预检仍会执行，必要时可能续期。仍在 `submitted` / `working` 的任务每次都会联网查询。缓存只对同一账号身份有效，身份变化仍返回 `CREDENTIAL_CHANGED_OR_MISSING`；用 `dayuan gc` 删除回执后，该结果也随之删除。

缺少 refresh token、长期授权到期或撤销时，需要重新授权。刷新请求发出后结果不确定时，客户端会停止自动重放并报告 `REFRESH_OUTCOME_UNKNOWN_LOGIN_REQUIRED`；重新运行 `login` 恢复。请求发出前就失败的情况（等待预算耗尽 `WAIT_TIMEOUT`、域名解析失败或连接被拒 `TRANSPORT_NOT_SENT`）不会进入该状态，稍后再试即可。不要把旧凭据文件覆盖回去。

切换用户或企业应使用新的 `--state-dir`。未到期的凭据可并发读取；需要续期时，其他调用最多等待 30 秒，仍未结束才返回 `AUTH_BUSY`，此时稍后再试。

### 回执管理

每次提交都会在状态目录的 `requests/` 下留下一份回执（`request_id`、状态、任务 ID、创建时间等，不含查询内容和凭据），并在 `.request-locks/` 下留下一个锁文件；两者都不会自动清理。追问另有 `reply-barriers/` 屏障，由回执状态判定是否仍不确定；`gc` 不删除屏障。两个维护命令只读写本地文件，不联网、不需要有效授权，也没有对应的 MCP 工具：

```bash
# 按创建时间倒序列出本地回执，可按状态过滤
dayuan list
dayuan list --state working

# 先预览：30 天前创建、且已到终态的回执
dayuan gc --older-than 30d
# 确认后真正删除回执，保留锁文件
dayuan gc --older-than 30d --yes
```

`gc` 只删除 `completed` / `failed` / `cancelled` / `rejected` 的回执；`dispatching`、`submitted`、`working`、`outcome_unknown` 等状态一律保留，因为它们可能仍在进行或需要人工判断。实际删除前会创建缺失的锁文件，并且必须能立即拿到该 `request_id` 的进程锁，拿不到的回执会列在 `skipped_busy` 中并跳过；持锁后重新读取回执并检查终态及创建时间，避免误删已重试的活动任务。锁文件永久保留，避免等待中的进程使用不同锁实例；预览不创建锁文件。`--older-than` 必须写成 `Nd`（N 为天数）。被删除的 `request_id` 之后再提交会被当作新任务。

## 数据与权限

- 凭据只保存在状态目录（默认 `~/.config/dayuan-lite`，不在仓库内），目录权限 700、文件权限 600；旧版的 `.private*/`、凭据文件和本地配置仍在 `.gitignore` 中。
- 凭据文件是受文件权限保护的明文，不要共享、提交或上传状态目录。授权链接也应视为敏感内容。
- 账号请求仅发往 `www.workbuddy.cn`，A2A 请求仅发往 `qyapi.weixin.qq.com`，拒绝 HTTP 重定向。
- 只暴露查询模板，提示远端不要发送消息或执行其他写操作。提示词不等于服务器强制的只读权限。
- 远端返回内容作为数据处理，不应作为 Agent 的新指令执行。
- 显式 `DAYUAN_ACCESS_TOKEN` 优先于云端会话；该方式不会自动续期。兼容命令 `import-workbuddy-auth` 是可选的旧凭据导入路径，首次独立登录不使用它。

## 开发与验证

无需安装依赖：

```bash
python3 scripts/check.py
python3 -m unittest test_dayuan.py test_workbuddy_auth.py test_wecom_bridge.py -v
```

离线测试覆盖统一入口与参数位置、默认状态目录及旧目录迁移提示、打包声明、查询约束、幂等提交、被拒请求重提、服务端有界等待、异常结果恢复、上游异常响应、并发续期、凭据轮换、稳定身份检查和 MCP 握手与工具声明。测试不访问真实账号。GitHub Actions 在 Python 3.9 / 3.12 / 3.13 上运行 `scripts/check.py`（项目约束检查）和全部测试。贡献规范见 [AGENTS.md](AGENTS.md)。

2026-10-15 的原型已完成独立扫码、真实换票、A2A 查询、主动续期及新 MCP 进程恢复任务。自动续期也通过了联机验证：人为将本地缓存期限设为过期，再由普通读取触发真实云端续期。尚未观察跨天自然到期的长期运行。

详细说明见 [架构](docs/ARCHITECTURE.md) 和 [验证范围](docs/VALIDATION.md)。
