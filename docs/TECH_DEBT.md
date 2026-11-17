# 技术债 / 踩坑 / 改进方向台账

活文档。每完成一票，把该票复盘里的坑、债、改进方向追加到对应小节；用户之后一起处理。
状态标记：`[已修]` 已解决但留教训 / `[待处理]` 欠的债 / `[候选]` 改进方向 / `[需拍板]` 必须用户决定。
路径一律带目录前缀，便于 grep。测试基线随票增长（票02 后 96）。

## 一、踩过的坑（已修，留教训）

- `[已修]` 票00 PDF 费用表粘连：pypdf plain 把 PDF2 费用表粘成 269 字符整行，丢 3 条费用 + 单号贪心吞尾 `DC-108478D/C`。修法是**按页**自适应回退 layout（`shipping_billing/ingest/pdf_loader.py:21-37,56-57`，`_FUSED_LINE_LIMIT=220`+`_FUSED_CHARGE_SIGNAL`），不能全局切 layout——PDF1 在 layout 下反而金额粘描述。教训：回退策略要按页判定，别一刀切。
- `[已修]` 票00 签发方粘连：`BALANCE DUE TO` 与 company 粘成 `TONEPTUNE`，`shipping_billing/extract/rules.py:235-236` 把 `TO\s+` 改 `TO\s*`；`rules.py:167-169` `_trim_company` 先剥尾部"币种+金额"再剥币种/括号。教训：OCR/文本粘连要在正则留零宽余地。
- `[已修]` 票01 假实现签名对不上：第一版假 LLM 凭印象写，对不上真实 `.extract(text, *, filename, kind)`。教训：**写假实现/金断言前先读被调方真实签名与输出装配代码**。受控面：假 LLM=`.enabled/.extract/.stats`；假 OCR=`.settings/.process_file/._load_cache(sha)/.stats`（pdf_loader 直接调 `_load_cache`）。LLM 合并提示落 `audit_trail.warnings` 而非 `ai_reasoning`。
- `[已修]` 票02 langgraph 导入：1.2.11 的 `CompiledStateGraph` 顶层导不到，须 `from langgraph.graph.state import CompiledStateGraph`。
- `[已修]` 票02 失败隔离测不到：现有 loader 容错太彻底，自然样本触发不了 ingest 异常，须 mock 注入 `load_document` 才能验证单文件失败隔离。教训：容错好的代码要靠注入故障测边界。
- `[已修]` 票01 循环依赖：`agent.py`↔`extract/document_extractor.py` 靠延迟 import 硬躲；已把 `build_document_chain` 搬进抽取层，依赖单向 pipeline→agent→extract。

## 二、欠的债（待处理）

- `[待处理]` **交付**（题目硬要求）：git 无 remote、未推送、未打 zip、未发邮件（LUKA@PLTPLT.COM / pan@pltplt.com，标题"应聘渠道+姓名+应聘岗位"）。实现完成后必须做。
- `[已修]` 票05：audit_trail 补数量/单价/小计/税额 + 原文片段 inline；尾巴落账前钱关键项确定性交叉验证。
- `[已修]` 票06：综合 agent 必需 key 不降级 + 配置收口 + 新旧评测对比（真模型质量验证，离线假实现只证结构不证质量）。
- `[已修]` 文档债：`ARCHITECTURE.md` 已重写 v2 四段+综合 agent；`ACCEPTANCE.md`/`HARNESS.md` 的测试数(137)、LangChain 指向(extract/graph)、熔断表述(改失败上限+回落)已同步；`DESIGN_THINKING.md` 为业务/技术思考仍适用；`docs/adr/0001-...md` 已取代 11-07 旧 ADR。
- `[已修]` README 测试数过期：已更新为 137（随票增长的历史数字不再残留）。
- `[待处理]` `.gitignore` 未覆盖 `pytest-cache-files-*`/`.pytest_tmp` 残留副产物；交付 zip 前需清理或补忽略，且**确认 zip 不含 `.env`**（真 key 在内，`.gitignore:12` 已忽略但 zip 不走 gitignore）。
- `[待处理]` `llm_extractor.py:110` `ChatOpenAI(max_retries=N)` × `:142` 外层 `range(N+1)` 双层重试叠乘，失败时请求数放大；应只留一层。
- `[已修]` 死代码：已删 `harness/cache.py`(ContentCache/stable_key)、`resilience.py` CircuitBreaker、`ledger.py` by_reference、`agent.py` build_document_chain 兼容再导出、`pipeline_utils.py` 未用的 factors 解包。纠正：`candidate_lines` 实为活代码（graph/tools.py 在用），保留。

