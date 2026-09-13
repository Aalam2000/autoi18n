# src/autoi18n/extractor/file_scanner.py
"""
Поиск файлов для сканирования без использования glob.

Причина: старая реализация полагалась на паттерны вида "*.{js,jsx,tsx}",
а встроенный в Python модуль glob НЕ поддерживает brace-expansion (это
фича bash/zsh, не Python) — такие паттерны молча находили ноль файлов.

Вместо этого явно задаём (папка + список расширений) и рекурсивно обходим
дерево через os.walk, пропуская служебные/тяжёлые директории.
"""
import os
from typing import Dict, List

IGNORED_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv",
    "dist", "build", ".idea", ".pytest_cache", ".next", "coverage",
}


def resolve_files(root: str, extensions: List[str]) -> List[str]:
    """
    Рекурсивно находит все файлы с указанными расширениями внутри root.
    Расширения сравниваются без учёта регистра, с ведущей точкой (".js").
    Не падает, если root не существует — просто возвращает пустой список
    (проект может не иметь, например, backend/templates).
    """
    if not root or not os.path.isdir(root):
        return []
    exts = {e.lower() for e in extensions}
    result: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.startswith(".")]
        for name in filenames:
            _, ext = os.path.splitext(name)
            if ext.lower() in exts:
                result.append(os.path.join(dirpath, name))
    return sorted(result)


def resolve_scan_paths(scan_paths: List[Dict[str, object]]) -> Dict[str, List[str]]:
    """
    Принимает список записей {"path": "frontend/src", "extensions": [".js", ".jsx"], "type": "js"}
    (см. config.DEFAULT_SCAN_PATHS) и возвращает {"js": [...], "html": [...]}.

    type "auto" раскладывает найденные файлы по расширению самостоятельно;
    неизвестный/отсутствующий type трактуется как "auto".
    """
    files: Dict[str, List[str]] = {"js": [], "html": []}
    for entry in scan_paths:
        root = str(entry.get("path", ""))
        extensions = list(entry.get("extensions", []))
        kind = str(entry.get("type", "auto"))
        found = resolve_files(root, extensions)

        if kind in files:
            files[kind].extend(found)
        else:
            for f in found:
                if f.lower().endswith((".html", ".htm")):
                    files["html"].append(f)
                else:
                    files["js"].append(f)

    files["js"] = sorted(set(files["js"]))
    files["html"] = sorted(set(files["html"]))
    return files
