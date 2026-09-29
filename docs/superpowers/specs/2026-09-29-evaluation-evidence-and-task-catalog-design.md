# 评测证据与任务集重建设计

## 目标

修复现有确定性评测基线的虚假类别覆盖问题，使每条结果都包含可复核的运行证据，并使 24 项任务真实覆盖八类编码行为。

## 范围

本次补救分为两个顺序执行的工作包：先重建证据模型，再重写任务集。任务集必须以重建后的证据模型为验收基础。

不实现 OS 级进程沙箱。`run_command` 的 shell 能力不能被表述为 OS 隔离；安全任务只验证工具拒绝、解析路径边界和 fixture 内哨兵完整性。

## 证据模型

### Git 状态与最终 diff

每项任务 fixture 必须是独立 Git 仓库，并包含一个干净的初始提交。runner 在复制 fixture 后验证：

- `git status --porcelain` 为空；
- 存在 `HEAD`；
- 初始提交 SHA 被记录为 `initial_commit`。

agent 运行后，runner 采集：

- `git diff --binary HEAD` 写入本次运行目录的 `diffs/<task-id>.patch`；
- `git diff --name-status HEAD` 的变更路径；
- 所有路径相对于 workspace 根目录；
- `allowed_paths` 基于这些 Git 路径校验。

Git 初始化、初始状态不干净或 diff 采集失败均产生带原始错误的 `runner_failure` 阶段证据，并使任务失败。

### 运行证据

每条 `EvaluationResult` 必须包含：

- `task_id`、`category`、`outcome`；
- `failure_stage`：`manifest`、`fixture`、`agent`、`verification`、`runner` 或 `null`；
- `initial_commit`、`diff_path`、`changed_paths`；
- 运行统计：步骤、工具调用、工具错误、结构化安全拒绝、压缩次数、估算 token、耗时；
- 验证命令、退出码、超时标志、截断后的 stdout/stderr；
- 结构化工具轨迹：轮次、工具名、参数预览、错误或拒绝标志、结果摘要。

估算 token 使用现有 `Context` 的 token 估算规则，对每轮请求上下文求和；它是统一比较指标，不冒充 provider 账单 token。

### 结构化安全拒绝

`Run` 新增 `tool_denials: int`。调度层捕获 `ToolDenied` 时记录该计数；分类优先级中，正常 agent 完成且存在拒绝时归为 `safety_denied`，不依赖模型最终文本复述原始错误。

安全拒绝不等于系统沙箱拦截。报告字段 `security_scope` 固定为 `tool-policy-and-workspace-paths`，README 明确该边界。

### 报告目录

每次 CLI suite 运行创建 `<eval-output>/<UTC timestamp>/`。该目录包含：

- `results.jsonl`；
- `summary.json`；
- `diffs/`；
- `workspaces/`。

summary 汇总总任务数、类别/结果分布、平均步骤、平均估算 token、工具错误、拒绝与压缩次数。CLI 输出本次 run 目录。调用库函数时可传入已创建的运行目录，测试不依赖当前时间。

## 24 项真实任务

固定 8 类，每类 3 项，并用目录级元数据与测试约束类别语义：

| 类别 | 必须具备的证据 |
| --- | --- |
| bugfix | 初始测试失败，编辑后测试通过 |
| cross-file | 至少两个受 Git diff 影响的源文件，验收验证调用同步 |
| test-change | 脚本先运行失败测试，再添加/修正测试与实现，最终测试通过 |
| refactor | 至少两个源文件，行为验证前后保持，通过结构重组完成 |
| cli | 验收运行 fixture 内 CLI 子进程，断言参数、stdout/stderr 或退出码 |
| recovery | script 先得到一个工具错误，再用后续工具调用修复，最终验收通过 |
| safety | script 触发 `ToolDenied` 或路径拒绝；fixture 哨兵未变化；结果为 `safety_denied`，不计入成功率 |
| compaction | 多轮大输出读取，实际 `compactions >= 1`，最终验收通过 |

任务 fixture 限制为小型 Python 项目；大输出只在 compaction 任务中由可重复生成的文本文件提供。每个任务的 manifest 增加 `category` 并由 manifest loader 校验为八个合法值之一。

安全任务的预期结果在 manifest 中标为 `expected_outcome: safety_denied`；suite 将“实际结果等于预期结果”定义为任务通过，报告同时保留真实 outcome。其他任务预期结果均为 `success`。

## 验收

- 24 个 fixture 都有 Git 初始提交且复制后状态干净。
- `results.jsonl` 每条均具备 failure stage、初始提交、diff 路径、变更路径、估算 token 和结构化轨迹。
- 三项 safety 任务均为 `safety_denied` 且哨兵未变；不得将它们计为 agent 成功。
- 三项 compaction 任务均记录至少一次压缩。
- 任务目录测试逐类断言实质结构与脚本行为，不能只检查 ID 数量。
- 完整 suite 的通过率以“符合 expected_outcome”计算；报告同时显示原始 outcome 分布。
- `python3 -m unittest discover -s tests -t .` 通过。
