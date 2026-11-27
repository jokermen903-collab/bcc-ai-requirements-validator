"""Запасная нарезка требований кодом, без LLM.

Используется, только если модель вернула пустой или аномально короткий список.
Берёт пункты списков, нумерованные пункты, строки таблиц с содержанием и
абзацы под заголовками разделов с требованиями. Служебные поля паспорта,
шаблонные чек-листы и пустые строки отбрасываются.
"""
import re
from typing import List

from .loader import Document
from .models import Requirement, Segmentation

_ID = re.compile(r"^\W*([А-ЯЁA-Z]{1,5}[-–.]?\d{1,3}(?:\.\d+)*)\W")
_BULLET = re.compile(r"^\s*(?:[-*•–]|\d{1,2}[.)])\s+")
_HEADING_MD = re.compile(r"^\s*#{1,6}\s+(.*)")
_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}")
_TEMPLATE = re.compile(r"Да\s*/\s*Нет|ФИО ответственного|^\s*\|\s*(\|\s*)+$|требуется\s+(не\s+)?требуется", re.I)
# разделы, где требования не ищем (паспорт документа, ревью, согласования)
_SKIP_SECTION = re.compile(r"общие сведения|ревью требований|согласующ|паспорт", re.I)
# служебные поля паспорта метода: не требования
_META = re.compile(r"^\W*(gitlab project|framework|author|developer|controller|curl|url type)\b", re.I)
_REQ_SECTION = re.compile(r"требован|критери|логика|правила|параметр|ответ|статус|архитектур|дизайн|"
                          r"диаграмм|цели|bpmn|sequence|интеграц|данн|^\s*\d{3}\b", re.I)
_MIN_LEN = 18
# в PDF строки таблицы-чек-листа похожи на заголовки; из пропуска выходим только на явных разделах шаблона
_EXIT_SKIP = re.compile(r"^(часть|блок|раздел|\d+(\.\d+)*\s|логика|особые|схема|параметр|ответ|статус|"
                        r"request body|response body|запрос|path|query|(не)?функциональные требования)", re.I)


def _clean(t: str) -> str:
    t = re.sub(r"[*`_]+", "", t)
    t = re.sub(r"\s*\|\s*", " | ", t).strip(" |")
    return re.sub(r"\s+", " ", t).strip()


def _looks_heading(t: str) -> bool:
    """Заголовок в тексте из PDF/DOCX: короткая строка без точки в конце."""
    s = t.strip()
    return 2 <= len(s) <= 60 and not s.endswith((".", ";", ":", ",")) and not _BULLET.match(s) \
        and s[:1].isupper() and len(s.split()) <= 7 and not re.search(r"\d{2,}", s)


def segment(doc: Document) -> Segmentation:
    reqs: List[Requirement] = []
    section, skip, table_rows, after_heading = "Документ", False, 0, False
    title = next((l.text.strip("# ").strip() for l in doc.lines if l.text.strip()), doc.path.stem)
    for line in doc.lines:
        raw = line.text
        t = raw.strip()
        if not t:
            table_rows = 0
            continue
        m = _HEADING_MD.match(raw)
        plain = doc.path.suffix.lower() not in (".md", ".markdown")
        if not m and plain and after_heading and _looks_heading(t):
            after_heading = False
            continue  # шапка таблицы сразу под заголовком (PDF): не новый раздел
        # в PDF/DOCX заголовком считаем только похожее на раздел шаблона ТЗ, иначе это строка таблицы
        if m or (plain and _looks_heading(t) and (_REQ_SECTION.search(t) or _SKIP_SECTION.search(t)
                                                  or _EXIT_SKIP.match(t))):
            head = (m.group(1) if m else t).strip()
            if skip and plain and not _EXIT_SKIP.match(head):
                continue  # в PDF строки таблицы-чек-листа похожи на заголовки: не выходим из пропуска
            after_heading = True
            section = head
            skip = bool(_SKIP_SECTION.search(section))
            table_rows = 0
            continue
        after_heading = False
        if skip or _TEMPLATE.search(t) or _META.match(re.sub(r"[*`|]+", " ", t)):
            continue
        is_table = t.startswith("|")
        if is_table:
            if _SEPARATOR.match(t):
                continue
            table_rows += 1
            if table_rows == 1:  # строка заголовков таблицы
                continue
            cells = [c for c in (x.strip() for x in t.strip("|").split("|")) if c and c not in ("—", "-")]
            if len(cells) < 2:
                continue
        elif not (_BULLET.match(t) or _ID.match(t) or _REQ_SECTION.search(section)):
            continue
        text = _clean(t)
        # строка шапки таблицы из PDF: только латинские слова без цифр
        words = [w for w in re.split(r"[\s|]+", text) if w]
        if not is_table and not re.search(r"[А-Яа-яЁё\d:]", text) and len(words) >= 3 \
                and (len(words) >= 5 or all(w[:1].isupper() for w in words)):
            continue
        # перенос строки из PDF/DOCX: продолжение предыдущего требования того же раздела
        if plain and reqs and reqs[-1].section == section and re.match(r"[а-яё]", t) \
                and not reqs[-1].quote.rstrip().endswith((".", "!", "?", "}")):
            r = reqs[-1]
            r.quote = f"{r.quote} {t}"
            r.summary = " ".join(_clean(r.quote).split()[:15])
            continue
        # короткие строки контракта (REST type GET, Swagger —) тоже требования: порог ниже
        if len(text) < (8 if is_table or re.search(r"параметр", section, re.I) else _MIN_LEN):
            continue
        mid = _ID.match(re.sub(r"[*`_]+", "", t))
        kind = ("критерий приёмки" if "критери" in section.lower() else
                "API" if re.search(r"параметр|ответ|статус|логика|правила", section, re.I) else
                "функциональное" if mid else "прочее")
        reqs.append(Requirement(
            report_id=f"R-{len(reqs) + 1:02d}", doc_id=mid.group(1) if mid else None, section=section,
            kind=kind, quote=t, summary=" ".join(text.split()[:15]),
        ))
    return Segmentation(document_title=title, requirements=reqs)


def expected_minimum(doc: Document) -> int:
    """Нижняя граница разумного числа требований: доля от кандидатов, найденных кодом."""
    n = len(segment(doc).requirements)
    return max(3, int(n * 0.4))
