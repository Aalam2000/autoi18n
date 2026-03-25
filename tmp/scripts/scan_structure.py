"""
tmp/scripts/scan_structure.py
====================================================
📘 Отчёт структуры библиотеки auto-i18n-lib

Назначение:
  Сканирует структуру библиотеки с учётом .gitignore
  и формирует компактный отчёт без лишних инфраструктурных блоков.

Что включает:
  - структуру проекта
  - метаданные пакета из pyproject.toml
  - список модулей пакета
  - краткую техсводку по translator.py
  - рабочие условия ChatGPT

Вывод:
  tmp/reports/project_structure_chatassist.txt
"""

from pathlib import Path
from pathspec import PathSpec
import tomllib

# ───────────────────────────────────────────────────────────────
# Пути
# ───────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[2]
GITIGNORE = ROOT / ".gitignore"
REPORTS_DIR = ROOT / "tmp" / "reports"
OUTPUT = REPORTS_DIR / "project_structure_chatassist.txt"

PYPROJECT = ROOT / "pyproject.toml"
README = ROOT / "README.md"
PACKAGE_DIR = ROOT / "src" / "autoi18n"
TRANSLATOR_FILE = PACKAGE_DIR / "translator.py"

# ───────────────────────────────────────────────────────────────
# Игнорируемые каталоги
# ───────────────────────────────────────────────────────────────
ALWAYS_IGNORE = {
    ".git",
    "__pycache__",
    "tmp",
    ".idea",
    ".vscode",
    "node_modules",
    "venv",
    ".venv",
    ".pytest_cache",
    ".mypy_cache",
    "build",
    "dist",
}

# ───────────────────────────────────────────────────────────────
# Условия работы ChatGPT
# ───────────────────────────────────────────────────────────────
WORKING_CONDITIONS = """
Рабочие условия для ChatGPT (фиксированный блок):

1. Пользователь работает на ноутбуке Windows 11 в PyCharm.
2. Весь проект использует НЕСТАНДАРТНЫЙ код, поэтому:
     — никаких догадок,
     — ни один вывод не строится без реальных данных,
     — при малейшей неоднозначности нужно запрашивать исходные файлы.
3. Всегда использовать пути относительно корня проекта.
4. Все отчёты должны генерироваться в tmp/reports.
"""

# ───────────────────────────────────────────────────────────────
# Вспомогательные функции
# ───────────────────────────────────────────────────────────────
def load_gitignore(path: Path) -> PathSpec:
    if not path.exists():
        return PathSpec.from_lines("gitwildmatch", [])
    with open(path, "r", encoding="utf-8") as f:
        lines = [
            line.strip()
            for line in f
            if line.strip() and not line.startswith("#")
        ]
    return PathSpec.from_lines("gitwildmatch", lines)


def is_ignored(entry: Path, spec: PathSpec) -> bool:
    rel = entry.relative_to(ROOT)
    rel_str = str(rel).replace("\\", "/")

    if entry.is_dir():
        if spec.match_file(rel_str + "/"):
            return True
    if spec.match_file(rel_str):
        return True

    parts = set(rel.parts)
    return bool(parts & ALWAYS_IGNORE)


def scan_dir(base: Path, spec: PathSpec, prefix: str = "") -> list[str]:
    lines = []
    entries = sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    for entry in entries:
        if is_ignored(entry, spec):
            continue
        marker = "📁" if entry.is_dir() else "📄"
        lines.append(f"{prefix}{marker} {entry.name}")
        if entry.is_dir():
            lines.extend(scan_dir(entry, spec, prefix + "    "))
    return lines


def safe_read(path: Path) -> str:
    if not path.exists():
        return f"[Файл отсутствует: {path.name}]"
    try:
        return path.read_text(encoding="utf-8")
    except Exception as e:
        return f"[Ошибка чтения {path.name}: {e}]"


def load_pyproject_meta() -> dict:
    if not PYPROJECT.exists():
        return {"error": "pyproject.toml not found"}
    try:
        with open(PYPROJECT, "rb") as f:
            data = tomllib.load(f)
        project = data.get("project", {})
        return {
            "name": project.get("name", ""),
            "version": project.get("version", ""),
            "description": project.get("description", ""),
            "requires_python": project.get("requires-python", ""),
            "readme": project.get("readme", ""),
        }
    except Exception as e:
        return {"error": f"Ошибка чтения pyproject.toml: {e}"}


def list_package_modules() -> list[str]:
    if not PACKAGE_DIR.exists():
        return []
    return sorted(
        str(p.relative_to(ROOT)).replace("\\", "/")
        for p in PACKAGE_DIR.glob("*.py")
    )


def translator_summary() -> list[str]:
    lines = []
    if not TRANSLATOR_FILE.exists():
        return ["translator.py not found"]

    text = safe_read(TRANSLATOR_FILE)

    checks = {
        "SimpleHTMLTranslator class": "class SimpleHTMLTranslator" in text,
        "Translator class": "class Translator" in text,
        "HTMLParser used": "HTMLParser" in text,
        "OpenAI client used": "OpenAI" in text,
        "Cache JSON save": "json.dump" in text,
        "translate_html method": "def translate_html" in text,
        "translate_text method": "def translate_text" in text,
        "detect_browser_lang method": "def detect_browser_lang" in text,
        "get_alternative_lang method": "def get_alternative_lang" in text,
        "script/style skip": '("script", "style")' in text,
        "langSwitch skip": 'value == "langSwitch"' in text,
        "long text chunking": "if len(text) > 2000" in text,
        "stale cache sync": 'self._cache.pop(old_key)' in text,
    }

    for name, present in checks.items():
        lines.append(f"  - {name}: {'yes' if present else 'no'}")

    return lines


# ───────────────────────────────────────────────────────────────
# Основная функция
# ───────────────────────────────────────────────────────────────
def main():
    print(f"[INFO] Сканирование библиотеки: {ROOT}")

    spec = load_gitignore(GITIGNORE)
    structure_lines = scan_dir(ROOT, spec)
    meta = load_pyproject_meta()
    modules = list_package_modules()

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    result = []

    result.append("=== STRUCTURE ===")
    result.extend(structure_lines)
    result.append("")

    result.append("=== PACKAGE_METADATA ===")
    if "error" in meta:
        result.append(meta["error"])
    else:
        result.append(f"name: {meta['name']}")
        result.append(f"version: {meta['version']}")
        result.append(f"description: {meta['description']}")
        result.append(f"requires_python: {meta['requires_python']}")
        result.append(f"readme: {meta['readme']}")
    result.append("")

    result.append("=== PACKAGE_MODULES ===")
    if modules:
        for module in modules:
            result.append(f"  - {module}")
    else:
        result.append("[Модули пакета не найдены]")
    result.append("")

    result.append("=== README_EXISTS ===")
    result.append("yes" if README.exists() else "no")
    result.append("")

    result.append("=== TRANSLATOR_OVERVIEW ===")
    result.extend(translator_summary())
    result.append("")

    result.append("=== WORKING_CONDITIONS ===")
    result.append(WORKING_CONDITIONS.strip())
    result.append("")

    OUTPUT.write_text("\n".join(result), encoding="utf-8")
    print(f"[OK] Отчёт создан: {OUTPUT}")


if __name__ == "__main__":
    main()