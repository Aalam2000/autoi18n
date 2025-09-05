import os
import json
from html.parser import HTMLParser
from openai import OpenAI


class SimpleHTMLTranslator(HTMLParser):
    def __init__(self, translate_callback):
        super().__init__()
        self.result = []
        self.translate_callback = translate_callback
        self._current_tag = None
        self._inside_skip = False

    def handle_starttag(self, tag, attrs):
        self._current_tag = tag
        self._inside_skip = False

        if tag in ("script", "style"):
            self._inside_skip = True

        # Кнопка langSwitch: пропускаем содержимое, но сам тег сохраняем
        for attr, value in attrs:
            if attr == "id" and value == "langSwitch":
                self._inside_skip = True

        self.result.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._inside_skip:
            self._inside_skip = False
        if tag == "button" and self._inside_skip:
            self._inside_skip = False
        self.result.append(f"</{tag}>")

    def handle_data(self, data):
        if self._inside_skip:
            # ⚡ Сохраняем содержимое <script> и <style> без изменений
            self.result.append(data)
        elif self._current_tag == "button":
            # Переводим обычные кнопки
            translated = self.translate_callback(data, prompt_type="button")
            self.result.append(translated)
        else:
            # Переводим обычный текст
            translated = self.translate_callback(data)
            self.result.append(translated)

    def get_html(self):
        return "".join(self.result)




class Translator:
    def __init__(self, cache_dir="./translations", api_key=None):
        # ⚡ Берём язык исходника из .env
        self.source_lang = os.getenv("SOURCE_LANG", "ru")
        self.cache_dir = cache_dir
        self.client = OpenAI(api_key=api_key)
        self._current_file = None
        self._cache = {}

    def _file_path(self, page_name: str, lang: str) -> str:
        filename = f"{page_name}.{lang}.json"
        return os.path.join(self.cache_dir, filename)

    def _load_storage(self, page_name: str, lang: str):
        path = self._file_path(page_name, lang)
        if os.path.exists(path):
            print(f"[CACHE] Загружаем переводы из {path}")
            with open(path, "r", encoding="utf-8") as f:
                self._cache = json.load(f)
        else:
            print(f"[CACHE] Файл {path} не найден — создаём новый")
            self._cache = {}
        self._current_file = path

    def _save_storage(self):
        if self._current_file:
            # ⚡ Создаём папку только при сохранении
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(self._current_file, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False, indent=2)
            print(f"[CACHE] Переводы сохранены в {self._current_file}")

    def translate_text(self, text: str, target_lang: str, page_name="page", prompt_type="normal") -> str:
        text = text.strip()
        if not text:
            return text

        if target_lang == self.source_lang:
            return text

        if not self._current_file:
            self._load_storage(page_name, target_lang)

        if text in self._cache:
            return self._cache[text]

        # ⚡ Разные подсказки для кнопок и для обычного текста
        if prompt_type == "button":
            prompt = f"Translate this button label briefly from {self.source_lang} to {target_lang}:\n\n{text}"
        else:
            prompt = f"Translate this text from {self.source_lang} to {target_lang}:\n\n{text}"

        response = self.client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
        )
        translated = response.choices[0].message.content.strip()

        self._cache[text] = translated
        self._save_storage()

        return translated

    def translate_html(self, html: str, target_lang: str, page_name="page") -> str:
        # ⚡ Если язык совпадает с исходным — возвращаем как есть
        if target_lang == self.source_lang:
            print(f"[SKIP] {self.source_lang} → {target_lang}: язык совпадает, возвращаем исходную страницу")
            return html

        # Загружаем кэш
        self._load_storage(page_name, target_lang)

        current_texts = []  # все тексты из текущей HTML-страницы

        def translate_with_chunks(text: str, prompt_type: str = "normal") -> str:
            text = text.strip()
            if not text:
                return text

            current_texts.append(text)

            # Проверяем кэш
            if text in self._cache:
                print(f"[CACHE-HIT] {self.source_lang} → {target_lang}: «{text[:40]}...»")
                return self._cache[text]

            # Если текст длинный — режем
            if len(text) > 2000:
                print(f"[SPLIT] Текст длинный ({len(text)} символов), режем на чанки")
                step = 1500
                chunks = [text[i:i + step] for i in range(0, len(text), step)]
            else:
                chunks = [text]

            translated_parts = []
            for chunk in chunks:
                if prompt_type == "button":
                    prompt = f"Translate this button label to {target_lang}. Keep it short, return only the translated label:\n\n{chunk}"
                else:
                    prompt = f"Translate to {target_lang}. Return only the translated text, no explanations:\n\n{chunk}"

                print(f"[OPENAI] {self.source_lang} → {target_lang}: «{chunk[:40]}...»")
                response = self.client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": prompt}],
                )
                translated_parts.append(response.choices[0].message.content.strip())

            translated_text = " ".join(translated_parts)

            self._cache[text] = translated_text
            self._save_storage()
            print(f"[OPENAI-RESULT] «{translated_text[:60]}...»")

            return translated_text

        # Парсим HTML
        parser = SimpleHTMLTranslator(
            translate_callback=translate_with_chunks
        )
        parser.feed(html)

        # ⚡ Синхронизация — удаляем устаревшие строки
        removed = []
        for old_key in list(self._cache.keys()):
            if old_key not in current_texts:
                removed.append(old_key)
                self._cache.pop(old_key)

        if removed:
            print(f"[SYNC] Удалены устаревшие строки: {len(removed)}")
            self._save_storage()

        return parser.get_html()

    def _collect_and_translate(self, text, target_lang, page_name, current_texts):
        text = text.strip()
        if text:
            current_texts.append(text)
        return self.translate_text(text, target_lang, page_name)

    def detect_browser_lang(self, accept_language: str) -> str:
        """Определяем язык браузера из заголовка Accept-Language"""
        if not accept_language:
            return self.source_lang
        return accept_language.split(",")[0].split("-")[0]

    def get_alternative_lang(self, current_lang: str, browser_lang: str) -> str:
        """Определяем какой язык должен быть на кнопке"""
        if current_lang == browser_lang:
            return "en"  # если мы на языке браузера → кнопка EN
        return browser_lang  # если мы на EN → кнопка = язык браузера
