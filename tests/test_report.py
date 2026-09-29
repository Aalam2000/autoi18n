import json
import threading

from autoi18n import Translator
from autoi18n.utils import text_hash
from conftest import make_ai


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def run_one_cycle(t):
    stop = threading.Event()
    stop.set()
    t.run_translation_loop(interval=1, stop_event=stop)


def test_cycle_report_shows_reasons(project):
    write(project / "autoi18n.json", {"doc_paths": ["help"], "terms": {"КП": {"en": "Quote"}}})
    (project / "help").mkdir()
    (project / "help" / "teacher.md").write_text("# Привет\n\nТекст.\n", encoding="utf-8")
    h1 = text_hash("Студент")
    write(project / "translations" / "ru.json", {h1: "Студент"})
    (project / ".env").write_text("SOURCE_LANG=ru\nAUTO_I18N_TARGET_LANGS=en\nOPENAI_API_KEY=x\n", encoding="utf-8")

    t = Translator(env_path=".env")
    api_down = RuntimeError("Connection error.")
    ai, _ = make_ai([
        # фраза "Студент": пакет -> объяснение модели, повтор -> снова плохо
        json.dumps({"items": [{"id": 1, "translated": "The translation of Студент is Student"}]}),
        "Студент",
        # документ: API недоступен (3 попытки _chat)
        api_down, api_down, api_down,
    ])
    t._ai_translator = ai
    run_one_cycle(t)

    rep = json.loads((project / "translations" / "_report.json").read_text(encoding="utf-8"))
    rej = rep["phrases"]["rejected"][0]
    assert rej["text"] == "Студент" and rej["response"].startswith("The translation")
    assert rej["retry_response"] == "Студент" and rej["problems"]
    doc = rep["documents"]["failed"][0]
    assert "ошибка API" in doc["problem"] and "Connection error" in doc["api_error"]
    assert rep["audit"]["bad_count"] == 0
    assert rep["errors"] == []

    failed = json.loads((project / "translations" / "_docs_failed.json").read_text(encoding="utf-8"))
    assert "Connection error" in failed["help/teacher.md|en"]["api_error"]
    pending = json.loads((project / "translations" / "_pending.json").read_text(encoding="utf-8"))
    assert pending["en"][h1]["last_response"] == "Студент"

    log = (project / "translations" / "_autoi18n.log").read_text(encoding="utf-8")
    assert "воркер запущен" in log and "ОТКЛОНЁН" in log and "документ НЕ переведён" in log


def test_cycle_step_error_is_reported_and_others_run(project, monkeypatch):
    t = Translator(env_path=".env")

    def boom():
        raise ValueError("сломалось")
    monkeypatch.setattr(t, "extract", boom)
    run_one_cycle(t)
    rep = json.loads((project / "translations" / "_report.json").read_text(encoding="utf-8"))
    assert rep["errors"][0]["where"] == "extract" and "сломалось" in rep["errors"][0]["error"]
    assert "audit" in rep and rep["audit"]["bad_count"] == 0
