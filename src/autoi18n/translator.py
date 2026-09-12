# src/autoi18n/translator.py
import os
import threading
import logging
from typing import Any, Dict, List, Optional, Tuple

from .storage import Storage
from .worker import Worker
from .page_registry import PageRegistry
from .ai_translator import get_translator, BaseTranslator

# Обновлённые импорты из парсеров
from .parsers.html_parser import (
    SimpleHTMLTranslator,
    collect_translatable_items,
    extract_html_keys_from_files,
)
from .parsers.js_parser import extract_js_keys_from_content, extract_js_keys_from_files

from .runtime import build_frontend_runtime_script
from .utils import (
    build_lang_chain,
    normalize_lang,
    normalize_backend_dict_name,
    parse_target_langs,
    parse_bool,
    parse_json_or_csv_list,
    parse_scan_paths,
    DEFAULT_SCAN_PATHS,
    resolve_glob_paths,
    should_translate,
    should_translate_ui_text,
    should_translate_backend_text,
    split_preserve_whitespace,
    text_hash,
    load_keys_mapping,
    save_keys_mapping,
    deep_copy_json_like,
    replace_translatable_strings,
)

# ---------- Логирование (отключаемое) ----------
logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class Translator:
    def __init__(
        self,
        cache_dir: str = "./translations",
        api_key: Optional[str] = None,
        source_lang: Optional[str] = None,
        model: str = "gpt-4o-mini",
        target_langs: Optional[List[str]] = None,
        ai_provider: str = "openai",
        ai_config: Optional[Dict[str, Any]] = None,
        scan_paths: Optional[List[Dict[str, str]]] = None,
    ):
        """
        Инициализация основного класса перевода.

        Args:
            cache_dir: Директория для хранения кэша переводов.
            api_key: API ключ для OpenAI (если не указан, берётся из OPENAI_API_KEY).
            source_lang: Исходный язык (по умолчанию 'ru').
            model: Модель OpenAI (по умолчанию 'gpt-4o-mini').
            target_langs: Список целевых языков.
            ai_provider: Провайдер AI (пока только 'openai').
            ai_config: Дополнительная конфигурация для провайдера (например, max_retries, retry_delay).
        """
        self.source_lang = normalize_lang(source_lang or os.getenv("SOURCE_LANG", "ru"))
        self.cache_dir = cache_dir
        self.target_langs = parse_target_langs(target_langs, source_lang=self.source_lang)
        # Приоритет: аргумент > env > DEFAULT_SCAN_PATHS
        if scan_paths:
            self.scan_paths = parse_scan_paths(scan_paths)
        else:
            env_paths = parse_scan_paths(os.getenv("AUTO_I18N_SCAN_PATHS"))
            self.scan_paths = env_paths if env_paths else list(DEFAULT_SCAN_PATHS)
        self.dynamic_dom_enabled = parse_bool(os.getenv("AUTO_I18N_DYNAMIC_DOM_ENABLED"), default=False)
        self.fallback_lang = self._normalize_lang(os.getenv("AUTO_I18N_FALLBACK_LANG", self.source_lang))

        self._storage = Storage(cache_dir, self.source_lang)

        # Создаём AI-переводчик через фабрику
        ai_provider = ai_provider or os.getenv("AUTO_I18N_AI_PROVIDER", "openai")
        ai_config = ai_config or {}
        # Если не передан api_key в ai_config, используем из параметров или переменной окружения
        if "api_key" not in ai_config:
            ai_config["api_key"] = api_key or os.getenv("OPENAI_API_KEY")
        if "model" not in ai_config:
            ai_config["model"] = model
        if "source_lang" not in ai_config:
            ai_config["source_lang"] = self.source_lang
        if "target_langs" not in ai_config:
            ai_config["target_langs"] = self.target_langs

        self.translator: BaseTranslator = get_translator(ai_provider, ai_config)

        # Создаём воркер с переводчиком
        self._worker = Worker(self._storage, self.translator, self.source_lang)
        self._page_registry = PageRegistry()
        self._lock = threading.RLock()

    def _normalize_lang(self, lang: Optional[str]) -> str:
        return normalize_lang(lang, source_lang=self.source_lang)

    def _get_lang_chain(self, target_lang: str) -> List[str]:
        return build_lang_chain(target_lang=target_lang, source_lang=self.source_lang)

    def _resolve_target_langs(self, target_langs: Optional[List[str]] = None) -> List[str]:
        langs = parse_target_langs(target_langs, source_lang=self.source_lang)
        return langs if langs else list(self.target_langs)

    def _load_shared_cache(self, lang: str) -> Dict[str, str]:
        return self._storage.load_cache("shared", lang)

    def _load_backend_cache(self, dict_name: str, lang: str) -> Dict[str, str]:
        return self._storage.load_cache(dict_name, lang)

    def _get_from_shared_chain(self, key: str, target_lang: str) -> Optional[str]:
        for lang in self._get_lang_chain(target_lang):
            cache = self._load_shared_cache(lang)
            if key in cache:
                return cache[key]
        return None

    def _get_from_backend_chain(self, key: str, dict_name: str, target_lang: str) -> Optional[str]:
        for lang in self._get_lang_chain(target_lang):
            cache = self._load_backend_cache(dict_name, lang)
            if key in cache:
                return cache[key]
        return None

    def _ensure_key_mapped(self, text: str) -> str:
        mapping = load_keys_mapping(self.cache_dir)
        h = text_hash(text)
        if h not in mapping:
            mapping[h] = text
            save_keys_mapping(self.cache_dir, mapping)
        return h

    def register_page(self, page_name: str, html_getter, target_langs: List[str], context: Optional[Dict[str, Any]] = None) -> None:
        normalized = [self._normalize_lang(l) for l in target_langs]
        self._page_registry.register_page(page_name, html_getter, normalized, context or {})

    def register_keys(self, items: Dict[str, str], dict_name: str = "bot", target_langs: Optional[List[str]] = None) -> int:
        dict_name = normalize_backend_dict_name(dict_name)
        if not isinstance(items, dict):
            raise TypeError("items must be a dict")
        langs = self._resolve_target_langs(target_langs)
        if not langs:
            return 0
        source_cache = self._load_backend_cache(dict_name, self.source_lang)
        pending = self._storage.load_pending(dict_name)
        added = 0
        for key, default_text in items.items():
            if default_text is None:
                continue
            normalized_key = str(key).strip()
            normalized_text = str(default_text).strip()
            if not normalized_key or not normalized_text or not should_translate_backend_text(normalized_text):
                continue
            h = self._ensure_key_mapped(normalized_text)
            source_cache[h] = normalized_text
            for lang in langs:
                if lang == self.source_lang:
                    continue
                lang_cache = self._load_backend_cache(dict_name, lang)
                lang_bucket = pending.setdefault(lang, {})
                if h in lang_cache or h in lang_bucket:
                    continue
                lang_bucket[h] = {"text": normalized_text, "prompt_type": "backend"}
                added += 1
        self._storage.save_cache(dict_name, self.source_lang, source_cache)
        self._storage.save_pending(dict_name, pending)
        return added

    def translate_html(self, html: str, target_lang: str, page_name: str = "page") -> str:
        target_lang = self._normalize_lang(target_lang)
        if target_lang == self.source_lang:
            return html

        # Собираем обычные тексты из HTML (для очереди)
        items = collect_translatable_items(html, self.cache_dir)
        pending = self._storage.load_pending("shared")
        bucket = pending.setdefault(target_lang, {})
        for item in items:
            h = item["hash"]
            if h not in bucket:
                bucket[h] = {"text": item["text"], "prompt_type": "normal"}
        if bucket:
            self._storage.save_pending("shared", pending)

        # Парсим HTML для рендера, попутно собираем скрипты
        cache = self._load_shared_cache(target_lang)
        parser = SimpleHTMLTranslator(
            translate_callback=lambda t, tag, attr: cache.get(text_hash(t), t) if t and should_translate(t) else t
        )
        parser.feed(html)
        parser.close()

        # Обрабатываем скрипты (используем обновлённый парсер JS)
        script_contents = parser.get_script_contents()
        if script_contents:
            all_js_items = []
            for script in script_contents:
                items_js = extract_js_keys_from_content(script)
                all_js_items.extend(items_js)
            if all_js_items:
                pending = self._storage.load_pending("shared")
                bucket = pending.setdefault(target_lang, {})
                for item in all_js_items:
                    text = item["text"]
                    if text and should_translate_ui_text(text):
                        h = self._ensure_key_mapped(text)
                        if h not in bucket:
                            bucket[h] = {"text": text, "prompt_type": "ui"}
                if bucket:
                    self._storage.save_pending("shared", pending)

        # Получаем переведённый HTML и заменяем строки в скриптах
        result_html = parser.get_html()
        if cache:
            from .utils import load_keys_mapping, replace_translatable_strings
            keys_mapping = load_keys_mapping(self.cache_dir)
            # Строим словарь оригинал -> перевод
            original_to_translation = {}
            for key, trans in cache.items():
                # Если ключ — хеш (40 hex-символов), ищем оригинал в keys_mapping
                if isinstance(key, str) and len(key) == 40 and all(c in '0123456789abcdefABCDEF' for c in key):
                    original = keys_mapping.get(key)
                    if original:
                        original_to_translation[original] = trans
                else:
                    # Иначе считаем, что ключ уже является оригинальным текстом
                    original_to_translation[key] = trans
            if original_to_translation:
                result_html = replace_translatable_strings(result_html, original_to_translation)
        return result_html

    def translate_dict(self, page_name: str, dict_name: str, source_dict: dict, target_lang: Optional[str] = None, filter_keys: Optional[List[str]] = None) -> dict:
        if not source_dict or not isinstance(source_dict, dict):
            return {}
        target_lang = self._normalize_lang(target_lang)
        if not target_lang or target_lang == self.source_lang:
            return deep_copy_json_like(source_dict)

        cache = self._load_shared_cache(target_lang)
        pending = self._storage.load_pending("shared")
        bucket = pending.setdefault(target_lang, {})
        filter_set = set(filter_keys or [])

        def walk(value: Any, path_parts: List[str]) -> Any:
            if isinstance(value, dict):
                return {k: walk(v, path_parts + [str(k)]) for k, v in value.items()}
            if isinstance(value, list):
                return [walk(v, path_parts + [str(i)]) for i, v in enumerate(value)]
            if isinstance(value, str):
                if filter_set and (not path_parts or path_parts[-1] not in filter_set):
                    return value
                if not should_translate_ui_text(value):
                    return value
                h = self._ensure_key_mapped(value)
                if h in cache:
                    return cache[h]
                if h not in bucket:
                    bucket[h] = {"text": value, "prompt_type": "ui"}
                return value
            return value

        result = walk(deep_copy_json_like(source_dict), [])
        if bucket:
            self._storage.save_pending("shared", pending)
        return result

    def translate_key(self, key: str, lang: str, default: str, dict_name: str = "bot") -> str:
        dict_name = normalize_backend_dict_name(dict_name)
        target_lang = self._normalize_lang(lang)
        if target_lang == self.source_lang:
            return default
        h = self._ensure_key_mapped(default)
        translated = self._get_from_backend_chain(h, dict_name, target_lang)
        if translated is not None:
            return translated
        pending = self._storage.load_pending(dict_name)
        bucket = pending.setdefault(target_lang, {})
        if h not in bucket:
            bucket[h] = {"text": default, "prompt_type": "backend"}
        self._storage.save_pending(dict_name, pending)
        return default

    def process_pending_translations(self, target_lang: Optional[str] = None, batch_size: int = 50) -> int:
        return self._worker.process_pending("shared", target_lang, batch_size)

    def process_backend_key_translations(self, dict_name: str = "bot", target_lang: Optional[str] = None, batch_size: int = 50) -> int:
        dict_name = normalize_backend_dict_name(dict_name)
        return self._worker.process_pending(dict_name, target_lang, batch_size)

    def process_all_backend_key_translations(self, batch_size: int = 50) -> Dict[str, int]:
        report = {}
        for d in ("bot", "system"):
            processed = self.process_backend_key_translations(d, batch_size=batch_size)
            if processed:
                report[d] = processed
        return report

    def process_all_translations(self, batch_size: int = 50) -> Dict[str, Dict[str, int]]:
        report = {}
        for page in self._page_registry.list_pages():
            page_report = {}
            for lang in page.target_langs:
                html = page.html_getter(**(page.context or {}))
                items = collect_translatable_items(html, self.cache_dir)
                pending = self._storage.load_pending("shared")
                bucket = pending.setdefault(lang, {})
                for item in items:
                    h = item["hash"]
                    if h not in bucket:
                        bucket[h] = {"text": item["text"], "prompt_type": "normal"}
                self._storage.save_pending("shared", pending)
                processed = self.process_pending_translations(target_lang=lang, batch_size=batch_size)
                if processed:
                    page_report[lang] = processed
            if page_report:
                report[page.page_name] = page_report
        backend_report = self.process_all_backend_key_translations(batch_size)
        if backend_report:
            report["_backend_keys"] = backend_report
        if self.scan_paths:
            keys_report = self.extract_keys()
            if keys_report.get("extracted", 0):
                report["_keys"] = {
                    "extracted": keys_report["extracted"],
                    "queued": keys_report["queued"],
                }
        return report

    def extract_keys(self, scan_paths: Optional[List[Dict[str, str]]] = None) -> Dict[str, int]:
        """
        Единая точка входа: обходит файлы согласно scan_paths,
        извлекает ключи (js + html), кладёт в pending для каждого целевого языка.
        """
        paths = parse_scan_paths(scan_paths) if scan_paths else list(self.scan_paths)
        if not paths:
            return {"files": 0, "extracted": 0, "queued": 0}

        js_files: List[str] = []
        html_files: List[str] = []
        for item in paths:
            matched = resolve_glob_paths([item["path"]])
            kind = item["type"]
            if kind == "js":
                js_files.extend(matched)
            elif kind == "html":
                html_files.extend(matched)
            else:  # auto
                for p in matched:
                    if p.lower().endswith((".html", ".htm")):
                        html_files.append(p)
                    elif p.lower().endswith((".js", ".jsx", ".tsx", ".ts")):
                        js_files.append(p)

        js_files = sorted(set(js_files))
        html_files = sorted(set(html_files))

        items: List[Dict[str, str]] = []
        if js_files:
            items.extend(extract_js_keys_from_files(js_files))
        if html_files:
            items.extend(extract_html_keys_from_files(html_files))
        if not items:
            return {"files": len(js_files) + len(html_files), "extracted": 0, "queued": 0}

        langs = self._resolve_target_langs()
        pending = self._storage.load_pending("shared")
        source_cache = self._load_shared_cache(self.source_lang)
        queued = 0
        for item in items:
            key = item["key"]
            text = item["text"]
            source_cache[key] = text
            for lang in langs:
                if lang == self.source_lang:
                    continue
                lang_cache = self._load_shared_cache(lang)
                bucket = pending.setdefault(lang, {})
                if key in lang_cache or key in bucket:
                    continue
                bucket[key] = {"text": text, "prompt_type": "ui"}
                queued += 1

        self._storage.save_cache("shared", self.source_lang, source_cache)
        self._storage.save_pending("shared", pending)
        return {
            "files": len(js_files) + len(html_files),
            "extracted": len(items),
            "queued": queued,
        }

    def run_translation_loop(self, interval: int = 300, target_lang: Optional[str] = None, batch_size: int = 50, stop_event: Optional[threading.Event] = None) -> None:
        self._worker.run_loop(interval, target_lang, batch_size, stop_event)

    def get_frontend_translations(self, lang: str) -> Dict[str, str]:
        target_lang = self._normalize_lang(lang)
        cache = self._load_shared_cache(target_lang)
        return cache

    def build_frontend_runtime(self, lang: str, dynamic_dom_enabled: Optional[bool] = None) -> str:
        target_lang = self._normalize_lang(lang)
        cache = self._load_shared_cache(target_lang)  # {хеш: перевод}
        if not cache:
            translations = {}
        else:
            from .utils import load_keys_mapping
            keys_mapping = load_keys_mapping(self.cache_dir)  # {хеш: оригинал}
            # Строим словарь {оригинал: перевод}
            translations = {}
            for h, trans in cache.items():
                original = keys_mapping.get(h)
                if original:
                    translations[original] = trans
                # если оригинала нет – пропускаем
        return build_frontend_runtime_script(
            translations=translations,
            fallback_lang=self.fallback_lang,
            dynamic_dom_enabled=self.dynamic_dom_enabled if dynamic_dom_enabled is None else bool(dynamic_dom_enabled),
        )

    def get_translation_coverage(self, lang: str) -> dict:
        lang = self._normalize_lang(lang)
        cache = self._load_shared_cache(lang)
        pending = self._storage.load_pending("shared").get(lang, {})
        total = len(cache) + len(pending)
        return {
            "lang": lang,
            "translated": len(cache),
            "pending": len(pending),
            "total": total,
            "percent": 100.0 if total == 0 else round((len(cache) / total) * 100, 2),
        }

    def get_backend_translation_coverage(self, lang: str, dict_name: str = "bot") -> dict:
        dict_name = normalize_backend_dict_name(dict_name)
        lang = self._normalize_lang(lang)
        cache = self._load_backend_cache(dict_name, lang)
        pending = self._storage.load_pending(dict_name).get(lang, {})
        total = len(cache) + len(pending)
        return {
            "dict_name": dict_name,
            "lang": lang,
            "translated": len(cache),
            "pending": len(pending),
            "total": total,
            "percent": 100.0 if total == 0 else round((len(cache) / total) * 100, 2),
        }

    def detect_browser_lang(self, accept_language: str) -> str:
        if not accept_language:
            return self.source_lang
        return self._normalize_lang(accept_language.split(",")[0])

    def get_alternative_lang(self, current_lang: str, browser_lang: str) -> str:
        current_lang = self._normalize_lang(current_lang)
        browser_lang = self._normalize_lang(browser_lang)
        if not browser_lang:
            return "en" if current_lang == self.source_lang else self.source_lang
        if current_lang == browser_lang:
            return "en" if current_lang != "en" else self.source_lang
        return browser_lang