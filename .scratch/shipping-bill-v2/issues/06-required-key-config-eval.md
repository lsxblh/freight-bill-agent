# 06: 必需 key + 配置 + 新旧评测

**What to build:** 收尾验证票。综合 agent 定为命脉：无 API key 明确报错、不静默降级（此为行为变更，故放在搬骨架之后）。配置新增 JUDGE_MODEL、重试上限、综合 agent 必需 key 开关，沿用 LLM_MODEL/OPENAI_BASE_URL/OPENAI_API_KEY/MY_COMPANY。跑新旧评测对比，7/7 不回归（recall/precision/operation/posting），更新 README/架构文档反映 v2。测试全部经主缝注入假 LLM，不依赖外网。

**Blocked by:** 03, 04, 05

**Status:** ready-for-agent

- [ ] 综合 agent 无 key 明确报错，不静默降级
- [ ] 配置含 JUDGE_MODEL/重试上限/必需 key 开关
- [ ] 新旧评测对比 7/7 不回归
- [ ] README/架构文档更新为 v2 四段式
- [ ] 全部测试离线（注入假 LLM/OCR 缓存）通过