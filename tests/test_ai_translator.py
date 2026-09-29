import json

from conftest import make_ai


def test_batch_validates_and_retries():
    batch = json.dumps({"items": [
        {"id": 1, "translated": "\"Student\""},
        {"id": 2, "translated": "Quote:"},                 # потерян {{0}}
        {"id": 3, "translated": "Проверено"},                 # не переведено
    ]})
    tr, fake = make_ai([batch, "Quote: {{0}}", "The translation of X is Checked", "still Проверено"])
    items = [{"storage_key": "a", "text": "Студент"},
             {"storage_key": "b", "text": "КП: {{0}}"},
             {"storage_key": "c", "text": "Проверено"}]
    result = tr.translate_batch(items, "en")
    assert result == {"a": "Student", "b": "Quote: {{0}}"}
    assert "c" in tr.last_problems
    # первый запрос — системная инструкция + JSON-режим + temperature 0
    first = fake.calls[0]
    assert first["messages"][0]["role"] == "system"
    assert first["response_format"] == {"type": "json_object"}
    assert first["temperature"] == 0
    # повтор идёт с объяснением причины
    assert "rejected" in fake.calls[1]["messages"][1]["content"]


def test_temperature_fallback():
    tr, fake = make_ai([Exception("Unsupported parameter: 'temperature'"), "Student"])
    assert tr.translate_single("Студент", "en") == "Student"
    assert "temperature" not in fake.calls[1]


def test_glossary_in_prompt(tmp_path):
    from autoi18n.glossary import Glossary
    p = tmp_path / "g.json"
    p.write_text(json.dumps({"context": "Acme CRM",
                             "terms": {"КП": {"az": "Təklif"}}}, ensure_ascii=False), encoding="utf-8")
    tr, fake = make_ai(["+ Təklif"], glossary=Glossary(str(p)))
    assert tr.translate_single("+ КП", "az") == "+ Təklif"
    system = fake.calls[0]["messages"][0]["content"]
    assert "Acme CRM" in system and "КП -> Təklif" in system


def test_markdown_retry_then_fail():
    src = "# Заголовок\n\nТекст **жирный**."
    tr, _ = make_ai(["Title\n\nText bold.", "# Title\n\nText **bold**."])
    assert tr.translate_markdown(src, "en") == "# Title\n\nText **bold**."
    tr, _ = make_ai(["bad", "bad"])
    assert tr.translate_markdown(src, "en") is None
