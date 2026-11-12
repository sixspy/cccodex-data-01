from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.db import get_db
from app.routes.auth import login_required
from app.services.mock_catalog import CATALOGS
from app.services.runner import (
    MODE_BACKFILL,
    MODE_FULL,
    MODE_LABELS,
    MODES,
    STATUS_LABELS,
    run_job,
)

bp = Blueprint("jobs", __name__)


@bp.route("/")
@login_required
def list_jobs():
    db = get_db()
    jobs = db.execute(
        "SELECT * FROM crawl_jobs ORDER BY id DESC"
    ).fetchall()
    counts = {
        row["job_id"]: row["c"]
        for row in db.execute(
            "SELECT job_id, COUNT(*) AS c FROM crawl_records GROUP BY job_id"
        )
    }
    return render_template(
        "jobs_list.html",
        jobs=jobs,
        counts=counts,
        seeds=sorted(CATALOGS.keys()),
        modes=MODES,
        mode_labels=MODE_LABELS,
        status_labels=STATUS_LABELS,
    )


@bp.post("/jobs")
@login_required
def create_job():
    name = request.form.get("name", "").strip()
    seed = request.form.get("seed_path", "").strip()
    mode = request.form.get("mode", "").strip()
    if not name or not seed:
        flash("请填写任务名与种子路径", "error")
        return redirect(url_for("jobs.list_jobs"))
    if seed not in CATALOGS:
        flash("种子路径必须是已注册的 mock 目录", "error")
        return redirect(url_for("jobs.list_jobs"))
    if mode not in MODES:
        flash("采集方式必须是：全量 / 增量 / 补采 之一", "error")
        return redirect(url_for("jobs.list_jobs"))
    db = get_db()
    db.execute(
        "INSERT INTO crawl_jobs (name, seed_path, mode, status) "
        "VALUES (?, ?, ?, 'draft')",
        (name, seed, mode),
    )
    db.commit()
    flash(f"任务已创建（方式：{MODE_LABELS[mode]}，创建后不可改）", "ok")
    return redirect(url_for("jobs.list_jobs"))


@bp.post("/jobs/<int:job_id>/run")
@login_required
def run_job_view(job_id: int):
    try:
        summary = run_job(job_id)
    except ValueError as e:
        flash(str(e), "error")
    except Exception as e:
        flash(f"运行失败: {e}", "error")
    else:
        if summary["status"] == "partial":
            flash(
                f"部分成功：新写入 {summary['inserted']} 条，跳过 {summary['skipped']} 条，"
                f"失败 {summary['failed']} 条（详见运行记录摘要）",
                "warn",
            )
        elif summary["status"] == "failed":
            flash(f"运行失败：{summary.get('error', '未知错误')}", "error")
        else:
            flash(
                f"运行完成（{MODE_LABELS[summary['mode']]}）："
                f"新写入 {summary['inserted']} 条，跳过 {summary['skipped']} 条，"
                f"失败 0 条",
                "ok",
            )
    return redirect(url_for("jobs.detail", job_id=job_id))


@bp.get("/jobs/<int:job_id>")
@login_required
def detail(job_id: int):
    db = get_db()
    job = db.execute("SELECT * FROM crawl_jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        flash("任务不存在", "error")
        return redirect(url_for("jobs.list_jobs"))
    records = db.execute(
        "SELECT * FROM crawl_records WHERE job_id = ? ORDER BY id",
        (job_id,),
    ).fetchall()
    # 运行记录按时间倒序
    runs = db.execute(
        "SELECT * FROM crawl_runs WHERE job_id = ? ORDER BY id DESC",
        (job_id,),
    ).fetchall()
    skips = {}
    for row in db.execute(
        """
        SELECT s.run_id, s.item_code, s.reason FROM crawl_run_skips s
        JOIN crawl_runs r ON r.id = s.run_id
        WHERE r.job_id = ? ORDER BY s.id
        """,
        (job_id,),
    ):
        skips.setdefault(row["run_id"], []).append(row)

    # 该种子最近一次运行（补采前置条件判断用）
    last_seed_run = db.execute(
        """
        SELECT r.* FROM crawl_runs r
        JOIN crawl_jobs j ON j.id = r.job_id
        WHERE j.seed_path = ? ORDER BY r.id DESC LIMIT 1
        """,
        (job["seed_path"],),
    ).fetchone()

    can_run, block_reason = _run_gate(job, last_seed_run)
    return render_template(
        "job_detail.html",
        job=job,
        records=records,
        runs=runs,
        skips=skips,
        can_run=can_run,
        block_reason=block_reason,
        last_seed_run=last_seed_run,
        mode_labels=MODE_LABELS,
        status_labels=STATUS_LABELS,
    )


def _run_gate(job, last_seed_run):
    """运行按钮是否可用及不可点原因（服务端 run_job 会再次校验）。"""
    if job["status"] == "running":
        return False, "任务正在运行中，禁止重叠触发第二次运行"
    if job["mode"] == MODE_FULL and job["status"] != "draft":
        return False, "全量任务一次跑完即锁定，禁止再次运行"
    if job["mode"] == MODE_BACKFILL:
        if last_seed_run is None:
            return False, "该种子尚无上次运行记录，补采不可用"
        if last_seed_run["status"] == "running":
            return False, "上一次运行仍在进行中，补采不可用"
    return True, ""
