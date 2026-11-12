from flask import Blueprint, Response, redirect, request

from app.services.mock_catalog import bump_catalog_revision, render_catalog_page

bp = Blueprint("mock", __name__)


@bp.get("/mock/catalog/<catalog_id>")
def catalog(catalog_id: str):
    path = f"/mock/catalog/{catalog_id}"
    page = request.args.get("page", 1, type=int)
    since_rev = request.args.get("since_rev", type=int)  # 缺省不带 → 全量
    codes_arg = request.args.get("codes")
    codes = [c for c in codes_arg.split(",") if c] if codes_arg is not None else None
    html = render_catalog_page(path, page=page, since_rev=since_rev, codes=codes)
    if isinstance(html, tuple):
        body, code = html
        return Response(body, status=code, mimetype="text/html; charset=utf-8")
    return Response(html, mimetype="text/html; charset=utf-8")


@bp.post("/mock/catalog/<catalog_id>/bump")
def bump(catalog_id: str):
    """模拟源站改版：修订序号 +1（至少新增一条、至少变更一条已有标题）。"""
    path = f"/mock/catalog/{catalog_id}"
    if not bump_catalog_revision(path):
        return Response(
            f"<html><body><h1>404</h1><p>{path}</p></body></html>",
            status=404,
            mimetype="text/html; charset=utf-8",
        )
    return redirect(f"/mock/catalog/{catalog_id}", code=303)