## 三、未来改进方向（候选，多数需拍板）

- `[候选]` 架构评审 8 候选中未做的：`rules.py`(912 行)拆五分区包；`BillLine` 26 字段构造收进 Ledger（`pipeline.py:246-268` 手工拷贝）+删死查询；`company_similarity` 从 `io_utils.py:216` 迁 `normalize/company.py` 语义谓词并统一散落阈值(0.7/0.8/0.85/0.86)；OCR 数字纠错/币种符号去重；loader 接口去 ocr_client 污染+`RawDocument.from_text` 工厂；factors 死输出回收。
- `[候选]` 逐条落账语义（synthesize⇄tail 循环，钉子 `tests/test_graph_skeleton.py::test_same_name_charges_interleave_with_ledger`）是正确性来源也是复杂度/性能来源；未来可考虑批处理+显式台账快照，但**必须改钉子测试=需拍板**。
- `[候选]` ReAct 轨迹细粒度断言：目前最高缝 `BillingAgent.run` 只断言外部行为，可加工具调用序列/提案理由的断言，提升 agent 可测性。
- `[候选]` 裁判/综合 agent 真模型选型与成本（`JUDGE_MODEL` 用哪个、token 预算），票06 评测时定。
- `[候选]` 统一 Python 环境：conda `code` vs 项目 `.venv`(uv 空壳 3.14)，双环境混乱，见拍板清单。

## 四、需用户拍板清单

- `[需拍板]` 逐条落账 vs 整单批处理（改行为钉子测试）。票04 已硬约束保持逐条；要改先问。
- `[需拍板]` 统一运行环境 conda code vs uv .venv（.venv 3.14 太新有兼容坑）。
- `[需拍板]` 架构评审 8 候选做哪些（"改动幅度>30%"诉求 vs 求职交付时间权衡）。
- `[需拍板]` 交付形式 zip vs GitHub 链接 + 邮件渠道/标题。
- `[需拍板]` 数量/单价/小计/税额是否进**主输出 JSON**（题目没强制；票05 默认进 audit_trail，进主字段需拍板）。
- `[需拍板]` 删除 harness 死模块 / ledger 死查询（删代码需确认）。
- `[需拍板]` 裁判/综合 agent 真模型选型与成本。

## 五、环境 / 工具坑（工作流）

- 沙箱拒 `0o700` 新建目录内写文件：pytest 加 `-p no:cacheprovider`，TMPDIR/TEMP/TMP 指 `.work/pytmp` 且内联 `os.mkdir` 强制 `0o777`，`PYTHONIOENCODING=utf-8`。
- 运行环境=conda `D:\miniconda\envs\code\python.exe`；项目 `.venv` 是 uv 空壳(Python 3.14.7，缺 openpyxl/pytest/langgraph)跑不了，不用。
- 副产物一律集中仓库根 `.work/`(已 gitignore)用完自删；脚本输出不带 ⚠️/✓ 符号(GBK 崩)。
- 完全权限+审批禁用后工具恢复；此前自动审查层报 `INVALID_REQUEST developer is not one of [...]` 随审批关闭消失。worktree 在仓库外建目录曾沙箱拒，完全权限后解除。
- `.env` `LLM_ENABLED` 显式 false 会盖掉"有 key 就开"的默认(`config.py:154`)；配了 key 仍关时先查这行。

## 六、逐票复盘追加

### 票04（综合 agent + ReAct 工具，102 passed，commits 3921fa0+def8360）
- `[已修]` 教训：假草稿 LLM 只实现 `.extract`(返回 LlmDocument) 驱动不了 ReAct(需 `.complete(prompt)->str`)，故新增独立 `agent_llm` 注入缝(`pipeline.py:71`)，缺省 None 保确定性行为不变。
- `[已修]` 教训："两份草稿"实际 extract 已融合成一份 doc(method/warnings 标注补充与冲突)，agent 拿融合草稿 + `read_raw` 回原件查证，不字面塞两个草稿对象。
- `[待处理]` 票06 把 agent 改"必需 key/默认启用"是行为变更，须显式更新评测预期（本票 agent_llm=None 故 eval 仍 7/7）。
- `[需拍板]` agent 提议的 charge_item 仅 advisory（输出标准名仍字典权威）；若要把 agent reconciled 名写进 audit/主输出 = 新增范围，需拍板。
- 落地：`posting.py` 抽 `_finalize_direction(:99)` 共用 + `posting_from_role(:160)`，`decide_posting` 逐字不变；`models.py:204-207` AuditTrail 增 `agent_confidence`/`agent_role`；逐条落账钉子在 agent 路径复现绿(`test_agent_preserves_per_charge_ledger_interleave`)；ReAct `MAX_ROUNDS=6` 不收敛/异常回落确定性。

