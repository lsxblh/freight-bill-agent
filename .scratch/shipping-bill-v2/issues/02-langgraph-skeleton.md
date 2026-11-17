# 02: 搬骨架进 LangGraph（行为不变）

**What to build:** 把现有编排内部换成四段式 LangGraph StateGraph 骨架（读进来→双路抽取→综合→尾巴），此步所有节点仍为死规则、行为与现在完全一致。对外入口签名与结果形状（records/summary/failures/ledger/write_outputs）不变，现有测试一行不改当回归闸门，CLI 一键跑通。这是"搬骨架不改行为"的验收票。

**Blocked by:** 01

**Status:** ready-for-agent

- [ ] 内部编排为 LangGraph StateGraph，四段骨架就位，条件边预留
- [ ] 对外入口签名与结果形状不变
- [ ] 现有全部测试（含端到端 7 条金断言）全绿
- [ ] CLI `run <文件>` 跑通，输出六字段契约不变