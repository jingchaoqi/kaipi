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
  <a href="https://github.com/jingchaoqi/kaipi/releases/latest">下载</a> ·
  <a href="docs/CONCEPTS.md">设计原理</a>
</p>

kaipi 能阅读代码、修改文件、执行命令和运行测试。你可以从任意一轮分支探索，
再把有用的结论带回主线，让后续对话保持精简。

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
uv tool install --force ./kaipi-0.1.5-py3-none-any.whl
cd /path/to/your/project
kaipi
```

首次运行先用双语问候，选择简体中文或 English，再按所选语言配置 API 提供商和模型。之后可在 `/config` 切换界面语言，CLI 与 Canvas 保持一致；模型回复语言不受影响。配置完成后，直接描述任务：

```text
找出登录接口偶发超时的原因，修复并运行相关测试。
```

后续用 `kaipi -c` 继续最近的会话，或用 `kaipi -r <会话id>` 打开指定会话。
用 `kaipi version` 或 `/version` 查看版本；用 `kaipi update` 更新，或在对话中输入
`/update`，校验安装包后更新并接回原会话（支持 uv tool 安装）。终端每天后台检查一次新版，
提示当前版本、最新版本和 Release 链接。用 `/config` 或 `kaipi config` 设置每日自动检查更新：
上下键选择、左关右开，修改立即保存，Esc 返回。未保存过此设置时，`KAIPI_NO_UPDATE_CHECK=1`
可关闭自动检查；在面板中明确设置后，以保存的选择为准。手动 `/update` 不受影响。
支持从任意历史正式版直接升级，保留已保存的会话、附件、配置和回退快照，不保证降级兼容。
旧版尚无更新命令时，先下载新版安装包，执行 `uv tool install --force <安装包.whl>` 后运行 `kaipi -c`。

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
| `/resume` | 上下键选择历史对话，回车进入；首条问题会自动生成会话标题 |
| `/ledger` · `/raw` | 查看用量账本；检查下一轮将发送的完整上下文 |
| `/provider` · `/model` | 配置或刷新提供商模型与价格；上下键切换模型，Esc 取消 |
| `/config` | 设置界面语言和每日自动检查更新：上下选择、左右修改，立即生效，Esc 返回 |
| `/statusline` | 定制底部状态行：上下选择、左右轮换选项，Esc 保存；目录可简约显示，上下文可显示百分比 |
| `/inspect-context` | 将下一轮上下文导出到 workspace 根目录，文件名为「年月日时分秒-对话名-当前节点编号-context.md」 |
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
