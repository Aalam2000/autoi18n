# src/autoi18n/parsers/__init__.py
"""Пакет парсеров для извлечения переводимых строк из HTML и JavaScript."""

from .html_parser import (
    SimpleHTMLTranslator,
    collect_translatable_items,
    resolve_prompt_type,
    ParserConfig,
    extract_html_keys_from_files,
)
from .js_parser import (
    extract_js_keys_from_content,
    extract_js_keys_from_files,
    JSParserConfig,
)

__all__ = [
    "SimpleHTMLTranslator",
    "collect_translatable_items",
    "resolve_prompt_type",
    "ParserConfig",
    "extract_js_keys_from_content",
    "extract_js_keys_from_files",
    "extract_html_keys_from_files",
    "JSParserConfig",
]