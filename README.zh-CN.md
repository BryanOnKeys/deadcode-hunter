# deadcode-hunter 死代码猎人

不止找出死代码，而是**证明"删掉它是安全的"**。

大多数死代码检测器（比如 vulture）只做"名字有没有出现过"的文本级匹配，跨文件、实例方法、装饰器一多就开始误报或漏报。deadcode-hunter 做的是**跨文件的引用解析**：它理解 `import` 链、`self` 方法调用、`obj = Klass()` 之后的方法调用，然后给每条候选打出**置信度**——只有"删掉它一定安全"的才标为 high。

## 安装

```bash
pip install deadcode-hunter
# 或从源码
pip install -e ".[dev]"
```

需要 Python >= 3.10。零重型依赖（CLI 仅用 typer + rich），分析引擎只用标准库 `ast`，**不联网、不调用 LLM，纯确定性**。

## 用法

```bash
# 扫描一个目录，输出 rich 表格
deadcode-hunter scan ./src

# 只看"可放心删除"的
deadcode-hunter scan ./src --min-confidence high

# 机器可读，供 CI / 脚本消费
deadcode-hunter scan ./src --format json --min-confidence high
```

### prove：删除安全证明

`scan` 找到候选之后，`prove` 对每条候选做**动态验证**：删掉它、跑你的测试套件，绿了才算"证明安全"。

```bash
# 对 high 置信度候选逐个验证（默认测试命令：python -m pytest -x -q）
deadcode-hunter prove ./src

# 只验证 medium 以上的候选
deadcode-hunter prove ./src --min-confidence medium

# 指定测试命令（比如只跑相关子集，快得多）
deadcode-hunter prove ./src --test-cmd "pytest tests/test_utils.py -q"

# 机器可读
deadcode-hunter prove ./src --format json
```

原理（一句话）：**用 git worktree 隔离 + 测试套件做删除安全证明**——每次删除都发生在 HEAD 的干净检出里，你的工作区绝不会被碰；删完跑测试，通过→"已证明安全"（proven），失败→"删除会破坏测试"（breaks）并附失败摘要，删不干净的复杂 case→跳过（skipped）并说明原因。

删除是 AST 级的：按 `lineno`/`end_lineno` 精确删（含装饰器行一并删）；`from x import a, b` 只摘掉死掉的那个名字；删掉某个方法导致类体被掏空时会自动补一个 `pass` 再验证——跟人手工删的写法一致。

性能提示：每个候选跑一次完整测试套件，大仓库会很慢。建议先在小仓库上用，或用 `--test-cmd` 指定测试子集（如 `pytest tests/test_x.py -q`）。

信任边界：`prove` 信任你的测试套件——它能证明的只是"删掉之后测试还是绿的"。测试覆盖不到的行为，它也看不见；库的 public API 在内部无引用时会被标 high，删之前请确认没有外部调用者。

示例输出：

```
┏━━━━━━━━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ File     ┃ Symbol          ┃ Kind     ┃ Confidence ┃ Evidence                 ┃
┡━━━━━━━━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ utils.py │ utils.retry     │ function │ high       │ defined at utils.py:12;  │
│          │                 │          │            │ no references found in   │
│          │                 │          │            │ the scanned codebase     │
│ api.py   │ api.legacy_sync │ method   │ medium     │ decorated with           │
│          │                 │          │            │ @app.route; imported but │
│          │                 │          │            │ never used               │
└──────────┴─────────────────┴──────────┴──────────┴──────────────────────────┘
```

### report：可视化 HTML 报告

把扫描结果做成**单个自包含的 HTML 文件**：零外部依赖（CSS/JS 全内联，不引用 CDN），双击就能在浏览器打开。

```bash
# 生成报告（默认输出 report.html）
deadcode-hunter report ./src

# 指定输出路径
deadcode-hunter report ./src --output /tmp/deadcode.html

# 把 prove 的结果合并进报告（proven / breaks 徽章 + 通过数汇总）
deadcode-hunter prove ./src --format json > prove.json
deadcode-hunter report ./src --prove-json prove.json
```

报告内容：

