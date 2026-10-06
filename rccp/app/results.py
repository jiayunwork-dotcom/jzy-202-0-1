"""负荷结果的持久化、切片查询、超负荷清单与版本对比。

结果行（load_result）记录：
* 所属计划（草稿或已发布版本）；
* 计算时绑定的资源清单版本、能力日历版本与两条展开策略——结果与输入版本
  严格绑定，输入版本不可变则结果可复现；
* 负荷矩阵、逾期负荷以及工作中心行序。

policy：
* ``draft``    草稿实时结果（每次编辑原地更新一行）；
* ``published`` 计划发布时的冻结快照；
* ``recalc``    已发布计划按新清单/新日历重算产生的结果（可多行）。
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np

from .database import Database
from .errors import NotFoundError, ValidationError
from .explosion import compare_loads, overload_cells


class ResultService:
    def __init__(self, db: Database):
        self.db = db

    # ---- 写 / 读 ---------------------------------------------------------

    def save(
        self,
        *,
        plan_id: str,
        policy: str,
        routing_version: int,
        capacity_version: int,
        lead_split: str,
        pre_horizon: str,
        workcenters: list[str],
        load: np.ndarray,
        overdue: np.ndarray,
        recalc_of: int | None = None,
        replace_policy: bool = False,
    ) -> int:
        n_wc = len(workcenters)
        horizon = load.shape[1]
        if load.shape != (n_wc, horizon):
            raise ValueError("负荷矩阵形状与工作中心数不符")
        with self.db.write() as conn:
            if replace_policy:
                conn.execute(
                    "DELETE FROM load_result WHERE plan_id=? AND policy=?",
                    (plan_id, policy),
                )
            cur = conn.execute(
                "INSERT INTO load_result(plan_id, policy, routing_version, "
                "capacity_version, lead_split, pre_horizon, n_workcenters, "
                "horizon, matrix_blob, overdue_blob, wc_codes_json, recalc_of_result) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    plan_id,
                    policy,
                    routing_version,
                    capacity_version,
                    lead_split,
                    pre_horizon,
                    n_wc,
                    horizon,
                    self.db.matrix_to_blob(load),
                    self.db.vector_to_blob(overdue),
                    self.db.dumps(workcenters),
                    recalc_of,
                ),
            )
            return int(cur.lastrowid)

    def _row_to_result(self, row) -> dict[str, Any]:
        wcs = json.loads(row["wc_codes_json"])
        load = self.db.blob_to_matrix(
            row["matrix_blob"], row["n_workcenters"], row["horizon"]
        )
        overdue = self.db.blob_to_vector(row["overdue_blob"], row["n_workcenters"])
        return {
            "id": row["id"],
            "plan_id": row["plan_id"],
            "policy": row["policy"],
            "routing_version": row["routing_version"],
            "capacity_version": row["capacity_version"],
            "lead_split": row["lead_split"],
            "pre_horizon": row["pre_horizon"],
            "workcenters": wcs,
            "load": load,
            "overdue": overdue,
            "recalc_of_result": row["recalc_of_result"],
            "created_at": row["created_at"],
        }

    def get(self, result_id: int) -> dict[str, Any]:
        row = self.db.reader.execute(
            "SELECT * FROM load_result WHERE id=?", (result_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"负荷结果 {result_id} 不存在")
        return self._row_to_result(row)

    def latest_for_plan(self, plan_id: str, policy: str) -> dict[str, Any]:
        if policy == "recalc":
            row = self.db.reader.execute(
                "SELECT * FROM load_result WHERE plan_id=? AND policy='recalc' "
                "ORDER BY id DESC LIMIT 1",
                (plan_id,),
            ).fetchone()
        else:
            row = self.db.reader.execute(
                "SELECT * FROM load_result WHERE plan_id=? AND policy=? "
                "ORDER BY id DESC LIMIT 1",
                (plan_id, policy),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"计划 {plan_id} 没有 {policy} 负荷结果")
        return self._row_to_result(row)

    def bound_result(self, plan_id: str) -> dict[str, Any]:
        """计划版本的“当前”结果：已发布计划取 published 快照，草稿取 draft。"""
        row = self.db.reader.execute(
            "SELECT status FROM plan_version WHERE id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"计划 {plan_id} 不存在")
        policy = "published" if row["status"] == "published" else "draft"
        return self.latest_for_plan(plan_id, policy)

    def list_for_plan(self, plan_id: str) -> list[dict[str, Any]]:
        rows = self.db.reader.execute(
            "SELECT id, policy, routing_version, capacity_version, lead_split, "
            "pre_horizon, created_at, recalc_of_result FROM load_result "
            "WHERE plan_id=? ORDER BY id",
            (plan_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- 查询 -------------------------------------------------------------

    def slice(
        self,
        result: dict[str, Any],
        capacity: np.ndarray,
        *,
        workcenter: str | None = None,
        week_from: int | None = None,
        week_to: int | None = None,
    ) -> dict[str, Any]:
        load = result["load"]
        wcs = result["workcenters"]
        H = load.shape[1]
        wf = max(1, week_from or 1)
        wt = min(H, week_to or H)
        if wf > wt:
            raise ValidationError(f"周切片非法：{wf}..{wt}")
        if workcenter is not None:
            if workcenter not in wcs:
                raise NotFoundError(f"工作中心 {workcenter} 不在结果中")
            wc_list = [workcenter]
        else:
            wc_list = list(wcs)
        rows = []
        i0, i1 = wf - 1, wt  # python 切片
        for wc in wc_list:
            wi = wcs.index(wc)
            l = load[wi, i0:i1]
            c = capacity[wi, i0:i1]
            with np.errstate(divide="ignore", invalid="ignore"):
                util = [
                    (float(li / ci) if ci > 0 else None)
                    for li, ci in zip(l, c, strict=True)
                ]
            rows.append(
                {
                    "workcenter": wc,
                    "week_from": wf,
                    "load": [float(x) for x in l],
                    "capacity": [float(x) for x in c],
                    "utilization": util,
                }
            )
        overdue = {wc: float(result["overdue"][wcs.index(wc)]) for wc in wc_list}
        return {
            "plan_id": result["plan_id"],
            "routing_version": result["routing_version"],
            "capacity_version": result["capacity_version"],
            "lead_split": result["lead_split"],
            "pre_horizon": result["pre_horizon"],
            "horizon": H,
            "workcenters": wc_list,
            "rows": rows,
            "overdue": overdue,
        }

    def overloads(
        self, result: dict[str, Any], capacity: np.ndarray
    ) -> list[dict[str, Any]]:
        items = overload_cells(result["load"], capacity, result["workcenters"])
        for item in items:
            item["utilization"] = float(item["load"] / item["capacity"])
        return items

    # ---- 对比 -------------------------------------------------------------

    @staticmethod
    def _align(
        left: dict[str, Any], right: dict[str, Any]
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray | None, np.ndarray | None, list[str]]:
        """按工作中心 code 并集对齐两份结果（主数据可能在两版之间扩维）。

        旧结果中不存在的工作中心行按全 0 负荷/能力处理——它在旧版本口径下
        本就没有被任何清单行使用。
        """
        union = sorted(set(left["workcenters"]) | set(right["workcenters"]))

        def expand(result, key):
            mat = result[key]
            out = np.zeros((len(union), mat.shape[1]))
            idx = [result["workcenters"].index(c) for c in union
                   if c in result["workcenters"]]
            rows = [i for i, c in enumerate(union) if c in result["workcenters"]]
            out[rows] = mat[idx]
            return out

        l_load = expand(left, "load")
        r_load = expand(right, "load")
        l_od = np.array(
            [left["overdue"][left["workcenters"].index(c)]
             if c in left["workcenters"] else 0.0 for c in union]
        )
        r_od = np.array(
            [right["overdue"][right["workcenters"].index(c)]
             if c in right["workcenters"] else 0.0 for c in union]
        )
        return l_load, r_load, l_od, r_od, union

    def compare(
        self,
        left: dict[str, Any],
        right: dict[str, Any],
        right_capacity: np.ndarray,
        *,
        left_capacity: np.ndarray | None = None,
        top_n: int = 20,
    ) -> dict[str, Any]:
        old_load, new_load, old_od, new_od, wcs = self._align(left, right)
        if old_load.shape[1] != new_load.shape[1]:
            raise ValidationError("两份结果计划期长度不同，无法逐格对比")

        def align_cap(cap: np.ndarray | None, result: dict[str, Any]) -> np.ndarray | None:
            if cap is None:
                return None
            out = np.zeros((len(wcs), cap.shape[1]))
            for i, c in enumerate(wcs):
                if c in result["workcenters"]:
                    out[i] = cap[result["workcenters"].index(c)]
            return out

        old_cap = align_cap(left_capacity, left)
        new_cap = align_cap(right_capacity, right)
        stats = compare_loads(
            old_load,
            new_load,
            wcs,
            old_capacity=old_cap,
            new_capacity=new_cap,
            top_n=top_n,
        )
        overdue_delta = {}
        for i, wc in enumerate(wcs):
            d = float(new_od[i] - old_od[i])
            if d != 0.0:
                overdue_delta[wc] = d
        added_workcenters = sorted(
            set(right["workcenters"]) - set(left["workcenters"])
        )
        return {
            "left": f"result:{left['id']}",
            "right": f"result:{right['id']}",
            "sum_abs_delta": stats["sum_abs_delta"],
            "max_abs_delta": stats["max_abs_delta"],
            "top_deltas": stats["top_deltas"],
            "flips": stats["flips"],
            "overdue_delta": overdue_delta,
            "added_workcenters": added_workcenters,
            "binds": {
                "left": {
                    "routing_version": left["routing_version"],
                    "capacity_version": left["capacity_version"],
                },
                "right": {
                    "routing_version": right["routing_version"],
                    "capacity_version": right["capacity_version"],
                },
            },
        }

    @staticmethod
    def _check_comparable(left: dict[str, Any], right: dict[str, Any]) -> None:
        if left["load"].shape[1] != right["load"].shape[1]:
            raise ValidationError("两份结果计划期长度不同，无法逐格对比")
