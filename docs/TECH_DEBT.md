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
- `[待处理]` 票05：audit_trail 补数量/单价/小计/税额 + 原文片段 inline；尾巴落账前钱关键项确定性交叉验证。
- `[待处理]` 票06：综合 agent 必需 key 不降级 + 配置收口 + 新旧评测对比（真模型质量验证，离线假实现只证结构不证质量）。
- `[待处理]` 文档债：`docs/ARCHITECTURE.md`/`HARNESS.md`/`DESIGN_THINKING.md`/`ACCEPTANCE.md` 仍描述旧确定性流水线，与 v2 四段+综合 agent 脱节；`docs/adr/0001-...md` 已取代 11-07 旧 ADR 但四份文档未同步。
- `[待处理]` README 测试数过期：称 89，实际票02 后 96 且随票增长。
- `[待处理]` `.gitignore` 未覆盖 `pytest-cache-files-*`/`.pytest_tmp` 残留副产物；交付 zip 前需清理或补忽略，且**确认 zip 不含 `.env`**（真 key 在内，`.gitignore:12` 已忽略但 zip 不走 gitignore）。
- `[待处理]` `llm_extractor.py:110` `ChatOpenAI(max_retries=N)` × `:142` 外层 `range(N+1)` 双层重试叠乘，失败时请求数放大；应只留一层。
- `[待处理]` 死代码：`harness` 的 `ContentCache`/`CircuitBreaker`/`stable_key` 零调用；`ledger.py:116 by_reference`/`:122 candidate_lines` 零调用。删需确认（见拍板清单）。

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
