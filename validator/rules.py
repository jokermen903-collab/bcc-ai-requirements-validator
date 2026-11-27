"""Детерминированные правила (без LLM).

Гибридная архитектура: дешёвые надёжные проверки кодом + смысловой анализ моделью.
Правила ловят типовые маркеры дефектов, которые модель может пропустить.
"""
import re
from typing import List

from .loader import Document
from .models import Finding, Severity


class RuleFinding(Finding):
    """Замечание, найденное правилом, а не моделью."""
    rule: str = ""


# (регэксп, свойство, критичность, проблема, исправление)
VAGUE = [
    (r"\bв течение (минуты|часа|дня|секунды)\b", "Проверяемость", Severity.major,
     "Срок без точки отсчёта, точки окончания и доли событий (процентиль)",
     "Указать: от какого события до какого, при каком объёме, для какой доли событий (например, p95)"),
    (r"\b(быстро|оперативно|своевременно|удобн\w*|интуитивн\w*|понятн\w*|надёжн\w*|надежн\w*|эффективн\w*)\b",
     "Однозначность", Severity.major, "Размытое качественное слово без измеримого критерия",
     "Заменить измеримым показателем"),
    (r"\b(и т\.\s?п\.|и т\.\s?д\.|и др\.|и прочее|при необходимости|по возможности|как правило|обычно|некоторы\w+)\b",
     "Однозначность", Severity.major, "Открытый перечень или условная формулировка",
     "Перечислить исчерпывающе или задать условие"),
    (r"\b(TBD|TODO|уточняется|будет определено|заполняются? \w+)\b", "Полнота", Severity.major,
     "Раздел или значение не заполнены", "Заполнить до передачи в разработку"),
    (r"\bДа / Нет\b", "Модифицируемость и трассируемость", Severity.minor,
     "Чек-лист ревью не заполнен: отметки не проставлены", "Провести ревью и проставить отметки"),
    (r"(Prod:\s*—)|(^\|\s*\*\*[^|]+\*\*\s*\|\s*—\s*\|)|(^\S[^|]*:\s*—\s*\.?$)", "Полнота", Severity.major,
     "Значение поля не указано (прочерк)", "Указать значение или явно написать «не применяется» с причиной"),
]

_ONCE = {"Модифицируемость и трассируемость"}  # такие правила срабатывают один раз на документ


def run(doc: Document) -> List[RuleFinding]:
    out: List[RuleFinding] = []
    fired = set()
    for line in doc.lines:
        t = line.text.strip()
        if not t:
            continue
        for rx, crit, sev, prob, fix in VAGUE:
            m = re.search(rx, t, flags=re.IGNORECASE)
            if not m:
                continue
            if crit in _ONCE and crit in fired:
                continue
            fired.add(crit)
            out.append(RuleFinding(
                criterion=crit, severity=sev, quote=t[:200], rule=rx,
                problem=f"{prob}: «{m.group(0)}»", fix=fix,
                question=f"Что именно означает «{m.group(0)}» в этом месте и как это проверить?",
            ))
    # пустые разделы: заголовок, за которым сразу другой заголовок того же или высшего уровня
    lines = [l for l in doc.lines if l.text.strip()]
    for a, b in zip(lines, lines[1:]):
        ha, hb = re.match(r"^(#+)\s", a.text), re.match(r"^(#+)\s", b.text)
        if ha and hb and len(hb.group(1)) <= len(ha.group(1)):
            out.append(RuleFinding(
                criterion="Полнота набора", severity=Severity.major, quote=a.text.strip(), rule="empty-section",
                problem="Раздел пуст", fix="Заполнить раздел или удалить", question=f"Что должно быть в разделе «{a.text.strip('# ')}»?",
            ))
    return out
