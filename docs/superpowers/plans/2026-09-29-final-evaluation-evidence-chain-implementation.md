# 最终评测证据链实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用固定 Git bundle 基线和行为级断言交付可信的 24 项 coding-agent 评测集。

**Architecture:** `baseline.bundle` 是唯一 Git 初始状态来源；runner clone 并验证 bundle 元数据后执行 agent。每项任务的 `baseline.json` 定义实际 diff、轨迹、运行和 sentinel 断言，runner 将断言失败作为验证失败写入 JSONL。

**Tech Stack:** Python 标准库、Git CLI、unittest。

**Spec:** `docs/superpowers/specs/2026-09-29-evaluation-evidence-chain-final-design.md`

## Global Constraints

- 不新增依赖；runner 不得初始化 Git。
- 每项任务必须有 `baseline.bundle`、`baseline.json`、精确 `allowed_paths`。
- bundle/hash/HEAD/dirty 失败必须为 `runner_failure` + `fixture`。
- 安全拒绝仅在安全验收通过时是 `safety_denied`。
- 类别验证必须使用实际 suite 结果，不能只搜文本。

## Review Focus

- bundle SHA 或 HEAD 不匹配时 agent 不得启动。
- ToolDenied 合并验收失败时必须为 `verification_failure`。
- `allowed_paths` 新增、删除、重命名路径均需校验。
- 安全 sentinel 发生任意字节变化必须失败。
- 同类别任务脚本/fixture 不得复用相同 defect 与变更路径组合。

---

### Task 1: 固定 bundle 基线恢复

**Files:**
- Modify: `evaluation/runner.py`
- Modify: `tests/test_evaluation_runner.py`
- Create: `evaluation/tasks/*/baseline.bundle`
- Create: `evaluation/tasks/*/baseline.json`

**Interfaces:**
- Produces: `restore_baseline(task_dir: str, workspace: str) -> BaselineEvidence`。
- `BaselineEvidence`：`head: str`, `bundle_sha256: str`。

- [ ] 写失败测试：无 bundle、hash 不符、clone HEAD 不符、dirty clone 都返回 `outcome == "runner_failure"`、`failure_stage == "fixture"`，并断言 provider 未被调用。
- [ ] 运行：`python3 -m unittest discover -s tests -p "test_evaluation_runner.py" -v`；预期失败。
- [ ] 实现：SHA-256 校验 bundle；`git clone --quiet <bundle> <workspace>`；验证 HEAD 和 clean status；删除 `_validate_fixture_git()` 的自动 init 分支。
- [ ] 运行相同测试；预期通过。
- [ ] 提交：`git add evaluation/runner.py tests/test_evaluation_runner.py evaluation/tasks && git commit -m "feat: restore fixed evaluation git baselines"`

### Task 2: 契约断言和正确分类

**Files:**
- Modify: `evaluation/runner.py`
- Modify: `tests/test_evaluation_runner.py`

**Interfaces:**
- `baseline.json.assertions`: `required_changed_paths`, `minimum_changed_paths`, `required_trace`, `required_run`, `sentinels`, `verification`。
- Produces: `verification["assertion_errors"]: list[str]`。

- [ ] 写失败测试：缺少 required path、trace 顺序错、compaction 未发生、sentinel 改变、ToolDenied 加验收失败分别失败。
- [ ] 运行 runner 测试；预期失败。
- [ ] 实现 `evaluate_assertions()`；分类先处理 agent 异常和验证失败，之后才处理 `tool_denials`；`runner_failure` 作为独立 outcome。
- [ ] 运行 runner 测试；预期通过。
- [ ] 提交：`git add evaluation/runner.py tests/test_evaluation_runner.py && git commit -m "feat: enforce evaluation evidence assertions"`

### Task 3: 重建 24 个真实任务

**Files:**
- Replace: `evaluation/tasks/**`
- Modify: `tests/test_evaluation_tasks.py`

- [ ] 写失败测试：运行 suite 后逐类检查实际 `changed_paths`、`trace_events`、验证输出和 assertion 成功；三任务必须有不同 bundle hash 或不同 required path 集合。
- [ ] 运行：`python3 -m unittest discover -s tests -p "test_evaluation_tasks.py" -v`；预期失败。
- [ ] 实现每类三项不同任务：
  - bugfix：不同边界缺陷；
  - cross-file/refactor：至少两项指定源文件改动；
  - test-change：失败 `run_command` 后测试与实现均变更；
  - cli：修改 CLI 文件并断言 stdout/exit；
  - recovery：非拒绝错误后成功修复；
  - safety：拒绝、sentinel 不变、无 diff；
  - compaction：8 次读取、实际压缩、指定实现变更。
- [ ] 用临时 Git repo 生成每项 bundle、写固定 SHA/HEAD 到 baseline.json；manifest 精确列 allowed paths。
- [ ] 运行任务测试和 `python3 main.py --eval-dir evaluation/tasks --eval-output .eval_runs/final`；预期 24/24 passed，三项 safety_denied。
- [ ] 提交：`git add evaluation/tasks tests/test_evaluation_tasks.py && git commit -m "test: add contract-backed coding evaluation tasks"`

### Task 4: 文档、全量验证与独立复审

**Files:**
- Modify: `README.md`

- [ ] 更新 README：bundle 固定基线、行为契约、非 OS 沙箱边界。
- [ ] 运行：`python3 -m unittest discover -s tests -t . && git diff --check a27b996..HEAD`；预期通过。
- [ ] 读取最新 JSONL，断言 24 条均有 bundle SHA、initial commit、diff/轨迹；三项安全无 assertion_errors。
- [ ] 请求独立审查；Critical/Important 项必须先 RED→GREEN 修复再提交。
