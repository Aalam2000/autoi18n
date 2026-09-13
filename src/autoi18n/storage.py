# src/autoi18n/storage.py
"""
Хранилище кэша переводов и очередей на перевод.

Важное упрощение относительно v1: отдельного файла-реестра (_keys.json)
больше нет. Файл исходного языка (cache_dir/{source_lang}.json) сам по себе
и есть реестр всех известных фраз проекта: {hash(текст): текст}. Файлы
целевых языков имеют тот же формат: {тот же hash: перевод}. Экстрактор
сверяется именно с файлом исходного языка, а не с отдельным маппингом —
поэтому отдельная сущность-реестр не нужна (см. обсуждение архитектуры v2).

Добавлен простой in-memory кэш с инвалидацией по mtime файла: translate_html/
translate_dict и т.п. могут вызываться на каждый запрос, и перечитывать с
диска JSON каждый раз — лишняя работа при небольшом словаре и лишний риск
при большом трафике.
"""
import os
from typing import Dict, Optional, Tuple

from .utils import safe_json_load, safe_json_save, normalize_lang, normalize_backend_dict_name


class Storage:
    def __init__(self, cache_dir: str, source_lang: str):
        self.cache_dir = cache_dir
        self.source_lang = normalize_lang(source_lang)
        self._mem_cache: Dict[str, Tuple[Optional[float], Dict]] = {}

    # ---------- пути ----------
    def _shared_cache_path(self, lang: str) -> str:
        return os.path.join(self.cache_dir, f"{lang}.json")

    def _shared_pending_path(self) -> str:
        return os.path.join(self.cache_dir, "_pending.json")

    def _backend_dir(self) -> str:
        return os.path.join(self.cache_dir, "backend")

    def _backend_cache_path(self, dict_name: str, lang: str) -> str:
        dict_name = normalize_backend_dict_name(dict_name)
        return os.path.join(self._backend_dir(), f"{dict_name}.{lang}.json")

    def _backend_pending_path(self, dict_name: str) -> str:
        dict_name = normalize_backend_dict_name(dict_name)
        filename = "_pending_bot.json" if dict_name == "bot" else "_pending_system.json"
        return os.path.join(self._backend_dir(), filename)

    def _cache_path(self, dict_type: str, lang: str) -> str:
        if dict_type == "shared":
            return self._shared_cache_path(lang)
        elif dict_type in ("bot", "system"):
            return self._backend_cache_path(dict_type, lang)
        raise ValueError(f"Unknown dict_type: {dict_type}")

    def _pending_path(self, dict_type: str) -> str:
        if dict_type == "shared":
            return self._shared_pending_path()
        elif dict_type in ("bot", "system"):
            return self._backend_pending_path(dict_type)
        raise ValueError(f"Unknown dict_type: {dict_type}")

    # ---------- in-memory кэш с инвалидацией по mtime ----------
    def _cached_load(self, path: str) -> Dict:
        try:
            mtime: Optional[float] = os.path.getmtime(path)
        except OSError:
            mtime = None

        cached = self._mem_cache.get(path)
        if cached is not None and cached[0] == mtime:
            return cached[1]

        data = safe_json_load(path)
        self._mem_cache[path] = (mtime, data)
        return data

    def _invalidate(self, path: str) -> None:
        self._mem_cache.pop(path, None)

    # ---------- общий интерфейс для кэша ----------
    def load_cache(self, dict_type: str, lang: str) -> Dict[str, str]:
        lang = normalize_lang(lang, source_lang=self.source_lang)
        return self._cached_load(self._cache_path(dict_type, lang))

    def save_cache(self, dict_type: str, lang: str, data: Dict[str, str]) -> None:
        lang = normalize_lang(lang, source_lang=self.source_lang)
        path = self._cache_path(dict_type, lang)
        safe_json_save(path, data)
        self._invalidate(path)

    # ---------- очереди ----------
    def load_pending(self, dict_type: str) -> Dict[str, Dict[str, Dict[str, str]]]:
        data = self._cached_load(self._pending_path(dict_type))
        result: Dict[str, Dict[str, Dict[str, str]]] = {}
        for lang, items in data.items():
            if not isinstance(lang, str) or not isinstance(items, dict):
                continue
            norm_lang = normalize_lang(lang, source_lang=self.source_lang)
            bucket = result.setdefault(norm_lang, {})
            for storage_key, meta in items.items():
                if not isinstance(storage_key, str):
                    continue
                if isinstance(meta, dict):
                    text = str(meta.get("text", storage_key))
                    prompt_type = str(meta.get("prompt_type", "normal"))
                else:
                    text = storage_key
                    prompt_type = "normal"
                bucket[storage_key] = {"text": text, "prompt_type": prompt_type}
        return result

    def save_pending(self, dict_type: str, data: Dict[str, Dict[str, Dict[str, str]]]) -> None:
        cleaned = {}
        for lang, items in data.items():
            if not items:
                continue
            norm_lang = normalize_lang(lang, source_lang=self.source_lang)
            cleaned[norm_lang] = items
        path = self._pending_path(dict_type)
        safe_json_save(path, cleaned)
        self._invalidate(path)
