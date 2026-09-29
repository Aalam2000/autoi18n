# src/autoi18n/glossary.py
"""
Механизм работы с глоссарием ПРОЕКТА. Библиотека не содержит никаких
терминов — это данные проекта: файл autoi18n.json в рабочей папке
проекта (см. config.py — там же doc_paths и scan_paths). Пример:

    {
      "context": "CRM для отдела продаж.",
      "terms": {
        "КП":             {"en": "Quote",       "de": "Angebot"},
        "Менеджер*":      {"en": "Manager",     "de": "Manager"},
        "баз* клиент*":   {"en": "Client base", "de": "Kundenbasis"}
      }
    }

Ключ термина — текст на исходном языке. `*` внутри ключа означает
"любое окончание слова" ("Менеджер*" совпадает с "менеджера",
"менеджеров"). Без `*` термин ищется как отдельное слово.

Термин используется так:
1. фраза интерфейса ЦЕЛИКОМ совпадает с ключом без `*` (аббревиатура,
   сокращение: "КП") — перевод берётся из глоссария, ИИ не вызывается
   (exact()); такой перевод имеет приоритет и над уже сохранённым;
2. иначе термин передаётся модели в запросе ("используй именно такой
   перевод");
3. и проверяется в готовом переводе (см. quality.py) — без учёта
   регистра и окончания (сравнивается основа перевода термина).

Файл перечитывается при изменении (mtime) — править можно на ходу.
"""
import os
import re
from typing import Dict, List, Optional, Tuple

from .utils import safe_json_load


def fold(text: str, lang: str = "") -> str:
    """Нижний регистр с учётом турецкой/азербайджанской I/İ."""
    if lang.split("-")[0] in ("az", "tr"):
        text = text.replace("I", "ı").replace("İ", "i")
    return text.lower()


def term_stem(term: str, lang: str = "") -> str:
    """Основа перевода термина для проверки: без двух последних букв у
    длинных слов — чтобы "Kundenbasis" совпадало с "Kundenbasen"."""
    t = fold(term.strip(), lang)
    return t[:-2] if len(t) > 5 else t


def _key_regex(key: str) -> "re.Pattern[str]":
    parts = [re.escape(p) for p in key.strip().lower().split("*")]
    body = r"\w*".join(parts)
    return re.compile(r"(?<!\w)" + body + r"(?!\w)" if not key.strip().endswith("*") else r"(?<!\w)" + body)


class Glossary:
    def __init__(self, path: Optional[str] = None):
        self.path = path
        self._mtime: Optional[float] = None
        self.context: str = ""
        self._terms: List[Tuple[str, "re.Pattern[str]", Dict[str, str]]] = []
        self._exact: Dict[str, Dict[str, str]] = {}

    def _reload(self) -> None:
        if not self.path:
            return
        try:
            mtime: Optional[float] = os.path.getmtime(self.path)
        except OSError:
            mtime = None
        if mtime == self._mtime:
            return
        self._mtime = mtime
        data = safe_json_load(self.path) if mtime is not None else {}
        self.context = str(data.get("context", "") or "").strip()
        terms = []
        for key, translations in (data.get("terms") or {}).items():
            if not isinstance(key, str) or not key.strip() or not isinstance(translations, dict):
                continue
            clean = {str(l).lower(): str(v) for l, v in translations.items() if str(v).strip()}
            terms.append((key, _key_regex(key), clean))
        self._terms = terms
        # точные записи (без *) для перевода фразы целиком без ИИ
        self._exact = {" ".join(k.split()): t for k, _, t in terms if "*" not in k}

    def exact(self, text: str, lang: str) -> Optional[str]:
        """Перевод фразы ЦЕЛИКОМ из глоссария (ключ без `*`, совпадение с
        точностью до пробелов по краям), либо None."""
        self._reload()
        translations = self._exact.get(" ".join(str(text).split()))
        if not translations:
            return None
        lang = lang.lower()
        return translations.get(lang) or translations.get(lang.split("-")[0])

    def exact_items(self):
        """[(исходный_текст, {lang: перевод})] — все точные записи."""
        self._reload()
        return list(self._exact.items())

    def get_context(self) -> str:
        self._reload()
        return self.context

    def terms_for(self, text: str, lang: str) -> List[Tuple[str, str]]:
        """[(ключ, перевод_термина)] для терминов, встречающихся в text."""
        self._reload()
        lang = lang.lower()
        low = text.lower()
        found = []
        for key, rx, translations in self._terms:
            target = translations.get(lang) or translations.get(lang.split("-")[0])
            if target and rx.search(low):
                found.append((key.replace("*", ""), target))
        return found

    def violations(self, source: str, translated: str, lang: str) -> List[str]:
        """Термины глоссария, которые есть в исходнике, но не соблюдены в переводе."""
        out = []
        folded = fold(translated, lang)
        for key, target in self.terms_for(source, lang):
            if term_stem(target, lang) not in folded:
                out.append(f"термин '{key}' должен переводиться как '{target}'")
        return out
