# coding-agent

一个能跑的最小 coding agent。`agent/` 约 1500 行实现 + 169 个测试，零第三方依赖（纯标准库）。

存在的目的不是"最强"，而是把一个 agent 里**真正会出 bug 的地方**摊开让你看清楚：
工具的错误消息、上下文的压缩接缝、循环的失败路径。这三样比模型选型重要得多。

```
agent/
  config.py     用 JSON 描述有哪些 model/provider（profile）
  provider.py   怎么跟模型说话（MockProvider / OpenAIProvider）
  tools.py      模型能做什么（6 个工具 + 工作区隔离）
  context.py    模型能看到什么，以及怎么忘（压缩）
  loop.py       思考 -> 行动 -> 观察，直到完成
  prompts.py    system prompt
demo.py         剧本化的假模型，用来跑通全流程
demo_workspace/ demo 的靶子（一个故意坏掉的小项目）
.agent.json     model/provider 配置文件（不含 key）
main.py         CLI
tests/          169 个测试
```

## 跑起来

```bash
# 1. 跑通全流程，不需要任何 API key
python3 main.py --demo

# 2. 跑测试
python3 -m unittest discover -s tests -t .

# 3. 接真实模型（key 从环境变量读，不要写进命令行）
export DEEPSEEK_API_KEY=sk-...     # 导出即可，.agent.json 里的 profile 会自动认出来
python3 main.py --workspace ~/some/repo "把 utils 里的 fetch 加上重试"
```

`--demo` 会在 `.demo_run/` 里建一份 fixture 副本再动手，原始 fixture 保持干净，
所以可以反复运行。

## 接不同模型

`OpenAIProvider` 走的是 OpenAI chat/completions 协议，所以任何兼容的服务都能用。
endpoint、model、key 放在哪，三样都写在 JSON 配置文件里（`.agent.json`）：

```json
{
  "default": "deepseek",
  "profiles": {
    "deepseek": {
      "base_url": "https://api.deepseek.com",
      "model": "deepseek-chat",
      "api_key_env": "DEEPSEEK_API_KEY"
    },
    "local": {
      "base_url": "http://localhost:8000/v1",
      "model": "qwen-max",
      "api_key_env": null
    }
  }
}
```

**key 不写进这个文件**，profile 只写「去哪个环境变量读」。这个文件是要提交的，
key 写进去就是等到有人 push 之后才暴露的泄漏。`api_key_env: null` 表示这个 endpoint
不要认证（本地 vLLM 的常见情况），此时连 `Authorization` 头都不会发。

```bash
export DEEPSEEK_API_KEY=sk-...
python3 main.py --workspace ~/some/repo "..."              # 用 default

python3 main.py --list-profiles                           # 有哪些 profile、key 齐不齐
python3 main.py --profile local --workspace ~/some/repo "..."
```

`--config` 可以指定别的文件；不给就在当前目录和各级父目录里找 `.agent.json` /
`agent.json`，都没有就用内置的 `openai` + `deepseek` 两套。导了哪个 key 就自动选
对应的 profile，所以多数时候一个 flag 都不用给。

换模型只改 `--model`，base url 留在 profile 里：

```bash
python3 main.py --model deepseek-reasoner "..."           # 换模型
```

配置写错了会直接告诉你哪个 profile 的哪个字段不对，然后 `exit 2`——不会等到发请求
才变成一个 401：

```
$ python3 main.py --config broken.json "..."
config problem: broken.json: profile 'p' has unknown field(s) ['base_uri'].
Known: ['base_url', 'model', 'api_key_env', 'timeout']
```

profile 只管 endpoint。`max_steps`、`budget_tokens`、workspace 仍然是命令行 flag，
这样每个设置只有一个地方，不会出现「两处都写了，听谁的」。

两个实测过的坑，DeepSeek 上都能碰到：

- **模型会无视 `parallel_tool_calls: false`，一轮吐多个 tool call。** 循环不丢任何一个
  （丢一个就会留下一条有 `tool_calls` 却没有 tool 结果的 assistant 消息，provider 直接
  400 拒掉整个请求），所以那个常量是"请求"而不是"限制"。
- **输出被 `max_tokens` 截断时（`finish_reason: length`），模型以为自己说完了。**
  循环会在文本后面追加一句说明，否则 agent 看起来只是"话少"。

`deepseek-reasoner` 走 tool call 时 `content` 是空串，只有 `reasoning_content`——
provider 主动丢掉后者（回传会报错），所以它的每一步文字输出都是空的，属正常。

