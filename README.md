<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/kaipi-logo-dark.svg">
    <img src="docs/assets/kaipi-logo.svg" alt="kaipi 标志：树状上下文与探索分支" width="64" height="64" align="absmiddle">
  </picture>
  &nbsp;kaipi（开辟）
</h1>

<p align="center"><strong>用会话树管理上下文的命令行编程 Agent。</strong></p>

<p align="center">
  <a href="https://github.com/jingchaoqi/kaipi/releases/latest"><img src="https://img.shields.io/github/v/release/jingchaoqi/kaipi?style=flat-square&amp;label=release&amp;color=58B99C" alt="最新版本"></a>
  <a href="https://github.com/jingchaoqi/kaipi/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/jingchaoqi/kaipi/ci.yml?branch=master&amp;style=flat-square&amp;label=build" alt="构建状态"></a>
  <a href="#快速开始"><img src="https://img.shields.io/badge/python-%E2%89%A53.11-627D6D?style=flat-square" alt="Python ≥3.11"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-627D6D?style=flat-square" alt="MIT 许可证"></a>
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="#分支工作流">分支工作流</a> ·
  <a href="#会话与恢复">会话恢复</a> ·
  <a href="#按你的习惯设置">个性化设置</a> ·
  <a href="https://github.com/jingchaoqi/kaipi/releases/latest">下载</a> ·
  <a href="docs/CONCEPTS.md">设计原理</a>
</p>

kaipi 能阅读代码、修改文件、执行命令和运行测试。你可以从任意一轮分支探索，
再把有用的结论带回主线，让后续对话保持精简。

## 核心亮点

- **探索独立，结论可复用。** 每轮对话都是一个节点。分支上的调查和尝试独立于主线，
  通过「嫁接」带回结论与摘要，减少后续反复携带的上下文。
- **上下文由你掌控。** 从任意节点继续，归档不再需要的分支，或回退对话、代码及两者。
  归档可恢复；代码回退只还原目标节点之后 Agent 改过的文件。用 `/inspect-context` 导出下一轮上下文，
  核对从 root 到当前节点的路径，以及主动嫁接的内容。
- **用量与费用可见。** 每轮展示 token、缓存命中和费用，账本记录累计消耗，
  并估算分支、嫁接和打断减少的上下文负担。
- **图片与文件一起提问。** 连续粘贴多张图片、PDF、Word、Excel 或代码文件，
  自动识别和转换；输入框用简洁的附件标签展示。
- **离开后，随时接着聊。** 自动生成会话标题，键盘选择历史会话；恢复时重现当前分支的提问、
  工具调用和回答，无需重新执行任务。
- **终端与实验性 Canvas 画布。** 中英文界面、Markdown 与代码块高亮、键盘补全、流畅状态动画和可定制状态行；
  浏览器中可视化操作会话树、查看工具输出、拖拽嫁接和归档，随时切回 CLI。
- **自由选择模型。** 支持 Anthropic、OpenAI Responses、OpenAI Chat 和 Gemini 四种协议，
  可接入 DeepSeek、Kimi、GLM、OpenRouter、Ollama 等服务，在会话中切换模型、刷新模型与价格目录。

## 快速开始

需要 **Python 3.11+、Git 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)**。
建议在 Git 仓库中使用，以启用代码快照、回退和探索时的工作区检查。

