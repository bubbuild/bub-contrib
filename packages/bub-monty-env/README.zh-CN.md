# bub-monty-env

把 Bub code mode 的 `run_code` 放进 [Monty](https://github.com/pydantic/monty) 执行，取代默认的 Python 子进程。Monty 是用 Rust 写的沙箱化 Python 解释器。插件通过 Bub 的 `provide_environment` hook 生效，同时提供 mixin 和包装函数，可以把 Monty 版 `run_code` 叠加到任意其他执行环境（Docker、本地沙箱等）上。

代码每次调用沙箱外的东西，Monty 都会挂起执行，把调用交给 Bub；Bub 执行完再恢复代码。因此：

- `tools.<name>(...)` 仍然经过 Bub 的工具执行器和 hooks，与默认实现一致；`asyncio.gather` 会并发执行。
- 文件访问（`pathlib.Path.read_text` / `write_text`）走执行环境的 `read_text` / `write_text`，代码看到的文件和 `fs.*` 一致。
- 代码不能启动进程、打开 socket、读取宿主的环境变量，也不能 import 第三方包。
- 每次运行的启动开销是微秒级，不需要启动 Python 进程。

需要 Bub 0.5 或更高版本。只有 `run_code` 改变：`bash` 和 `fs.*` 仍在底层执行环境里运行。

English documentation: [README.md](./README.md)

## Python 子集

Monty 只支持 Python 的一个子集。模型写的胶水代码常用的写法都能跑：函数、闭包、推导式、f-string、`try`/`except`、简单类和 `@dataclass`、`async`/`await` 加 `asyncio.gather`，以及 `json`、`re`、`datetime`、`collections`、`itertools`、`math`、`pathlib` 的大部分功能。不支持类继承、`match`、生成器（`yield`）、`del`、方法装饰器（`@property`）和任何第三方包。代码因语法不支持而失败时，错误信息会告诉模型代码运行在 Monty 里，让它改写。详见 Monty 的 [limitations](https://pydantic.dev/docs/monty/limitations/)。

适合 code mode 代码主要是串联工具的场景；如果模型需要 `pandas`、`numpy` 等包，请保留默认实现。

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
| `sessions` | `["*"]` | 匹配 session id（`<channel>:<chat id>`）的 glob；其他会话使用 Bub 的宿主执行环境 |
| `filesystem` | `read-write` | 代码的文件访问：`read-write`、`read-only` 或 `none` |
| `max_memory_mb` | `256` | 每次运行的堆内存上限；`null` 表示不限 |
| `max_recursion_depth` | `1000` | 最大调用栈深度 |
| `max_suspensions` | `10000` | 每次运行最多的工具调用、文件访问等宿主往返次数 |
| `max_processes` | CPU 核数 | 每个会话的 Monty worker 进程数上限 |

运行时长上限由 `run_code` 自身的 `timeout_seconds` 控制。

## 与其他执行环境组合

插件提供的是 `MontyEnvironment`：Bub 的宿主执行环境加上 Monty 版 `run_code`。Bub 只采用第一个插件提供的执行环境，所以如果想让 `bash` 跑在 Docker 或本地沙箱里、`run_code` 跑在 Monty 里，需要在自己的插件中把 Monty 叠加到那个执行环境上：

```python
import bub
from bub import hookimpl
from bub_monty_env import MontyCodeMixin, MontyOptions, create_monty_environment
from bub_sandbox_env.config import SandboxEnvConfig
from bub_sandbox_env.environment import SandboxEnvironment


# 包装实例：除 run_code 外全部委托给它，关闭包装时也会关闭它。
@hookimpl
def provide_environment(session_id, workspace):
    config = bub.ensure_config(SandboxEnvConfig)
    return create_monty_environment(SandboxEnvironment(config, workspace=workspace))


# 或者混入类：mixin 放在前面。
class SandboxMontyEnvironment(MontyCodeMixin, SandboxEnvironment):
    monty_options = MontyOptions(filesystem="read-only")
```

更底层的部件：`run_code_in_monty(pool, code, ...)` 在 `pydantic_monty.AsyncMonty` 池上实现 `Environment.run_code`；可以覆盖 `MontyCodeMixin.monty_os_handler()` 来改变代码能做的文件访问。

## 限制

- 不支持 `open()`，读写文件请用 `pathlib.Path`。`Path.exists`、`iterdir`、`mkdir` 等其他文件操作不可用。
- Monty 还很新，部分行为与 CPython 不同，以官方 limitations 文档为准。
- 工具结果以 JSON 兼容的数据传入沙箱，与默认实现一致。
