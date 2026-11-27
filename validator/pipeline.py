"""Пайплайн агента.

1 нарезка → 2 правила (код) → 3 проверка требований (LLM) → 4 проверка набора и
трассировка (LLM) → 5 верификатор цитат (код) → 6 критик (LLM) → 7 уточнения →
8 итог (LLM).
"""
import math
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from . import criteria, rules
from .loader import Document
from .models import (Severity, BatchCheck, Clustering, Recheck, CriticReview, Finding, LensCheck, MergedFinding, ScenarioCheck, RequirementCheck, Segmentation, SetCheckFull,
                     Verdict)
from .verify import Locator

SYSTEM = (
    "Ты — ведущий системный аналитик и ревьюер требований в банке. Проводишь статическое ревью "
    "спецификации до разработки по ISO/IEC/IEEE 29148:2018 (5.2.5, 5.2.6) и IEEE 830-1998 (4.3). "
    "Пиши по-русски. Каждое замечание опирай на ТОЧНУЮ дословную цитату из документа (копируй символ в символ, "
    "без меток [стр:строка], цитата — фрагмент одной строки). Не выдумывай дефекты и не пиши общих фраз без "
    "привязки к тексту. Если требование корректно — так и скажи, замечаний не добавляй. "
    "Замечание пиши, только если из-за дефекта разработчик или тестировщик сделает что-то по-разному или не сможет "
    "сделать вовсе; пожелания «хорошо бы добавить» без такого следствия не пиши. Один дефект — одно замечание: "
    "не дроби его на части и не повторяй в другом месте."
)

BATCH = 6
ECONOMY_BATCHES = 3  # пакетов проверки требований в экономном режиме
CRITIC_MAX_SHARE = 0.35  # доля отклонений критика, выше которой перепроверяются все отклонения
MERGE_SECOND_SHARE = 0.45  # если после слияния осталось больше этой доли замечаний — второй проход слияния
MERGE_SECOND_MIN = 25      # ...и только для длинных списков
MERGE_BIG_GROUP = 6        # группа дублей больше этого — подозрение на склейку разных дефектов
SEV_ORDER = {"критичный": 0, "существенный": 1, "незначительный": 2}
READINESS_SCALE = 150  # штраф 150 ≈ индекс 37: документ с десятком серьёзных дефектов
SEV_WEIGHT = {"критичный": 10, "существенный": 4, "незначительный": 1}


@dataclass
class Dismissed:
    finding: Finding
    reason: str
    original: str = ""
    owner: object = None


@dataclass
class Result:
    doc: Document
    model: str
    segmentation: Segmentation
    checks: List[RequirementCheck]
    set_check: SetCheckFull
    verdict: Verdict
    rule_findings: List[Finding] = field(default_factory=list)
    answers: Dict[str, str] = field(default_factory=dict)
    rejected: List[Finding] = field(default_factory=list)      # цитата не найдена в документе
    dismissed: List[Dismissed] = field(default_factory=list)   # отклонено критиком
    locator: Optional[Locator] = None
    stats: Dict[str, int] = field(default_factory=dict)

    def all_findings(self) -> List[Tuple[str, Finding]]:
        """Все подтверждённые замечания с идентификаторами F-1, F-2, ..."""
        items = [(c.report_id, f) for c in self.checks for f in c.findings]
        items += [(",".join(f.related_ids) or "набор", f) for f in self.set_check.findings]
        items += [("правило", f) for f in self.rule_findings]
        return sorted(items, key=lambda x: SEV_ORDER.get(x[1].severity.value, 3))

    def readiness(self) -> int:
        """Индекс готовности 0–100 по уникальным дефектам: 100·exp(−штраф/150), штраф = 10·критичные + 4·существенные + 1·незначительные.
        Собственная эвристика команды, не из стандарта; с насыщением, чтобы не обнулялся."""
        penalty = sum(SEV_WEIGHT.get(f.severity.value, 1) for _, f in self.all_findings())
        return round(100 * math.exp(-penalty / READINESS_SCALE))


