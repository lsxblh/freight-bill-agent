# 08: 裁判断链修复 + 重抽反馈真生效 + judge/repair 死代码清理

**What to build:** 集成审查 review-v2 的必修项与用户拍板项。
1. 【blocker】graph/judge.py:246 judge() 直调 self._chain.invoke 但 _chain 恒 None(:197)、_build()(:200-230) 无调用点→裁判启用即抛错被 :258 捕获静默放行，真裁判从未生效。修=judge() 改 self._build().invoke（或构造时建链），并补"真 LlmJudge+假 chain"回归测试钉住启用路径（现有测试全注 FakeJudge 抓不到）。
2. 【major·用户拍板 A=让反馈真生效】graph/repair.py:84-85 reextract 空转：不带 force_ocr、gate_reason 只进 entry.warnings 不进抽取路径→重试必然同结果。修=reextract 策略带 force_ocr（换路重读），并把 gate_reason 作为反馈喂进抽取（LLM 草稿提示或抽取策略），使重试真正不同；耗尽仍安全落 PENDING_REVIEW。
3. 【minor·死代码】repair.py:59 self.ocr 只存不读、:35 logger 未用、:61 stats 无读者；judge.py:198 stats 无读者。清理或接上读者（stats 若保留须有汇总读者）。

**Blocked by:** 06

**Status:** ready-for-agent

- [ ] 真 LlmJudge 启用路径回归测试（假 chain 驱动，钉住不再静默放行）
- [ ] reextract 带 force_ocr + gate_reason 进抽取反馈（重试与首次真正不同）
- [ ] judge/repair 死代码清理（self.ocr/logger/无读者 stats）
- [ ] 现有测试一行不改；全量离线 pytest 绿；离线 eval 7/7 不回归
- [ ] 顶层六字段/operation 四值/OCR 契约/费用名字典不动；重试≤2、耗尽→PENDING_REVIEW 不变
- [ ] Retro 六问写本文件 ## Retro 并附回报

**范围限制:** 只改 graph/judge.py、graph/repair.py、graph/graph.py 的 ingest/repair 路由区、及抽取反馈接入点；【不要改 graph/agents.py】（impl-07 正在改提示词，避免冲突）。不要 merge。

**注意:** 裁判默认 judge_enabled=False 保持（离线不花 key）；reextract  force_ocr 走 OCR 网关不花 LLM key；gate_reason 喂 LLM 草稿才花 LLM key（草稿可选）。行为变更已获用户批准（A）。