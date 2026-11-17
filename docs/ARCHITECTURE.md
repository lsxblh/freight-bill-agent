# 架构与关键决策（v2：综合 agent + 死规则尾巴）

> 本文档描述 v2 四段式架构。架构取舍的正式记录见
> [`adr/0001-v2-comprehensive-agent-deterministic-tail.md`](adr/0001-v2-comprehensive-agent-deterministic-tail.md)
> （取代 11-07「确定性骨架 + 独立小模型质检 + 多个定点 agent」的旧决定）；
> 领域词汇见根目录 [`GLOSSARY.md`](../GLOSSARY.md)。

## 1. 总体数据流：四段式 LangGraph

编排收敛为四段式 StateGraph（`shipping_billing/graph/graph.py`），
`pipeline.py` 的 `BillingAgent` 是图的薄封装（批次循环、运行日志、汇总输出）。
贯穿原则：**综合 agent 负责"理解和提议"，死规则尾巴负责"把关和落账"**。

```
┌────────────────────────────────────────────────────────────────────────────┐
│ ① 读进来 Ingest（ingest 节点 + 软失败闸门 + 解析补救）                        │
│   EML → 邮件头/正文/HTML → 附件递归（PDF/图片/Excel/Word，最大 5 层）         │
│   PDF → 文本层优先；字符数不足视为扫描件 → OCR                                │
│   图片 → OCR；Excel → openpyxl/xlrd；Word → python-docx / LibreOffice        │
│   统一产物：RawDocument(text, pages, tables, metadata, attachments, warnings)│
│   硬失败（完全读不出）→ PENDING_REVIEW 记录 + failure 隔离，不拿垃圾跑下游    │
│   软失败闸门 gate：代码指标先筛，边界 case 交小模型裁判吹哨（JUDGE_MODEL）     │
│   软失败且重试 < INGEST_MAX_RETRIES(默认2) → repair 换策略回 ingest 重走；    │
│   重试耗尽 → PENDING_REVIEW（主动转人工，不计批次失败）                       │
└───────────────────────────────┬────────────────────────────────────────────┘
                                ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ ② 双路抽取 Extract（规则草稿 ∥ LLM 草稿，LangChain RunnableParallel）         │
│   规则通道：类型/编号/日期/双方/币种/合计/业务标识/明细行（硬事实，可复现）     │
│   LLM 通道：语义补位（可选，Pydantic 结构化 + 缓存 + 解析修复重试）            │
│   融合：数值以规则为准，语义缺失用 LLM 补，冲突→warnings + 降置信度            │
│   LLM 草稿读失败留空、不阻断；随后标准化（费用名/币种/付款方式）               │
│   产出：ExtractedDocument 队列，逐条费用进入综合段                            │
└───────────────────────────────┬────────────────────────────────────────────┘
                                ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ ③ 综合 agent Synthesize（ReAct 工作台，graph/agents.py）                     │
│   拿融合草稿 + 原件 + 台账 + 记账主体，自主决定何时调哪个工具：                │
│   match_charge（认亲·四级匹配）/ read_raw（读原文）/                          │
│   query_ledger（查台账）/ lookup_charge_name（查费用名字典）                  │
│   工具全部返回确定性事实（同输入同结果），agent 只推理疑难 case                │
│   产出提案：角色候选 issuer/recipient + 标准名建议 + 理由 + 自标置信度         │
│   **只提议不落账**；理由/置信度/角色写入 audit_trail                          │
└───────────────────────────────┬────────────────────────────────────────────┘
                                ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ ④ 死规则尾巴 Tail（graph/crosscheck.py + 落账）                              │
│   落账前钱关键项确定性交叉验证（复用现有校验/认亲/四要素，不新造一套）：        │
│   金额重算（单价×数量/明细加总/币种）· 认亲复查（match_charge 同源）·          │
│   方向复查（decide_posting 四要素，仅确定矛盾才拦）· agent 自标置信度 < 0.5    │
│   任一不一致/不可信 → 降级 PENDING_REVIEW（错提案不落账）                     │
│   通过 → 定 operation（CREATE/UPDATE/NO_CHANGE）→ 增量落账 → 登记重复         │
│   逐条"提案→落账"循环回 ③（认亲读活台账，先落账的行可被同单据后条认亲）         │
│   输出：results.json（六字段 + 审计链）/ ledger_after.json / report.md / run_log│
└────────────────────────────────────────────────────────────────────────────┘
```

条件边四处（`graph/graph.py`）：ingest 后（硬失败隔离/进闸门）、gate 后
（放行/补救/转人工）、extract 后（失败隔离/空队列收尾）、tail 后（逐条循环）。

