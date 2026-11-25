"""SQLite 持久化层。

并发策略：所有写操作在 *同一个* ``IMMEDIATE`` 事务里完成“读旧状态 →
覆盖数校验 → 写入 → 重算结论”，事务提交前拿到的覆盖数与结论必然来自同一
已提交状态；会使覆盖数超过网关总数的提交在事务内被拒绝并整体回滚，旧结论
原样保留。WAL 模式 + busy_timeout 让并发补录/更正自动排队而不是互相报锁。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .evaluation import (
    Histogram,
    HistogramError,
    PairEvaluation,
    evaluate_overall,
    evaluate_pair,
    parse_histogram,
    validate_submission,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    gateway_total INTEGER NOT NULL CHECK (gateway_total > 0),
    latency_limit REAL NOT NULL CHECK (latency_limit >= 0),
    target_ratio REAL NOT NULL CHECK (target_ratio > 0 AND target_ratio <= 1),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS instances (
    instance_id TEXT PRIMARY KEY,
    start_marker TEXT NOT NULL,
    end_marker TEXT NOT NULL,
    start_bounds TEXT NOT NULL,
    start_counts TEXT NOT NULL,
    end_bounds TEXT NOT NULL,
    end_counts TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class ConfigError(ValueError):
    """网关配置缺失或不合法。"""


class CoverageExceededError(Exception):
    """提交会使覆盖数超过网关完结请求总数。"""

    def __init__(self, coverage_before: int, attempted: int, gateway_total: int):
        self.coverage_before = coverage_before
        self.attempted = attempted
        self.gateway_total = gateway_total
        super().__init__(
            f"提交后覆盖数将达到 {attempted}，超过网关完结请求总数 "
            f"{gateway_total}（当前覆盖 {coverage_before}），已退回"
        )


def init_db(db_path: str | Path) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)


@contextmanager
def connect(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def immediate_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """打开立即事务：执行 BEGIN IMMEDIATE 后即持有写锁直至提交/回滚。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def get_config(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT gateway_total, latency_limit, target_ratio FROM config WHERE id = 1"
    ).fetchone()
    if row is None:
        return None
    return {
        "gateway_total": row["gateway_total"],
        "latency_limit": row["latency_limit"],
        "target_ratio": row["target_ratio"],
    }


def _hist_to_json(hist: Histogram) -> tuple[str, str]:
    return json.dumps(list(hist.bounds)), json.dumps(list(hist.counts))


def _row_to_pair(row: sqlite3.Row, latency_limit: float) -> PairEvaluation:
    start = Histogram(
        tuple(json.loads(row["start_bounds"])),
        tuple(json.loads(row["start_counts"])),
    )
    end = Histogram(
        tuple(json.loads(row["end_bounds"])),
        tuple(json.loads(row["end_counts"])),
    )
    return evaluate_pair(
        row["instance_id"],
        row["start_marker"],
        row["end_marker"],
        start,
        end,
        latency_limit,
    )


def _all_pairs(conn: sqlite3.Connection, latency_limit: float) -> list[PairEvaluation]:
    rows = conn.execute(
        "SELECT * FROM instances ORDER BY instance_id"
    ).fetchall()
    return [_row_to_pair(row, latency_limit) for row in rows]


def build_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    """基于当前已提交状态生成完整复盘快照（覆盖数与结论同源）。"""
    config = get_config(conn)
    if config is None:
        pairs = []
        evaluation = evaluate_overall([], None, None)
    else:
        pairs = _all_pairs(conn, config["latency_limit"])
        evaluation = evaluate_overall(
            pairs, config["gateway_total"], config["target_ratio"]
        )
    return {
        "config": config,
        "instances": [_serialize_pair(p) for p in evaluation.pairs],
        "summary": {
            "coverage": evaluation.coverage,
            "good_lower": evaluation.good_lower,
            "good_upper": evaluation.good_upper,
            "bad_lower": evaluation.bad_lower,
            "unknown": evaluation.unknown,
            "decision": evaluation.decision,
            "decision_reason": evaluation.decision_reason,
        },
    }


def _serialize_pair(pair: PairEvaluation) -> dict[str, Any]:
    return {
        "instance_id": pair.instance_id,
        "valid": pair.valid,
        "reason": pair.reason,
        "start_marker": pair.start_marker,
        "end_marker": pair.end_marker,
        "total": pair.total,
        "good_lower": pair.good_lower,
        "bad_lower": pair.bad_lower,
        "unknown": pair.unknown,
        "buckets": [
            {
                "index": b.index,
                "upper_bound": b.upper_bound,
                "count": b.count,
                "classification": b.classification,
            }
            for b in pair.buckets
        ],
    }


