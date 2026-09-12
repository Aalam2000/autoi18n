# src/autoi18n/worker.py
import time
import threading
import logging
from typing import Optional, List, Dict, Any

from .ai_translator import BaseTranslator

# ---------- Логирование (отключаемое) ----------
logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class Worker:
    def __init__(
        self,
        storage,
        translator: BaseTranslator,
        source_lang: str,
    ):
        """
        Инициализация воркера.

        Args:
            storage: Экземпляр Storage для работы с кэшем и очередями.
            translator: Экземпляр переводчика (реализация BaseTranslator).
            source_lang: Исходный язык (используется при необходимости).
        """
        self.storage = storage
        self.translator = translator
        self.source_lang = source_lang

        # Для обратной совместимости сохраняем максимальное количество повторных попыток из translator,
        # но оно в основном используется внутри translator.
        # Также можно читать переменные окружения, если нужно управлять поведением воркера.
        import os
        self.batch_size = int(os.getenv("AUTO_I18N_BATCH_SIZE", "50"))
        self.interval = int(os.getenv("AUTO_I18N_WORKER_INTERVAL", "300"))

    def process_pending(
        self,
        dict_type: str,
        target_lang: Optional[str] = None,
        batch_size: int = 50
    ) -> int:
        """
        Обрабатывает очередь переводов для указанного типа словаря.

        Args:
            dict_type: 'shared', 'bot' или 'system'
            target_lang: Если указан, обрабатываем только этот язык.
            batch_size: Размер пакета для пакетного перевода.

        Returns:
            Количество обработанных элементов.
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

            # Собираем элементы, которых нет в кэше
            items = [
                {"storage_key": k, "text": v["text"], "prompt_type": v["prompt_type"]}
                for k, v in bucket.items() if k not in cache
            ]
            if not items:
                pending.pop(lang, None)
                continue

            processed = 0
            # Обрабатываем батчами
            for i in range(0, len(items), batch_size):
                batch = items[i:i + batch_size]
                # Вызываем пакетный перевод (возвращает словарь {storage_key: translated_text})
                translated = self.translator.translate_batch(batch, lang)

                # Обновляем кэш для успешно переведённых
                if translated:
                    cache.update(translated)
                    self.storage.save_cache(dict_type, lang, cache)

                # Удаляем из bucket те, что переведены
                for item in batch:
                    if item["storage_key"] in translated:
                        bucket.pop(item["storage_key"], None)
                    # Если перевод не удался – оставляем в bucket для следующей попытки

                processed += len(batch)

            total_processed += processed

            # Если bucket опустел, удаляем язык из pending
            if not bucket:
                pending.pop(lang, None)

        self.storage.save_pending(dict_type, pending)
        return total_processed

    def run_loop(
        self,
        interval: int = 300,
        target_lang: Optional[str] = None,
        batch_size: int = 50,
        stop_event: Optional[threading.Event] = None
    ) -> None:
        """
        Запускает бесконечный цикл обработки очередей.

        Args:
            interval: Интервал между циклами (секунды).
            target_lang: Язык для обработки (если None – все).
            batch_size: Размер пакета.
            stop_event: Событие для остановки цикла.
        """
        if interval <= 0:
            raise ValueError("interval must be > 0")

        logger.info(f"Worker loop started, interval={interval}s, target_lang={target_lang or 'all'}")
        while True:
            try:
                for dict_type in ("shared", "bot", "system"):
                    processed = self.process_pending(dict_type, target_lang, batch_size)
                    if processed:
                        logger.debug(f"Processed {processed} items for {dict_type}")
            except Exception as e:
                logger.error(f"Unhandled error in worker loop: {e}", exc_info=True)

            if stop_event and stop_event.is_set():
                logger.info("Worker loop stopped by event")
                break

            time.sleep(interval)

            if stop_event and stop_event.is_set():
                logger.info("Worker loop stopped by event")
                break