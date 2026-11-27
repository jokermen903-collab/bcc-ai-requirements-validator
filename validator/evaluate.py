"""Оценка качества агента против эталонного списка дефектов (gold/defects.md).

Полнота (recall) = доля эталонных дефектов, которые агент нашёл.
Сопоставление «эталон ↔ замечание» делает модель-судья (LLM-as-a-judge),
итог считается кодом. Запуск:
    python -m validator.evaluate reports/results.json gold/defects.md -o reports/eval.md
"""
import argparse
import json
import re
from pathlib import Path
from typing import List, Tuple

from .models import EvalResult

JUDGE = ("Ты — строгий эксперт по ревью требований. Сопоставь эталонные дефекты с замечаниями агента. "
         "Дефект считается найденным, только если замечание агента указывает на ту же проблему в том же месте "
         "документа (формулировка может отличаться). Не засчитывай общие или соседние замечания.")


def parse_gold(path: str) -> List[Tuple[int, str, str]]:
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and re.fullmatch(r"\d+", cells[0]):
            out.append((int(cells[0]), cells[1], cells[2]))
    return out


def evaluate(llm, results: dict, gold: List[Tuple[int, str, str]]):
    fs = results["findings"]
    listing = "\n".join(f"{f['id']}: [{f['criterion']}] «{f['quote']}» — {f['problem']}" for f in fs)
    listing += "\n" + "\n".join(f"M-{i}: [пропуск в наборе] {m}" for i, m in enumerate(results.get("missing_topics", []), 1))
    gl = "\n".join(f"{n}. ({where}) {what}" for n, where, what in gold)
    res = llm.ask(JUDGE, f"ЭТАЛОН:\n{gl}\n\nЗАМЕЧАНИЯ АГЕНТА:\n{listing}\n\nВерни запись для каждого номера эталона.", EvalResult)
    m = {x.gold_no: x for x in res.matches}
    found = [n for n, _, _ in gold if m.get(n) and m[n].found]
    used = {fid for x in res.matches if x.found for fid in x.finding_ids}
    return m, found, used


def render(results: dict, gold, m, found, used) -> str:
    fs = results["findings"]
    recall = len(found) / len(gold) if gold else 0
    used_f = {u for u in used if u.startswith("F-")}
    prec = len(used_f) / len(fs) if fs else 0
    L = [f"# Оценка агента против эталона\n",
         f"- Модель: {results['model']}, версия агента: {results['agent_version']}",
         f"- Эталонных дефектов: {len(gold)}; найдено агентом: **{len(found)}** → полнота **{recall:.0%}**",
         f"- Замечаний агента: {len(fs)}; из них совпали с эталоном: {len(used_f)} ({prec:.0%}). "
         f"Остальные — не обязательно ошибки: эталон составлен вручную и может быть неполным.\n",
         "| # | Где | Эталонный дефект | Найден | Замечания агента |", "| :- | :- | :- | :- | :- |"]
    for n, where, what in gold:
        x = m.get(n)
        L.append(f"| {n} | {where} | {what} | {'да' if x and x.found else '**нет**'} | {', '.join(x.finding_ids) if x else ''} |")
    extra = [f for f in fs if f["id"] not in used]
    if extra:
        L.append("\n## Замечания агента вне эталона (кандидаты на пополнение эталона)\n")
        for f in extra:
            L.append(f"- {f['id']} [{f['severity']}] {f['criterion']}: {f['problem']}")
    return "\n".join(L) + "\n"


def main(argv=None):
    from dotenv import load_dotenv

    from . import llm as llm_mod

    p = argparse.ArgumentParser(prog="validator.evaluate")
    p.add_argument("results", help="results.json прогона")
    p.add_argument("gold", help="gold/defects.md")
    p.add_argument("-o", "--output", default="reports/eval.md")
    p.add_argument("--model")
    a = p.parse_args(argv)
    load_dotenv()
    results = json.loads(Path(a.results).read_text(encoding="utf-8"))
    gold = parse_gold(a.gold)
    m, found, used = evaluate(llm_mod.from_env(a.model), results, gold)
    Path(a.output).parent.mkdir(parents=True, exist_ok=True)
    Path(a.output).write_text(render(results, gold, m, found, used), encoding="utf-8")
    print(f"Полнота: {len(found)}/{len(gold)}. Отчёт об оценке: {Path(a.output).resolve()}")


if __name__ == "__main__":
    main()
