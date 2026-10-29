kaipi 的第一个正式版本。一个以会话树组织上下文的命令行 coding agent。

- 会话树、探索分支、归档、嫁接和代码／对话回退。
- 多种 API 协议、模型配置及 token／费用统计。
- 终端 Markdown 与代码块高亮，紧凑的工具调用展示和清晰的消息间距。
- 斜杠命令即时匹配、方向键选择，以及 60fps 状态动画和完成渐隐。
- 浏览器 Canvas 会话视图。

需要 Python 3.11+ 和 Git。下载下方 `.whl` 文件后安装：

```sh
uv tool install --force ./kaipi-0.1.0+gb8d4c0-py3-none-any.whl
kaipi
```

首次运行会引导配置 API 提供商、key 和模型。

发布提交：`b8d4c0d8dd471c1e8f36b22a713f99ebadf7e98f`。安装包版本 `0.1.0+gb8d4c0`，保留提交标识，方便确认安装来源。附件同时提供源码包和 SHA-256 校验值。
