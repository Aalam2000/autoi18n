# src/autoi18n/parsers/html_parser.py
import re
import logging
from html import escape
from html.parser import HTMLParser
from typing import Callable, Dict, List, Optional, Tuple

from ..utils import (
    should_translate,
    split_preserve_whitespace,
    text_hash,
    load_keys_mapping,
    save_keys_mapping,
)

# ---------- Конфигурация парсера (может быть переопределена извне) ----------
class ParserConfig:
    SKIP_TAGS = {"script", "style", "noscript"}
    TRANSLATABLE_ATTRS = {"placeholder", "title", "alt", "aria-label"}
    BUTTON_VALUE_TYPES = {"button", "submit", "reset"}

    @classmethod
    def update_from_env(cls):
        """Обновить настройки из переменных окружения (пример)"""
        import os
        # Можно добавить парсинг JSON-списков, если потребуется
        pass


# ---------- Логирование (отключаемое) ----------
logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())  # по умолчанию ничего не выводит
# Включить через: logger.setLevel(logging.DEBUG) или переменную окружения


def resolve_prompt_type(tag: Optional[str], attr_name: Optional[str]) -> str:
    if tag == "button" or attr_name == "value":
        return "button"
    if attr_name is not None:
        return "attr"
    return "normal"


class SimpleHTMLTranslator(HTMLParser):
    def __init__(self, translate_callback: Callable[[str, Optional[str], Optional[str]], str]):
        super().__init__(convert_charrefs=False)
        self.result: List[str] = []
        self.translate_callback = translate_callback
        self.tag_stack: List[str] = []
        self.skip_depth = 0
        self._script_buffer: List[str] = []
        self._in_script = False
        self._script_contents: List[str] = []   # накопленное содержимое скриптов

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

        # ---------- ИСПРАВЛЕНИЕ БАГА ----------
        # Проверяем, что это тег script и он действительно пропущен (skip_depth == 1)
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


# ---------- Основные функции для внешнего использования ----------
def collect_translatable_items(html: str, cache_dir: str) -> List[Dict[str, str]]:
    """
    Собирает все уникальные тексты из HTML (текст + атрибуты).
    Возвращает список словарей {text, hash}.
    """
    mapping = load_keys_mapping(cache_dir)
    result: Dict[str, str] = {}  # hash -> text
    seen_texts = set()

    def collector(text: str, tag: Optional[str], attr_name: Optional[str]) -> str:
        if not text:
            return text
        core = text.strip()
        if not core or not should_translate(core):
            return text
        if core in seen_texts:
            return text
        seen_texts.add(core)
        h = text_hash(core)
        if h not in mapping:
            mapping[h] = core
        result[h] = core
        return text

    parser = SimpleHTMLTranslator(translate_callback=collector)
    parser.feed(html)
    parser.close()
    save_keys_mapping(cache_dir, mapping)
    return [{"text": result[h], "hash": h} for h in sorted(result.keys())]

def extract_html_keys_from_files(file_paths: List[str]) -> List[Dict[str, str]]:
    """
    Извлекает переводимые ключи из HTML-файлов:
      - видимый текст и атрибуты через SimpleHTMLTranslator;
      - содержимое <script> через JS-парсер.
    Возвращает список {"key": ..., "text": ...}.
    """
    from .js_parser import extract_js_keys_from_content

    result: Dict[str, str] = {}
    for path in sorted(file_paths):
        try:
            with open(path, "r", encoding="utf-8") as f:
                html = f.read()
        except Exception as e:
            logger.debug(f"Error reading {path}: {e}")
            continue

        # 1. HTML: текст и атрибуты (collector собирает пары hash -> text)
        def _collector(text, tag, attr_name):
            return text

        parser = SimpleHTMLTranslator(translate_callback=_collector)
        parser.feed(html)
        parser.close()

        # 2. <script>: JS-ключи
        for script in parser.get_script_contents():
            for item in extract_js_keys_from_content(script):
                key = item["key"]
                if key not in result:
                    result[key] = item["text"]

        # 3. Видимый HTML-текст (вне script/style): собираем через отдельный прогон
        for tag in ("h1", "h2", "h3", "h4", "h5", "h6", "p", "button",
                    "span", "a", "div", "label", "option", "title", "footer"):
            pattern = re.compile(rf"<{tag}[^>]*>([^<>]+)</{tag}>", re.IGNORECASE | re.DOTALL)
            for match in pattern.findall(html):
                clean = match.strip()
                if not clean or not should_translate(clean):
                    continue
                h = text_hash(clean)
                if h not in result:
                    result[h] = clean

    return [{"key": k, "text": v} for k, v in result.items()]