## 2. graph 包模块地图

| 模块 | 职责 |
|---|---|
| `graph/state.py` | `BillingState`：单文件三层模型流转的状态定义 |
| `graph/graph.py` | 四段式 StateGraph 装配 + 条件边路由；`RECURSION_LIMIT=10000`（逐条循环超步多） |
| `graph/judge.py` | 软失败闸门：代码指标（`compute_metrics`/`code_gate`）+ 独立小模型裁判 `LlmJudge`（只吹哨不抽取，故障退化放行） |
| `graph/repair.py` | 解析补救 agent：自主换策略（文件级重 OCR 等）+ `build_pending_review_record` |
| `graph/agents.py` | 综合 agent `ComprehensiveAgent`（ReAct 循环，`MAX_ROUNDS=6`）+ 真实适配器 `ChatAgentLlm` + 综合入口 `synthesize_charge` |
| `graph/tools.py` | `AgentTools` 四确定性工具：认亲/读原文/查台账/查字典，全由现有模块包一层，只返回事实 |
| `graph/crosscheck.py` | 尾巴落账前钱关键项交叉验证，任一不可信→降级 PENDING_REVIEW |

依赖方向：`pipeline → graph → extract/matching/validation/finance`，graph 不反向
import pipeline（编排器实例由调用方以 owner 传入）。

## 3. 为什么是"三层模型"

| 层 | 类型 | 作用 | 为什么不能合并 |
|---|---|---|---|
| 证据层 | `RawDocument` | 原文、表格、页码、OCR 来源、附件树 | 出问题时要能回答"原文长什么样"，这是审计与复盘的根 |
| 语义层 | `ExtractedDocument` / `ChargeLine` | 结构化字段 + 逐字段置信度 + provenance | 抽取算法的中间态，便于规则/LLM 融合与再加工 |
| 输出层 | `OutputRecord` | 题目规定的 JSON 契约（顶层六字段） | 对外契约必须稳定，不随内部模型演进变化 |

audit_trail 在六字段之外补齐：数量/单价/小计/税额/原文片段 + agent 理由
（`ai_reasoning`）/匹配规则/`agent_confidence`/`agent_role`/字段变化，供人工复核。

## 4. 自主边界：agent 提议，死规则把关落账

这是 v2 的核心取舍（ADR-0001）：

* **autonomy 集中在一个综合 agent**：核对两稿、归一化、认亲疑难、AR/AP 推理都由它
  自主调工具完成，而不是分散在多个定点 agent（旧 18 节点方案被否：碎、过于工作流）。
* **确定的事不让模型碰**：四级匹配是确定性工具（与装配同源 `match_charge`），
  费用名字典权威（未收录保留原名 + 低置信，不臆造），"角色→AR/AP"那一下是死规则
  `posting_from_role`（四要素，同输入同结果）。
* **幻觉记不进账**：提案必须经尾巴交叉验证才能落账；金额对不上、认亲结论被篡改、
  方向与四要素确定矛盾、agent 自标置信度 < 0.5，任一命中即 PENDING_REVIEW。
* **逐条落账是正确性来源**：synthesize⇄tail 逐条"提案→落账→登记重复"交错，
  认亲读的是活台账（先落账的 CREATE 行可被同单据后一条费用认亲）。
  行为钉子：`tests/test_graph_skeleton.py::test_same_name_charges_interleave_with_ledger`。
* **ReAct 不收敛/异常有兜底**：超过 `MAX_ROUNDS` 或模型故障时回落确定性推理
  （`_deterministic_proposal`），单条费用失败不拖垮整批——这是运行期韧性，
  与"无 key 静默降级"是两回事（后者被命脉模式禁止，见 §5）。

## 5. 综合 agent 是命脉：必需 key，不静默降级

规格 Q5 已批准的行为变更：本作品的定位"就是需要 agent"，不允许无 key 时悄悄
退成纯规则流水线掩盖定位。

* 开关：`Settings.agent_required_key`（环境变量 `AGENT_REQUIRED_KEY`）。
* 命脉模式（True）：综合 agent 必须可用——无 key 且未注入 `agent_llm` 时
  `BillingAgent` 构造即抛 `MissingAgentKeyError`（CLI 退出码 2、点名 `LLM_API_KEY`）；
  配了 key（`LLM_ENABLED=true` + `LLM_API_KEY`）则默认启用真实适配器 `ChatAgentLlm`；
  注入但 `.enabled=False` 同样报错。
* 兼容模式（False，数据类缺省）：`agent_llm` 缺省 None 走确定性入账链（票04 行为），
  供离线测试与显式回退使用。测试全部经主缝注入假 LLM/假 OCR 缓存，不依赖外网。
