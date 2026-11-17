# 05: audit_trail 补齐 + 尾巴交叉验证

**What to build:** 闭合两个软缺口并装上最后一道网。audit_trail 增补数量/单价/小计/税额与原文片段（顶层六字段不动）。死规则尾巴落账前对钱关键项做确定性交叉验证：金额重算（单价×数量/明细加总）、匹配用认亲工具复查、方向用四要素规则复查（复用现有校验/认亲/过账代码），再结合校验 issues 与 agent 自标低置信；任何一项对不上或不可信 → PENDING_REVIEW。保证 agent 的错提案进不了总账。

**Blocked by:** 04

**Status:** done (commits bd1f5e2+3c22530, 106 passed, merged)

- [x] audit_trail 含数量/单价/小计/税额/原文片段，顶层六字段不变
- [x] 尾巴落账前对金额/匹配/方向做确定性交叉验证（复用现有代码）
- [x] 交叉验证不一致或不可信 → PENDING_REVIEW，错提案不落账
- [x] 端到端测试覆盖"agent 提案被尾巴拦下"场景，现有测试不回归

## Retro

**1. 做了什么**
- audit_trail 增五字段 quantity/unit_rate/subtotal/tax/source_snippet（models.py:209-217 定义、pipeline_utils.py:247-251 装配，确定性/agent 两路共用）；顶层六字段不动。subtotal/tax 取单据级 doc.subtotal/doc.tax，source_snippet 取 charge.source_line 或 provenance.snippet。
- 新增 shipping_billing/graph/crosscheck.py：tail 落账前复用 validators（金额重算）、matcher.match_charge（认亲复查）、posting.decide_posting（四要素方向复查），结合 agent 自标低置信（<0.5）；任一项对不上/不可信 → 降级 PENDING_REVIEW（改写 operation、理由入 ai_reasoning/warnings、清空 updates），_apply_to_ledger 不命中任何落账分支。
- graph/graph.py 仅 tail 区域接线（import :30 + else 分支 :169 插 crosscheck_assessment 于 _apply_to_ledger 前），ingest/synthesize 不动。
- 新增 tests/test_tail_crosscheck.py 4 条（自带假 OCR/假草稿 LLM/假 agent LLM，离线）。

**2. 结果**
- 全量 pytest 106 passed（102 基线一行未改 + 4 新），junitxml 核验 0 失败 0 错误 0 跳过。
- 离线 eval 7/7，recall/precision/operation/posting/field_accuracy 全 1.0。
- commit bd1f5e2（实现）+ 本票文件提交；分支 ticket-05，未 merge，留 lead。

**3. 哪步顺**
- 先写校准探针跑确定性流水线逐条打印 issues/方向/金额复查，确认真实 7 样例无钱关键 warning 码、方向复查"仅确定矛盾才拦"不误伤 → 三项复查设计一次落地，TDD 红→绿顺。
- 全程复用现有 validators/matcher/posting，不新造校验逻辑，改动面小（3 文件 +23/-2，另 2 新文件）。

**4. 哪步卡 + 根因**
- 低置信阈值校准：现有 test_agent_keeps_unknown_charge_name_no_fabrication 用 confidence=0.5 期望 CREATE，阈值必须严格 <0.5（不能 ≤）否则基线红。根因：基线测试的边界值本身就是契约。
- 认亲复查时机：探针在流水线跑完后重跑 match_charge，CREATE 行已落账导致假性不一致；确认复查必须在 tail 时跑（synthesize→tail 间活台账未变），确定性路径必然自洽。根因：认亲读活台账，探针时机与 tail 时机不同。
- 模块归属：本想放 validation/，但 validation 已 import pipeline_utils，crosscheck 放那会循环；改放 graph/（graph 已依赖 validation/matching/finance/pipeline_utils，无环）。

**5. 约定检查**
- 现有测试一行未改（git diff --name-only -- tests/ 为空）；顶层六字段/operation 四值/OCR 契约/费用名字典不动。
- graph.py 改动严格限 tail 区域 + import 行；新逻辑在新模块 crosscheck.py。
- 注释 Google 风格中文、UTF-8 无 BOM；脚本输出无 ⚠️/✓；副产物集中 .work/ 用完自删。
- 未遇需用户拍板决策（未改行为钉子测试/未改对外契约/未扩范围/未删测试）。

**6. 下一票（票06）注意**
- 交叉验证已对两路一致生效；票06 改综合 agent 默认启用/必需 key 时，改默认行为须显式更新评测预期（本票缺省确定性故 eval 仍 7/7）。
- _AGENT_LOW_CONFIDENCE=0.5 严格 <；票06 若调 agent 置信度分布，注意别误伤 0.5-CREATE 基线。
- 方向复查"仅确定矛盾才拦"（规则拿不准不覆盖 agent 疑难推理）；若票06 要规则在疑难 case 覆盖 agent，属行为变更需拍板。
- ChatAgentLlm 已就位（票04），票06 直接接。

**坑/债/改进**
- 坑：rules.extract_totals 命中 TOTAL 模式即返回 subtotal/tax=None，真实样例全为 None；仅"有 SUBTOTAL/TAX 且无 TOTAL"时非空。新字段在真实样例为 None 属正常，非 bug。
- 债：_MONEY_ISSUE_CODES 含 AMOUNT_MISSING/CURRENCY_INVALID（上游已阻断），属有意独立复核、不依赖上游是否已拦，略冗余。
- 改进：方向复查当前规则拿不准不覆盖；如需更强方向把关属行为变更，另行拍板。