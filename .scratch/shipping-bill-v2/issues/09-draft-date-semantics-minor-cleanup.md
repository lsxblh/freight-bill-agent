# 09: 草稿日期语义约束 + 审查剩余安全 minor 清理

**What to build:** 用户拍板（剩余质量债选 A）+ 审查剩余安全 minor。
1. 【A·草稿日期语义】LLM 草稿提示词（extract/llm_extractor.py）约束单据日期语义：document_date 取签发/到货/账单日期（invoice date / bill date / arrival date），**不取 release/notice/RELEASE DATE 等通知类日期**。根因：PDF1 规则抽不出日期→真 LLM 草稿补位 RELEASE DATE 2026-10-19→merge.py:45-47 无条件采纳→与台账 10-26 不一致→pipeline_utils 判 overwrite→UPDATE（期望 NO_CHANGE）。
2. 【缓存正确性·必做】LLM 草稿缓存键须含提示词指纹/版本，使提示词改动自动失效旧草稿缓存；否则 e2e 会复用旧提示词的 stale 草稿（如 e9941e79）看不到改进。验证 e2e 重跑确实重算 PDF1 草稿。
3. 【审查 minor·安全清理】graph/agents.py:76-77 Proposal.charge_item/standardized 死字段（只赋值无读取）删除（含 :153-154/:254-255 赋值）；:345-347 装配异常回落 record_chain 时在 audit_trail 留痕（如 warnings 追加"agent 装配异常回落确定性"），使生产可区分真 agent/静默回落（additive，不改六字段）。补离线测试钉住回落留痕。

**Blocked by:** 07, 08

**Status:** done (commit a729566+docs, 待 lead merge; 离线 137 绿/eval 7/7, e2e operation 0.8571/posting 1.0, PDF1 恢复 NO_CHANGE)

- [x] 草稿提示词含日期语义约束（取签发/到货/账单日期，不取 release/notice）
- [x] LLM 草稿缓存键含提示词指纹（提示词改动失效旧缓存）
- [x] agents.py 死字段删除 + 装配异常回落 audit 留痕 + 离线回归测试
- [x] 现有测试一行不改；全量离线 pytest 绿；离线 eval 7/7 不回归
- [x] 真模型 e2e 复跑：PDF1 On Carriage 恢复 NO_CHANGE（operation 目标 1.0），报 before/after
- [x] 顶层六字段/operation 四值/OCR 契约/费用名字典不动；merge"规则为准/LLM 补位"语义不改（只约束草稿取哪个日期）
- [x] Retro 六问写本文件 ## Retro 并附回报

**范围限制:** 只改 extract/llm_extractor.py（提示词+缓存键）、graph/agents.py（死字段+回落留痕）、新增测试。不改 merge.py/pipeline_utils.py 的 overwrite/enrich 语义（那是候选 B/C，用户未选）。不要 merge。

**注意:** e2e 约 20-30 分钟，用 job_output wait 阻塞保持 turn 活跃；key 只进环境变量不入仓库/日志；e2e 非硬闸门、真模型有波动，不擅自改断言/减样例。

## Retro

**做了什么**
- 只改 2 个源文件 + 新增 2 个测试文件（现有测试一行未改）：
  1. `shipping_billing/extract/llm_extractor.py:61` SYSTEM_PROMPT 加一句日期语义约束+反例：document_date 只取单据本身的签发/账单/到货日期（invoice date / bill date / arrival date）；release/notice/RELEASE DATE 等通知类日期不是单据日期，不得填入；反例 "RELEASE DATE: 10/19/2026" 是放货通知日期；没有签发/账单/到货日期时返回 null。只约束草稿取哪个日期，merge/pipeline_utils 的 overwrite/enrich 语义未动。
  2. `shipping_billing/extract/llm_extractor.py:145-150,154` 缓存键加提示词指纹：sha256(SYSTEM_PROMPT+USER_PROMPT+schema_text) 前 12 位 hex 前置进 key_material（指纹|model|filename|clipped）。每次调用时计算——提示词或输出 Schema 任一改动→键变→旧草稿缓存自动失效；提示词不变时键稳定，缓存行为与票09 前一致。feedback 仍按票08 语义计入键。
  3. `shipping_billing/graph/agents.py:117-133` 删 Proposal.charge_item/standardized 死字段（只赋值无读取）及 docstring 条目；`:188` 删 propose() 里因此孤立的 charge_item 局部变量；两处 Proposal 构造（`:201-207`/`:300-306`）各删 2 个赋值。agent 提示词未动（finish 仍输出 charge_item，仍 advisory 丢弃，与票09 前行为一致）。
  4. `shipping_billing/graph/agents.py:392-397` 装配异常回落留痕：except 分支 record_chain.invoke 后往 audit_trail.warnings 追加 "agent 装配异常，回落确定性链"（additive，不改六字段）。
