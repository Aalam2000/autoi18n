# src/autoi18n/translator.py
"""
Главный фасад библиотеки — Translator.

Архитектура v2 (кратко, полное обсуждение — вне кода):
- Ключ хранения = hash(текст на исходном языке). Никаких семантических ID.
- Файл исходного языка (translations/{source_lang}.json) — сам по себе
  реестр всех известных фраз проекта. Отдельного файла-маппинга нет.
- extract() сравнивает найденное в файлах ТОЛЬКО с этим реестром — новое
  добавляется в реестр и ставится в очередь на все активные целевые языки.
  Данные, которые появляются только в момент рендера (контент оператора,
  БД), extract() не видит вообще — сканируются только файлы проекта.
- Целевые языки — из .env (Config), редактируются через add_target_lang().
- JS/JSX/TSX парсится через AST (Node/Babel мост), HTML — через stdlib
  HTMLParser. Оба типа файлов ищутся explicit-путями (extractor.file_scanner),
  без glob и без brace-паттернов.
"""
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

from .config import Config
from .storage import Storage
from .worker import Worker
from .ai_translator import get_translator, BaseTranslator
from .extractor import (
    resolve_scan_paths,
    extract_js_items_from_files,
    extract_html_keys_from_files,
    apply_translations_to_html,
)
from .runtime import build_frontend_runtime_script
from .utils import (
    text_hash,
    normalize_lang,
    normalize_backend_dict_name,
    should_translate_ui_text,
    should_translate_backend_text,
    deep_copy_json_like,
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class Translator:
    def __init__(
        self,
        cache_dir: Optional[str] = None,
        api_key: Optional[str] = None,
        model: str = "gpt-4o-mini",
        ai_provider: str = "openai",
        ai_config: Optional[Dict[str, Any]] = None,
        env_path: str = ".env",
    ):
        self.config = Config(env_path=env_path)
        self.cache_dir = cache_dir or self.config.cache_dir
        self.source_lang = self.config.source_lang

        self._storage = Storage(self.cache_dir, self.source_lang)

        # AI-клиент собираем ЛЕНИВО — только при первом реальном обращении
        # к переводу (process_queue/add_target_lang/translate_key). Это
        # значит, что Translator() и extract(dry_run=True) работают вообще
        # без OPENAI_API_KEY — обязательное требование к отладочному этапу.
        self._ai_provider = ai_provider
        self._ai_config_base: Dict[str, Any] = dict(ai_config or {})
        self._ai_config_base.setdefault("api_key", api_key or os.getenv("OPENAI_API_KEY"))
        self._ai_config_base.setdefault("model", model)
        self._ai_translator: Optional[BaseTranslator] = None

        self._worker: Optional[Worker] = None
        self._lock = threading.RLock()

    @property
    def translator(self) -> BaseTranslator:
        """AI-клиент, созданный при первом обращении (см. комментарий в __init__)."""
        if self._ai_translator is None:
            ai_config = dict(self._ai_config_base)
            ai_config.setdefault("source_lang", self.source_lang)
            ai_config.setdefault("target_langs", self.config.get_target_langs())
            self._ai_translator = get_translator(self._ai_provider, ai_config)
        return self._ai_translator

    @property
    def _worker_instance(self) -> Worker:
        if self._worker is None:
            self._worker = Worker(self._storage, self.translator, self.source_lang)
        return self._worker

    def _normalize(self, lang: str) -> str:
        return normalize_lang(lang, source_lang=self.source_lang)

    # ---------- целевые языки ----------
    def get_target_langs(self) -> List[str]:
        return self.config.get_target_langs()

    def add_target_lang(self, lang: str, batch_size: int = 50) -> Dict[str, int]:
        """
        Регистрирует новый целевой язык (дописывает .env через Config) и
        сразу переводит на него весь уже известный файл исходного языка —
        не дожидаясь очередного цикла воркера. Это и есть действие для
        админки: "появился новый язык -> быстро сделать файл перевода".
        """
        added = self.config.add_target_lang(lang)
        if not added:
            return {"added": 0, "translated": 0}

        lang = self._normalize(lang)
        source_cache = self._storage.load_cache("shared", self.source_lang)
        if not source_cache:
            return {"added": 1, "translated": 0}

        target_cache = self._storage.load_cache("shared", lang)
        items = [
            {"storage_key": h, "text": text, "prompt_type": "ui"}
            for h, text in source_cache.items()
            if h not in target_cache
        ]

        translated_count = 0
        for i in range(0, len(items), batch_size):
            batch = items[i:i + batch_size]
            translated = self.translator.translate_batch(batch, lang)
            if translated:
                target_cache.update(translated)
                translated_count += len(translated)

        self._storage.save_cache("shared", lang, target_cache)
        return {"added": 1, "translated": translated_count}

    # ---------- извлечение (сбор фраз проекта) ----------
    def extract(self, dry_run: bool = False) -> Dict[str, Any]:
        """
        Сканирует проект (HTML + JS/JSX/TSX по config.scan_paths) и
        сравнивает найденное с файлом исходного языка.

        dry_run=True — ничего не пишет и не ставит в очередь, только
        возвращает отчёт {file, kind, text, placeholders, line} по каждой
        найденной фразе. Это обязательный отладочный этап: прогнать,
        руками сверить на мусор/пропуски, и только потом включать ИИ.
        """
        files = resolve_scan_paths(self.config.scan_paths)
        report_items: List[Dict[str, object]] = []

        if files["js"]:
            js_results = extract_js_items_from_files(files["js"])
            for file_path, items in js_results.items():
                for item in items:
                    report_items.append({**item, "file": file_path, "kind": "js"})

        if files["html"]:
            html_results = extract_html_keys_from_files(files["html"])
            for file_path, items in html_results.items():
                for item in items:
                    report_items.append({**item, "file": file_path, "kind": "html"})

        # дедуп по тексту в рамках всего скана (один и тот же текст мог
        # встретиться в нескольких местах — это не разные фразы)
        unique_texts: Dict[str, Dict[str, object]] = {}
        for item in report_items:
            unique_texts.setdefault(str(item["text"]), item)

        if dry_run:
            return {
                "files_scanned": len(files["js"]) + len(files["html"]),
                "phrases_found": len(unique_texts),
                "items": list(unique_texts.values()),
            }

        with self._lock:
            source_cache = self._storage.load_cache("shared", self.source_lang)
            target_langs = self.config.get_target_langs()
            pending = self._storage.load_pending("shared")

            new_count = 0
            for text, item in unique_texts.items():
                if not should_translate_ui_text(text):
                    continue
                h = text_hash(text)
                if h in source_cache:
                    continue
                source_cache[h] = text
                new_count += 1
                for lang in target_langs:
                    lang_cache = self._storage.load_cache("shared", lang)
                    bucket = pending.setdefault(lang, {})
                    if h in lang_cache or h in bucket:
                        continue
                    bucket[h] = {"text": text, "prompt_type": "ui"}

            self._storage.save_cache("shared", self.source_lang, source_cache)
            self._storage.save_pending("shared", pending)

        return {
            "files_scanned": len(files["js"]) + len(files["html"]),
            "phrases_found": len(unique_texts),
            "new_phrases": new_count,
        }

    # ---------- рендер ----------
    def _translations_map(self, lang: str) -> Dict[str, str]:
        """{оригинальный_текст: перевод} для данного языка — строится из
        {hash: перевод} целевого кэша + {hash: текст} реестра исходного языка."""
        cache = self._storage.load_cache("shared", lang)
        source_cache = self._storage.load_cache("shared", self.source_lang)
        return {source_cache[h]: trans for h, trans in cache.items() if h in source_cache}

    def get_translations_dict(self, lang: str) -> Dict[str, str]:
        """
        Публичная обёртка над _translations_map: плоский словарь
        {оригинальный_текст: перевод} для данного языка. Именно этот формат
        должен отдавать backend-эндпоинт, который вызывает клиентский
        рантайм (window.autoI18n.setLanguage) при смене языка.
        """
        lang = self._normalize(lang)
        if lang == self.source_lang:
            return {}
        return self._translations_map(lang)

    def apply_to_html(self, html: str, lang: str) -> str:
        lang = self._normalize(lang)
        if lang == self.source_lang:
            return html
        return apply_translations_to_html(html, self._translations_map(lang))

    def apply_to_dict(self, source_dict: dict, lang: str, filter_keys: Optional[List[str]] = None) -> dict:
        lang = self._normalize(lang)
        if not source_dict or not isinstance(source_dict, dict):
            return {}
        if lang == self.source_lang:
            return deep_copy_json_like(source_dict)

        cache = self._storage.load_cache("shared", lang)
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
                h = text_hash(value.strip())
                return cache.get(h, value)
            return value

        return walk(deep_copy_json_like(source_dict), [])

    def build_runtime(
        self,
        lang: str,
        dynamic_dom_enabled: bool = False,
        translations_url_template: Optional[str] = None,
    ) -> str:
        """
        translations_url_template — по умолчанию относительный путь
        ("/i18n/translations?lang={lang}"), рассчитанный на то, что backend
        и frontend отдаются с одного origin. Если они на разных origin
        (например, React dev-server на :3000 и API на :8000) — рантайм
        выполняется в контексте страницы (тот origin, где стоит <script>,
        а не тот, откуда он загружен), и относительный fetch уйдёт не туда.
        В этом случае нужно передать сюда абсолютный URL backend'а.
        """
        lang = self._normalize(lang)
        return build_frontend_runtime_script(
            translations=self._translations_map(lang),
            fallback_lang=self.source_lang,
            dynamic_dom_enabled=dynamic_dom_enabled,
            translations_url_template=translations_url_template,
        )

    # ---------- backend-словари: явно зарегистрированные бэкенд-фразы
    # (не из файлового скана — например, генерируемые сообщения) ----------
    def register_keys(self, items: Dict[str, str], dict_name: str = "bot", target_langs: Optional[List[str]] = None) -> int:
        dict_name = normalize_backend_dict_name(dict_name)
        langs = [self._normalize(l) for l in (target_langs or self.config.get_target_langs())]
        source_cache = self._storage.load_cache(dict_name, self.source_lang)
        pending = self._storage.load_pending(dict_name)
        added = 0
        for default_text in items.values():
            if default_text is None:
                continue
            text = str(default_text).strip()
            if not text or not should_translate_backend_text(text):
                continue
            h = text_hash(text)
            source_cache[h] = text
            for lang in langs:
                if lang == self.source_lang:
                    continue
                lang_cache = self._storage.load_cache(dict_name, lang)
                bucket = pending.setdefault(lang, {})
                if h in lang_cache or h in bucket:
                    continue
                bucket[h] = {"text": text, "prompt_type": "backend"}
                added += 1
        self._storage.save_cache(dict_name, self.source_lang, source_cache)
        self._storage.save_pending(dict_name, pending)
        return added

    def translate_key(self, default: str, lang: str, dict_name: str = "bot") -> str:
        """
        Возвращает перевод бэкенд-фразы по её тексту (не по семантическому
        ключу — ключа больше нет, см. README после миграции). Если перевода
        ещё нет — ставит в очередь и возвращает default.
        """
        dict_name = normalize_backend_dict_name(dict_name)
        lang = self._normalize(lang)
        if lang == self.source_lang:
            return default
        h = text_hash(default.strip())
        cache = self._storage.load_cache(dict_name, lang)
        if h in cache:
            return cache[h]
        pending = self._storage.load_pending(dict_name)
        bucket = pending.setdefault(lang, {})
        if h not in bucket:
            bucket[h] = {"text": default, "prompt_type": "backend"}
            self._storage.save_pending(dict_name, pending)
        return default

    # ---------- очередь / фоновый цикл ----------
    def process_queue(self, batch_size: int = 50) -> int:
        total = 0
        for dict_type in ("shared", "bot", "system"):
            total += self._worker_instance.process_pending(dict_type, None, batch_size)
        return total

    def run_translation_loop(self, interval: int = 300, batch_size: int = 50, stop_event=None) -> None:
        """
        Фоновый цикл: пересканировать проект (extract) -> перевести очередь
        (process_queue). В v1 воркер никогда сам не пересканировал файлы —
        это и было одной из причин, по которой новые фразы не подхватывались.
        """
        if interval <= 0:
            raise ValueError("interval must be > 0")

        logger.info(f"Translator loop started, interval={interval}s")
        while True:
            try:
                report = self.extract()
                if report.get("new_phrases"):
                    logger.info(f"extract(): {report['new_phrases']} новых фраз")
                processed = self.process_queue(batch_size)
                if processed:
                    logger.info(f"process_queue(): обработано {processed}")
            except Exception:
                logger.exception("Ошибка в цикле Translator.run_translation_loop")

            if stop_event and stop_event.is_set():
                break
            time.sleep(interval)
            if stop_event and stop_event.is_set():
                break

    def get_translation_coverage(self, lang: str) -> dict:
        lang = self._normalize(lang)
        cache = self._storage.load_cache("shared", lang)
        pending = self._storage.load_pending("shared").get(lang, {})
        total = len(cache) + len(pending)
        return {
            "lang": lang,
            "translated": len(cache),
            "pending": len(pending),
            "total": total,
            "percent": 100.0 if total == 0 else round((len(cache) / total) * 100, 2),
        }
