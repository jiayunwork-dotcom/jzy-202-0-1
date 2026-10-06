"""主生产计划草稿、增量负荷维护、发布、重算与重启恢复。

增量维护
--------
每个打开中的草稿持有一份运行时状态 ``DraftState``：

* ``P``：产品×周数量矩阵（内存真值，与 plan_cell 表在同一把草稿锁内同步更新）；
* ``L`` / ``overdue``：工作中心×周负荷矩阵与逾期向量；
* 展开引擎（绑定清单版本）与能力矩阵（绑定日历版本）。

改一个格子 (product, week, new_q) 时：

1. 用引擎只展开该产品一条数量向量（成本只与该产品的清单行数相关），
   得到新贡献与旧贡献；
2. ``L += 贡献(new) - 贡献(old)``，``P`` 同步改一格；
3. 在同一个草稿锁 + 一个 SQLite 事务里更新 plan_cell 与 draft 结果行。

清单/日历本身的改动作用在“草稿”上时通过显式重绑定处理；若新增了产品或
工作中心（主数据扩维），所有草稿状态以 registry epoch 为标记惰性整体重建。
已发布计划永不自动改变，只能通过“按新清单重算”另产一份 recalc 结果。

并发
----
进程内每个草稿一把 ``threading.Lock``（FastAPI 同步端点在线程池中执行）；
同格后到覆盖必须携带 ``expected_quantity``，与锁内读到的当前值不一致即
409 拒绝并返回当前值。多进程部署时需要把这把 CAS 下沉到 SQL
（``UPDATE ... WHERE quantity=?``，按 rowcount 判定），见 README。
"""

from __future__ import annotations

import itertools
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .calendar import CapacityService
from .database import Database
from .errors import ConflictError, ImmutableError, NotFoundError, ValidationError
from .explosion import (
    LEAD_SPLITS,
    PRE_HORIZON_POLICIES,
    ExplosionEngine,
    RoutingLine,
)
from .models import CellEdit
from .registry import Registry
from .results import ResultService
from .routing import RoutingService

log = logging.getLogger("rccp.planner")


@dataclass
class DraftState:
    plan_id: str
    engine: ExplosionEngine
    P: np.ndarray
    L: np.ndarray
    overdue: np.ndarray
    capacity: np.ndarray
    routing_version: int
    capacity_version: int
    lock: threading.Lock = field(default_factory=threading.Lock)


