# src/autoi18n/config.py
"""
Конфигурация autoi18n.

Ключевые решения (см. обсуждение архитектуры v2):
- source_lang и target_langs берутся из .env — единственный источник истины.
- target_langs можно менять "на лету" через Config.add_target_lang(): это дописывает
  .env (чтобы список пережил рестарт) и сразу обновляет os.environ текущего процесса
  (чтобы изменение было видно немедленно, без ожидания рестарта или отдельного
  файла-сторожа). Саму трансляцию нового языка (перевод всего файла исходного
  языка) выполняет Translator.add_target_lang() — эта функция только управляет
  списком языков, не переводом.
- scan_paths задаются как явные пары (папка + список расширений), а не как
  glob-паттерны с {a,b,c} — Python-модуль glob не поддерживает brace-expansion,
  и такие паттерны раньше молча находили ноль файлов.
- Файл проекта autoi18n.json (в рабочей папке; путь можно поменять через
  AUTO_I18N_PROJECT_FILE) — лежит в git проекта, поэтому одинаков на всех
  серверах и виден и приложению, и CLI без правок .env:
      {"context": "...", "terms": {...},
       "scan_paths": [{"path": "frontend/src", "type": "js"}, {"path": "templates", "type": "html"}],
       "doc_paths": ["docs"]}
  context и terms — см. glossary.py.
- scan_paths — какие папки проекта сканировать на фразы интерфейса.
  Задаёт проект (autoi18n.json); type: "js" (.js .jsx .ts .tsx), "html"
  (.html) или "auto"; extensions можно указать явно. Если проект не задал —
  используется DEFAULT_SCAN_PATHS (типовая структура backend+frontend).
- attributes — дополнительные атрибуты/свойства, значение которых —
  видимый пользователю текст (например, свои подсказки "data-tip", "tip").
  К стандартным (title, placeholder, alt, aria-label, label) добавляются
  из autoi18n.json проекта; учитываются сканером и рантаймом.
- doc_paths — папки с документами (.md), которые переводятся целиком
  (см. documents.py). Приоритет: Translator(doc_paths=...) >
  AUTO_I18N_DOC_PATHS (через запятую) > autoi18n.json. По умолчанию пусто:
  документы переводятся только из явно указанных папок.
- glossary_path — файл с context/terms. Приоритет: Translator(glossary_path=...)
  > AUTO_I18N_GLOSSARY > autoi18n.json (если есть) > <cache_dir>/_glossary.json.
- max_attempts — сколько раз пытаться перевести фразу/документ, перевод
  которых не проходит проверку качества (AUTO_I18N_MAX_ATTEMPTS, по умолчанию 3).
"""
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List

from dotenv import load_dotenv, set_key

from .utils import normalize_lang, parse_target_langs, safe_json_load

ATTR_NAME_RE = re.compile(r"^[A-Za-z_][-A-Za-z0-9_:.]*$")

TYPE_EXTENSIONS: Dict[str, List[str]] = {
    "js": [".js", ".jsx", ".ts", ".tsx"],
    "html": [".html"],
    "auto": [".js", ".jsx", ".ts", ".tsx", ".html"],
}

DEFAULT_SCAN_PATHS: List[Dict[str, object]] = [
    {"path": "frontend/src", "extensions": [".js", ".jsx", ".ts", ".tsx"], "type": "js"},
    {"path": "src", "extensions": [".js", ".jsx", ".ts", ".tsx"], "type": "js"},
    {"path": "app", "extensions": [".js", ".jsx", ".ts", ".tsx"], "type": "js"},
    {"path": "templates", "extensions": [".html"], "type": "html"},
    {"path": "app/templates", "extensions": [".html"], "type": "html"},
    {"path": "backend/templates", "extensions": [".html"], "type": "html"},
]


