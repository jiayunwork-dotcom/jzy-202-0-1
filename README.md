# RCCP — 粗能力计划服务

面向注塑/装配工厂的粗能力计划（Rough-Cut Capacity Planning）后端：
维护资源清单与能力日历的多个版本，把主生产计划（产品×周数量）展开成
各工作中心各周的负荷，与能力对比；计划一改，负荷立即可见。

- Python 3.12 + FastAPI，存储 SQLite（库文件放挂载卷），数值用 NumPy
- 草稿负荷矩阵内存中增量维护，单元格编辑即时反映到负荷
- 已发布计划只读，负荷结果绑定计划/清单/日历三个版本，可对比、可按新清单重算
- 设计取舍（小数提前量拆分、逾期负荷、增量维护）见 [docs/DESIGN.md](docs/DESIGN.md)

## 运行

```bash
pip install -r requirements.txt
uvicorn rccp.asgi:app --host 0.0.0.0 --port 8000
```

环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `RCCP_DB_PATH` | `/data/rccp.db` | SQLite 库文件路径（挂载卷上） |
| `RCCP_WEEKS` | `20` | 计划期长度（周），周号 1..N |

## Docker

```bash
docker build -t rccp .
docker run -p 8000:8000 -v rccp-data:/data rccp
```

镜像基于 `python:3.12-slim`，只对外暴露 8000/HTTP，数据落在挂载卷 `/data`。

## 测试

```bash
pip install -r requirements-dev.txt
pytest
```

## API 一览（前缀 `/api`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/meta` | 计划期周数、当前清单/日历版本、注册表、版本列表 |
| POST | `/bom-versions` | 录入资源清单新版本：`{lines: [{product, work_center, hours_per_unit, lead_weeks}]}` |
| GET | `/bom-versions`、`/bom-versions/current`、`/bom-versions/{v}` | 查询清单版本 |
| POST | `/calendar-versions` | 录入能力日历新版本：`{entries: [{work_center, week, available_hours}]}` |
| GET | `/calendar-versions`、`/calendar-versions/current`、`/calendar-versions/{v}` | 查询日历版本 |
| GET | `/draft` | 草稿全部非零格 + 提示 |
| PUT | `/draft/cell` | 逐格编辑（CAS）：`{product, week, qty, expected_old_qty}` |
| POST | `/draft/batch` | 批量导入：`{cells: [...]}`，整批校验、按顺序生效 |
| GET | `/draft/load?work_center=&week=` | 草稿负荷矩阵，可按工作中心/周切片 |
| GET | `/draft/overloads` | 草稿超负荷清单 + 逾期负荷 |
| POST | `/plan-versions` | 发布草稿为只读版本（绑定当前清单/日历版本） |
| GET | `/plan-versions`、`/plan-versions/{v}` | 版本列表与详情 |
| GET | `/plan-versions/{v}/load?work_center=&week=` | 已发布版本的负荷（绑定版本不变） |
| GET | `/plan-versions/{v}/overloads` | 已发布版本的超负荷清单（用绑定日历判定） |
| GET | `/plan-versions/{v}/results` | 该版本的全部负荷结果（publish + recompute） |
| GET | `/plan-versions/compare?a=&b=&top=` | 版本对比：差异最大的格子、超负荷翻转、逾期变化 |
| POST | `/plan-versions/{v}/recompute` | 按新清单（和/或新日历）重算，返回新结果与对比 |

## 三分钟上手

```bash
# 1. 录入资源清单：P1 在 W1 每件 2h 提前 1 周，在 W2 每件 0.5h 当周
curl -X POST localhost:8000/api/bom-versions -H 'content-type: application/json' -d '{
  "lines": [
    {"product": "P1", "work_center": "W1", "hours_per_unit": 2.0, "lead_weeks": 1.0},
    {"product": "P1", "work_center": "W2", "hours_per_unit": 0.5, "lead_weeks": 0.0}
  ]}'

# 2. 录入能力日历：W1/W2 每周 40h
curl -X POST localhost:8000/api/calendar-versions -H 'content-type: application/json' -d '{
  "entries": [
    {"work_center": "W1", "week": 2, "available_hours": 40},
    {"work_center": "W2", "week": 3, "available_hours": 40}
  ]}'

# 3. 草稿：P1 第 3 周完工 10 件（新格子旧值为 0）
curl -X PUT localhost:8000/api/draft/cell -H 'content-type: application/json' -d \
  '{"product": "P1", "week": 3, "qty": 10, "expected_old_qty": 0}'

# 4. 看负荷：W1 第 2 周 20h、W2 第 3 周 5h
curl localhost:8000/api/draft/load

# 5. 发布、改草稿再发布、对比两个版本
curl -X POST localhost:8000/api/plan-versions -H 'content-type: application/json' -d '{"note": "v1"}'
curl -X PUT localhost:8000/api/draft/cell -H 'content-type: application/json' -d \
  '{"product": "P1", "week": 3, "qty": 30, "expected_old_qty": 10}'
curl -X POST localhost:8000/api/plan-versions -H 'content-type: application/json' -d '{"note": "v2"}'
curl 'localhost:8000/api/plan-versions/compare?a=1&b=2'
```

## 模块结构

```
rccp/
  config.py       配置（计划期周数、库文件路径）
  errors.py       领域异常 → HTTP 状态码
  db.py           SQLite 连接与建表
  bom.py          资源清单与版本
  capacity.py     能力日历与版本
  explosion.py    负荷全量展开（纯函数）
  incremental.py  草稿负荷矩阵的增量维护
  plans.py        草稿、已发布版本、负荷结果持久化
  compare.py      超负荷分析与结果对比
  service.py      应用服务层（装配 + 写锁 + 校验）
  schemas.py      请求模型
  api.py          FastAPI 路由
  main.py         应用工厂
  asgi.py         uvicorn 入口
tests/            pytest（展开规则、增量≡全量、接口、并发、版本、重启）
docs/DESIGN.md    设计决策
```
