# 手动端到端测试（亲自执行版）

一次跑完 4 个样例的全流程：读进来 → 双路抽取 → 综合 agent(ReAct+工具) → 死规则尾巴(交叉验证+落账)。
真模型联网（花 LLM key），OCR 走本地缓存回放（不上传网关）。约 20–30 分钟，中途不要中断。

## 1. 准备（你自己的终端）

```
conda activate code
cd /d D:\Deepseek_Workhome\航运agent
```

key 不用手动设：`.env` 里的 `LLM_API_KEY` 会在程序导入配置时自动读取。

## 2. 唯一一条命令（复制整行执行）

```
python -m freight_bill run --inputs data/samples --ocr-mode cache --out-dir output/e2e_manual --verbose
```

- `--verbose`：把每一段（ingest/extract/synthesize/tail）逐文件、逐条费用的动作打成 DEBUG 日志，这就是"全流程"。
- `--ocr-mode cache`：OCR 用 `data/ocr_cache` 回放，不依赖网关。
- `--out-dir output/e2e_manual`：真实六字段 JSON 落在这里，跑完逐条打开看。
- 命脉模式默认开 + `.env` 有 key → 自动用真综合 agent（不注入假实现）。

## 3. 用例清单（4 个文件 → 7 条期望记录）

期望值来自 `data/expectations/golden.json`。记账主体 = NEPTUNE SHIPPING LIMITED。

| 文件 | 单据/单号 | 费用 | 金额 | 期望 operation | 期望方向 |
|---|---|---|---|---|---|
| PDF示例账单1.pdf | Arrival Notice / CMDUCHN4053601 | On Carriage Charge | 245.0 USD | NO_CHANGE | AR |
| PDF 示例账单2.pdf | Debit Note / DC-108478 | Handling Charge | 65.0 USD | UPDATE | AR |
| PDF 示例账单2.pdf | Debit Note / DC-108478 | Detention Charge | 1310.4 USD | CREATE | AR |
| PDF 示例账单2.pdf | Debit Note / DC-108478 | Handling Charge(第二条) | 35.0 USD | NO_CHANGE | AR |
| 图片示例.png | Invoice / ORFR1618731 | Wharfage Administration Fee | 5.0 USD | PENDING_REVIEW | 空(转人工) |
| 图片示例.png | Invoice / ORFR1618731 | Wharfage | 21.9 USD | PENDING_REVIEW | 空(转人工) |
| 深圳容宇 PRE-ALERT….eml | Pre-Alert / OOLU4055331281 | Handling Charge | 50.0 USD | PENDING_REVIEW | 空(转人工) |

要点：
- PDF1 命中台账且金额一致 → NO_CHANGE；PDF2 的 Handling 65 是把台账 40 改成 65 → UPDATE；Detention 台账没有 → CREATE。
- 图片与 eml 的交易双方都不是我方（或只是邮箱地址）→ 不能凭单据类型判 AR/AP → PENDING_REVIEW、方向留空。这是"钱的事拿不准就转人工"的安全设计，不是 bug。

## 4. 跑完看什么

1. 终端 DEBUG 日志：按文件看四段顺序；synthesize 段能看到综合 agent 调工具（认亲/读原文/查台账/查字典）与提案理由；tail 段能看到交叉验证与落账。
2. 终端末尾摘要表：每条的 operation / 单号 / 费用 / 金额 / AR-AP / 置信度。
3. `output/e2e_manual/*.json`：逐条六字段 + `audit_trail`。确认 `audit_trail.agent_confidence` 非 null（证明真 agent 激活，而非静默回落确定性链）。

## 5. 已知局限（真模型非确定性，属质量证据非硬闸门）

PDF2 第二条 Handling 35 期望 NO_CHANGE，真模型偶尔判 UPDATE（草稿把接收方误读为我方 → 与台账比对判 overwrite）。机制与决策见 `docs/QUALITY_EVIDENCE.md`。其余 6 条应稳定命中。离线确定性闸门（`python -m pytest` 137 绿 / `eval` 7/7）不受此影响。