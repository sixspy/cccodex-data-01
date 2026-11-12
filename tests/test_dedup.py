from app.services.dedup import normalize_item_key


def test_item_key_depends_on_code_and_path_not_title():
    # 主键由条目编码与种子路径决定：编码规范化（空白/大小写）后相同即同键
    a = normalize_item_key("/mock/catalog/alpha", " A-001 ")
    b = normalize_item_key("/mock/catalog/alpha/", "a-001")
    assert a == b
    # 不同编码不同键（即使将来标题相同也不影响）
    assert a != normalize_item_key("/mock/catalog/alpha", "A-002")
    # 不同种子路径不同键
    assert a != normalize_item_key("/mock/catalog/beta", "A-001")
