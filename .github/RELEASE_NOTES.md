这次更新，让 kaipi 的升级更顺手。

- **新版提醒**：终端每天后台检查一次，发现新版时展示当前版本、最新版本和 Release 链接；可通过 `KAIPI_NO_UPDATE_CHECK=1` 关闭。
- **一条命令更新**：支持 `kaipi update` 和 `/update`，校验安装包后更新；在对话中更新会保存并接回原会话。适用于 uv tool 安装。
- **随时查看版本**：使用 `kaipi version` 或 `/version`。
- **全新项目门面**：树状 Logo 与更简洁的 README，突出分支探索和上下文管理。

支持从旧版直接升级，保留已保存的会话、附件、配置和回退快照。0.1.0–0.1.2 尚无更新命令，请先下载本页的 `.whl` 文件，执行：

```sh
uv tool install --force ./kaipi-0.1.3-py3-none-any.whl
kaipi -c
```
