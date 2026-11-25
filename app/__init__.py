"""Flask 应用：滚动发布延迟达标复盘页。

页面路由返回 HTML；/api 下为 JSON 接口，所有写操作串行化在 SQLite
IMMEDIATE 事务中，保证补录与更正交错提交时覆盖数与结论同源。
"""

from __future__ import annotations

import os
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from .db import (
    CoverageExceededError,
    ConfigError,
    build_snapshot,
    connect,
    delete_instance,
    init_db,
    submit_instance,
    upsert_config,
)
from .evaluation import HistogramError


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path or os.environ.get(
        "LATENCY_DB", str(Path(__file__).resolve().parent.parent / "data" / "latency.db")
    )
    init_db(app.config["DB_PATH"])

    def db():
        return connect(app.config["DB_PATH"])

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/snapshot")
    def api_snapshot():
        with db() as conn:
            return jsonify(build_snapshot(conn))

    @app.post("/api/config")
    def api_config():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "请求体必须是 JSON 对象"}), 400
        try:
            with db() as conn:
                snapshot = upsert_config(
                    conn,
                    body.get("gateway_total"),
                    body.get("latency_limit"),
                    body.get("target_ratio"),
                )
        except ConfigError as exc:
            return jsonify({"error": str(exc)}), 400
        except CoverageExceededError as exc:
            return jsonify({"error": str(exc), "code": "coverage_exceeded"}), 409
        return jsonify(snapshot)

    @app.post("/api/instances")
    def api_submit_instance():
        body = request.get_json(silent=True)
        try:
            with db() as conn:
                snapshot = submit_instance(conn, body)
        except HistogramError as exc:
            return jsonify({"error": str(exc)}), 400
        except ConfigError as exc:
            return jsonify({"error": str(exc)}), 400
        except CoverageExceededError as exc:
            return jsonify(
                {
                    "error": str(exc),
                    "code": "coverage_exceeded",
                    "coverage_before": exc.coverage_before,
                    "attempted": exc.attempted,
                    "gateway_total": exc.gateway_total,
                }
            ), 409
        return jsonify(snapshot)

    @app.delete("/api/instances/<instance_id>")
    def api_delete_instance(instance_id: str):
        try:
            with db() as conn:
                snapshot = delete_instance(conn, instance_id)
        except LookupError as exc:
            return jsonify({"error": str(exc)}), 404
        return jsonify(snapshot)

    @app.errorhandler(500)
    def handle_500(err):
        return jsonify({"error": f"服务器内部错误: {err}"}), 500

    return app


def main() -> None:  # pragma: no cover - 本地裸跑入口
    app = create_app()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":  # pragma: no cover
    main()
