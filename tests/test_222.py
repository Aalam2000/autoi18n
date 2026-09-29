import json

from autoi18n import Translator
from conftest import make_ai


def test_added_heading_is_stripped():
    src = "Приветствую. Тут будет инструкция для Преподавателя."
    tr, fake = make_ai(["# Welcome. Here will be the instructions for the Teacher."])
    assert tr.translate_markdown(src, "en") == "Welcome. Here will be the instructions for the Teacher."
    assert "Never add anything" in fake.calls[0]["messages"][0]["content"]


def test_invented_document_is_still_rejected():
    src = "Приветствую. Тут будет инструкция для Администратора."
    invented = "# Welcome\n\n## Contents\n\n1. [Users](#users)\n2. [Groups](#groups)\n\n## Users\n\n- Add users"
    tr, _ = make_ai([invented, invented])
    assert tr.translate_markdown(src, "en") is None


def test_real_heading_kept():
    src = "# Заголовок\n\nТекст."
    tr, _ = make_ai(["# Title\n\nText."])
    assert tr.translate_markdown(src, "en") == "# Title\n\nText."


def test_attempts_reset_after_library_update(project):
    (project / "autoi18n.json").write_text(json.dumps({"doc_paths": ["help"]}), encoding="utf-8")
    (project / "help").mkdir()
    (project / "help" / "t.md").write_text("Привет.\n", encoding="utf-8")
    t = Translator(env_path=".env")
    t.version = t.documents.version = "2.2.1"

    class Bad:
        last_markdown_detail = {"attempts": [{"response": "# x", "problems": ["p"]}], "api_error": None}
        def translate_markdown(self, text, lang): return None
    t._ai_translator = Bad()
    for _ in range(t.max_attempts):
        t.translate_documents()
    assert all(r["state"] == "failed" for r in t.document_status())

    t2 = Translator(env_path=".env")
    t2.version = t2.documents.version = "2.2.2"   # новая версия библиотеки
    assert all(r["state"] == "missing" for r in t2.document_status())

    class Good:
        def translate_markdown(self, text, lang): return "Hello." if lang == "en" else "Salam."
    t2._ai_translator = Good()
    assert len(t2.translate_documents()["translated"]) == 2
