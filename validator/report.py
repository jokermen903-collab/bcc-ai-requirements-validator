"""Отчёт в Markdown по разделам из ТЗ хакатона (п. 6) + выгрузка JSON."""
import datetime as dt
import functools
import json
import subprocess
from pathlib import Path

from . import __version__
from .pipeline import SEV_ORDER, Result
from .rules import RuleFinding


@functools.lru_cache(maxsize=1)
def agent_version() -> str:
    """Считается один раз за процесс: первый вызов — до записи отчётов, поэтому report.md, .json и .html
    получают одну и ту же версию, без «-dirty» от только что перезаписанного соседнего отчёта."""
    try:
        r = subprocess.run(["git", "describe", "--tags", "--always", "--dirty"], capture_output=True, text=True,
                           cwd=Path(__file__).parent, timeout=5)
        sha = r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        sha = ""
    return f"{__version__} ({sha})" if sha else f"{__version__} (коммит не определён: запуск не из git)"


def _cell(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ").strip()


def numbered(res: Result):
    """[(F-n, к чему относится, Finding)] в порядке: требования, набор, правила."""
    return [(f"F-{i}", owner, f) for i, (owner, f) in enumerate(res.all_findings(), 1)]


def source(f) -> str:
    return "правило" if isinstance(f, RuleFinding) or getattr(f, "rule", "") else "модель"


def build(res: Result) -> str:
    seg, loc = res.segmentation, res.locator
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    items = numbered(res)
    fid = {id(f): n for n, _, f in items}
    sev = {}
    for _, _, f in items:
        sev[f.severity.value] = sev.get(f.severity.value, 0) + 1
    crit = [(n, o, f) for n, o, f in items if f.severity.value == "критичный"]

    L = []
    a = L.append
    a(f"# Отчёт о проверке требований: {seg.document_title}\n")

    a("## 1. Паспорт прогона\n")
    a("| Поле | Значение |\n| :--- | :--- |")
    a(f"| Документ | {_cell(seg.document_title)} (`{res.doc.path.name}`) |")
    a(f"| Дата прогона | {now} |")
    a(f"| Модель | {res.model} |")
    a(f"| Нарезка требований | {getattr(seg, '_source', 'модель')}, {len(seg.requirements)} шт. |")
    a(f"| Версия агента | {agent_version()} |")
    a("| Критерии | ISO/IEC/IEEE 29148:2018 п. 5.2.5–5.2.6; IEEE Std 830-1998 п. 4.3 |")
    a(f"| Шаги | нарезка → правила → проверка требований → проверка набора → 8 целевых проходов (в экономном режиме 3 группы) → верификация цитат → критик → уточнения → итог |")
    a(f"| Обращений к модели | {res.stats.get('llm_calls', 0)} (из кэша: {res.stats.get('cache_hits', 0)}) |\n")

    a("## 2. Как агент нарезал требования\n")
    a("| ID в отчёте | ID в документе | Раздел | Тип | Кратко | Место |\n| :--- | :--- | :--- | :--- | :--- | :--- |")
    for r in seg.requirements:
        a(f"| {r.report_id} | {_cell(r.doc_id or '— (присвоен агентом)')} | {_cell(r.section)} | {_cell(r.kind)} | "
          f"{_cell(r.summary)} | {loc.where(r.quote)} |")
    a("")

    a("## 3. Проверка каждого требования\n")
    by_id = {r.report_id: r for r in seg.requirements}
    for c in res.checks:
        r = by_id.get(c.report_id)
        verdict = c.verdict if c.findings else "соответствует"
        a(f"### {c.report_id}" + (f" ({r.doc_id})" if r and r.doc_id else "") + f": {verdict}\n")
        if r:
            a(f"> {r.quote}\n")
        if not c.findings:
            a("Замечаний нет.\n")
            continue
        for f in sorted(c.findings, key=lambda x: SEV_ORDER.get(x.severity.value, 3)):
            _finding(a, fid[id(f)], f, loc)
        a("")

    a("## 4. Проверка всего набора требований\n")
    for f in sorted(res.set_check.findings, key=lambda x: SEV_ORDER.get(x.severity.value, 3)):
        rel = ", ".join(f.related_ids) or "весь набор"
        _finding(a, fid[id(f)], f, loc, extra=f"затронуты: {rel}")
    if res.rule_findings:
        a("\n**Найдено детерминированными правилами (без LLM):**\n")
        for f in res.rule_findings:
            _finding(a, fid[id(f)], f, loc)
    if res.set_check.missing_topics:
        a("\n**Пропуски в наборе:**")
        for m in res.set_check.missing_topics:
            a(f"- {m}")
    if res.set_check.coverage:
        a("\n**Трассировка требований на критерии приёмки:**\n")
        a("| Требование | Кратко | Покрыто критериями приёмки |\n| :--- | :--- | :--- |")
        for cv in res.set_check.coverage:
            r = by_id.get(cv.report_id)
            a(f"| {cv.report_id}{' (' + r.doc_id + ')' if r and r.doc_id else ''} | {_cell(r.summary if r else '')} | "
              f"{', '.join(cv.covered_by) if cv.covered_by else '**не покрыто**'} |")
        unc = [cv.report_id for cv in res.set_check.coverage if not cv.covered_by]
        total = len(res.set_check.coverage)
        a(f"\nПокрыто критериями приёмки: {total - len(unc)} из {total}.")
    a("")

    a("## 5. Уточняющие вопросы к заказчику\n")
    for i, (q, ans) in enumerate(res.answers.items(), 1):
        a(f"{i}. {q}  \n   Ответ: {ans or 'не получен'}")
    a("")

    a("## 6. Сводка\n")
    a(f"- Требований выделено: **{len(seg.requirements)}**")
    a(f"- Требований с замечаниями: **{sum(1 for c in res.checks if c.findings)}**")
    a(f"- Уникальных дефектов: **{len(items)}** (" + ", ".join(
        f"{k}: {sev[k]}" for k in sorted(sev, key=lambda k: SEV_ORDER.get(k, 3))) + ")")
    a(f"- Из них найдено правилами без LLM: {len(res.rule_findings)}")
    a(f"- Индекс готовности документа: **{res.readiness()}/100** (эвристика агента: 100·e^(−штраф/150), штраф = 10 за критичный, 4 за существенный, 1 за незначительный дефект)")
    merged = sum(getattr(f, "merged_count", 1) for _, _, f in items)
    if merged > len(items):
        a(f"- Дубли объединены: {merged} замечаний ревьюеров → {len(items)} уникальных дефектов")
    a(f"- Контроль качества: отброшено верификатором цитат {len(res.rejected)}, отклонено критиком {len(res.dismissed)}")
    a("\n**Критичные дефекты:**")
    for n, o, f in crit:
        a(f"- {n} ({o}): {f.criterion}. {f.problem} ({loc.where(f.quote)})")
    if not crit:
        a("- нет")
    a("")

    v = res.verdict
    a("## 7. Итог\n")
    a(f"- Можно начинать разработку: **{'да' if v.ready_for_development else 'нет'}**")
    a(f"- Можно начинать тестирование: **{'да' if v.ready_for_testing else 'нет'}**\n")
    a(v.rationale + "\n")
    a("**Исправить в первую очередь:**")
    for i, t in enumerate(v.top_fixes, 1):
        a(f"{i}. {t}")
    a("")

    if res.dismissed or res.rejected:
        a("## Приложение. Отклонённые замечания (прозрачность)\n")
        for d in res.dismissed:
            a(f"- Критик: «{d.finding.quote}». {d.finding.problem} Причина: {d.reason}")
        for f in res.rejected:
            a(f"- Верификатор: цитата не найдена в документе: «{f.quote}»")
        a("")
    return "\n".join(L)


def _finding(a, n, f, loc, extra=""):
    meta = "; ".join(x for x in [loc.where(f.quote), extra, source(f)] if x)
    a(f"- **{n} [{f.severity.value}] {f.criterion}** ({meta}): «{f.quote}»  ")
    a(f"  Проблема: {f.problem}  ")
    also = getattr(f, "also_quotes", [])
    if also:
        a("  Тот же дефект также здесь: " + "; ".join(f"{loc.where(q)} «{q}»" for q in also) + "  ")
    a(f"  Исправить: {f.fix}  ")
    a(f"  Вопрос: {f.question}")


def to_json(res: Result) -> str:
    data = {
        "document": res.doc.path.name,
        "title": res.segmentation.document_title,
        "model": res.model,
        "agent_version": agent_version(),
        "readiness": res.readiness(),
        "segmentation_source": getattr(res.segmentation, "_source", "модель"),
        "requirements": [r.model_dump() for r in res.segmentation.requirements],
        "findings": [
            {"id": n, "owner": o, "source": source(f), "location": res.locator.where(f.quote),
             "also": [{"quote": q, "location": res.locator.where(q)} for q in getattr(f, "also_quotes", [])],
             **{k: v for k, v in f.model_dump(mode="json").items() if k not in ("also_quotes", "rule")}}
            for n, o, f in numbered(res)
        ],
        "coverage": [c.model_dump() for c in res.set_check.coverage],
        "missing_topics": res.set_check.missing_topics,
        "questions": res.answers,
        "verdict": res.verdict.model_dump(),
        "rejected": [f.model_dump(mode="json") for f in res.rejected],
        "dismissed": [{"reason": d.reason, **d.finding.model_dump(mode="json")} for d in res.dismissed],
        "stats": res.stats,
    }
    return json.dumps(data, ensure_ascii=False, indent=2)
