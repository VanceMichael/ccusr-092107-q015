"""读取并检查项目领域资料。"""

import json
from pathlib import Path


def load_context(path: Path) -> dict:
    """返回字段完整的领域资料。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    required = {"domain", "version", "facts", "sample_id"}
    if not required.issubset(data):
        raise ValueError("领域资料缺少必要字段")
    return data