从 [Releases](https://github.com/jingchaoqi/kaipi/releases/latest) 下载 `.whl` 安装包，然后执行：

```sh
uv tool install --force ./kaipi-0.1.5-py3-none-any.whl
cd /path/to/your/project
kaipi
```

首次运行通过双语引导选择简体中文或 English，再配置 API 提供商和模型。
配置完成后自动清屏，直接描述任务：

```text
找出登录接口偶发超时的原因，修复并运行相关测试。
```

## 会话与恢复

首条问题会独立生成会话标题，命名请求不会进入对话上下文；用 `/rename <名字>` 自行修改。
`/new` 开始新会话，`/resume` 用上下键选择历史会话、回车进入，也可从终端直接恢复：

```sh
kaipi -c             # 继续最近的会话
kaipi -r <会话id>    # 打开指定会话
```

恢复时显示从 root 到当前节点的历史提问、工具调用和回答，保留 Markdown 与代码高亮。
退出时清屏，留下会话名、工作目录、总节点数、累计费用和恢复命令。
空闲时按 `Ctrl+C` 需在 3 秒内再按一次确认，避免误退出。

## 更新

运行 `kaipi update`，或在对话中输入 `/update`：校验安装包后更新并接回原会话（支持 uv tool 安装）。
`kaipi version` 或 `/version` 查看当前版本；每日后台检查会提示当前版本、新版及 Release 链接，
可在 `/config` 关闭。

支持从任意历史正式版直接升级，保留已保存的会话、附件、配置和回退快照，不保证降级兼容。
0.1.0–0.1.2 尚无更新命令，请下载新版 `.whl`，执行 `uv tool install --force <安装包.whl>` 后用 `kaipi -c` 继续。

## 图片与文件

- **终端**：粘贴文件路径添加附件；用 `Ctrl+V` 读取系统剪贴板中的截图或文件。
  可连续粘贴多张图片，输入框以 `[image1]`、`[image2]` 显示，其他文件以 `[file1]` 显示；退格移除。
  Linux 剪贴板需要 `wl-clipboard` 或 `xclip`。
- **Canvas**：直接粘贴、拖入文件，或点击「附件」选择；发送前可以移除，历史记录中可以展开查看。
- 按文件实际内容识别：HEIC、AVIF、TIFF 等图片自动转换，Office、演示文稿、EPUB 等文档自动提取；
  代码和文本保留原文。图片、PDF 需要所选模型支持。

附件随消息保存转换快照。每条最多 8 个原文件，单个最多 20 MiB；
更多格式及限制见[附件说明](docs/ADVANCED.md#attachments)。

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

## 按你的习惯设置

- **`/config`**：切换简体中文 / English、开关每日自动检查更新。上下选择、左右轮换，立即保存生效；
  Esc 返回。CLI 与 Canvas 使用同一界面语言，模型回复语言不受此设置限制。
- **`/statusline`**：选择底部显示的模型、会话、目录、用量与费用等信息。目录可选完整 / 简约 / 关闭，
  上下文可选完整 / 百分比 / 关闭，其余项目可开关；上下选择、左右轮换，Esc 保存返回。
- **`/provider` 与 `/model`**：提供商支持输入序号或上下键选择，模型用上下键选择、回车确认。
  已配置的提供商可编辑、清除，或从 kaipi 在线目录刷新模型与价格；保存后即可在 `/model` 选择新模型。
  Esc 取消未保存的更改，菜单关闭后回到原来的对话画面。

也可在终端运行 `kaipi config`。未保存过自动检查设置时，`KAIPI_NO_UPDATE_CHECK=1` 可关闭检查；
之后以面板中的设置为准，手动更新不受影响。

## 常用操作

| 命令或按键 | 用途 |
|---|---|
| `/tree` · `/go` | 打开会话树导航，方向键选择节点，回车从此继续 |
| `/explore <问题>` | 从当前位置开始探索，保留主线 |
| `/go N1` · `/graft N2` | 从指定节点继续；将另一分支的结论带入下一轮 |
| `/archive N2` · `/restore N2` | 归档或恢复整条子树 |
| `/rewind` | 用方向键选择节点，预览并回退代码、对话或两者 |
| `/status` | 查看模型、会话、上下文占用、用量与费用概览 |
| `/new` | 保留旧对话，清空显示并进入新对话；用 `/resume` 找回历史 |
| `/resume` · `/rename <名字>` | 选择并恢复历史对话；修改当前会话标题 |
| `/ledger` · `/raw` | 查看用量账本；检查下一轮将发送的完整上下文 |
| `/provider` · `/model` | 添加、编辑、清除提供商或刷新模型与价格；切换模型 |
| `/config` · `/statusline` | 设置语言、更新检查和底部状态行 |
| `/inspect-context` | 将下一轮上下文导出为工作目录根部的 `.md` 文件，并显示路径 |
| `/version` · `/update` | 查看版本；检查并安装新版 |
| `/canvas` | 打开实验性浏览器画布，输入 `/cli` 返回终端 |
| `Esc` | 停止当前回合；该回合不再进入后续上下文，已发生的消耗仍计费 |

会话树中 ↑/↓ 浏览节点，← 选择父节点，→ 进入子分支，Esc 取消；`kaipi tree` 可直接打印完整树。

输入 `/` 在光标上方展示全部命令，默认选中 `/help`；输入前缀可缩小匹配范围。↑/↓ 选择，回车执行。
完整用法见 `/help` 或 `kaipi --help`。

上下文导出文件名为 `年月日时分秒-对话名-当前节点编号-context.md`，只包含系统提示和消息，
保留下一句占位符，方便检查实际发送内容。

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
