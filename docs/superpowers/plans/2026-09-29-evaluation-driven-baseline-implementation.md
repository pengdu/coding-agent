# 评测驱动基线实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为当前单代理 coding agent 建立可重复执行的本地任务评测框架、24 个隔离任务 fixture 和机器可读基线报告。

**Architecture:** 新增 `evaluation/` 包，专门负责读取 JSON manifest、复制 fixture、运行 `Agent`、执行验收命令、分类结果并写入 JSONL 与汇总 JSON。评测包不修改 `Agent` 的决策逻辑；只通过已有 `Agent.run()`、`Run`、`Workspace` 和 `MockProvider` 采集运行结果。任务数据置于 `evaluation/tasks/`，每项任务用目录隔离，避免共享状态和对仓库根目录造成写入。

**Tech Stack:** Python 3 标准库、`unittest`、现有 `agent.Agent`、`agent.MockProvider`、JSON、subprocess。

**Spec:** `docs/superpowers/specs/2026-09-29-coding-agent-eval-driven-development-design.md`

## Global Constraints

- 保持标准库优先，不新增第三方依赖。
- 每个评测必须在从 fixture 新建的 workspace 副本中运行。
- 每个任务都必须定义任务说明、验收命令、最大步骤、token 预算和允许修改范围。
- 结果必须按 success、verification_failure、provider_error、tool_error、budget_exhausted、step_limit、safety_denied 分类。
- JSONL 不记录环境变量值、Authorization header 或 workspace 以外的内容。
- 评测基础设施必须可用确定性 `MockProvider` 回放；真实 profile 只作为显式选择的后续运行方式。
- 现有完整测试命令为 `python3 -m unittest discover -s tests -t .`。

## Review Focus

- fixture 路径包含 `..` 或绝对路径时必须被拒绝，不能复制到评测根目录外；由任务 1 的 manifest 校验测试覆盖。
- 验收命令超时必须归类为 `verification_failure`，且报告保留命令和超时信息；由任务 3 覆盖。
- agent 正常停止但验收命令失败时必须是 `verification_failure`，不能误记为 success；由任务 3 覆盖。
- agent 因工具错误而最终正常结束时必须保留 `tool_errors` 计数而非覆盖结果；由任务 3 覆盖。
- 一个任务运行失败不能阻止后续任务写入结果；由任务 4 覆盖。

---

## 目标文件结构

```text
evaluation/
  __init__.py                 评测包入口
  manifest.py                 manifest 数据类型、加载与校验
  runner.py                   单任务和批量评测执行、分类、报告写入
  task_provider.py            确定性 task script provider 加载器
  tasks/
    <task-id>/
      task.json               单项任务 manifest
      fixture/                初始工作区
      script.json             MockProvider 的确定性回放脚本
tests/
  test_evaluation_manifest.py manifest 的边界与校验测试
  test_evaluation_runner.py   复制、运行、验收、分类、报告测试
  test_evaluation_tasks.py    24 项任务的契约测试
main.py                       增加显式 `--eval-dir`、`--eval-output` 入口
```

### Task 1: 定义并校验任务 Manifest

**Files:**
- Create: `evaluation/__init__.py`
- Create: `evaluation/manifest.py`
- Create: `tests/test_evaluation_manifest.py`

**Interfaces:**
- Produces: `TaskManifest`, `ManifestError`, `load_manifest(path: str) -> TaskManifest`。
- Produces: `TaskManifest.fixture_dir`, `task`, `verify_command`, `max_steps`, `budget_tokens`, `allowed_paths`, `timeout_seconds`。
- Consumes: 标准库 `json`、`os`、`dataclasses`。

- [ ] **Step 1: 写 manifest 必填字段的失败测试**

```python
from evaluation.manifest import ManifestError, load_manifest


def test_missing_task_is_actionable(tmp_path):
    path = tmp_path / "task.json"
    path.write_text('{"id": "x", "fixture": "fixture"}', encoding="utf-8")

    with pytest.raises(ManifestError, match="missing required field\(s\): task"):
        load_manifest(str(path))
```

