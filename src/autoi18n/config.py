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
"""
import os
from dataclasses import dataclass, field
from typing import Dict, List

from dotenv import load_dotenv, set_key

from .utils import normalize_lang, parse_target_langs

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

    def __post_init__(self) -> None:
        load_dotenv(self.env_path)
        self.source_lang = normalize_lang(os.getenv("SOURCE_LANG", "ru"))
        self.cache_dir = os.getenv("AUTO_I18N_CACHE_DIR", "./translations")
        self.scan_paths = self._load_scan_paths()

    def _load_scan_paths(self) -> List[Dict[str, object]]:
        # Явный scan_paths через env/аргумент конструктора Translator — задел на будущее,
        # пока используем проверенный дефолт (типовая структура backend+frontend проекта).
        return list(DEFAULT_SCAN_PATHS)

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
