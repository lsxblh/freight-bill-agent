# 01: 预重构——编排层解耦 + 依赖注入收口

**What to build:** 让"换图"变容易的铺路改动：把 LLM/OCR 的创建从编排层内部收口为可从主缝注入，消除编排层与抽取器之间的循环依赖。对外行为完全不变，现有测试仍全绿。完成后，主缝可以注入假 LLM/假 OCR，为后续 agent 测试与换图打好基础。

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] LLM 与 OCR 客户端可从主缝（入口参数/Settings）注入，编排层不再自行硬造
- [ ] 编排层与抽取器的循环依赖消除
- [ ] 现有全部测试不改一行仍全绿
- [ ] 主缝能注入假 LLM/假 OCR 并跑通端到端