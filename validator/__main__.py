"""CLI: python -m validator <файл требований> -o <файл отчёта>"""
import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from . import evaluate, html_report
from . import llm as llm_mod
from . import loader, pipeline, report


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # консоль Windows
    except Exception:
        pass
    p = argparse.ArgumentParser(prog="validator", description="Агент статической проверки требований")
    p.add_argument("input", help="Документ требований: PDF, DOCX, MD, TXT")
    p.add_argument("-o", "--output", default="reports/report.md", help="Файл отчёта .md (рядом появятся .html и .json)")
    p.add_argument("--model", help="Основная модель, например cerebras:gpt-oss-120b или gemini-3.8-flash (иначе GEMINI_MODEL из .env)")
    p.add_argument("-i", "--interactive", action="store_true", help="Задать уточняющие вопросы в терминале")
    p.add_argument("--gold", help="Эталон дефектов (gold/defects.md): посчитать полноту агента")
    p.add_argument("--no-critic", action="store_true", help="Отключить шаг критика")
    p.add_argument("--no-lenses", action="store_true", help="Отключить целевые проходы-чеклисты (быстрее, меньше полнота)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--full", dest="full", action="store_true", default=None,
                   help="Полный режим: пакеты по 6 требований, каждый проход и каждое ФТ отдельно (~30 запросов)")
    g.add_argument("--eco", dest="full", action="store_false",
                   help="Экономный режим (~14 запросов). По умолчанию режим выбирается по модели: lite — полный")
    p.add_argument("--no-cache", action="store_true", help="Не использовать кэш ответов модели")
    p.add_argument("--ensemble", help="Дополнительные модели через запятую (например gemini-3.5-flash-lite): "
                                      "проверяют те же требования параллельно, замечания объединяются. "
                                      "Иначе ENSEMBLE_MODELS из .env; пусто — одна модель")
    a = p.parse_args(argv)

    load_dotenv()
    report.agent_version()  # зафиксировать версию до того, как прогон перезапишет отчёты в git
    doc = loader.load(a.input)
    llm = llm_mod.from_env(a.model, cache=not a.no_cache)
    print(f"Документ: {doc.path.name}, строк: {len(doc.lines)}; модели (по порядку): {', '.join(llm.chain)}")
    extras = llm_mod.ensemble_from_env(a.ensemble, exclude=llm.chain[0], cache=not a.no_cache)

    ask = None
    if a.interactive:
        print("\nИнтерактивный режим: агент задаст уточняющие вопросы. Enter — пропустить вопрос.")

        def ask(q):
            print(f"\n? {q}")
            return input("  ответ: ").strip()

    res = pipeline.run(llm, doc, ask=ask, use_critic=not a.no_critic, use_lenses=not a.no_lenses, full=a.full,
                       ensemble=extras)
    out = Path(a.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.build(res), encoding="utf-8")
    data = report.to_json(res)
    out.with_suffix(".json").write_text(data, encoding="utf-8")
    out.with_suffix(".html").write_text(html_report.build(data), encoding="utf-8")
    print(f"\nМодель: {res.model}")
    print(f"Готово. Индекс готовности: {res.readiness()}/100, уникальных дефектов: {len(res.all_findings())}")
    print(f"Отчёт: {out.resolve()}\nHTML:  {out.with_suffix('.html').resolve()}\nJSON:  {out.with_suffix('.json').resolve()}")

    if a.gold:
        import json
        results = json.loads(data)
        gold = evaluate.parse_gold(a.gold)
        m, found, used = evaluate.evaluate(llm, results, gold)
        ev = out.with_name(out.stem + "_eval.md")
        ev.write_text(evaluate.render(results, gold, m, found, used), encoding="utf-8")
        print(f"Полнота против эталона: {len(found)}/{len(gold)}. Оценка: {ev.resolve()}")


if __name__ == "__main__":
    sys.exit(main())
