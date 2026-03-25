# src/autoi18n/translator.py
import json
import os
import re
from html import escape
from html.parser import HTMLParser
from typing import Callable, Dict, List, Optional, Tuple

from openai import OpenAI


SKIP_TAGS = {"script", "style", "noscript"}
TRANSLATABLE_ATTRS = {"placeholder", "title", "alt", "aria-label"}
BUTTON_VALUE_TYPES = {"button", "submit", "reset"}

WHITESPACE_ONLY_RE = re.compile(r"^\s*$")
NUMBER_LIKE_RE = re.compile(r"^[\d\s\.,:/\-]+$")
HEX_LIKE_RE = re.compile(r"^[A-Fa-f0-9\-]{8,}$")
PURE_LATIN_TECH_RE = re.compile(r"^[A-Za-z0-9_\-\s\.:/@#%+=]+$")


def _split_preserve_whitespace(text: str) -> Tuple[str, str, str]:
    match = re.match(r"^(\s*)(.*?)(\s*)$", text, flags=re.DOTALL)
    if not match:
        return "", text, ""
    return match.group(1), match.group(2), match.group(3)


def should_translate(text: str, tag: Optional[str] = None, attr_name: Optional[str] = None) -> bool:
    """
    Определяем, нужно ли переводить значение.
    Не режем таблицы/формы — наоборот, теперь они поддерживаются.
    """
    if text is None:
        return False

    raw = text
    text = text.strip()

    if not text:
        return False

    if WHITESPACE_ONLY_RE.fullmatch(raw):
        return False

    # Числа и почти-числа
    if text.isdigit():
        return False
    if NUMBER_LIKE_RE.fullmatch(text):
        return False

    # UUID / hash-like
    if HEX_LIKE_RE.fullmatch(text):
        return False

    # Тех. строки на латинице: коды, логины, пути, идентификаторы
    if PURE_LATIN_TECH_RE.fullmatch(text):
        # Но некоторые атрибуты UI на латинице надо переводить
        if attr_name in TRANSLATABLE_ATTRS or attr_name == "value":
            # переводим только если там есть хотя бы одна буква и это похоже на UI-текст
            words = text.split()
            if len(words) >= 2:
                return True
        return False

    return True


