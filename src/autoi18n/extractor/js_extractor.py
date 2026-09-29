# src/autoi18n/extractor/js_extractor.py
"""
Python-обёртка над Node/Babel мостом (js_bridge/extract.js).

Раньше JS/JSX парсились регулярными выражениями — хрупко на вложенных
выражениях, многострочных шаблонах, JSX-фрагментах и т.д. Теперь разбор
делает настоящий AST-парсер (Babel) через отдельный процесс Node.js —
Node обязателен для этой части (см. README, раздел "Требования").

Важно по производительности: все файлы передаются в ОДИН вызов Node,
а не по одному — иначе на каждый файл уходило бы время на старт Node,
что на большом проекте будет заметно медленнее.

Отказоустойчивость: если конкретный файл не смог распарситься (непривычный
синтаксис), это логируется и файл пропускается — не валит весь скан.
"""
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_BRIDGE_DIR = Path(__file__).parent / "js_bridge"
_EXTRACT_SCRIPT = _BRIDGE_DIR / "extract.js"

# Пока файлов много, но каждый в среднем небольшой — таймаут с запасом.
_SUBPROCESS_TIMEOUT_SECONDS = 120


# Проблемы последнего вызова extract_js_items_from_files — для отчёта
# (report.py): {"bridge": ошибка самого моста или None, "files": {файл: ошибка разбора}}
LAST_PROBLEMS: Dict[str, object] = {"bridge": None, "files": {}}


class NodeNotFoundError(RuntimeError):
    """Node.js не найден в PATH — обязателен для разбора JS/JSX/TSX через AST."""


def _ensure_node_available() -> str:
    node_path = shutil.which("node")
    if not node_path:
        raise NodeNotFoundError(
            "Node.js не найден в PATH. autoi18n требует Node.js для разбора "
            "JS/JSX/TSX через AST-парсер (Babel) — см. README, раздел 'Требования'. "
            "Установите Node.js в окружении, где запускается сканирование/воркер."
        )
    return node_path


def extract_js_items_from_files(
    file_paths: List[str], extra_attrs: Optional[List[str]] = None
) -> Dict[str, List[Dict[str, object]]]:
    """
    Извлекает переводимые фразы из списка JS/JSX/TSX файлов одним вызовом Node.

    Возвращает {file_path: [{"text": str, "placeholders": int, "line": int|None}, ...]}.
    Файлы с ошибкой парсинга в результат не попадают (только предупреждение в лог).

    Бросает NodeNotFoundError, если Node.js недоступен — это осознанное жёсткое
    требование (см. README), а не тихий пропуск JS-сканирования.
    """
    LAST_PROBLEMS["bridge"] = None
    LAST_PROBLEMS["files"] = {}
    if not file_paths:
        return {}

    try:
        node_path = _ensure_node_available()
    except NodeNotFoundError as e:
        LAST_PROBLEMS["bridge"] = str(e)
        raise

    try:
        env = dict(os.environ)
        env["AUTOI18N_EXTRA_ATTRS"] = ",".join(extra_attrs or [])
        proc = subprocess.run(
            [node_path, str(_EXTRACT_SCRIPT), *file_paths],
            env=env,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        LAST_PROBLEMS["bridge"] = f"JS-мост не уложился в {_SUBPROCESS_TIMEOUT_SECONDS} с"
        logger.error(f"JS extraction bridge timed out after {_SUBPROCESS_TIMEOUT_SECONDS}s")
        return {}

    if proc.returncode != 0:
        LAST_PROBLEMS["bridge"] = f"JS-мост завершился с кодом {proc.returncode}: {proc.stderr[:1000]}"
        logger.error(f"JS extraction bridge exited with {proc.returncode}: {proc.stderr[:1000]}")
        return {}

    try:
        raw: Dict[str, object] = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        LAST_PROBLEMS["bridge"] = f"JS-мост вернул не JSON: {exc}"
        logger.error(f"JS extraction bridge returned invalid JSON: {exc}")
        return {}

    errors = raw.pop("_errors", {}) or {}
    LAST_PROBLEMS["files"] = dict(errors)
    for file_path, message in errors.items():
        logger.warning(f"Пропущен файл (ошибка разбора): {file_path}: {message}")

    return raw  # type: ignore[return-value]