### 票03 软失败闸门 + 解析补救 agent（108 绿，已 merge）
- 已修坑：金额混淆检测误报箱号/单号（`MRSU2571577,`、`NO.:GSDRHZMHK331281`）→端到端 5 红。修复：疑似混淆 token 须**整体**只由数字/小数点/千分位/易混字母组成且含至少一个易混字母（箱号含 M/R/U/N 等非易混字母天然排除）；回归钉子兜底。
- 已修坑：text 模式图片读空救不回被误记 failure，撞 `test_ocr_modes_offline[text]` 的 `not result.failures`。修复：PENDING_REVIEW 是"主动转人工复核"非批次失败，`_pending_review_updates` 用 `hard` 区分（仅 ingest 硬崩溃计 failure）。
- 欠债/候选改进：补救目前仅"文件级重 OCR + 带反馈重抽"两策略；**页级重 OCR 需改 OCR 网关契约（现为文件级）**，留后续票/需拍板。
- 经验：易混字母集 `_CONFUSABLE_LETTERS` 是启发式，OCR 混淆模式变化时需调整（已有回归钉子）。
- 语义待明确：PENDING_REVIEW≠failure 的边界建议写进 GLOSSARY。
- 落地：`graph/judge.py` compute_metrics:100/code_gate:141/LlmJudge:184/run_gate:264；`graph/repair.py` RepairAgent:51/build_pending_review_record:88；`graph.py` 仅 ingest 路由区 gate:140/repair:147/pending_review:160/route_after_gate:272（重试≤2）；`config.py:98-103` gate_min_confidence=0.5/ingest_max_retries=2/judge_enabled/judge_model；`state.py:51-54`；`dispatcher.py:49`/`pdf_loader.py:68,89` force_ocr；`pipeline.py` judge/repair 注入缝。裁判默认关、离线退化纯代码指标。

### 票05 audit_trail 补齐 + 尾巴交叉验证（106 绿，合并后 118，已 merge）
- 落地：`models.py:209-217` AuditTrail 增 quantity/unit_rate/subtotal/tax/source_snippet（顶层六字段不动）；`pipeline_utils.py:247-251` assemble_record 两路共用装配；新增 `graph/crosscheck.py` 落账前复用 validators(金额重算)/matcher.match_charge(认亲)/posting.decide_posting(四要素方向)+agent 低置信<0.5，任一不一致→_downgrade_to_pending（PENDING_REVIEW、清空 updates、不落账）；`graph.py` 仅 tail:169+import:30；`tests/test_tail_crosscheck.py` 4 条（含 test_agent_wrong_direction_proposal_blocked_by_tail）。
- 经验/坑：低置信阈值须**严格<0.5**（基线 test_agent_keeps_unknown_charge_name_no_fabrication 用 0.5 期望 CREATE，边界值即契约）；认亲复查须在 tail 时跑（跑完后重跑会因 CREATE 已落账产生假性不一致）；crosscheck 放 graph/ 而非 validation/（validation 已引 pipeline_utils 会循环）。
- 欠债/候选：_MONEY_ISSUE_CODES 含上游已阻断的 AMOUNT_MISSING/CURRENCY_INVALID，属有意独立复核、略冗余；方向复查"仅确定矛盾才拦"、规则拿不准不覆盖 agent 疑难——如需更强方向把关属行为变更、需拍板。
- 语义：rules.extract_totals 命中 TOTAL 模式即 subtotal/tax=None，真实样例全 None 属正常非 bug。
- 票06 注意：改综合 agent 默认启用/必需 key 会改默认行为，须显式更新评测预期；ChatAgentLlm 已就位（票04）直接接。

