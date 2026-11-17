# 09: 草稿日期语义约束 + 审查剩余安全 minor 清理

**What to build:** 用户拍板（剩余质量债选 A）+ 审查剩余安全 minor。
1. 【A·草稿日期语义】LLM 草稿提示词（extract/llm_extractor.py）约束单据日期语义：document_date 取签发/到货/账单日期（invoice date / bill date / arrival date），**不取 release/notice/RELEASE DATE 等通知类日期**。根因：PDF1 规则抽不出日期→真 LLM 草稿补位 RELEASE DATE 2026-10-19→merge.py:45-47 无条件采纳→与台账 10-26 不一致→pipeline_utils 判 overwrite→UPDATE（期望 NO_CHANGE）。
2. 【缓存正确性·必做】LLM 草稿缓存键须含提示词指纹/版本，使提示词改动自动失效旧草稿缓存；否则 e2e 会复用旧提示词的 stale 草稿（如 e9941e79）看不到改进。验证 e2e 重跑确实重算 PDF1 草稿。
3. 【审查 minor·安全清理】graph/agents.py:76-77 Proposal.charge_item/standardized 死字段（只赋值无读取）删除（含 :153-154/:254-255 赋值）；:345-347 装配异常回落 record_chain 时在 audit_trail 留痕（如 warnings 追加"agent 装配异常回落确定性"），使生产可区分真 agent/静默回落（additive，不改六字段）。补离线测试钉住回落留痕。

**Blocked by:** 07, 08

**Status:** ready-for-agent

- [ ] 草稿提示词含日期语义约束（取签发/到货/账单日期，不取 release/notice）
- [ ] LLM 草稿缓存键含提示词指纹（提示词改动失效旧缓存）
- [ ] agents.py 死字段删除 + 装配异常回落 audit 留痕 + 离线回归测试
- [ ] 现有测试一行不改；全量离线 pytest 绿；离线 eval 7/7 不回归
- [ ] 真模型 e2e 复跑：PDF1 On Carriage 恢复 NO_CHANGE（operation 目标 1.0），报 before/after
- [ ] 顶层六字段/operation 四值/OCR 契约/费用名字典不动；merge"规则为准/LLM 补位"语义不改（只约束草稿取哪个日期）
- [ ] Retro 六问写本文件 ## Retro 并附回报

**范围限制:** 只改 extract/llm_extractor.py（提示词+缓存键）、graph/agents.py（死字段+回落留痕）、新增测试。不改 merge.py/pipeline_utils.py 的 overwrite/enrich 语义（那是候选 B/C，用户未选）。不要 merge。

**注意:** e2e 约 20-30 分钟，用 job_output wait 阻塞保持 turn 活跃；key 只进环境变量不入仓库/日志；e2e 非硬闸门、真模型有波动，不擅自改断言/减样例。