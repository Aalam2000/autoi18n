import json

from autoi18n.config import Config
from autoi18n.extractor.html_extractor import apply_translations_to_html, collect_translatable_items
from autoi18n.runtime import build_frontend_runtime_script


def test_attributes_from_project(project):
    (project / "autoi18n.json").write_text(json.dumps({"attributes": ["data-tip", "tip", "bad name!"]}), encoding="utf-8")
    assert Config(env_path=".env").attributes == ["data-tip", "tip"]


def test_html_extra_attrs():
    html = '<button data-tip="Удалить урок">x</button>'
    assert collect_translatable_items(html) == []
    assert [i["text"] for i in collect_translatable_items(html, extra_attrs=["data-tip"])] == ["Удалить урок"]
    out = apply_translations_to_html(html, {"Удалить урок": "Delete lesson"}, ["data-tip"])
    assert 'data-tip="Delete lesson"' in out


def test_runtime_attrs_list():
    js = build_frontend_runtime_script({}, extra_attrs=["data-tip"])
    assert '["placeholder", "title", "alt", "aria-label", "label", "data-tip"]' in js
    assert "attributeFilter: TRANSLATABLE_ATTRS" in js


def test_template_tags_in_script_are_neutralized():
    from autoi18n.extractor.html_extractor import neutralize_template_tags as f
    src = 'const Q = {{ q }};\n{% if x %}go(){% endif %}\nalert("Привет {{ name }}");'
    out = f(src)
    assert out.startswith("const Q = null") and "{%" not in out
    assert 'alert("Привет {{ name }}")' in out           # внутри строки — не трогаем
    assert out.count("\n") == src.count("\n")            # номера строк сохранены
