"""Детерминированная проверка: цитаты модели должны реально быть в документе.

Это защита от галлюцинаций: замечание без найденной цитаты в отчёт как
подтверждённое не попадает.
"""
import re
from difflib import SequenceMatcher
from typing import Optional, Tuple

from .loader import Document

_STRIP = re.compile(r"[*`_|#>]+")
_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    s = _STRIP.sub(" ", s).replace("ё", "е").replace("Ё", "Е")
    s = s.replace("«", '"').replace("»", '"').replace("“", '"').replace("”", '"')
    s = s.replace("—", "-").replace("–", "-").replace(" ", " ").replace(" ", " ")
    return _WS.sub(" ", s).strip().lower()


class Locator:
    def __init__(self, doc: Document):
        self.doc = doc
        parts, self._map = [], []  # map: позиция в норм. тексте -> индекс строки
        pos = 0
        for i, line in enumerate(doc.lines):
            n = _norm(line.text)
            if not n:
                continue
            parts.append(n)
            self._map.append((pos, i))
            pos += len(n) + 1
        self.text = " ".join(parts)

    def _line_at(self, pos: int) -> int:
        idx = 0
        for start, i in self._map:
            if start > pos:
                break
            idx = i
        return idx

    def find(self, quote: str) -> Optional[Tuple[int, int]]:
        """Вернёт (страница, строка) начала цитаты или None."""
        parts = [p for p in (quote or "").splitlines() if p.strip()]
        if len(parts) > 1:
            # цитата из нескольких строк документа (например, «404» и пример тела ответа): каждая часть
            # должна найтись дословно, и хотя бы одна — достаточно длинная, чтобы не принять выдумку
            hits = []
            for p in parts:
                n = _norm(p).strip(' ."')
                hits.append(self.find(p) if len(n) >= 4 else (n in self.text))
            if all(hits) and any(isinstance(h, tuple) for h in hits):
                return next(h for h in hits if isinstance(h, tuple))
        q = _norm(quote).strip(' ."')
        if len(q) < 4:
            return None
        pos = self.text.find(q)
        if pos < 0 and len(q) > 60:  # длинная цитата: достаточно совпадения начала и конца
            head, tail = q[:40], q[-30:]
            pos = self.text.find(head)
            if pos < 0 or self.text.find(tail, pos) < 0:
                pos = -1
        if pos < 0:
            return self._fuzzy(q)
        line = self.doc.lines[self._line_at(pos)]
        return line.page, line.no

    def _fuzzy(self, q: str, threshold: float = 0.82):
        """Модель слегка исказила цитату (кавычки, пробелы, обрезка): ищем строку или пару строк,
        которая содержит почти такую же последовательность. Порог высокий, чтобы не пропустить выдумку."""
        best, where = 0.0, None
        lines = [(i, _norm(l.text)) for i, l in enumerate(self.doc.lines) if l.text.strip()]
        for k, (i, t) in enumerate(lines):
            for cand in (t, t + " " + lines[k + 1][1] if k + 1 < len(lines) else t):
                if len(cand) < len(q) * 0.5:
                    continue
                sm = SequenceMatcher(None, q, cand, autojunk=False)
                m = sm.find_longest_match(0, len(q), 0, len(cand))
                # доля цитаты, покрытая совпадающими блоками
                cov = sum(b.size for b in sm.get_matching_blocks()) / len(q)
                if cov > best and m.size >= min(20, len(q) // 2):
                    best, where = cov, i
        if best >= threshold:
            line = self.doc.lines[where]
            return line.page, line.no
        return None

    def where(self, quote: str) -> str:
        loc = self.find(quote)
        if not loc:
            return "не найдено"
        page, no = loc
        return f"стр. {page}, строка {no}" if self.doc.path.suffix.lower() == ".pdf" else f"строка {no}"
