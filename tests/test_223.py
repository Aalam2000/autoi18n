import json

from autoi18n import Translator
from autoi18n.config import Config
from autoi18n.utils import text_hash
from conftest import make_ai


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


GLOSSARY = {"terms": {"КП": {"en": "Quote"}, "Менеджер*": {"en": "Manager"}}}


def test_exact_glossary_phrase_skips_ai(project, tmp_path):
    from autoi18n.glossary import Glossary
    write(tmp_path / "g.json", GLOSSARY)
    tr, fake = make_ai([json.dumps({"items": [{"id": 1, "translated": "Client"}]})], glossary=Glossary(str(tmp_path / "g.json")))
    res = tr.translate_batch([{"storage_key": "a", "text": "КП"}, {"storage_key": "b", "text": "Клиент"}], "en")
    assert res == {"a": "Quote", "b": "Client"}
    assert len(fake.calls) == 1 and "КП" not in fake.calls[0]["messages"][1]["content"]


def test_glossary_overrides_saved_and_bad_are_fixed(project):
    write(project / "autoi18n.json", GLOSSARY)
    hq, hm, hc = text_hash("КП"), text_hash("+ Менеджер"), text_hash("Клиент")
    write(project / "translations" / "ru.json", {hq: "КП", hm: "+ Менеджер", hc: "Клиент"})
    write(project / "translations" / "en.json", {hq: "CP", hm: "+ Seller", hc: "Client"})
    (project / ".env").write_text("SOURCE_LANG=ru\nAUTO_I18N_TARGET_LANGS=en\nOPENAI_API_KEY=x\n", encoding="utf-8")
    t = Translator(env_path=".env")

    assert t.apply_glossary() == 1
    assert t._storage.load_cache("shared", "en")[hq] == "Quote"

    assert t.requeue_bad() == 1                     # "+ Seller" нарушает глоссарий
    assert t.requeue_bad() == 0                     # повторно не ставится

    # ИИ снова ошибается -> старый перевод остаётся, попытка засчитана
    ai, _ = make_ai([json.dumps({"items": [{"id": 1, "translated": "+ Seller"}]})] + ["+ Seller"], glossary=t.glossary)
    t._ai_translator = ai
    t.process_queue()
    assert t._storage.load_cache("shared", "en")[hm] == "+ Seller"
    assert t._storage.load_pending("shared")["en"][hm]["attempts"] == 1

    # ИИ перевёл правильно -> перевод заменён, очередь пуста
    ai, _ = make_ai([json.dumps({"items": [{"id": 1, "translated": "+ Manager"}]})], glossary=t.glossary)
    t._ai_translator = ai
    t.process_queue()
    assert t._storage.load_cache("shared", "en")[hm] == "+ Manager"
    assert hm not in t._storage.load_pending("shared").get("en", {})


def test_scan_paths_from_project(project):
    write(project / "autoi18n.json", {"scan_paths": [{"path": "web", "type": "js"}, "tpl"]})
    c = Config(env_path=".env")
    assert c.scan_paths == [
        {"path": "web", "extensions": [".js", ".jsx", ".ts", ".tsx"], "type": "js"},
        {"path": "tpl", "extensions": [".js", ".jsx", ".ts", ".tsx", ".html"], "type": "auto"},
    ]
    (project / "autoi18n.json").unlink()
    assert Config(env_path=".env").scan_paths[0]["path"] == "frontend/src"  # дефолт
