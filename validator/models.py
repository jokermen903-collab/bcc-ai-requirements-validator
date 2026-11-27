"""Схемы данных. Модель обязана отвечать строго по ним (structured output)."""
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, PrivateAttr, Field


class Severity(str, Enum):
    critical = "критичный"
    major = "существенный"
    minor = "незначительный"


class Requirement(BaseModel):
    report_id: str = Field(description="Идентификатор в отчёте: R-01, R-02, ...")
    doc_id: Optional[str] = Field(None, description="Идентификатор в документе (Ф-1 и т.п.) или null, если номера нет")
    section: str = Field(description="Раздел документа, где находится требование")
    kind: str = Field(description="функциональное / нефункциональное / данные / интеграция / API / критерий приёмки / прочее")
    quote: str = Field(description="Точная дословная цитата требования из документа")
    summary: str = Field(description="Краткая формулировка, до 15 слов")


class Segmentation(BaseModel):
    document_title: str
    requirements: List[Requirement]
    _source: str = PrivateAttr(default="модель")  # кто нарезал: модель или код; в схему для LLM не входит


class Finding(BaseModel):
    criterion: str = Field(description="Нарушенное свойство из списка критериев")
    severity: Severity
    quote: str = Field(description="Точная дословная цитата из документа, где дефект")
    problem: str = Field(description="В чём дефект, конкретно и со ссылкой на текст")
    fix: str = Field(description="Как переформулировать или что дописать")
    question: str = Field(description="Уточняющий вопрос аналитику/владельцу продукта")
    related_ids: List[str] = Field(default_factory=list, description="report_id связанных требований")


class RequirementCheck(BaseModel):
    report_id: str
    verdict: str = Field(description="соответствует / есть замечания / не соответствует")
    findings: List[Finding]


class BatchCheck(BaseModel):
    checks: List[RequirementCheck]


class SetCheck(BaseModel):
    findings: List[Finding]
    missing_topics: List[str] = Field(description="Чего не хватает в наборе (пропуски), каждое со ссылкой на раздел")


class Verdict(BaseModel):
    ready_for_development: bool
    ready_for_testing: bool
    rationale: str
    top_fixes: List[str] = Field(description="Что исправить в первую очередь, по порядку")


class Coverage(BaseModel):
    report_id: str = Field(description="report_id функционального/нефункционального требования")
    covered_by: List[str] = Field(description="report_id критериев приёмки, которые его проверяют; пусто, если не покрыто")


class SetCheckFull(SetCheck):
    coverage: List[Coverage] = Field(description="Трассировка: каждое функциональное и нефункциональное требование → критерии приёмки")


class CriticDecision(BaseModel):
    index: int = Field(description="Номер замечания из списка")
    keep: bool = Field(description="false — замечание ложное, дублирующее или не подтверждается текстом")
    severity: Severity
    reason: str = Field(description="Коротко: почему оставлено/отклонено/изменена критичность")


class CriticReview(BaseModel):
    decisions: List[CriticDecision]


class GoldMatch(BaseModel):
    gold_no: int = Field(description="Номер эталонного дефекта")
    found: bool
    finding_ids: List[str] = Field(description="Идентификаторы замечаний агента, которые его покрывают (F-1, ...)")


class EvalResult(BaseModel):
    matches: List[GoldMatch]


class LensCheck(BaseModel):
    findings: List[Finding]


class Scenario(BaseModel):
    report_id: str = Field(description="report_id требования, которое проверяет сценарий")
    scenario: str = Field(description="исходное состояние и действия")
    trace: str = Field(description="пошаговый расчёт по правилам документа: какое правило применяется на каждом шаге")
    outcome_defined: bool = Field(description="true, если документ однозначно задаёт результат и он не противоречит другим местам")
    finding: Optional[Finding] = Field(default=None, description="замечание, только если outcome_defined=false")


class ScenarioCheck(BaseModel):
    scenarios: List[Scenario]


class Cluster(BaseModel):
    members: List[int] = Field(description="Номера замечаний об ОДНОМ И ТОМ ЖЕ дефекте")
    primary: int = Field(description="Номер лучше всего сформулированного замечания группы")
    severity: Severity = Field(description="Критичность дефекта в целом")


class Clustering(BaseModel):
    clusters: List[Cluster]


class MergedFinding(Finding):
    """Дефект, собранный из нескольких замечаний; служебные поля в схему LLM не идут."""
    also_quotes: List[str] = []
    merged_count: int = 1
    rule: str = ""


class RecheckDecision(BaseModel):
    index: int
    dismiss: bool = Field(description="true — отклонение подтверждено, false — вернуть замечание в отчёт")
    evidence_quote: str = Field(description="Дословная цитата документа, которая снимает замечание; пусто, если такой нет")
    reason: str


class Recheck(BaseModel):
    decisions: List[RecheckDecision]
