# 08: 裁判断链修复 + 重抽反馈真生效 + judge/repair 死代码清理

**What to build:** 集成审查 review-v2 的必修项与用户拍板项。
1. 【blocker】graph/judge.py:246 judge() 直调 self._chain.invoke 但 _chain 恒 None(:197)、_build()(:200-230) 无调用点→裁判启用即抛错被 :258 捕获静默放行，真裁判从未生效。修=judge() 改 self._build().invoke（或构造时建链），并补"真 LlmJudge+假 chain"回归测试钉住启用路径（现有测试全注 FakeJudge 抓不到）。
2. 【major·用户拍板 A=让反馈真生效】graph/repair.py:84-85 reextract 空转：不带 force_ocr、gate_reason 只进 entry.warnings 不进抽取路径→重试必然同结果。修=reextract 策略带 force_ocr（换路重读），并把 gate_reason 作为反馈喂进抽取（LLM 草稿提示或抽取策略），使重试真正不同；耗尽仍安全落 PENDING_REVIEW。
3. 【minor·死代码】repair.py:59 self.ocr 只存不读、:35 logger 未用、:61 stats 无读者；judge.py:198 stats 无读者。清理或接上读者（stats 若保留须有汇总读者）。

**Blocked by:** 06

**Status:** done (commits 92b8696+c254d88, 133 passed/1 deselected, merged)

- [x] 真 LlmJudge 启用路径回归测试（假 chain 驱动，钉住不再静默放行）
- [x] reextract 带 force_ocr + gate_reason 进抽取反馈（重试与首次真正不同）
- [x] judge/repair 死代码清理（self.ocr/logger/无读者 stats）
- [x] 现有测试一行不改；全量离线 pytest 绿；离线 eval 7/7 不回归
- [x] 顶层六字段/operation 四值/OCR 契约/费用名字典不动；重试≤2、耗尽→PENDING_REVIEW 不变
- [x] Retro 六问写本文件 ## Retro 并附回报

**范围限制:** 只改 graph/judge.py、graph/repair.py、graph/graph.py 的 ingest/repair 路由区、及抽取反馈接入点；【不要改 graph/agents.py】（impl-07 正在改提示词，避免冲突）。不要 merge。

**注意:** 裁判默认 judge_enabled=False 保持（离线不花 key）；reextract  force_ocr 走 OCR 网关不花 LLM key；gate_reason 喂 LLM 草稿才花 LLM key（草稿可选）。行为变更已获用户批准（A）。

## Retro

**1. 做了什么**
- 【blocker】graph/judge.py:246 judge() 由直调 `self._chain.invoke` 改为 `self._build().invoke`（_build 首次懒建链并缓存，:200-230 原逻辑不动）。修复"启用即 AttributeError 被 :258 捕获→永远故障退化放行、真裁判从未生效"。
- 【major·A】reextract 空转修复，三处接线：①graph/graph.py:124 ingest 的 force_ocr 由 `=="ocr"` 改 `in ("ocr","reextract")`，reextract 也强制重 OCR 换路重读（走 OCR 网关）；②graph/graph.py repair 节点把 gate_reason 留存为 repair_feedback（gate 放行会清空 gate_reason，故单独携带，state.py 增一字段），extract 节点经 `owner.extractor.extract(raw, feedback=...)` 传入；③抽取反馈接入点 document_extractor.py（extract/extract_one/llm_node 透传 feedback）+ llm_extractor.py（feedback 非空时渲染"上次解析闸门反馈"段进草稿提示、并计入缓存键，保证带反馈重试不命中首次缓存）。
- 【minor】死代码清理：repair.py 删 self.ocr（只存不读）、模块级 logger（未用）、self.stats（无读者）；judge.py 删 self.stats（无读者）。RepairAgent 构造签名 `(ocr_client, settings)` 保留以兼容主缝注入（ocr_client 不再留存，重读由图 ingest 统一执行）。
- 新增 tests/test_judge_reextract_feedback.py 7 条：裁判启用路径 4（真 LlmJudge+monkeypatch 假 ChatOpenAI：判负真判负/判正放行/抛错退化放行/未启用不建链）+ reextract 主缝 3（强制重 OCR+gate_reason 进反馈/耗尽 PENDING_REVIEW/首次放行无反馈）。

