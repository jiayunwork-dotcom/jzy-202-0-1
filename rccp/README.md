# RCCP — 粗能力计划后端服务

面向注塑+装配工厂的**粗能力计划（Rough-Cut Capacity Planning）**服务：维护资源清单
（工艺路线工时）、能力日历、主生产计划（MPS）的**版本**，向量化展开每个工作中心
每周的负荷，草稿**增量维护**、改一个格子立刻看到负荷变化；发布后冻结，支持任意
两版对比、按新清单/新日历重算。

* Python 3.12 · FastAPI · SQLite（库文件在挂载卷 `/data`）· NumPy
* 只有 HTTP 接口（默认 8000 端口），无 UI

---

## 1. 快速开始

```bash
docker build -t rccp:latest .
docker run -p 8000:8000 -v $(pwd)/data:/data \
  -e RCCP_HORIZON_WEEKS=20 rccp:latest
```

无 Docker 时：

```bash
uv sync                                 # 安装依赖（Python 3.12）
RCCP_DATA_DIR=./data uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
uv run pytest -q                        # 全部测试
```

环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `RCCP_DATA_DIR` | `/data` | SQLite 库文件目录（挂载卷） |
| `RCCP_DB_NAME` | `rccp.db` | 库文件名 |
| `RCCP_HORIZON_WEEKS` | `20` | 计划期周数（建库时固定，所有版本共用） |
| `RCCP_LEAD_SPLIT` | `proportional` | 小数提前量默认拆分规则，可被每个草稿覆盖 |
| `RCCP_PRE_HORIZON` | `overdue` | 期初之前负荷的默认处理，可被每个草稿覆盖 |

## 2. 核对例子

产品 P 第 3 周完工 10 件：W1 每件 2h / 提前 1 周；W2 每件 0.5h / 提前 0 周
→ **W1 第 2 周 20h，W2 第 3 周 5h**。`tests/test_explosion.py::test_reference_example`
与 `tests/test_api.py::test_full_workflow` 均断言该结果。

## 3. 展开规则（重要：乐观 / 悲观口径）

设某产品第 `t` 周完工 `q` 件，在工作中心上单件工时 `h`、提前量 `lead` 周，
负荷 `q·h` 的目标周为 `center = t − lead`（内部按 0-based 计算）。

### 3.1 提前量为小数时如何拆到相邻两周（`lead_split`）

| 取值 | 行为 | 负荷图倾向 |
|---|---|---|
| `proportional`（默认） | `center` 非整数时按比例拆：前一周得 `frac`、后一周得 `1−frac`（例：1.5 周提前、20h → 前后各 10h） | **中性**。数学期望无偏，总量严格守恒 |
| `floor`（整体归前一周） | 全部落在 `floor(center)` | **偏悲观/保守**。一律提前占用能力，更早暴露瓶颈，但会把本来落在后续周的缓冲吃掉 |
| `ceil`（整体归后一周） | 全部落在 `ceil(center)` | **偏乐观**。一律推后显示，临近完工才显出压力，可能造成误判 |

整数提前量在三种规则下一致，只落一个桶。

### 3.2 目标周落在计划期开始之前怎么办（`pre_horizon`）

| 取值 | 行为 | 负荷图倾向 |
|---|---|---|
| `overdue`（默认） | 单独累计为**逾期负荷**（每个工作中心一个数），不进任何计划周 | **中性、可核对**。总量守恒成立（周负荷之和 + 逾期 = 需求总工时），逾期本身是一个需要响应的信号 |
| `accumulate`（累到第一周） | 全部压到第 1 周 | **偏悲观**。第 1 周利用率虚高、常显超负荷，适合“宁可前置备货”的管理口径 |
| `discard`（丢弃） | 直接不计 | **偏乐观**。负荷图最好看，但**总量守恒被破坏**，少掉的正是被丢弃部分；仅在确认期初确无在制时使用 |

例如 10 件 × 2h、提前 1.5 周、第 2 周完工：`proportional + overdue` 下
第 1 周 10h、逾期 10h；`accumulate` 下第 1 周 20h；`discard` 下只剩第 1 周 10h。

### 3.3 必须满足的性质（均有测试）

1. **总量守恒**：所有工作中心 × 所有周负荷之和 **+ 逾期部分** =
   Σ 产品数量 × 单件总工时（`overdue` / `accumulate` 下成立；`discard` 例外，见上）。
