# deadcode-hunter 死代码猎人

不止找出死代码，而是**证明"删掉它是安全的"**。

大多数死代码检测器（比如 vulture）只做"名字有没有出现过"的文本级匹配，跨文件、实例方法、装饰器一多就开始误报或漏报。deadcode-hunter 做的是**跨文件的引用解析**：它理解 `import` 链、`self` 方法调用、`obj = Klass()` 之后的方法调用，然后给每条候选打出**置信度**——只有"删掉它一定安全"的才标为 high。

## 安装

```bash
pip install deadcode-hunter
# 加 Java 支持（基于 tree-sitter 的扫描器）
pip install "deadcode-hunter[java]"
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

### clean：一键清理

`prove` 证明删掉是安全的，`clean` 则把这一步做完：扫描 → 在隔离的 git worktree 里逐个验证 → 把已证明安全的删除应用到**工作区**。改动不提交，`git diff` 看过再自己 commit。

```bash
$ deadcode-hunter clean . --dry-run
 deadcode-hunter clean: 3 proven-safe deletion(s) in .
┏━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┓
┃ File       ┃ Symbol                      ┃ Kind     ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━┩
│ billing.py │ billing.legacy_discount     │ function │
│ billing.py │ billing.LegacyReport        │ class    │
│ billing.py │ billing.LegacyReport.render │ method   │
└────────────┴─────────────────────────────┴──────────┘
Dry run: no files were changed.
```

安全护栏：目标必须是 git 仓库且工作区干净（有未提交改动就拒绝，先 commit 或 stash）；删除复用 `prove` 的 AST 级删除逻辑；默认先打印清单让你确认，`--yes` 跳过确认。

```bash
deadcode-hunter clean . --yes            # 不确认直接应用
deadcode-hunter clean . -i               # 逐条挑选（y/n/a/q），带源码片段
deadcode-hunter clean . --format json    # 机器可读的计划 + 结果
```

`--interactive`（`-i`）在验证之后、应用之前，让你对每个已证明安全的候选逐条决定：`y` 删除、`n` 跳过、`a` 删掉本条及剩余全部、`q` 退出（什么都不改）。需要终端——脚本和 CI 里用 `--yes`。

### Baseline：只看新增的死代码

一次性大扫除很好，但"不再新增死代码"更好。存一份基线并提交，CI 里卡住它：

```bash
$ deadcode-hunter scan . --save-baseline baseline.json --min-confidence high
Baseline saved to baseline.json (3 candidates)
```

```bash
$ deadcode-hunter scan . --baseline baseline.json --min-confidence high
# 只报告基线里没有的新增候选
```

`--fail-on-new`：有（新增）候选就 exit 1，没有就 exit 0——直接当 CI 门禁：

```bash
deadcode-hunter scan src --min-confidence high --fail-on-new
```

### 配置文件

在**被扫项目**自己的 `pyproject.toml` 里写默认配置（只读扫描目标根目录的，不向上递归）：

```toml
[tool.deadcode-hunter]
exclude = ["tests/fixtures/**", "docs/**"]  # glob，匹配扫描根目录相对路径
entry-points = ["mypackage.api.*"]          # glob，匹配符号 qualname：命中则视为活着
min-confidence = "high"                     # scan / prove / clean 的默认值
test-cmd = "pytest -q"                      # prove / clean 的默认测试命令
```

CLI 显式传参永远覆盖配置文件。

### deps：找出没人用的依赖

换个维度的死代码：`requirements.txt` / `[project] dependencies` 里声明了、但代码里从没 import 过的包。import 名到发行包名的映射靠内置表（`yaml` → `PyYAML`、`PIL` → `pillow`、`sklearn` → `scikit-learn` 等著名错位）+ 大小写/`-`/`_` 归一化启发式。

```bash
$ deadcode-hunter deps .
        deadcode-hunter deps: unused dependencies in .
