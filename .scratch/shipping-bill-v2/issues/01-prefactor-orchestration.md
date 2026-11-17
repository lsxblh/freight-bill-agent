# 01: 预重构——编排层解耦 + 依赖注入收口

**What to build:** 让"换图"变容易的铺路改动：把 LLM/OCR 的创建从编排层内部收口为可从主缝注入，消除编排层与抽取器之间的循环依赖。对外行为完全不变，现有测试仍全绿。完成后，主缝可以注入假 LLM/假 OCR，为后续 agent 测试与换图打好基础。

**Blocked by:** 00

**Status:** ready-for-agent

- [x] LLM 与 OCR 客户端可从主缝（入口参数/Settings）注入，编排层不再自行硬造
- [x] 编排层与抽取器的循环依赖消除
- [x] 现有全部测试不改一行仍全绿
- [x] 主缝能注入假 LLM/假 OCR 并跑通端到端

## Retro

（retro 技能为用户手动触发专用（disable-model-invocation），模型/子代理不可加载，按 lead 预案以六问 inline 复盘。）

1. **做了什么**：改 3 个源文件 + 新增 1 个测试。①shipping_billing/pipeline.py:59-88 BillingAgent.__init__ 新增 `llm` 关键字注入参数，`self.llm = llm or LlmExtractor(...)`（:79），与既有 ocr_client 注入缝对称；②build_document_chain 从 shipping_billing/agent.py 整体移入 shipping_billing/extract/document_extractor.py:79-105，DocumentExtractor.chain()（:164）删除 `from ..agent import ...` 延迟导入（原 :132），agent.py 仅保留兼容再导出；③新增 tests/test_injection.py：假 OCR 返回 canned 文本（规则抽不出任何字段）+ 假 LLM 返回完整草稿，端到端断言输出字段只能来自假客户端。
2. **结果**：全量 92 passed（基线 91 + 新增 1），junit.xml 核验 tests=92 failures=0 errors=0 skipped=0，退出码 0。现有测试文件一行未改（git status 仅 3 源文件修改 + 1 新测试）。commit 957fac6（ticket-01，基于 bd92cc3），未 merge。
3. **哪步顺**：红→绿主循环一次通过——红（TypeError: unexpected keyword argument 'llm'）→ 三处实现 edit → 注入测试字段断言全过。链搬移是纯搬移零行为改动，91 条存量测试全程绿。
4. **哪步卡 + 根因**：卡了两处，根因相同——**凭想象的接口写测试，而不是先读真实代码**。①第一版假 LLM 实现了 extract_document(raw)，真实受控面是 `extract(text, *, filename, kind)` + `.enabled` + `.stats`（llm_node_factory 调 llm.extract；pipeline.py:204 describe() 读 llm.stats）；②断言 LLM 合并提示出现在 ai_reasoning，实际落在 audit_trail.warnings 与 source.extractor="llm"（pipeline_utils.py:188-241：ai_reasoning 由 provenance/posting/match/operation 组成，warnings 才收 charge.warnings）。另有一次 edit 失败：old_string 按假设的签名拼，实际 __init__ 是 `settings: Settings | None = None` + keyword-only。教训：写假实现/金断言前，先读被调方真实签名与输出装配代码；edit 前先读当前原文。
5. **约定检查**：UTF-8 无 BOM；Google 风格中文注释（新增 Args/Returns docstring）；未引入新依赖；未动 OCR 网关契约/六字段契约/费用名字典；副产物（.work/ 下 pytest 日志与 junit.xml）用完已删；未请求提权；只在 ticket-01 提交、未 merge；测试只测主缝外部行为，不测内部节点。
6. **下一票注意（票02 LangGraph 换图）**：主缝注入面现为 `BillingAgent(settings, *, ledger=None, ocr_client=None, llm=None)`。假 LLM 受控面 = `.enabled` / `.extract(text, *, filename, kind)` / `.stats`；假 OCR 受控面 = `.settings` / `.process_file(path)` / `._load_cache(sha)` / `.stats`（pdf_loader.py:105 会直接调 _load_cache，假 OCR 必须提供）。build_document_chain 唯一定义在 extract/document_extractor.py（agent.py 是兼容再导出），换图时从抽取层导入或直接重写，不要让 extract 再反向依赖编排层。LLM 草稿入口是 llm_node_factory → llm.extract，merge_node 已处理 None（读失败留空不阻断），换图保持该语义。