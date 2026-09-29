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
- Каждый перевод проверяется до записи (quality.py); модели передаются
  описание приложения и глоссарий терминов (glossary.py).
- Документы (.md) из явно указанных папок переводятся целиком, перевод
  кладётся файлом рядом с исходником (documents.py) — не в общие словари.
"""
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

from .config import Config
from .storage import Storage
from .worker import Worker, effective_attempts
from .ai_translator import get_translator, BaseTranslator
from .documents import DocumentTranslator
from .glossary import Glossary
from .quality import check_phrase
from .report import Report, clip
from .extractor import js_extractor as _js_extractor
from .extractor import html_extractor as _html_extractor


def _lib_version() -> str:
    try:
        from importlib.metadata import version
        return version("auto-i18n-lib")
    except Exception:  # noqa: BLE001
        return ""
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
        glossary_path: Optional[str] = None,
        doc_paths: Optional[List[str]] = None,
    ):
        self.config = Config(env_path=env_path)
        self.cache_dir = cache_dir or self.config.cache_dir
        self.source_lang = self.config.source_lang
        self.max_attempts = self.config.max_attempts

        self._storage = Storage(self.cache_dir, self.source_lang)
        self.glossary = Glossary(
            glossary_path or self.config.glossary_path or os.path.join(self.cache_dir, "_glossary.json")
        )
        self.version = _lib_version()
        self.documents = DocumentTranslator(
            self.cache_dir,
            self.source_lang,
            doc_paths if doc_paths is not None else self.config.doc_paths,
            max_attempts=self.max_attempts,
            version=self.version,
        )
        # отчёт последнего цикла/команды и журнал: <cache_dir>/_report.json, _autoi18n.log
        self.report = Report(self.cache_dir, self.version)

        # AI-клиент собираем ЛЕНИВО — только при первом реальном обращении
        # к переводу (process_queue/add_target_lang/translate_key). Это
        # значит, что Translator() и extract(dry_run=True) работают вообще
        # без OPENAI_API_KEY — обязательное требование к отладочному этапу.
        self._ai_provider = ai_provider
        self._ai_config_base: Dict[str, Any] = dict(ai_config or {})
        self._ai_config_base.setdefault("api_key", api_key or os.getenv("OPENAI_API_KEY"))
        self._ai_config_base.setdefault("model", model)
        self._ai_config_base.setdefault("glossary", self.glossary)
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
            self._worker = Worker(self._storage, self.translator, self.source_lang, self.max_attempts)
        self._worker.translator = self.translator
        self._worker.report = self.report
        self._worker.version = self.version
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
        rejected: Dict[str, Dict[str, Any]] = {}
        for i in range(0, len(items), batch_size):
            batch = items[i:i + batch_size]
            translated = self.translator.translate_batch(batch, lang)
            problems = getattr(self.translator, "last_problems", {}) or {}
            if translated:
                target_cache.update(translated)
                translated_count += len(translated)
            for item in batch:
                if item["storage_key"] not in translated:
                    rejected[item["storage_key"]] = {
                        "text": item["text"], "prompt_type": "ui", "attempts": 1,
                        "problems": problems.get(item["storage_key"], []),
                    }

        self._storage.save_cache("shared", lang, target_cache)
        if rejected:
            # не прошедшие проверку — в очередь, воркер попробует ещё раз
            with self._lock:
                pending = self._storage.load_pending("shared")
                pending.setdefault(lang, {}).update(rejected)
                self._storage.save_pending("shared", pending)
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
        parse_errors: Dict[str, str] = {}
        bridge_error = None

        if files["js"]:
            js_results = extract_js_items_from_files(files["js"], self.config.attributes)
            parse_errors.update({k: str(v) for k, v in (_js_extractor.LAST_PROBLEMS.get("files") or {}).items()})
            bridge_error = _js_extractor.LAST_PROBLEMS.get("bridge")
            for file_path, items in js_results.items():
                for item in items:
                    report_items.append({**item, "file": file_path, "kind": "js"})

        if files["html"]:
            html_results = extract_html_keys_from_files(files["html"], self.config.attributes)
            parse_errors.update(_html_extractor.LAST_SCRIPT_PROBLEMS)
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

            # Самовосстановление: ЛЮБАЯ известная фраза без перевода на
            # целевой язык (новая, отклонённая проверкой при add-lang,
            # удалённая руками из файла языка) снова ставится в очередь.
            # Раньше в очередь попадали только новые фразы — пропавший
            # перевод не восстанавливался никогда.
            requeued = 0
            for lang in target_langs:
                lang_cache = self._storage.load_cache("shared", lang)
                bucket = pending.setdefault(lang, {})
                for h, text in source_cache.items():
                    if h not in lang_cache and h not in bucket:
                        bucket[h] = {"text": text, "prompt_type": "ui"}
                        requeued += 1
            requeued -= new_count * len(target_langs)

            self._storage.save_cache("shared", self.source_lang, source_cache)
            self._storage.save_pending("shared", pending)

        self.report.data["extract"] = {
            "files_scanned": {"js": len(files["js"]), "html": len(files["html"])},
            "phrases_found": len(unique_texts),
            "new_phrases": new_count,
            "requeued_missing": max(requeued, 0),
            "parse_errors": parse_errors,
            "bridge_error": bridge_error,
            "scan_paths_missing": [str(e.get("path")) for e in self.config.scan_paths
                                   if not os.path.isdir(str(e.get("path")))],
        }
        for f, err in parse_errors.items():
            self.report.log("WARNING", f"файл не разобран, фразы из него не собраны: {f}: {clip(err, 300)}")
        if bridge_error:
            self.report.log("ERROR", f"JS-мост: {bridge_error}")
        if new_count:
            self.report.log("INFO", f"найдено новых фраз: {new_count}")

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
        return apply_translations_to_html(html, self._translations_map(lang), self.config.attributes)

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
            extra_attrs=self.config.attributes,
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
        text = default.strip()
        h = text_hash(text)
        cache = self._storage.load_cache(dict_name, lang)
        if h in cache:
            return cache[h]
        pending = self._storage.load_pending(dict_name)
        bucket = pending.setdefault(lang, {})
        if h not in bucket:
            bucket[h] = {"text": text, "prompt_type": "backend"}
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
        self.report.log("INFO", f"воркер запущен: версия {self.report.version}, интервал {interval} с, "
                                f"языки {self.config.get_target_langs()}, документы {self.documents.doc_paths}, "
                                f"глоссарий {self.glossary.path}")
        while True:
            self.report.start("cycle")
            for step, fn in (
                ("extract", self.extract),
                ("apply_glossary", self.apply_glossary),
                ("requeue_bad", self.requeue_bad),
                ("process_queue", lambda: self.process_queue(batch_size)),
                ("translate_documents", self.translate_documents),
                ("audit", self._report_audit),
            ):
                try:
                    fn()
                except Exception as e:  # noqa: BLE001 — один сбой не должен останавливать остальные шаги
                    self.report.error(step, e)
                    logger.exception(f"Ошибка в цикле Translator.run_translation_loop ({step})")
            self.report.save()
            self.report.log("INFO", self.report.summary())

            if stop_event and stop_event.is_set():
                break
            time.sleep(interval)
            if stop_event and stop_event.is_set():
                break

    def get_translation_coverage(self, lang: str) -> dict:
        lang = self._normalize(lang)
        cache = self._storage.load_cache("shared", lang)
        pending = {k: v for k, v in self._storage.load_pending("shared").get(lang, {}).items()
                   if not v.get("recheck")}
        total = len(cache) + len(pending)
        return {
            "lang": lang,
            "translated": len(cache),
            "pending": len(pending),
            "total": total,
            "percent": 100.0 if total == 0 else round((len(cache) / total) * 100, 2),
        }

    # ---------- документы (.md) целиком ----------
    def translate_documents(self, force: bool = False, dry_run: bool = False) -> Dict[str, Any]:
        """Переводит .md-документы из doc_paths, у которых перевода нет или
        он устарел. Перевод кладётся файлом рядом с исходником."""
        return self.documents.process(
            lambda: self.translator, self.config.get_target_langs(), force=force, dry_run=dry_run,
            sink=None if dry_run else self.report,
        )

    def get_document(self, source_path: str, lang: str) -> Dict[str, str]:
        """{"text": markdown, "lang": язык текста} для показа пользователю.
        Нет перевода на lang — отдаётся исходник."""
        text, text_lang = self.documents.get_text(source_path, self._normalize(lang))
        return {"text": text, "lang": text_lang}

    def document_status(self) -> List[Dict[str, object]]:
        return self.documents.status(self.config.get_target_langs())

    # ---------- глоссарий проекта и исправление сохранённых переводов ----------
    def apply_glossary(self) -> int:
        """
        Фразы, которые ЦЕЛИКОМ есть в глоссарии проекта (ключ без `*`),
        получают перевод из глоссария — и новые, и уже переведённые (перевод
        глоссария имеет приоритет над сохранённым). ИИ не вызывается.
        Возвращает число изменённых переводов.
        """
        exact = self.glossary.exact_items()
        if not exact:
            return 0
        changed = 0
        with self._lock:
            source_cache = self._storage.load_cache("shared", self.source_lang)
            by_text = {" ".join(t.split()): h for h, t in source_cache.items()}
            pending = self._storage.load_pending("shared")
            pending_changed = False
            for lg in self.config.get_target_langs():
                cache = self._storage.load_cache("shared", lg)
                lang_changed = False
                for text, translations in exact:
                    h = by_text.get(text)
                    target = translations.get(lg) or translations.get(lg.split("-")[0])
                    if not h or not target or cache.get(h) == target:
                        continue
                    old = cache.get(h)
                    cache[h] = target
                    lang_changed = True
                    changed += 1
                    if pending.get(lg, {}).pop(h, None) is not None:
                        pending_changed = True
                    self.report.log("INFO", f"глоссарий [{lg}] {text!r}: {old!r} -> {target!r}")
                if lang_changed:
                    self._storage.save_cache("shared", lg, cache)
            if pending_changed:
                self._storage.save_pending("shared", pending)
        self.report.data["glossary_applied"] = changed
        return changed

    def requeue_bad(self) -> int:
        """
        Сохранённые переводы, которые не проходят проверку (quality +
        глоссарий), ставятся в очередь на повторный перевод с флагом recheck.
        Старый перевод остаётся, пока новый не пройдёт проверку полностью
        (см. worker.py). Уже стоящие в очереди не трогаются — счётчик
        попыток сохраняется, после лимита фраза больше не отправляется в ИИ.
        """
        queued = 0
        with self._lock:
            source_cache = self._storage.load_cache("shared", self.source_lang)
            pending = self._storage.load_pending("shared")
            for lg in self.config.get_target_langs():
                cache = self._storage.load_cache("shared", lg)
                bucket = pending.setdefault(lg, {})
                for h, tr in cache.items():
                    src = source_cache.get(h)
                    if src is None or h in bucket:
                        continue
                    problems = check_phrase(src, tr, lg, self.glossary)
                    if problems:
                        bucket[h] = {"text": src, "prompt_type": "ui", "recheck": True, "problems": problems}
                        queued += 1
            if queued:
                self._storage.save_pending("shared", pending)
        self.report.data["requeued_bad"] = queued
        if queued:
            self.report.log("INFO", f"плохих сохранённых переводов поставлено на исправление: {queued}")
        return queued

    def save_report(self, kind: str = "command") -> None:
        """Сохраняет отчёт (_report.json) — для команд CLI вне фонового цикла."""
        self.report.data["kind"] = kind
        self.report.save()

    def _report_audit(self) -> None:
        """Итоги audit() в отчёт цикла (только чтение, ничего не меняет)."""
        a = self.audit()
        self.report.data["audit"] = {
            "bad_count": len(a["bad"]),
            "failed_count": len(a["failed"]),
            "pending": a["pending"],
            "bad": a["bad"][:300],
            "failed": a["failed"][:300],
            "documents_not_ok": [
                {k: r[k] for k in ("source", "lang", "state", "attempts", "problem")} for r in a["documents"]
            ],
        }

    # ---------- контроль качества уже сохранённых переводов ----------
    def audit(self, lang: Optional[str] = None) -> Dict[str, Any]:
        """
        Проверяет уже сохранённые переводы фраз интерфейса теми же правилами,
        что и новые (quality.check_phrase + глоссарий), и собирает:
        - bad:     [{lang, text, translation, problems}] — плохие переводы в файлах;
        - failed:  [{lang, text, attempts, problems}] — фразы, которые воркер
                   так и не смог перевести (исчерпан лимит попыток);
        - pending: {lang: число фраз в очереди};
        - documents: документы без актуального перевода.
        """
        langs = [self._normalize(lang)] if lang else self.config.get_target_langs()
        source_cache = self._storage.load_cache("shared", self.source_lang)
        pending = self._storage.load_pending("shared")
        bad, failed = [], []
        for lg in langs:
            cache = self._storage.load_cache("shared", lg)
            for h, tr in cache.items():
                src = source_cache.get(h)
                if src is None:
                    continue
                problems = check_phrase(src, tr, lg, self.glossary)
                if problems:
                    bad.append({"lang": lg, "text": src, "translation": tr, "problems": problems})
            for h, meta in pending.get(lg, {}).items():
                if effective_attempts(meta, self.version) >= self.max_attempts:
                    failed.append({"lang": lg, "text": meta["text"], "attempts": meta["attempts"],
                                   "problems": meta.get("problems", [])})
        docs = [r for r in self.documents.status(langs) if r["state"] != "ok"]
        return {
            "bad": bad,
            "failed": failed,
            "pending": {lg: len(pending.get(lg, {})) for lg in langs},
            "documents": docs,
        }

    def retranslate(
        self,
        lang: Optional[str] = None,
        texts: Optional[List[str]] = None,
        contains: Optional[str] = None,
        from_audit: bool = False,
        run: bool = True,
        batch_size: int = 50,
    ) -> Dict[str, int]:
        """
        Удаляет выбранные переводы и ставит фразы в очередь заново (со
        сброшенным счётчиком попыток). Выбор:
        - texts      — точные исходные тексты;
        - contains   — все фразы, в исходном тексте которых есть подстрока
                       (например, термин после изменения глоссария);
        - from_audit — все плохие и не переведённые фразы из audit().
        run=True — сразу перевести очередь.
        """
        langs = [self._normalize(lang)] if lang else self.config.get_target_langs()
        source_cache = self._storage.load_cache("shared", self.source_lang)

        selected: Dict[str, set] = {lg: set() for lg in langs}
        if texts:
            for t in texts:
                h = text_hash(t.strip())
                if h in source_cache:
                    for lg in langs:
                        selected[lg].add(h)
        if contains:
            needle = contains.lower()
            for h, src in source_cache.items():
                if needle in src.lower():
                    for lg in langs:
                        selected[lg].add(h)
        if from_audit:
            report = self.audit(lang)
            for row in report["bad"] + report["failed"]:
                if row["lang"] in selected:
                    selected[row["lang"]].add(text_hash(row["text"]))

        queued = 0
        with self._lock:
            pending = self._storage.load_pending("shared")
            for lg, hashes in selected.items():
                if not hashes:
                    continue
                cache = self._storage.load_cache("shared", lg)
                bucket = pending.setdefault(lg, {})
                for h in hashes:
                    if h not in source_cache:
                        continue
                    cache.pop(h, None)
                    bucket[h] = {"text": source_cache[h], "prompt_type": "ui"}
                    queued += 1
                self._storage.save_cache("shared", lg, cache)
            self._storage.save_pending("shared", pending)

        translated = self.process_queue(batch_size) if (run and queued) else 0
        return {"queued": queued, "translated": translated}

    def set_translation(self, lang: str, source_text: str, translation: str) -> List[str]:
        """
        Ручная правка перевода фразы интерфейса по исходному тексту (без
        хешей). Возвращает замечания проверки качества (перевод всё равно
        записывается — это осознанное решение человека).
        """
        lang = self._normalize(lang)
        src = source_text.strip()
        h = text_hash(src)
        source_cache = self._storage.load_cache("shared", self.source_lang)
        if h not in source_cache:
            raise ValueError(f"Фразы нет в реестре исходного языка: {src!r}")
        with self._lock:
            cache = self._storage.load_cache("shared", lang)
            cache[h] = translation.strip()
            self._storage.save_cache("shared", lang, cache)
            pending = self._storage.load_pending("shared")
            if h in pending.get(lang, {}):
                pending[lang].pop(h, None)
                self._storage.save_pending("shared", pending)
        return check_phrase(src, translation, lang, self.glossary)
