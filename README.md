# kaipi（开辟）

**用会话树管理上下文的命令行编程 Agent。**

kaipi 能阅读代码、修改文件、执行命令和运行测试。你可以从任意一轮分支探索，
再把有用的结论带回主线，让后续对话保持精简。

[快速开始](#快速开始) · [分支工作流](#分支工作流) · [下载](https://github.com/jingchaoqi/kaipi/releases/latest) · [设计原理](docs/CONCEPTS.md)

## 核心亮点

- **探索独立，结论可复用。** 每轮对话都是一个节点。分支上的调查和尝试独立于主线，
  通过「嫁接」带回结论与摘要，减少后续反复携带的上下文。
- **上下文由你掌控。** 从任意节点继续，归档不再需要的分支，或回退对话、代码及两者。
  归档可恢复；代码回退只还原目标节点之后 Agent 改过的文件。
- **用量与费用可见。** 每轮展示 token、缓存命中和费用，账本记录累计消耗，
  并估算分支、嫁接和打断减少的上下文负担。
- **终端与实验性 Canvas 画布。** 终端提供 Markdown、代码高亮、命令补全和流畅的状态动画；
  Canvas 可在浏览器中可视化操作会话树，支持展开工具输出、拖拽嫁接和归档节点。
- **自由选择模型。** 支持 Anthropic、OpenAI Responses、OpenAI Chat 和 Gemini 四种协议，
  可接入 DeepSeek、Kimi、GLM、OpenRouter、Ollama 等服务，在会话中切换模型。

## 快速开始

需要 **Python 3.11+、Git 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)**。
建议在 Git 仓库中使用，以启用代码快照、回退和探索时的工作区检查。

从 [Releases](https://github.com/jingchaoqi/kaipi/releases/latest) 下载 `.whl` 安装包，然后执行：

```sh
uv tool install --force ./kaipi-*.whl
cd /path/to/your/project
kaipi
```

首次运行会引导配置 API 提供商、地址、key 和模型。配置完成后，直接描述任务：

```text
找出登录接口偶发超时的原因，修复并运行相关测试。
```

后续用 `kaipi -c` 继续最近的会话，或用 `kaipi -r <会话id>` 打开指定会话。
升级时下载新版安装包，执行同一条安装命令即可。

## 分支工作流

假设主线已经推进到 `N1`，你想先调查一个方案：

```text
N1> /explore 分析给登录接口加缓存是否合适，先不要修改代码
N2> /go N1
N1> /graft N2
N1> 根据这个结论实现方案，并补充测试
```

`/explore` 保留主线位置并创建探索分支；`/go N1` 回到主线；
`/graft N2` 将探索结论与路径摘要登记到下一轮输入。
主线随后沿新节点继续，无需携带整段探索中的工具调用记录。

探索模式使用只读提示和 Git 工作区检查，发现意外修改时会提醒并提供还原选项。
它使用的仍是当前工作目录。节点编号可通过 `/tree` 查看。

## Canvas 画布（实验性）

在会话中输入 `/canvas`，或运行 `kaipi canvas -c`，即可在本机浏览器中打开会话画布。
你可以点击节点继续对话，将节点拖到输入框进行嫁接，或拖到归档区收起分支。
**同一会话可随时在 CLI 与 Canvas 之间切换**：CLI 中输入 `/canvas` 打开画布，
画布中输入 `/cli` 返回终端，保留当前会话与进度。

## 常用操作

| 命令或按键 | 用途 |
|---|---|
| `/tree` · `/go` | 打开会话树导航，方向键选择节点，回车从此继续 |
| `/explore <问题>` | 从当前位置开始探索，保留主线 |
| `/go N1` · `/graft N2` | 从指定节点继续；将另一分支的结论带入下一轮 |
| `/archive N2` · `/restore N2` | 归档或恢复整条子树 |
| `/rewind` | 用方向键选择节点，预览并回退代码、对话或两者 |
| `/ledger` · `/raw` | 查看用量账本；检查下一轮将发送的完整上下文 |
| `/provider` · `/model` | 配置 API 提供商；切换模型 |
| `/canvas` | 打开实验性浏览器画布，输入 `/cli` 返回终端 |
| `Esc` | 停止当前回合；该回合不再进入后续上下文，已发生的消耗仍计费 |

会话树中 ↑/↓ 浏览节点，← 选择父节点，→ 进入子分支，Esc 取消；`kaipi tree` 可直接打印完整树。

输入 `/` 展示全部命令，默认选中 `/help`；输入前缀可缩小匹配范围。↑/↓ 选择，回车执行。
完整用法见 `/help` 或 `kaipi --help`。

会话记录保存在项目的 `.kaipi/`；API 配置保存在 `~/.config/kaipi/auth.toml`
（遵循 `XDG_CONFIG_HOME`）。费用根据 API 返回的用量及配置价格计算，详见[账本说明](docs/LEDGER.md)。

## 开发与文档

```sh
git clone https://github.com/jingchaoqi/kaipi.git
cd kaipi
uv sync
uv run kaipi
```

- [设计原理](docs/CONCEPTS.md)：节点、分支、上下文与嫁接
- [模型接入](docs/PROVIDERS.md)：提供商、端点、模型与价格配置
- [费用账本](docs/LEDGER.md)：token、缓存、费用与节省估算
- [进阶与开发](docs/ADVANCED.md)：快照、本地 API、构建与测试

[MIT License](LICENSE)