┏━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━┓
┃ Package         ┃ Declared in      ┃ Requirement          ┃
┡━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━┩
│ some-unused-pkg │ requirements.txt │ some-unused-pkg==1.0 │
└─────────────────┴──────────────────┴──────────────────────┘
```

诚实规则：项目里用了动态导入（`__import__`、`getattr`……）时，"没用过"的结论不可信——这些会进单独的 **undetermined** 列表，而不是被判死刑。`-r` 嵌套和 URL 依赖会标注为跳过，不会崩。

### duplicates：找出复制粘贴的代码

死代码的表亲。按 token 归一化每一行（标识符 → `ID`、字符串/数字 → `LIT`，关键字保留），滑窗哈希找重复块——改了变量名也照样能匹配：

```bash
$ deadcode-hunter duplicates .
Group 1: 11 lines × 2 occurrence(s), 56 tokens
┏━━━━━━┳━━━━━━━┓
┃ File ┃ Lines ┃
┡━━━━━━╇━━━━━━━┩
│ a.py │ 1-11  │
│ b.py │ 1-11  │
└──────┴───────┘
```

```bash
deadcode-hunter duplicates . --min-lines 6 --min-tokens 50  # 默认值
```

同一文件内和跨文件的重复都报，按组展示。v1 只做 Python（TypeScript/Java 在 roadmap）。

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

## Java 支持

Java 引擎基于 [tree-sitter](https://tree-sitter.github.io/)，走**同一套管线**——同样的置信度分级、证据链、`prove`、HTML 报告。按文件扩展名自动识别语言（`.py` → Python，`.java` → Java），混有两种文件的目录都会扫；可用 `--lang` 覆盖：

```bash
deadcode-hunter scan ./my-java-project
deadcode-hunter scan ./mixed --lang java   # 只扫 .java
deadcode-hunter scan ./mixed --lang python # 只扫 .py
```

Java 特有的规则：

- `main` 方法（含它的类）是程序入口——永不报告
- `@Override` 方法视为"生来就活着"（它和父类/接口绑死了）——不报告
- 其他注解（如 `@Test`）降为 medium，和 Python 的装饰器一个待遇
- `private` 成员无引用 → high；`public` 成员无引用 → high，但证据链会注明"可能是对外 API"
- 反射字符串（`Class.forName("com.foo.Bar")`、`getMethod("name")`）会把目标降为 medium 而不是 high
- 未使用的 import → high；`import pkg.*` 永不报告（它到底提供了哪些名字无从得知，报了就是误报）
- 构造器会被跟踪：`new Foo()` 会同时保住类和构造器

Java 项目用 `prove` 时，用 `--test-cmd` 指定自己的构建命令：

```bash
deadcode-hunter prove ./my-java-project --test-cmd "mvn -q test"
# 或：deadcode-hunter prove ./my-java-project --test-cmd "gradle test"
```

Java 特有的局限：同样没有完整类型推导；注解处理器 / DI 框架（Spring、Dagger……）的装配是隐式的——框架管理的 bean 被报 high 时多留个心眼。

## TypeScript 支持

TypeScript 引擎基于 [tree-sitter](https://tree-sitter.github.io/)（`tree-sitter-typescript` + `tree-sitter-javascript`），走**同一套管线**——同样的置信度分级、证据链、`prove`、HTML 报告。`.ts` 用 typescript grammar，`.tsx` 用 tsx grammar（含 JSX 组件引用），`.js`/`.jsx` 用 javascript grammar。按扩展名自动识别；Python、Java、TypeScript 混在一个目录里会一起扫；可用 `--lang` 覆盖：

```bash
pip install "deadcode-hunter[typescript]"

deadcode-hunter scan ./my-ts-project
deadcode-hunter scan ./mixed --lang typescript   # 只扫 .ts/.tsx/.js/.jsx
```

TypeScript 特有的规则：

- `export` 出去的符号是模块的公共接口——**永不报告**（和 Python 的 `__all__` 一个待遇），包括导出类的成员方法和 `export { a, b }` 的名字
- 接口（interface）和类型别名是契约——永不报告；枚举是真实的值，会正常分析
- 被装饰的类/函数降为 medium（装饰类的成员继承这个降级）
- `this.x` 解析到所在类的成员；`new Foo()` 保住类；`const s = new Service()` + `s.serve()` 靠构造器类型跟踪解析
- `console`/`process`/`window`/`globalThis` 是平台全局——不算引用，不产生噪音
- 相对导入（`./util`、`../lib`）会解析到磁盘上的文件；副作用导入（`import "./polyfill"`）永不报告

TypeScript 项目用 `prove`/`clean` 时，用 `--test-cmd` 指定自己的测试命令：

```bash
deadcode-hunter prove ./my-ts-project --test-cmd "npm test"
deadcode-hunter clean ./my-ts-project --test-cmd "npx jest"
```

TypeScript 特有的局限：无类型标注的变量走同样的保守弱引用规则；框架魔法（Angular DI、注册路由的装饰器）是隐式的——框架管理的代码被报 high 时多留个心眼。

## GitHub Action

CI 里用 composite action（`action.yml`）：

```yaml
- uses: zheng0629/deadcode-hunter@v0.4.0
  with:
    path: .
    min-confidence: high
