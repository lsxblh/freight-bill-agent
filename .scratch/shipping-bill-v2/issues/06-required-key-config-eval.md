# 06: 必需 key + 配置 + 新旧评测

**What to build:** 收尾验证票。综合 agent 定为命脉：无 API key 明确报错、不静默降级（此为行为变更，故放在搬骨架之后）。配置新增 JUDGE_MODEL、重试上限、综合 agent 必需 key 开关，沿用 LLM_MODEL/OPENAI_BASE_URL/OPENAI_API_KEY/MY_COMPANY。跑新旧评测对比，7/7 不回归（recall/precision/operation/posting），更新 README/架构文档反映 v2。测试全部经主缝注入假 LLM，不依赖外网。

**Blocked by:** 03, 04, 05

**Status:** done

- [x] 综合 agent 无 key 明确报错，不静默降级
- [x] 配置含 JUDGE_MODEL/重试上限/必需 key 开关
- [x] 新旧评测对比 7/7 不回归
- [x] README/架构文档更新为 v2 四段式
- [x] 全部测试离线（注入假 LLM/OCR 缓存）通过

## Retro

**做了什么**
- 命脉机制（规格 Q5 行为变更）：`Settings.agent_required_key` 开关，缺省分两层（拍板 Q1=A）——数据类缺省 False（程序化构造/离线测试不受影响），`from_env` 缺省 True（CLI/生产入口体现命脉定位）。`BillingAgent._resolve_agent_llm` 构造期把关：命脉模式无 key 且无注入→抛 `MissingAgentKeyError`；有 key→默认启用 `ChatAgentLlm`（懒构造）；注入禁用实现同样拒绝（静默降级禁止）。CLI `main` 捕获后退出码 2 + 明确配置错误信息。
- 配置：新增 `AGENT_REQUIRED_KEY`；JUDGE_MODEL/重试上限票03 已有，不重复加；沿用真实键 LLM_API_KEY/LLM_BASE_URL/LLM_MODEL/LLM_ENABLED，不新造票文旧称 OPENAI_*。
- 评测（拍板 Q2=两者都要）：`cmd_eval` 显式钉住离线确定性（`with_overrides(agent_required_key=False, llm_enabled=False)`），7/7 闸门不受命脉环境变量影响；另加真模型 e2e（`pytest -m e2e`，默认离线套件排除，无 key 优雅 skip，宽松 recall/precision≥0.8，不当 7/7 硬闸门）。
- 文档：ARCHITECTURE.md 全文重写为 v2 四段式（数据流图/graph 模块地图/自主边界/命脉模式/沿承 v1 决策）；README 补命脉模式、配 key 步骤、离线闸门与 e2e 跑法、目录与测试数更新。
- 测试：新增 tests/test_required_key.py 8 条（全离线）+ tests/test_e2e_real_agent.py 1 条（真模型）；现有 118 条一行未改。

**结果**
- 离线：126 passed, 1 deselected（junit 0 失败 0 错误 0 跳过）；eval 闸门 7/7，recall/precision/operation/posting/field_accuracy 全 1.0，与 examples/demo/eval_report.json 逐项一致。
- e2e 真 key 实跑（qwen3.8-flash @ tokenrhythm.studio，OCR cache 回放，4 样例文件）：passed，25 分 34 秒；recall 1.0 / precision 1.0 / operation_accuracy 0.4286 / posting_accuracy 0.5；7 条记录 agent_confidence 全非空（agent 真激活，非静默回落）。6 处字段差异集中在 operation 语义（NO_CHANGE↔UPDATE）与 debit note 方向；其中 2 条被尾巴交叉验证拦成 PENDING_REVIEW（宁转人工不错落账，兜底生效）。第一次实跑亦 passed（26 分 41 秒），指标取自第二次（见"哪步卡"）。
- 提交：aff4235（实现）+ 本文件所在提交（票文件勾选验收 + Retro）。

**哪步顺**
- 机制本身 TDD 切片先红后绿一路顺：票04/05 留的缝（agent_llm 注入/ChatAgentLlm/crosscheck）齐全，票06 只做"把关 + 默认启用"，不用动图。
- 离线闸门与命脉模式互不干扰：cmd_eval 一行 with_overrides 钉住兼容，闸门测试在 AGENT_REQUIRED_KEY=true 环境下仍绿。

**哪步卡 + 根因**
- 卡1：e2e 实跑被杀两次。第一次用户误点停止；第二次我启动后台 job 后就结束了 turn——子代理 turn 结束会连带杀掉后台 job。教训：后台 job 未收完必须用 job_output wait 阻塞保持 turn 活跃，不能提前结束。
- 卡2：e2e 通过了却拿不到指标。根因：pytest 通过时丢弃捕获的 stdout，测试只 print 未落盘。修：给测试加 3 行把指标写 tmp_path/e2e_metrics.json，带 -s 重跑一次拿到数字。教训：作为"证据"的测试要把结果落盘，别依赖 stdout。
- 卡3：端点慢（单次调用分钟级，全程 25-27 分钟；早前探针仅 1.32s，实跑时端点变慢）。非代码问题，阻塞循环等完，未擅自减样例。

**约定检查**
- 顶层六字段/operation 四值/OCR 网关契约/费用名字典：未动。
- 现有测试一行未改（118→126 全为新增）；注释 Google 风格中文；UTF-8 无 BOM；脚本输出无 ⚠️/✓ 符号；副产物集中 .work/ 用完自删；key 只进环境变量，不入仓库/提交/日志。
- 决策流程：Q1/Q2 均停下报 lead 转用户拍板后才落地；e2e 断言未擅自改、样例未擅自减。

**下一票注意**
- from_env 缺省命脉：任何新 CLI 入口/脚本经 from_env 构造 BillingAgent 而无 key 会构造即报错；离线场景显式 AGENT_REQUIRED_KEY=false 或注入假 agent_llm（数据类直接构造缺省 False，不受影响）。
- cmd_eval 的 with_overrides 钉住别删，否则闸门受环境命脉模式影响。
- e2e 不是闸门：真模型 operation/posting 波动大（本次 0.43/0.5），别拿 e2e 数字当回归基线；回归基线是离线 eval 7/7。
- 真模型 operation/posting 质量是主要技术债（见下），改 prompt 或换模型属行为变更，需拍板。

**坑/债/改进（供 lead 追加 docs/TECH_DEBT.md）**
1. [质量债] 真模型（qwen3.8-flash）e2e operation_accuracy 0.4286 / posting_accuracy 0.5：NO_CHANGE↔UPDATE 语义混淆（PDF示例账单1.pdf On Carriage Charge、PDF 示例账单2.pdf Handling Charge）；debit note Handling/Detention 方向推理弱，2 条被交叉验证拦成 PENDING_REVIEW（posting_type=null）。recall/precision 均 1.0（不漏不多）。改进方向：synthesize 提示词强化 operation 语义与方向 few-shot，或评估更强模型；属行为变更需拍板。
2. [可观测/可回放] ChatAgentLlm 无响应缓存，e2e 每次 25-27 分钟且依赖端点状态；可考虑加 agent 响应缓存缝（类比 llm_cache_dir）使 e2e 可回放。新增缝需拍板。
3. [流程坑] 子代理结束 turn 会杀后台 job：长任务必须 job_output wait 阻塞保持 turn 活跃；pytest 通过时丢弃捕获 stdout，证据型测试应落盘结果（本票 e2e 已加 e2e_metrics.json）。
4. [端点] tokenrhythm.studio 探针时 1.32s、实跑时单次调用分钟级，e2e 时间预算按 30 分钟+ 安排。