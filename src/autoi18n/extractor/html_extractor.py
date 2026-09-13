# src/autoi18n/extractor/html_extractor.py
"""
Извлечение и применение переводов для HTML-шаблонов (Jinja/Django/FastAPI).

Разбор HTML не меняется — html.parser.HTMLParser (stdlib) уже был настоящим
парсером, а не регуляркой, и работал корректно в v1. Единственное реальное
изменение: содержимое <script> внутри HTML теперь тоже разбирается через
AST (Node/Babel мост), а не через старый regex JS-парсер.
"""
import os
import tempfile
from html import escape
from html.parser import HTMLParser
from typing import Callable, Dict, List, Optional, Tuple

from ..utils import should_translate, split_preserve_whitespace, replace_translatable_strings
from .js_extractor import extract_js_items_from_files


class ParserConfig:
    SKIP_TAGS = {"script", "style", "noscript"}
    TRANSLATABLE_ATTRS = {"placeholder", "title", "alt", "aria-label"}
    BUTTON_VALUE_TYPES = {"button", "submit", "reset"}


class SimpleHTMLTranslator(HTMLParser):
    """Один и тот же парсер используется и для извлечения (callback просто
    собирает текст, возвращая его без изменений), и для применения перевода
    (callback подменяет текст на перевод)."""

    def __init__(self, translate_callback: Callable[[str, Optional[str], Optional[str]], str]):
        super().__init__(convert_charrefs=False)
        self.result: List[str] = []
        self.translate_callback = translate_callback
        self.tag_stack: List[str] = []
        self.skip_depth = 0
        self._script_buffer: List[str] = []
        self._in_script = False
        self._script_contents: List[str] = []

    @property
    def current_tag(self) -> Optional[str]:
        return self.tag_stack[-1] if self.tag_stack else None

    def _should_skip_tag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> bool:
        if tag in ParserConfig.SKIP_TAGS:
            return True
        attrs_dict = {k: v for k, v in attrs}
        if attrs_dict.get("id") == "langSwitch":
            return True
        if attrs_dict.get("translate") == "no":
            return True
        if attrs_dict.get("data-translate") == "no":
            return True
        return False

    def _render_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]], closing: str = ">") -> str:
        rendered_attrs = []
        attrs_dict = {k: v for k, v in attrs}
        input_type = (attrs_dict.get("type") or "").strip().lower()
        for name, value in attrs:
            if value is None:
                rendered_attrs.append(name)
                continue
            new_value = value
            if self.skip_depth == 0:
                if name in ParserConfig.TRANSLATABLE_ATTRS and should_translate(value, attr_name=name):
                    new_value = self.translate_callback(value, tag, name)
                elif (tag == "input" and name == "value"
                      and input_type in ParserConfig.BUTTON_VALUE_TYPES
                      and should_translate(value, attr_name=name)):
                    new_value = self.translate_callback(value, tag, name)
            rendered_attrs.append(f'{name}="{escape(new_value, quote=True)}"')
        if rendered_attrs:
            return f"<{tag} {' '.join(rendered_attrs)}{closing}"
        return f"<{tag}{closing}"

    def handle_starttag(self, tag, attrs):
        skip_this_tag = self._should_skip_tag(tag, attrs)
        self.result.append(self._render_starttag(tag, attrs, closing=">"))
        self.tag_stack.append(tag)
        if skip_this_tag:
            self.skip_depth += 1
        if tag == "script" and self.skip_depth == 1:
            self._in_script = True
            self._script_buffer = []

    def handle_startendtag(self, tag, attrs):
        self.result.append(self._render_starttag(tag, attrs, closing=" />"))

    def handle_endtag(self, tag):
        self.result.append(f"</{tag}>")
        if self.tag_stack:
            self.tag_stack.pop()
        if self.skip_depth > 0:
            self.skip_depth -= 1
        if tag == "script" and self._in_script:
            self._in_script = False
            script_content = "".join(self._script_buffer)
            if script_content.strip():
                self._script_contents.append(script_content)
            self._script_buffer = []

    def handle_data(self, data):
        if self._in_script:
            self._script_buffer.append(data)
        if self.skip_depth > 0:
            self.result.append(data)
            return
        if not should_translate(data):
            self.result.append(data)
            return
        leading, core, trailing = split_preserve_whitespace(data)
        if not core:
            self.result.append(data)
            return
        translated = self.translate_callback(core, self.current_tag, None)
        self.result.append(f"{leading}{translated}{trailing}")

    def handle_entityref(self, name):
        self.result.append(f"&{name};")

    def handle_charref(self, name):
        self.result.append(f"&#{name};")

    def handle_comment(self, data):
        self.result.append(f"<!--{data}-->")

    def handle_decl(self, decl):
        self.result.append(f"<!{decl}>")

    def handle_pi(self, data):
        self.result.append(f"<?{data}>")

    def unknown_decl(self, data):
        self.result.append(f"<![{data}]>")

    def get_html(self) -> str:
        return "".join(self.result)

    def get_script_contents(self) -> List[str]:
        return self._script_contents


