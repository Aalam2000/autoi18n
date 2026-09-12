import logging
import re
from typing import Dict, List, Callable

from ..utils import should_translate_js_text, text_hash

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class JSParserConfig:
    # t('key', 'text') и t("key", "text")
    CALL_PLAIN_RE = re.compile(
        r"""(?:\bt|translateKey)\s*\(\s*['"]([^'"]+)['"]\s*,\s*['"]([^'"]*)['"]""",
    )
    # t('key', `text ${var} more`)
    CALL_TEMPLATE_RE = re.compile(
        r"""(?:\bt|translateKey)\s*\(\s*['"]([^'"]+)['"]\s*,\s*`([^`]*)`""",
        re.DOTALL,
    )
    # Текстовый узел JSX: >Какой-то текст<
    JSX_TEXT_RE = re.compile(r">([^<>{}\n]+)<")
    # Атрибуты JSX, которые видит пользователь
    JSX_ATTR_RE = re.compile(
        r'\b(label|placeholder|title|aria-label)\s*=\s*["\']([^"\']+)["\']'
    )

    _extra_handlers: List[Callable[[str], List[Dict[str, str]]]] = []

    @classmethod
    def add_handler(cls, handler):
        cls._extra_handlers.append(handler)


def _convert_template_placeholders(text: str) -> str:
    """${expr} → {{N}}"""
    parts = re.split(r"(\$\{[^}]*\})", text)
    out, idx = [], 0
    for part in parts:
        if part.startswith("${"):
            out.append(f"{{{{{idx}}}}}")
            idx += 1
        else:
            out.append(part)
    return "".join(out)


def _unescape(text: str) -> str:
    return (
        text.replace("\\'", "'")
            .replace('\\"', '"')
            .replace("\\`", "`")
            .replace("\\n", "\n")
            .replace("\\t", "\t")
    )


def extract_js_keys_from_content(content: str) -> List[Dict[str, str]]:
    result: Dict[str, str] = {}

    # 1. t('key', 'текст')
    for key, raw in JSParserConfig.CALL_PLAIN_RE.findall(content):
        text = _unescape(raw).strip()
        if not should_translate_js_text(text):
            continue
        result.setdefault(key, text)

    # 2. t('key', `шаблон ${var}`)
    for key, raw in JSParserConfig.CALL_TEMPLATE_RE.findall(content):
        text = _unescape(_convert_template_placeholders(raw)).strip()
        if not should_translate_js_text(text):
            continue
        result.setdefault(key, text)

    # 3. Текстовые узлы JSX
    for raw in JSParserConfig.JSX_TEXT_RE.findall(content):
        text = raw.strip()
        if not should_translate_js_text(text):
            continue
        result.setdefault("text_" + text_hash(text)[:8], text)

    # 4. JSX-атрибуты
    for attr, raw in JSParserConfig.JSX_ATTR_RE.findall(content):
        text = raw.strip()
        if not should_translate_js_text(text):
            continue
        result.setdefault(f"attr_{attr}_{text_hash(text)[:8]}", text)

    # 5. Пользовательские обработчики
    for handler in JSParserConfig._extra_handlers:
        try:
            for item in handler(content):
                if isinstance(item, dict) and item.get("key") and item.get("text"):
                    k, t = item["key"], item["text"]
                    if should_translate_js_text(t):
                        result.setdefault(k, t)
        except Exception as e:
            logger.debug(f"Extra handler error: {e}")

    return [{"key": k, "text": v} for k, v in result.items()]


def extract_js_keys_from_files(file_paths: List[str]) -> List[Dict[str, str]]:
    all_items: Dict[str, Dict[str, str]] = {}
    for path in sorted(file_paths):
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            logger.debug(f"Error reading {path}: {e}")
            continue
        for item in extract_js_keys_from_content(content):
            all_items.setdefault(item["key"], item)
    return list(all_items.values())