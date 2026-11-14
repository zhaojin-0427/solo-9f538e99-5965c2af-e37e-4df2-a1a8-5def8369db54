"""Flask 网页应用：上传闸机补传 CSV，按卡号查看配对行程与异常。

路由：
- GET  /                 上传入口 + 批次历史 + 卡号汇总
- POST /upload           事务性合并一个 CSV 批次（失败整批回滚）
- GET  /cards/<card_no>  该卡的配对行程、异常记录与现行事件
- GET  /healthz          容器健康检查
"""
from __future__ import annotations

import os
import sqlite3

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   url_for)

from db import apply_batch, close_db, get_db, init_db, utc_now
from processing import BatchError, pair_events, parse_csv


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        DATABASE=db_path or os.environ.get("APP_DB", os.path.join(os.getcwd(), "app.db")),
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-change-me"),
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,  # 单个 CSV 上限 16MB
    )

    db_dir = os.path.dirname(os.path.abspath(app.config["DATABASE"]))
    os.makedirs(db_dir, exist_ok=True)

    app.teardown_appcontext(close_db)

    with app.app_context():
        init_db(get_db())

    # ---------------------------------------------------------------- 查询

    @app.get("/")
    def index():
        db = get_db()
        uploads = db.execute(
            "SELECT * FROM uploads ORDER BY id DESC LIMIT 50").fetchall()
        cards = db.execute(
            "SELECT card_no, COUNT(*) AS n FROM current_events "
            "GROUP BY card_no ORDER BY card_no").fetchall()
        summaries = []
        for c in cards:
            events = _card_events(c["card_no"])
            trips, anomalies = pair_events(events)
            summaries.append({
                "card_no": c["card_no"],
                "events": c["n"],
                "trips": len(trips),
                "anomalies": len(anomalies),
            })
        return render_template("index.html", uploads=uploads, summaries=summaries)

    @app.get("/cards/<path:card_no>")
    def card_detail(card_no: str):
        events = _card_events(card_no)
        if not events:
            abort(404)
        trips, anomalies = pair_events(events)
        return render_template(
            "card.html",
            card_no=card_no,
            events=events,
            trips=trips,
            anomalies=anomalies,
        )

    @app.get("/healthz")
    def healthz():
        return "ok", 200

    # ---------------------------------------------------------------- 上传

    @app.post("/upload")
    def upload():
        f = request.files.get("file")
        if f is None or not f.filename:
            flash("请选择要上传的 CSV 文件", "error")
            return redirect(url_for("index"))

        try:
            text = f.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            _record_rejected(f.filename, "文件不是有效的 UTF-8 编码", 0)
            flash("上传被拒绝：文件不是有效的 UTF-8 编码，原有结果保持不变", "error")
            return redirect(url_for("index"))

        # 1) 解析与批内冲突检测（不触碰数据库）
        try:
            rows, duplicates = parse_csv(text)
        except BatchError as e:
            _record_rejected(f.filename, str(e), 0)
            flash(f"上传被拒绝：{e}；原有结果保持不变", "error")
            return redirect(url_for("index"))

        # 2) 单事务合并：任何冲突整体回滚，查询侧不会看到新旧混合
        db = get_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            upload_id = db.execute(
                "INSERT INTO uploads (filename, uploaded_at, status) VALUES (?, ?, 'applied')",
                (f.filename, utc_now()),
            ).lastrowid
            applied, stale, noop = apply_batch(db, rows, upload_id)
            db.execute(
                "UPDATE uploads SET rows_total=?, rows_applied=?, rows_stale=?, rows_noop=?, message=? "
                "WHERE id=?",
                (len(rows), applied, stale, noop + duplicates, "OK", upload_id),
            )
            db.commit()
        except BatchError as e:
            db.rollback()
            _record_rejected(f.filename, str(e), len(rows))
            flash(f"上传被拒绝：{e}；原有结果保持不变", "error")
            return redirect(url_for("index"))
        except sqlite3.Error as e:
            db.rollback()
            _record_rejected(f.filename, f"数据库错误：{e}", len(rows))
            flash(f"上传失败（数据库错误），原有结果保持不变：{e}", "error")
            return redirect(url_for("index"))

        flash(
            f"上传成功：写入 {applied} 条新修订，忽略过期修订 {stale} 条，"
            f"重复/一致 {noop + duplicates} 条",
            "success",
        )
        return redirect(url_for("index"))

    # ---------------------------------------------------------------- 辅助

    def _card_events(card_no: str) -> list[dict]:
        rows = get_db().execute(
            "SELECT event_id, revision, card_no, event_time, station, direction "
            "FROM current_events WHERE card_no = ? "
            "ORDER BY event_time, event_id",
            (card_no,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _record_rejected(filename: str, message: str, rows_total: int) -> None:
        """失败批次单独落一条审计记录（不影响事件数据）。"""
        db = get_db()
        db.execute(
            "INSERT INTO uploads (filename, uploaded_at, status, message, rows_total) "
            "VALUES (?, ?, 'rejected', ?, ?)",
            (filename, utc_now(), message, rows_total),
        )
        db.commit()

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