def _segment_llm(llm, doc: Document, salt: str = "") -> Segmentation:
    prompt = (
        "Выдели из документа ВСЕ отдельные проверяемые требования: функциональные, нефункциональные, "
        "к данным, к интеграции, к UI, к процессу, параметры и правила API, логика метода, коды ответов, "
        "критерии приёмки (каждая строка таблицы критериев — отдельное требование). "
        "Шаблонные чек-листы без содержания и служебные поля паспорта не считай требованиями. "
        "Если у требования есть номер в документе (Ф-1 и т.п.) — укажи его в doc_id, иначе null. "
        "report_id присваивай по порядку: R-01, R-02, ...\n\nДОКУМЕНТ:\n" + doc.numbered()
    )
    return llm.ask(SYSTEM, prompt, Segmentation, salt=salt) if salt else llm.ask(SYSTEM, prompt, Segmentation)


def _renumber(seg: Segmentation, loc: Locator) -> Segmentation:
    """Оставить требования, чья цитата есть в документе, и пронумеровать заново R-01…"""
    keep = [r for r in seg.requirements if r.quote.strip() and loc.find(r.quote)]
    for i, r in enumerate(keep, 1):
        r.report_id = f"R-{i:02d}"
    seg.requirements = keep
    return seg


def segment(llm, doc: Document, log=print) -> Segmentation:
    """Нарезка с защитой: пустой или аномально короткий список никогда не идёт дальше.

    Порядок: модель → повтор мимо кэша → следующая модель цепочки → нарезка кодом (segment_rules).
    """
    from . import segment_rules
    loc = Locator(doc)
    need = segment_rules.expected_minimum(doc)
    best = None
    attempts = [("", None), ("retry-1", None), ("retry-2", "next")]
    for salt, switch in attempts:
        if switch == "next":
            chain, cur = getattr(llm, "chain", None) or [], getattr(llm, "current", 0)
            nxt = next((i for i in range(cur + 1, len(chain)) if chain[i] not in getattr(llm, "gone", ())), None)
            if nxt is None:
                break
            log(f"     нарезка: переключаюсь на {llm.chain[nxt]}")
            llm.current = nxt
        try:
            seg = _renumber(_segment_llm(llm, doc, salt), loc)
        except Exception as e:  # noqa: BLE001 — сбой модели не должен ронять прогон, ниже есть запасной путь
            log(f"     нарезка моделью не удалась: {e}")
            continue
        n = len(seg.requirements)
        if best is None or n > len(best.requirements):
            best = seg
        if n >= need:
            seg._source = "модель" + (f" (повтор {salt})" if salt else "")
            return _union(seg, segment_rules.segment(doc), loc, log)
        log(f"     нарезка моделью дала {n} требований при ожидаемом минимуме {need}: повторяю")
    rules = _renumber(segment_rules.segment(doc), loc)
    if best is not None and best.requirements:
        best._source = "модель (лучшая из попыток)"
        return _union(best, rules, loc, log)
    log(f"     нарезка кодом (запасной путь): {len(rules.requirements)} требований")
    rules._source = "код (запасной путь)"
    if not rules.requirements:
        raise RuntimeError("В документе не найдено ни одного требования: проверьте, что файл читается как текст.")
    return rules


_TOK = re.compile(r"[a-zа-яё0-9]+", re.I)


def _tokens(t: str) -> set:
    return {w.lower().replace("ё", "е") for w in _TOK.findall(t) if len(w) > 1 or w.isdigit()}


def _union(seg: Segmentation, code: Segmentation, loc: Locator, log=print) -> Segmentation:
    """Модель могла пропустить разделы (URL, тестирование, UI, диаграммы): добавляем кандидатов из нарезки
    кодом, которых нет ни в одном требовании модели. Кандидат покрыт, если ≥60% его слов есть в одном
    требовании модели. Порядок требований — как в документе."""
    have = [_tokens(r.quote) for r in seg.requirements]
    added = []
    for r in code.requirements:
        tk = _tokens(r.quote)
        if not tk or not loc.find(r.quote):
            continue
        if any(len(tk & h) >= 0.6 * len(tk) for h in have):
            continue
        r.summary = r.summary + " (добавлено нарезкой кодом)"
        added.append(r)
        have.append(tk)
    if not added:
        return seg
    pos = lambda r: loc.find(r.quote) or (10 ** 6, 0)  # noqa: E731
    src = seg._source
    seg.requirements = sorted(seg.requirements + added, key=lambda r: (pos(r)[0] or 0, pos(r)[1]))
    for i, r in enumerate(seg.requirements, 1):
        r.report_id = f"R-{i:02d}"
    seg._source = f"{src} + код (добавлено {len(added)})"
    log(f"     нарезка кодом добавила пропущенных моделью требований: {len(added)}")
    return seg