使用仓库现有 `unittest` 风格实现等价测试：临时目录写入 JSON，并断言错误消息包含缺失字段名。

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m unittest tests.test_evaluation_manifest.TestLoadManifest.test_missing_task_is_actionable -v`

Expected: FAIL，提示 `evaluation.manifest` 尚不存在。

- [ ] **Step 3: 添加最小 manifest 类型和加载器**

```python
@dataclass(frozen=True)
class TaskManifest:
    id: str
    fixture_dir: str
    task: str
    verify_command: str
    max_steps: int
    budget_tokens: int
    allowed_paths: tuple[str, ...]
    timeout_seconds: int

class ManifestError(ValueError):
    pass


def load_manifest(path: str) -> TaskManifest:
    ...
```

`load_manifest` 必须：读取 UTF-8 JSON、拒绝未知字段、逐一校验字符串/正整数/字符串数组类型；将 `fixture` 解析为相对 manifest 所在目录的路径；拒绝绝对路径和任何包含 `..` 的 fixture 值；确认 fixture 是已有目录；默认 `timeout_seconds=60`，`allowed_paths=["."]`。

- [ ] **Step 4: 补齐 fixture 越界、未知字段、零预算和成功加载的测试**

```python
cases = [
    ({"fixture": "../outside"}, "must stay under"),
    ({"fixture": "/tmp/x"}, "must be relative"),
    ({"extra": True}, "unknown field"),
    ({"max_steps": 0}, "must be a positive integer"),
]
```

成功样例断言 `fixture_dir` 是解析后的真实路径、`allowed_paths == ("src", "tests")`，并验证默认超时值。

- [ ] **Step 5: 运行 manifest 测试**

Run: `python3 -m unittest tests.test_evaluation_manifest -v`

Expected: PASS。

- [ ] **Step 6: 提交 manifest 边界**

```bash
git add evaluation/__init__.py evaluation/manifest.py tests/test_evaluation_manifest.py
git commit -m "feat: add evaluation task manifests"
```

### Task 2: 实现确定性任务脚本 Provider

**Files:**
- Create: `evaluation/task_provider.py`
- Modify: `tests/test_evaluation_manifest.py`
- Create: `tests/test_evaluation_runner.py`

**Interfaces:**
- Produces: `load_scripted_provider(path: str) -> MockProvider`。
- Script schema: `[{"text": str, "tool_calls": [{"id": str, "name": str, "arguments": object}]}]`。
- Consumes: `agent.provider.AssistantTurn`, `ToolCall`, `MockProvider`。

- [ ] **Step 1: 写 script JSON 解析测试**

```python
provider = load_scripted_provider(str(script_path))
turn = provider.complete([], [])
assert turn.text == "inspect file"
assert turn.tool_calls[0].name == "read_file"
assert turn.tool_calls[0].arguments == {"path": "src/math.py"}
```

另写失败用例：根对象不是数组、tool call 缺失 `name`、`arguments` 不是对象、重复 call id。

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m unittest tests.test_evaluation_runner.TestTaskProvider -v`

Expected: FAIL，提示 `evaluation.task_provider` 尚不存在。

- [ ] **Step 3: 实现严格脚本加载器**

```python
def load_scripted_provider(path: str) -> MockProvider:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    turns: list[AssistantTurn] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw, 1):
        ...
    return MockProvider(turns, name=f"eval:{Path(path).parent.name}")
```

错误使用 `ManifestError`，消息必须包含 `script.json`、turn 序号和字段名。无 tool call 的最后一轮可只包含 `text`；其他字段一律拒绝，避免“脚本看似生效但字段拼错”。

- [ ] **Step 4: 运行 task provider 测试**

Run: `python3 -m unittest tests.test_evaluation_runner.TestTaskProvider -v`

Expected: PASS。

- [ ] **Step 5: 提交确定性 Provider**

```bash
git add evaluation/task_provider.py tests/test_evaluation_manifest.py tests/test_evaluation_runner.py
git commit -m "feat: load deterministic evaluation scripts"
```

### Task 3: 实现单任务执行、验收和分类

**Files:**
- Create: `evaluation/runner.py`
- Modify: `tests/test_evaluation_runner.py`

**Interfaces:**
- Produces: `EvaluationResult`, `run_task(manifest_path: str, output_root: str) -> EvaluationResult`。
- Produces: `classify_run(run: Run, verify_returncode: int | None, verification_timed_out: bool) -> str`。
- `EvaluationResult` JSON 字段：`task_id`, `outcome`, `run`, `verification`, `workspace`, `started_at`, `elapsed_seconds`。
- Consumes: `TaskManifest`, `load_scripted_provider`, `Agent`, `Workspace`, `subprocess.run`。

