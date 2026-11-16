# qualiscore — 抽样数据质量评分与缺失模式

客服工单抽样数据的月度质量报告工具。一次运行把一份导出的 CSV 走完
采信 → 画像 → 缺失 → 重复 → 离群 → 评分 → 导出，每个数字都能追回原始行号、
原因码与运行清单。

- 一个 FastAPI 服务、Jinja2 模板、原生 HTML/CSS/JS、SQLite 存数据集与运行记录
- 端口 `18204`（容器内 `8000`）
- 规则唯一来源：`app/config.py`（权重、容忍度、刻度、阈值）；原因码唯一来源：`app/reasons.py`

## 能力

| 能力 | 说明 |
|---|---|
| 采信 | 编码回退链、BOM 剥离、重复表头分段、列数校验、末行截断判定、空态三分 |
| 画像 | 逐格四态（缺失 / 格式不合格 / 写法未定 / 有值）、填充率、基数、格式率、取值范围、高频取值 |
| 数值写法裁定 | 逐 token 语法证明小数位置；`1,204` 这类两读法都成立的格记写法未定，不进任何分数 |
| 缺失模式 | 单元格账、连续缺失段（同列连续 ≥3 格）、缺失签名分组、共缺对 |
| 重复判定 | 全行哈希账与业务键账分开；同键不同值的两行都保留原值并列出冲突列 |
| 稳健离群 | 中位数 + MAD 稳健 z，MAD=0 回退 IQR/1.349，离散度低于刻度下限判退化 |
| 评分 | 100 − 缺失 30 − 重复 25 − 离群 20 − 格式 25，每维按容忍度封顶，Decimal 计分 |
| 范围说明 | 报告页与九个导出同源给出可评 / 排除格数、裁定规则与逐格原值行号 |
| 批次与导出 | run_id、输入 sha256、版本自增、同输入幂等复用、陈旧提交 409、原子写入、快照校验与回退、九种 CSV + 清单 JSON |

## 功能模块

| 模块 | 入口 | 核心文件 | 测试 |
|---|---|---|---|
| M1 数据采信 | `POST /api/datasets/import`、`POST /api/datasets/upload` | `app/modules/m1_ingest/decode.py`、`reader.py`、`sniff.py`、`service.py` | `tests/test_m1_ingest.py` |
| M2 字段画像 | `GET /api/profile`、`POST /api/profile/cell`、页面 `/profile` | `app/modules/m2_profile/formats.py`、`column_stats.py`、`profile.py` | `tests/test_m2_profile.py` |
| M3 缺失模式 | `GET /api/missing`、页面 `/missing` | `app/modules/m3_missing/cells.py`、`runs.py`、`matrix.py` | `tests/test_m3_missing.py` |
| M4 重复判定 | `GET /api/duplicates`、页面 `/duplicates` | `app/modules/m4_duplicates/hashdup.py`、`keydup.py`、`groups.py` | `tests/test_m4_duplicates.py` |
| M5 稳健离群 | `GET /api/outliers`、页面 `/outliers` | `app/modules/m5_outliers/robust.py`、`zscore.py`、`flags.py` | `tests/test_m5_outliers.py` |
| M6 评分与批次 | `POST /api/score`、`GET /api/batches`、`GET /api/reconcile`、页面 `/batches` | `app/modules/m6_score/score.py`、`grade.py`、`runstore.py`、`service.py` | `tests/test_m6_score.py` |
| M7 报告导出 | `GET /`、`GET /api/export/*` | `app/modules/m7_export/report.py`、`csv_out.py`、`manifest_out.py` | `tests/test_m7_export.py` |

## 核心规则

1. **单元格状态**：空串、空白串、哨兵统一为缺失；无法解析或越界为格式不合格；
   数值分隔符语法无法唯一确定小数位置的记写法未定；其余为有值。
   `单元格 = 缺失 + 写法未定 + 有值` 恒成立。
2. **数值写法**：`1,204.5` 只有美式读法成立、`1.204,5` 只有欧式读法成立，两者都归一为
   1204.5；`1,204` 与 `1.204` 两种读法都成立且数值不同 → 写法未定，不取值、
   不进缺失/格式/离群/评分的任何分子分母，只保留原值与行号。
   全按一种写法解释会放大或缩小小数，逐个猜测会让相似工单得到两种口径，都不允许。
3. **重复两账**：全行哈希用于「行 = 唯一 + 重复」；业务键重复单独成账，
   同键不同值时两行都进冲突表，逐列列出冲突原值，不做覆盖。
