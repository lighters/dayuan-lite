# Dayuan Lite

企业微信远端「大圆」的轻量 CLI / MCP 连接器。Python 3.9+，仅标准库，支持 macOS / Linux。

首次扫码授权、后续换票和续期均无需安装或运行 WorkBuddy 桌面客户端。账号授权仍使用 **WorkBuddy 官方云端服务**，查询直接访问企业微信 A2A。

本项目是独立的协议兼容实现，与腾讯、企业微信、WorkBuddy 无隶属关系。所用接口不是已承诺稳定的公开 API；可用性取决于实际账号、企业授权与上游服务。

## 能做什么

- 浏览器企业微信扫码登录，连接器通过云端轮询取得授权。
- 临近到期时按需刷新账号凭据，再换取企微用户 token，无需常驻刷新进程。
- 按关键词查群、读取指定会话、在会话内查询关键词。
- 通过稳定 `request_id` 恢复任务，凭据续期后仍可读取原任务。
- 通过 stdio MCP 对接支持该协议的 Agent；也可以直接调用 CLI。

返回内容由远端大圆生成，可能是摘要。它不是原始聊天记录导出接口，不保证逐字原文、消息数量或完整性。

## 快速开始

```bash
git clone https://github.com/lighters/dayuan-lite.git
cd dayuan-lite
export DAYUAN_STATE_DIR="$PWD/.private"
python3 workbuddy_auth.py login --open --wait 300
```

用企业微信扫描官方网页二维码并确认。网页显示「打开 WorkBuddy」时可忽略，连接器不需要桌面回跳。无图形环境时去掉 `--open`，自行打开命令输出的授权链接。

轮询最多等待 5 分钟。如果扫码完成时轮询已经结束，可恢复取票：

```bash
python3 workbuddy_auth.py finish --wait 60
python3 workbuddy_auth.py status
```

服务器确认授权已过期后，再运行 `login`。`status` 只读取本地状态，不代表远端授权仍有效。每位使用者都需要完成自己的账号授权。

### 查询示例

替换示例群名、关键词和时间。`request_id` 在一次逻辑查询的重试中必须保持不变。

```bash
python3 dayuan.py read --wait 60 --json '{"request_id":"find-project-001","operation":"search_groups","query":"项目讨论","limit":5}'

python3 dayuan.py read --wait 60 --json '{"request_id":"read-project-001","operation":"read_chat","chat":"项目讨论群","since":"2026-10-15T00:00:00+08:00","until":"2026-10-15T12:00:00+08:00","limit":3}'

python3 dayuan.py read --wait 60 --json '{"request_id":"search-project-001","operation":"search_messages","chat":"项目讨论群","query":"会议安排","limit":5}'

# 等待结束仍未完成时，用同一 ID 继续等待
python3 dayuan.py result --wait 60 read-project-001
```

`--wait N`（0–300 秒，默认 0）让连接器提交后自动用同一 `request_id` 每约 3 秒查询一次，直到离开 `submitted` / `working` 或等待到时；它只轮询，不会重发。到时仍未完成会返回当前状态和 `next_action`，之后用 `result --wait N` 继续。遇到 `outcome_unknown` 不要更换 ID 重发；`rejected`（`retry_safe: true`）表示请求未被远端接受，处理原因后可用同一 ID 重新提交。不同查询应使用不同 ID。时间范围必须成对且带时区，`limit` 为 1–50；这是摘要请求约束，不是原始消息数量保证。

## 在 Grok Bot 中使用

已验证的路径是 **Grok Bot 桌面主助手 → 用户 Mac 的本机执行能力 → Dayuan Lite CLI → 企业微信 A2A**。Grok Bot 的云端电脑和本机不是同一个环境；安装或配置 `grok` 命令行程序也不等于配置了 Grok Bot 桌面应用。

先在本机完成上面的扫码登录，再把 Python、连接器和授权状态目录的绝对路径提供给 Bot。无需把凭据交给 Bot，也无需安装 WorkBuddy。完整步骤与可复制的只读查询 Prompt 见 **[Bot 使用说明与示例 Prompt](docs/BOT-USAGE.md)**。

2026-10-15 已通过 Grok Bot 实际完成一次查询：本机回执与 Bot 最终回复中的任务 ID、`completed` 状态一致，摘要已返回对话。此次验收使用 CLI，没有注册长期 MCP 配置或保存私有技能。