def _extract_script_items(script_contents: List[str]) -> List[Dict[str, object]]:
    """
    Прогоняет содержимое каждого <script> через Node/Babel мост.
    Node принимает файлы, а не сырой текст, поэтому каждый скрипт временно
    пишется во врéменный .js — но все скрипты одного вызова extract() уходят
    в Node ОДНИМ батчем, не по одному (важно для скорости на больших проектах).
    """
    if not script_contents:
        return []

    tmp_files = []
    try:
        for content in script_contents:
            fd, path = tempfile.mkstemp(suffix=".js", prefix=".autoi18n_script_")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            tmp_files.append(path)

        results = extract_js_items_from_files(tmp_files)
        items: List[Dict[str, object]] = []
        for path in tmp_files:
            items.extend(results.get(path, []))
        return items
    finally:
        for path in tmp_files:
            try:
                os.remove(path)
            except OSError:
                pass


def collect_translatable_items(html: str) -> List[Dict[str, object]]:
    """
    Извлекает все переводимые фразы из одного HTML-документа: видимый текст,
    переводимые атрибуты и содержимое <script> (через AST). Возвращает
    список {"text": ..., "placeholders": 0, "line": None}, без дублей в
    рамках документа.
    """
    seen = set()
    items: List[Dict[str, object]] = []

    def collector(text: str, tag: Optional[str], attr_name: Optional[str]) -> str:
        core = text.strip()
        if core and core not in seen:
            seen.add(core)
            items.append({"text": core, "placeholders": 0, "line": None})
        return text

    parser = SimpleHTMLTranslator(translate_callback=collector)
    parser.feed(html)
    parser.close()

    for item in _extract_script_items(parser.get_script_contents()):
        text = item["text"]
        if text not in seen:
            seen.add(text)
            items.append(item)

    return items


def extract_html_keys_from_files(file_paths: List[str]) -> Dict[str, List[Dict[str, object]]]:
    """Аналог extract_js_items_from_files, но для HTML-файлов."""
    result: Dict[str, List[Dict[str, object]]] = {}
    for path in sorted(file_paths):
        try:
            with open(path, "r", encoding="utf-8") as f:
                html = f.read()
        except Exception:
            continue
        result[path] = collect_translatable_items(html)
    return result


def apply_translations_to_html(html: str, translations: Dict[str, str]) -> str:
    """
    Применяет перевод к готовому HTML: видимый текст и переводимые атрибуты
    заменяются на лету при повторном проходе парсера; содержимое <script>
    заменяется отдельно по той же карте {оригинал: перевод} (в т.ч. внутри
    шаблонных строк с ${...} — см. utils.replace_translatable_strings).

    translations — плоский словарь {оригинальный_текст: перевод}, БЕЗ хешей:
    хеш используется только как ключ хранения в файлах кэша, а на этапе
    применения перевода всегда работаем по исходному тексту.
    """
    def callback(text: str, tag: Optional[str], attr_name: Optional[str]) -> str:
        core = text.strip()
        return translations.get(core, text) if core in translations else text

    parser = SimpleHTMLTranslator(translate_callback=callback)
    parser.feed(html)
    parser.close()
    result_html = parser.get_html()

    if parser.get_script_contents() and translations:
        result_html = replace_translatable_strings(result_html, translations)

    return result_html
