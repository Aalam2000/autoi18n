# src/autoi18n/runtime.py
"""
Генерация клиентского JS-рантайма.

Архитектура v2: рантайм НЕ требует вызовов в коде компонентов (никаких
t()/translateKey()). Вместо этого он после рендера проходит по уже
отрисованному DOM и подменяет видимый текст на перевод — так же для
JSX-контента, обычного HTML и текста, который код меняет императивно
(например, textContent счётчика в квизе). Динамические изменения DOM
(перерисовка React, обновление счётчика/таймера) подхватываются через
MutationObserver — если он включён.

Параметризованные фразы ("Вопрос {{0}} / {{1}}") сопоставляются с живым
текстом на странице по маске (регулярка из шаблона с capture-группами на
месте {{n}}) — без вызова в коде это единственный способ понять, что
означает уже подставленное на странице значение.

Чтобы переключение языка работало без перезагрузки страницы, рантайм
исходно получает словарь только для текущего языка (встроен в HTML при
первом рендере), а при setLanguage() подгружает словарь нового языка через
fetch(translationsUrl) — эндпоинт должен отдавать плоский JSON
{"оригинальный текст": "перевод", ...} (см. Translator._translations_map).
"""
import json
from typing import Dict, Optional


def build_frontend_runtime_script(
    translations: Dict[str, str],
    fallback_lang: str = "ru",
    dynamic_dom_enabled: bool = True,
    translations_url_template: Optional[str] = None,
) -> str:
    payload = json.dumps(translations or {}, ensure_ascii=False)
    observer_enabled = "true" if dynamic_dom_enabled else "false"
    url_template = json.dumps(translations_url_template or "/i18n/translations?lang={lang}")

    return f"""
(function () {{
  var store = {payload};
  var fallbackLang = {json.dumps(fallback_lang)};
  var dynamicDomEnabled = {observer_enabled};
  var translationsUrlTemplate = {url_template};
  var currentLang = fallbackLang;

  // Текст, который узел показывал ДО первой подмены — нужен, чтобы при
  // повторном переключении языка всегда сопоставлять с оригиналом, а не
  // с ранее подставленным переводом.
  var originalTextByNode = new WeakMap();

  // Скомпилированные маски для параметризованных фраз (строятся один раз
  // на смену store): [{{regex, template}}], template — перевод с {{{{n}}}}.
  var patterns = [];

  function escapeRegExp(s) {{
    return s.replace(/[.*+?^${{}}()|[\\]\\\\]/g, "\\\\$&");
  }}

  function compilePatterns() {{
    patterns = [];
    for (var original in store) {{
      if (!Object.prototype.hasOwnProperty.call(store, original)) continue;
      if (original.indexOf("{{{{") === -1) continue; // не параметризованная фраза
      var parts = original.split(/\\{{\\{{\\d+\\}}\\}}/);
      var regexSrc = "^" + parts.map(escapeRegExp).join("(.+?)") + "$";
      try {{
        patterns.push({{ regex: new RegExp(regexSrc), template: store[original] }});
      }} catch (e) {{ /* игнорируем некорректный шаблон */ }}
    }}
  }}

  function applyTemplate(template, values) {{
    return template.replace(/\\{{\\{{(\\d+)\\}}\\}}/g, function (_, i) {{
      return values[parseInt(i, 10)] != null ? values[parseInt(i, 10)] : "";
    }});
  }}

  function translateText(original) {{
    if (Object.prototype.hasOwnProperty.call(store, original)) {{
      return store[original];
    }}
    for (var i = 0; i < patterns.length; i++) {{
      var m = original.match(patterns[i].regex);
      if (m) {{
        return applyTemplate(patterns[i].template, m.slice(1));
      }}
    }}
    return null; // перевода нет — оставляем как есть
  }}

  function shouldSkip(node) {{
    var el = node.parentElement;
    if (!el) return true;
    var tag = el.tagName;
    if (tag === "SCRIPT" || tag === "STYLE" || tag === "NOSCRIPT") return true;
    if (el.closest && el.closest('[translate="no"],[data-translate="no"]')) return true;
    return false;
  }}

  function translateTextNode(node) {{
    if (shouldSkip(node)) return;
    var original = originalTextByNode.has(node) ? originalTextByNode.get(node) : node.nodeValue;
    var trimmed = original.trim();
    if (!trimmed) return;

    if (!originalTextByNode.has(node)) {{
      originalTextByNode.set(node, original);
    }}

    if (currentLang === fallbackLang) {{
      if (node.nodeValue !== original) node.nodeValue = original;
      return;
    }}

    var translated = translateText(trimmed);
    if (translated == null) return;
    node.nodeValue = original.replace(trimmed, translated);
  }}

  var TRANSLATABLE_ATTRS = ["placeholder", "title", "alt", "aria-label"];
  var originalAttrByEl = new WeakMap();

  function translateAttrs(el) {{
    for (var i = 0; i < TRANSLATABLE_ATTRS.length; i++) {{
      var attr = TRANSLATABLE_ATTRS[i];
      if (!el.hasAttribute(attr)) continue;
      var stored = originalAttrByEl.get(el) || {{}};
      var original = stored[attr] != null ? stored[attr] : el.getAttribute(attr);
      stored[attr] = original;
      originalAttrByEl.set(el, stored);

      if (currentLang === fallbackLang) {{
        el.setAttribute(attr, original);
        continue;
      }}
      var translated = translateText(original.trim());
      if (translated != null) el.setAttribute(attr, translated);
    }}
  }}

  function walk(root) {{
    var walker = document.createTreeWalker(
      root,
      NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT,
      null
    );
    var node;
    while ((node = walker.nextNode())) {{
      if (node.nodeType === 3) {{
        translateTextNode(node);
      }} else if (node.nodeType === 1) {{
        translateAttrs(node);
      }}
    }}
  }}

  var observer = null;
  function mountObserver() {{
    if (!dynamicDomEnabled || observer) return;
    observer = new MutationObserver(function (mutations) {{
      for (var i = 0; i < mutations.length; i++) {{
        var m = mutations[i];
        if (m.type === "characterData" && m.target) {{
          translateTextNode(m.target);
        }} else {{
          walk(m.target);
        }}
      }}
    }});
    observer.observe(document.body, {{
      childList: true,
      subtree: true,
      characterData: true,
    }});
  }}

  function setLanguage(lang) {{
    if (lang === currentLang) return Promise.resolve();

    var apply = function () {{
      currentLang = lang;
      compilePatterns();
      walk(document.body);
      try {{ localStorage.setItem("autoI18nLang", lang); }} catch (e) {{}}
    }};

    if (lang === fallbackLang) {{
      apply();
      return Promise.resolve();
    }}

    var url = translationsUrlTemplate.replace("{{lang}}", encodeURIComponent(lang));
    return fetch(url)
      .then(function (res) {{ return res.json(); }})
      .then(function (data) {{
        store = data || {{}};
        apply();
      }})
      .catch(function () {{ /* сеть недоступна — остаёмся на текущем языке */ }});
  }}

  compilePatterns();

  window.autoI18n = window.autoI18n || {{}};
  window.autoI18n.setLanguage = setLanguage;
  window.autoI18n.currentLang = currentLang;
  window.autoI18n.fallbackLang = fallbackLang;

  function init() {{
    walk(document.body);
    mountObserver();
  }}

  if (document.readyState === "loading") {{
    document.addEventListener("DOMContentLoaded", init);
  }} else {{
    init();
  }}
}})();
""".strip()
