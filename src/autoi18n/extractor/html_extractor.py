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

    def __init__(
        self,
        translate_callback: Callable[[str, Optional[str], Optional[str]], str],
        script_translate: Optional[Callable[[str], str]] = None,
    ):
        super().__init__(convert_charrefs=False)
        self.result: List[str] = []
        self.translate_callback = translate_callback
        # Применяется к содержимому КАЖДОГО <script> ПО ОТДЕЛЬНОСТИ, в момент
        # его закрытия (см. handle_endtag) — не как один общий regex-проход
        # по уже собранному документу целиком (см. handle_data/handle_endtag
        # про то, почему это важно).
        self.script_translate = script_translate
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
        # Контент <script> закрываем и вставляем в result ЗДЕСЬ, одним
        # куском, ДО закрывающего тега — см. handle_data про то, почему он
        # больше не льётся в result по мере парсинга.
        if tag == "script" and self._in_script:
            self._in_script = False
            script_content = "".join(self._script_buffer)
            if script_content.strip():
                self._script_contents.append(script_content)
            rendered = self.script_translate(script_content) if self.script_translate else script_content
            self.result.append(rendered)
            self._script_buffer = []

        self.result.append(f"</{tag}>")
        if self.tag_stack:
            self.tag_stack.pop()
        if self.skip_depth > 0:
            self.skip_depth -= 1

    def handle_data(self, data):
        if self._in_script:
            # Раньше сырой текст скрипта одновременно буферизовался И сразу
            # лился в result по мере парсинга — а перевод содержимого script
            # делался ПОСЛЕ, отдельным regex-проходом по уже склеенному
            # документу целиком (replace_translatable_strings). Проблема:
            # если перевод обычного видимого текста ДО этого script вносит
            # непарную кавычку/апостроф (например "Время вышло!" -> "Time's
            # up!" — новый апостроф, которого не было в оригинале), общий
            # regex парности кавычек по всему документу сбивается, и всё,
            # что физически идёт в файле ПОСЛЕ такого места, перестаёт
            # находить свои кавычки правильно — script выше по файлу мог
            # перевестись, а ниже — молча нет. Поэтому теперь: content
            # только буферизуется тут, а переводится и вставляется в result
            # ОДНИМ КУСКОМ в handle_endtag — независимо от остального
            # документа, без общего прохода по кавычкам всего файла.
            self._script_buffer.append(data)
            return
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
    заменяются на лету при проходе парсера; содержимое КАЖДОГО <script>
    переводится ОТДЕЛЬНО, само по себе (см. utils.replace_translatable_strings
    — в т.ч. внутри шаблонных строк с ${...}), в момент закрытия именно
    этого тега — а не одним общим regex-проходом по всему уже склеенному
    документу. Раньше именно общий проход был багом: перевод обычного
    текста ДО script мог внести непарную кавычку/апостроф (например
    "Время вышло!" -> "Time's up!"), из-за чего парность кавычек по всему
    документу сбивалась и всё, что физически ниже в файле, переставало
    находить свои кавычки — часть скриптов переводилась, часть молча нет.

    translations — плоский словарь {оригинальный_текст: перевод}, БЕЗ хешей:
    хеш используется только как ключ хранения в файлах кэша, а на этапе
    применения перевода всегда работаем по исходному тексту.
    """
    def callback(text: str, tag: Optional[str], attr_name: Optional[str]) -> str:
        core = text.strip()
        return translations.get(core, text) if core in translations else text

    def script_translate(script_content: str) -> str:
        if not translations:
            return script_content
        return replace_translatable_strings(script_content, translations)

    parser = SimpleHTMLTranslator(translate_callback=callback, script_translate=script_translate)
    parser.feed(html)
    parser.close()
    return parser.get_html()
