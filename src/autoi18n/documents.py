# src/autoi18n/documents.py
"""
Перевод документов (.md) целиком.

В отличие от фраз интерфейса (которые попадают в общие файлы
translations/<lang>.json), документ переводится ЦЕЛИКОМ, и перевод
кладётся ФАЙЛОМ рядом с исходником:

    help/teacher.md      <- исходник (на исходном языке проекта)
    help/teacher.en.md   <- перевод, создаёт библиотека
    help/teacher.az.md

Какие папки считаются папками документов — задаётся явно
(Translator(doc_paths=[...]) или AUTO_I18N_DOC_PATHS в .env). Никакие
другие .md в проекте не трогаются.

Первая строка перевода — служебный HTML-комментарий:
    <!-- autoi18n: source=teacher.md lang=en sha1=<хеш исходника> -->
По хешу видно, устарел ли перевод (исходник поменяли) — отдельного
файла-манифеста не нужно. По этой же строке библиотека отличает свой
перевод от исходника. При показе строка не видна (HTML-комментарий),
а get_text() её убирает.

Длинный документ режется на куски по абзацам (блоки кода не режутся),
каждый кусок переводится и проверяется отдельно (quality.check_markdown).
Если хоть один кусок не прошёл проверку — файл перевода не пишется
(частичный перевод хуже, чем исходник), документ получает попытку в
_docs_failed.json; после max_attempts неудачных попыток для той же
версии исходника он больше не отправляется в ИИ до изменения исходника
или `autoi18n docs --force`.
"""
import hashlib
import logging
import os
import re
import tempfile
from typing import Callable, Dict, List, Optional, Tuple

from .extractor.file_scanner import IGNORED_DIRS
from .report import clip
from .utils import safe_json_load, safe_json_save

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

DOC_EXTENSIONS = (".md",)
MARKER_RE = re.compile(r"^<!--\s*autoi18n:\s*(.*?)\s*-->[ \t]*\r?\n?")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s")
_LETTER_RE = re.compile(r"[^\W\d_]")
CHUNK_LIMIT = 3000


