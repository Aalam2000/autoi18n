import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class FakeCompletions:
    """Подставной OpenAI: отвечает по очереди из answers (str или callable(messages))."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.answers.pop(0)
        if callable(answer):
            answer = answer(kwargs["messages"])
        if isinstance(answer, Exception):
            raise answer
        msg = types.SimpleNamespace(content=answer)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


def make_ai(answers, glossary=None, source_lang="ru"):
    from autoi18n.ai_translator import OpenAITranslator
    tr = OpenAITranslator({"api_key": "x", "source_lang": source_lang, "retry_delay": 0, "glossary": glossary})
    fake = FakeCompletions(answers)
    tr.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=fake))
    return tr, fake


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Пустой проект в tmp: .env с целевыми языками, рабочая папка = tmp."""
    monkeypatch.chdir(tmp_path)
    for var in ("AUTO_I18N_TARGET_LANGS", "SOURCE_LANG", "AUTO_I18N_CACHE_DIR", "AUTO_I18N_DOC_PATHS",
                "AUTO_I18N_GLOSSARY", "AUTO_I18N_PROJECT_FILE", "AUTO_I18N_MAX_ATTEMPTS"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".env").write_text("SOURCE_LANG=ru\nAUTO_I18N_TARGET_LANGS=en,az\nOPENAI_API_KEY=x\n", encoding="utf-8")
    return tmp_path
