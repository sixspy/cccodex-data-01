"""落库主键：规范化种子路径 + 条目编码，供 UNIQUE 约束与冲突检测使用。

主键只由「条目编码 + 种子路径」决定，与标题无关——
标题变更（修订）不应产生新主键，同编码双标题由 runner 判冲突。
"""

import re
from urllib.parse import unquote


def normalize_item_key(seed_path: str, item_code: str) -> str:
    path = unquote(seed_path.strip().lower().rstrip("/"))
    code = re.sub(r"\s+", "", item_code.strip().lower())
    return f"{path}::{code}"