2. **线性缩放**：全部计划数量乘 k，负荷（含逾期）逐格乘 k。
3. **改回复原**：某格子数量改回原值，该草稿负荷恢复到修改前（逐格相等）。
4. **零提前量**：提前量全为 0 时负荷就落在完工当周。
5. **增量≡全量**：随机 400 步修改链上，每一步增量矩阵与从头展开逐格相同
   （含落库结果行）。见 `tests/test_incremental.py`。

## 4. 增量维护 vs 从头展开 —— 设计选择与代价

**选择：维护负荷矩阵，单元格变化时增量更新；每步都能用一次全量展开校验。**

* 全量展开本身已经很快（400 产品 × 1800 清单行 × 40 工作中心 × 20 周：
  NumPy 向量化约 **3ms**），但交互路径上仍采用增量：
  改一个格子只重算**该产品**一条数量向量（复杂度只与该产品的清单行数有关），
  `L += 贡献(new) − 贡献(old)`，实测**均值 0.17ms / p95 0.33ms**，计划员无感。
* 批量 `merge` 只重算受影响产品；批量 `replace` 与发布走一次全量。
* 随机一致性证明：`tests/test_incremental.py` 用 400 步随机操作（逐格编辑 /
  批量合并 / 整表替换）断言**每一步**增量矩阵、逾期向量、持久化 blob 都与
  `explode_all(当前数量表)` 逐格 `allclose`。

| 维度 | 影响 |
|---|---|
| **内存** | 每个打开的草稿常驻：P（产品×周，float64）、L（工作中心×周）、逾期、能力矩阵与一张按产品索引的清单行数组。400×20 + 40×20 量级约几十 KB/草稿；另有每把草稿锁。整个规模 RSS 约 60MB。只有被打开过的草稿才装载 |
| **并发写入** | 同一进程内每草稿一把锁串行化写；不同草稿互不阻塞。同格编辑用乐观锁（CAS，见 §7）。写 SQLite 全局一把进程锁，事务都很短（单行 upsert + 一行结果 blob 覆写）。多进程/多副本部署时需把 CAS 下沉为 SQL 条件更新并让增量计算由单一写者执行（见 §10） |
| **重启恢复** | `plan_cell`（数量行）是**唯一真值**；草稿负荷结果行只是快照。启动 `bootstrap()` 装载所有草稿：按数量表全量展开，与落库 `draft` 结果逐格核对——一致直接热机；不一致（仅可能在事务边界外被强杀）以数量表为准修复。矩阵 blob 反序列化 + 一次全量展开，400 产品规模启动为毫秒级 |
| **清单/日历改动** | 版本是不可变快照：草稿绑定已发布版本，新版本不影响任何已有结果。已发布计划只能经显式 `recalc` 生成新结果。避免了“清单悄悄改、负荷没人知道按哪版算”的问题 |

## 5. 版本模型（解决“上周那版负荷按哪个计划算”）

* `routing_version` / `capacity_version`：草稿可变 → 发布后只读；新草稿可从
  任意已发布版本复制。
* 计划：`draft`（可编辑，id 自定义或 `draft-N`）→ 发布生成 `plan-N`
  （只读，序号单调），并把当时数量表与负荷矩阵各复制一份冻结。
* 每个 `load_result` 行都带 **计划 id + policy + 资源清单版本 + 能力日历版本
  + 两条展开策略 + 工作中心行序**，结果与输入严格绑定、可复现。
  * policy：`draft`（每草稿一行，随编辑原地更新）/ `published`（发布冻结）
    / `recalc`（每次重算追加一行，不覆盖旧结果）。
* `GET /api/plans/{id}/results` 可看到该计划所有结果及其版本绑定。

## 6. 接口一览（详见各路由，均为 JSON）

