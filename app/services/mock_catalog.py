"""站内 mock 目录页：模拟可采集列表源站（无外网依赖）。

支持能力：
- 分页：每页 3 条，页内给出 ``next-page`` 链接，翻页不离开本系统页面；
- 修订序号：每个目录维护 ``revision``（缺省 0），条目带 ``rev`` 表示
  最后被新增/变更时的修订序号。``bump_catalog_revision`` 模拟源站改版：
  每次升序号至少新增一条、且至少变更一条已有标题；
- 增量水位：请求带 ``since_rev=K`` 时只返回 ``rev > K`` 的条目，
  调用方不必每次全量重抓；
- 指定编码：请求带 ``codes=A,B`` 时只返回这些编码的当前条目
  （补采用，此时不按 since_rev 过滤）。

``LAST_REQUESTS`` 记录源站收到的请求参数，供测试断言"请求确实带了修订序号"。
"""

from html import escape

PAGE_SIZE = 3

# 初始目录数据（修订序号均为 0）
_SEED = {
    "/mock/catalog/alpha": [
        {"sku": "A-001", "title": "市政管线测绘记录表"},
        {"sku": "A-002", "title": "排水井巡查台账"},
        {"sku": "A-003", "title": "排水井巡查台账（东片区）"},
        # 同编码同标题重复出现：演示"同次抓取重复 → 跳过"
        {"sku": "A-002", "title": "排水井巡查台账"},
        {"sku": "A-004", "title": "河道断面测量月报"},
    ],
    "/mock/catalog/beta": [
        {"sku": "B-101", "title": "泵站运行日报"},
        {"sku": "B-102", "title": "雨污分流抽检单"},
    ],
    "/mock/catalog/gamma": [
        {"sku": "C-201", "title": "闸门启闭记录"},
        {"sku": "C-202", "title": "水质快检汇总"},
        # 同编码不同标题：演示冲突 → 两条均拒收、计失败
        {"sku": "C-201", "title": "闸门启闭记录（改版）"},
    ],
}

# 运行态：{path: {"revision": int, "items": [ {sku, title, rev} ]}}
_STATE: dict[str, dict] = {}

# 源站收到的请求日志（测试可断言增量/补采确实携带修订序号）
LAST_REQUESTS: list[dict] = []


def reset_catalogs() -> None:
    """恢复初始目录与修订序号（测试隔离用）。"""
    _STATE.clear()
    LAST_REQUESTS.clear()
    for path, items in _SEED.items():
        _STATE[path] = {
            "revision": 0,
            "items": [dict(it, rev=0) for it in items],
        }


reset_catalogs()

# 兼容旧引用：种子路径集合
CATALOGS = _STATE


def bump_catalog_revision(path: str) -> bool:
    """模拟源站改版：修订序号 +1，至少新增一条、至少变更一条已有标题。"""
    state = _STATE.get(path)
    if state is None:
        return False
    state["revision"] += 1
    rev = state["revision"]
    items = state["items"]

    # 变更一条已有标题（按唯一编码轮流选择；同编码多行一起改，保持目录自洽）
    unique_skus = list(dict.fromkeys(it["sku"] for it in items))
    target_sku = unique_skus[(rev - 1) % len(unique_skus)]
    for it in items:
        if it["sku"] == target_sku:
            it["title"] = f"{it['title']}（修订{rev}）"
            it["rev"] = rev

    # 新增一条
    prefix = unique_skus[0].split("-")[0]
    max_num = max(int(s.split("-")[1]) for s in unique_skus)
    items.append(
        {
            "sku": f"{prefix}-{max_num + 1:03d}",
            "title": f"新增巡查条目{max_num + 1}（修订{rev}）",
            "rev": rev,
        }
    )
    return True


def render_catalog_page(
    path: str,
    page: int = 1,
    since_rev: int | None = None,
    codes: list[str] | None = None,
):
    """渲染一页目录 HTML；未知路径返回 (html, 404)。"""
    state = _STATE.get(path)
    if state is None:
        return _not_found(path)

    LAST_REQUESTS.append(
        {"path": path, "page": page, "since_rev": since_rev, "codes": codes}
    )

    items = state["items"]
    if codes is not None:
        wanted = set(codes)
        items = [it for it in items if it["sku"] in wanted]
    elif since_rev is not None:
        items = [it for it in items if it["rev"] > since_rev]

    page = max(page, 1)
    start = (page - 1) * PAGE_SIZE
    page_items = items[start : start + PAGE_SIZE]
    has_next = start + PAGE_SIZE < len(items)

    rows = "".join(
        f"<li data-sku='{escape(it['sku'])}' data-rev='{it['rev']}'>"
        f"{escape(it['title'])}</li>"
        for it in page_items
    )

    params = []
    if since_rev is not None:
        params.append(f"since_rev={since_rev}")
    if codes is not None:
        params.append("codes=" + ",".join(codes))
    suffix = ("&" + "&".join(params)) if params else ""
    next_link = (
        f"<a class='next-page' href='{escape(path)}?page={page + 1}{suffix}'>下一页</a>"
        if has_next
        else ""
    )

    catalog_id = path.rsplit("/", 1)[-1]
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="catalog-revision" content="{state['revision']}">
<title>Mock 目录 {escape(path)}</title></head>
<body>
<h1>采集演练目录</h1>
<p class="seed">{escape(path)}</p>
<p class="revision">当前修订序号：{state['revision']} · 第 {page} 页</p>
<form method="post" action="/mock/catalog/{escape(catalog_id)}/bump">
  <button type="submit">模拟源站改版（修订序号 +1）</button>
</form>
<ul id="items">{rows}</ul>
{next_link}
</body></html>"""


def _not_found(path: str):
    return f"<html><body><h1>404</h1><p>{escape(path)}</p></body></html>", 404


def parse_catalog_html(html: str, seed_path: str) -> dict:
    """解析目录页：条目（含 rev）、当前修订序号、下一页链接。"""
    import re

    rev_m = re.search(r'<meta\s+name="catalog-revision"\s+content="(\d+)"', html)
    items = []
    for sku, rev, title in re.findall(
        r"<li\s+data-sku='([^']+)'\s+data-rev='(\d+)'>([^<]+)</li>",
        html,
        re.IGNORECASE,
    ):
        items.append(
            {
                "sku": sku,
                "title": title.strip(),
                "rev": int(rev),
                "seed_path": seed_path,
            }
        )
    next_m = re.search(r"<a\s+class='next-page'\s+href='([^']+)'>", html)
    return {
        "items": items,
        "revision": int(rev_m.group(1)) if rev_m else 0,
        "next_url": next_m.group(1) if next_m else None,
    }