def _safe_json_load(path: str) -> Dict[str, str]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _safe_json_save(path: str, data: Dict[str, str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)


def _extract_json(text: str):
    text = text.strip()
    if not text:
        raise ValueError("Empty model response")

    # сначала пробуем как есть
    try:
        return json.loads(text)
    except Exception:
        pass

    # потом ищем первый JSON-блок
    start_obj = text.find("{")
    end_obj = text.rfind("}")
    if start_obj != -1 and end_obj != -1 and end_obj > start_obj:
        candidate = text[start_obj:end_obj + 1]
        try:
            return json.loads(candidate)
        except Exception:
            pass

    start_arr = text.find("[")
    end_arr = text.rfind("]")
    if start_arr != -1 and end_arr != -1 and end_arr > start_arr:
        candidate = text[start_arr:end_arr + 1]
        return json.loads(candidate)

    raise ValueError("JSON not found in model response")


class SimpleHTMLTranslator(HTMLParser):
    """
    HTML parser:
    - сохраняет структуру HTML;
    - собирает переводимые тексты и атрибуты;
    - затем подставляет переводы без ломания верстки.
    """

    def __init__(self, translate_callback: Callable[[str, Optional[str], Optional[str]], str]):
        super().__init__(convert_charrefs=False)
        self.result: List[str] = []
        self.translate_callback = translate_callback
        self.tag_stack: List[str] = []
        self.skip_depth = 0

    @property
    def current_tag(self) -> Optional[str]:
        return self.tag_stack[-1] if self.tag_stack else None

    def _should_skip_tag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> bool:
        if tag in SKIP_TAGS:
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
                if name in TRANSLATABLE_ATTRS and should_translate(value, tag=tag, attr_name=name):
                    new_value = self.translate_callback(value, tag, name)

                elif (
                    tag == "input"
                    and name == "value"
                    and input_type in BUTTON_VALUE_TYPES
                    and should_translate(value, tag=tag, attr_name=name)
                ):
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

    def handle_startendtag(self, tag, attrs):
        # self-closing tag
        self.result.append(self._render_starttag(tag, attrs, closing=" />"))

    def handle_endtag(self, tag):
        self.result.append(f"</{tag}>")
        if self.tag_stack:
            popped = self.tag_stack.pop()
            if popped == tag and self.skip_depth > 0:
                # если закрываем skip-tag, выходим из режима skip
                # это корректно, т.к. skip включается только на starttag
                attrs_skip_tags = SKIP_TAGS.union(set())
                if tag in attrs_skip_tags or tag in {"button", "div", "span", "a", "section", "p", "label"}:
                    # уменьшаем только если реально были в skip
                    self.skip_depth -= 1
                    if self.skip_depth < 0:
                        self.skip_depth = 0

    def handle_data(self, data):
        if self.skip_depth > 0:
            self.result.append(data)
            return

        if not should_translate(data, tag=self.current_tag):
            self.result.append(data)
            return

        leading, core, trailing = _split_preserve_whitespace(data)
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


class Translator:
    """
    Основной класс переводчика.

    Совместимость:
    - старый вызов Translator(cache_dir="./translations", api_key=...)
      продолжает работать;
    - поддержан source_lang как в README;
    - старые page.lang.json автоматически подхватываются и вливаются в новый кэш lang.json.
    """

    def __init__(
        self,
        cache_dir: str = "./translations",
        api_key: Optional[str] = None,
        source_lang: Optional[str] = None,
        model: str = "gpt-4o-mini",
    ):
        self.source_lang = source_lang or os.getenv("SOURCE_LANG", "ru")
        self.cache_dir = cache_dir
        self.client = OpenAI(api_key=api_key)
        self.model = model

        self._current_lang: Optional[str] = None
        self._current_page_name: Optional[str] = None
        self._current_file: Optional[str] = None
        self._cache: Dict[str, str] = {}

    def _legacy_file_path(self, page_name: str, lang: str) -> str:
        filename = f"{page_name}.{lang}.json"
        return os.path.join(self.cache_dir, filename)

    def _file_path(self, lang: str) -> str:
        filename = f"{lang}.json"
        return os.path.join(self.cache_dir, filename)

    def _load_storage(self, page_name: str, lang: str) -> None:
        target_path = self._file_path(lang)
        cache = _safe_json_load(target_path)

        # legacy migration: page.lang.json -> lang.json
        legacy_path = self._legacy_file_path(page_name, lang)
        legacy_cache = _safe_json_load(legacy_path)

        if legacy_cache:
            cache.update({k: v for k, v in legacy_cache.items() if k not in cache})
            _safe_json_save(target_path, cache)

        self._cache = cache
        self._current_file = target_path
        self._current_lang = lang
        self._current_page_name = page_name

    def _ensure_storage(self, page_name: str, lang: str) -> None:
        if self._current_lang != lang or not self._current_file:
            self._load_storage(page_name, lang)

    def _save_storage(self) -> None:
        if self._current_file:
            _safe_json_save(self._current_file, self._cache)

    def _build_single_prompt(self, text: str, target_lang: str, prompt_type: str = "normal") -> str:
        if prompt_type == "button":
            return (
                f"Translate the UI button label from {self.source_lang} to {target_lang}. "
                f"Return only the translation, without explanations.\n\n{text}"
            )

        if prompt_type == "attr":
            return (
                f"Translate the UI attribute text from {self.source_lang} to {target_lang}. "
                f"Return only the translation, without explanations.\n\n{text}"
            )

        return (
            f"Translate the text from {self.source_lang} to {target_lang}. "
            f"Return only the translation, without explanations.\n\n{text}"
        )

    def _translate_via_api(self, prompt: str) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
        )
        return (response.choices[0].message.content or "").strip()

    def _translate_single(self, text: str, target_lang: str, prompt_type: str = "normal") -> str:
        prompt = self._build_single_prompt(text, target_lang, prompt_type)
        translated = self._translate_via_api(prompt)
        return translated or text

    def _translate_batch(self, items: List[Tuple[str, str]], target_lang: str) -> Dict[str, str]:
        """
        items: [(text, prompt_type), ...]
        Возвращает dict[source_text] = translated_text
        """
        if not items:
            return {}

        payload = [
            {"id": i, "text": text, "kind": prompt_type}
            for i, (text, prompt_type) in enumerate(items, start=1)
        ]

        prompt = (
            f"Translate each item from {self.source_lang} to {target_lang}.\n"
            f"Rules:\n"
            f"- Return STRICT JSON object only.\n"
            f"- Format: {{\"items\": [{{\"id\": 1, \"translated\": \"...\"}}]}}\n"
            f"- Keep meaning precise.\n"
            f"- For buttons and UI labels keep text concise.\n"
            f"- Do not omit any item.\n"
            f"- Do not add comments.\n\n"
            f"Input JSON:\n{json.dumps(payload, ensure_ascii=False)}"
        )

        raw = self._translate_via_api(prompt)
        parsed = _extract_json(raw)

        result: Dict[str, str] = {}
        translated_items = parsed.get("items", []) if isinstance(parsed, dict) else []

        by_id = {}
        for item in translated_items:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            translated = item.get("translated")
            if isinstance(item_id, int) and isinstance(translated, str):
                by_id[item_id] = translated.strip()

        for idx, (text, _) in enumerate(items, start=1):
            result[text] = by_id.get(idx) or text

        return result

    def translate_text(
        self,
        text: str,
        target_lang: str,
        page_name: str = "page",
        prompt_type: str = "normal",
    ) -> str:
        if text is None:
            return text

        original = text
        leading, core, trailing = _split_preserve_whitespace(original)

        if not core:
            return original

        if target_lang == self.source_lang:
            return original

        self._ensure_storage(page_name, target_lang)

        if not should_translate(core):
            return original

        if core in self._cache:
            return f"{leading}{self._cache[core]}{trailing}"

        translated = self._translate_single(core, target_lang, prompt_type=prompt_type)
        self._cache[core] = translated
        self._save_storage()
        return f"{leading}{translated}{trailing}"

    def translate_html(self, html: str, target_lang: str, page_name: str = "page") -> str:
        if target_lang == self.source_lang:
            return html

        self._ensure_storage(page_name, target_lang)

        collected: List[Tuple[str, str]] = []
        seen = set()

        def collector(text: str, tag: Optional[str], attr_name: Optional[str]) -> str:
            prompt_type = "normal"
            if tag == "button" or attr_name == "value":
                prompt_type = "button"
            elif attr_name is not None:
                prompt_type = "attr"

            if text in self._cache:
                return self._cache[text]

            key = (text, prompt_type)
            if key not in seen:
                seen.add(key)
                collected.append(key)

            return text

        # проход 1: собрать новые строки
        pre_parser = SimpleHTMLTranslator(translate_callback=collector)
        pre_parser.feed(html)
        pre_parser.close()

        if collected:
            # длинные строки страхуем одиночным запросом
            short_items = []
            long_items = []

            for text, prompt_type in collected:
                if len(text) > 3000:
                    long_items.append((text, prompt_type))
                else:
                    short_items.append((text, prompt_type))

            if short_items:
                batch_result = self._translate_batch(short_items, target_lang)
                self._cache.update(batch_result)

            for text, prompt_type in long_items:
                self._cache[text] = self._translate_single(text, target_lang, prompt_type=prompt_type)

            self._save_storage()

        # проход 2: подставить переводы
        def renderer(text: str, tag: Optional[str], attr_name: Optional[str]) -> str:
            if text in self._cache:
                return self._cache[text]
            return text

        parser = SimpleHTMLTranslator(translate_callback=renderer)
        parser.feed(html)
        parser.close()
        return parser.get_html()

    def detect_browser_lang(self, accept_language: str) -> str:
        if not accept_language:
            return self.source_lang
        return accept_language.split(",")[0].split("-")[0].strip().lower()

    def get_alternative_lang(self, current_lang: str, browser_lang: str) -> str:
        current_lang = (current_lang or "").strip().lower()
        browser_lang = (browser_lang or "").strip().lower()

        if not browser_lang:
            return "en" if current_lang == self.source_lang else self.source_lang

        if current_lang == browser_lang:
            return "en" if current_lang != "en" else self.source_lang

        return browser_lang