```
POST   /api/products | /api/workcenters            主数据
GET    /api/products | /api/workcenters

POST   /api/routings/versions            {copy_from?, note?, lines?} 建草稿(可复制)
GET    /api/routings/versions            版本列表
GET    /api/routings/versions/{v}        明细（含全部行）
PUT    /api/routings/versions/{v}/lines  {lines:[...]} 草稿整体替换
POST   /api/routings/versions/{v}/publish

POST   /api/capacities/versions …        结构对称（weeks 代替 lines）
PUT    /api/capacities/versions/{v}/weeks
POST   /api/capacities/versions/{v}/publish

POST   /api/plans/drafts                 {id?, name?, routing_version?, capacity_version?,
                                          lead_split?, pre_horizon?, cells?}
GET    /api/plans?status=draft|published
GET    /api/plans/{id}                   摘要（含 uncovered_products、总负荷）
GET    /api/plans/{id}/cells
GET    /api/plans/{id}/uncovered         未被清单覆盖的产品提示
PUT    /api/plans/{id}/cell              {product, week, quantity, expected_quantity?}
POST   /api/plans/{id}/import            {mode:"merge"|"replace", cells:[...]}
POST   /api/plans/{draft_id}/publish     → plan-N + 冻结结果
POST   /api/plans/{plan_id}/recalc       {routing_version?, capacity_version?} → 新结果+对比
GET    /api/plans/{left}/compare/{right} 两版计划：top_deltas + flips

GET    /api/plans/{id}/load              负荷矩阵（?workcenter=&week_from=&week_to=&result_id=）
GET    /api/plans/{id}/overloads         超负荷清单（load > capacity，按超出量降序）
GET    /api/plans/{id}/results           该计划全部结果
GET    /api/results/{rid}                结果元信息
GET    /api/results/{a}/compare/{b}      任意两个结果对比（recalc 与旧结果也可）
GET    /health
```

负荷切片每行同时返回 `load / capacity / utilization`（能力为 0 的周利用率为
`null`，因为任何正负荷都算超载、比例无定义）。未在能力日历中出现的
工作中心×周按 **0 可用工时**处理（保守口径，任何负荷都会被点名为超负荷）。

## 7. 同格并发协议

编辑按到达顺序在草稿锁内生效。改同一格时后到者必须带 `expected_quantity`
（它基于的旧值）：

* 与当前值一致 → 接受；
* 不一致 → **409**，返回 `{current_quantity}`，调用方刷新后可重试；
* 不带 `expected_quantity` → 无条件覆盖（用于“我就要改成这个值”的场景）。

不同格子的并发编辑互不冲突；`tests/test_planner.py::test_concurrent_*`
用多线程验证。

## 8. 校验与拒收（统一 422，404/409 另有语义）

* 数量为**负或非整数** → 拒收（负数在 pydantic 层、非整数在服务层）；
* 单件工时为负、提前量为负、能力为负 → 拒收；
* 周号超出 `1..HORIZON_WEEKS`、引用不存在的产品/工作中心 → 拒收；
* 同一 (产品, 工作中心) 清单行重复、同一 (工作中心, 周) 能力重复 → 拒收；
* 草稿只能绑定**已发布**的清单/日历版本；
* **草稿出现清单未覆盖的产品不报错**（它们贡献 0 工时），但在草稿摘要与
  `/uncovered` 接口中列出产品清单并给出提示；发布、重算响应中也带该清单。

## 9. 模块划分

| 文件 | 职责 |
|---|---|
| `app/config.py` | 配置（数据目录、计划期、默认策略） |
| `app/database.py` | SQLite 连接、schema（WAL）、矩阵 blob 序列化 |
| `app/explosion.py` | 纯数值：全量向量化展开、单产品增量、能力矩阵、对比、超负荷 |
| `app/registry.py` | 产品/工作中心主数据与存在性校验 |
| `app/routing.py` | 资源清单版本（草稿/发布/复制/替换） |
| `app/calendar.py` | 能力日历版本 |
| `app/planner.py` | 草稿运行时状态、增量维护、CAS、批量导入、发布、重算、重启恢复 |
| `app/results.py` | 结果持久化、切片、超负荷、版本对比 |
| `app/models.py` | Pydantic 接口模型 |
| `app/routers/*.py` + `app/main.py` | HTTP 接口 |
| `tests/` | pytest：引擎性质、随机增量一致性、服务、HTTP、重启 |

`explosion.py` 不接触数据库，可独立用于离线核对。

## 10. 部署边界与后续扩展

* 镜像 `python:3.12-slim`，仅暴露 HTTP；卷挂载 `/data`。
* 当前并发模型针对**单进程多线程**（uvicorn 单 worker）：FastAPI 同步端点在线程池
  执行，进程锁 + 每草稿锁保证正确。水平扩容到多副本时，建议：
  1. CAS 改为 SQL：`UPDATE plan_cell SET quantity=? WHERE plan_id=? AND product=?
     AND week=? AND quantity=?`，按 rowcount 判冲突；
  2. 指定单一写者（或写分片按 draft_id）持有增量矩阵，其余副本只读已落库结果；
  3. 结果 blob 的读写本身是原子的（DELETE+INSERT 在一个事务里）。
* 计划期在建库时固定，保证所有版本矩阵同形状、可直接相减；需要滚动计划期时
  应新建库或增加显式迁移（目前刻意不支持热改，避免跨版本坐标错位）。
