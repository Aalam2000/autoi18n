# src/autoi18n/cli.py
"""
CLI autoi18n.

Команды:
  autoi18n scan --dry-run   — обязательный отладочный этап (см. README).
                               Ничего не пишет и не требует OPENAI_API_KEY.
                               Показывает, что именно нашла библиотека —
                               чтобы руками проверить на мусор/пропуски
                               ДО подключения ИИ.
  autoi18n scan              — реальное сканирование: новые фразы уходят
                               в реестр исходного языка и в очередь на
                               перевод для всех активных целевых языков.
  autoi18n translate          — обрабатывает очередь (нужен OPENAI_API_KEY
                               или другой настроенный AI-провайдер).
  autoi18n add-lang <lang>    — добавляет целевой язык (пишет в .env) и
                               сразу переводит весь реестр исходного языка
                               на него. CLI-эквивалент действия в админке.
  autoi18n coverage <lang>    — процент покрытия перевода для языка.
"""
import argparse
import json
import sys

from .translator import Translator


def _cmd_scan(args: argparse.Namespace) -> int:
    translator = Translator(env_path=args.env)
    report = translator.extract(dry_run=args.dry_run)

    if args.dry_run:
        print(f"Просканировано файлов: {report['files_scanned']}")
        print(f"Найдено фраз: {report['phrases_found']}")
        if args.json:
            print(json.dumps(report["items"], ensure_ascii=False, indent=2))
        else:
            for item in report["items"]:
                marker = f" [{{{item['placeholders']}}} параметров]" if item["placeholders"] else ""
                print(f"  [{item['kind']}] {item['file']}:{item['line']} — {item['text']!r}{marker}")
        if report.get("items"):
            print(
                "\nЭто dry-run: ничего не записано и не поставлено в очередь. "
                "Проверьте список на мусор/пропуски, затем запустите 'autoi18n scan' без --dry-run."
            )
        return 0

    print(f"Просканировано файлов: {report['files_scanned']}")
    print(f"Найдено фраз всего: {report['phrases_found']}")
    print(f"Новых фраз добавлено в реестр и очередь: {report['new_phrases']}")
    return 0


def _cmd_translate(args: argparse.Namespace) -> int:
    translator = Translator(env_path=args.env)
    processed = translator.process_queue(batch_size=args.batch_size)
    print(f"Обработано фраз: {processed}")
    return 0


def _cmd_add_lang(args: argparse.Namespace) -> int:
    translator = Translator(env_path=args.env)
    result = translator.add_target_lang(args.lang, batch_size=args.batch_size)
    if not result["added"]:
        print(f"Язык '{args.lang}' уже есть в списке целевых (или совпадает с исходным) — ничего не изменено.")
        return 0
    print(f"Язык '{args.lang}' добавлен в .env. Переведено фраз: {result['translated']}")
    return 0


def _cmd_coverage(args: argparse.Namespace) -> int:
    translator = Translator(env_path=args.env)
    stats = translator.get_translation_coverage(args.lang)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


def _cmd_langs(args: argparse.Namespace) -> int:
    translator = Translator(env_path=args.env)
    print(f"Исходный язык: {translator.source_lang}")
    print(f"Целевые языки: {', '.join(translator.get_target_langs()) or '(нет)'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autoi18n", description="autoi18n — автоперевод UI без доработки кода проекта")
    parser.add_argument("--env", default=".env", help="путь к .env проекта (по умолчанию: .env)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="сканировать проект и найти фразы")
    p_scan.add_argument("--dry-run", action="store_true", help="только показать найденное, ничего не писать (обязательный отладочный этап)")
    p_scan.add_argument("--json", action="store_true", help="вывести отчёт dry-run как JSON")
    p_scan.set_defaults(func=_cmd_scan)

    p_translate = sub.add_parser("translate", help="перевести очередь (требует настроенного ИИ)")
    p_translate.add_argument("--batch-size", type=int, default=50)
    p_translate.set_defaults(func=_cmd_translate)

    p_add_lang = sub.add_parser("add-lang", help="добавить целевой язык и сразу перевести на него реестр")
    p_add_lang.add_argument("lang", help="код языка, например en")
    p_add_lang.add_argument("--batch-size", type=int, default=50)
    p_add_lang.set_defaults(func=_cmd_add_lang)

    p_coverage = sub.add_parser("coverage", help="показать процент покрытия перевода для языка")
    p_coverage.add_argument("lang")
    p_coverage.set_defaults(func=_cmd_coverage)

    p_langs = sub.add_parser("langs", help="показать исходный и целевые языки")
    p_langs.set_defaults(func=_cmd_langs)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
