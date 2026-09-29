# src/autoi18n/report.py
"""
Отчёт о работе библиотеки — чтобы все важные моменты были видны без
отладки и без доступа к логам контейнера.

Два файла в папке переводов (cache_dir):

- _report.json   — подробный отчёт ПОСЛЕДНЕГО цикла воркера (или последней
                   команды CLI): что просканировано, что переведено, что
                   отклонено и почему (ответ модели, ошибка API, какие
                   проверки не прошли), какие файлы не удалось разобрать,
                   какие документы не переведены, сколько плохих переводов
                   в сохранённых файлах (audit), ошибки самого цикла.
- _autoi18n.log  — журнал: по строке на каждое важное событие, с датой;
                   дописывается, при размере > 1 МБ старый журнал
                   переименовывается в _autoi18n.log.1.

Раньше предупреждения шли в логгер с NullHandler — то есть не выводились
никуда, и причину сбоя было не узнать.
"""
import datetime as _dt
import logging
import os
import traceback
from typing import Any, Dict, Optional

from .utils import safe_json_save

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

LOG_MAX_BYTES = 1_000_000
TEXT_LIMIT = 500  # сколько символов ответа модели сохранять в отчёте


def clip(value: Optional[str], limit: int = TEXT_LIMIT) -> Optional[str]:
    if value is None:
        return None
    value = str(value)
    return value if len(value) <= limit else value[:limit] + "…"


def _now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


class Report:
    def __init__(self, cache_dir: str, version: str = ""):
        self.cache_dir = cache_dir
        self.version = version
        self.data: Dict[str, Any] = {}
        self.start()

    @property
    def report_path(self) -> str:
        return os.path.join(self.cache_dir, "_report.json")

    @property
    def log_path(self) -> str:
        return os.path.join(self.cache_dir, "_autoi18n.log")

    def start(self, kind: str = "cycle") -> None:
        self.data = {
            "kind": kind,
            "started": _now(),
            "finished": None,
            "version": self.version,
            "extract": {},
            "phrases": {"translated": {}, "rejected": [], "accepted_with_remarks": [], "api_errors": []},
            "documents": {"translated": [], "failed": [], "skipped": [], "up_to_date": 0},
            "audit": {},
            "errors": [],
        }

    # ---------- журнал ----------
    def log(self, level: str, message: str) -> None:
        getattr(logger, level.lower(), logger.info)(message)
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            if os.path.exists(self.log_path) and os.path.getsize(self.log_path) > LOG_MAX_BYTES:
                os.replace(self.log_path, self.log_path + ".1")
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(f"{_now()} {level.upper():7} {message}\n")
        except OSError:
            pass

    def error(self, where: str, exc: BaseException) -> None:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self.data["errors"].append({"where": where, "error": f"{type(exc).__name__}: {exc}", "traceback": clip(tb, 4000)})
        self.log("ERROR", f"{where}: {type(exc).__name__}: {exc}")

    # ---------- запись ----------
    def save(self) -> None:
        self.data["finished"] = _now()
        try:
            safe_json_save(self.report_path, self.data)
        except OSError as e:
            logger.warning(f"Не удалось записать отчёт {self.report_path}: {e}")

    def summary(self) -> str:
        d = self.data
        ph = d["phrases"]
        docs = d["documents"]
        audit = d.get("audit") or {}
        ex = d.get("extract") or {}
        return (
            f"цикл: новых фраз {ex.get('new_phrases', 0)}, восстановлено в очередь {ex.get('requeued_missing', 0)}, "
            f"ошибок разбора файлов {len(ex.get('parse_errors', {}))}; "
            f"переведено фраз {sum(ph['translated'].values())}, отклонено {len(ph['rejected'])}, "
            f"ошибок API {len(ph['api_errors'])}; "
            f"документов переведено {len(docs['translated'])}, не удалось {len(docs['failed'])}; "
            f"плохих сохранённых переводов {audit.get('bad_count', 0)}; ошибок цикла {len(d['errors'])}"
        )
