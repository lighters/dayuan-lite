# 统一只读接入实现计划

范围：在现有三模块中补齐交付入口与只读追问；不增加业务写接口、下载、附件或后台服务。使用 Python 标准库与现有 setuptools 打包。

- [x] 追问：`dayuan reply --json` / MCP `dayuan_read_reply`，原 request_id 作为 parent_request_id，每次回复独立 request_id。仅最新远端状态为 input_required 时发送，携带原 taskId/contextId，保留只读约束。父任务锁与回复锁串行化；先持久化再发送；不确定结果不自动重放；拒绝任务/账号变化和重复 ID 不同输入。
- [x] 能力检查：`doctor` / MCP `dayuan_capabilities` 返回已实现能力、环境与授权层次。默认本地检查，显式 online 才换票及检查 CLI 版本；不调用业务接口，不将配置存在或换票成功当成业务权限证明。错误附具体恢复步骤，保留原机器码。
- [x] 交付：仓库安装脚本复用 uv/pipx，官方 CLI 固定已验版本作为可选项；可显式复制项目 Skill。安装后 `dayuan setup` 输出使用当前解释器的 MCP 配置，不自动改其他客户端设置。wheel/sdist 携带 Skill。
- [x] 验证：新增回归先在旧实现上失败，再实现并运行规定检查；构建隔离安装包，实际执行 setup、doctor、MCP，测试安装脚本受控依赖调用与 Skill 内容。真实追问只在能取得 input_required 样本时验证，无法取得则如实记录。
- [x] 同步 README、架构、验收、Bot 指引并审查 diff、身份；交付使用功能分支与 PR，远端检查结果以 PR 页面为准，不直接推送 main。

验收重点：首次安装缺少 uv/pipx/npm；含空格的路径；非默认状态目录；无授权/refresh_unknown；跨账号回复；同 ID 并发与不同 ID 回复竞争；POST 后断网；返回错 task/context；旧工具与 CLI 行为兼容。