**2. 结果**
- 全量 pytest 133 passed（126 基线一行未改 + 7 新），junitxml 核验 tests=133 failures=0 errors=0 skipped=0。
- 离线 eval 7/7，recall/precision/operation/posting/field_accuracy 全 1.0。
- commit 92b8696（实现）+ 本票文件提交；分支 ticket-08，未 merge，留 lead。

**3. 哪步顺**
- 裁判修复先写"真 LlmJudge+假 ChatOpenAI"回归（monkeypatch 模型构造层，提示词/建链仍走真实 _build），先红（`'NoneType' object has no attribute 'invoke'`）后绿，一行修复即钉住启用路径。
- reextract 用"手写最小 PDF 纯文本层"做探针：首读走文本层不 OCR、force_ocr 必 OCR，据此把"换路重读"变成可观察断言（OCR 调用次数），主缝测试一次落地。

**4. 哪步卡 + 根因**
- 裁判提示词断言：`str(ChatPromptValue)` 是 repr、换行被转义成 `\\n`，导致"文本在提示词里"的子串断言假红。根因：repr 不等于渲染文本；改 `.to_string()` 取真实拼接内容。
- reextract 反馈为何要单独存 repair_feedback：gate 放行时返回 `gate_reason=None` 会清空状态里的原因，若 extract 直接读 gate_reason 则重试成功后拿不到反馈。根因：gate_reason 语义是"最近一次不放行的原因"，放行即清空；故在 repair 节点（仍持有失败原因时）留存副本。

**5. 约定检查**
- 现有测试一行未改（git diff 仅新增 test_judge_reextract_feedback.py）；顶层六字段/operation 四值/OCR 契约/费用名字典不动；重试≤2、耗尽→PENDING_REVIEW 由原条件边把关未改。
- 改动严格限 graph/judge.py、graph/repair.py、graph/graph.py 的 ingest/repair/extract 路由区、graph/state.py（+1 字段）、抽取反馈接入点 document_extractor/llm_extractor；【未碰 graph/agents.py】（impl-07 在改）。
- 裁判默认 judge_enabled=False 保持；reextract force_ocr 走 OCR 网关不花 LLM key，gate_reason 仅喂 LLM 草稿（草稿可选）。注释 Google 风格中文、UTF-8 无 BOM；脚本输出无 ⚠️/✓；副产物集中 .work/ 用完自删。
- 两项行为变更（裁判修复/重抽反馈真生效）均已获用户批准（A），未遇新的需拍板决策。

**6. 下一票注意**
- 裁判启用路径已被真 LlmJudge 回归钉住；后续若改 _build 提示词/模型参数，注意 judge() 仍须经 _build() 取链，勿退回直调 self._chain。
- reextract 反馈只作用于主单据 LLM 草稿，附件抽取不带反馈；feedback 计入 LLM 缓存键，带反馈重试与首次是不同缓存条目。
- RepairAgent 不再持有 ocr/stats；若后续要统计补救次数，需接真实读者（如 pipeline 汇总）再加，勿回退成无读者死代码。

**坑/债/改进**
- 坑：`str(ChatPromptValue)` 返回 repr（换行转义），断言提示词内容要用 `.to_string()`。
- 债：reextract 的"换路"目前对 PDF/图片统一强制重 OCR；对文本层本就干净的 PDF，重 OCR 未必比文本层更准，属"换路重试"的既定语义（用户拍板 A），非本票扩大。
- 改进：judge/repair 的 stats 已删；如需可观测性（裁判调用数/补救策略分布），应在 pipeline 汇总接真实读者后再加，避免复现无读者死代码。
