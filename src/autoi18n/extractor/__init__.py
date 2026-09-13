# src/autoi18n/extractor/__init__.py
"""Экстракторы переводимых фраз: файловый скан + HTML-парсер + Node/Babel AST-мост для JS/JSX/TSX."""

from .file_scanner import resolve_files, resolve_scan_paths
from .js_extractor import NodeNotFoundError, extract_js_items_from_files
from .html_extractor import (
    apply_translations_to_html,
    collect_translatable_items,
    extract_html_keys_from_files,
)

__all__ = [
    "resolve_files",
    "resolve_scan_paths",
    "extract_js_items_from_files",
    "NodeNotFoundError",
    "collect_translatable_items",
    "extract_html_keys_from_files",
    "apply_translations_to_html",
]