class PlannerService:
    def __init__(
        self,
        db: Database,
        registry: Registry,
        routings: RoutingService,
        capacities: CapacityService,
        results: ResultService,
    ):
        self.db = db
        self.registry = registry
        self.routings = routings
        self.capacities = capacities
        self.results = results
        self._states: dict[str, DraftState] = {}
        self._states_guard = threading.Lock()
        self._registry_epoch = 0  # Registry 每次新增主数据后 bump
        self._id_seq = itertools.count(1)

    def bump_registry_epoch(self) -> None:
        with self._states_guard:
            self._registry_epoch += 1

    # ---- 引擎装配 --------------------------------------------------------

    def _build_engine(
        self, routing_version: int, lead_split: str, pre_horizon: str
    ) -> ExplosionEngine:
        lines = [
            RoutingLine(
                product=ln.product,
                workcenter=ln.workcenter,
                hours_per_unit=ln.hours_per_unit,
                lead_weeks=ln.lead_weeks,
            )
            for ln in self.routings.lines_of(routing_version)
        ]
        return ExplosionEngine(
            product_codes=self.registry.all_product_codes(),
            workcenter_codes=self.registry.all_workcenter_codes(),
            horizon=self.db.get_horizon(),
            lines=lines,
            lead_split=lead_split,
            pre_horizon=pre_horizon,
        )

    def _load_plan_matrix(self, plan_id: str, engine: ExplosionEngine) -> np.ndarray:
        P = np.zeros((len(engine.products), engine.horizon))
        rows = self.db.reader.execute(
            "SELECT product, week, quantity FROM plan_cell WHERE plan_id=?",
            (plan_id,),
        ).fetchall()
        for r in rows:
            P[engine.p_index[r["product"]], int(r["week"]) - 1] = float(
                r["quantity"]
            )
        return P

    def _capacity_matrix(
        self, capacity_version: int, engine: ExplosionEngine
    ) -> np.ndarray:
        return engine.build_capacity(self.capacities.sparse_of(capacity_version))

    # ---- 草稿状态缓存 ----------------------------------------------------

    def _get_meta(self, plan_id: str):
        row = self.db.reader.execute(
            "SELECT * FROM plan_version WHERE id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"计划 {plan_id} 不存在")
        return row

    def state(self, plan_id: str) -> DraftState:
        meta = self._get_meta(plan_id)
        if meta["status"] != "draft":
            raise ImmutableError(f"计划 {plan_id} 是已发布只读版本")
        with self._states_guard:
            st = self._states.get(plan_id)
            epoch = self._registry_epoch
        if st is not None and getattr(st, "_epoch", -1) == epoch:
            return st

        # 首次打开或主数据扩维后重建
        engine = self._build_engine(
            meta["routing_version"], meta["lead_split"], meta["pre_horizon"]
        )
        P = self._load_plan_matrix(plan_id, engine)
        L, overdue = engine.explode_all(P)
        cap = self._capacity_matrix(meta["capacity_version"], engine)
        st = DraftState(
            plan_id=plan_id,
            engine=engine,
            P=P,
            L=L,
            overdue=overdue,
            capacity=cap,
            routing_version=meta["routing_version"],
            capacity_version=meta["capacity_version"],
        )
        st._epoch = epoch  # type: ignore[attr-defined]
        with self._states_guard:
            self._states[plan_id] = st
        return st

    # ---- 草稿生命周期 ----------------------------------------------------

    def create_draft(self, payload) -> dict:
        routing_version = (
            payload.routing_version
            if payload.routing_version is not None
            else self.routings.latest_published()
        )
        capacity_version = (
            payload.capacity_version
            if payload.capacity_version is not None
            else self.capacities.latest_published()
        )
        self._check_published_refs(routing_version, capacity_version)
        if payload.lead_split not in LEAD_SPLITS:
            raise ValidationError(f"未知提前量拆分规则 {payload.lead_split!r}")
        if payload.pre_horizon not in PRE_HORIZON_POLICIES:
            raise ValidationError(f"未知期初处理规则 {payload.pre_horizon!r}")

        draft_id = payload.id or f"draft-{next(self._id_seq)}"
        cells = payload.cells or []
        self._validate_cells(cells)
        try:
            with self.db.write() as conn:
                conn.execute(
                    "INSERT INTO plan_version(id, status, name, routing_version, "
                    "capacity_version, lead_split, pre_horizon) "
                    "VALUES (?, 'draft', ?, ?, ?, ?, ?)",
                    (
                        draft_id,
                        payload.name,
                        routing_version,
                        capacity_version,
                        payload.lead_split,
                        payload.pre_horizon,
                    ),
                )
                conn.executemany(
                    "INSERT INTO plan_cell(plan_id, product, week, quantity) "
                    "VALUES (?, ?, ?, ?)",
                    [(draft_id, c.product, c.week, self._as_int(c.quantity)) for c in cells],
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"草稿 id {draft_id!r} 已存在") from exc
        st = self.state(draft_id)
        self._persist_draft_result(st)
        return self.get_draft(draft_id)

    def _check_published_refs(self, routing_v: int, capacity_v: int) -> None:
        r = self.routings._get_row(routing_v)
        if r["status"] != "published":
            raise ValidationError(f"资源清单版本 {routing_v} 尚未发布，不能绑定计划")
        c = self.capacities._get_row(capacity_v)
        if c["status"] != "published":
            raise ValidationError(f"能力日历版本 {capacity_v} 尚未发布，不能绑定计划")

    def list_plans(self, status: str | None = None) -> list[dict]:
        sql = (
            "SELECT p.*, (SELECT COUNT(*) FROM plan_cell c WHERE c.plan_id=p.id) "
            "AS cell_count FROM plan_version p"
        )
        args: tuple = ()
        if status:
            sql += " WHERE p.status=?"
            args = (status,)
        sql += " ORDER BY p.created_at, p.id"
        return [dict(r) for r in self.db.reader.execute(sql, args).fetchall()]

    def get_draft(self, plan_id: str) -> dict:
        meta = self._get_meta(plan_id)
        st = None
        if meta["status"] == "draft":
            st = self.state(plan_id)
        covered = self.routings.covered_products(meta["routing_version"])
        uncovered = sorted(
            {
                r["product"]
                for r in self.db.reader.execute(
                    "SELECT DISTINCT product FROM plan_cell WHERE plan_id=?",
                    (plan_id,),
                ).fetchall()
            }
            - covered
        )
        cell_count = self.db.reader.execute(
            "SELECT COUNT(*) AS n FROM plan_cell WHERE plan_id=?", (plan_id,)
        ).fetchone()["n"]
        return {
            "id": meta["id"],
            "name": meta["name"],
            "status": meta["status"],
            "routing_version": meta["routing_version"],
            "capacity_version": meta["capacity_version"],
            "lead_split": meta["lead_split"],
            "pre_horizon": meta["pre_horizon"],
            "cell_count": cell_count,
            "uncovered_products": uncovered,
            "total_load_hours": float(st.L.sum() + st.overdue.sum()) if st else None,
            "version": meta["version"],
            "published_from": meta["published_from"],
        }

    def list_cells(self, plan_id: str) -> list[dict]:
        self._get_meta(plan_id)
        rows = self.db.reader.execute(
            "SELECT product, week, quantity FROM plan_cell "
            "WHERE plan_id=? ORDER BY week, product",
            (plan_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- 校验 -------------------------------------------------------------

    def _validate_cells(self, cells) -> None:
        horizon = self.db.get_horizon()
        seen: set[tuple[str, int]] = set()
        for c in cells:
            if c.week > horizon:
                raise ValidationError(
                    f"周号 {c.week} 超出计划期（1..{horizon}）",
                    details={"week": c.week, "horizon": horizon},
                )
            self._check_integer_quantity(c.quantity)
            key = (c.product, c.week)
            if key in seen:
                raise ValidationError(
                    f"导入数据中 {c.product} 第 {c.week} 周重复"
                )
            seen.add(key)
        self.registry.require_products({c.product for c in cells})

    @staticmethod
    def _check_integer_quantity(q: float) -> None:
        fq = float(q)
        if fq < 0 or not np.isfinite(fq) or not float(fq).is_integer():
            raise ValidationError(
                f"数量必须是非负整数，收到 {q!r}",
                details={"quantity": q},
            )

    @staticmethod
    def _as_int(q: float) -> int:
        return int(float(q))

    # ---- 逐格编辑（CAS + 增量）-------------------------------------------

    def edit_cell(self, plan_id: str, edit: CellEdit) -> dict:
        self._check_integer_quantity(edit.quantity)
        horizon = self.db.get_horizon()
        if edit.week > horizon:
            raise ValidationError(
                f"周号 {edit.week} 超出计划期（1..{horizon}）",
                details={"week": edit.week, "horizon": horizon},
            )
        self.registry.require_products([edit.product])
        st = self.state(plan_id)
        pi = st.engine.p_index[edit.product]
        ti = edit.week - 1
        with st.lock:
            current = float(st.P[pi, ti])
            if (
                edit.expected_quantity is not None
                and float(edit.expected_quantity) != current
            ):
                raise ConflictError(
                    f"格子已被他人修改：期望旧值 {edit.expected_quantity}，"
                    f"当前值 {current:g}",
                    details={
                        "product": edit.product,
                        "week": edit.week,
                        "current_quantity": current,
                    },
                )
            return self._apply_cell(
                st, edit.product, pi, ti, float(edit.quantity), current
            )

    def _apply_cell(
        self, st: DraftState, product: str, pi: int, ti: int, new_q: float, old_q: float
    ) -> dict:
        if new_q == old_q:
            return {
                "product": product,
                "week": ti + 1,
                "quantity": new_q,
                "changed": False,
            }
        old_vec = st.P[pi].copy()
        new_vec = old_vec.copy()
        new_vec[ti] = new_q
        dL, dOverdue = self._product_delta(st, product, old_vec, new_vec)
        with self.db.write() as conn:
            if new_q == 0.0:
                conn.execute(
                    "DELETE FROM plan_cell WHERE plan_id=? AND product=? AND week=?",
                    (st.plan_id, product, ti + 1),
                )
            else:
                conn.execute(
                    "INSERT INTO plan_cell(plan_id, product, week, quantity) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(plan_id, product, week) DO UPDATE SET quantity=excluded.quantity",
                    (st.plan_id, product, ti + 1, self._as_int(new_q)),
                )
            st.P[pi, ti] = new_q
            st.L += dL
            st.overdue += dOverdue
            self._write_draft_result(conn, st)
        return {
            "product": product,
            "week": ti + 1,
            "quantity": new_q,
            "changed": True,
            "old_quantity": old_q,
        }

    def _product_delta(
        self, st: DraftState, product: str, old_vec: np.ndarray, new_vec: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        """单产品数量向量变化 -> (ΔL, Δoverdue)。"""
        if not np.any(new_vec) and not np.any(old_vec):
            z = np.zeros_like(st.L)
            return z, np.zeros(len(st.engine.workcenters))
        new_contrib = st.engine.explode_product(product, new_vec)
        old_contrib = st.engine.explode_product(product, old_vec)
        return new_contrib[0] - old_contrib[0], new_contrib[1] - old_contrib[1]

    # ---- 批量导入 ---------------------------------------------------------

    def bulk_import(self, plan_id: str, payload) -> dict:
        st = self.state(plan_id)
        # merge / replace 都要求请求内同一格不重复
        self._validate_cells(payload.cells)
        touched = sorted({c.product for c in payload.cells})
        with st.lock:
            old_vectors = {p: st.P[st.engine.p_index[p]].copy() for p in touched}
            with self.db.write() as conn:
                if payload.mode == "replace":
                    conn.execute(
                        "DELETE FROM plan_cell WHERE plan_id=?", (plan_id,)
                    )
                    st.P.fill(0.0)
                for c in payload.cells:
                    pi = st.engine.p_index[c.product]
                    st.P[pi, c.week - 1] = float(c.quantity)
                    conn.execute(
                        "INSERT INTO plan_cell(plan_id, product, week, quantity) "
                        "VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(plan_id, product, week) DO UPDATE SET quantity=excluded.quantity",
                        (plan_id, c.product, c.week, self._as_int(c.quantity)),
                    )
                # 增量：只重算受影响产品
                for product in touched:
                    new_vec = st.P[st.engine.p_index[product]]
                    dL, dO = self._product_delta(
                        st, product, old_vectors[product], new_vec
                    )
                    st.L += dL
                    st.overdue += dO
                self._write_draft_result(conn, st)
        return self.get_draft(plan_id)

    # ---- 结果落盘 ---------------------------------------------------------

    def _write_draft_result(self, conn, st: DraftState) -> None:
        conn.execute(
            "DELETE FROM load_result WHERE plan_id=? AND policy='draft'",
            (st.plan_id,),
        )
        conn.execute(
            "INSERT INTO load_result(plan_id, policy, routing_version, "
            "capacity_version, lead_split, pre_horizon, n_workcenters, horizon, "
            "matrix_blob, overdue_blob, wc_codes_json) "
            "VALUES (?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                st.plan_id,
                st.routing_version,
                st.capacity_version,
                st.engine.lead_split,
                st.engine.pre_horizon,
                len(st.engine.workcenters),
                st.engine.horizon,
                self.db.matrix_to_blob(st.L),
                self.db.vector_to_blob(st.overdue),
                self.db.dumps(st.engine.workcenters),
            ),
        )

    def _persist_draft_result(self, st: DraftState) -> None:
        with self.db.write() as conn:
            self._write_draft_result(conn, st)

    # ---- 发布 -------------------------------------------------------------

    def publish(self, draft_id: str) -> dict:
        st = self.state(draft_id)
        with st.lock:
            with self.db.write() as conn:
                nrow = conn.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 AS nv FROM plan_version "
                    "WHERE status='published'"
                ).fetchone()
                nv = int(nrow["nv"])
                plan_id = f"plan-{nv}"
                conn.execute(
                    "INSERT INTO plan_version(id, status, name, routing_version, "
                    "capacity_version, lead_split, pre_horizon, parent_draft_id, "
                    "published_from, version, published_at) "
                    "VALUES (?, 'published', ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
                    (
                        plan_id,
                        self._get_meta(draft_id)["name"],
                        st.routing_version,
                        st.capacity_version,
                        st.engine.lead_split,
                        st.engine.pre_horizon,
                        draft_id,
                        draft_id,
                        nv,
                    ),
                )
                conn.execute(
                    "INSERT INTO plan_cell(plan_id, product, week, quantity) "
                    "SELECT ?, product, week, quantity FROM plan_cell WHERE plan_id=?",
                    (plan_id, draft_id),
                )
                # 冻结负荷结果：与草稿内存矩阵逐格相同，但绑定不可变版本
                conn.execute(
                    "INSERT INTO load_result(plan_id, policy, routing_version, "
                    "capacity_version, lead_split, pre_horizon, n_workcenters, "
                    "horizon, matrix_blob, overdue_blob, wc_codes_json) "
                    "VALUES (?, 'published', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        plan_id,
                        st.routing_version,
                        st.capacity_version,
                        st.engine.lead_split,
                        st.engine.pre_horizon,
                        len(st.engine.workcenters),
                        st.engine.horizon,
                        self.db.matrix_to_blob(st.L),
                        self.db.vector_to_blob(st.overdue),
                        self.db.dumps(st.engine.workcenters),
                    ),
                )
        published = self.get_draft(plan_id)
        result_id = self.results.latest_for_plan(plan_id, "published")["id"]
        return {
            "draft_id": draft_id,
            "plan_id": plan_id,
            "version": nv,
            "routing_version": st.routing_version,
            "capacity_version": st.capacity_version,
            "result_id": result_id,
            "uncovered_products": published["uncovered_products"],
        }

    # ---- 按新清单/日历重算（已发布计划）------------------------------------

    def recalc(
        self,
        plan_id: str,
        routing_version: int | None = None,
        capacity_version: int | None = None,
    ) -> dict:
        meta = self._get_meta(plan_id)
        if meta["status"] != "published":
            raise ValidationError("只能对已发布计划执行按新清单重算")
        rv = routing_version or meta["routing_version"]
        cv = capacity_version or meta["capacity_version"]
        self._check_published_refs(rv, cv)

        old_result = self.results.latest_for_plan(plan_id, "published")
        engine = self._build_engine(rv, meta["lead_split"], meta["pre_horizon"])
        P = self._load_plan_matrix(plan_id, engine)
        L, overdue = engine.explode_all(P)
        cap = self._capacity_matrix(cv, engine)
        covered = self.routings.covered_products(rv)
        uncovered = sorted(
            {
                r["product"]
                for r in self.db.reader.execute(
                    "SELECT DISTINCT product FROM plan_cell WHERE plan_id=?",
                    (plan_id,),
                ).fetchall()
            }
            - covered
        )
        new_id = self.results.save(
            plan_id=plan_id,
            policy="recalc",
            routing_version=rv,
            capacity_version=cv,
            lead_split=meta["lead_split"],
            pre_horizon=meta["pre_horizon"],
            workcenters=engine.workcenters,
            load=L,
            overdue=overdue,
            recalc_of=old_result["id"],
        )
        new_result = self.results.get(new_id)
        # 翻格用各自当时的日历口径（识别“因能力变化而解除/新增”的格子）
        old_engine = self._build_engine(
            old_result["routing_version"],
            old_result["lead_split"],
            old_result["pre_horizon"],
        )
        old_cap = self._capacity_matrix(old_result["capacity_version"], old_engine)
        comparison = self.results.compare(
            old_result, new_result, cap, left_capacity=old_cap
        )
        comparison["left"] = f"plan:{plan_id}@routing{old_result['routing_version']}"
        comparison["right"] = f"plan:{plan_id}@routing{rv}"
        return {
            "plan_id": plan_id,
            "old_result_id": old_result["id"],
            "new_result_id": new_id,
            "routing_version": rv,
            "capacity_version": cv,
            "uncovered_products": uncovered,
            "compare": comparison,
        }

    # ---- 版本对比 ---------------------------------------------------------

    def compare_plans(
        self, left_id: str, right_id: str, *, top_n: int = 20
    ) -> dict[str, Any]:
        left = self.results.bound_result(left_id)
        right = self.results.bound_result(right_id)
        # 翻格判定以右侧结果绑定的能力日历为准（通常对比的是新版计划）
        meta = self._get_meta(right_id)
        engine = self._build_engine(
            right["routing_version"], right["lead_split"], right["pre_horizon"]
        )
        cap = self._capacity_matrix(right["capacity_version"], engine)
        out = self.results.compare(left, right, cap, left_capacity=cap, top_n=top_n)
        out["left"] = f"plan:{left_id}"
        out["right"] = f"plan:{right_id}"
        out["left_meta"] = {"id": left_id, **{
            k: self._get_meta(left_id)[k]
            for k in ("status", "version", "routing_version", "capacity_version")
        }}
        out["right_meta"] = {"id": right_id, **{
            k: self._get_meta(right_id)[k]
            for k in ("status", "version", "routing_version", "capacity_version")
        }}
        return out

    # ---- 重启恢复 ---------------------------------------------------------

    def bootstrap(self) -> dict:
        """服务启动时调用：装载所有草稿，校验/修复持久化负荷矩阵。

        plan_cell 是数量真值；按它重新展开一次，与 load_result 中的 draft 行
        逐格比对：一致则直接热机；不一致（理论上只有进程在事务边界外被
        强杀才可能发生）以数量表为准覆盖结果，保证“草稿内容与负荷状态一致”。
        """
        rows = self.db.reader.execute(
            "SELECT id FROM plan_version WHERE status='draft'"
        ).fetchall()
        report = {"drafts": len(rows), "repaired": [], "loaded": []}
        for r in rows:
            plan_id = r["id"]
            st = self.state(plan_id)
            stored = None
            try:
                stored = self.results.latest_for_plan(plan_id, "draft")
            except NotFoundError:
                pass
            repair = stored is None
            if stored is not None:
                if (
                    stored["load"].shape != st.L.shape
                    or not np.allclose(stored["load"], st.L, atol=1e-9, rtol=1e-9)
                    or not np.allclose(stored["overdue"], st.overdue, atol=1e-9)
                ):
                    repair = True
            if repair:
                log.warning("草稿 %s 的持久化负荷与数量表不一致，按数量表修复", plan_id)
                self._persist_draft_result(st)
                report["repaired"].append(plan_id)
            report["loaded"].append(plan_id)
        return report
