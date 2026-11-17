# 质量证据与已知局限

本文件记录 v2 重构后的两类质量证据（离线可复现闸门 + 真模型端到端），以及经用户拍板确认的已知局限。所有数字均可按文末命令复现。

## 1. 离线回归闸门（可复现，不联网）

- 全量离线测试：**137 passed, 1 deselected**（deselected 为真模型 e2e，默认不跑）。现有基线测试一行未改，全部为新增。
- 评测闸门：**7/7**，recall / precision / operation / posting / field_accuracy 全 1.0。`cmd_eval` 显式钉住确定性兼容模式（`agent_required_key=False, llm_enabled=False`），不受环境命脉模式影响，保证可复现。
- 离线测试经主缝注入假 LLM / 假 agent LLM / OCR cache 回放，不依赖外网与 key。

## 2. 真模型端到端（质量证据，非硬闸门）

模型 qwen3.8-flash（OpenAI 兼容端点），OCR 走 cache 回放，4 个样例文件。真模型有非确定性，e2e 数字是质量证据、不当回归基线（回归基线是上面的离线 7/7）。

| 阶段 | recall / precision | operation_accuracy | posting_accuracy | 说明 |
|---|---|---|---|---|
| 票06 基线 | 1.0 / 1.0 | 0.4286 (3/7) | 0.5 (2/4) | debit note 角色判 null、NO_CHANGE↔UPDATE 混淆 |
| 票07 提示词强化 | 1.0 / 1.0 | 0.8571 (6/7) | 1.0 (4/4) | 四要素=推理输入非齐全清单；operation 语义+few-shot |
| 票09 日期语义+缓存指纹 | 1.0 / 1.0 | 0.8571 (6/7) | 1.0 (4/4) | PDF1 恢复 NO_CHANGE；差异转 PDF2 recipient 补位 |

全程 7 条记录 agent_confidence 非空（agent 真激活，无静默回落）。

## 3. 已知局限（用户拍板：停止逐字段追分、记债定稿）

**机制**：凡规则读不出、没有规则值防守的字段，LLM 草稿补位一旦补错，融合层 `extract/merge.py` 的"规则缺失即采纳 LLM"会无条件接受错值；尾巴 `pipeline_utils.compute_field_changes` 拿错值与台账正确值比较判 overwrite，把 NO_CHANGE 翻成 UPDATE。日期（PDF1，已用提示词日期语义约束修复）与接收方（PDF2，剩余）先后中招。

**剩余 1 处**：PDF2 Handling 35 期望 NO_CHANGE 实际 UPDATE。根因=真草稿把 recipient 误读为我方 NEPTUNE（规则 recipient=None、台账真实接收方 CIMC WETRANS），补位后与台账比对判 overwrite。

**决策**：逐字段加提示词约束是"打地鼠"（按住一个冒出一个），且在 4 样例上追非确定小模型满分属过拟合。经用户拍板停止追分，把本局限如实记录；0.86/1.0 + 全召回 + 安全网兜底为定稿成绩。结构性候选（规则缺失而 LLM 补位的双方字段不得驱动 overwrite）留 `docs/TECH_DEBT.md` 待后续。

## 4. 安全网（错提案进不了总账）

- 尾巴交叉验证 `graph/crosscheck.py`：落账前复用 validators/matcher/posting 复查金额/认亲/方向 + agent 低置信(<0.5)，任一不一致→PENDING_REVIEW、清空 updates、不落账。
- agent 装配异常回落确定性链时在 audit_trail.warnings 留痕，生产可区分真 agent 与静默回落。
- 软失败闸门救不回→PENDING_REVIEW 记录（六字段 null、原因入审计），不跑下游。

## 5. 复现命令

```bash
# 离线闸门（不联网）
python -m pytest -p no:cacheprovider          # 137 passed, 1 deselected
python -m freight_bill eval ...           # 7/7（cmd_eval 钉住确定性）

# 真模型端到端（需 LLM_API_KEY；OCR cache 回放；约 20-30 分钟）
python -m pytest tests/test_e2e_real_agent.py -m e2e
```

key 仅存 `.env`（已 gitignore），不入仓库/提交/日志。