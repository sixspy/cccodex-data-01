import pytest

from app.db import get_db
from app.services.mock_catalog import LAST_REQUESTS
from app.services.runner import run_job

ALPHA = "/mock/catalog/alpha"
GAMMA = "/mock/catalog/gamma"
BETA = "/mock/catalog/beta"


def login(client):
    return client.post(
        "/login",
        data={"username": "admin", "password": "123456"},
        follow_redirects=True,
    )


def create_job(client, name, seed, mode):
    client.post(
        "/jobs",
        data={"name": name, "seed_path": seed, "mode": mode},
        follow_redirects=True,
    )
    with client.application.app_context():
        return get_db().execute(
            "SELECT id FROM crawl_jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()["id"]


def db_row(app, sql, args=()):
    with app.app_context():
        return get_db().execute(sql, args).fetchone()


def db_all(app, sql, args=()):
    with app.app_context():
        return get_db().execute(sql, args).fetchall()


def run(app, job_id):
    with app.app_context():
        return run_job(job_id)


# ---------- 1. 增量连续两次 ----------

def test_incremental_run_twice(app, client):
    login(client)
    job_id = create_job(client, "inc", ALPHA, "incremental")

    s1 = run(app, job_id)
    assert s1["status"] == "done"
    assert s1["inserted"] == 4   # alpha 5 条里 A-002 同编码重复 1 条
    assert s1["skipped"] == 1
    assert s1["failed"] == 0
    assert s1["rev_start"] == 0 and s1["rev_end"] == 0

    # 第二次：源站未改版，请求带已知最大修订序号，取回 0 条，什么都不写
    s2 = run(app, job_id)
    assert s2["status"] == "done"
    assert (s2["inserted"], s2["skipped"], s2["failed"]) == (0, 0, 0)
    assert s2["total_source"] == 0
    assert LAST_REQUESTS[-1]["since_rev"] == 0  # 确实带水位请求，而非全量重抓

    assert db_row(app, "SELECT COUNT(*) c FROM crawl_records WHERE job_id=?",
                  (job_id,))["c"] == 4
    assert db_row(app, "SELECT COUNT(*) c FROM crawl_runs WHERE job_id=?",
                  (job_id,))["c"] == 2
    job = db_row(app, "SELECT * FROM crawl_jobs WHERE id=?", (job_id,))
    assert job["status"] == "done" and job["known_rev"] == 0


# ---------- 2. 补采仅吃上次跳过的编码 ----------

def test_backfill_only_consumes_last_run_skips(app, client):
    login(client)
    inc_id = create_job(client, "inc", ALPHA, "incremental")
    s1 = run(app, inc_id)
    assert s1["skipped"] == 1  # A-002 同次重复被记为跳过

    # 源站改版：修订 0 → 1（A-001 改标题、新增 A-005）
    r = client.post("/mock/catalog/alpha/bump", follow_redirects=False)
    assert r.status_code == 303

    bf_id = create_job(client, "bf", ALPHA, "backfill")
    s2 = run(app, bf_id)
    assert s2["status"] == "done"
    # 只处理上次跳过的 A-002：源站返回它的两行（同标题）→ 1 写入 + 1 重复跳过
    assert s2["inserted"] == 1
    assert s2["skipped"] == 1
    assert s2["failed"] == 0
    # 补采请求携带已知最大修订序号与指定编码
    bf_req = [q for q in LAST_REQUESTS if q["codes"] == ["A-002"]]
    assert bf_req and bf_req[0]["since_rev"] == 0

    # 补采任务只落了 A-002；新增的 A-005、改标题的 A-001 都不归它处理
    codes = [r["item_code"] for r in db_all(
        app, "SELECT item_code FROM crawl_records WHERE job_id=?", (bf_id,))]
    assert codes == ["A-002"]
    # 上一个任务的库未被补采改动（A-001 仍是旧标题）
    t = db_row(app,
               "SELECT title FROM crawl_records WHERE job_id=? AND item_code='A-001'",
               (inc_id,))["title"]
    assert t == "市政管线测绘记录表"


# ---------- 3. 修订序号升高后的增量行为 ----------

def test_revision_bump_incremental_fetches_only_delta(app, client):
    login(client)
    job_id = create_job(client, "inc", ALPHA, "incremental")
    run(app, job_id)

    client.post("/mock/catalog/alpha/bump")
    s2 = run(app, job_id)
    # 只取回 rev>0 的两条：改标题的 A-001（库中已有→跳过）、新增的 A-005（写入）
    assert s2["total_source"] == 2
    assert s2["inserted"] == 1
    assert s2["skipped"] == 1
    assert s2["rev_start"] == 0 and s2["rev_end"] == 1
    assert LAST_REQUESTS[-1]["since_rev"] == 0  # 带水位，未全量重抓

    job = db_row(app, "SELECT * FROM crawl_jobs WHERE id=?", (job_id,))
    assert job["known_rev"] == 1
    assert db_row(app, "SELECT COUNT(*) c FROM crawl_records WHERE job_id=?",
                  (job_id,))["c"] == 5
    # 增量不改已有条目标题
    assert db_row(app,
                  "SELECT title FROM crawl_records WHERE job_id=? AND item_code='A-001'",
                  (job_id,))["title"] == "市政管线测绘记录表"


# ---------- 4. 同次抓取同编码两种标题：整组拒收 ----------

def test_same_code_dual_titles_rejected(app, client):
    login(client)
    job_id = create_job(client, "full-gamma", GAMMA, "full")
    s = run(app, job_id)
    assert s["status"] == "partial"
    assert s["inserted"] == 1   # 只有 C-202 入库
    assert s["failed"] == 2     # C-201 两条均拒收
    assert s["skipped"] == 0

    codes = [r["item_code"] for r in db_all(
        app, "SELECT item_code FROM crawl_records WHERE job_id=?", (job_id,))]
    assert codes == ["C-202"]   # C-201 未静默覆盖、未入库

    row = db_row(app, "SELECT * FROM crawl_runs WHERE job_id=?", (job_id,))
    assert row["failed"] == 2
    assert "C-201" in row["fail_summary"]
    assert "拒收" in row["fail_summary"]


# ---------- 5. 运行中禁止重叠第二次运行 ----------

def test_run_while_running_rejected(app, client):
    login(client)
    job_id = create_job(client, "inc", ALPHA, "incremental")
    with app.app_context():
        db = get_db()
        db.execute("UPDATE crawl_jobs SET status='running' WHERE id=?", (job_id,))
        db.commit()
        with pytest.raises(ValueError, match="重叠|运行中"):
            run_job(job_id)

    r = client.post(f"/jobs/{job_id}/run", follow_redirects=True)
    assert "禁止重叠" in r.get_data(as_text=True)
    assert db_row(app, "SELECT COUNT(*) c FROM crawl_runs WHERE job_id=?",
                  (job_id,))["c"] == 0


# ---------- 6. 部分成功状态与计数 ----------

def test_partial_success_status_and_counts(app, client):
    login(client)
    job_id = create_job(client, "full-gamma", GAMMA, "full")
    s = run(app, job_id)
    assert s["status"] == "partial"

    job = db_row(app, "SELECT * FROM crawl_jobs WHERE id=?", (job_id,))
    assert job["status"] == "partial"  # 失败>0 → 不得展示为全胜

    r = client.get(f"/jobs/{job_id}")
    html = r.get_data(as_text=True)
    assert "部分成功" in html
    assert "失败 2" in html
    assert "C-201" in html  # 失败摘要可点开查看


# ---------- 7. 补采前置条件：无上次运行记录禁止补采 ----------

def test_backfill_requires_previous_run(app, client):
    login(client)
    job_id = create_job(client, "bf", ALPHA, "backfill")
    with app.app_context():
        with pytest.raises(ValueError, match="上次运行记录"):
            run_job(job_id)

    r = client.post(f"/jobs/{job_id}/run", follow_redirects=True)
    assert "禁止补采" in r.get_data(as_text=True)
    assert db_row(app, "SELECT COUNT(*) c FROM crawl_runs WHERE job_id=?",
                  (job_id,))["c"] == 0

    # 详情页按钮不可点并给出原因
    html = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert "disabled" in html
    assert "尚无上次运行记录" in html


# ---------- 8. 全量一次跑完锁死 ----------

def test_full_mode_locks_after_run(app, client):
    login(client)
    job_id = create_job(client, "full-beta", BETA, "full")
    s = run(app, job_id)
    assert s["status"] == "done" and s["inserted"] == 2

    with app.app_context():
        with pytest.raises(ValueError, match="锁定"):
            run_job(job_id)
    r = client.post(f"/jobs/{job_id}/run", follow_redirects=True)
    assert "锁定" in r.get_data(as_text=True)
    assert db_row(app, "SELECT COUNT(*) c FROM crawl_runs WHERE job_id=?",
                  (job_id,))["c"] == 1


# ---------- 9. 目录分页与修订序号 ----------

def test_mock_catalog_pagination_and_revision(client):
    r = client.get("/mock/catalog/alpha")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert '<meta name="catalog-revision" content="0">' in html
    assert html.count("data-sku=") == 3          # 每页 3 条
    assert "next-page" in html                    # 有下一页（本系统页面）

    html2 = client.get("/mock/catalog/alpha?page=2").get_data(as_text=True)
    assert html2.count("data-sku=") == 2
    assert "next-page" not in html2

    # 改版：修订 +1，至少新增一条、至少变更一条已有标题
    client.post("/mock/catalog/alpha/bump")
    html3 = client.get("/mock/catalog/alpha").get_data(as_text=True)
    assert '<meta name="catalog-revision" content="1">' in html3
    assert "（修订1）" in html3                      # 已有标题被变更（A-001，第 1 页）
    html3b = client.get("/mock/catalog/alpha?page=2").get_data(as_text=True)
    assert "A-005" in html3b and "新增巡查条目" in html3b  # 新增条目（第 2 页）

    # since_rev=0 只返回修订后的条目
    html4 = client.get("/mock/catalog/alpha?since_rev=0").get_data(as_text=True)
    assert html4.count("data-sku=") == 2


def test_mock_catalog_http(client):
    r = client.get("/mock/catalog/alpha")
    assert r.status_code == 200
    assert "市政管线测绘记录表" in r.get_data(as_text=True)
