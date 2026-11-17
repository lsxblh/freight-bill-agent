# 02: 搬骨架进 LangGraph（行为不变）

**What to build:** 把现有编排内部换成四段式 LangGraph StateGraph 骨架（读进来→双路抽取→综合→尾巴），此步所有节点仍为死规则、行为与现在完全一致。对外入口签名与结果形状（records/summary/failures/ledger/write_outputs）不变，现有测试一行不改当回归闸门，CLI 一键跑通。这是"搬骨架不改行为"的验收票。

**Blocked by:** 01

**Status:** done (commits 46c3346+0a1fda6, 96 passed, merged)

- [x] 内部编排为 LangGraph StateGraph，四段骨架就位，条件边预留
- [x] 对外入口签名与结果形状不变
- [x] 现有全部测试（含端到端 7 条金断言）全绿
- [x] CLI `run <文件>` 跑通，输出六字段契约不变

## Retro

（retro 技能为用户手动触发专用（disable-model-invocation），模型/子代理不可加载，按 lead 预案以六问 inline 复盘。）

1. **做了什么**：新增 graph 包 + 编排换图 + 4 条新测试，共 7 文件。①新增 shipping_billing/graph/state.py:19 BillingState（TypedDict，单文件内三层模型流转：raw→documents/queue→records/assessments/failures）；②新增 shipping_billing/graph/graph.py:41 build_billing_graph(owner)：四段节点 ingest(:62)/extract(:83)/synthesize(:108)/tail(:142)，三条条件边 route_after_ingest(:184)/route_after_extract(:188)/route_after_tail(:194)，synthesize⇄tail 逐条费用循环；③shipping_billing/pipeline.py:97 新增 graph() 懒编译缝，run()(:107) 的逐文件循环改为 graph.invoke(:125)+收拢结果，删掉被搬进图的 retry/load_document/validate_document 导入；④requirements.txt 与 pyproject.toml 登记 langgraph>=1.0,<2（环境已有 1.2.11，pip install 确认满足）；⑤新增 tests/test_graph_skeleton.py：2 条行为钉子（单据内重复转人工、同名费用逐条落账交错）+ 2 条骨架验收（四段节点+条件边、run 真正走图）。
2. **结果**：全量 96 passed（基线 92 一行未改 + 新增 4），junit 核验 tests=96 failures=0 errors=0 skipped=0。CLI run 单文件 3 记录、六字段契约脚本核验通过；eval 7/7（recall/precision/operation/posting 全 1.0）；ingest 失败隔离用注入异常探针验证（files_failed=1、其余文件继续）。commit 46c3346（ticket-02，基于 27265bd），未 merge。langgraph 版本 1.2.11。
3. **哪步顺**：动手前先读 matcher.py/pipeline_utils.py/merge.py/validators.py 真实代码，识别出唯一行为陷阱——认亲读活台账、重复按条登记，所以必须逐条"装配→落账→登记"交错，不能整单批量装配再落账；据此把图设计成 synthesize⇄tail 逐条循环，并先写 2 条行为钉子在旧代码上跑绿钉住基线，换图后一次转绿。langgraph API 先写 .work 探针验证（StateGraph/条件边/get_graph）再动手，主循环零返工。
4. **哪步卡 + 根因**：①CompiledStateGraph 在 1.2.11 不从 langgraph.graph 导出（ImportError），改从 langgraph.graph.state 导入——根因：凭旧版记忆写导入，没先探针导出面；②失败隔离探针连红两次：不存在的文件被 collect_input_files 静默跳过、损坏 PDF/xlsx 被 loader 内部兜底吞掉，自然样本触发不了 ingest 异常——根因：loader 容错太彻底，最后用 mock 向图模块注入 load_document 异常才验证到路由；③_RECURSION_LIMIT 私有名跨模块引用不妥，改为公开常量 RECURSION_LIMIT。
5. **约定检查**：现有测试一行未改（git diff 仅 pipeline/requirements/pyproject + 新增文件）；对外签名/结果形状/六字段/费用名字典/OCR 网关契约未动；注释 Google 风格中文、新文件 UTF-8 无 BOM 逐一核验；依赖单向 pipeline→graph→extract/matching/validation（graph 只在 TYPE_CHECKING 引用 pipeline）；副产物全部在 .work/ 用完已删；未请求提权；只在 ticket-02 提交、未 merge。
6. **下一票注意**：票03 软失败闸门接 route_after_ingest（现仅失败隔离），补救循环可复用 ingest 节点重走；票04 综合 agent 替换 synthesize 节点，逐条队列 state["queue"] 与 doc_issues 缓存已就位——注意尾巴现在是"装配一条立即落账"，若 04 改成整单提案再落账，会改变"同单据后条认亲先条 CREATE 行"的语义，行为钉子 test_same_name_charges_interleave_with_ledger 钉住了现行为，届时若有意变更须显式更新该测试并说明；票05 交叉验证插在 tail 节点 _apply_to_ledger 之前。逐条循环每条费用耗 2 超步，RECURSION_LIMIT=10000 已放宽（默认 25 会卡多费用单据）。langgraph 1.2.11 的 CompiledStateGraph 在 langgraph.graph.state，不在 langgraph.graph。