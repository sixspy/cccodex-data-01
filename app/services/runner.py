"""采集运行器：三种方式（全量/增量/补采）+ 运行记录 + 冲突拒收。

方式（创建任务时选定，创建后不可改）：
- full        全量：抓整个目录，一次跑完锁死，禁止再次运行；
- incremental 增量：只入库本任务尚无的条目编码，可多次运行；
              请求携带本任务已知最大修订序号 since_rev，只取回变更条目；
- backfill    补采：只处理该种子上一次运行里被记为跳过的编码；
              无上次运行记录、或上次仍在运行中时禁止补采。

每次运行写一条 crawl_runs 记录（方式、修订起止、新写入、跳过、失败、
失败摘要），被跳过的编码写入 crawl_run_skips 供补采消费。
同次抓取同编码出现两种不同标题：整组拒收、计入失败、写失败摘要，
禁止静默覆盖。失败数 > 0 时任务状态为 partial（部分成功）。
"""

import json
import sqlite3

from flask import current_app

from app.db import get_db
from app.services.dedup import normalize_item_key
from app.services.mock_catalog import CATALOGS, parse_catalog_html

MODE_FULL = "full"
MODE_INCREMENTAL = "incremental"
MODE_BACKFILL = "backfill"
MODES = (MODE_FULL, MODE_INCREMENTAL, MODE_BACKFILL)

MODE_LABELS = {
    MODE_FULL: "全量",
    MODE_INCREMENTAL: "增量",
    MODE_BACKFILL: "补采",
}
STATUS_LABELS = {
    "draft": "草稿",
    "running": "运行中",
    "done": "成功",
    "partial": "部分成功",
    "failed": "失败",
}

_MAX_PAGES = 50  # 翻页保险，防止源站死循环


