# src/autoi18n/ai_translator.py
import os
import logging
from abc import ABC, abstractmethod
from typing import List, Dict, Optional, Any

from openai import OpenAI

# ---------- Логирование (отключаемое) ----------
logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class BaseTranslator(ABC):
    """Абстрактный базовый класс для провайдеров перевода (AI)."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.source_lang = config.get("source_lang", "ru")
        self.target_langs = config.get("target_langs", [])
        self._setup_client()

    @abstractmethod
    def _setup_client(self) -> None:
        """Инициализирует клиент к API провайдера."""
        pass

    @abstractmethod
    def translate_single(self, text: str, target_lang: str) -> Optional[str]:
        """Переводит один текст. Возвращает перевод или None при ошибке."""
        pass

    @abstractmethod
    def translate_batch(self, items: List[Dict[str, str]], target_lang: str) -> Dict[str, str]:
        """
        Переводит пакет текстов.
        items: список словарей с полями 'storage_key' и 'text'
        возвращает словарь {storage_key: translated_text}
        """
        pass


class OpenAITranslator(BaseTranslator):
    """Реализация для OpenAI (ChatGPT)."""

    def __init__(self, config: Dict[str, Any]):
        # Извлекаем специфические параметры
        self.api_key = config.get("api_key") or os.getenv("OPENAI_API_KEY")
        self.model = config.get("model", "gpt-4o-mini")
        self.max_retries = config.get("max_retries", 3)
        self.retry_delay = config.get("retry_delay", 1.0)
        self.client = None
        super().__init__(config)

    def _setup_client(self) -> None:
        if not self.api_key:
            raise ValueError("OpenAI API key is required. Set OPENAI_API_KEY or provide in config.")
        self.client = OpenAI(api_key=self.api_key)

    def _build_prompt(self, text: str, target_lang: str) -> str:
        return f"Translate from {self.source_lang} to {target_lang}: {text}"

    def translate_single(self, text: str, target_lang: str) -> Optional[str]:
        for attempt in range(self.max_retries):
            try:
                prompt = self._build_prompt(text, target_lang)
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                )
                return response.choices[0].message.content.strip()
            except Exception as e:
                logger.debug(f"Single translation error (attempt {attempt+1}/{self.max_retries}): {e}")
                if attempt < self.max_retries - 1:
                    import time
                    time.sleep(self.retry_delay)
                else:
                    logger.warning(f"Single translation failed for text: {text[:50]}...")
                    return None
        return None

    def translate_batch(self, items: List[Dict[str, str]], target_lang: str) -> Dict[str, str]:
        if not items:
            return {}

        import json
        import time

        # Разделяем на короткие и длинные (длинные обрабатываем по одному)
        short = [item for item in items if len(item["text"]) <= 3000]
        long = [item for item in items if len(item["text"]) > 3000]

        result = {}

        # Обработка коротких (пакетно)
        if short:
            payload = [{"id": i, "text": item["text"]} for i, item in enumerate(short, 1)]
            prompt = (
                f"Translate each text from {self.source_lang} to {target_lang}. "
                f"Return JSON: {{\"items\": [{{\"id\": 1, \"translated\": \"...\"}}]}}\n"
                f"Input: {json.dumps(payload, ensure_ascii=False)}"
            )

            for attempt in range(self.max_retries):
                try:
                    response = self.client.chat.completions.create(
                        model=self.model,
                        messages=[{"role": "user", "content": prompt}],
                    )
                    raw = response.choices[0].message.content.strip()
                    # Используем утилиту extract_json (она теперь возвращает None при ошибке)
                    from .utils import extract_json
                    data = extract_json(raw)
                    if data is None or not isinstance(data, dict) or "items" not in data:
                        raise ValueError("Invalid JSON structure from model")

                    for item in data.get("items", []):
                        if isinstance(item, dict) and "id" in item and "translated" in item:
                            idx = item["id"] - 1
                            if 0 <= idx < len(short):
                                result[short[idx]["storage_key"]] = item["translated"]
                    # Если все элементы получены, выходим
                    if len(result) == len(short):
                        break
                    else:
                        # Частичный результат – добираем недостающие по одному
                        missing = [item for item in short if item["storage_key"] not in result]
                        for miss in missing:
                            translated = self.translate_single(miss["text"], target_lang)
                            if translated is not None:
                                result[miss["storage_key"]] = translated
                        break
                except Exception as e:
                    logger.debug(f"Batch translation error (attempt {attempt+1}/{self.max_retries}): {e}")
                    if attempt < self.max_retries - 1:
                        time.sleep(self.retry_delay)
                    else:
                        logger.warning("Batch translation failed, falling back to single translations")
                        for item in short:
                            translated = self.translate_single(item["text"], target_lang)
                            if translated is not None:
                                result[item["storage_key"]] = translated
                        break

        # Обработка длинных (по одному)
        for item in long:
            translated = self.translate_single(item["text"], target_lang)
            if translated is not None:
                result[item["storage_key"]] = translated

        return result


# ---------- Фабрика для создания провайдеров ----------
def get_translator(provider: str = "openai", config: Optional[Dict[str, Any]] = None) -> BaseTranslator:
    """
    Возвращает экземпляр переводчика для указанного провайдера.
    provider: "openai" (пока только он)
    config: словарь с параметрами для провайдера (api_key, model, source_lang и т.д.)
    """
    if config is None:
        config = {}

    provider = provider or os.getenv("AUTO_I18N_AI_PROVIDER", "openai")
    provider = provider.lower()

    if provider == "openai":
        return OpenAITranslator(config)
    else:
        raise ValueError(f"Unsupported AI provider: {provider}")