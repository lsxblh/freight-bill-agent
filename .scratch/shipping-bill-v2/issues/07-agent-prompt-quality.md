# 07: 综合 agent 提示词强化（operation 语义 + 方向 few-shot）

**What to build:** 用户拍板（质量债选 A）：真模型 e2e 暴露 operation_accuracy 0.4286 / posting_accuracy 0.5（NO_CHANGE↔UPDATE 语义混淆、debit note 方向推理弱）。强化综合 agent 的 synthesize 提示词：①明确 operation 四值语义与判定依据（CREATE=台账无认亲；UPDATE=认亲命中且字段有变；NO_CHANGE=认亲命中且无实质变化；PENDING_REVIEW=不可信/冲突），给对照 few-shot；②强化 AR/AP 方向推理（记账主体+双方+单据类型+Credit/红字），给 debit note / credit note 方向 few-shot。只改提示词与 few-shot，不改工具/死规则/契约。

**Blocked by:** 06

**Status:** done (commits 51a780a+2c17741, merged; 离线 133 绿, e2e operation 0.8571/posting 1.0)

- [x] synthesize 提示词含 operation 四值语义+判定依据+对照 few-shot
- [x] 提示词含 AR/AP 方向推理要点+debit/credit note 方向 few-shot
- [x] 现有测试一行不改；全量离线 pytest 绿；离线 eval 7/7 不回归
- [x] 真模型 e2e 复跑：operation/posting 较基线 0.4286/0.5 有实质提升（报 before/after 与差异明细）
- [x] 顶层六字段/operation 四值/OCR 契约/费用名字典不动；crosscheck 兜底仍生效
- [x] Retro 六问写本文件 ## Retro 并附回报

**基线（票06 e2e，qwen3.8-flash，4 样例）:** recall 1.0 / precision 1.0 / operation 0.4286 / posting 0.5；6 处差异集中在 NO_CHANGE↔UPDATE 与 debit note 方向，2 条被 crosscheck 拦成 PENDING_REVIEW。

**注意:** 提示词属行为变更（用户已批准 A）；e2e 非硬闸门、真模型有波动，复跑报指标不擅自改断言；e2e 约 25-30 分钟，用 job_output wait 阻塞保持 turn 活跃；key 只进环境变量不入仓库/日志。

## Retro

**做了什么**
- 只改 `shipping_billing/graph/agents.py:40-114` 的 SYSTEM_PROMPT（1 文件 +61/-5），工具/死规则/crosscheck/契约/字典/现有测试一律未动。
- 强化点五处：
  1. 输出边界：agent 只产角色候选+标准名+理由+置信度；AR/AP 由 posting_from_role 死规则落出、operation 由尾巴死规则算出——不输出 operation，也不因拿不准 operation 而降低置信度（confidence=对角色判断与事实的把握）。
  2. operation 四值语义+判定依据：CREATE=四级认亲全未命中；UPDATE=认亲命中+改写台账已有非空值；NO_CHANGE=认亲命中+无实质改写（补全空字段不算更新）；PENDING_REVIEW=重复/阻断/角色判不出/冲突/低置信。认没认出以 match_charge 工具事实为准，实质变化由尾巴逐字段比对。
  3. 四要素角色推理步骤：公司名模糊比对（别名/分支后缀视为同一公司）→主体≈签发方→issuer；主体≈接收方→recipient；都不匹配（别的公司/邮箱/人名）→null 绝不猜。**四要素是推理输入不是齐全清单——接收方缺失但签发方明确是我方时 role=issuer 仍成立**（PDF2 recipient=None 被误判"四要素不全"的根因修复）。
  4. 置信度标定：事实明确 0.85~0.95、轻微歧义 0.7~0.85、判不出→null+低置信；事实明确时不低于 0.5（crosscheck 把 <0.5 视为不可信整条拦下）。
  5. 9 个对照 few-shot：UPDATE(40→65)/NO_CHANGE(金额一致+同名第二条)/NO_CHANGE(补全不算更新)/CREATE/Debit Note 接收方缺失仍可判/Credit Note 反转归死规则/红字行反转归死规则/双方别的公司→null/双方邮箱→null。
- match_charge 工具描述补 matched_line 返回字段（模型可见命中台账行的金额等事实，支撑 NO_CHANGE vs UPDATE 推理）。

**结果**
- 离线：126 passed/1 deselected（junit tests=126 failures=0 errors=0 skipped=0），现有测试一行未改；eval 7/7 全 1.0。
- 真模型 e2e（qwen3.8-flash @ tokenrhythm.studio，OCR cache 回放，4 样例，21m06s，passed）：recall 1.0 / precision 1.0 / **operation 0.8571（基线 0.4286）/ posting 1.0（基线 0.5）**；7 条记录 agent_confidence 全非空（agent 真激活，无静默回落）。
- before/after 差异明细：基线 4 处 operation 错（2 条 debit note role=null→posting=null 被拦成 PENDING_REVIEW + On Carriage/Handling 的 NO_CHANGE↔UPDATE 混淆）→ 本次仅剩 1 处：PDF示例账单1.pdf On Carriage Charge 期望 NO_CHANGE 实际 UPDATE。posting 2/4→4/4（4 条 NEPTUNE 费用全 AR，证明 debit note role=null 已修复）；Handling 35 NO_CHANGE 也恢复正确。
- 剩余 1 处根因（证据：e2e 草稿缓存 e9941e79）：规则抽不出 PDF1 日期（document_date=None），真 LLM 草稿补位 2026-10-19（取了原文 RELEASE DATE 10/19/2026），`shipping_billing/extract/merge.py:45-47` "LLM 补位"采纳，台账 BILL-2002 日期=2026-10-26 → `shipping_billing/pipeline_utils.py:102-117` 判 overwrite → UPDATE。**属草稿层补位策略问题，与 synthesize 提示词无关**（doc 在 agent 拿到前已融合，operation 是确定性计算），超出本票范围，列候选改进需拍板。