- [ ] **Step 1: 写通过路径的端到端失败测试**

```python
result = run_task(manifest_path, output_root)
self.assertEqual(result.outcome, "success")
self.assertEqual(result.run["stop_reason"], "done")
self.assertEqual(result.verification["returncode"], 0)
self.assertTrue(os.path.exists(os.path.join(result.workspace, "answer.txt")))
```

fixture 放一个 `answer.txt` 初始为 `old`，script 依次调用 `edit_file` 修改为 `new`、`run_command` 验证、最终输出文本，manifest 的验收命令为 `python3 -c "assert open('answer.txt').read() == 'new'"`。

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m unittest tests.test_evaluation_runner.TestRunTask.test_successful_scripted_task -v`

Expected: FAIL，提示 `run_task` 尚不存在。

- [ ] **Step 3: 实现受隔离保护的单任务执行器**

```python
@dataclass(frozen=True)
class EvaluationResult:
    task_id: str
    outcome: str
    run: dict[str, object]
    verification: dict[str, object]
    workspace: str
    started_at: str
    elapsed_seconds: float


def run_task(manifest_path: str, output_root: str) -> EvaluationResult:
    manifest = load_manifest(manifest_path)
    workspace = _copy_fixture(manifest, output_root)
    agent = Agent(load_scripted_provider(_script_path(manifest_path)), Workspace(workspace),
                  max_steps=manifest.max_steps, budget_tokens=manifest.budget_tokens)
    run = agent.run(manifest.task)
    verification = _verify(manifest.verify_command, workspace, manifest.timeout_seconds)
    return _build_result(manifest, workspace, run, verification)
```

`_copy_fixture` 目标固定为 `<output_root>/workspaces/<task-id>`；若目标已存在则先删除该目标本身，随后 `copytree`。验证必须以 `shell=True`、`cwd=workspace`、`capture_output=True` 运行，stdout/stderr 经现有 `truncate_middle` 截断。`allowed_paths` 以最终 Git diff 的相对路径校验；git 不可用、或变更越界时将验证标记为失败并写入原因。

`classify_run` 优先级固定为：`provider_error`（`stop_reason` 前缀）、`step_limit`、`budget_exhausted`、`safety_denied`、`verification_failure`、`tool_error`、`success`。`tool_error` 仅在 agent 正常结束、验收通过、但 `tool_errors > 0` 时返回，以避免掩盖验收失败。

- [ ] **Step 4: 增加失败模式测试**

```python
self.assertEqual(classify_run(run_with_provider_error, 0, False), "provider_error")
self.assertEqual(classify_run(done_run_with_tool_error, 0, False), "tool_error")
self.assertEqual(classify_run(done_run, 1, False), "verification_failure")
self.assertEqual(classify_run(done_run, None, True), "verification_failure")
```

补充：fixture 原目录内容未变化；最大步骤耗尽归类为 `step_limit`；验证 stdout/stderr 超长时仍含末尾错误文本。

- [ ] **Step 5: 运行 runner 测试**

Run: `python3 -m unittest tests.test_evaluation_runner -v`

Expected: PASS。

- [ ] **Step 6: 提交单任务评测器**

```bash
git add evaluation/runner.py tests/test_evaluation_runner.py
git commit -m "feat: run isolated coding task evaluations"
```

### Task 4: 实现批量报告与 CLI 入口

**Files:**
- Modify: `evaluation/runner.py`
- Modify: `main.py:74-155`
- Modify: `tests/test_evaluation_runner.py`
- Modify: `tests/test_main.py`

**Interfaces:**
- Produces: `run_suite(tasks_root: str, output_root: str) -> list[EvaluationResult]`。
- Produces: `write_reports(results: list[EvaluationResult], output_root: str) -> tuple[str, str]`。
- CLI: `python3 main.py --eval-dir evaluation/tasks --eval-output .eval_runs`。

- [ ] **Step 1: 写批量运行不会中断的失败测试**

```python
results = run_suite(tasks_root, output_root)
self.assertEqual([r.task_id for r in results], ["bad", "good"])
self.assertEqual(results[0].outcome, "verification_failure")
self.assertEqual(results[1].outcome, "success")
```

使用一个验收失败任务与一个成功任务，验证按 task id 排序，失败不阻止后续任务。

- [ ] **Step 2: 写 JSONL 和 summary 契约测试**

```python
jsonl_path, summary_path = write_reports(results, output_root)
lines = Path(jsonl_path).read_text(encoding="utf-8").splitlines()
summary = json.loads(Path(summary_path).read_text(encoding="utf-8"))
self.assertEqual(len(lines), 2)
self.assertEqual(summary["total"], 2)
self.assertEqual(summary["outcomes"], {"success": 1, "verification_failure": 1})
self.assertNotIn("SECRET_VALUE", Path(jsonl_path).read_text(encoding="utf-8"))
```

- [ ] **Step 3: 运行测试确认失败**

Run: `python3 -m unittest tests.test_evaluation_runner.TestSuiteReporting -v`

Expected: FAIL，提示 `run_suite` 或 `write_reports` 尚不存在。

- [ ] **Step 4: 实现批量发现、报告和 CLI 参数**

```python
def run_suite(tasks_root: str, output_root: str) -> list[EvaluationResult]:
    manifests = sorted(Path(tasks_root).glob("*/task.json"))
    return [_run_task_catching_failures(str(path), output_root) for path in manifests]

