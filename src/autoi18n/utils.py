# src/autoi18n/utils.py
import json
import os
import re
import tempfile
import hashlib
import logging
from glob import glob
from typing import Any, Dict, List, Optional, Tuple

WHITESPACE_ONLY_RE = re.compile(r"^\s*$")
NUMBER_LIKE_RE = re.compile(r"^[\d\s\.,:/\-]+$")
HEX_LIKE_RE = re.compile(r"^[A-Fa-f0-9\-]{8,}$")
PURE_LATIN_TECH_RE = re.compile(r"^[A-Za-z0-9_\-\s\.:/@#%+=]+$")

# Константы для хранения маппинга "текст -> хеш"
KEYS_MAPPING_FILENAME = "_keys.json"

# ---------- Логирование (отключаемое) ----------
logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


def text_hash(text: str) -> str:
    """Возвращает SHA1 хеш текста (используется как ключ в кэше)."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def load_keys_mapping(cache_dir: str) -> Dict[str, str]:
    """Загружает маппинг {text: hash} из cache_dir/_keys.json."""
    path = os.path.join(cache_dir, KEYS_MAPPING_FILENAME)
    data = safe_json_load(path)
    if isinstance(data, dict):
        return data
    return {}


def save_keys_mapping(cache_dir: str, mapping: Dict[str, str]) -> None:
    """Сохраняет маппинг {text: hash} в cache_dir/_keys.json."""
    path = os.path.join(cache_dir, KEYS_MAPPING_FILENAME)
    safe_json_save(path, mapping)


# ---------- остальные утилиты (без изменений) ----------

def split_preserve_whitespace(text: str) -> Tuple[str, str, str]:
    match = re.match(r"^(\s*)(.*?)(\s*)$", text, flags=re.DOTALL)
    if not match:
        return "", text, ""
    return match.group(1), match.group(2), match.group(3)


def should_translate(text: str, attr_name: str = None) -> bool:
    if text is None:
        return False
    raw = text
    text = text.strip()
    if not text:
        return False
    if WHITESPACE_ONLY_RE.fullmatch(raw):
        return False
    if text.isdigit():
        return False
    if NUMBER_LIKE_RE.fullmatch(text):
        return False
    if HEX_LIKE_RE.fullmatch(text):
        return False
    if PURE_LATIN_TECH_RE.fullmatch(text):
        if attr_name in {"placeholder", "title", "alt", "aria-label", "value"}:
            words = text.split()
            if len(words) >= 2:
                return True
        return False
    return True


def should_translate_ui_text(text: str) -> bool:
    if text is None:
        return False
    raw = str(text)
    text = raw.strip()
    if not text:
        return False
    if WHITESPACE_ONLY_RE.fullmatch(raw):
        return False
    if text.isdigit():
        return False
    if NUMBER_LIKE_RE.fullmatch(text):
        return False
    if HEX_LIKE_RE.fullmatch(text):
        return False
    return True

# Символы, которых не бывает в UI-тексте из JS-кода (признак служебной строки)
JS_FORBIDDEN_CHARS_RE = re.compile(r"[{}<>`=$\n\r\t\\#]")
# Строки, начинающиеся со служебных символов (URL, id, класс, hex-цвет)
JS_BAD_START_RE = re.compile(r"^[/#.]")
# Технические значения: 12px, 1.5rem, rgba(...), #fff, одиночное латинское слово
JS_TECH_VALUE_RE = re.compile(
    r"^(?:[\d.]+(?:px|em|rem|%|vh|vw|s|ms|deg|fr)?|#[0-9A-Fa-f]{3,8}|rgba?\([^)]*\)|[A-Za-z][A-Za-z0-9_\-]*)$"
)

def should_translate_js_text(text: str) -> bool:
    if text is None:
        return False
    t = str(text).strip()
    if len(t) < 2 or len(t) > 200:
        return False
    if JS_FORBIDDEN_CHARS_RE.search(t):
        return False
    if JS_BAD_START_RE.match(t):
        return False
    if t.isdigit() or NUMBER_LIKE_RE.fullmatch(t) or HEX_LIKE_RE.fullmatch(t):
        return False
    if JS_TECH_VALUE_RE.fullmatch(t):
        return False
    if not re.search(r"[A-Za-zА-Яа-яЁё]", t):
        return False
    return True

def should_translate_backend_text(text: str) -> bool:
    return should_translate_ui_text(text)


def normalize_lang(lang: str, source_lang: str = "ru") -> str:
    if not lang:
        return source_lang
    lang = str(lang).strip().lower().replace("_", "-")
    if not lang:
        return source_lang
    return lang


def normalize_backend_dict_name(dict_name: Optional[str]) -> str:
    value = str(dict_name or "bot").strip().lower()
    if value not in {"bot", "system"}:
        raise ValueError(f"dict_name must be one of: bot, system")
    return value


def parse_target_langs(value: Any, source_lang: str = "ru") -> List[str]:
    if value is None:
        value = os.getenv("AUTO_I18N_TARGET_LANGS", "")
    if isinstance(value, str):
        raw_items = [item.strip() for item in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        raw_items = [str(item).strip() for item in value]
    else:
        raw_items = [str(value).strip()]
    result: List[str] = []
    for item in raw_items:
        if not item:
            continue
        lang = normalize_lang(item, source_lang=source_lang)
        if lang not in result and lang != source_lang:
            result.append(lang)
    return result


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def parse_json_or_csv_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    raw = str(value).strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except Exception:
            pass
    return [item.strip() for item in raw.split(",") if item.strip()]


def resolve_glob_paths(patterns: List[str]) -> List[str]:
    results = []
    seen = set()
    for pattern in patterns:
        for path in sorted(glob(pattern, recursive=True)):
            if path not in seen and os.path.isfile(path):
                seen.add(path)
                results.append(path)
    return results


def build_lang_chain(target_lang: str, source_lang: str) -> List[str]:
    target_lang = normalize_lang(target_lang, source_lang=source_lang)
    source_lang = normalize_lang(source_lang, source_lang=source_lang)
    chain: List[str] = []
    def add(item: str) -> None:
        if item and item not in chain:
            chain.append(item)
    add(target_lang)
    if "-" in target_lang:
        add(target_lang.split("-")[0])
    add(source_lang)
    return chain


def safe_json_load(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def safe_json_save(path: str, data: Dict[str, Any]) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".tmp_autoi18n_",
        suffix=".json",
        dir=directory,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def extract_json(text: str) -> Optional[Any]:
    """
    Извлекает JSON из текста (например, из ответа модели).
    Возвращает десериализованный объект (dict или list) или None, если JSON не найден.
    """
    if not text or not text.strip():
        logger.debug("extract_json: empty input")
        return None

    cleaned = text.strip()
    # Удаляем маркдаун-обёртку
    cleaned = re.sub(r'^```json\s*', '', cleaned)
    cleaned = re.sub(r'\s*```$', '', cleaned)

    decoder = json.JSONDecoder()

    # Попытка распарсить весь текст
    try:
        obj, idx = decoder.raw_decode(cleaned)
        return obj
    except json.JSONDecodeError:
        pass

    # Попытка найти JSON-объект, начиная с первой фигурной скобки
    start = cleaned.find("{")
    if start != -1:
        try:
            obj, idx = decoder.raw_decode(cleaned[start:])
            return obj
        except json.JSONDecodeError:
            pass

    # Попытка найти JSON-массив, начиная с первой квадратной скобки
    start = cleaned.find("[")
    if start != -1:
        try:
            obj, idx = decoder.raw_decode(cleaned[start:])
            return obj
        except json.JSONDecodeError:
            pass

    logger.debug("extract_json: JSON not found in text")
    return None


def deep_copy_json_like(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))

def replace_translatable_strings(html: str, translations: Dict[str, str]) -> str:
    """
    Заменяет все строки в кавычках (включая шаблонные) внутри HTML на переводы.
    Для шаблонных строк разбивает по ${...} и заменяет текстовые части,
    обрезая пробелы при поиске в словаре.
    Для обычных строк также обрезает пробелы.
    """
    if not translations:
        return html

    placeholder_re = re.compile(r'(\$\{[^}]*\})')
    jinja_placeholder_re = re.compile(r'(\{\{[^}]*\}\})')

    def _replace_template_partially(content: str) -> str:
        parts = placeholder_re.split(content)
        new_parts = []
        for part in parts:
            if part.startswith('${'):
                new_parts.append(part)
            else:
                stripped = part.strip()
                if stripped in translations:
                    # Сохраняем исходные пробелы вокруг сегмента
                    leading = part[: len(part) - len(part.lstrip())]
                    trailing = part[len(part.rstrip()):]
                    new_parts.append(f"{leading}{translations[stripped]}{trailing}")
                else:
                    new_parts.append(part)
        return ''.join(new_parts)

    def _replace_template_full(content: str) -> str:
        parts = placeholder_re.split(content)
        text_parts = []
        placeholders = []
        placeholder_index = 0
        for part in parts:
            if part.startswith('${'):
                placeholders.append(part)
                text_parts.append(f"{{{{{placeholder_index}}}}}")
                placeholder_index += 1
            else:
                text_parts.append(part)
        pattern = ''.join(text_parts).strip()
        translated = translations.get(pattern)
        if not translated:
            return content
        for i, expr in enumerate(placeholders):
            translated = translated.replace(f"{{{{{i}}}}}", expr)
        return translated

    def _replace_jinja_text_full(content: str) -> str:
        parts = jinja_placeholder_re.split(content)
        text_parts = []
        placeholders = []
        placeholder_index = 0
        for part in parts:
            if part.startswith('{{'):
                placeholders.append(part)
                text_parts.append(f"{{{{{placeholder_index}}}}}")
                placeholder_index += 1
            else:
                text_parts.append(part)
        pattern = ''.join(text_parts).strip()
        translated = translations.get(pattern)
        if not translated:
            return content
        for i, expr in enumerate(placeholders):
            translated = translated.replace(f"{{{{{i}}}}}", expr)
        return translated

    def _replace_plain_text_match(text: str) -> str:
        full_replaced = _replace_jinja_text_full(text)
        if full_replaced != text:
            return full_replaced
        stripped = text.strip()
        if stripped in translations:
            leading = text[: len(text) - len(text.lstrip())]
            trailing = text[len(text.rstrip()):]
            return f"{leading}{translations[stripped]}{trailing}"
        return text

    def replacer(match):
        quote = match.group(1)      # ', " или `
        content = match.group(2)    # содержимое строки

        if quote == '`':
            # Сначала пытаемся заменить весь шаблон с плейсхолдерами,
            # затем применяем частичную замену как fallback.
            full_replaced = _replace_template_full(content)
            if full_replaced != content:
                return f'`{full_replaced}`'
            return f'`{_replace_template_partially(content)}`'
        else:
            # Обычная строка в кавычках
            stripped = content.strip()
            if stripped in translations:
                return f'{quote}{translations[stripped]}{quote}'
            return match.group(0)

    pattern = re.compile(r"(['\"`])(.*?)\1", re.DOTALL)
    html = pattern.sub(replacer, html)

    # Переводим текстовые узлы HTML, чтобы покрыть видимый текст вне кавычек.
    def text_node_replacer(match):
        text = match.group(1)
        replaced = _replace_plain_text_match(text)
        return f">{replaced}<"

    text_node_pattern = re.compile(r">([^<]+)<", re.DOTALL)
    return text_node_pattern.sub(text_node_replacer, html)