- 新增测试 4 条（先红后绿/行为钉子）：
  - `tests/test_agent_fallback_audit.py` 1 条：假 agent LLM 返回畸形 finish 提案（reasons=42 非列表）→ ReAct 循环外解析抛 TypeError → 装配回落确定性链 → PENDING_REVIEW + warnings 留痕 + agent_role/agent_confidence 均 None。图片样例是 ZIM 发票（双方非我方），确定性链判 PENDING_REVIEW 而 agent 路径会落 CREATE——operation 本身即证明输出出自哪条链。
  - `tests/test_llm_cache_fingerprint.py` 3 条：提示词不变→第二次命中缓存；提示词改动→重算+两个不同键文件；按旧键公式（无指纹）预写 stale 草稿→不被读取、重算。只把 _build 换假链，不联网。

**结果**
- 离线：137 passed（基线 133 一行未改 + 新增 4，junit tests=137 failures=0 errors=0 skipped=0）；eval 7/7 全 1.0。
- 真模型 e2e（qwen3.8-flash @ tokenrhythm.studio，OCR cache 回放，4 样例，18m20s，EXIT_CODE=0）：recall 1.0 / precision 1.0 / **operation 0.8571 / posting 1.0**；7 条 agent_confidence 全非空（agent 真激活，无静默回落）。
- before/after：基线（票07）operation 0.8571，唯一差异=PDF1 On Carriage NO_CHANGE→UPDATE；本次 **PDF1 On Carriage 恢复 NO_CHANGE**（日期约束生效），差异转移到 PDF2 Handling 35 NO_CHANGE→UPDATE。operation 数值持平（6/7），posting 1.0 不变。
- 缓存正确性证据：PDF1 新草稿键 51cc50a2478d6a22...（指纹 4c4c9b670333），旧 stale 键 e9941e79... 不存在；新草稿 document_date=None（模型遵守约束不取 RELEASE DATE 10/19，无签发/账单/到货日期时返回 null）→ merge 不补日期 → 无 overwrite → NO_CHANGE。
- 新差异根因（离线复现：规则+缓存真草稿→merge→compute_field_changes）：本次真草稿把 PDF2 recipient 误读为 NEPTUNE SHIPPING LIMITED（规则 recipient=None，真实接收方 CIMC WETRANS INTL LOG CO LTD），`shipping_billing/extract/merge.py:53-55` "LLM 补位"无条件采纳 → recipient overwrite → Handling 35 翻 UPDATE（Handling 65 因金额 overwrite 本就是 UPDATE，被掩盖）。与 PDF1 日期问题同机制（草稿补位质量×融合无条件采纳），属候选 B/C（用户未选）；与本票日期约束无关——PDF2 日期由规则抽出 2026-10-24，不走 LLM 补位。该草稿同时误读 issuer 为 Pilot Aiship Tech（票07 同款，被融合"规则为准"吸收）。

**哪步顺**
- 动手前探针读 PDF1 原文日期分布（10/26 在 PICKUP # 下、10/19 是 RELEASE DATE/通知时间戳、10/20-21 是正文截止日期）并复现旧缓存键=e9941e79 → 约束措辞按"只取单据本身的签发/账单/到货日期+无则 null"设计，e2e 一次验证生效。
- TDD 两轮先红后绿：回落留痕测试先红（warnings=[]）；缓存指纹测试先红（2 红 1 绿）；最小实现转绿，离线 137 + eval 7/7 零回归。
- e2e 证据链：--basetemp 把 tmp 落 .work/e2e_tmp，跑后脚本核验新键存在/旧键 absent/草稿 date=None；新差异用离线 merge 复现定位，不必二次联网。

