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
  autoi18n audit [--lang]     — проверить уже сохранённые переводы: плохие
                               переводы, фразы с исчерпанными попытками,
                               документы без актуального перевода.
  autoi18n retranslate ...    — перевести заново: --audit (всё найденное
                               audit), --text "исходный текст", --contains
                               "подстрока"; --no-run — только поставить в очередь.
  autoi18n set <lang> <текст> <перевод> — ручная правка перевода по тексту.
  autoi18n docs [--dry-run] [--force] — перевести документы (.md) сейчас.
  autoi18n report [--json]    — итоги последнего цикла (файл _report.json).
"""
import argparse
import json
import os
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
    translator.save_report("scan")
    print(f"Отчёт: {translator.report.report_path}")
    return 0


def _cmd_translate(args: argparse.Namespace) -> int:
    translator = Translator(env_path=args.env)
    processed = translator.process_queue(batch_size=args.batch_size)
    translator.save_report("translate")
    print(f"Переведено фраз: {processed}. Отчёт: {translator.report.report_path}")
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


def _cmd_audit(args: argparse.Namespace) -> int:
    translator = Translator(env_path=args.env)
    report = translator.audit(args.lang)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    for row in report["bad"]:
        print(f"[{row['lang']}] ПЛОХО  {row['text']!r} -> {row['translation']!r}: {'; '.join(row['problems'])}")
    for row in report["failed"]:
        print(f"[{row['lang']}] НЕ ПЕРЕВЕДЕНО ({row['attempts']} попыток) {row['text']!r}: {'; '.join(row['problems'])}")
    for row in report["documents"]:
        print(f"[{row['lang']}] ДОКУМЕНТ {row['state']}: {row['source']} {row.get('problem') or ''}".rstrip())
    print(
        f"Итого: плохих переводов {len(report['bad'])}, не переведено {len(report['failed'])}, "
        f"документов без перевода {len(report['documents'])}, в очереди {report['pending']}"
    )
    return 0


def _cmd_retranslate(args: argparse.Namespace) -> int:
    if not (args.audit or args.text or args.contains):
        print("Укажите, что переводить заново: --audit, --text или --contains")
        return 2
    translator = Translator(env_path=args.env)
    result = translator.retranslate(
        lang=args.lang, texts=args.text, contains=args.contains,
        from_audit=args.audit, run=not args.no_run, batch_size=args.batch_size,
    )
    translator.save_report("retranslate")
    print(f"Поставлено в очередь: {result['queued']}, переведено: {result['translated']}. "
          f"Отчёт: {translator.report.report_path}")
    return 0


def _cmd_set(args: argparse.Namespace) -> int:
    translator = Translator(env_path=args.env)
    try:
        problems = translator.set_translation(args.lang, args.text, args.translation)
    except ValueError as e:
        print(str(e))
        return 1
    print("Сохранено." + (f" Замечания: {'; '.join(problems)}" if problems else ""))
    return 0


def _cmd_docs(args: argparse.Namespace) -> int:
    translator = Translator(env_path=args.env, doc_paths=args.path or None)
    report = translator.translate_documents(force=args.force, dry_run=args.dry_run)
    for line in report["translated"]:
        print(f"переведено: {line}")
    for line in report["failed"]:
        print(f"НЕ УДАЛОСЬ: {line}")
    for line in report["skipped"]:
        print(f"пропущено (лимит попыток, см. --force): {line}")
    print(f"Актуальных переводов: {report['up_to_date']}")
    if not args.dry_run:
        translator.save_report("docs")
        print(f"Отчёт: {translator.report.report_path}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    translator = Translator(env_path=args.env)
    path = translator.report.report_path
    if not os.path.exists(path):
        print(f"Отчёта ещё нет: {path}")
        return 1
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    translator.report.data = data
    print(f"{data.get('kind')} {data.get('started')} → {data.get('finished')}, версия {data.get('version')}")
    print(translator.report.summary())
    print(f"Подробно: {path}; журнал: {translator.report.log_path}")
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

    p_audit = sub.add_parser("audit", help="проверить сохранённые переводы")
    p_audit.add_argument("--lang")
    p_audit.add_argument("--json", action="store_true")
    p_audit.set_defaults(func=_cmd_audit)

    p_re = sub.add_parser("retranslate", help="перевести заново выбранные фразы")
    p_re.add_argument("--lang")
    p_re.add_argument("--audit", action="store_true", help="всё, что нашёл audit")
    p_re.add_argument("--text", action="append", help="точный исходный текст (можно несколько раз)")
    p_re.add_argument("--contains", help="все фразы, содержащие подстроку")
    p_re.add_argument("--no-run", action="store_true", help="только поставить в очередь")
    p_re.add_argument("--batch-size", type=int, default=50)
    p_re.set_defaults(func=_cmd_retranslate)

    p_set = sub.add_parser("set", help="вручную задать перевод фразы")
    p_set.add_argument("lang")
    p_set.add_argument("text", help="исходный текст фразы")
    p_set.add_argument("translation")
    p_set.set_defaults(func=_cmd_set)

    p_docs = sub.add_parser("docs", help="перевести документы (.md) сейчас")
    p_docs.add_argument("--path", action="append", help="папка документов (по умолчанию из настроек)")
    p_docs.add_argument("--dry-run", action="store_true")
    p_docs.add_argument("--force", action="store_true", help="игнорировать лимит неудачных попыток")
    p_docs.set_defaults(func=_cmd_docs)

    p_report = sub.add_parser("report", help="итоги последнего цикла воркера / команды")
    p_report.add_argument("--json", action="store_true", help="весь отчёт целиком")
    p_report.set_defaults(func=_cmd_report)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
