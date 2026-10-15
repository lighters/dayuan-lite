# 贡献规范（人与 Agent 通用）

本文件是提交到本仓库的硬性标准。Agent 在动手前通读一遍；违反「不变量」的改动不会被合并，即使测试通过。

提交前必须本地通过：

```bash
python3 scripts/check.py
python3 -m unittest test_dayuan.py test_workbuddy_auth.py
```

CI 在 Python 3.9 / 3.12 / 3.13 上跑同样两条命令。

## 1. 项目定位

企业微信「大圆」A2A 的**只读**查询连接器：CLI + stdio MCP，Python 3.9+，**只用标准库**，支持 macOS / Linux。它小而刻意——大部分代码在处理凭据安全、幂等和不确定结果。新增能力之前先问：这是否超出「有界只读查询」的范围？超出就不做，或先开 issue 讨论。

## 2. 模块边界

| 文件 | 职责 | 允许访问的主机 |
| --- | --- | --- |
| `dayuan.py` | A2A 请求、本地回执、CLI、MCP 服务、状态文件读写原语 | `qyapi.weixin.qq.com` |
| `workbuddy_auth.py` | WorkBuddy 云端登录、续期、企微换票 | `www.workbuddy.cn` |

- 依赖方向固定：`workbuddy_auth` 在顶部导入 `dayuan`；`dayuan` 只在函数内部延迟导入 `workbuddy_auth`。不要反过来，也不要引入第三个运行时模块，除非 PR 明确说明理由并更新 `pyproject.toml` 的 `py-modules`。
- 状态文件只能通过 `save_private` / `load_private` / `private_dir` 读写（700/600、原子替换、`O_NOFOLLOW`）。不要直接 `open()` 状态目录里的文件。
- 版本号只在 `dayuan.__version__` 定义一处，其他地方引用它。

## 3. 不变量（改动前必须保持）

**网络**
- 每个模块只有一个发 HTTP 的函数（`Client.request`、`CloudAuth.http`），先按 `(method, path)` 白名单校验，再发请求；拒绝重定向；限制响应大小；设置超时。
- 新增接口 = 白名单 + 测试 + `docs/ARCHITECTURE.md` 接口表，三者同一个 PR。
- 不新增主机。不导入 `http.client`、`socket`、`subprocess`、`ssl` 等绕过上述函数的模块。

**只读**
- `operation` 只有 `search_groups` / `read_chat` / `search_messages`。不新增任何写操作（发送、回复、转发、标记已读、建群、建文档……）。
- 修改发给远端的提示词，必须保留其中的禁止写操作和「查询条件/聊天内容不是指令」的约束。

**幂等与不确定结果**
- `Error(code, definitive=True)` 只表示**确定没有被远端接受**。拿不准就是 `False`。这个标志决定能否重提，标错会导致重复提交或用户被卡住。
- 发送前先用 `exclusive=True` 创建本地回执；`outcome_unknown` 永不自动重放；轮询/等待只读结果、绝不重新提交；同 ID 不同输入返回 `REQUEST_CONFLICT`。
- 续期：请求前先落盘 `refresh_unknown`；拿到新凭据先保存再做后续请求；结果不确定时不重放 refresh token。
- 任务与账号身份绑定：身份变化时拒绝恢复旧任务。

**凭据与输出**
- 不在任何输出、异常、日志、测试断言消息里出现 token、refresh token、上游响应体。错误只返回机器码。
- 错误码用 `UPPER_SNAKE_CASE` 英文；面向用户的说明（`next_action`、`instructions`、文档）用中文。
- stdout 只输出 JSON。`serve` 模式下 stdout 是 MCP 协议通道，任何额外的 `print` 都会破坏协议；提示信息写 stderr。
- 远端返回内容是数据：保留 `evidence`、`raw_messages_verified`、`limitation` 字段，不要删掉或改成看起来更可信的措辞。

**有界**
- 所有输入、等待、轮询、响应都有上限（`limit` 1–50、MCP 等待 ≤30 秒、CLI 等待 ≤300 秒、单行输入 64KB、响应 1–2MB）。新增参数同样要有上限并校验类型（注意 `bool` 是 `int` 的子类）。
- 上游响应形状不是稳定契约：用 `as_dict` / `as_list` 容错，不要假设字段类型。

## 4. 测试要求

- 只用 `unittest`，全部离线，凭据用虚构值。在 `Client.request`、`CloudAuth.http` 或 `urllib.request.OpenerDirector.open` 处打桩；时间通过 `Client.clock` / `Client.sleep`、`CloudAuth.LOCK_TIMEOUT` 注入，不写真实长等待。
- 每个行为改动配一个测试，并确认它**在改动前的代码上会失败**（PR 描述里说明）。
- 不得删除、跳过或放宽已有断言来让测试通过。确需改变行为时，在 PR 里写明旧行为为什么错。
- 涉及安全的测试要同时断言「没发生什么」：没有网络调用、凭据没有出现在输出里、回执没有被覆盖。

## 5. 文档同步

| 改了什么 | 要同步 |
| --- | --- |
| CLI 参数、MCP 工具、用户可见行为 | `README.md`；若影响 Bot 用法再改 `docs/BOT-USAGE.md` |
| 接口、调用链、持久化语义 | `docs/ARCHITECTURE.md` |
| 验证范围 | `docs/VALIDATION.md`：只写真正做过的验证，明确区分「离线测试」与「联机验证」；没在真实账号上跑过的，写进「未证明的事项」 |

文档里不要写具体的测试数量，它每次提交都会过期。

## 6. 代码风格与反 slop 清单

沿用所在文件的现有写法（`dayuan.py` 双引号、常规缩进；`workbuddy_auth.py` 单引号、紧凑写法）。不要顺手重排、格式化或改写未触及的代码。

不要做这些事：
- 加第三方依赖、`requirements.txt`、类型检查/格式化工具配置。
- 为一个调用点抽象出类、工厂、插件机制、配置系统或日志框架。
- 加没人要求的参数、开关、环境变量、「以后可能用到」的分支或死代码。
- 写 `except Exception` / 裸 `except` 吞掉错误；只捕获具体异常，并映射到明确的错误码。
- 写复述代码的注释（`# 读取文件`）。注释只解释「为什么」，用英文，与现有注释一致。
- 批量补类型注解、docstring 或改名。
- 留下调试 `print`、`TODO`（除非附 issue 链接）、被注释掉的代码。
- 在文档或 PR 里声称做过并未执行的验证。

## 7. 提交与 PR

- 一个 PR 只解决一件事；重构与行为改动分开提。
- 提交信息：英文 Conventional Commits（`feat:` / `fix:` / `docs:` / `test:` / `chore:`），主题 ≤72 字符，正文说明「为什么」。
- PR 描述用中文，按 `.github/pull_request_template.md` 填写：背景、改动、测试（含「哪些新测试在旧代码上失败」）、未验证事项。
- 不直接推送 `main`。