### 票06 必需 key + 配置 + 评测 + 文档（126 绿/1 deselected，已 merge，六票全部完成）
- 落地：`config.py:105-111` agent_required_key 数据类缺省 False、`:180` from_env 缺省 True（拍板 Q1=A 两层缺省）；`pipeline.py:46-53` MissingAgentKeyError、`:109/:124-158` _resolve_agent_llm 构造期把关（命脉无 key 无注入→报错；有 key→自动启用 ChatAgentLlm；注入禁用实现→拒绝）；`cli.py:158-161` cmd_eval 钉住 with_overrides(agent_required_key=False, llm_enabled=False)、`:260-265` 捕获报错退出码 2；`pyproject.toml:39-44` addopts="-q -m 'not e2e'"+e2e marker；新增 tests/test_required_key.py 8 条 + tests/test_e2e_real_agent.py 1 条（真模型、无 key skip、指标落盘 e2e_metrics.json）；README/ARCHITECTURE 重写 v2。
- 拍板：Q1=A（from_env 缺省命脉 true/数据类 false）；Q2=离线确定性当 7/7 闸门 + 真模型 e2e 宽松证据（用户："离线的测试是肯定需要的，我也需要端到端的测试"）。
- 【质量债·需拍板】真模型 qwen3.8-flash e2e（4 样例、25m34s）：recall 1.0/precision 1.0 但 operation_accuracy 0.4286/posting_accuracy 0.5；NO_CHANGE↔UPDATE 语义混淆 + debit note 方向推理弱，2 条被尾巴交叉验证拦成 PENDING_REVIEW（兜底生效）。改进=强化 synthesize 提示词 operation 语义/方向 few-shot 或换更强模型，属行为变更需拍板。e2e 数字不当回归基线（基线=离线 eval 7/7）。
- 【候选改进·需拍板】ChatAgentLlm 无响应缓存，e2e 每次 25-27 分钟且依赖端点；可加 agent 响应缓存缝（类比 llm_cache_dir）使 e2e 可回放。
- 流程坑：子代理结束 turn 会连带杀后台 job→长任务必须 job_output wait 阻塞保持 turn 活跃；pytest 通过时丢弃捕获 stdout→证据型测试要落盘结果（e2e_metrics.json）。
- 端点：tokenrhythm.studio 探针 1.32s、实跑单次分钟级，e2e 时间预算按 30 分钟+。
- 下一入口注意：任何经 from_env 构造 BillingAgent 而无 key 会构造即报错；离线场景显式 AGENT_REQUIRED_KEY=false 或注入假 agent_llm；cmd_eval 的 with_overrides 钉住别删。

### 集成后代码审查 review-v2（f2c9916..63ae416，20 提交 39 文件 +3697/−214）
- 【blocker·必修】graph/judge.py:246 judge() 直调 self._chain.invoke 但 _chain 恒 None(:197)、_build()(:200-230) 全仓库无调用点→裁判 enabled=True 必抛 AttributeError 被 :258 捕获→永远"故障退化放行"，真裁判从未生效；现有测试全注 FakeJudge 故抓不到。修=judge() 改 self._build().invoke 或构造时建链+补"真 LlmJudge+假 chain"回归测试。
- 【major·需拍板】graph/repair.py:84-85 + graph/graph.py:124 reextract 空转：不带 force_ocr、gate_reason 只进 entry.warnings(:154-156) 不进解析/抽取路径→裁判判负重试必然同结果、纯耗次数；最终安全落 PENDING_REVIEW 但与 spec.md:48"带反馈重抽"不符。方向二选一：让 gate_reason 真进重抽（force_ocr/影响抽取提示）vs 如实降级表述为"重试重读+原因留痕"。
- 【minor·死代码】repair.py:59 self.ocr 只存不读、:35 logger 未用、:61 stats 无读者；judge.py:198 stats 同病；agents.py:76-77 Proposal.charge_item/standardized 死字段；agents.py:114 max_rounds 无覆盖(Speculative Generality)。
- 【minor·spec 偏差】agents.py:217-221 agent 只拿融合草稿+warnings、拿不到规则/LLM 两份独立草稿逐字段核对（票04 Retro 自认）；agents.py:139,151-159 agent 提议 charge_item 全丢弃、连 advisory 都不入 audit_trail；agents.py:345-347 装配异常回落 record_chain 无审计标记(agent_confidence=None) 生产无法区分真 agent/静默回落。
- 【minor·冗余】crosscheck.py:100-112 认亲复查恒真（两路同源 match_charge，agent 无法篡改）防御性冗余非错误。
- 【smell·入台账非门槛】agents.py:280-287 与 judge.py:221-228 _build 同参可提共享工厂；5 测试文件重复 FakeOcr/FakeLlm 可收 conftest；(doc,charge,tools,settings,doc_issues,duplicate_of) 六件套穿参(Data Clumps)；graph 节点伸手 owner 私有方法(Feature Envy)。
- 【已核查通过】循环依赖真消除；UTF-8 无 BOM；循环内无例行日志；Google 中文注释齐备；硬约束1-6 全过（六字段/operation/OCR 契约/字典/测试完整性含交错钉子/自主边界/离线 e2e 隔离）；无 scope creep（normalize/matching/validation/ocr_client 零 diff）。
- 结论：有条件不可交付——judge blocker 必修；reextract major 需拍板；其余 minor 入台账或同批清理。