```

| Input | 默认值 | 说明 |
|---|---|---|
| `path` | `.` | 扫描的目录（或文件） |
| `min-confidence` | `high` | 最低置信度：high / medium / low |
| `pip-version` | `0.4.0` | 从 PyPI 安装的版本 |

action 输出 `findings`（如 `total=3 high=2 medium=1 low=0`）。[`examples/pr-comment.yml`](examples/pr-comment.yml) 是可直接抄的 workflow 示例——在每个 PR 下发评论展示 findings 表格（首次创建，之后更新同一条）。

本仓库自己就在用：[`.github/workflows/ci.yml`](.github/workflows/ci.yml) 跑测试，外加 `scan src --min-confidence high --fail-on-new` 当门禁。

## Roadmap

- **M1 跨文件扫描**（已实现）：跨文件引用解析，high/medium/low 三档置信度 + 证据链。
- **M2 --prove**（已实现）：`deadcode-hunter prove`——git worktree 隔离 + 测试套件，对 high 置信度候选做"删除验证"，把"证明安全"从静态做到动态。
- **M3 HTML 报告**（已实现）：`deadcode-hunter report`——单个自包含 HTML 文件（零外部依赖），汇总统计、可点击文件树、证据链、源码片段展开 + 死代码行高亮、符号搜索、深色模式；可用 `--prove-json` 把 prove 结果合并进来。
- **Java 支持**（已实现）：基于 tree-sitter 的扫描器（可选安装 `pip install "deadcode-hunter[java]"`），走同一套管线——置信度、证据链、`prove`、HTML 报告全都有；`--lang auto|python|java` 可选语言。
- **M6 `clean`**（已实现）：一键清理——扫描 → worktree 里逐个验证 → 把已证明安全的删除应用到工作区（`--dry-run` 预览、`--yes` 跳过确认、工作区脏则拒绝）。
- **M7 GitHub Action**（已实现）：composite `action.yml` + PR 评论 workflow 示例 + 本仓库自己的 CI（含 `--fail-on-new` dogfood 门禁）。
- **M8 Baseline**（已实现）：`--save-baseline` / `--baseline` 只报告新增的死代码；`--fail-on-new` 供 CI 当门禁（exit 1/0）。
- **M9 配置文件**（已实现）：被扫项目 `pyproject.toml` 的 `[tool.deadcode-hunter]`——`exclude`、`entry-points`、`min-confidence`、`test-cmd` 默认值，CLI 显式传参覆盖。
- **TypeScript 支持**（已实现）：基于 tree-sitter 的扫描器（可选安装 `pip install "deadcode-hunter[typescript]"`），支持 `.ts`/`.tsx`/`.js`/`.jsx`（含 JSX 组件引用），走同一套管线；`export` 的符号视为公共接口永不报告；`--lang typescript`。
- **M11 `deps`**（已实现）：`deadcode-hunter deps`——从 `requirements.txt` / `[project] dependencies` 找声明了但从没 import 的包，内置 import→发行包名映射表；动态导入项目里的可疑项进 *undetermined* 而不是判死刑。
- **M12 `duplicates`**（已实现）：`deadcode-hunter duplicates`——token 归一化的复制粘贴检测（Python），同文件/跨文件都报，按组展示，`--min-lines` / `--min-tokens` 可调。
- **M13 交互式清理**（已实现）：`clean --interactive`（`-i`）——验证之后逐条展示源码片段，y/n/a/q 逐条决定删不删；非终端直接拒绝（脚本用 `--yes`）。

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
