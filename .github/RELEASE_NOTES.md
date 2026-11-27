- **更新内置模型目录和参考费率**。OpenAI 默认提供 GPT-6.2 Astra、GPT-6.2 Sol、GPT-6.1 Sol、GPT-6.1 Terra 和 GPT-6 Luna。Anthropic、Gemini、DeepSeek、Kimi、GLM、Qwen 等供应商和聚合平台的目录也已更新。
- **修正跨供应商计价**。新轮次、会话标题和摘要使用所选供应商的费率。地区和平台有独立定价时，账本按对应费率估算。
- **刷新已有配置**。在 `/provider` 中选择已配置的供应商，再选择「刷新模型与价格」，即可把新模型加入 `/model`，同时保留已保存的模型。

运行 `kaipi update`，或在对话中输入 `/update` 即可升级。升级保留已有配置、会话、附件和回退快照。
