# 04: 综合 agent + 工具层（ReAct 工作台）

**What to build:** 第三段换成综合 agent：拿两份草稿+原件+台账+记账主体，以 ReAct 工具循环自主决定何时调用工具，产出完整提案（reconciled 字段/标准名/匹配结果/角色候选）+理由+置信度，只提议不落账。工具层=认亲（四级匹配，确定性，返回"事实"）/读原文/查台账/查费用名字典，全部由现有模块包一层。归一化（字典权威、未收录保留原名+低置信）与 AR/AP 四要素推理进 agent；"角色→AR/AP"落账那一下仍留死规则。LLM 草稿可选、读失败留空不阻断。用假 LLM 离线验证。

**Blocked by:** 02

**Status:** ready-for-agent

- [x] 综合 agent 以 ReAct 循环自主调工具，产提案+理由+置信度，不直接落账
- [x] 四级匹配做成确定性工具被 agent 调用，返回认没认出/哪条/第几级
- [x] 归一化字典权威、未收录保留原名+低置信；AR/AP 推理在 agent、落账那一下在死规则
- [x] LLM 草稿可选，读失败留空不阻断
- [x] 理由/匹配规则/置信度写入 audit_trail
- [x] 注入假 LLM 的端到端测试通过，现有测试不回归

## Retro

（retro 技能为用户手动触发专用（disable-model-invocation），模型/子代理不可加载，按 lead 预案以六问 inline 复盘。）

1. **做了什么**：第三段 synthesize 换综合 agent + 确定性工具层，共 7 文件。①新增 shipping_billing/graph/agents.py：ComprehensiveAgent.propose(:119)/_react_loop(:162)（MAX_ROUNDS=6，模型出 JSON 动作→执行工具→观察回喂，finish 产提案，超限/异常回落确定性），Proposal dataclass(:61)，synthesize_charge(:305) 入口（注入启用 agent LLM 走 ReAct+死规则装配，未注入沿用票02 record_chain），ChatAgentLlm(:263) 真实适配器（懒构造 ChatOpenAI 温度0，受控面 .enabled/.complete），_annotate_agent(:296) 把理由/自标置信度/角色写进 audit_trail；②新增 shipping_billing/graph/tools.py：AgentTools(:49) 四确定性工具 match_charge(:78)/read_raw(:95)/query_ledger(:111)/lookup_charge_name(:129)，全由现有模块包一层只返回事实，dispatch(:142) 分发；③shipping_billing/finance/posting.py 抽 _finalize_direction(:99) 共用"角色→AR/AP"死规则，新增 posting_from_role(:160)（采用 agent 角色候选，角色缺失/非法转人工），decide_posting 行为逐字不变；④shipping_billing/graph/graph.py 仅 synthesize 区域：import + docstring + record_chain.invoke 一行换 synthesize_charge(:130)，ingest 路由与 tail 不动；⑤shipping_billing/models.py:204 AuditTrail 增 agent_confidence/agent_role（确定性路径保持 None）；⑥shipping_billing/pipeline.py:71 BillingAgent 增 agent_llm 注入缝（缺省 None）；⑦新增 tests/test_comprehensive_agent.py 6 条主缝测试（假 agent LLM 离线）。
2. **结果**：全量 102 passed（基线 96 一行未改 + 新增 6），junit 核验 tests=102 failures=0 errors=0 skipped=0。实现 commit 3921fa0（ticket-04，基于集成尖端 0a1fda6），票文件勾选+Retro 另起一提交，均未 merge。默认 agent_llm=None 走确定性链，端到端 7 条金断言与 eval 行为不变。
3. **哪步顺**：沿用票01 教训——写假实现/装配前先读真实签名与输出装配代码（assemble_record 的 payload 形状、validate_charge(charge,doc)、MatchResult 全默认字段、Settings.llm_* 字段、BillLine.identity），假 agent LLM 受控面与死规则装配一次对齐，主循环零返工。把 decide_posting 尾部抽成 _finalize_direction 共用，6 条直测 decide_posting 的测试逐字仍绿。synthesize 保持逐条、tail 完全不动，逐条落账交错钉子 test_same_name_charges_interleave_with_ledger 在 agent 路径上一次复现绿（同名两条 CREATE→UPDATE）。
4. **哪步卡 + 根因**：卡点在设计岔路而非运行时红绿。①复用 llm 缝还是新增 agent_llm——根因：假草稿 LLM 只实现 .extract 返回 LlmDocument，驱动不了 ReAct（需 .complete(prompt)->str），故新增独立 agent_llm 缝，缺省 None 保定确定性行为不变；②"两份草稿"怎么给 agent 又不越界改 extract 节点（票03 并行改 ingest，我被限死在 synthesize 区域）——根因：extract 已把规则+LLM 融合成一份 doc（method/warnings 标注 LLM 补充与两稿冲突），遂让 agent 拿融合草稿 + read_raw 回原件查证，而非字面塞两个草稿对象，既满足"拿两份草稿+原件"又不碰 extract。读签名在先，无运行时阻塞。
5. **约定检查**：现有测试一行未改（git diff 仅 posting/graph/models/pipeline + 3 新增文件）；对外契约/六字段/operation 四值/OCR 网关契约/费用名字典未动；graph.py 改动严格限 synthesize 区域，ingest 路由与 tail 未碰（与票03 无冲突）；综合 agent LLM 可注入、测试全用假实现绝不联网（.env 真 key 不进测试）；注释 Google 风格中文、新文件 UTF-8 无 BOM；副产物全在 .work/ 用完已删；未请求提权；只在 ticket-04 提交、未 merge。
6. **下一票注意**：票05 交叉验证插 tail 的 _apply_to_ledger 之前——agent 路径与确定性路径都经 assemble_record→tail，交叉验证对两路一致生效；audit_trail 新增 agent_confidence/agent_role（确定性路径 None），票05 可据此区分来源。票06 接 ChatAgentLlm（真实适配器已就位 .enabled/.complete）："必须配 key、无 key 报错、默认启用"是票06 行为变更——本票 agent_llm 缺省 None 走确定性，故 eval 仍 7/7，票06 改默认启用时须显式更新评测预期。agent 提议的 charge_item 仅 advisory（输出标准名仍字典权威，取草稿 charge.charge_item）；若票05/06 要把 agent reconciled 名写进 audit 属新增范围。ReAct MAX_ROUNDS=6，不收敛/异常回落确定性提案，单条费用失败不拖垮其余。