def text_sha1(text: str) -> str:
    return hashlib.sha1(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def parse_marker(text: str) -> Optional[Dict[str, str]]:
    m = MARKER_RE.match(text)
    if not m:
        return None
    return dict(part.split("=", 1) for part in m.group(1).split() if "=" in part)


def strip_marker(text: str) -> str:
    return MARKER_RE.sub("", text, count=1)


def copy_path(source_path: str, lang: str) -> str:
    base, ext = os.path.splitext(source_path)
    return f"{base}.{lang}{ext}"


def is_translated_copy(path: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return parse_marker(f.readline()) is not None
    except OSError:
        return False


def find_source_documents(doc_paths: List[str]) -> List[str]:
    result: List[str] = []
    for root in doc_paths:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.startswith(".")]
            for name in filenames:
                if not name.lower().endswith(DOC_EXTENSIONS):
                    continue
                path = os.path.join(dirpath, name)
                if not is_translated_copy(path):
                    result.append(path)
    return sorted(set(result))


def split_markdown(text: str, limit: int = CHUNK_LIMIT) -> List[str]:
    """Режет Markdown на куски по пустым строкам; блоки кода не режет;
    новый кусок начинается с заголовка, если текущий уже не маленький."""
    blocks: List[str] = []
    current: List[str] = []
    in_fence = False
    for line in text.replace("\r\n", "\n").split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        if not in_fence and not line.strip():
            if current:
                blocks.append("\n".join(current))
                current = []
            continue
        current.append(line)
    if current:
        blocks.append("\n".join(current))

    chunks: List[str] = []
    buf: List[str] = []
    size = 0
    for block in blocks:
        starts_heading = bool(_HEADING_RE.match(block))
        if buf and (size + len(block) > limit or (starts_heading and size > limit // 2)):
            chunks.append("\n\n".join(buf))
            buf, size = [], 0
        buf.append(block)
        size += len(block) + 2
    if buf:
        chunks.append("\n\n".join(buf))
    return chunks


def _atomic_write(path: str, text: str) -> None:
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tmp_autoi18n_", suffix=".md", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


class DocumentTranslator:
    def __init__(self, cache_dir: str, source_lang: str, doc_paths: List[str], max_attempts: int = 3,
                 version: str = ""):
        self.cache_dir = cache_dir
        self.source_lang = source_lang
        self.doc_paths = list(doc_paths or [])
        self.max_attempts = max_attempts
        # Попытки считаются для пары (версия исходника, версия библиотеки):
        # после обновления библиотеки (исправлен промпт/проверка) документ,
        # исчерпавший попытки, снова пробуется.
        self.version = version

    def _attempts(self, entry: Optional[Dict], sha: str) -> int:
        if not entry or entry.get("sha1") != sha or entry.get("version", "") != self.version:
            return 0
        return int(entry.get("attempts", 0))

    # ---------- состояние неудачных попыток ----------
    def _failed_path(self) -> str:
        return os.path.join(self.cache_dir, "_docs_failed.json")

    @staticmethod
    def _key(path: str, lang: str) -> str:
        return f"{os.path.relpath(path).replace(os.sep, '/')}|{lang}"

    def _load_failed(self) -> Dict[str, Dict]:
        return safe_json_load(self._failed_path())

    def _save_failed(self, data: Dict[str, Dict]) -> None:
        safe_json_save(self._failed_path(), data)

    # ---------- статус ----------
    def status(self, target_langs: List[str]) -> List[Dict[str, object]]:
        """[{source, lang, copy, state, attempts}] — state: ok | missing | stale | failed."""
        failed = self._load_failed()
        rows = []
        for src in find_source_documents(self.doc_paths):
            sha = text_sha1(read_text(src))
            for lang in target_langs:
                dst = copy_path(src, lang)
                meta = None
                if os.path.exists(dst):
                    try:
                        meta = parse_marker(read_text(dst))
                    except OSError:
                        meta = None
                if meta and meta.get("sha1") == sha:
                    state = "ok"
                else:
                    state = "stale" if meta else "missing"
                f = failed.get(self._key(src, lang))
                attempts = self._attempts(f, sha)
                if state != "ok" and attempts >= self.max_attempts:
                    state = "failed"
                rows.append({"source": src, "lang": lang, "copy": dst, "state": state,
                             "attempts": attempts, "problem": (f or {}).get("problem", "") if attempts else ""})
        return rows

    # ---------- перевод ----------
    def process(
        self,
        get_translator: Callable[[], object],
        target_langs: List[str],
        force: bool = False,
        dry_run: bool = False,
        sink=None,
    ) -> Dict[str, object]:
        """
        Переводит все документы, у которых перевода нет или он устарел.
        get_translator — ленивый доступ к ИИ-клиенту (создаётся только если
        реально есть что переводить). force=True — игнорировать счётчик
        неудачных попыток. dry_run=True — только отчёт.
        """
        failed = self._load_failed()
        report: Dict[str, object] = {"translated": [], "failed": [], "skipped": [], "up_to_date": 0}
        rows = self.status(target_langs)
        todo = [r for r in rows if r["state"] != "ok"]
        report["up_to_date"] = len(rows) - len(todo)

        for row in todo:
            src, lang, dst = str(row["source"]), str(row["lang"]), str(row["copy"])
            if row["state"] == "failed" and not force:
                report["skipped"].append(f"{src} -> {lang}")
                if sink is not None:
                    sink.data["documents"]["skipped"].append(
                        {"source": src, "lang": lang, "reason": "исчерпан лимит попыток", "problem": row.get("problem")})
                continue
            if dry_run:
                report["translated"].append(f"{src} -> {lang} (dry-run)")
                continue

            text = read_text(src)
            sha = text_sha1(text)
            ok, problem, out, detail, bad_chunk = True, "", [], {}, ""
            for chunk in split_markdown(text):
                if not _LETTER_RE.search(chunk):
                    out.append(chunk)
                    continue
                translator = get_translator()
                translated = translator.translate_markdown(chunk, lang)
                if translated is None:
                    detail = dict(getattr(translator, "last_markdown_detail", {}) or {})
                    bad_chunk = chunk
                    if detail.get("api_error"):
                        problem = f"ошибка API: {detail['api_error']}"
                    else:
                        tries = detail.get("attempts") or []
                        problem = "перевод не прошёл проверку: " + (
                            "; ".join(tries[-1].get("problems", [])) if tries else "нет ответа модели")
                    ok = False
                    break
                out.append(translated)

            key = self._key(src, lang)
            if not ok:
                attempts = self._attempts(failed.get(key), sha) + 1
                entry = {
                    "sha1": sha,
                    "version": self.version,
                    "attempts": attempts,
                    "gave_up": attempts >= self.max_attempts,
                    "problem": problem,
                    "chunk": clip(bad_chunk, 300),
                    "api_error": clip(detail.get("api_error")),
                    "responses": [
                        {"response": clip(a.get("response")), "problems": a.get("problems", [])}
                        for a in detail.get("attempts") or []
                    ],
                }
                failed[key] = entry
                report["failed"].append(f"{src} -> {lang}: {problem}")
                logger.warning(f"Документ не переведён {src} -> {lang}: {problem}")
                if sink is not None:
                    sink.data["documents"]["failed"].append({"source": src, "lang": lang, **entry})
                    sink.log("WARNING", f"документ НЕ переведён {src} -> {lang} (попытка {attempts}): {problem}"
                                        + (f" | ответ: {entry['responses'][-1]['response']!r}" if entry["responses"] else ""))
                continue

            marker = f"<!-- autoi18n: source={os.path.basename(src)} lang={lang} sha1={sha} -->\n"
            _atomic_write(dst, marker + "\n\n".join(out).rstrip("\n") + "\n")
            failed.pop(key, None)
            report["translated"].append(f"{src} -> {lang}")
            if sink is not None:
                sink.data["documents"]["translated"].append(f"{src} -> {lang}")
                sink.log("INFO", f"документ переведён {src} -> {lang}")

        if not dry_run:
            self._save_failed(failed)
        if sink is not None:
            sink.data["documents"]["up_to_date"] = report["up_to_date"]
        return report

    # ---------- чтение для показа ----------
    def get_text(self, source_path: str, lang: str) -> Tuple[str, str]:
        """
        (текст, язык_текста) для показа пользователю. Если перевода на lang
        ещё нет — отдаётся исходник (язык = исходный). Устаревший перевод
        отдаётся как есть, пока воркер его не обновит.
        """
        if lang and lang != self.source_lang:
            dst = copy_path(source_path, lang)
            if os.path.exists(dst):
                return strip_marker(read_text(dst)), lang
        return read_text(source_path), self.source_lang
