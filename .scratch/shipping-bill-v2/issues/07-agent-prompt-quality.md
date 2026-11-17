# 07: 综合 agent 提示词强化（operation 语义 + 方向 few-shot）

**What to build:** 用户拍板（质量债选 A）：真模型 e2e 暴露 operation_accuracy 0.4286 / posting_accuracy 0.5（NO_CHANGE↔UPDATE 语义混淆、debit note 方向推理弱）。强化综合 agent 的 synthesize 提示词：①明确 operation 四值语义与判定依据（CREATE=台账无认亲；UPDATE=认亲命中且字段有变；NO_CHANGE=认亲命中且无实质变化；PENDING_REVIEW=不可信/冲突），给对照 few-shot；②强化 AR/AP 方向推理（记账主体+双方+单据类型+Credit/红字），给 debit note / credit note 方向 few-shot。只改提示词与 few-shot，不改工具/死规则/契约。

**Blocked by:** 06

**Status:** ready-for-agent

- [ ] synthesize 提示词含 operation 四值语义+判定依据+对照 few-shot
- [ ] 提示词含 AR/AP 方向推理要点+debit/credit note 方向 few-shot
- [ ] 现有测试一行不改；全量离线 pytest 绿；离线 eval 7/7 不回归
- [ ] 真模型 e2e 复跑：operation/posting 较基线 0.4286/0.5 有实质提升（报 before/after 与差异明细）
- [ ] 顶层六字段/operation 四值/OCR 契约/费用名字典不动；crosscheck 兜底仍生效
- [ ] Retro 六问写本文件 ## Retro 并附回报

**基线（票06 e2e，qwen3.8-flash，4 样例）:** recall 1.0 / precision 1.0 / operation 0.4286 / posting 0.5；6 处差异集中在 NO_CHANGE↔UPDATE 与 debit note 方向，2 条被 crosscheck 拦成 PENDING_REVIEW。

**注意:** 提示词属行为变更（用户已批准 A）；e2e 非硬闸门、真模型有波动，复跑报指标不擅自改断言；e2e 约 25-30 分钟，用 job_output wait 阻塞保持 turn 活跃；key 只进环境变量不入仓库/日志。