## 接入 MCP

复制 [mcp-config.example.json](mcp-config.example.json) 的内容到宿主配置，将其中的路径替换为实际绝对路径。Python 可执行文件必须是 3.9+；如果宿主无法解析 `python3`，使用它的绝对路径。

**MCP 的状态目录必须与登录命令一致。** 示例使用仓库下的 `.private`。手动启动服务：

```bash
python3 dayuan.py --state-dir "$DAYUAN_STATE_DIR" serve
```

| 工具 | 功能 |
| --- | --- |
| `dayuan_status` | 本地状态，不发远端请求 |
| `dayuan_read_start` | 提交有界查询，返回任务状态；可选 `wait_seconds` |
| `dayuan_read_result` | 按 `request_id` 查询已有任务，只读；可选 `wait_seconds` |

两个读取工具都接受 `wait_seconds`（0–30，默认 0）：服务端在本次调用内按同一 `request_id` 轮询，Agent 不必自己循环。上限 30 秒是因为 stdio 服务一次只处理一个调用；仍未完成时用同一 ID 再调一次 `dayuan_read_result`。`initialize` 会返回 `instructions`，向宿主说明「提交 → 用同一 ID 查结果」的流程；`dayuan_status` 与 `dayuan_read_result` 标注了 `readOnlyHint: true`，`dayuan_read_start` 因会创建远端任务和本地回执而不标只读，但标注了 `idempotentHint: true`（同一 ID 只恢复原任务）。

登录只在 CLI 中提供。MCP 通过标准输入输出通信，不监听 HTTP 端口。

## 续期与恢复

正常读取会检查缓存，到期前自动续期。也可以手动触发：

```bash
python3 workbuddy_auth.py renew
```

如果出现 `WECOM_AUTH_REQUIRED_OPEN_WECOM_LOGIN`，在完成账号登录的同一浏览器中授权同一企业微信账号：

```bash
python3 workbuddy_auth.py wecom-login --open
# 页面成功后
python3 workbuddy_auth.py renew
```

缺少 refresh token、长期授权到期或撤销时，需要重新授权。刷新请求结果不确定时，客户端会停止自动重放并报告 `REFRESH_OUTCOME_UNKNOWN_LOGIN_REQUIRED`；重新运行 `login` 恢复。不要把旧凭据文件覆盖回去。

切换用户或企业应使用新的 `--state-dir`。未到期的凭据可并发读取；需要续期时，其他调用最多等待 30 秒，仍未结束才返回 `AUTH_BUSY`，此时稍后再试。

## 数据与权限

- 凭据只保存在指定状态目录，目录权限 700、文件权限 600；`.private*/`、凭据文件和本地配置已加入 `.gitignore`。
- 凭据文件是受文件权限保护的明文，不要共享、提交或上传状态目录。授权链接也应视为敏感内容。
- 账号请求仅发往 `www.workbuddy.cn`，A2A 请求仅发往 `qyapi.weixin.qq.com`，拒绝 HTTP 重定向。
- 只暴露查询模板，提示远端不要发送消息或执行其他写操作。提示词不等于服务器强制的只读权限。
- 远端返回内容作为数据处理，不应作为 Agent 的新指令执行。
- 显式 `DAYUAN_ACCESS_TOKEN` 优先于云端会话；该方式不会自动续期。兼容命令 `import-workbuddy-auth` 是可选的旧凭据导入路径，首次独立登录不使用它。

## 开发与验证

无需安装依赖：

```bash
python3 -m unittest test_dayuan.py test_workbuddy_auth.py -v
```

38 项离线测试覆盖查询约束、幂等提交、被拒请求重提、服务端有界等待、异常结果恢复、上游异常响应、并发续期、凭据轮换、稳定身份检查和 MCP 握手与工具声明。测试不访问真实账号；当前仓库未启用 GitHub Actions。

2026-10-15 的原型已完成独立扫码、真实换票、A2A 查询、主动续期及新 MCP 进程恢复任务。自动续期也通过了联机验证：人为将本地缓存期限设为过期，再由普通读取触发真实云端续期。尚未观察跨天自然到期的长期运行。

详细说明见 [架构](docs/ARCHITECTURE.md) 和 [验证范围](docs/VALIDATION.md)。