def upsert_config(
    conn: sqlite3.Connection,
    gateway_total: Any,
    latency_limit: Any,
    target_ratio: Any,
) -> dict[str, Any]:
    """写入/更新网关参数。

    新的网关总数不得低于当前已提交覆盖数，否则同样退回并保留原结论。
    """
    if isinstance(gateway_total, bool) or not isinstance(gateway_total, int):
        raise ConfigError("网关完结请求总数必须是大于 0 的整数")
    if gateway_total <= 0:
        raise ConfigError("网关完结请求总数必须大于 0")
    if isinstance(latency_limit, bool) or not isinstance(
        latency_limit, (int, float)
    ):
        raise ConfigError("延迟上限必须是非负数字")
    latency_limit = float(latency_limit)
    if latency_limit < 0:
        raise ConfigError("延迟上限必须是非负数字")
    if isinstance(target_ratio, bool) or not isinstance(target_ratio, (int, float)):
        raise ConfigError("目标达标率必须是 (0, 1] 之间的数字")
    target_ratio = float(target_ratio)
    if not (0 < target_ratio <= 1):
        raise ConfigError("目标达标率必须落在 (0, 1] 区间")

    with immediate_tx(conn):
        old = get_config(conn)
        coverage_before = 0
        if old is not None:
            coverage_before = build_snapshot(conn)["summary"]["coverage"]
        if gateway_total < coverage_before:
            raise CoverageExceededError(
                coverage_before, coverage_before, gateway_total
            )
        conn.execute(
            """
            INSERT INTO config (id, gateway_total, latency_limit, target_ratio)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                gateway_total = excluded.gateway_total,
                latency_limit = excluded.latency_limit,
                target_ratio = excluded.target_ratio,
                updated_at = datetime('now')
            """,
            (gateway_total, latency_limit, target_ratio),
        )
    return build_snapshot(conn)


def submit_instance(conn: sqlite3.Connection, payload: Any) -> dict[str, Any]:
    """补录或更正单个实例样本（同 instance_id 即替换旧样本）。

    整个过程在一个 IMMEDIATE 事务内：校验 → 计算候选覆盖数 → 与网关总数
    比较 → UPSERT → 重算快照。超限则抛 CoverageExceededError 并回滚。
    """
    if not isinstance(payload, dict):
        raise HistogramError("请求体必须是 JSON 对象")
    try:
        instance_id, start_marker, end_marker, start, end = validate_submission(
            payload.get("instance_id"),
            payload.get("start_marker"),
            payload.get("end_marker"),
            payload.get("start_histogram"),
            payload.get("end_histogram"),
        )
    except HistogramError:
        raise

    with immediate_tx(conn):
        config = get_config(conn)
        if config is None:
            raise ConfigError("请先配置网关完结请求总数、延迟上限与目标达标率")

        candidate = evaluate_pair(
            instance_id, start_marker, end_marker, start, end,
            config["latency_limit"],
        )
        snapshot_before = build_snapshot(conn)
        old_row = conn.execute(
            "SELECT * FROM instances WHERE instance_id = ?", (instance_id,)
        ).fetchone()
        old_contribution = 0
        if old_row is not None:
            old_pair = _row_to_pair(old_row, config["latency_limit"])
            if old_pair.valid:
                old_contribution = old_pair.total

        attempted = (
            snapshot_before["summary"]["coverage"]
            - old_contribution
            + (candidate.total if candidate.valid else 0)
        )
        if attempted > config["gateway_total"]:
            raise CoverageExceededError(
                snapshot_before["summary"]["coverage"],
                attempted,
                config["gateway_total"],
            )

        s_bounds, s_counts = _hist_to_json(start)
        e_bounds, e_counts = _hist_to_json(end)
        conn.execute(
            """
            INSERT INTO instances (
                instance_id, start_marker, end_marker,
                start_bounds, start_counts, end_bounds, end_counts
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(instance_id) DO UPDATE SET
                start_marker = excluded.start_marker,
                end_marker = excluded.end_marker,
                start_bounds = excluded.start_bounds,
                start_counts = excluded.start_counts,
                end_bounds = excluded.end_bounds,
                end_counts = excluded.end_counts,
                updated_at = datetime('now')
            """,
            (
                instance_id, start_marker, end_marker,
                s_bounds, s_counts, e_bounds, e_counts,
            ),
        )
        snapshot = build_snapshot(conn)

    return snapshot


def delete_instance(conn: sqlite3.Connection, instance_id: str) -> dict[str, Any]:
    with immediate_tx(conn):
        cur = conn.execute(
            "DELETE FROM instances WHERE instance_id = ?", (instance_id,)
        )
        if cur.rowcount == 0:
            raise LookupError(f"实例 {instance_id} 不存在")
    return build_snapshot(conn)