### 票08 裁判断链修复 + 重抽反馈真生效 + 死代码清理（133 绿，已 merge；审查 blocker/major 已闭环）
- 落地：`graph/judge.py:247` judge() 改 self._build().invoke（懒建链缓存）→真裁判启用路径生效；删 judge/repair 无读者 stats、repair self.ocr/模块 logger（构造签名 (ocr_client,settings) 保留兼容占位）。`graph/graph.py:125` force_ocr in ("ocr","reextract")→reextract 换路重 OCR；:169 repair 留存 repair_feedback（gate 放行会清 gate_reason 故单独携带）；:188-190 extract 传 feedback；`state.py:57` +repair_feedback；`document_extractor.py:39-49/178-195` feedback 透传（只主单据、附件不带；兼容只接受基础签名的假 LLM）；`llm_extractor.py:115-148/160-179` feedback 计入缓存键+非空渲染"上次解析闸门反馈"段（无反馈逐字不变）。新增 tests/test_judge_reextract_feedback.py 7 条（真 LlmJudge+monkeypatch 假 ChatOpenAI 4 + reextract 主缝 3）。
- 回归钉住：test_llm_judge_enabled_fail_verdict_really_blocks（先红 'NoneType' object has no attribute 'invoke'）；test_reextract_retry_forces_ocr_and_feeds_gate_reason（OCR 恰1次+假LLM实收 feedback）；test_reextract_exhausts_retries_to_pending_review（OCR 恰2次→PENDING_REVIEW）。
- 经验/坑：str(ChatPromptValue) 是 repr（换行转义）→断言提示词内容须 .to_string()；gate_reason 语义="最近一次不放行的原因"、放行即清空→反馈须在 repair 节点留存副本。
- 债/改进：reextract 对文本层本就干净的 PDF 重 OCR 未必更准，属"换路重试"既定语义（用户拍板 A）；stats 已删，如需可观测性须先接 pipeline 汇总真实读者再加，勿回退无读者死代码；judge() 后续改 _build 勿退回直调 self._chain。
- 审查 blocker/major 至此闭环；剩余 minor（agents.py 死字段/回落无审计标记/两草稿偏差/提议名不留痕）待票07 合并后另批处理或入台账。