@dataclass
class Config:
    env_path: str = ".env"
    source_lang: str = field(init=False)
    cache_dir: str = field(init=False)
    scan_paths: List[Dict[str, object]] = field(init=False)
    attributes: List[str] = field(init=False)
    doc_paths: List[str] = field(init=False)
    glossary_path: str = field(init=False)
    max_attempts: int = field(init=False)

    def __post_init__(self) -> None:
        load_dotenv(self.env_path)
        self.source_lang = normalize_lang(os.getenv("SOURCE_LANG", "ru"))
        self.cache_dir = os.getenv("AUTO_I18N_CACHE_DIR", "./translations")
        project_file = os.getenv("AUTO_I18N_PROJECT_FILE", "autoi18n.json")
        project = safe_json_load(project_file) if os.path.isfile(project_file) else {}
        self.scan_paths = self._load_scan_paths(project.get("scan_paths"))
        self.attributes = [
            str(a).strip() for a in (project.get("attributes") or [])
            if isinstance(a, str) and ATTR_NAME_RE.match(str(a).strip())
        ]

        env_docs = [p.strip() for p in os.getenv("AUTO_I18N_DOC_PATHS", "").split(",") if p.strip()]
        file_docs = [str(p).strip() for p in (project.get("doc_paths") or []) if str(p).strip()]
        self.doc_paths = env_docs or file_docs

        self.glossary_path = os.getenv("AUTO_I18N_GLOSSARY", "") or (
            project_file if os.path.isfile(project_file) else ""
        )
        try:
            self.max_attempts = max(1, int(os.getenv("AUTO_I18N_MAX_ATTEMPTS", "3")))
        except ValueError:
            self.max_attempts = 3

    @staticmethod
    def _load_scan_paths(value) -> List[Dict[str, object]]:
        """scan_paths из файла проекта; нет/пусто/ошибка — DEFAULT_SCAN_PATHS."""
        result: List[Dict[str, object]] = []
        for entry in value or []:
            if isinstance(entry, str):
                entry = {"path": entry, "type": "auto"}
            if not isinstance(entry, dict) or not str(entry.get("path", "")).strip():
                continue
            kind = str(entry.get("type", "auto")).strip().lower()
            if kind not in TYPE_EXTENSIONS:
                kind = "auto"
            exts = entry.get("extensions") or TYPE_EXTENSIONS[kind]
            exts = [e if str(e).startswith(".") else f".{e}" for e in exts]
            result.append({"path": str(entry["path"]).strip(), "extensions": exts, "type": kind})
        return result or list(DEFAULT_SCAN_PATHS)

    def get_target_langs(self) -> List[str]:
        """
        Возвращает актуальный список активных целевых языков.
        Перечитывает .env при каждом вызове (override=True) — так воркер видит
        изменения, сделанные через add_target_lang() в этом же процессе или
        руками между циклами, без необходимости отдельного механизма отслеживания.
        """
        load_dotenv(self.env_path, override=True)
        raw = os.getenv("AUTO_I18N_TARGET_LANGS", "")
        return parse_target_langs(raw, source_lang=self.source_lang)

    def add_target_lang(self, lang: str) -> bool:
        """
        Регистрирует новый целевой язык в конфигурации.

        Делает ровно две вещи:
        1. дописывает язык в .env (AUTO_I18N_TARGET_LANGS) — переживает рестарт;
        2. обновляет os.environ текущего процесса — изменение видно сразу.

        Не выполняет перевод — это ответственность Translator.add_target_lang(),
        который вызывает этот метод, а затем прогоняет перевод всего файла
        исходного языка на новый язык.

        Возвращает True, если язык был добавлен; False, если он уже есть в списке
        (или совпадает с исходным языком) — в этом случае ничего не меняется.
        """
        lang = normalize_lang(lang, source_lang=self.source_lang)
        current = self.get_target_langs()
        if lang == self.source_lang or lang in current:
            return False
        updated = current + [lang]
        value = ",".join(updated)
        set_key(self.env_path, "AUTO_I18N_TARGET_LANGS", value)
        os.environ["AUTO_I18N_TARGET_LANGS"] = value
        return True
