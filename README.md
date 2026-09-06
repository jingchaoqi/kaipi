# kaipi（开辟）

一个很小的命令行 coding agent。别人的对话是一条**直线**，kaipi 的对话是一棵**树**——
你可以从任意一轮岔出去试，试砸了整枝扔掉，试成了只把结论摘回来。

省钱是这么省出来的，不是靠压缩、也不是靠换便宜模型。

## 为什么一条直线会越聊越贵

跟 AI 聊代码，你每问一句，模型都要把**前面所有的对话重新读一遍**，并且重新收一次钱。

于是走过的弯路特别贵：你试了个方案，发现不行，放弃了——但那段对话还赖在上下文里，
之后每问一句都要为它再付一次。聊得越久越贵，而且越来越慢、越来越糊涂。

问题不在于"读得太多"，在于**你没有办法只扔掉那一段**。一条直线上，
唯一的操作是"继续说"和"全部重来"。

## kaipi 给你的是节点级的操作

一轮对话（你问一次 + AI 干完活给出回答）就是一个**节点**。节点连成树，
于是"扔掉这一段"变成了一个真实存在的动作：

| 你想干的事 | 在 kaipi 里 | 上下文发生了什么 |
|---|---|---|
| 从半路岔出去试个方案 | `/explore` 开一条**探索**分支 | 它的 token 永远不进主干 |
| 这条路走死了 | `/archive` 整枝收起（可恢复） | 整棵子树一起退出上下文 |
| 这条路有用 | `/graft` **嫁接**回主干 | 只搬结论的快照，不搬全过程 |
| 这一轮跑歪了 | **Esc** 停掉 | 这一轮作废，永不再读，但钱照记 |
| 连代码一起退回去 | `/rewind` | 只还原 agent 改过的文件 |

**主干**是你真正在推进的那条路径——只有主干上的东西会被反复付费。
所以 kaipi 每轮都告诉你一个数字：**主干负担（trunk burden）**，
也就是"你之后每问一句都要重新付的 token 量"。

一句话：**探索花的是单利，主干花的是复利。** 上面每个动作都是在阻止某段对话变成复利，
其中探索、嫁接、打断三种能被折算成具体数字——`/ledger` 里分开记着，状态栏上也一直显示着。

## 安装

需要 Python 3.11+ 和 git。装的是一个 `.whl` 文件——纯 Python，不用编译，装完就有 `kaipi` 命令。

