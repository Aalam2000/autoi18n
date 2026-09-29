import json

from autoi18n import Translator
from autoi18n.utils import text_hash


class FakeAI:
    """Подставной переводчик уровня BaseTranslator."""

    def __init__(self, fn):
        self.fn = fn
        self.last_problems = {}

    def translate_batch(self, items, lang):
        self.last_problems = {}
        out = {}
        for it in items:
            tr = self.fn(it["text"], lang)
            if tr is None:
                self.last_problems[it["storage_key"]] = ["отклонено"]
            else:
                out[it["storage_key"]] = tr
        return out

    def translate_markdown(self, text, lang):
        return self.fn(text, lang)


def translit(text, lang):
    table = str.maketrans("абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ",
                          "abvgdeejziiklmnoprstufhccss_y_euaABVGDEEJZIIKLMNOPRSTUFHCCSS_Y_EUA")
    return text.translate(table)


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_worker_attempts_and_self_heal(project):
    t = Translator(env_path=".env")
    ru = {text_hash("Урок"): "Урок", text_hash("Студент"): "Студент"}
    write(project / "translations" / "ru.json", ru)
    write(project / "translations" / "en.json", {text_hash("Урок"): "Lesson"})  # студента нет
    write(project / "translations" / "az.json", {text_hash("Урок"): "Dərs", text_hash("Студент"): "Tələbə"})

    t.extract()  # скан пустой, но недостающий перевод должен встать в очередь
    pending = json.loads((project / "translations" / "_pending.json").read_text(encoding="utf-8"))
    assert list(pending) == ["en"] and text_hash("Студент") in pending["en"]

    t._ai_translator = FakeAI(lambda s, l: None)  # ИИ всё отклоняет
    for _ in range(5):
        assert t.process_queue() == 0
    meta = t._storage.load_pending("shared")["en"][text_hash("Студент")]
    assert meta["attempts"] == t.max_attempts  # больше лимита не пытались
    assert t.audit()["failed"][0]["text"] == "Студент"

    t._ai_translator = FakeAI(lambda s, l: "Student")
    res = t.retranslate(from_audit=True)
    assert res["translated"] == 1
    assert t._storage.load_cache("shared", "en")[text_hash("Студент")] == "Student"


def test_audit_and_set(project):
    write(project / "autoi18n.json", {"terms": {"КП": {"en": "Quote", "az": "Təklif"}}})
    t = Translator(env_path=".env")
    h = text_hash("+ КП")
    write(project / "translations" / "ru.json", {h: "+ КП"})
    write(project / "translations" / "en.json", {h: "+ Quote"})
    write(project / "translations" / "az.json", {h: "+ CP"})
    bad = t.audit()["bad"]
    assert [b["lang"] for b in bad] == ["az"]
    assert t.set_translation("az", "+ КП", "+ Təklif") == []
    assert t.audit()["bad"] == []


def test_documents(project):
    write(project / "autoi18n.json", {"doc_paths": ["help"]})
    help_dir = project / "help"
    help_dir.mkdir()
    (help_dir / "teacher.md").write_text("# Привет\n\nЭто **инструкция**.\n", encoding="utf-8")
    (project / "notes.md").write_text("Не документ", encoding="utf-8")  # вне doc_paths

    t = Translator(env_path=".env")
    t._ai_translator = FakeAI(translit)
    rep = t.translate_documents()
    assert sorted(rep["translated"]) == [f"help/teacher.md -> az", f"help/teacher.md -> en"]
    en = (help_dir / "teacher.en.md").read_text(encoding="utf-8")
    assert en.startswith("<!-- autoi18n: source=teacher.md lang=en sha1=")
    assert "# Privet" in en and "**instrukcia**" in en
    assert not (project / "notes.en.md").exists()

    # повторный проход — ничего не переводит, копии не считаются исходниками
    assert t.translate_documents()["translated"] == []
    assert t.get_document("help/teacher.md", "en")["text"].startswith("# Privet")
    assert t.get_document("help/teacher.md", "ru")["lang"] == "ru"

    # исходник изменился -> перевод обновляется
    (help_dir / "teacher.md").write_text("# Привет\n\nНовый текст.\n", encoding="utf-8")
    assert len(t.translate_documents()["translated"]) == 2

    # ИИ не справляется -> копия не пишется, после лимита попыток не дёргаем ИИ
    (help_dir / "teacher.md").write_text("# Пока\n", encoding="utf-8")
    t._ai_translator = FakeAI(lambda s, l: None)
    for _ in range(t.max_attempts):
        assert t.translate_documents()["failed"]
    rep = t.translate_documents()
    assert rep["failed"] == [] and len(rep["skipped"]) == 2
    assert "Новый" not in t.get_document("help/teacher.md", "en")["text"]  # старый перевод остался как есть
    assert t.translate_documents(force=True)["failed"]


def test_split_markdown_keeps_code_blocks():
    from autoi18n.documents import split_markdown
    text = "# A\n\n" + "абзац\n\n" * 5 + "```\nкод\n\nкод\n```\n"
    chunks = split_markdown(text, limit=20)
    assert any("```\nкод\n\nкод\n```" in c for c in chunks)
    assert "\n\n".join(chunks).strip() == text.strip()
