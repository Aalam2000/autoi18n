# src/autoi18n/runtime.py
import json
from typing import Dict

def build_frontend_runtime_script(
    translations: Dict[str, str],
    fallback_lang: str = "ru",
    dynamic_dom_enabled: bool = False,
) -> str:
    payload = json.dumps(translations or {}, ensure_ascii=False)
    observer_enabled = "true" if dynamic_dom_enabled else "false"

    return f"""
(function () {{
  var store = {payload};
  var fallbackLang = {json.dumps(fallback_lang)};
  var dynamicDomEnabled = {observer_enabled};
  var currentLang = null;
  var baseUrl = "";

  function translateKey(key, fallback) {{
    // key – это оригинальный текст (или любой ключ)
    if (typeof key !== "string" || !key.trim()) {{
      return fallback || key;
    }}
    // Сначала ищем по самому key (если передан осмысленный ключ)
    if (Object.prototype.hasOwnProperty.call(store, key)) {{
      return store[key];
    }}
    // Если не нашли – возвращаем fallback
    return fallback || key;
  }}

  // ... остальная часть функции (translateElement, translateDom, setLanguage, mountObserver) остаётся без изменений ...
  // (она уже есть в вашем runtime.py, её не трогаем)

  window.autoI18n = window.autoI18n || {{}};
  window.autoI18n.store = store;
  window.autoI18n.fallbackLang = fallbackLang;
  window.autoI18n.translateKey = translateKey;
  window.autoI18n.translateDom = translateDom;
  window.autoI18n.setLanguage = setLanguage;
  window.autoI18n.currentLang = currentLang;

  if (document.readyState === "loading") {{
    document.addEventListener("DOMContentLoaded", function () {{
      translateDom(document);
      mountObserver();
    }});
  }} else {{
    translateDom(document);
    mountObserver();
  }}
}})();
""".strip()