def run_job(job_id: int) -> dict:
    db = get_db()
    job = db.execute(
        "SELECT * FROM crawl_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if job is None:
        raise ValueError("任务不存在")
    if job["status"] == "running":
        raise ValueError("任务正在运行中，禁止重叠触发第二次运行")

    mode = job["mode"]
    seed = job["seed_path"]
    if seed not in CATALOGS:
        raise ValueError(f"未知种子路径: {seed}")
    if mode == MODE_FULL and job["status"] != "draft":
        raise ValueError("全量任务一次跑完即锁定，禁止再次运行")

    candidate_codes = None
    if mode == MODE_BACKFILL:
        last_run = _last_run_on_seed(db, seed)
        if last_run is None:
            raise ValueError("该种子尚无上次运行记录，禁止补采")
        if last_run["status"] == "running":
            raise ValueError("上一次运行仍在进行中，禁止补采")
        candidate_codes = [
            row["item_code"]
            for row in db.execute(
                "SELECT item_code FROM crawl_run_skips WHERE run_id = ? ORDER BY id",
                (last_run["id"],),
            )
        ]
        # 补采请求必须带上系统已知的该种子最大修订序号
        watermark = _seed_known_rev(db, seed)
    elif mode == MODE_INCREMENTAL:
        # 增量请求携带本任务已知最大修订序号；首次运行（NULL）不带，做基线全抓
        watermark = job["known_rev"]
    else:
        watermark = None

    rev_start = watermark if watermark is not None else 0
    run_id = _open_run(db, job_id, mode, rev_start)

    try:
        items, rev_end = _collect(seed, mode, watermark, candidate_codes)
        outcome = _ingest(db, job, run_id, mode, items, candidate_codes)
    except Exception as exc:  # 源站异常等：运行记录与任务都标记失败
        db.rollback()
        _close_run(db, run_id, status="failed", rev_end=rev_start,
                   counters={"inserted": 0, "skipped": 0, "failed": 0},
                   fail_summary=f"运行中断：{exc}")
        db.execute(
            "UPDATE crawl_jobs SET status = 'failed', finished_at = datetime('now') "
            "WHERE id = ?",
            (job_id,),
        )
        db.commit()
        current_app.logger.exception("job %s run failed", job_id)
        return {"status": "failed", "run_id": run_id, "mode": mode, "error": str(exc)}

    status = "done" if outcome["failed"] == 0 else "partial"
    _close_run(db, run_id, status=status, rev_end=rev_end, counters=outcome,
               fail_summary=outcome["fail_summary"])
    known_rev = max(rev_end, job["known_rev"] if job["known_rev"] is not None else 0)
    db.execute(
        "UPDATE crawl_jobs SET status = ?, known_rev = ?, "
        "finished_at = datetime('now') WHERE id = ?",
        (status, known_rev, job_id),
    )
    db.commit()

    current_app.logger.info(
        "job %s run %s %s inserted=%s skipped=%s failed=%s rev=%s->%s",
        job_id, run_id, status, outcome["inserted"], outcome["skipped"],
        outcome["failed"], rev_start, rev_end,
    )
    return {
        "status": status,
        "run_id": run_id,
        "mode": mode,
        "inserted": outcome["inserted"],
        "skipped": outcome["skipped"],
        "failed": outcome["failed"],
        "rev_start": rev_start,
        "rev_end": rev_end,
        "total_source": len(items),
    }


def _last_run_on_seed(db, seed: str):
    """该种子路径下（不限任务）最近一次运行记录。"""
    return db.execute(
        """
        SELECT r.* FROM crawl_runs r
        JOIN crawl_jobs j ON j.id = r.job_id
        WHERE j.seed_path = ?
        ORDER BY r.id DESC LIMIT 1
        """,
        (seed,),
    ).fetchone()


def _seed_known_rev(db, seed: str) -> int:
    row = db.execute(
        """
        SELECT MAX(r.rev_end) AS m FROM crawl_runs r
        JOIN crawl_jobs j ON j.id = r.job_id
        WHERE j.seed_path = ?
        """,
        (seed,),
    ).fetchone()
    return row["m"] if row and row["m"] is not None else 0


def _open_run(db, job_id: int, mode: str, rev_start: int) -> int:
    cur = db.execute(
        "INSERT INTO crawl_runs (job_id, mode, status, rev_start, rev_end) "
        "VALUES (?, ?, 'running', ?, ?)",
        (job_id, mode, rev_start, rev_start),
    )
    db.execute(
        "UPDATE crawl_jobs SET status = 'running', finished_at = NULL WHERE id = ?",
        (job_id,),
    )
    db.commit()  # 运行中状态先落库，重叠触发会被拒绝
    return cur.lastrowid


def _close_run(db, run_id: int, *, status: str, rev_end: int,
               counters: dict, fail_summary: str) -> None:
    db.execute(
        """
        UPDATE crawl_runs
        SET status = ?, rev_end = ?, inserted = ?, skipped = ?, failed = ?,
            fail_summary = ?, finished_at = datetime('now')
        WHERE id = ?
        """,
        (status, rev_end, counters["inserted"], counters["skipped"],
         counters["failed"], fail_summary, run_id),
    )


def _collect(seed: str, mode: str, watermark, candidate_codes):
    """按方式抓目录：全量翻全页；增量/补采携带已知最大修订序号。

    返回 (items, 源站当前修订序号)。翻页只跟随本系统页面链接。
    """
    params = []
    if mode == MODE_INCREMENTAL and watermark is not None:
        params.append(f"since_rev={watermark}")
    if mode == MODE_BACKFILL:
        if not candidate_codes:
            return [], watermark  # 上次运行没有跳过编码，无需请求
        params.append(f"since_rev={watermark}")
        params.append("codes=" + ",".join(candidate_codes))

    url = seed + ("?" + "&".join(params) if params else "")
    http = current_app.test_client()
    items: list[dict] = []
    revision = watermark if watermark is not None else 0
    for _ in range(_MAX_PAGES):
        resp = http.get(url)
        if resp.status_code != 200:
            raise RuntimeError(f"源站 {url} 返回 {resp.status_code}")
        page = parse_catalog_html(resp.get_data(as_text=True), seed)
        items.extend(page["items"])
        revision = page["revision"]
        nxt = page["next_url"]
        if not nxt:
            break
        if nxt != seed and not nxt.startswith(seed + "?"):
            raise RuntimeError(f"下一页链接越出本系统页面: {nxt}")
        url = nxt
    else:
        raise RuntimeError(f"翻页超过 {_MAX_PAGES} 页，疑似源站分页异常")
    return items, revision


def _ingest(db, job, run_id: int, mode: str, items: list[dict],
            candidate_codes) -> dict:
    """去重、判冲突、按方式落库；返回计数与失败摘要。"""
    groups: dict[str, list[dict]] = {}
    for it in items:
        groups.setdefault(normalize_item_key(job["seed_path"], it["sku"]), []).append(it)

    inserted = skipped = failed = 0
    fail_msgs: list[str] = []
    skip_rows: list[tuple[str, str]] = []

    for key, group in groups.items():
        code = group[0]["sku"]
        titles = {g["title"] for g in group}
        if len(titles) > 1:
            # 同次抓取同编码两种标题：整组拒收，禁止静默覆盖
            failed += len(group)
            fail_msgs.append(
                f"编码 {code} 同次抓取出现 {len(titles)} 种标题"
                f"（{' / '.join(sorted(titles))}），{len(group)} 条均拒收"
            )
            continue
        for _dup in group[1:]:
            skipped += 1
            skip_rows.append((code, "同次抓取重复"))
        it = group[0]
        existing = db.execute(
            "SELECT id, title FROM crawl_records WHERE job_id = ? AND item_key = ?",
            (job["id"], key),
        ).fetchone()
        payload = json.dumps(it, ensure_ascii=False)
        if mode in (MODE_FULL, MODE_INCREMENTAL):
            if existing is not None:
                skipped += 1
                skip_rows.append((code, "库中已存在"))
            else:
                _insert_record(db, job["id"], key, it, payload)
                inserted += 1
        else:  # 补采：重新写入（已存在则更新标题与负载）
            if existing is not None:
                db.execute(
                    "UPDATE crawl_records SET title = ?, payload_json = ?, "
                    "fetched_at = datetime('now') WHERE id = ?",
                    (it["title"], payload, existing["id"]),
                )
            else:
                _insert_record(db, job["id"], key, it, payload)
            inserted += 1

    if mode == MODE_BACKFILL:
        fetched = {it["sku"] for it in items}
        for code in candidate_codes or []:
            if code not in fetched:
                skipped += 1
                skip_rows.append((code, "源站本次未返回该编码"))

    db.executemany(
        "INSERT INTO crawl_run_skips (run_id, item_code, reason) VALUES (?, ?, ?)",
        [(run_id, code, reason) for code, reason in skip_rows],
    )
    return {
        "inserted": inserted,
        "skipped": skipped,
        "failed": failed,
        "fail_summary": "\n".join(fail_msgs),
    }


def _insert_record(db, job_id: int, key: str, it: dict, payload: str) -> None:
    try:
        db.execute(
            """
            INSERT INTO crawl_records (job_id, item_key, item_code, title, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (job_id, key, it["sku"], it["title"], payload),
        )
    except sqlite3.IntegrityError as exc:
        raise RuntimeError(f"编码 {it['sku']} 落库冲突（禁止静默覆盖）: {exc}") from exc
