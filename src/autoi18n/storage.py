# src/autoi18n/storage.py
import os
from typing import Dict, Optional, List
from .utils import (
    safe_json_load,
    safe_json_save,
    normalize_lang,
    normalize_backend_dict_name,
    KEYS_MAPPING_FILENAME,
)

class Storage:
    def __init__(self, cache_dir: str, source_lang: str):
        self.cache_dir = cache_dir
        self.source_lang = normalize_lang(source_lang)

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

    # ---------- общий интерфейс для кэша ----------
    def load_cache(self, dict_type: str, lang: str) -> Dict[str, str]:
        lang = normalize_lang(lang, source_lang=self.source_lang)
        if dict_type == "shared":
            path = self._shared_cache_path(lang)
        elif dict_type in ("bot", "system"):
            path = self._backend_cache_path(dict_type, lang)
        else:
            raise ValueError(f"Unknown dict_type: {dict_type}")
        return safe_json_load(path)

    def save_cache(self, dict_type: str, lang: str, data: Dict[str, str]) -> None:
        lang = normalize_lang(lang, source_lang=self.source_lang)
        if dict_type == "shared":
            path = self._shared_cache_path(lang)
        elif dict_type in ("bot", "system"):
            path = self._backend_cache_path(dict_type, lang)
        else:
            raise ValueError(f"Unknown dict_type: {dict_type}")
        safe_json_save(path, data)

    # ---------- очереди ----------
    def load_pending(self, dict_type: str) -> Dict[str, Dict[str, Dict[str, str]]]:
        if dict_type == "shared":
            path = self._shared_pending_path()
        elif dict_type in ("bot", "system"):
            path = self._backend_pending_path(dict_type)
        else:
            raise ValueError(f"Unknown dict_type: {dict_type}")
        data = safe_json_load(path)
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
        if dict_type == "shared":
            path = self._shared_pending_path()
        elif dict_type in ("bot", "system"):
            path = self._backend_pending_path(dict_type)
        else:
            raise ValueError(f"Unknown dict_type: {dict_type}")
        safe_json_save(path, cleaned)