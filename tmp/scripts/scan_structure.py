from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "tmp" / "reports" / "structure.txt"
IGNORE = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build", "tmp", ".idea", ".vscode"}

def scan(dir, prefix=""):
    lines = []
    for p in sorted(dir.iterdir()):
        if p.name in IGNORE or p.name.startswith("."):
            continue
        lines.append(f"{prefix}{'📁' if p.is_dir() else '📄'} {p.name}")
        if p.is_dir():
            lines.extend(scan(p, prefix + "  "))
    return lines

OUTPUT.parent.mkdir(parents=True, exist_ok=True)
OUTPUT.write_text("\n".join(scan(ROOT)), encoding="utf-8")
print(f"Структура сохранена в {OUTPUT}")