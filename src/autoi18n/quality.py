# src/autoi18n/quality.py
"""
Проверка ответа модели ПЕРЕД записью в кэш.

Зачем: раньше любой ответ модели сразу попадал в файл перевода и на
экран — в том числе рассуждение вместо перевода ('The translation of
"X" ... is "Y."'), потерянные {{0}}, буквы исходного алфавита внутри
перевода, лишняя скобка в конце. Исправить такое потом
можно было только руками по хешу.

Две функции:
- check_phrase()   — фраза интерфейса;
- check_markdown() — кусок .md-документа (структура разметки + язык).

Каждая возвращает список проблем (пустой = перевод годится). Проблемы
делятся на жёсткие (перевод не записывается) и мягкие (глоссарий —
после повторной попытки перевод всё же принимается, см. ai_translator).
"""
import re
from typing import List, Optional

PLACEHOLDER_RE = re.compile(r"\{\{\d+\}\}")
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
LETTER_RE = re.compile(r"[^\W\d_]")
EXPLAIN_RE = re.compile(r"(?i)\b(translat\w*|перев[оеё]д\w*|tərcümə\w*)\b")
# если в исходнике само слово "перевод/перевести/translate" — это легальная тема фразы
EXPLAIN_SRC_RE = re.compile(r"(?i)(перев|translat|tərcüm)")
CYRILLIC_LANGS = {"ru", "uk", "be", "bg", "sr", "mk", "kk", "ky", "tg", "mn"}
BRACKETS = (("(", ")"), ("[", "]"), ("{", "}"))

GLOSSARY_PREFIX = "термин "


def _base(lang: str) -> str:
    return (lang or "").lower().split("-")[0]


def is_soft(problem: str) -> bool:
    return problem.startswith(GLOSSARY_PREFIX)


def check_phrase(source: str, translated: Optional[str], lang: str, glossary=None) -> List[str]:
    problems: List[str] = []
    if translated is None or not str(translated).strip():
        return ["пустой перевод"]
    src, tr = source.strip(), str(translated).strip()

    if sorted(PLACEHOLDER_RE.findall(src)) != sorted(PLACEHOLDER_RE.findall(tr)):
        problems.append("не совпадают параметры {{n}}")

    if CYRILLIC_RE.search(src) and _base(lang) not in CYRILLIC_LANGS and CYRILLIC_RE.search(tr):
        problems.append("в переводе остались буквы исходного алфавита")

    if EXPLAIN_RE.search(tr) and not EXPLAIN_SRC_RE.search(src):
        problems.append("похоже на объяснение модели, а не перевод")

    for op, cl in BRACKETS:
        if src.count(op) - src.count(cl) != tr.count(op) - tr.count(cl):
            problems.append(f"непарные скобки {op}{cl}")

    if len(tr) > 4 * len(src) + 30:
        problems.append("перевод подозрительно длинный")

    if glossary is not None:
        problems.extend(glossary.violations(src, tr, lang))
    return problems


# ---------- документы (.md) ----------
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s", re.M)
_FENCE_RE = re.compile(r"^\s*(```|~~~)", re.M)
_LIST_RE = re.compile(r"^\s*([-*+]|\d+[.)])\s", re.M)
_TABLE_RE = re.compile(r"^\s*\|", re.M)
_LINK_RE = re.compile(r"\]\(")
_IMAGE_RE = re.compile(r"!\[")
_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")


def _strip_code(text: str) -> str:
    text = re.sub(r"(?ms)^\s*(```|~~~).*?^\s*\1", "", text)
    return re.sub(r"`[^`\n]*`", "", text)


def check_markdown(source: str, translated: Optional[str], lang: str) -> List[str]:
    if translated is None or not translated.strip():
        return ["пустой перевод"]
    problems: List[str] = []
    for name, rx in (
        ("заголовков", _HEADING_RE),
        ("блоков кода", _FENCE_RE),
        ("пунктов списка", _LIST_RE),
        ("строк таблицы", _TABLE_RE),
        ("ссылок", _LINK_RE),
        ("картинок", _IMAGE_RE),
        ("HTML-тегов", _TAG_RE),
    ):
        a, b = len(rx.findall(source)), len(rx.findall(translated))
        if a != b:
            problems.append(f"не совпадает число {name}: {a} -> {b}")

    if sorted(PLACEHOLDER_RE.findall(source)) != sorted(PLACEHOLDER_RE.findall(translated)):
        problems.append("не совпадают параметры {{n}}")

    src_text, tr_text = _strip_code(source), _strip_code(translated)
    if CYRILLIC_RE.search(src_text) and _base(lang) not in CYRILLIC_LANGS:
        letters = len(LETTER_RE.findall(tr_text)) or 1
        if len(CYRILLIC_RE.findall(tr_text)) / letters > 0.03:
            problems.append("в переводе остался текст на исходном языке")

    ratio = len(translated) / max(len(source), 1)
    if ratio < 0.4 or ratio > 3.0:
        problems.append(f"длина перевода подозрительная ({ratio:.1f}x)")
    return problems