要接 Anthropic 这类原生协议，写一个 adapter 就行——内部统一用 OpenAI 格式，
所以只是两个纯函数：`to_native(messages, tools)` 和 `from_native(response)`。
`provider.py` 末尾的 `translate_notes` 记了两个容易踩的坑。

## 循环

```
while 还有步数:
    turn = 模型(上下文, 工具清单)      # 决策
    if 没有工具调用: 结束              # 完成了
    for 每个工具调用:                  # 行动
        结果 = 执行(调用)
        原样回灌到上下文               # 观察
    超预算就压缩上下文
```

循环只保证三件事，不多也不少：

1. **模型异常会暴露**，不会被吞掉。`stop_reason` 里带着原始错误和 traceback。
2. **工具异常会变成"工具结果"回灌**，模型有一次机会自我纠正。这就是全部的
   错误恢复机制——再往后是模型该报告的问题，不是循环该粉饰的。
3. **步数和工具调用数有硬上限**，所以模型转晕了不会把你的钱烧光。

串行：请求里带 `parallel_tool_calls: false`，一次只要一个工具调用。串行看起来慢，
但让"看到结果再决定下一步"成为可能，而这是 agent 区别于脚本的地方。

模型不一定会听话。真遇上批量调用，循环会把每一个都执行掉——不是纵容，是必须：
只回灌一部分会让对话记录本身变成非法格式。

## 几个刻意的设计决定

**工具的错误消息就是产品。** 模型改不对，通常不是它笨，是你没告诉它错在哪。
`edit_file` 匹配失败时，小文件直接把整个文件带行号打回去——这样模型能照抄，
不用再猜一次缩进：

```
old_string not found in a.py (searched 32 chars).

Here is the whole file (2 lines). Copy from it exactly, including indentation:
     1	def add(a, b):
     2	    return a + b
```

**截断要保留有信息的那一端。** 命令输出在**尾部**才有错误，所以 `run_command`
保留头尾；文件开头才是重点，所以 `read_file` 保留头部。搞反了是真实且常见的 bug。

**压缩不切断 assistant/tool 配对。** 工具结果如果丢了发起它的 assistant 消息就
成了孤儿，多数 provider 会直接拒绝整个请求。压缩时修剪的是**保留窗口的头部**，
不是中部（`context.py` 里有这个 bug 的注释和对应的参数化测试）。

**压缩是抽取式的，不调模型。** 保留 system + 原始任务 + 最近 N 轮 + 中间一段
"谁调了什么工具、作用于哪个文件、结果首行"的结构化摘要。代价是保真度，收益是
确定性、零额外开销、不可能幻觉。想要真正的理解就换成模型摘要，接口不变。

**危险操作收敛但不是沙箱。** `run_command` 拦一小撮明显不可恢复的模式
（`rm -rf /`、`dd of=/dev/`、`git push --force`…），并对解析后的路径做工作区
隔离（含 symlink）。真正的隔离是操作系统给的，不是这里。

## 从这里往哪走

按收益排序，不按新鲜度排序：

- **给 `edit_file` 加 undo 栈。** 现在改错了只能靠模型自己看出来，加个
  `.agent_undo` 日志能救回大部分情况。
- **工具结果去重。** 长任务里同一个 8KB 的测试输出会被反复回灌，白烧 token。
- **接真实模型后先看失败模式。** 跑 10 个真实任务，记录每一步为什么选错工具。
  大概率不是模型问题，是工具描述问题。
- **评测集。** 现在只有单测（保证机制正确），没有任务集（保证效果）。这是从
  "能用"到"可交付"的距离。
- **流式输出。** 现在要等一整轮才有输出，长任务体感很差。

**不要**先加 RAG、多 agent、记忆库。闭环还不稳的时候加这些，复杂度爆炸的速度
比 bug 修复快得多。

## 环境变量

| 变量 | 说明 |
|---|---|
| `DEEPSEEK_API_KEY` | 内置 profile 用；导了就自动选 deepseek |
| `OPENAI_API_KEY` | 内置 profile 用；导了就自动选 openai |

自己写 profile 的话，变量名由 `api_key_env` 决定，可以是任意名字。

优先级：`--model/--base-url` > profile > key 推断。`--demo` 不需要任何 key。

key 只从环境变量读——命令行参数会进 shell history 和 `ps` 输出，配置文件会进 git。