def _req_list(seg: Segmentation, ids=None) -> str:
    return "\n".join(
        f"{r.report_id} ({r.doc_id or 'без номера'}, {r.section}, {r.kind}): {r.quote}"
        for r in seg.requirements
        if ids is None or r.report_id in ids
    )


def check_requirements(llm, doc: Document, seg: Segmentation, log=print, batch: int = BATCH) -> List[RequirementCheck]:
    out: List[RequirementCheck] = []
    reqs = seg.requirements
    for i in range(0, len(reqs), batch):
        part = reqs[i : i + batch]
        log(f"     {part[0].report_id}…{part[-1].report_id}")
        prompt = (
            "Проверь КАЖДОЕ требование из списка по свойствам отдельного требования:\n"
            + criteria.as_text(criteria.SINGLE)
            + "\n\nУчитывай весь документ как контекст: сверяй требование с критериями приёмки, "
            "моделью данных, API, диаграммами и другими разделами. Ищи граничные и ошибочные случаи, "
            "которые не описаны. Верни запись для каждого report_id.\n\n"
            "ПРОВЕРЯЕМЫЕ ТРЕБОВАНИЯ:\n" + _req_list(seg, {r.report_id for r in part})
            + "\n\nВЕСЬ ДОКУМЕНТ:\n" + doc.numbered()
        )
        res = llm.ask(SYSTEM, prompt, BatchCheck)
        got = {c.report_id: c for c in res.checks}
        miss = [r for r in part if r.report_id not in got]
        if len(miss) > max(2, len(part) // 4):  # большой пакет: модель пропустила часть требований — дозапрос один раз
            log(f"     модель пропустила {len(miss)} требований, дозапрос")
            res2 = llm.ask(SYSTEM, prompt.replace(_req_list(seg, {r.report_id for r in part}),
                                                  _req_list(seg, {r.report_id for r in miss})), BatchCheck)
            got.update({c.report_id: c for c in res2.checks if c.report_id not in got})
        for r in part:
            out.append(got.get(r.report_id) or RequirementCheck(report_id=r.report_id, verdict="не проверено", findings=[]))
    return out


def check_set(llm, doc: Document, seg: Segmentation) -> SetCheckFull:
    prompt = (
        "Проверь набор требований ЦЕЛИКОМ по свойствам набора:\n" + criteria.as_text(criteria.SET)
        + "\n\nИщи прежде всего: противоречия между требованиями и между частями документа "
        "(основной и дочерней спецификацией), противоречия требований и критериев приёмки, "
        "несогласованные единицы и типы данных, незаполненные разделы, отсутствие обработки ошибок, "
        "информационной безопасности, отката, версионности. В related_ids укажи report_id затронутых требований. "
        "Отдельно построй трассировку coverage: для каждого функционального и нефункционального требования "
        "перечисли report_id критериев приёмки, которые его проверяют.\n\n"
        "СПИСОК ТРЕБОВАНИЙ:\n" + _req_list(seg) + "\n\nВЕСЬ ДОКУМЕНТ:\n" + doc.numbered()
    )
    return llm.ask(SYSTEM, prompt, SetCheckFull)


def audit_lens(llm, doc: Document, seg: Segmentation, name: str, checklist) -> LensCheck:
    """Один проход или группа проходов (тогда checklist — dict {проход: [пункты]})."""
    if isinstance(checklist, dict):
        items = "\n".join(f"\n[{n}]\n" + "\n".join(f"- {c}" for c in cl) for n, cl in checklist.items())
        name = "; ".join(checklist)
    else:
        items = "\n".join(f"- {c}" for c in checklist)
    prompt = (
        f"Целевой проход «{name}». Проверь документ ТОЛЬКО по этому чеклисту, пункт за пунктом, "
        "в criterion укажи только название прохода из квадратных скобок (если скобок нет — нарушенное свойство), "
        "не копируй в criterion текст пункта чеклиста, "
        "и сообщи о каждом найденном дефекте отдельным замечанием. Если пункт выполнен — замечание не нужно. "
        "Проверяй логически: сопоставляй разные места документа, пересчитывай примеры. "
        "В related_ids укажи report_id затронутых требований.\n\nЧЕКЛИСТ:\n" + items
        + "\n\nСПИСОК ТРЕБОВАНИЙ:\n" + _req_list(seg) + "\n\nВЕСЬ ДОКУМЕНТ:\n" + doc.numbered()
    )
    return llm.ask(SYSTEM, prompt, LensCheck)


def _functional(seg: Segmentation):
    """Функциональные требования раздела бизнес-логики: с номером Ф-… или типом «функциональное»."""
    return [r for r in seg.requirements
            if (r.doc_id or "").upper().startswith(("Ф", "F", "FR")) or "функц" in (r.kind or "").lower()
            and "нефункц" not in (r.kind or "").lower()]


def simulate(llm, doc: Document, seg: Segmentation, reqs) -> ScenarioCheck:
    """Прогон сценариев: модель пошагово исполняет требования на критериях приёмки и граничных случаях.
    Дефект — сценарий, где документ не даёт однозначного результата или два места дают разный."""
    items = "\n".join(f"- {c}" for c in criteria.SCENARIOS)
    prompt = (
        "Ты тестировщик-аналитик. Для КАЖДОГО требования ниже прогони сценарии из списка (те, что к нему применимы), "
        "как будто исполняешь требования вручную: в trace распиши шаги и какое правило документа применяется на "
        "каждом шаге, с числами. outcome_defined=false, если на каком-то шаге документ молчит, допускает два "
        "толкования или разные места документа дают разный результат; тогда заполни finding с дословной цитатой "
        "требования, problem (какой шаг не определён и почему), fix и вопросом заказчику. Если результат определён "
        "однозначно — finding не заполняй. Не придумывай правил, которых нет в документе.\n\nСЦЕНАРИИ:\n" + items
        + "\n\nТРЕБОВАНИЯ ДЛЯ ПРОГОНА:\n" + _req_list(seg, {r.report_id for r in reqs})
        + "\n\nВЕСЬ ДОКУМЕНТ:\n" + doc.numbered()
    )
    return llm.ask(SYSTEM, prompt, ScenarioCheck)


def cluster(llm, doc: Document, items: List[Finding]) -> Clustering:
    listing = "\n".join(f"{i}. [{f.severity.value}] [{f.criterion}] «{f.quote}» — {f.problem}"
                        for i, f in enumerate(items))
    prompt = (
        "Ниже замечания нескольких ревьюеров к одному документу. Многие описывают ОДИН И ТОТ ЖЕ дефект разными "
        "словами или с цитатами из разных мест (например, одно и то же несоответствие типа поля в запросе и в "
        "ответе, или один неверный HTTP-метод, замеченный в описании и в примере). Сгруппируй замечания по сути "
        "дефекта: одна группа = одна проблема, которую исправят одной правкой документа. Разные проблемы в одном "
        "месте — разные группы. Объединяй ТОЛЬКО если одна правка документа устраняет все замечания группы; "
        "если сомневаешься — не объединяй: потерять отдельный дефект хуже, чем оставить дубль. "
        "Не объединяй замечания только потому, что они про один раздел или один метод API: неверный метод, "
        "несоответствие описания, заголовки и тело запроса, разные коды ответа, разные поля — это разные дефекты. "
        "Каждый номер должен попасть ровно в одну группу (одиночные замечания — группа из "
        "одного). Для группы укажи номер лучшей формулировки и общую критичность.\n\nЗАМЕЧАНИЯ:\n" + listing
    )
    return llm.ask(SYSTEM, prompt, Clustering)


def _merge(llm, doc, owners, log) -> int:
    """owners: [(контейнер с .findings, Finding)]. Сливает дубли на месте, возвращает число групп."""
    items = [f for _, f in owners]
    if len(items) < 2:
        return len(items)
    cl = cluster(llm, doc, items)
    seen, groups = set(), []
    for c in cl.clusters:
        mem = [i for i in dict.fromkeys(c.members) if 0 <= i < len(items) and i not in seen]
        if not mem:
            continue
        seen.update(mem)
        prim = c.primary if c.primary in mem else mem[0]
        if len(mem) > MERGE_BIG_GROUP:
            # слишком большая группа — модель склеила разные дефекты одного раздела. Внутри неё оставляем
            # слитыми только замечания к одному и тому же месту документа (одинаковая цитата)
            by_quote: Dict[str, List[int]] = {}
            for i in mem:
                by_quote.setdefault(" ".join(items[i].quote.lower().split())[:60], []).append(i)
            for sub in by_quote.values():
                groups.append((prim if prim in sub else sub[0], sub, c.severity))
            log(f"     группа из {len(mem)} замечаний разбита по местам документа на {len(by_quote)}")
            continue
        groups.append((prim, mem, c.severity))
    groups += [(i, [i], items[i].severity) for i in range(len(items)) if i not in seen]
    for owner, f in owners:
        owner.findings.remove(f)
    for prim, mem, sev in groups:
        owner, f = owners[prim]
        m = MergedFinding(**f.model_dump())
        m.severity = sev
        # повторное слияние уже слитых замечаний: счётчики и цитаты складываются
        m.merged_count = sum(getattr(items[i], "merged_count", 1) or 1 for i in mem)
        quotes = []
        for i in mem:
            quotes += [items[i].quote] + list(getattr(items[i], "also_quotes", []) or [])
        m.also_quotes = list(dict.fromkeys(q for q in quotes if q and q != f.quote))
        m.related_ids = sorted({r for i in mem for r in items[i].related_ids})
        # дефект подтверждён правилом: помечаем, чтобы критик его не снимал
        m.rule = next((getattr(items[i], "rule", "") for i in mem if getattr(items[i], "rule", "")), "")
        # дефект независимо нашли две модели ансамбля — тоже не отдаём критику
        srcs = set()
        for i in mem:
            srcs |= _SOURCES.get(id(items[i]), {_SOURCE.get(id(items[i]), "main")})
        _SOURCES[id(m)] = srcs
        if len(srcs) > 1 or any(id(items[i]) in CONFIRMED for i in mem):
            CONFIRMED.add(id(m))
        owner.findings.append(m)
    return len(groups)


def critic(llm, doc: Document, items: List[Finding]) -> CriticReview:
    listing = "\n".join(
        f"{i}. [{f.severity.value}] {f.criterion}: «{f.quote}» — {f.problem}" for i, f in enumerate(items)
    )
    prompt = (
        "Ты — независимый второй ревьюер. Ниже замечания первого ревьюера к документу. Для каждого реши: "
        "keep=false, если: (а) замечание ложное — документ это прямо регулирует; (б) дублирует замечание с меньшим "
        "номером; (в) это вкусовщина, стиль, оформление или пожелание «хорошо бы», из-за которого разработчик и "
        "тестировщик не сделают ничего по-разному; (г) замечание требует от ТЗ того, что по стандарту в ТЗ не "
        "входит (детали реализации, выбор технологий). Будь строгим: в отчёте должны остаться только дефекты, "
        "которые аналитик реально исправит. Реальный пробел, противоречие или риск не отклоняй, даже если он мелкий. "
        "Откалибруй критичность: «критичный» — противоречие или невозможность реализовать/проверить; "
        "«существенный» — неполнота или двусмысленность, ведущая к разной реализации; "
        "«незначительный» — оформление. Верни решение для каждого номера.\n\n"
        "ЗАМЕЧАНИЯ:\n" + listing + "\n\nДОКУМЕНТ:\n" + doc.numbered()
    )
    return llm.ask(SYSTEM, prompt, CriticReview)


def recheck(llm, doc: Document, items: List[Tuple[Finding, str]]) -> Recheck:
    listing = "\n".join(f"{i}. [{f.severity.value}] {f.criterion}: «{f.quote}» — {f.problem}\n   причина отклонения: {r}"
                        for i, (f, r) in enumerate(items))
    prompt = (
        "Это важные замечания, которые другой ревьюер отклонил. Перепроверь каждое по тексту документа. "
        "Подтверди отклонение (dismiss=true) ТОЛЬКО если можешь привести дословную цитату документа, которая "
        "прямо снимает замечание, или замечание явно повторяет другое. Иначе верни замечание (dismiss=false).\n\n"
        "ОТКЛОНЁННЫЕ ЗАМЕЧАНИЯ:\n" + listing + "\n\nДОКУМЕНТ:\n" + doc.numbered()
    )
    return llm.ask(SYSTEM, prompt, Recheck)


def collect_questions(findings: List[Finding], limit: int = 12) -> List[str]:
    fs = sorted(findings, key=lambda f: SEV_ORDER.get(f.severity.value, 3))
    seen, qs = set(), []
    for f in fs:
        q = f.question.strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            qs.append(q)
    return qs[:limit]


def final_verdict(llm, res: "Result") -> Verdict:
    crit = [f"[{f.severity.value}] {f.criterion}: {f.problem}" for _, f in res.all_findings()]
    ans = "\n".join(f"В: {q}\nО: {a}" for q, a in res.answers.items() if a) or "нет"
    prompt = (
        f"Требований: {len(res.segmentation.requirements)}. Замечания:\n" + "\n".join(crit)
        + "\n\nОтветы заказчика на уточняющие вопросы:\n" + ans
        + "\n\nРеши: можно ли по документу начинать разработку и тестирование. Дай обоснование со ссылками "
        "на конкретные требования и упорядоченный список того, что исправить в первую очередь. "
        "Если ответы заказчика снимают замечание — учти это."
    )
    return llm.ask(SYSTEM, prompt, Verdict)


# Модель иногда копирует в цитату служебный префикс из списка требований (см. _req_list):
# «R-51 (без номера, Раздел, тип): текст». Префикса нет в документе, поэтому верная цитата отбрасывалась.
_REQ_PREFIX = re.compile(r"^\s*R-\d+(?:-\d+)?\s*\([^()\n]*\)\s*:\s*", re.M)


def clean_quote(q: str) -> str:
    return _REQ_PREFIX.sub("", q or "").strip()


def _verify(loc: Locator, findings: List[Finding], rejected: List[Finding]) -> List[Finding]:
    ok = []
    for f in findings:
        f.quote = clean_quote(f.quote)
        (ok if loc.find(f.quote) else rejected).append(f)
    return ok


def _analyze(llm, doc: Document, seg: Segmentation, full: bool, use_lenses: bool, log=print):
    """Шаги 3–4 одной моделью: проверка каждого требования, набора, проходы-чеклисты, сценарии."""
    log("3/8 Проверка каждого требования")
    batch = BATCH if full else max(BATCH, math.ceil(len(seg.requirements) / ECONOMY_BATCHES))
    checks = check_requirements(llm, doc, seg, log, batch)

    log("4/8 Проверка набора, трассировка и целевые проходы-чеклисты")
    sc = check_set(llm, doc, seg)
    if use_lenses:
        groups = [[n] for n in criteria.LENSES] if full else criteria.LENS_GROUPS
        for g in groups:
            name = g[0] if len(g) == 1 else "; ".join(g)
            lc = audit_lens(llm, doc, seg, name, criteria.LENSES[g[0]] if len(g) == 1
                            else {n: criteria.LENSES[n] for n in g})
            for f in lc.findings:
                f.criterion = criteria.normalize_criterion(f.criterion, g[0])
            sc.findings.extend(lc.findings)
            log(f"     проход «{name}»: {len(lc.findings)}")

    fr = _functional(seg)
    if use_lenses and fr:
        parts = [[r] for r in fr] if full else [fr]
        n_sc = 0
        for part in parts:
            scn = simulate(llm, doc, seg, part)
            for x in scn.scenarios:
                if x.finding and not x.outcome_defined:
                    crit = criteria.normalize_criterion(x.finding.criterion, "Однозначность")
                    x.finding.criterion = f"{crit} · прогон сценария"
                    x.finding.problem = f"Сценарий: {x.scenario.strip().rstrip('.')}. {x.finding.problem}"
                    x.finding.related_ids = sorted(set(x.finding.related_ids) | {x.report_id})
                    sc.findings.append(x.finding)
                    n_sc += 1
        log(f"     прогон сценариев по {len(fr)} функциональным требованиям: дефектов {n_sc}")
    return checks, sc


# id замечания -> модель ансамбля, которая его нашла (замечания основной модели сюда не попадают).
# Нужна, чтобы дефект, найденный независимо двумя моделями, не снимал критик.
_SOURCE: Dict[int, str] = {}
CONFIRMED: set = set()  # id слитых замечаний, подтверждённых двумя и более моделями
_SOURCES: Dict[int, set] = {}  # id слитого замечания -> модели, нашедшие входящие в него замечания


def _absorb(checks, sc, extra_checks, extra_sc, name: str = "extra") -> int:
    """Замечания второй модели ансамбля добавляются к замечаниям основной: по требованию — в ту же запись,
    по набору — в общий список. Дубли потом сливает общий шаг 6. Возвращает число добавленных."""
    by_id = {c.report_id: c for c in checks}
    for f in [f for c in extra_checks for f in c.findings] + list(extra_sc.findings):
        _SOURCE[id(f)] = name
    n = 0
    for c in extra_checks:
        own = by_id.get(c.report_id)
        if own is None:
            checks.append(c)
            n += len(c.findings)
            continue
        own.findings.extend(c.findings)
        n += len(c.findings)
    sc.findings.extend(extra_sc.findings)
    return n + len(extra_sc.findings)


def run(llm, doc: Document, ask: Optional[Callable[[str], str]] = None, log=print, use_critic: bool = True,
        use_lenses: bool = True, full: Optional[bool] = None, ensemble: Optional[list] = None) -> Result:
    """ensemble — дополнительные модели (объекты LLM). Они проверяют те же нарезанные требования (шаги 3–4)
    параллельно с основной, их замечания объединяются, а слияние дублей, критик и итог делает основная модель.
    Без ensemble поведение прежнее.
    full=None (по умолчанию) — режим по модели: облегчённая (lite) модель с высоким лимитом получает
    полный режим, остальные — экономный. full=False — экономный режим: ≤3 пакета проверки требований и 3 группы проходов,
    весь прогон около 12–14 запросов. full=True — пакеты по 6 и каждый проход отдельно (точнее, но 20+ запросов)."""
    _SOURCE.clear(), CONFIRMED.clear(), _SOURCES.clear()  # служебные отметки прошлого прогона (веб-оболочка)
    loc = Locator(doc)
    log("1/8 Нарезка требований")
    seg = segment(llm, doc, log)
    log(f"     найдено требований: {len(seg.requirements)} (нарезал: {seg._source})")

    log("2/8 Детерминированные правила (без LLM)")
    rf = rules.run(doc)
    log(f"     сработало правил: {len(rf)}")

    if full is None:
        m = str(getattr(llm, "model", "")).lower()
        full = "lite" in m or m.startswith("cerebras:")  # у этих моделей бесплатный лимит выше
        log(f"     режим: {'полный (узкие проходы, у модели высокий лимит запросов)' if full else 'экономный'}")

    extras = list(ensemble or [])
    if not extras:
        checks, sc = _analyze(llm, doc, seg, full, use_lenses, log)
    else:
        import copy
        from concurrent.futures import ThreadPoolExecutor

        def tagged(name):
            return lambda msg: log(f"[{name}] {msg.strip()}")

        log(f"     ансамбль: основная {llm.model} + " + ", ".join(e.model for e in extras)
            + " (дополнительные — в экономном режиме, параллельно)")
        with ThreadPoolExecutor(max_workers=1 + len(extras)) as pool:
            main = pool.submit(_analyze, llm, doc, seg, full, use_lenses, tagged(llm.model))
            futs = [(e, pool.submit(_analyze, e, doc, copy.deepcopy(seg), False, use_lenses, tagged(e.model)))
                    for e in extras]
            checks, sc = main.result()
            for e, fu in futs:
                try:
                    ec, es = fu.result()
                except Exception as ex:  # noqa: BLE001 — сбой дополнительной модели не роняет прогон
                    log(f"     {e.model}: дополнительная модель не отработала ({ex}), продолжаю без неё")
                    continue
                n = _absorb(checks, sc, ec, es, e.model)
                log(f"     {e.model}: добавлено замечаний {n}")

    log("5/8 Верификация цитат")
    rejected: List[Finding] = []
    for c in checks:
        c.findings = _verify(loc, c.findings, rejected)
    sc.findings = _verify(loc, sc.findings, rejected)
    log(f"     не подтверждено цитатой и отброшено: {len(rejected)}")

    class _Rules:  # контейнер, чтобы правила участвовали в слиянии наравне с замечаниями модели
        findings = rf

    log("6/8 Слияние дублей и критик")
    owners = [(c, f) for c in checks for f in c.findings] + [(sc, f) for f in sc.findings] + \
             [(_Rules, f) for f in _Rules.findings]
    raw = len(owners)
    groups = _merge(llm, doc, owners, log)
    log(f"     замечаний {raw} → уникальных дефектов {groups}")
    if groups >= MERGE_SECOND_MIN and groups > MERGE_SECOND_SHARE * raw:
        # на длинном списке модель группирует не всё: второй проход по уже слитым замечаниям
        # ловит оставшиеся дубли (в ансамбле — одно и то же у двух моделей разными словами)
        owners = [(c, f) for c in checks for f in c.findings] + [(sc, f) for f in sc.findings] + \
                 [(_Rules, f) for f in _Rules.findings]
        before = groups
        groups = _merge(llm, doc, owners, log)
        log(f"     второй проход слияния: {before} → {groups}")

    dismissed: List[Dismissed] = []
    if use_critic:
        # находки, подтверждённые детерминированным правилом, критику не отдаём: их факт проверен кодом
        def judged(f):
            return not getattr(f, "rule", "") and id(f) not in CONFIRMED
        protected = sum(1 for c in checks for f in c.findings if id(f) in CONFIRMED) + \
            sum(1 for f in sc.findings if id(f) in CONFIRMED)
        if protected:
            log(f"     подтверждено двумя моделями (критик не снимает): {protected}")
        owners = [(c, f) for c in checks for f in c.findings if judged(f)] + \
                 [(sc, f) for f in sc.findings if judged(f)]
        if owners:
            review = critic(llm, doc, [f for _, f in owners])
            dec = {d.index: d for d in review.decisions}
            for i, (owner, f) in enumerate(owners):
                d = dec.get(i)
                if not d:
                    continue
                if not d.keep:
                    owner.findings.remove(f)
                    dismissed.append(Dismissed(f, d.reason, f.severity.value, owner))
                else:
                    f.severity = d.severity
        # важные отклонения перепроверяем: без цитаты-доказательства замечание возвращается
        # важные отклонения перепроверяются всегда; если критик слишком строг — все.
        # Отклонение остаётся, только если найдена цитата-основание (или это явный дубль).
        important = [d for d in dismissed if d.original in ("критичный", "существенный")]
        if owners and len(dismissed) > CRITIC_MAX_SHARE * len(owners):
            log(f"     критик отклонил {len(dismissed)} из {len(owners)} (> {CRITIC_MAX_SHARE:.0%}): слишком строг, "
                "перепроверяю все")
            important = list(dismissed)
        if important:
            rc = recheck(llm, doc, [(d.finding, d.reason) for d in important])
            decs = {x.index: x for x in rc.decisions}
            restored = 0
            for i, d in enumerate(important):
                x = decs.get(i)
                ev = x.evidence_quote.strip() if x else ""
                dup = x and ("дубл" in x.reason.lower() or "повтор" in x.reason.lower())
                ev_at = loc.find(ev) if ev else None
                # цитата-«основание», указывающая на то же место, что и само замечание, ничего не доказывает:
                # критик просто повторил спорный текст
                own = {loc.find(q) for q in [d.finding.quote, *getattr(d.finding, "also_quotes", [])] if q}
                self_ref = bool(ev_at) and ev_at in own and not dup
                proven = x and x.dismiss and not self_ref and (ev == "" and dup or bool(ev_at))
                if not proven:
                    d.finding.severity = Severity(d.original)
                    d.owner.findings.append(d.finding)
                    dismissed.remove(d)
                    restored += 1
                elif x.evidence_quote.strip():
                    d.reason += f" Основание: «{x.evidence_quote.strip()}» ({loc.where(x.evidence_quote)})."
            log(f"     возвращено после перепроверки: {restored}")
        log(f"     отклонено критиком окончательно: {len(dismissed)}")
    rf = _Rules.findings

    res = Result(doc, llm.model, seg, checks, sc, Verdict(ready_for_development=False, ready_for_testing=False,
                 rationale="", top_fixes=[]), rf, {}, rejected, dismissed, loc)

    log("7/8 Уточняющие вопросы")
    res.answers = {q: "" for q in collect_questions([f for _, f in res.all_findings()])}
    if ask:
        for q in res.answers:
            res.answers[q] = ask(q)

    log("8/8 Итоговое заключение")
    res.verdict = final_verdict(llm, res)
    allm = [llm] + extras
    res.stats = {"llm_calls": sum(getattr(m, "calls", 0) for m in allm),
                 "cache_hits": sum(getattr(m, "cache_hits", 0) for m in allm)}
    res.model = llm.label() if hasattr(llm, "label") else llm.model
    if extras:
        res.model += " + ансамбль: " + ", ".join(e.label() if hasattr(e, "label") else e.model for e in extras)
    return res
