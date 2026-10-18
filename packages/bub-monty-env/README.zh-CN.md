# bub-monty-env

把 Bub code mode 的 `run_code` 放进 [Monty](https://github.com/pydantic/monty) 执行，取代默认的 Python 子进程。Monty 是用 Rust 写的沙箱化 Python 解释器。插件的 `provide_environment` hook 会包装其他插件提供的执行环境（Docker、本地沙箱，或 Bub 的宿主执行环境），只替换 `run_code`。另外也提供 mixin 和包装函数，方便用在你自己的执行环境上。

代码每次调用沙箱外的东西，Monty 都会挂起执行，把调用交给 Bub；Bub 执行完再恢复代码。因此：

- `tools.<name>(...)` 仍然经过 Bub 的工具执行器和 hooks，与默认实现一致；`asyncio.gather` 会并发执行。
- 文件访问（`pathlib.Path.read_text` / `write_text`）走执行环境的 `read_text` / `write_text`，代码看到的文件和 `fs.*` 一致。
- 和默认实现一样可以用 `republic` 模块：模型调用会回到 Bub，由 Bub 用自己的凭据和配置执行。见 [`republic` 模块](#republic-模块)。
- 代码不能启动进程、打开 socket、读取宿主的环境变量，也不能 import 第三方包。
- 每次运行的启动开销是微秒级，不需要启动 Python 进程。

需要 Bub 0.5 或更高版本。只有 `run_code` 改变：`bash` 和 `fs.*` 仍在底层执行环境里运行。

English documentation: [README.md](./README.md)

## Python 子集

Monty 只支持 Python 的一个子集。模型写的胶水代码常用的写法都能跑：函数、闭包、推导式、f-string、`try`/`except`、简单类和 `@dataclass`、`async`/`await` 加 `asyncio.gather`，以及 `json`、`re`、`datetime`、`collections`、`itertools`、`math`、`pathlib` 的大部分功能。不支持类继承、`match`、生成器（`yield`）、`async with`、`async for`、`del`、方法装饰器（`@property`）和任何第三方包。代码因语法不支持而失败时，错误信息会告诉模型代码运行在 Monty 里，让它改写。详见 Monty 的 [limitations](https://pydantic.dev/docs/monty/limitations/)。

适合 code mode 代码主要是串联工具的场景；如果模型需要 `pandas`、`numpy` 等包，请保留默认实现。

## `republic` 模块

Bub 的默认实现会给 code mode 代码提供一个 `republic` 模块，用来处理媒体和调用模型；本插件在 Monty 里提供同样的模块。模型调用以 `republic.<operation>` 的形式经由 `run_code` 的 `call_tool` 交给 Bub，Republic 值编码为带 `"$republic"` 键的 JSON 对象，协议与默认实现相同，所以 Bub 侧无需改动，代码也接触不到 provider 凭据。

```python
from republic import Image, get_model, user

model = get_model()  # 当前会话使用的模型；也可以 get_model("provider:model")
chart = Image("image/png", url="https://example.com/chart.png")
reply = await model.chat([user("Describe this chart.", chart)])
print(reply.text, reply.token_usage.total_tokens)
```

可用的内容：`Image` / `Audio` / `Video`、`Message`、`system` / `user` / `assistant`、`get_model(spec=None).chat(...)`、`get_embedding_model(spec).embed(...)` / `embed_many(...)`、`get_decision_model(spec).decide(...)` 以及 `republic.decisions.Noul` / `Choice` / `Score`。`tools.*` 参数和结果里的媒体同样以 Republic 值传递。

受 Monty 限制，与默认实现有以下不同：

- `republic` 是预设的全局变量。Monty 无法 import 它没有内置的模块，所以插件会在运行前把 `import republic` 和 `from republic[.decisions] import ...` 改写成从这个全局变量赋值，行号保持不变。
- 不支持流式输出：`model.stream(...)` 需要 `async with` 和 `async for`，Monty 不支持。请用 `await model.chat(...)` 拿完整响应，使用模型时也不要写 `async with`。

## 配置

写在 `~/.bub/config.yml` 的 `monty_env:` 段，或用环境变量 `BUB_MONTY_ENV_<KEY>`（列表用 JSON：`BUB_MONTY_ENV_SESSIONS='["qq:*"]'`）。修改后需重启 Bub。

```yaml
monty_env:
  sessions: ["qq:*"]
  filesystem: read-only
  max_memory_mb: 256
```

| 键 | 默认值 | 作用 |
| --- | --- | --- |
| `sessions` | `["*"]` | 匹配 session id（`<channel>:<chat id>`）的 glob；其他会话使用其他插件提供的执行环境，不做包装 |
| `filesystem` | `read-write` | 代码的文件访问：`read-write`、`read-only` 或 `none` |
| `max_memory_mb` | `256` | 每次运行的堆内存上限；`null` 表示不限 |
| `max_recursion_depth` | `1000` | 最大调用栈深度 |
| `max_suspensions` | `10000` | 每次运行最多的工具调用、文件访问等宿主往返次数 |
| `max_processes` | CPU 核数 | 每个会话的 Monty worker 进程数上限 |

运行时长上限由 `run_code` 自身的 `timeout_seconds` 控制。

## 与其他执行环境组合

插件的 hook 最先运行（`tryfirst`），通过 pluggy 的 `subset_hook_caller` 向其他插件获取该会话的执行环境，再用 `create_monty_environment` 包装。装了 `bub-docker-env` 或 `bub-sandbox-env` 时，`bash` 和 `fs.*` 仍在 Docker 或沙箱里运行，`run_code` 跑在 Monty 里，文件访问也走那个执行环境。没有其他插件提供执行环境时，包装 Bub 的宿主执行环境。不在 `sessions` 范围内的会话直接使用其他插件提供的执行环境，不做包装。

用在你自己的执行环境上：

```python
from bub_monty_env import MontyCodeMixin, MontyOptions, create_monty_environment

# 包装实例：除 run_code 外全部委托给它，关闭包装时也会关闭它。
environment = create_monty_environment(MyEnvironment(...), MontyOptions(filesystem="read-only"))


# 或者混入类：mixin 放在前面。
class MyMontyEnvironment(MontyCodeMixin, MyEnvironment):
    monty_options = MontyOptions(filesystem="read-only")
```

`MontyEnvironment` 是 Bub 的宿主执行环境加上 Monty 版 `run_code`。

更底层的部件：`run_code_in_monty(pool, code, ...)` 在 `pydantic_monty.AsyncMonty` 池上实现 `Environment.run_code`；可以覆盖 `MontyCodeMixin.monty_os_handler()` 来改变代码能做的文件访问。

## 限制

- 不支持 `open()`，读写文件请用 `pathlib.Path`。`Path.exists`、`iterdir`、`mkdir` 等其他文件操作不可用。
- Monty 还很新，部分行为与 CPython 不同，以官方 limitations 文档为准。
- 工具结果以 JSON 兼容的数据传入沙箱，与默认实现一致。
- 不支持 `Path.read_bytes`，文件里的媒体需要从别处拿到 `data=`，或者用 `tools.attach_image(image="<path>")` 让 Bub 读取。