- 顶部汇总：候选总数、置信度分布（high/medium/low）、按文件分布、prove 通过数（如果合并了 prove 结果）
- 左侧文件树（点击按文件过滤），右侧符号列表：符号名、类型、置信度徽章（颜色区分）、证据链
- 点击符号展开源码片段，死代码行高亮
- 搜索框：按符号名过滤
- 深色模式：跟随系统 `prefers-color-scheme`

> 截图待补充

### 置信度三档

| 置信度 | 含义 | 例子 |
|--------|------|------|
| **high**（可删） | 全代码库零引用、无任何"活着"的理由 | 从未被 import、从未被调用的函数 |
| **medium**（看一眼） | 有弱引用证据，或被装饰器装饰 | `@app.route` 注册的视图、`obj.attr` 这种解析不了的访问 |
| **low**（疑似活着） | 所在作用域用了 `getattr`/`eval`/`__import__` | 插件式动态加载的代码 |

每条候选都带**证据链**：定义位置、引用次数、为什么判死/疑似。

### 它能正确处理

- `__all__` 导出的符号不报（那是 public API）
- `if __name__ == "__main__"` 块里的定义不报
- dunder 方法（`__str__` 等）、`@abstractmethod` 不报
- 被装饰器装饰的函数 → 降为 medium（装饰器可能是框架注册）
- `getattr` / `eval` / `__import__` / 字符串拼接 → 相关作用域降为 low
- 测试文件（`test_*.py`、`tests/` 目录）正常分析，但 `test_*` 函数和 `@pytest.fixture` 不报，并标注来源
- 跨文件：`from pkg.mod import foo`、`import pkg.mod` + `pkg.mod.foo()` 都能链起来
- 实例方法：`self._helper()`、`worker = Worker(); worker.run()`、`Worker().run()` 都能解析
- 参数注解当类型用：`def f(x: Foo)` 里的 `x.bar()` 能定位到 `Foo.bar`
- 尊重 `.gitignore`，跳过 `.venv` / `__pycache__` / `.git`

## 与 vulture 的差异

| | vulture | deadcode-hunter |
|---|---|---|
| 引用分析 | 单文件内名字匹配 | **跨文件** import 链 + 引用解析 |
| 实例方法 | 基本不支持（`self._helper` 常误报） | `self` / 变量类型 / 构造器链式调用可解析 |
| 输出 | 死代码列表 | 死代码列表 + **置信度分级 + 证据链** |
| 目标 | 找出"可能没用"的代码 | **证明"删掉安全"**：high = 零引用零借口 |
| 动态代码 | 容易误判 | `getattr`/`eval` 作用域自动降为 low |
| 确定性 | 确定性 | 确定性（无 LLM、无网络） |

简单说：vulture 回答"这段代码可能没用"；deadcode-hunter 回答"这段代码删掉是安全的，这是证据"。

## Roadmap

- **M1 跨文件扫描**（已实现）：跨文件引用解析，high/medium/low 三档置信度 + 证据链。
- **M2 --prove**（已实现）：`deadcode-hunter prove`——git worktree 隔离 + 测试套件，对 high 置信度候选做"删除验证"，把"证明安全"从静态做到动态。
- **M3 HTML 报告**（已实现）：`deadcode-hunter report`——单个自包含 HTML 文件（零外部依赖），汇总统计、可点击文件树、证据链、源码片段展开 + 死代码行高亮、符号搜索、深色模式；可用 `--prove-json` 把 prove 结果合并进来。
- **TypeScript 支持**：把同一套"跨文件引用 + 置信度"模型搬到 TS（基于 tsc 的符号解析）。

## 开发

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -q
```

测试包含 fixture 样例仓库（覆盖各种死/活代码 case）和 dogfood（在项目自身上扫描，断言零 high 误报）。

## 已知局限（M1）

- 只分析**被扫描的代码库内部**：库的 public API（如 `typer.colors.RED`）在库内部无引用会被报 high——这是诚实的"内部无引用"，删之前请确认没有外部调用者。M2 的 `--prove` 会缓解这一问题。
- 无完整类型推导：`x.attr` 中 x 是无注解局部变量/循环变量时，只能记为弱引用（medium），这是保守选择。
- 不执行代码：`__getattr__`、元类魔法、 C 扩展里的调用看不见。

MIT License.