### 票07 综合 agent 提示词强化（e2e operation 0.4286→0.8571、posting 0.5→1.0，已 merge）
- 落地：仅 `graph/agents.py:40-114` SYSTEM_PROMPT（+61/-5）。五处强化：输出边界(agent 只产角色候选+标准名+理由+置信度，不输出 operation)；operation 四值语义+判定依据(补全空字段不算更新)；四要素角色推理(**四要素是推理输入不是齐全清单——接收方缺失但签发方明确是我方时 role=issuer 仍成立**，PDF2 recipient=None 误判根因)；置信度标定(事实明确 0.85-0.95 且不低于 0.5 防 crosscheck 误拦)；9 个对照 few-shot。match_charge 工具描述补 matched_line 返回字段。
- e2e before/after（qwen3.8-flash,4 样例,21m06s）：recall/precision 1.0/1.0 不变；operation 0.4286(3/7)→0.8571(6/7)；posting 0.5(2/4)→1.0(4/4)。7 条 agent_confidence 全非空。
- 【剩余质量债·需拍板】唯一剩余差异 PDF1 On Carriage NO_CHANGE→UPDATE：根因=规则抽不出 PDF1 日期(document_date=None)，真 LLM 草稿补位 2026-10-19(取原文 RELEASE DATE)，`extract/merge.py:45-47` "LLM 补位"无条件采纳，台账 BILL-2002 日期=2026-10-26→`pipeline_utils.py:102-117` 判 overwrite→UPDATE。属草稿层补位策略，synthesize 提示词修不了。候选修法(均行为变更需拍板)：①草稿提示词约束"单据日期取签发/到货日期,不取 release/notice 日期"；②融合层对 LLM 补位日期降级(改 compute_field_changes 语义,影响面大)；③规则缺失日期时不补位。
- 经验：提示词杠杆边界=operation 由尾巴死规则算(pipeline_utils.py:124-155 _decide_operation)，agent 只经 role→posting 与自标置信度影响它；few-shot 内部自洽=每条示例事实须完整支撑提案每个字段(例2/3 补"签发方=记账主体"后重启 e2e)。
- 观察：PDF2 真草稿 issuer 误读 "Pilot Aiship Tech co., Ltd."(规则 NEPTUNE 正确)，融合"规则为准"+冲突 warning+置信×0.8 正确兜底；agent 在冲突 warning 下信任规则值，若后续模型被 warning 带偏可能回归。
- 审查剩余 minor 待另批：agents.py:76-77 Proposal.charge_item/standardized 死字段、:345-347 装配异常回落无审计标记、:217-221 拿不到两份独立草稿、:139,151-159 提议名不入 audit_trail。

### 票09 草稿日期语义 + 缓存指纹 + minor 清理（137 绿，已 merge；PDF1 修复但差异转 PDF2）
- 落地：`llm_extractor.py:61` SYSTEM_PROMPT 日期语义约束（document_date 只取签发/账单/到货日期，release/notice/RELEASE DATE 不取，无则 null）+反例；`:145-150,154` 缓存键加提示词指纹 sha256(SYSTEM+USER+schema)[:12] 前置（提示词/Schema 改动自动失效旧草稿缓存）；`agents.py:117-133/188/201-207/300-306` 删 Proposal 死字段+孤立变量+4 赋值；`:392-397` 装配异常回落往 audit_trail.warnings 追加"agent 装配异常，回落确定性链"。新增 tests/test_agent_fallback_audit.py 1 + test_llm_cache_fingerprint.py 3。merge/pipeline_utils 零 diff。
- e2e（18m20s）：PDF1 On Carriage 恢复 NO_CHANGE（新草稿键 51cc50a2/指纹 4c4c9b670333，旧 stale e9941e79 不存在，新草稿 date=None）；但 operation 仍 0.8571——差异转 PDF2 Handling 35 NO_CHANGE→UPDATE。
- 【剩余质量债·根因同机制·需拍板】PDF2 真草稿 recipient 误读为 NEPTUNE（规则 recipient=None、台账真实接收方 CIMC WETRANS）→merge.py:53-55 无条件补位→doc.recipient=NEPTUNE 与台账 CIMC 比 similarity<0.85→pipeline_utils.py:110-115 判 overwrite→UPDATE。与 PDF1 日期同机制=**无规则防守的 LLM 补位字段×融合无条件采纳**（日期/recipient 先后中招）。逐字段提示词约束=打地鼠。结构性候选：B-targeted=规则缺失而 LLM 补位的双方字段不得驱动 overwrite（低信任/enrich 处理）；C=规则缺失不补双方；A2=提示词 recipient 语义约束（仍打地鼠）。或接受 0.86/1.0 记债停止。
- 经验：两层回落结构=循环内 complete() 异常→确定性提案(agent_confidence 非空)；循环外装配异常→record_chain(agent_confidence=None+warnings 留痕)；测外层用畸形提案(reasons 非列表)而非抛异常 complete()。提示词指纹覆盖模板+输出 Schema；agent SYSTEM_PROMPT 不在草稿缓存指纹范围（agent 无响应缓存）。
- 观察：无规则防守的补位字段是草稿质量波动重灾区；PDF2 issuer 误读(Pilot Aiship)有规则防守被"规则为准"吸收，recipient 无规则防守则误读直穿。
- 流程：e2e --basetemp=.work/e2e_tmp 落证据；跑后核验新旧缓存键+离线 merge 复现差异根因，不必二次联网。
