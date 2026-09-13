# src/autoi18n/worker.py
"""
Исполнитель очереди на перевод.

Сам цикл ("сколько ждать между проходами", "когда пересканировать файлы")
теперь во владении Translator.run_translation_loop() — там же вызывается
extract(), которого в v1 в цикле воркера не было вовсе (воркер только
разгребал pending, но никогда сам не пересканировал проект). Worker здесь
отвечает только за одну операцию: перевести накопленную очередь пачками
через AI-провайдер.
"""
import logging
from typing import Optional

from .ai_translator import BaseTranslator

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class Worker:
    def __init__(self, storage, translator: BaseTranslator, source_lang: str):
        self.storage = storage
        self.translator = translator
        self.source_lang = source_lang

    def process_pending(
        self,
        dict_type: str,
        target_lang: Optional[str] = None,
        batch_size: int = 50,
    ) -> int:
        """
        Переводит накопленную очередь для указанного типа словаря
        ('shared', 'bot' или 'system'). Возвращает число обработанных
        элементов (успешно переведённых и оставшихся в очереди на повтор
        при ошибке — вместе).
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
            items = [
                {"storage_key": k, "text": v["text"], "prompt_type": v["prompt_type"]}
                for k, v in bucket.items() if k not in cache
            ]
            if not items:
                pending.pop(lang, None)
                continue

            for i in range(0, len(items), batch_size):
                batch = items[i:i + batch_size]
                translated = self.translator.translate_batch(batch, lang)

                if translated:
                    cache.update(translated)
                    self.storage.save_cache(dict_type, lang, cache)

                for item in batch:
                    if item["storage_key"] in translated:
                        bucket.pop(item["storage_key"], None)
                    # не переведённые остаются в bucket — уйдут на следующем проходе

                total_processed += len(batch)

            if not bucket:
                pending.pop(lang, None)

        self.storage.save_pending(dict_type, pending)
        return total_processed