**哪步卡 + 根因**
- 卡1：票面"注入会抛异常的假 agent LLM→回落留痕"——complete() 直接抛异常会被 _react_loop 内部 except 捕获（回落确定性提案，仍走 agent 装配，到不了 record_chain 回落）。根因：两层回落结构（循环内 LLM 故障 vs 循环外装配异常），票面措辞对应外层。解法：假 LLM 返回畸形 finish 提案（reasons=42），循环外 reasons 解析抛 TypeError 命中外层 except；测试 docstring 写明机制。
- 卡2（剩余差异）：operation 目标 1.0 未达——PDF1 恢复但 PDF2 Handling 35 翻转。根因不在本票改动：真草稿 recipient 误读 + merge 无条件补位。属真模型草稿质量波动×候选 B/C 债，超出本票范围（用户只批了 A 日期约束）；如实记录，不改断言、不减样例、不重跑刷指标。

**约定检查**
- 顶层六字段/operation 四值/OCR 契约/费用名字典：未动；merge.py/pipeline_utils.py 零 diff；agent SYSTEM_PROMPT（票07 版）一字未动。
- 现有测试一行未改（git 仅 2 源文件+2 新测试文件）；注释中文 Google 风格；UTF-8 无 BOM（逐文件核验）；脚本输出无 ⚠️/✓ 符号；key 只进进程环境变量（主 checkout .env 读入，不入仓库/提交/日志）；副产物集中 .work/ 用完自删。
- e2e 断言未改、样例未减、无端点超时回落（18m20s 正常跑完 EXIT_CODE=0）；只在 ticket-09 提交，未 merge。

**下一票注意**
- 剩余质量债转移到 PDF2 草稿 recipient 补位：候选 B/C（融合层补位降级/规则缺失不补位）仍待拍板；若继续提示词 A 路线，可加 recipient 语义约束（取被收费的实际接收方，不取签发方自身名称）——属新行为变更需拍板。
- 提示词指纹覆盖 SYSTEM/USER 模板+输出 Schema：改 LlmDocument 字段描述也会失效草稿缓存；agent SYSTEM_PROMPT（agents.py）不在草稿缓存指纹范围（agent 无响应缓存，票06 候选改进仍开放）。
- 回落测试区分两层：循环内 complete() 异常→确定性提案（agent_confidence 非空）；循环外装配异常→record_chain（agent_confidence=None + warnings 留痕）。生产监控可用"agent_confidence=None 且 warnings 含装配异常"识别静默回落。
- e2e 用 --basetemp=.work/e2e_tmp 便于证据检视（pytest 默认 tmp 在系统目录难找）；跑后核验脚本+离线 merge 复现模式可复用。

**坑/债/改进（供 lead 追加 docs/TECH_DEBT.md）**
1. `[质量债·剩余·需拍板]` e2e operation 0.8571：PDF1 日期问题已修（A 生效，恢复 NO_CHANGE），新差异 PDF2 Handling 35 NO_CHANGE→UPDATE，根因=真草稿 recipient 误读为 NEPTUNE（规则 None）→ `shipping_billing/extract/merge.py:53-55` 无条件补位 → recipient overwrite。与 PDF1 日期同机制（草稿补位质量×无条件采纳），候选 B/C 待拍板；提示词层 recipient 约束亦可选（新行为变更）。
2. `[经验]` 两层回落结构：complete() 异常被 ReAct 循环内捕获（确定性提案），循环外装配异常才到 record_chain 回落；测外层要用畸形提案（reasons 非列表）而非抛异常的 complete()。
3. `[经验]` 提示词指纹=sha256(SYSTEM+USER+schema)[:12] 逐调用计算、前置缓存键；覆盖模板与输出结构改动，提示词不变时键稳定；旧格式键 stale 文件天然绕过（单测钉住）。
4. `[观察]` PDF2 真草稿 issuer 误读（Pilot Aiship Tech）再次出现，融合"规则为准"吸收；recipient 无规则值防守则误读直穿——**无规则防守的补位字段是草稿质量波动重灾区**（日期/recipient 先后中招）。
5. `[流程]` e2e --basetemp 落证据于 .work/e2e_tmp（metrics+草稿缓存），跑后脚本核验新旧缓存键+离线 merge 复现差异根因，不必二次联网。