4. **稳健离群**：`z = 0.6745 × (x − 中位数) / MAD`；MAD=0 时回退
   `spread = 0.6745 × IQR / 1.349`；离散度低于「列刻度 × 5」或 MAD 与 IQR 同为 0
   判退化，该列不判离群。阈值固定 `|z| > 3.5`。
5. **评分**：每维扣分 = 封顶分 × min(1, 实际比率 ÷ 容忍度)，量化 0.01；
   总分 = 100 − 扣分之和，恒在 [0, 100]，`总分 + 扣分之和 = 100` 逐位成立。
   可评范围 = 单元格 − 写法未定；报告页与导出都标明这个范围，
   覆盖率低于 95% 时结论标「仅覆盖可评格」。
6. **幂等与版本**：同名数据集按内容 sha256 + 规则版本 + 阈值摘要识别，
   同输入重跑返回同一运行记录，不重复计分；客户端版本落后于库里版本时返回 409。
   数据集原文与运行记录同事务提交，失败整体回滚；快照带 sha256，读坏回退到
   同数据集上一完整版本并显式标注，坏记录保留不删。

## 启动

```bash
docker compose up -d --build
open http://127.0.0.1:18204/
```

页面首次打开时库里没有运行记录，选一个样例点「采信并评分」即可看到报告。
样例数据随仓库自带，不需要准备文件。

## 演示步骤

1. 打开 `/`，样例选 `tickets_dirty.csv`，点「采信并评分」→ 报告页显示 65.24 分 C 级。
2. `/profile` 看待映射列与格式不合格；`/missing` 看连续缺失段与共缺对。
3. `/duplicates` 看全行重复组与 `T-2026-000204` 的冲突列。
4. `/outliers` 看 `first_response_min` 的极端值与 `satisfaction` 的退化标记。
5. `/batches` 看版本、阈值与五项对账；再点「重跑」验证幂等复用。
6. 任一页面底部导出 CSV，或直接 `curl /api/export/manifest.json` 取运行清单。
7. 样例选 `tickets_mixed_notation.csv`：报告页「本批判定范围」显示 118 / 120 格可评，
   排除的 2 格 `1,204` 与 `1.204` 列出两种读法与原始行号；`/api/export/notation.csv` 可逐格追溯。

## API 与页面

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET | `/api/datasets` | 可用样例与已存数据集 |
| POST | `/api/datasets/import` | 采信样例（`{"sample": "..."}`） |
| POST | `/api/datasets/upload` | 上传 CSV（multipart，`file` + `encoding`） |
| GET | `/api/datasets/{name}/rows` | 逐行诊断预览 |
| GET | `/api/profile` | 字段画像 |
| POST | `/api/profile/cell` | 单格试解析 |
| GET | `/api/missing` | 缺失账、连续段、共缺对 |
| GET | `/api/duplicates` | 两套重复账与冲突 |
| GET | `/api/outliers` | 逐列稳健统计与离群格 |
| POST | `/api/score` | 跑一次评分（可带 `expected_version`） |
| GET | `/api/batches` | 运行历史 |
| GET | `/api/reconcile` | 五项对账 |
| GET | `/api/export/report.csv` 等 | 九种 CSV（含 `assessable.csv`、`notation.csv`）+ `manifest.json` |
| GET | `/`、`/profile`、`/missing`、`/duplicates`、`/outliers`、`/batches` | 六个页面 |

## 目录

```text
app/
  main.py config.py errors.py reasons.py manifest.py store.py web.py
  modules/m1_ingest … m7_export/   每个模块 handler + service + 3 个规则/读写文件
  templates/  base.html + 6 页
  static/     theme.css layout.css components.css app.js table.js charts.js
docs/         DEPTH.md DATA-DICTIONARY.md RUNBOOK.md
sample_data/  10 个 CSV（脏、干净、写法混写、GB18030、BOM、截断、常量列、仅表头、空行、空文件）
tests/        conftest.py + 7 个模块测试 + e2e + invariants + fixtures
```

## 定义与已覆盖

列契约、单位、刻度、空值三态、去重键、评分常量、等级阈值见
`docs/DATA-DICTIONARY.md`；陷阱登记与测试映射见 `docs/DEPTH.md`。

不覆盖：多表关联、增量追加（每次按整份文件算）、图表之外的统计检验、
自定义权重（权重与容忍度写在配置里，改完需重新运行）。
