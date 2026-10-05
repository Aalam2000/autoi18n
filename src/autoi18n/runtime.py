# src/autoi18n/runtime.py
"""
Генерация клиентского JS-рантайма.

Архитектура v2: рантайм НЕ требует вызовов в коде компонентов (никаких
t()/translateKey()). Вместо этого он после рендера проходит по уже
отрисованному DOM и подменяет видимый текст на перевод — так же для
JSX-контента, обычного HTML и текста, который код меняет императивно
(например, textContent счётчика или таймера). Динамические изменения DOM
(перерисовка React, обновление счётчика/таймера) подхватываются через
MutationObserver — если он включён.

Нативные диалоги alert()/confirm() в DOM не попадают — их сообщение
переводится в момент вызова (обёртка над window.alert / window.confirm).

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
from typing import Dict, List, Optional


def build_frontend_runtime_script(
    translations: Dict[str, str],
    fallback_lang: str = "ru",
    dynamic_dom_enabled: bool = True,
    translations_url_template: Optional[str] = None,
    extra_attrs: Optional[List[str]] = None,
) -> str:
    payload = json.dumps(translations or {}, ensure_ascii=False)
    # стандартные атрибуты + дополнительные из данных проекта (autoi18n.json "attributes")
    attrs = ["placeholder", "title", "alt", "aria-label", "label"]
    attrs += [a for a in (extra_attrs or []) if a not in attrs]
    attrs_json = json.dumps(attrs)
    observer_enabled = "true" if dynamic_dom_enabled else "false"
    url_template = json.dumps(translations_url_template or "/i18n/translations?lang={lang}")

    return f"""
