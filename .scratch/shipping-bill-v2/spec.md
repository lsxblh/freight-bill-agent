# 规格：航运智能账单助手 v2 —— 综合 agent + 死规则尾巴

Status: ready-for-agent

## Problem Statement

现在的账单助手是一条"写死的规则流水线"：大模型只在一个点做结构化输出，遇到看不懂的单据、认不出的老账单、判不清的应收/应付，只会按固定按钮跳过或转人工，不会自己想办法。用户（求职者）要把它重构成一个**真正会自主应对**的 agent 作品：能自己核对、自己查证据、自己决定重试，同时保证"钱算得对、错账进不了总账"。这不是从零新建，而是在现有成品底子上做有理解、有取舍的重构。

## Solution

把编排收敛为**四段式 LangGraph**：①读进来（软失败闸门 + 解析补救 agent）→ ②双路抽取（规则草稿 + LLM 草稿）→ ③综合 agent（ReAct 工作台，集中理解与提议）→ ④死规则尾巴（交叉验证 + 把关落账 + 六字段 JSON）。贯穿原则：**综合 agent 负责"理解和提议"，死规则尾巴负责"把关和落账"**。agent 可以大胆推理，但幻觉记不进账。对外接口与输出契约不变，现有测试当回归闸门。

## User Stories

1. As a 账单录入员, I want 丢进 EML/PDF/图片/Excel/Word 任意一种都能被读进来, so that 不用先手动转格式。
2. As a 账单录入员, I want 完全读不出的文件也产出一条 PENDING_REVIEW 记录并写明原因, so that 批次不会悄悄少文件、我能知道哪个没读出来。
3. As a 账单录入员, I want 文字太脏/太散时系统自己换解析策略重试（≤2 次）, so that 扫描件/糊页有机会被救回而不是直接放弃。
4. As a 账单录入员, I want 救不回的文件不拿垃圾往下跑, so that 下游不会基于错误原文算错账。
5. As a 账单录入员, I want 规则和 LLM 各读一份原件出两份草稿, so that 固定数字和模糊语义互相补漏。
6. As a 账单录入员, I want LLM 草稿读失败时留空、不拖累整体, so that 单次模型故障不让整单陪葬。
7. As a 账单录入员, I want 一个综合 agent 核对两份草稿的相同/不同、必要时回原件查证, so that 冲突有人裁决而不是简单覆盖。
8. As a 账单录入员, I want 综合 agent 像员工坐工作台一样自己决定何时查台账/读原文/查字典/认亲, so that 疑难case被主动查证而不是干瞪眼。
9. As a 账单录入员, I want 费用名方言被翻成标准名、字典没收录的保留原名+低置信, so that 能对账又不臆造。
10. As a 账单录入员, I want 四级匹配作为确定性工具被 agent 调用、一调用就直接给出"是不是老账单", so that 确定的认亲可复现、agent 只推理疑难。
11. As a 账单录入员, I want 认不出老账单时记 CREATE、认出且有更新记 UPDATE、无变化记 NO_CHANGE, so that 台账增量正确。
12. As a 账单录入员, I want 应收/应付结合签发方/接收方/记账主体/收费方向四要素推理, so that 不只看单据类型判错方向。
13. As a 账单录入员, I want 综合 agent 的每个判断都带理由和置信度写进 audit_trail, so that 人工复核能看到"为什么"。
14. As a 账单录入员, I want audit_trail 补齐数量/单价/小计/税额和原文片段, so that 复核有据、不用翻原始文件。
15. As a 财务审核, I want 尾巴落账前对金额/匹配/方向做确定性交叉验证, so that agent 的错提案进不了总账。
16. As a 财务审核, I want 任何对不上或不可信的记录转 PENDING_REVIEW, so that 错账宁可让人看一眼也不悄悄入账。
17. As a 财务审核, I want 输出严格六字段、operation 四值、金额数字或 null, so that 下游系统契约不被破坏。
18. As a 财务审核, I want 同一单据喂两遍被识别为重复、不重复入账, so that 台账不重记。
19. As a 财务审核, I want 不同记账主体的台账互相隔离, so that 我方视角切换不串账。
20. As a 开发者, I want 对外入口签名与结果形状不变, so that 89 个现有测试一行不改当回归闸门。
21. As a 开发者, I want OCR 走缓存离线回放、LLM 可注入假实现, so that 测试与演示不依赖外网。
22. As a 开发者, I want 综合 agent 必须配 key、无 key 明确报错, so that 作品定位"就是需要 agent"不被静默降级掩盖。
23. As a 开发者, I want 单个文件失败被隔离、整批继续, so that 一个坏文件不拖死其余。
24. As a 开发者, I want 命令行 `run <文件>` 一键跑通, so that 无需前端即可演示。
25. As a 求职者, I want 新旧评测对比 7/7 不回归, so that 重构证明"更自主但不更错"。
26. As a 求职者, I want 架构决策与领域词汇落盘（ADR + GLOSSARY）, so that 面试能讲清取舍而不是黑箱。