p.add_argument("--eval-dir", metavar="DIR", help="run deterministic evaluation tasks from DIR")
p.add_argument("--eval-output", metavar="DIR", default=".eval_runs", help="write evaluation reports and workspaces here")
```

`_run_task_catching_failures` 把 manifest/fixture/script 解析异常也序列化为一条 `verification_failure`，并继续执行其他任务。`write_reports` 写 `<output_root>/results.jsonl` 和 `<output_root>/summary.json`，报告目录用 UTC 时间戳子目录防止覆盖历史。CLI 指定 `--eval-dir` 时不得初始化真实 provider，返回码仅在所有任务 `success` 时为 0；打印任务数、成功数、结果路径。

- [ ] **Step 5: 补 CLI 测试**

```python
code = cli.main(["--eval-dir", tasks_dir, "--eval-output", output_dir])
self.assertEqual(code, 0)
self.assertTrue(os.path.exists(os.path.join(output_dir, "summary.json")))
```

另测无 manifest 时返回 2 并说明目录；`--eval-dir` 与 `--demo` 同时指定时由 argparse 报互斥错误。

- [ ] **Step 6: 运行评测和 CLI 测试**

Run: `python3 -m unittest tests.test_evaluation_runner tests.test_main -v`

Expected: PASS。

- [ ] **Step 7: 提交批量报告入口**

```bash
git add evaluation/runner.py main.py tests/test_evaluation_runner.py tests/test_main.py
git commit -m "feat: add evaluation suite reports"
```

### Task 5: 建立 24 个基线任务目录

**Files:**
- Create: `evaluation/tasks/*/task.json`
- Create: `evaluation/tasks/*/fixture/**`
- Create: `evaluation/tasks/*/script.json`
- Create: `tests/test_evaluation_tasks.py`
- Modify: `README.md:166-180`

**Interfaces:**
- Consumes: `load_manifest`, `load_scripted_provider`, `run_suite`。
- Produces: 24 个可回放任务，按 `bugfix`、`cross_file`、`test_change`、`refactor`、`cli`、`recovery`、`safety`、`compaction` 八类各 3 个。

- [ ] **Step 1: 写任务目录契约失败测试**

```python
TASKS_ROOT = Path(ROOT, "evaluation", "tasks")


def test_exactly_twenty_four_task_manifests_exist(self):
    manifests = sorted(TASKS_ROOT.glob("*/task.json"))
    self.assertEqual(len(manifests), 24)
    self.assertEqual({load_manifest(str(path)).id for path in manifests},
                     {f"{kind}-{n:02d}" for kind in KINDS for n in range(1, 4)})
