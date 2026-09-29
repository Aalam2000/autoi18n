import json

from autoi18n.glossary import Glossary, fold
from autoi18n.quality import check_markdown, check_phrase, is_soft


def glossary(tmp_path):
    p = tmp_path / "g.json"
    p.write_text(json.dumps({
        "context": "Acme CRM",
        "terms": {
            "КП": {"en": "Quote", "az": "Təklif"},
            "Менеджер*": {"en": "Manager", "az": "Menecer"},
            "Отчёт*": {"en": "Report", "az": "Hesabat"},
            "баз* клиент*": {"en": "Client base", "az": "Müştəri bazası"},
        },
    }, ensure_ascii=False), encoding="utf-8")
    return Glossary(str(p))


def test_typical_model_errors_are_caught(tmp_path):
    g = glossary(tmp_path)
    assert check_phrase("+ КП всем", "+ CP all", "en", g)                         # аббревиатура не по глоссарию
    assert check_phrase("КП из базы клиентов", "Offer from the database", "en", g)
    assert check_phrase("+ Добавить менеджера", "+ Add seller", "en", g)
    assert check_phrase("Отчёты", "Hesabatlar", "az", g) == []
    assert check_phrase("Отчёты", "Нesabatlar", "az", g)                          # кириллическая Н
    assert any("скобки" in p for p in check_phrase("Сайт (ссылка)", "Site (link)[", "en"))
    assert check_phrase("Клиент", 'The translation of "Клиент" from Russian to English is "Client."', "en")
    assert check_phrase("КП: {{0}}", "Quote:", "en")


def test_good_translations_pass(tmp_path):
    g = glossary(tmp_path)
    assert check_phrase("+ КП всем", "+ Quote for everyone", "en", g) == []
    assert check_phrase("Итог по менеджерам", "Menecerlər üzrə nəticə", "az", g) == []
    assert check_phrase("КП из базы клиентов", "Quote from the client base", "en", g) == []
    assert check_phrase("Нельзя удалить «{{0}}»", "«{{0}}» silinməsi mümkün deyil", "az") == []
    assert check_phrase("Перевести", "Translate", "en") == []  # слово "перевод" в исходнике — легально


def test_glossary_violation_is_soft(tmp_path):
    g = glossary(tmp_path)
    problems = check_phrase("Менеджер", "Seller", "en", g)
    assert problems and all(is_soft(p) for p in problems)


def test_fold_az():
    assert fold("İstanbul", "az") == "istanbul"
    assert fold("Istanbul", "az") == "ıstanbul"


def test_markdown_structure():
    src = "# Заголовок\n\n- пункт **жирный**\n- пункт [ссылка](http://x)\n"
    assert check_markdown(src, "# Title\n\n- item **bold**\n- item [link](http://x)\n", "en") == []
    assert check_markdown(src, "Title\n\n- item\n- item link\n", "en")
    assert check_markdown(src, src, "en")  # не переведено