**哪步顺**
- 先读透死规则链（assemble_record/_decide_operation/posting_from_role/crosscheck/merge）再动提示词：确认 operation 不是 agent 的输出，提示词杠杆在"角色判断+置信度标定"，few-shot 按根因设计（recipient 缺失/null 反例/置信度下限），e2e 一次复跑即实质提升。
- 离线测试不断言 SYSTEM_PROMPT 内容（grep 确认），提示词改动对 126 条离线测试天然安全。

**哪步卡 + 根因**
- 卡1：首次 e2e 启动约 2 分钟后被我杀掉重启——发现 few-shot 例2/3 提案 role=issuer 但事实未写"签发方=记账主体"，few-shot 内部不自洽可能诱导小模型"恒输出 issuer"。根因：写示例时只盯 operation 维度，漏了角色维度的自洽。教训：每条 few-shot 的事实必须完整支撑提案的每个字段。
- 卡2（剩余差异）：On Carriage NO_CHANGE→UPDATE 提示词修不了。根因：草稿 LLM 补位日期与台账不一致（取 release date 10/19 而非台账 10/26），融合策略"规则为准、LLM 补位"无条件采纳。属草稿层行为变更，需另拍板。

**约定检查**
- 顶层六字段/operation 四值/OCR 契约/费用名字典：未动；工具/死规则/crosscheck：未动（crosscheck 兜底仍生效：代码未改 + tests/test_tail_crosscheck.py 4 条绿）；现有测试一行未改。
- 只改 agents.py 的 SYSTEM_PROMPT 字符串；注释中文；UTF-8 无 BOM；脚本输出无 ⚠️/✓ 符号；key 只进进程环境变量，不入仓库/提交/日志；副产物集中 .work/ 用完自删。
- e2e 断言未改、样例未减；无端点超时回落（21m06s 正常跑完，EXIT_CODE=0）。

**下一票注意**
- 提示词属行为变更：离线 eval 7/7 仍是回归基线，e2e 数字是质量证据不是闸门（真模型有波动）。
- 剩余质量债转移到草稿层：LLM 草稿日期补位可把 NO_CHANGE 翻成 UPDATE（PDF1 取 release date）。候选修法（均需拍板）：①草稿提示词约束"单据日期取签发/到货日期，不取 release/notice 日期"；②融合层对 LLM 补位日期降级处理（改 compute_field_changes 语义，影响面大）；③规则缺失日期时不补位。都不在本票范围。
- PDF2 真草稿把 issuer 误读为 "Pilot Aiship Tech co., Ltd."（规则 NEPTUNE 正确），融合保留规则值+冲突 warning+置信度×0.8——本次 agent 在冲突 warning 下正确信任规则值；若后续模型被 warning 带偏可能回归，值得观察。
- e2e 时间预算：本次 21m06s（快于票06 的 25m34s），仍按 30 分钟+ 安排；长 job 必须 job_output wait 阻塞保持 turn 活跃，不能启动后台就结束 turn。

**坑/债/改进（供 lead 追加 docs/TECH_DEBT.md）**
1. `[质量债·剩余·需拍板]` 提示词强化后 e2e operation 0.8571/posting 1.0。唯一剩余：PDF1 On Carriage NO_CHANGE→UPDATE，根因=草稿 LLM 补位日期 2026-10-19（release date）vs 台账 2026-10-26，`shipping_billing/extract/merge.py:45-47` 无条件采纳→overwrite。修法候选：草稿日期语义约束/融合补位降级/不补日期，均属行为变更需拍板。
2. `[经验]` 提示词杠杆边界：operation 由尾巴死规则算出（`shipping_billing/pipeline_utils.py:124-155` _decide_operation），agent 只能经 role→posting 与自标置信度影响它；"教 operation 语义"的价值在防 agent 因 operation 不确定而降置信/误判角色，不在让 agent 输出 operation。
3. `[经验]` few-shot 内部自洽：每条示例的事实必须完整支撑提案每个字段（本票例2/3 补"签发方=记账主体"后重启 e2e，保证被测提示词=最终提交）。
4. `[观察]` PDF2 真草稿 issuer 误读（Pilot Aiship Tech），融合"规则为准"正确兜底；草稿质量波动由融合层吸收，agent 应在冲突 warning 下信任规则值。
5. `[流程]` e2e 证据链：e2e_metrics.json+草稿缓存（llm_cache_dir）可定位剩余差异根因；本票副产物按约定清理，关键数字与证据已记入本 Retro。