# 00: 基线修复——让现有测试在 code 环境真绿

**What to build:** 重构前的安全网校准。现有成品在当前 conda code 环境里自己就没通过自己的测试，必须先修到全绿，才能作为后续"搬骨架不改行为"的回归闸门。本票只修基线 bug，不做任何架构重构。环境依赖 openpyxl/xlrd 已由 lead 安装完毕。

已知症状（需逐一诊断根因并修复）：
- `test_extract_rules.py::test_debit_note_pdf`：单号被抽成 `DC-108478D/C`，期望 `DC-108478`。疑似 Debit/Credit 单号正则字符类允许 `/` 导致贪心吞掉尾部 "D/C"（extract/rules.py 的 _REFERENCE_PATTERNS）。
- `test_pipeline.py::test_end_to_end_samples` / `test_output_json_is_valid_and_complete`：records 4 != 7，样例 debit note（PDF 示例账单2.pdf）的 3 条费用记录缺失，需诊断该 PDF 在当前解析栈下的抽取为何丢行。
- `test_pipeline.py::test_duplicate_document_detection`：重复单据未转人工，疑与上述 debit note 抽取/单号 bug 连带。

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] 全量 `pytest` 在 conda code 环境全绿（含端到端 7 条金断言）
- [ ] 只修基线 bug，不改变对外契约、不做架构重构
- [ ] 修复处有对应回归测试佐证（先红后绿）
- [ ] 不新增费用名字典条目、不改 OCR 网关契约