"""Чтение документа: PDF, DOCX, MD/TXT. Возвращает текст с нумерацией строк."""
from dataclasses import dataclass
from pathlib import Path
from typing import List


@dataclass
class Line:
    no: int
    page: int
    text: str


@dataclass
class Document:
    path: Path
    lines: List[Line]

    @property
    def text(self) -> str:
        return "\n".join(l.text for l in self.lines)

    def numbered(self) -> str:
        """Текст для модели: каждая строка с меткой [стр.P:L]."""
        return "\n".join(f"[{l.page}:{l.no}] {l.text}" for l in self.lines if l.text.strip())


def _from_pdf(path: Path) -> List[Line]:
    import pdfplumber

    out, n = [], 0
    with pdfplumber.open(path) as pdf:
        for p, page in enumerate(pdf.pages, start=1):
            for t in (page.extract_text() or "").splitlines():
                n += 1
                out.append(Line(n, p, t))
    return out


def _from_docx(path: Path) -> List[Line]:
    import docx

    d = docx.Document(str(path))
    texts = [p.text for p in d.paragraphs]
    for table in d.tables:
        for row in table.rows:
            texts.append(" | ".join(c.text for c in row.cells))
    return [Line(i, 1, t) for i, t in enumerate(texts, start=1)]


def load(path: str) -> Document:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Файл не найден: {p}")
    ext = p.suffix.lower()
    if ext == ".pdf":
        lines = _from_pdf(p)
    elif ext == ".docx":
        lines = _from_docx(p)
    elif ext in (".md", ".txt", ".markdown"):
        lines = [Line(i, 1, t) for i, t in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1)]
    else:
        raise ValueError(f"Формат {ext} не поддерживается: используйте PDF, DOCX, MD или TXT")
    if not any(l.text.strip() for l in lines):
        raise ValueError("Документ пустой или текст не извлекается (скан без текстового слоя?)")
    return Document(p, lines)
