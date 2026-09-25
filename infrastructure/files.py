"""Низкоуровневые операции с файловыми артефактами и их хешами."""
import hashlib
import json
from pathlib import Path
import tempfile


def read_json(path):
    """Прочитать JSON из локального файла."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    """Атомарно записать JSON, не оставляя частично записанный целевой файл."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8",
            dir=path.parent, suffix=".tmp", delete=False,
        ) as output:
            temporary = Path(output.name)
            json.dump(value, output, ensure_ascii=False, indent=2)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def file_sha256(path):
    """Вычислить SHA-256 файла с постоянным расходом памяти."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def json_sha256(value):
    """Стабильный SHA-256 JSON-совместимого значения."""
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
