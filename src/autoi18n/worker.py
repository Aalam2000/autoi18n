# src/autoi18n/worker.py
"""
Исполнитель очереди на перевод.

Сам цикл ("сколько ждать между проходами", "когда пересканировать файлы")
во владении Translator.run_translation_loop(). Worker отвечает только за
одну операцию: перевести накопленную очередь пачками через AI-провайдер.

Фраза, перевод которой не прошёл проверку качества (quality.py), остаётся
в очереди со счётчиком попыток (attempts) и списком проблем (problems).
После max_attempts неудачных попыток она больше не отправляется в ИИ
(не тратим деньги на бесконечные повторы) и видна в `autoi18n audit`;
сбросить счётчик и перевести заново — `autoi18n retranslate`.

Фраза с флагом recheck — это уже переведённая фраза, чей сохранённый
перевод не проходит проверку (её ставит в очередь Translator при
проверке сохранённых переводов). Старый перевод остаётся в кэше, пока
новый не пройдёт проверку ПОЛНОСТЬЮ (включая глоссарий) — иначе каждый
цикл заменял бы один плохой перевод другим.
"""
import logging
from typing import Optional

from .ai_translator import BaseTranslator
from .report import clip

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

DEFAULT_MAX_ATTEMPTS = 3


def effective_attempts(meta: dict, version: str) -> int:
    """Попытки считаются в пределах одной версии библиотеки: после
    обновления (исправлен промпт/проверка) фраза снова пробуется."""
    if meta.get("version", "") != version:
        return 0
    return int(meta.get("attempts", 0) or 0)


class Worker:
    def __init__(self, storage, translator: BaseTranslator, source_lang: str,
                 max_attempts: int = DEFAULT_MAX_ATTEMPTS, report=None):
        self.storage = storage
        self.translator = translator
        self.source_lang = source_lang
        self.max_attempts = max_attempts
        self.report = report
        self.version = getattr(report, "version", "") if report is not None else ""

    def process_pending(
        self,
        dict_type: str,
        target_lang: Optional[str] = None,
        batch_size: int = 50,
    ) -> int:
        """
        Переводит накопленную очередь для указанного типа словаря
        ('shared', 'bot' или 'system'). Возвращает число реально
        переведённых фраз; не переведённые остаются в очереди на повтор
        и в счётчик не входят.
        """
        pending = self.storage.load_pending(dict_type)
        if not pending:
            return 0

        langs = [target_lang] if target_lang else list(pending.keys())
        total_processed = 0

        for lang in langs:
            bucket = pending.get(lang, {})
            if not bucket:
                continue

            cache = self.storage.load_cache(dict_type, lang)
            for k in [k for k in bucket if k in cache and not bucket[k].get("recheck")]:
                bucket.pop(k, None)
            items = [
                {"storage_key": k, "text": v["text"], "prompt_type": v["prompt_type"]}
                for k, v in bucket.items()
                if effective_attempts(v, self.version) < self.max_attempts
            ]
            if not items:
                if not bucket:
                    pending.pop(lang, None)
                continue

            for i in range(0, len(items), batch_size):
                batch = items[i:i + batch_size]
                translated = dict(self.translator.translate_batch(batch, lang))
                problems = getattr(self.translator, "last_problems", {}) or {}
                details = getattr(self.translator, "last_details", {}) or {}
                api_errors = getattr(self.translator, "last_api_errors", []) or []
                for item in batch:
                    key = item["storage_key"]
                    if key in translated and bucket[key].get("recheck") and details.get(key):
                        translated.pop(key)  # исправление принимается только без замечаний

                if translated:
                    cache.update(translated)
                    self.storage.save_cache(dict_type, lang, cache)

                for item in batch:
                    key = item["storage_key"]
                    detail = details.get(key, {})
                    if key in translated:
                        bucket.pop(key, None)
                        if detail:
                            self._note("accepted_with_remarks", lang, dict_type, item["text"], translated[key], detail)
                    else:
                        meta = bucket[key]
                        meta["attempts"] = effective_attempts(meta, self.version) + 1
                        meta["version"] = self.version
                        meta["problems"] = problems.get(key) or detail.get("problems") or ["нет ответа модели"]
                        meta["last_response"] = clip(detail.get("retry_response") or detail.get("response"))
                        api_error = detail.get("retry_api_error") or detail.get("api_error")
                        if api_error:
                            meta["api_error"] = clip(api_error)
                        else:
                            meta.pop("api_error", None)
                        self._note("rejected", lang, dict_type, item["text"], None, detail, meta["attempts"])

                if self.report is not None:
                    ph = self.report.data["phrases"]
                    ph["translated"][lang] = ph["translated"].get(lang, 0) + len(translated)
                    for err in api_errors:
                        ph["api_errors"].append({"lang": lang, "dict": dict_type, "error": clip(err)})
                        self.report.log("ERROR", f"API [{lang}] {clip(err, 300)}")

                total_processed += len(translated)

            if not bucket:
                pending.pop(lang, None)

        self.storage.save_pending(dict_type, pending)
        return total_processed

    def _note(self, section, lang, dict_type, text, translation, detail, attempts=None) -> None:
        if self.report is None:
            return
        row = {
            "lang": lang,
            "dict": dict_type,
            "text": text,
            "problems": detail.get("retry_problems") or detail.get("problems") or ["нет ответа модели"],
            "response": clip(detail.get("response")),
            "retry_response": clip(detail.get("retry_response")),
            "api_error": clip(detail.get("retry_api_error") or detail.get("api_error")),
        }
        if translation is not None:
            row["saved"] = translation
        if attempts is not None:
            row["attempts"] = attempts
            row["gave_up"] = attempts >= self.max_attempts
        self.report.data["phrases"][section].append(row)
        verb = "ОТКЛОНЁН" if section == "rejected" else "принят с замечанием"
        self.report.log("WARNING", f"перевод {verb} [{lang}] {text[:80]!r}: {'; '.join(row['problems'])}"
                                   + (f" | ответ: {row['retry_response'] or row['response']!r}" if (row['retry_response'] or row['response']) else "")
                                   + (f" | API: {row['api_error']}" if row['api_error'] else ""))
