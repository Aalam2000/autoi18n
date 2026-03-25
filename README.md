# auto-i18n-lib

Lightweight HTML translation library for Python projects.

`auto-i18n-lib` translates already-rendered HTML into the user’s language with minimal integration effort.
It is designed for projects where you want multilingual UI without rewriting templates, models, or business logic.

The library works at the rendered HTML level:

* keeps existing project structure intact;
* translates visible text, table content, form-related UI text, and useful attributes;
* caches translations locally by target language;
* can be integrated into existing render flows with minimal changes.

---

## Key idea

You do **not** need to rebuild the project around a full i18n framework.

Instead, you:

1. render HTML as usual;
2. pass the resulting HTML through the translator;
3. return translated HTML to the user.

This makes the library especially useful for:

* legacy systems;
* custom admin panels;
* internal tools;
* SaaS products with fast evolving UI;
* projects where database structure should remain untouched.

---

## Features

* HTML translation after render
* Translation of:

  * regular text nodes
  * table content
  * button labels
  * `option`
  * `textarea`
  * useful attributes:

    * `placeholder`
    * `title`
    * `alt`
    * `aria-label`
    * `value` for button-like inputs
* Per-language JSON cache
* Batch translation of new strings
* Reuse of previously saved translations
* Minimal integration into existing projects
* Backward-aware cache loading for older cache files

---

## Installation

```bash
pip install auto-i18n-lib
```

---

## Requirements

* Python 3.8+
* OpenAI API key

---

## Quick start

```python
from autoi18n import Translator

translator = Translator(
    api_key="YOUR_OPENAI_API_KEY",
    cache_dir="./translations",
    source_lang="ru",
)

html = """
<h1>Добро пожаловать</h1>
<p>Это тестовая страница</p>
<button>Сохранить</button>
"""

translated_html = translator.translate_html(
    html=html,
    target_lang="en",
    page_name="home",
)

print(translated_html)
```

---

## Basic usage

### Translate plain text

```python
from autoi18n import Translator

translator = Translator(
    api_key="YOUR_OPENAI_API_KEY",
    cache_dir="./translations",
    source_lang="ru",
)

result = translator.translate_text(
    text="Привет, мир!",
    target_lang="en",
    page_name="common",
)

print(result)
```

### Translate HTML

```python
from autoi18n import Translator

translator = Translator(
    api_key="YOUR_OPENAI_API_KEY",
    cache_dir="./translations",
    source_lang="ru",
)

html = """
<form>
    <label>Имя</label>
    <input type="text" placeholder="Введите имя">
    <textarea placeholder="Введите комментарий"></textarea>
    <input type="submit" value="Отправить">
</form>
"""

translated_html = translator.translate_html(
    html=html,
    target_lang="en",
    page_name="form_page",
)

print(translated_html)
```

---

## Tables and forms

The library now supports translation of common UI structures such as tables and forms.

Example:

```python
html = """
<table>
    <tr>
        <th>Клиент</th>
        <th>Статус</th>
    </tr>
    <tr>
        <td>Иван Петров</td>
        <td>Ожидает</td>
    </tr>
</table>

<select>
    <option>Выберите страну</option>
    <option>Азербайджан</option>
</select>
"""
```

All visible text inside these elements is processed through the same HTML translation flow.

---

## FastAPI example

```python
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from autoi18n import Translator

app = FastAPI()

translator = Translator(
    api_key="YOUR_OPENAI_API_KEY",
    cache_dir="./translations",
    source_lang="ru",
)

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    html = """
    <h1>Главная страница</h1>
    <p>Добро пожаловать в систему</p>
    <button>Продолжить</button>
    """

    accept_language = request.headers.get("accept-language", "")
    target_lang = translator.detect_browser_lang(accept_language)

    translated_html = translator.translate_html(
        html=html,
        target_lang=target_lang,
        page_name="index",
    )
    return translated_html
```

---

## Flask example

```python
from flask import Flask, request
from autoi18n import Translator

app = Flask(__name__)

translator = Translator(
    api_key="YOUR_OPENAI_API_KEY",
    cache_dir="./translations",
    source_lang="ru",
)

@app.route("/")
def index():
    html = """
    <h1>Панель управления</h1>
    <p>Здесь отображается основная информация</p>
    """

    accept_language = request.headers.get("Accept-Language", "")
    target_lang = translator.detect_browser_lang(accept_language)

    return translator.translate_html(
        html=html,
        target_lang=target_lang,
        page_name="dashboard",
    )
```

---

## Cache behavior

Translations are stored in JSON files inside the cache directory.

Current cache format:

* one file per target language:

  * `translations/en.json`
  * `translations/de.json`
  * `translations/az.json`

This allows reuse of the same translated strings across different pages.

### Legacy cache compatibility

Older versions could store cache in page-based files such as:

* `home.en.json`
* `profile.de.json`

The new version can read legacy page-based cache files and merge their data into the new language-level cache automatically.

---

## Constructor

```python
Translator(
    cache_dir="./translations",
    api_key=None,
    source_lang=None,
    model="gpt-4o-mini",
)
```

### Parameters

* `cache_dir` — directory for translation cache files
* `api_key` — OpenAI API key
* `source_lang` — source language of your project content
* `model` — OpenAI model name used for translation

If `source_lang` is not provided, the library tries to read it from the `SOURCE_LANG` environment variable.
If that variable is also missing, the default source language is `ru`.

---

## Public methods

### `translate_text(text, target_lang, page_name="page", prompt_type="normal")`

Translates a single text string.

### `translate_html(html, target_lang, page_name="page")`

Translates rendered HTML while preserving the HTML structure.

### `detect_browser_lang(accept_language)`

Extracts the primary browser language from the `Accept-Language` header.

### `get_alternative_lang(current_lang, browser_lang)`

Returns an alternative language value for language switch logic.

---

## Recommended content rule

For better translation quality and more stable terminology, it is recommended to store structured business data in English where practical.

Typical recommendation:

* UI labels may stay in the project’s main source language;
* structured reference values and reusable business terms are best kept normalized and consistent;
* English is often the most convenient base language for long-term multilingual scaling.

---

## Upgrade notes

### What changed in the new version

* translation coverage was expanded;
* table content is now translated;
* form-related UI text is now translated;
* attribute translation support was extended;
* new strings are translated in batches;
* cache storage moved to per-language files instead of page-only cache files;
* old cache entries are no longer automatically removed during page rendering.

### Why this changed

The new behavior improves:

* translation completeness;
* cache stability;
* reuse of translations across pages;
* performance for repeated UI elements.

### Migration impact

In most projects, integration code can remain unchanged.

Potential differences after upgrade:

* cache files may now be created as `translations/<lang>.json`;
* projects relying on old page-specific cache layout should review deployment or backup rules;
* if you had custom scripts around cache maintenance, update them to the new per-language structure.

---

## Practical integration model

Typical flow in a project:

```python
html = render_template(...)
translated_html = translator.translate_html(
    html,
    target_lang=user_lang,
    page_name="some_page",
)
return translated_html
```

This keeps:

* templates unchanged;
* business logic unchanged;
* database schema unchanged.

---

## Notes

* The library is intended for pragmatic multilingual delivery, not for replacing every possible i18n workflow.
* Translation quality depends on the source text quality.
* Repeated, consistent source wording produces the best cache reuse and the most stable translations.

---

## License

MIT