(function () {{
  var store = {payload};
  var fallbackLang = {json.dumps(fallback_lang)};
  var dynamicDomEnabled = {observer_enabled};
  var translationsUrlTemplate = {url_template};
  var currentLang = fallbackLang;

  // Для каждого узла храним {{ original, lastApplied }}, а НЕ просто
  // "оригинал, зафиксированный раз и навсегда". Иначе первое же значение,
  // увиденное узлом (например, пустое имя пользователя до ответа /auth/me),
  // навсегда застревает как "оригинал" — и когда React позже обновляет тот
  // же текстовый узел настоящими данными, рантайм ошибочно принимает это
  // за "перевод откатился" и затирает свежее значение обратно на старое.
  // lastApplied — то, что мы сами последний раз туда подставили (оригинал
  // или перевод). Если текущее значение узла совпадает с lastApplied — это
  // наша же подстановка, отражённая обратно (просто смена языка либо
  // повторный проход MutationObserver), и "original" переиспользуем как
  // есть. Если не совпадает — значит, контент подменил кто-то извне (React
  // перерисовал узел новыми данными), и именно это новое значение и есть
  // настоящий новый "оригинал".
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
    var current = node.nodeValue;
    var entry = originalTextByNode.get(node);

    var original;
    if (!entry) {{
      original = current; // впервые видим узел — текущее значение и есть оригинал
    }} else if (current === entry.lastApplied) {{
      original = entry.original; // это наша же подстановка, отражённая обратно
    }} else {{
      original = current; // контент подменили извне — это новый оригинал
    }}

    var trimmed = original.trim();
    if (!trimmed) {{
      originalTextByNode.set(node, {{ original: original, lastApplied: current }});
      return;
    }}

    var nextValue;
    if (currentLang === fallbackLang) {{
      nextValue = original;
    }} else {{
      var translated = translateText(trimmed);
      nextValue = translated == null ? original : original.replace(trimmed, translated);
    }}

    if (node.nodeValue !== nextValue) node.nodeValue = nextValue;
    originalTextByNode.set(node, {{ original: original, lastApplied: nextValue }});
  }}

  var TRANSLATABLE_ATTRS = {attrs_json};
  var originalAttrByEl = new WeakMap();

  function translateAttrs(el) {{
    for (var i = 0; i < TRANSLATABLE_ATTRS.length; i++) {{
      var attr = TRANSLATABLE_ATTRS[i];
      if (!el.hasAttribute(attr)) continue;
      var current = el.getAttribute(attr);
      var stored = originalAttrByEl.get(el) || {{}};
      var entry = stored[attr];

      var original;
      if (!entry) {{
        original = current;
      }} else if (current === entry.lastApplied) {{
        original = entry.original;
      }} else {{
        original = current;
      }}

      var trimmed = original.trim();
      var nextValue;
      if (!trimmed) {{
        nextValue = original;
      }} else if (currentLang === fallbackLang) {{
        nextValue = original;
      }} else {{
        var translated = translateText(trimmed);
        nextValue = translated == null ? original : translated;
      }}

      if (current !== nextValue) el.setAttribute(attr, nextValue);
      stored[attr] = {{ original: original, lastApplied: nextValue }};
      originalAttrByEl.set(el, stored);
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
        }} else if (m.type === "attributes" && m.target) {{
          // React поменял значение подсказки/placeholder и т.п. — переводим заново
          translateAttrs(m.target);
        }} else {{
          walk(m.target);
        }}
      }}
    }});
    observer.observe(document.body, {{
      childList: true,
      subtree: true,
      characterData: true,
      attributes: true,
      attributeFilter: TRANSLATABLE_ATTRS,
    }});
  }}

  function setLanguage(lang) {{
    if (lang === currentLang) {{
      return Promise.resolve();
    }}

    var apply = function () {{
      currentLang = lang;
      // window.autoI18n.currentLang — публичное поле, по которому код
      // проекта (например, useLang() на фронтенде) узнаёт текущий язык
      // при своей инициализации. Раньше оно выставлялось только один раз
      // при загрузке скрипта и после первого переключения языка навсегда
      // врало старое значение — держим его синхронным с реальным языком.
      window.autoI18n.currentLang = lang;
      compilePatterns();
      // Обход DOM не оборачивали в try/catch — одно исключение на одном
      // узле обрывало весь walk() молча, и переключение выглядело так,
      // будто вообще ничего не произошло (в любую сторону, включая ru).
      try {{
        walk(document.body);
      }} catch (e) {{
        console.error("autoi18n: ошибка при обходе DOM", e);
      }}
      try {{ localStorage.setItem("autoI18nLang", lang); }} catch (e) {{}}
      // Оповещаем уже смонтированные компоненты (например, независимые
      // друг от друга вызовы useLang() в разных местах React-дерева) —
      // без этого события они держат язык, каким он был на момент их
      // собственного монтирования, и не узнают о переключении, сделанном
      // из другого места страницы.
      try {{
        window.dispatchEvent(new CustomEvent("autoi18nlangchange", {{ detail: {{ lang: lang }} }}));
      }} catch (e) {{}}
    }};

    if (lang === fallbackLang) {{
      apply();
      return Promise.resolve();
    }}

    var url = translationsUrlTemplate.replace("{{lang}}", encodeURIComponent(lang));
    return fetch(url)
      .then(function (res) {{
        return res.json();
      }})
      .then(function (data) {{
        store = data || {{}};
        apply();
      }})
      .catch(function (e) {{
        console.error("autoi18n: не удалось загрузить словарь", e);
      }});
  }}

  // Нативные диалоги alert()/confirm() — не DOM, обход и MutationObserver их
  // не видят: переводим сообщение в момент вызова тем же словарём и масками.
  function translateMessage(message) {{
    if (currentLang === fallbackLang || typeof message !== "string") return message;
    var trimmed = message.trim();
    if (!trimmed) return message;
    var translated = translateText(trimmed);
    return translated == null ? message : message.replace(trimmed, function () {{ return translated; }});
  }}

  function wrapDialog(name) {{
    var native = window[name];
    if (typeof native !== "function" || native.autoI18nWrapped) return;
    var wrapped = function () {{
      var args = Array.prototype.slice.call(arguments);
      if (args.length) args[0] = translateMessage(args[0]);
      return native.apply(window, args);
    }};
    wrapped.autoI18nWrapped = true;
    window[name] = wrapped;
  }}

  wrapDialog("alert");
  wrapDialog("confirm");

  compilePatterns();

  window.autoI18n = window.autoI18n || {{}};
  window.autoI18n.setLanguage = setLanguage;
  window.autoI18n.currentLang = currentLang;
  window.autoI18n.fallbackLang = fallbackLang;

  function init() {{
    // Если пользователь раньше переключал язык, он сохранён в localStorage
    // (см. setLanguage → apply → localStorage.setItem). Раньше init() этого
    // не проверял и всегда стартовал с fallbackLang — поэтому при простом
    // обновлении страницы (F5) язык в переключателе (он тоже читает
    // localStorage, но независимо, через useLang()) визуально оставался
    // выбранным, а сам текст на странице откатывался на исходный язык.
    var saved = null;
    try {{
      saved = localStorage.getItem("autoI18nLang");
    }} catch (e) {{}}
    if (saved && saved !== fallbackLang) {{
      setLanguage(saved);
    }} else {{
      walk(document.body);
    }}
    mountObserver();
  }}

  if (document.readyState === "loading") {{
    document.addEventListener("DOMContentLoaded", init);
  }} else {{
    init();
  }}
}})();
""".strip()