**1. 下载。** 去 [Releases](https://github.com/jingchaoqi/kaipi/releases) 拿最新的
`kaipi-<版本>+g<commit>-py3-none-any.whl`。
还没有 release 的话，从 [Actions](https://github.com/jingchaoqi/kaipi/actions) 里点最近一次绿色的
**构建打包**，页面底部 **Artifacts** 有个 `kaipi-<commit前6位>`，下载解压即可。
文件名里的 `+g` 后面就是它的 commit，用来对上是哪一版。

**2. 安装。**

```sh
uv tool install ./kaipi-0.1.0+g1a2b3c-py3-none-any.whl   # 换成你下载到的那个文件名
kaipi --help
```

没有 `uv` 就先 `curl -LsSf https://astral.sh/uv/install.sh | sh`；用 `pipx install ./kaipi-*.whl`
或 `pip install --user ./kaipi-*.whl` 也一样。

**升级**就是拿新的 whl 再 `uv tool install --force ./kaipi-*.whl`。装的是哪一版可以用
`uv tool list` 或 `pip show kaipi` 看，版本号后面带着 commit。卸载见下一节。

<details>
<summary>或者直接从源码装（要改 kaipi 本身时）</summary>

```sh
git clone https://github.com/jingchaoqi/kaipi && cd kaipi
uv sync
uv run kaipi --help
uv build          # 自己产出 dist/*.whl
```
</details>

## 卸载（清理干净）

kaipi 一共往你机器上写三种东西，卸载就是把这三样都清掉。

**1. 卸掉命令本身**

```sh
uv tool uninstall kaipi       # 或 pipx uninstall kaipi / pip uninstall kaipi
```

**2. 清掉每个用过 kaipi 的项目里的痕迹**

先找出它们：

```sh
find ~ -maxdepth 6 -type d -name .kaipi 2>/dev/null
```

然后在每个项目里执行三步——**第三步不能省**：

```sh
cd ~/那个项目
rm -rf .kaipi                                     # 会话日志、游标、锁、私有索引
git for-each-ref --format='%(refname)' refs/kaipi | while read -r r; do git update-ref -d "$r"; done
git gc --prune=now                                # 让快照对象真正被回收
```

为什么第三步是必须的：kaipi 为了让 `/rewind` 随时可用，把每一轮的代码快照**钉在
`refs/kaipi/` 下面**，就是为了让 `git gc` 不会顺手清掉它们。所以只删 `.kaipi/` 目录不够——
ref 还在，快照对象就永远留在你的 `.git` 里。删掉 ref 之后再 `gc`，它们才真的消失。

**3. 全局价格覆盖（只有你自己建过才有）**

```sh
rm -rf ~/.config/kaipi        # 设过 XDG_CONFIG_HOME 的话在 $XDG_CONFIG_HOME/kaipi
```

除此之外没有别的了：没有系统级配置、没有缓存目录、不往 `~/.gitconfig` 写东西，
也从没碰过你的 index、stash 和分支。

**验证清干净了**（在项目里跑）：

```sh
test -d .kaipi && echo "还有残留" || echo "目录已清"
git for-each-ref | grep kaipi || echo "refs 已清"
git fsck                      # 无输出即仓库完好
```

我实测过这套流程：一个仓库用 kaipi 跑两轮后，按上面三步清理，文件列表和用之前**逐字节一致**，
快照对象数归零，`git fsck` 干净，提交历史和工作区不受任何影响。

## 开始用

```sh
export ANTHROPIC_API_KEY=...       # 也支持 OPENAI_API_KEY / GEMINI_API_KEY / DEEPSEEK_API_KEY ...
export KAIPI_MODEL=claude-opus-5

cd ~/你的项目                       # 建议是个 git 仓库：代码回退和探索保护需要它
kaipi
```

然后就是一个提示符，直接说人话：

```
kaipi  anthropic/claude-opus-5  ~/你的项目  context 0.0k/200k  spent $0.0000  saved 0.0k/turn = $0.0000
root> 找一下 app.py 里的 bug 并修掉
    $ cat app.py
    ...
    已修复：rate_limit 现在和 LIMIT 比较。测试通过。
[078S8K trunk] $0.0064  cache read 0.3k/0.7k  trunk burden: 0.4k
078S8K>
```

每轮结束的那一行才是重点：这轮花了多少钱、多少是从缓存里读的、
以及 **trunk burden** —— 你之后每一轮都要重新付的量。

最上面那行是状态栏：**提供方 / 模型 · 工作目录 · 上下文占用与窗口 · 累计花费 · 累计省下**。
`/tree` 里每个节点也各自标着这一轮花了多少钱。

## 全部命令

会话里敲斜杠命令；同样的事在会话外也能用 `kaipi <子命令>` 做。下面是**全部**，没有更多了。

### 会话里的斜杠命令

| 命令 | 作用 |
|---|---|
| `/tree` | 会话树：哪条是主干、光标在哪、每个节点的 token 和花费、归档与废弃的标记 |
| `/explore <你的问题>` | 从当前位置岔出去问一句，主干钉在原地不动 |
| `/go <节点id>` | 把光标移到某个节点，下一句从那里长出分支 |
| `/graft <节点id> [--depth leaf\|leaf+summary\|branch] [--with-tool <id>...]` | 把那个节点嫁接进**下一句**输入。默认 `leaf+summary`；`--with-tool` 额外带上指定的工具输出。执行前会打印三种深度各多花多少 token |
| `/archive <节点id>` | 把这个节点连同整棵子树收起来，退出上下文 |
| `/restore <节点id>` | 撤销上面那步 |
| `/trunk` | 显示当前主干是哪条、是钉住的还是自动判断的 |
| `/trunk pin <节点id>` | 把主干钉到这个节点 |
| `/rewind <节点id> [both\|code\|conversation]` | 回退。不给模式就交互式问你；`conversation` 只挪光标，`code` 只还原文件，`both` 两个都做 |
| `/ledger` | 总账：花了多少、各项 token、探索 / 嫁接 / 打断分别替主干挡下了多少 |
| `/canvas` | 把这个会话搬到浏览器（画布里敲 `/cli` 搬回来） |
| `/quit`、`/exit` | 结束 |

**按键**：`Esc` 停掉当前这一轮；提示符上按 `Ctrl-C` 直接退出（游标已存，下次接着来）；
`↑`、`↓` 翻历史输入。

### 会话外的子命令

| 命令 | 作用 |
|---|---|
| `kaipi` | 开一个交互式会话（自动接上这个目录里最近的那个） |
| `kaipi --new` | 强制开一个全新会话，不接旧的 |
| `kaipi canvas [--new]` | 直接开在浏览器里，跳过终端 |
| `kaipi tree` | 同 `/tree` |
| `kaipi go <节点id>` | 同 `/go` |
| `kaipi graft <节点id> [--depth ...] [--with-tool ...]` | 同 `/graft` |
| `kaipi archive <节点id>` / `kaipi restore <节点id>` | 同上 |
| `kaipi trunk` / `kaipi trunk pin <节点id>` | 同上 |
| `kaipi rewind <节点id> [--mode both\|code\|conversation]` | 同 `/rewind` |
| `kaipi ledger` | 同 `/ledger` |
| `kaipi sessions` | 列出这个目录下所有会话（只有子命令有，斜杠命令里没有） |
| `kaipi --help`、`kaipi <子命令> --help` | 用法 |

`/explore` 是要发一句话给模型的，所以只有斜杠命令形式。

**节点 id 可以只敲前几位或后几位**，只要在本次会话里不重复就行（树里显示的是后 6 位）。

### 环境变量

| 变量 | 作用 |
|---|---|
| `KAIPI_MODEL` | 用哪个模型。`pricing.toml` 里列过的直接写 id，没列过的写 `<提供方>/<模型id>` |
| `<提供方>_API_KEY` | 密钥，比如 `ANTHROPIC_API_KEY`、`OPENAI_API_KEY`、`MOONSHOT_API_KEY`、`DEEPSEEK_API_KEY` |
| `<提供方>_BASE_URL` | 换端点，比如 `KIMI_BASE_URL=https://api.moonshot.cn/v1` |
| `KAIPI_PORT` | 画布用哪个端口（默认随机） |

### 关于 Esc

停掉的那一轮会被标成"已废弃"：它产出的内容**不会进入之后的任何上下文**（这就是省钱的地方），
但**已经花掉的钱照样记账**，而且正在跑的命令会连同整个进程组一起被杀——
按下去就停，不用等它跑完。下一句会从它的父节点重新长出一个兄弟节点，
你是重试，不是在残局上继续。

## 画布

```sh
kaipi canvas       # 或者在会话里敲 /canvas
```

会话变成一张图，在本机浏览器里打开（不联网、不上传）：

- 顶部状态栏和终端里是同一组数字；跑起来之后右上角会出现**停止**按钮；
- 被停掉的节点画成红色划掉的样子，标着"已废弃 · 不进上下文"；
- 点节点 = 从它继续；
- **把节点拖到输入框 = 嫁接**，会弹出深度选择和 token 预览；
- 把节点拖到回收区 = 归档；右键可以恢复、改钉主干、回退；
- 敲 `/cli` 就把会话交还给终端。

同一个会话同一时刻只在一个地方打开：终端和画布之间来回交接，不会两边打架。

## 支持哪些模型

原生支持四种接口协议（Anthropic、OpenAI Responses、OpenAI Chat、Gemini），
预置了 anthropic、openai、gemini、deepseek、kimi、glm、opencode、qwen、
openrouter、groq、ollama。其他 OpenAI 兼容的服务，加一段 TOML 就能用。
详见 [`docs/PROVIDERS.md`](docs/PROVIDERS.md)。

## 几个可能担心的点

**会不会弄乱我的代码？** 探索分支是只读的：kaipi 每条命令后都检查工作区，
一旦变脏就提醒你，并提供一键还原。

**回退会覆盖我自己手改的文件吗？** 不会。代码回退只还原**agent 改过的那些文件**，
你自己在别处的修改原样保留。回退前的状态也存了一份，还能反悔。

**会污染我的 git 吗？** 不会。会话数据放在项目下的 `.kaipi/`，它会自动忽略自己，
`git status` 保持干净。快照走的是 kaipi 自己的索引，不碰你的 index、stash 和分支。

**归档是删除吗？** 不是。归档只是"退出上下文"，随时 `/restore` 拿回来。

**"累计省下"是怎么算的？** 三个来源，都按"主干每轮不用重读的 token"计：
探索分支没并进主干、嫁接带的是结论而不是整条分支、打断的回合不进上下文。
换算成钱时只乘"从那时起真正发生过的主干轮数"，所以它是**已经没花出去的钱**，不是预测。
详见 [`docs/LEDGER.md`](docs/LEDGER.md)。

## 想深入了解

- [`docs/CONCEPTS.md`](docs/CONCEPTS.md) —— 设计原理：节点、边、嫁接、打断、七条不变量
- [`docs/LEDGER.md`](docs/LEDGER.md) —— 账本怎么算的
- [`docs/PROVIDERS.md`](docs/PROVIDERS.md) —— 怎么接自己的模型
- [`docs/ADVANCED.md`](docs/ADVANCED.md) —— 本地 HTTP 接口、缓存断点、构建与冒烟测试

MIT License.