```

其中 `KINDS = ("bugfix", "cross-file", "test-change", "refactor", "cli", "recovery", "safety", "compaction")`。

- [ ] **Step 2: 运行任务目录测试确认失败**

Run: `python3 -m unittest tests.test_evaluation_tasks.TestTaskCatalog.test_exactly_twenty_four_task_manifests_exist -v`

Expected: FAIL，当前尚无任务目录。

- [ ] **Step 3: 添加 24 个小型、独立、确定性任务**

每项任务严格遵循以下 manifest 形状：

```json
{
  "id": "bugfix-01",
  "fixture": "fixture",
  "task": "修复 add 函数对负数输入的错误结果，并运行测试验证。",
  "verify_command": "python3 -m unittest discover -q",
  "max_steps": 8,
  "budget_tokens": 8000,
  "allowed_paths": ["src", "tests"],
  "timeout_seconds": 20
}
```

任务类别和最低覆盖点：

| 类别 | 3 个任务的差异 | 必须验证 |
| --- | --- | --- |
| `bugfix` | 边界值、错误分支、返回值 | 修改后 unittest 通过 |
| `cross-file` | 函数重命名、调用点同步、导出同步 | 所有调用点可运行 |
| `test-change` | 添加回归测试、修正断言、补 fixture | 新测试可捕获原缺陷 |
| `refactor` | 提取 helper、消除重复、重命名局部 API | 行为和测试不变 |
| `cli` | 参数默认值、错误消息、退出码 | 子进程断言 stdout/stderr 和 code |
| `recovery` | 首次 edit miss、命令失败后修复、未知文件 | script 必须出现并恢复一次错误 |
| `safety` | 路径越界、危险命令、symlink 越界 | 无 fixture 外修改且 agent 完成安全报告 |
| `compaction` | 大文件读取、重复读取、连续工具轮 | `budget_tokens` 足够低以触发至少一次压缩 |

每个 fixture 限制在 1–5 个小文件；每个 `script.json` 最后一轮必须无 tool call；安全任务的验收命令检查哨兵文件仍存在且 fixture 外无新文件。

- [ ] **Step 4: 编写每项任务的元数据和回放测试**

```python
for manifest_path in manifests:
    with self.subTest(manifest=manifest_path.parent.name):
        manifest = load_manifest(str(manifest_path))
        provider = load_scripted_provider(str(manifest_path.parent / "script.json"))
        self.assertGreater(provider.turn_count, 0)
        self.assertTrue((manifest_path.parent / "fixture").is_dir())
        self.assertTrue(manifest.verify_command)
```

加一项完整 suite 测试，断言 24 个确定性任务全部完成；失败信息必须附带 `task_id`、`outcome` 和 workspace。

- [ ] **Step 5: 运行任务目录与完整评测**

Run: `python3 -m unittest tests.test_evaluation_tasks -v && python3 main.py --eval-dir evaluation/tasks --eval-output .eval_runs/baseline`

Expected: 24 个任务全部 `success`，生成 `results.jsonl` 和 `summary.json`。

- [ ] **Step 6: 在 README 增加评测运行方式与基线解释**

添加以下命令及说明：

```bash
python3 main.py --eval-dir evaluation/tasks --eval-output .eval_runs/baseline
```

说明该模式用确定性脚本检查评测框架与工具闭环，不代表真实模型能力；真实模型评测必须在后续单独实现显式 `--eval-real` 和固定 profile 记录后运行。

- [ ] **Step 7: 运行全量回归**

Run: `python3 -m unittest discover -s tests -t .`

Expected: PASS，且无网络请求。

- [ ] **Step 8: 提交任务集基线**

```bash
git add evaluation/tasks tests/test_evaluation_tasks.py README.md
git commit -m "test: add coding agent evaluation baseline"
```

## 实施后验证

- [ ] 确认 `python3 main.py --eval-dir evaluation/tasks --eval-output .eval_runs/baseline` 退出 0。
- [ ] 读取 `.eval_runs/baseline/summary.json`，确认 `total == 24`、`outcomes.success == 24`。
- [ ] 确认 `results.jsonl` 每行包含 task id、分类、运行统计、验证结果且不含密钥。
- [ ] 运行 `python3 -m unittest discover -s tests -t .`。
- [ ] 使用 `git status --short` 确认仅存在预期报告目录（应被 `.gitignore` 忽略）或无未提交改动。

## 后续边界

本计划只交付阶段 A 的确定性基线。真实模型 profile 执行、undo、trace、去重、流式输出、策略化安全规则、多角色、项目记忆和并行探索均属于阶段 B/C，必须以此基线报告的失败分类为输入另立计划。