## Implementation Decisions

- **架构形状**：采用四段式 LangGraph StateGraph（读进来→双路抽取→综合agent→死规则尾巴），取代现有裸 Runnable 编排。条件边四处：软失败闸门、匹配结果、角色明确与否、重试计数。遵循 ADR-0001（v2）。
- **模块划分**：新建 graph 包，含 state（三层模型流转）、graph（StateGraph 装配）、judge（软失败小模型裁判）、agents（解析补救 agent + 综合 agent）、tools（工具层）。编排层（现 pipeline/agent 两模块）改写为图的薄封装。
- **主缝不变**：对外入口 `BillingAgent(settings).run(paths)` 的签名与结果形状（records/summary/failures/ledger/write_outputs）保持不变；内部换图。OCR 经 settings.ocr_mode 缓存回放；LLM 从主缝注入。
- **软失败闸门（混合）**：代码指标先筛（有无金额数字/费用行/单号箱号、OCR 置信度、金额混字母），边界 case 交独立小模型裁判（JUDGE_MODEL）。裁判只吹哨不抽取。fail 且重试<2 → 解析补救 agent；重试耗尽 → PENDING_REVIEW。
- **解析补救 agent**：自主选策略（文本层空→OCR、某页糊→重OCR该页、带反馈重抽），回到读进来重走，≤2 次。
- **双路抽取**：规则草稿（死规则）+ LLM 草稿（可选）。LLM 草稿读失败留空、不阻断。
- **综合 agent（ReAct）**：拿两稿+原件+台账+记账主体，自主决定调用工具；产出提案（reconciled 字段/标准名/匹配结果/角色候选）+理由+置信度。只提议不落账。必须配 key，无 key 报错。
- **工具层**：认亲（四级匹配，确定性）、读原文、查台账、查费用名字典。全部由现有模块包一层，不新造轮子。四级匹配返回"事实"（认没认出/哪条/第几级），不做决策。
- **归一化**：字典（28 组）权威；未收录保留原名+低置信，不臆造。
- **AR/AP**：推理在综合 agent；"角色→AR/AP"落账那一下在死规则（四要素，同输入同结果）。posting 模块仅新增"接受 agent 判出角色"入口。
- **死规则尾巴**：校验→定 operation→交叉验证→落账→六字段 JSON。交叉验证复用现有校验/认亲/四要素代码：金额重算（单价×数量/明细加总）、匹配用认亲工具复查、方向用四要素复查；结合校验 issues 与 agent 自标低置信，任一不可信→PENDING_REVIEW。
- **数据契约**：三层模型（RawDocument→ExtractedDocument/ChargeLine→OutputRecord）不变；顶层六字段不变；audit_trail 扩展数量/单价/小计/税额/原文片段 + agent 理由/匹配规则/置信度/field_changes。
- **OCR 契约**：沿用现有网关 URL 与 task 参数，不改。
- **配置**：新增 JUDGE_MODEL、重试上限、综合 agent 必需 key 开关；沿用 LLM_MODEL/OPENAI_BASE_URL/OPENAI_API_KEY/MY_COMPANY。

## Testing Decisions

- **好测试的标准**：只测外部行为（records/operation/audit_trail/ledger/输出 JSON），不测内部节点或实现细节。
- **主缝端到端**：沿用现有端到端回归闸门（样例目录→7 条记录金断言），重构后必须全绿；这是"搬骨架不改行为"的验收。
- **现有窄缝保留**：归一化、四级匹配、校验、AR/AP 的纯函数测试继续用，不新增缝。
- **agent 行为测试**：通过主缝注入假 LLM 驱动 ReAct/质检/补救，断言外部行为（如软失败触发重试、认亲工具被调用、冲突提案被尾巴拦下转 PENDING_REVIEW）。
- **离线**：OCR 用缓存回放（ocr_mode=cache/text），LLM 用注入假实现；不依赖外网。
- **Prior art**：现有端到端 pipeline 测试、matching/normalize/validators 测试、conftest 缓存回放夹具。
- **评测**：新旧对比 7/7 不回归（recall/precision/operation/posting）。

## Out of Scope

- 前端/UI、实时流式输出。
- 交付动作（git 提交/推远程/打 zip/发邮件）——实现完成后另行处理。
- 改动输出六字段顶层契约、OCR 网关契约。
- agent 自主落账（钱必须死规则把关）。
- 新增费用名字典条目（沿用现有 28 组）。
- 多上下文/monorepo 拆分。

## Further Notes

- 落地顺序：第一步"搬骨架不改行为、89 测试全绿"→ 加软失败闸门+补救 → 加综合 agent+工具 → 尾巴交叉验证 → 新旧评测对比。"综合 agent 必需 key"是行为变更，放在第一步之后。
- 领域词汇见 GLOSSARY.md；架构取舍见 docs/adr/0001（v2，取代 11-07 旧决定）。
- 本重构是求职者"理解后自主修改"的作品，强调能讲清"哪儿该 agent、哪儿该死规则、为什么"。