# 03: 软失败闸门 + 解析补救 agent

**What to build:** 读进来一段加上"软失败闸门"：代码指标先筛（有无金额数字/费用行/单号箱号、OCR 置信度、金额混字母），边界 case 交独立小模型裁判（只吹哨不抽取）。不合格且重试<2 → 解析补救 agent 自主换策略（文本层空→OCR、某页糊→重OCR该页、带反馈重抽）回到读进来重走；重试耗尽或完全读不出 → 产出 PENDING_REVIEW 记录，不拿垃圾跑下游。用假裁判/假 OCR 可离线验证。

**Blocked by:** 02

**Status:** done (commits 748b4bd+8dba641, 108 passed, merged)

- [x] 软失败闸门=代码指标+小模型裁判混合，裁判只判定不抽取
- [x] 解析补救 agent 换路重试≤2，重试计数条件边生效
- [x] 救不回/完全读不出 → PENDING_REVIEW 记录（含原因），不跑下游
- [x] 单文件失败隔离、整批继续
- [x] 注入假裁判/假 OCR 的端到端测试通过，现有测试不回归

## Retro

（retro 技能为用户手动触发专用（disable-model-invocation），模型/子代理不可加载，按 lead 预案以六问 inline 复盘。）

1. **做了什么**：新增闸门/补救两模块 + ingest 路由区接图 + 12 条新测试，共 10 文件。①新增 shipping_billing/graph/judge.py：compute_metrics(:100) 纯函数代码指标（金额/费用行/单号箱号/日期/表格/OCR置信度/金额混字母）、code_gate(:141) 只在明确垃圾信号判软失败、LlmJudge(:184) 独立小模型裁判只吹哨不抽取（JUDGE_MODEL 复用 LLM 端点/key、故障退化放行）、run_gate(:264) 混合（边界 case 才交裁判）；②新增 shipping_billing/graph/repair.py：RepairAgent.plan(:63) 自主选策略（空/混淆/低置信→ocr，其余→reextract 带反馈）、build_pending_review_record(:88) 六字段 null+原因入 audit_trail；③graph/graph.py 只动 ingest 路由区：新增 gate(:140)/repair(:147)/pending_review(:160) 节点、route_after_gate(:272) 按 retries<上限把关、_pending_review_updates(:63) 以 hard 区分硬崩溃计 failure/主动转人工不计 failure，synthesize/tail 逐条循环语义未动；④config.py:98-103 新增 gate_min_confidence/ingest_max_retries/judge_enabled/judge_model + from_env；⑤state.py:51-54 新增 gate_passed/gate_reason/retries/repair_strategy；⑥dispatcher.py:49 / pdf_loader.py:68,89 加 force_ocr 透传（补救重 OCR 入口，不改默认行为）；⑦pipeline.py:94-95,107 加 judge/repair 注入缝；⑧新增 tests/test_soft_fail_gate.py 12 条（假 OCR/假 LLM/假裁判端到端 + 闸门/补救确定性单元 + 误报回归钉子）。
2. **结果**：全量 108 passed（基线 96 一行未改 + 新增 12），junit 核验 tests=108 failures=0 errors=0 skipped=0。from_env 配置冒烟 + 图装配冒烟通过（7 节点就位、离线 judge=None）。commit 见 ticket-03（基于集成尖端 0a1fda6），未 merge。
3. **哪步顺**：动手前先读真实代码定位唯一行为陷阱——闸门判的是"读出来的文字够不够干净"而非"有没有抽到字段"，canned 注入文本（干净但无账单字段）无裁判时必须放行，否则误伤票01/02 注入回归；据此把"干净无信号"设为边界 case（有裁判交裁判、无裁判放行），代码指标只在明确垃圾信号（空文本/低置信/金额混字母）判软失败。先写 TDD 红（import judge 报 ModuleNotFoundError）再实现。
4. **哪步卡 + 根因**：①_corrupt_amount_tokens 误报箱号/单号（MRSU2571577,、NO.:GSDRHZMHK331281、NO.:OOLU4055331281 被判"金额混字母"）→端到端 5 红；根因：只看"含数字+含字母+含小数点"太宽。修复：token 须整体只由数字/./,/易混字母(OoIlSsBbZz)组成且至少含一个易混字母——箱号含 M/R/U/N 等非易混字母天然排除、纯数字金额无易混字母不误报；探针验证 4 样例零误报+5 合成混淆全命中。②text 模式图片无 OCR 可读→空文本→闸门软失败→重试耗尽→我原无条件记 failure，撞 test_ocr_modes_offline[text] 的 not result.failures；根因：把"主动转人工复核"误当"批次失败"。修复：PENDING_REVIEW 是复核结论非失败（与"方向不明转人工"同类，baseline 此类本就不进 failures），_pending_review_updates 加 hard 参数——硬崩溃计 failure、闸门救不回只产 PENDING_REVIEW 不计 failure。
5. **约定检查**：现有测试一行未改（git diff 仅 config/graph/ingest/pipeline + 新增 judge/repair/test_soft_fail_gate）；对外签名/六字段/费用名字典/OCR 网关契约未动；graph.py 改动严格限 ingest 路由区、未重排未动 synthesize/tail；注释 Google 风格中文、新文件 UTF-8 无 BOM；副产物全部在 .work/ 用完已删；未请求提权；只在 ticket-03 提交、未 merge。补救"某页糊→重OCR该页"按文件级重 OCR 实现（OCR 网关契约是文件级、不可改，页级需改契约故不做）。
6. **下一票注意**：票04 综合 agent 替换 synthesize 节点时，gate/repair/pending_review 已在 ingest 路由区就位、不冲突；state 新增 gate_passed/gate_reason/retries/repair_strategy 四字段供复用。票05 交叉验证插 tail 的 _apply_to_ledger 前。裁判 JUDGE_MODEL 默认关（judge_enabled=bool(JUDGE_MODEL)），离线/测试退化为纯代码指标不阻断；真裁判供票06 评测。PENDING_REVIEW 记录 source.extractor="gate"、confidence=0.0、六字段 null，评测/报告按 audit_trail.source.file 回溯文件。