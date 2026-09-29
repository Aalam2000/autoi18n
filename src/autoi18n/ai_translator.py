# src/autoi18n/ai_translator.py
"""
ИИ-провайдеры перевода.

Каждый ответ модели проверяется (quality.py) ДО того, как попасть в
результат: не прошедшие проверку фразы переводятся повторно поштучно,
с указанием, что было не так. Фразы, которые так и не прошли жёсткие
проверки, в результат не попадают — остаются в очереди (см. worker.py).

Модели передаются: описание приложения и термины глоссария (glossary.py).
"""
import json
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from openai import OpenAI

from .quality import check_markdown, check_phrase, is_soft
from .utils import extract_json

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class BaseTranslator(ABC):
    """Абстрактный базовый класс для провайдеров перевода (AI)."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.source_lang = config.get("source_lang", "ru")
        self.target_langs = config.get("target_langs", [])
        self.glossary = config.get("glossary")
        # {storage_key: [проблемы]} последнего translate_batch — для отчёта воркера
        self.last_problems: Dict[str, List[str]] = {}
        # подробности последнего translate_batch по отклонённым/замечаниям:
        # {storage_key: {text, response, problems, retry_response, retry_problems, api_error}}
        self.last_details: Dict[str, Dict[str, Any]] = {}
        # ошибки API/формата ответа последнего translate_batch (для отчёта)
        self.last_api_errors: List[str] = []
        # подробности последнего translate_markdown: {attempts: [{response, problems}], api_error}
        self.last_markdown_detail: Dict[str, Any] = {}
        self._setup_client()

    @abstractmethod
    def _setup_client(self) -> None:
        """Инициализирует клиент к API провайдера."""

    @abstractmethod
    def translate_single(self, text: str, target_lang: str, feedback: Optional[str] = None) -> Optional[str]:
        """Переводит один текст. Возвращает перевод или None при ошибке."""

    @abstractmethod
    def translate_batch(self, items: List[Dict[str, str]], target_lang: str) -> Dict[str, str]:
        """
        Переводит пакет фраз интерфейса.
        items: список словарей с полями 'storage_key' и 'text'
        возвращает {storage_key: перевод} — только прошедшие проверку.
        """

    def translate_markdown(self, text: str, target_lang: str) -> Optional[str]:
        """Переводит кусок .md-документа целиком. None — перевод не удался."""
        raise NotImplementedError


class OpenAITranslator(BaseTranslator):
    """Реализация для OpenAI (ChatGPT)."""

    LONG_TEXT = 3000

    def __init__(self, config: Dict[str, Any]):
        self.api_key = config.get("api_key") or os.getenv("OPENAI_API_KEY")
        self.model = config.get("model", "gpt-4o-mini")
        self.max_retries = config.get("max_retries", 3)
        self.retry_delay = config.get("retry_delay", 1.0)
        self.client = None
        # Не все модели принимают temperature/response_format — при отказе
        # API отключаем параметр и повторяем запрос без него.
        self._use_temperature = True
        self._use_json_mode = True
        super().__init__(config)

    def _setup_client(self) -> None:
        if not self.api_key:
            raise ValueError("OpenAI API key is required. Set OPENAI_API_KEY or provide in config.")
        self.client = OpenAI(api_key=self.api_key)

    # ---------- запрос к модели ----------
    def _chat(self, messages: List[Dict[str, str]], json_mode: bool = False) -> str:
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries):
            kwargs: Dict[str, Any] = {"model": self.model, "messages": messages}
            if self._use_temperature:
                kwargs["temperature"] = 0
            if json_mode and self._use_json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            try:
                response = self.client.chat.completions.create(**kwargs)
                return (response.choices[0].message.content or "").strip()
            except Exception as e:  # noqa: BLE001 — любой сбой API = повтор
                msg = str(e).lower()
                if "temperature" in msg and self._use_temperature:
                    self._use_temperature = False
                    continue
                if "response_format" in msg and self._use_json_mode:
                    self._use_json_mode = False
                    continue
                last_error = e
                logger.debug(f"OpenAI error (attempt {attempt + 1}/{self.max_retries}): {e}")
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay)
        raise RuntimeError(f"OpenAI request failed: {type(last_error).__name__}: {last_error}")

    # ---------- промпты ----------
    def _context_lines(self, texts: List[str], target_lang: str) -> str:
        lines = []
        context = ""
        if self.glossary is not None:
            context = self.glossary.get_context()
        context = context or self.config.get("context", "")
        if context:
            lines.append(f"Application context: {context}")
        if self.glossary is not None:
            terms = {}
            for t in texts:
                for key, target in self.glossary.terms_for(t, target_lang):
                    terms[key] = target
            if terms:
                lines.append("Glossary — always translate these terms exactly like this "
                             "(inflect only if grammar requires, never abbreviate or replace with synonyms):")
                lines.extend(f"- {k} -> {v}" for k, v in terms.items())
        return "\n".join(lines)

    def _system_prompt(self, target_lang: str, texts: Optional[List[str]] = None) -> str:
        # Без системной инструкции модель на коротких фразах иногда отвечала
        # рассуждением ('The translation of "X" ... is "Y."')
        # или брала перевод в кавычки — и это целиком попадало в кэш и на экран.
        base = (
            f"You translate user interface strings of a web application "
            f"from {self.source_lang} to {target_lang}.\n"
            "Rules:\n"
            "- Output ONLY the translation: no explanations, no notes, no surrounding quotes.\n"
            "- Keep placeholders like {{0}}, {{1}} exactly as they are; they will be replaced "
            "with values at runtime. Move them only if the target grammar requires it.\n"
            "- Keep HTML tags, emoji, punctuation, brackets and leading/trailing symbols.\n"
            "- Do not use abbreviations from other languages (no English abbreviations inside non-English text).\n"
            "- Use correct spelling and letters of the target language.\n"
            "- Keep the text short, as a UI label, button or message.\n"
            "- If the text must not be translated (a name, code, URL), return it unchanged."
        )
        extra = self._context_lines(texts or [], target_lang)
        return base + ("\n\n" + extra if extra else "")

    def _markdown_prompt(self, target_lang: str, text: str) -> str:
        base = (
            f"You translate a Markdown document of a web application from {self.source_lang} "
            f"to {target_lang}.\n"
            "Rules:\n"
            "- Translate ONLY the given text. Never add anything that is not in it: no titles, "
            "headings, sections, lists, links, tables of contents or explanations. A single line "
            "of plain text must stay a single line of plain text.\n"
            "- Output ONLY the translated Markdown, nothing before or after it, no code fences around it.\n"
            "- Keep the Markdown structure exactly, line by line: the same headings (and only where "
            "the source has them), list items, tables, blank lines, emphasis (**bold**, *italic*), "
            "links and images.\n"
            "- Translate link texts and image alt texts, but never change URLs or file paths.\n"
            "- Keep HTML tags and their attributes unchanged, translate only the visible text inside.\n"
            "- Do not translate code blocks and inline code.\n"
            "- Use natural, correct language; no abbreviations from other languages."
        )
        extra = self._context_lines([text], target_lang)
        return base + ("\n\n" + extra if extra else "")

    @staticmethod
    def _clean(source: str, translated: str) -> str:
        """Снимает лишние кавычки вокруг ответа, если их не было в исходном тексте."""
        t = translated.strip()
        src = source.strip()
        for q_open, q_close in (('"', '"'), ("'", "'"), ("«", "»"), ("“", "”")):
            inner = t[1:-1]
            if (len(t) >= 2 and t.startswith(q_open) and t.endswith(q_close)
                    and q_open not in inner and q_close not in inner
                    and not (src.startswith(q_open) and src.endswith(q_close))):
                t = inner.strip()
                break
        return t

    # ---------- фразы интерфейса ----------
    def translate_single(self, text: str, target_lang: str, feedback: Optional[str] = None) -> Optional[str]:
        user = text
        if feedback:
            user = (f"{text}\n\n(Your previous translation was rejected: {feedback}. "
                    f"Reply with the corrected translation only.)")
        self._last_api_error: Optional[str] = None
        try:
            raw = self._chat([
                {"role": "system", "content": self._system_prompt(target_lang, [text])},
                {"role": "user", "content": user},
            ])
        except RuntimeError as e:
            self._last_api_error = str(e)
            self.last_api_errors.append(str(e))
            logger.warning(f"Single translation failed for text: {text[:50]}... ({e})")
            return None
        return self._clean(text, raw) if raw else None

    def _batch_raw(self, short: List[Dict[str, str]], target_lang: str) -> Dict[str, str]:
        payload = [{"id": i, "text": item["text"]} for i, item in enumerate(short, 1)]
        system = (
            self._system_prompt(target_lang, [it["text"] for it in short])
            + "\n\nYou receive a JSON list of texts. Translate each \"text\" separately "
            "and return only JSON: {\"items\": [{\"id\": 1, \"translated\": \"...\"}]} with the same ids."
        )
        try:
            raw = self._chat(
                [{"role": "system", "content": system},
                 {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                json_mode=True,
            )
        except RuntimeError as e:
            self.last_api_errors.append(f"пакет из {len(short)} фраз: {e}")
            logger.warning(f"Batch translation failed: {e}")
            return {}
        data = extract_json(raw)
        result: Dict[str, str] = {}
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            self.last_api_errors.append(f"пакет из {len(short)} фраз: ответ модели не в формате JSON: {raw[:300]!r}")
            logger.debug("Batch: invalid JSON structure from model")
            return result
        for item in data["items"]:
            if not isinstance(item, dict) or "translated" not in item:
                continue
            try:
                idx = int(item.get("id")) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(short) and item["translated"] is not None:
                result[short[idx]["storage_key"]] = self._clean(short[idx]["text"], str(item["translated"]))
        return result

    def translate_batch(self, items: List[Dict[str, str]], target_lang: str) -> Dict[str, str]:
        self.last_problems = {}
        self.last_details = {}
        self.last_api_errors = []
        if not items:
            return {}

        # Глоссарий проекта первым: фраза целиком есть в глоссарии
        # (аббревиатура, сокращение) — перевод оттуда, ИИ не вызывается.
        result: Dict[str, str] = {}
        if self.glossary is not None:
            for item in items:
                fixed = self.glossary.exact(item["text"], target_lang)
                if fixed:
                    result[item["storage_key"]] = fixed
            items = [it for it in items if it["storage_key"] not in result]
            if not items:
                return result

        short = [it for it in items if len(it["text"]) <= self.LONG_TEXT]
        candidates = self._batch_raw(short, target_lang) if short else {}

        for item in items:
            key, text = item["storage_key"], item["text"]
            detail: Dict[str, Any] = {"text": text}
            translated = candidates.get(key)
            if translated is None:
                translated = self.translate_single(text, target_lang)
                detail["api_error"] = getattr(self, "_last_api_error", None)
            problems = check_phrase(text, translated, target_lang, self.glossary)
            detail.update({"response": translated, "problems": problems})
            if problems:
                # вторая попытка — поштучно и с объяснением, что было не так
                retry = self.translate_single(text, target_lang, feedback="; ".join(problems))
                retry_problems = check_phrase(text, retry, target_lang, self.glossary)
                detail.update({"retry_response": retry, "retry_problems": retry_problems,
                               "retry_api_error": getattr(self, "_last_api_error", None)})
                if not retry_problems or len(retry_problems) < len(problems):
                    translated, problems = retry, retry_problems
            hard = [p for p in problems if not is_soft(p)]
            if problems:
                self.last_details[key] = detail
            if hard:
                self.last_problems[key] = problems
                logger.warning(f"Перевод отклонён [{target_lang}] {text[:60]!r}: {'; '.join(problems)}")
                continue
            if problems:
                # мягкая проблема (глоссарий) после повторной попытки — принимаем, но отмечаем
                self.last_problems[key] = problems
                logger.warning(f"Перевод принят с замечанием [{target_lang}] {text[:60]!r}: {'; '.join(problems)}")
            result[key] = translated
        return result

    # ---------- документы ----------
    def translate_markdown(self, text: str, target_lang: str) -> Optional[str]:
        feedback = None
        self.last_markdown_detail = {"attempts": [], "api_error": None}
        for _ in range(2):
            user = text if not feedback else (
                f"{text}\n\n<!-- Your previous translation was rejected: {feedback}. "
                f"Translate again following all rules. -->"
            )
            try:
                raw = self._chat([
                    {"role": "system", "content": self._markdown_prompt(target_lang, text)},
                    {"role": "user", "content": user},
                ])
            except RuntimeError as e:
                self.last_markdown_detail["api_error"] = str(e)
                logger.warning(f"Markdown translation failed: {e}")
                return None
            raw = raw.strip()
            if raw.startswith("```") and raw.endswith("```") and not text.strip().startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip() if "\n" in raw else raw
            problems = check_markdown(text, raw, target_lang)
            if problems:
                # Страховка: модель любит "оформлять" первую строку заголовком
                # ("# Welcome...") там, где в исходнике простой текст. Если
                # единственная разница в этом — снимаем лишний # и проверяем снова.
                fixed = _strip_added_heading(text, raw)
                if fixed is not None and not check_markdown(text, fixed, target_lang):
                    raw, problems = fixed, []
            self.last_markdown_detail["attempts"].append({"response": raw, "problems": problems})
            if not problems:
                return raw
            feedback = "; ".join(problems)
            logger.warning(f"Кусок документа отклонён [{target_lang}]: {feedback}")
        return None


_HEADING_PREFIX_RE = re.compile(r"^\s{0,3}#{1,6}\s+")


def _strip_added_heading(source: str, translated: str) -> Optional[str]:
    """Убирает '#' с первой строки перевода, если в исходнике первая строка — не заголовок."""
    src_first = source.lstrip().split("\n", 1)[0]
    tr = translated.lstrip()
    if _HEADING_PREFIX_RE.match(src_first) or not _HEADING_PREFIX_RE.match(tr):
        return None
    return _HEADING_PREFIX_RE.sub("", tr, count=1)


# ---------- Фабрика для создания провайдеров ----------
def get_translator(provider: str = "openai", config: Optional[Dict[str, Any]] = None) -> BaseTranslator:
    """
    Возвращает экземпляр переводчика для указанного провайдера.
    provider: "openai" (пока только он)
    config: словарь с параметрами (api_key, model, source_lang, glossary, context и т.д.)
    """
    if config is None:
        config = {}
    provider = (provider or os.getenv("AUTO_I18N_AI_PROVIDER", "openai")).lower()
    if provider == "openai":
        return OpenAITranslator(config)
    raise ValueError(f"Unsupported AI provider: {provider}")
