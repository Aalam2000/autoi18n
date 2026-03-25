# auto-i18n-lib

Lightweight post-render HTML translation for Python projects.

---

## Why this library

Most i18n solutions require you to:

- mark strings in templates;
- restructure project code;
- maintain translation catalogs;
- adapt data storage rules.

This library uses a different approach:

1. render HTML as usual;
2. pass the final HTML through the translator;
3. return translated HTML to the user.

That makes it practical for:

- existing projects;
- legacy systems;
- admin panels;
- internal tools;
- rapidly changing SaaS interfaces.

---

## Features

- Post-render HTML translation
- Translation of:
  - text nodes
  - table content
  - button labels
  - `option`
  - `textarea`
  - selected attributes:
    - `placeholder`
    - `title`
    - `alt`
    - `aria-label`
    - `value` for button-like inputs
- Per-language JSON cache
- Batch translation of new strings
- Reuse of cached translations across pages
- Backward-aware loading of legacy page-based cache files
- Minimal project integration

The current implementation is based on `HTMLParser`, the `Translator` class, local JSON cache saving, browser language detection, and OpenAI-based translation. :contentReference[oaicite:2]{index=2}

---

## Installation

```bash
pip install auto-i18n-lib


---

## Requirements

* Python 3.9+
* OpenAI API key

The package metadata currently requires Python `>=3.9`.  

---

## Environment variables

You can configure the translator through constructor arguments or environment variables.

### Supported environment variables

* `OPENAI_API_KEY` — your OpenAI API key
* `SOURCE_LANG` — source language of your project content

Example:

```bash
export OPENAI_API_KEY="your_key"
export SOURCE_LANG="ru"
```

Or on Windows PowerShell:

```powershell
$env:OPENAI_API_KEY="your_key"
$env:SOURCE_LANG="ru"
```

Note: `source_lang` is read from `SOURCE_LANG` if not provided explicitly in the constructor. This behavior is implemented in the current `Translator` class. 

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

## Typical integration pattern

```python
html = render_template(...)
translated_html = translator.translate_html(
    html=html,
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

## What is translated

The current implementation processes:

* regular visible text nodes;
* table text;
* form-related visible text;
* selected UI attributes;
* button-like input values.

These behaviors are implemented in the current parser and translator logic. 

---

## What is not translated

The current implementation intentionally skips:

* `script`
* `style`
* `noscript`
* elements with:

  * `translate="no"`
  * `data-translate="no"`
  * `id="langSwitch"`

It also skips values that look like:

* empty or whitespace-only content;
* pure numbers;
* number-like strings;
* UUID/hash-like strings;
* many technical Latin-only identifiers, paths, and codes.

These skip rules are explicitly present in the current code. 

---

## Cache behavior

Translations are stored as JSON files in the cache directory.

### Current cache format

One file per target language, for example:

* `translations/en.json`
* `translations/de.json`
* `translations/az.json`

This allows the same translated strings to be reused across different pages.

### Legacy cache compatibility

Older versions could store cache in page-based files such as:

* `home.en.json`
* `profile.de.json`

The current version can read legacy page-based cache files and merge them into the new language-level cache automatically. This behavior is implemented by `_legacy_file_path()`, `_file_path()`, and `_load_storage()`. 

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
* `source_lang` — source language of the original project content
* `model` — OpenAI model used for translation

The current default model is `gpt-4o-mini`. 

---

## Public API

### `translate_text(text, target_lang, page_name="page", prompt_type="normal")`

Translates a single text string.

### `translate_html(html, target_lang, page_name="page")`

Translates rendered HTML while preserving HTML structure.

### `detect_browser_lang(accept_language)`

Extracts the primary browser language from the `Accept-Language` header.

### `get_alternative_lang(current_lang, browser_lang)`

Returns an alternative language value for language switch logic.

These public methods exist in the current `Translator` class. 

---

## Upgrade notes

### What changed in the new version

* translation coverage was expanded;
* table content is translated;
* form-related UI text is translated;
* useful attributes are translated;
* new strings are translated in batches;
* cache storage is language-based;
* old cache entries are no longer deleted during page rendering.

### Why this changed

The new behavior improves:

* translation completeness;
* cache stability;
* reuse across pages;
* overall translation efficiency.

### Migration impact

In many projects, integration code can remain unchanged.

Possible differences after upgrade:

* cache files may now be created as `translations/<lang>.json`;
* old page-specific cache layout may no longer be the only active cache format;
* custom scripts that depend on page-based cache naming may need adjustment.

---

## Recommended content rule

For better translation quality and more stable terminology, it is recommended to keep structured business data normalized and consistent.

In many projects, English is the most convenient base language for structured reference values and reusable business terms.

---

## Limitations

* The library translates rendered HTML, not source templates.
* Translation quality depends on source text quality.
* Highly dynamic or fragmented HTML may reduce cache efficiency.
* This library is intended as a pragmatic i18n layer, not a full replacement for every localization workflow.

---

## License

MIT

See the `LICENSE` file included in the package structure. 