* LLM 配置沿用真实键 `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` / `LLM_ENABLED`
  （OpenAI 兼容端点）；裁判独立模型 `JUDGE_MODEL`（复用端点/key，默认关闭）。

## 6. 关键决策与取舍（沿承 v1，仍然有效）

### 6.1 PDF：文本层优先，而不是"一律 OCR"

* 文本层是矢量原文，准确率 100%、零网络依赖、零成本；
* 扫描件（每页字符数 < 120）才转 OCR；费用表粘连时**按页**自适应回退 layout
  （不能全局切：PDF1 在 layout 下反而金额粘描述）；
* 保留 `--ocr-mode api` 强制全量 OCR，满足"必须调用 OCR API"的验收诉求；
* 代价：文本层阅读顺序可能与视觉不一致（多栏票据会串行），字段抽取全部用
  "模式 + 上下文"而不是"坐标"。

### 6.2 费用行：布局模板 + 区块定位，而不是纯正则扫全文

* 纯"结尾是金额就当费用"会误抓箱号行、提单号行、地址电话；
* 先找表头（`DESCRIPTION…AMOUNT` / `CHARGES CUR AMOUNT` / 中文表头），再往下读到
  `TOTAL/空行` 为止；8 种布局按信息量降序尝试。

### 6.3 表格：列语义映射，而不是固定列序

`extract/tables.py` 把表头词映射成 `charge/amount/currency/quantity/unit_rate/payment`
七种语义，中英文同义词覆盖，Excel/Word/OCR tables 共用同一套解析器。

### 6.4 规则 ∥ LLM 的并联，而不是串联

* 并联：两者看同一份原文，互不影响，任何一个失败都不阻塞主链路；
* 串联更省 token 但无法做交叉校验；并联的收益是"两个独立信源互相印证"。
* v2 中两份草稿融合后交综合 agent 核对，冲突有人裁决而不是简单覆盖。

### 6.5 台账按记账主体隔离

`Ledger.scope_lines(entity)` 只返回"属于该主体"或"未标注主体"的行，
避免 A 公司的提单号匹配到 B 公司的账单。

### 6.6 幂等与重复入账

* 批次内：`(单据号, 费用名, 金额, 币种)` 去重键，重复命中 → PENDING_REVIEW；
* 台账侧：匹配成功且无 overwrite → NO_CHANGE；
* 缓存侧：OCR/LLM 结果按内容哈希缓存，重跑不产生不同结果。

### 6.7 软失败闸门：混合判定，PENDING_REVIEW ≠ failure

* 代码指标先筛（金额数字/费用行/单号箱号、OCR 置信度、金额混字母），
  边界 case 交独立小模型裁判吹哨；裁判只判"够不够干净"，不抽字段，故障退化放行。
* 补救按**文件级**重读（OCR 网关契约是文件级，页级重 OCR 需改契约，留后续）。
* 救不回 → PENDING_REVIEW 记录（主动转人工复核），**不计批次失败**；
  只有硬崩溃才计 failure。两者性质不同，输出与日志严格区分。

## 7. 扩展一个新格式需要改哪里

1. `ingest/xxx_loader.py` 实现 `parse_xxx() -> RawDocument`（文本 / 表格 / metadata）；
2. `ingest/dispatcher.py` 注册扩展名与魔数；
3. 若新格式有独特的费用表达，在 `extract/rows.py` 增加一个布局模板；
4. 补 2 个单测 + 1 个样例，跑 `pytest` 与 `eval`。

抽取、标准化、匹配、校验、综合、尾巴、输出层**完全不需要改**——这是分层的收益。

## 8. 目录与职责边界

| 目录 | 只允许做的事 |
|---|---|
| `ingest/` | 把字节变成 `RawDocument`，不做业务判断 |
| `extract/` | 把 `RawDocument` 变成 `ExtractedDocument`，不碰台账 |
| `normalize/` | 纯函数映射（名称/币种/付款方式），无 IO |
| `finance/` | 只做 AR/AP 判定（含接受 agent 角色候选的 `posting_from_role`），无 IO |
| `matching/` | 台账读写与匹配，不做校验 |
| `validation/` | 只产出 `Issue`，不决定操作类型 |
| `graph/` | 四段式编排 + 闸门/补救/综合 agent/工具/尾巴交叉验证 |
| `pipeline_utils.py` | 唯一决定 `operation` 的地方（装配两路共用） |
| `pipeline.py` | 图的薄封装：批次循环、运行日志、汇总、输出 |
| `harness/` | 横切能力：重试/日志/报告/评测 |
