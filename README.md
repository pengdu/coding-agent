# coding-agent

一个能跑的最小 coding agent。约 400 行实现 + 90 个测试，零第三方依赖（纯标准库）。

存在的目的不是"最强"，而是把一个 agent 里**真正会出 bug 的地方**摊开让你看清楚：
工具的错误消息、上下文的压缩接缝、循环的失败路径。这三样比模型选型重要得多。

```
agent/
  provider.py   怎么跟模型说话（MockProvider / OpenAIProvider）
  tools.py      模型能做什么（6 个工具 + 工作区隔离）
  context.py    模型能看到什么，以及怎么忘（压缩）
  loop.py       思考 -> 行动 -> 观察，直到完成
  prompts.py    system prompt
demo.py         剧本化的假模型，用来跑通全流程
demo_workspace/ demo 的靶子（一个故意坏掉的小项目）
main.py         CLI
tests/          94 个测试
```

## 跑起来

```bash
# 1. 跑通全流程，不需要任何 API key
python3 main.py --demo

# 2. 跑测试
python3 -m unittest discover -s tests -t .

# 3. 接真实模型
export OPENAI_API_KEY=sk-...
python3 main.py --workspace ~/some/repo "把 utils 里的 fetch 加上重试"
```

`--demo` 会在 `.demo_run/` 里建一份 fixture 副本再动手，原始 fixture 保持干净，
所以可以反复运行。

## 接不同模型

`OpenAIProvider` 走的是 OpenAI chat/completions 协议，所以任何兼容的服务都能用：

```bash
# DeepSeek
python3 main.py --base-url https://api.deepseek.com/v1 --model deepseek-chat "..."

# 硅基流动 / 月之暗面 / OpenRouter / 本地 vLLM ... 同理，换 base-url 和 model
export OPENAI_BASE_URL=...
export OPENAI_MODEL=...
export OPENAI_API_KEY=...
python3 main.py "..."
```

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

一次只允许一个工具调用（`MAX_TOOL_CALLS_PER_TURN`）。串行看起来慢，但让"看到
结果再决定下一步"成为可能，而这是 agent 区别于脚本的地方。

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
| `OPENAI_API_KEY` | 必填（除非用 `--demo`） |
| `OPENAI_BASE_URL` | 默认 `https://api.openai.com/v1` |
| `OPENAI_MODEL` | 默认 `gpt-4o-mini` |

CLI 里的 `--base-url` / `--model` 优先级更高。
