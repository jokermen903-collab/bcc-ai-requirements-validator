"""Офлайн-тест пайплайна без ключа: модель заменена заглушкой.

Запуск: python -m unittest discover -s tests
"""
import json
import unittest
from pathlib import Path

from validator import evaluate, html_report, loader, pipeline, report, rules
from validator.models import (BatchCheck, Coverage, CriticDecision, CriticReview, EvalResult, Finding, GoldMatch, LensCheck, Clustering, Cluster, Recheck, RecheckDecision,
                              Requirement, RequirementCheck, Scenario, ScenarioCheck, Segmentation, SetCheckFull, Severity, Verdict)

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "requirements.md"


def F(quote, crit="Непротиворечивость", sev=Severity.critical, q="Вопрос?"):
    return Finding(criterion=crit, severity=sev, quote=quote, problem="проблема", fix="исправить", question=q)


class FakeLLM:
    model = "fake"
    reject_important = False
    calls = 0
    cache_hits = 0

    def ask(self, system, prompt, schema):
        self.calls += 1
        if schema is Segmentation:
            return Segmentation(document_title="Уведомление о расходах по карте", requirements=[
                Requirement(report_id="R-01", doc_id="Ф-3", section="ФТ", kind="функциональное",
                            quote="На карту приходится максимум один такой push за календарный месяц.", summary="1 push в месяц"),
                Requirement(report_id="R-02", doc_id=None, section="REST API", kind="API",
                            quote="| **REST type** | GET |", summary="Метод GET"),
                Requirement(report_id="R-03", doc_id=None, section="Критерии приёмки", kind="критерий приёмки",
                            quote="Расходы 48 500 ₸, порог 50 000 ₸", summary="Достижение порога"),
            ])
        if schema is BatchCheck:
            return BatchCheck(checks=[
                RequirementCheck(report_id="R-01", verdict="не соответствует", findings=[
                    F("следующее достижение вызывает push независимо от истории предыдущей настройки", q="Второй push допустим?"),
                    F("Возврат уменьшает расходы месяца исходной покупки.", "Полнота", Severity.minor, q="Что с push после возврата?")]),
                RequirementCheck(report_id="R-02", verdict="есть замечания", findings=[F("ЭТОЙ ЦИТАТЫ НЕТ В ДОКУМЕНТЕ")]),
            ])
        if schema is SetCheckFull:
            return SetCheckFull(findings=[F("Для чужой карты возвращается 404, как и для отсутствующей карты.", q="Какой код?")],
                                missing_topics=["Схема отката не описана"],
                                coverage=[Coverage(report_id="R-01", covered_by=[]), Coverage(report_id="R-02", covered_by=["R-03"])])
        if schema is ScenarioCheck:
            return ScenarioCheck(scenarios=[
                Scenario(report_id="R-05", scenario="порог 50 000, расходы 49 000, покупка 30 000", trace="Ф-3: push", outcome_defined=True),
                Scenario(report_id="R-07", scenario="push отправлен, порог повышен, расходы снова достигли порога",
                         trace="Ф-3 запрещает второй push, Ф-4 разрешает", outcome_defined=False,
                         finding=F("следующее достижение вызывает push независимо от истории предыдущей настройки",
                                   q="Сколько push за месяц?"))])
        if schema is LensCheck:
            if "Контракт API" in prompt:
                return LensCheck(findings=[F("Для чужой карты возвращается 404, как и для отсутствующей карты", q="Код 403 или 404?")])
            return LensCheck(findings=[])
        if schema is Recheck:
            body = prompt.split("ОТКЛОНЁННЫЕ ЗАМЕЧАНИЯ:\n")[1].split("\n\nДОКУМЕНТ")[0]
            reasons = [x for x in body.split("причина отклонения: ")[1:]]
            # для «регулируется Ф-2» есть настоящая цитата-основание, для остального — выдумка
            return Recheck(decisions=[RecheckDecision(
                index=i, dismiss=True, reason="регулируется",
                evidence_quote="Возврат уменьшает расходы месяца исходной покупки" if "Ф-2" in r else "ВЫДУМАННОЕ ОСНОВАНИЕ")
                for i, r in enumerate(reasons)])
        if schema is Clustering:
            lines = prompt.split("ЗАМЕЧАНИЯ:\n")[1].split("\n")
            dup = [i for i, l in enumerate(lines) if "404" in l]
            return Clustering(clusters=[Cluster(members=dup, primary=dup[0], severity=Severity.critical)] if dup else [])
        if schema is CriticReview:
            n = prompt.split("ЗАМЕЧАНИЯ:\n")[1].split("\n\nДОКУМЕНТ")[0].count("\n") + 1
            ds = [CriticDecision(index=i, keep=True, severity=Severity.critical, reason="ok") for i in range(n)]
            ds[1] = CriticDecision(index=1, keep=False, severity=Severity.minor, reason="регулируется Ф-2")
            if self.reject_important:
                ds[0] = CriticDecision(index=0, keep=False, severity=Severity.minor, reason="кажется лишним")
            return CriticReview(decisions=ds)
        if schema is Verdict:
            return Verdict(ready_for_development=False, ready_for_testing=False, rationale="Есть противоречия.",
                           top_fixes=["Разрешить конфликт Ф-3/Ф-4"])
        if schema is EvalResult:
            return EvalResult(matches=[GoldMatch(gold_no=9, found=True, finding_ids=["F-1"]),
                                       GoldMatch(gold_no=4, found=True, finding_ids=["F-2"])])
        raise AssertionError(schema)


class OfflineTest(unittest.TestCase):
    def setUp(self):
        self.res = pipeline.run(FakeLLM(), loader.load(str(DOC)), ask=lambda q: "да", log=lambda *_: None)

    def test_verifier_critic_merge(self):
        self.assertEqual(len(self.res.rejected), 1, "галлюцинация отброшена верификатором")
        self.assertEqual(len(self.res.dismissed), 1, "ложное замечание отклонено критиком")
        merged = [f for _, f in self.res.all_findings() if getattr(f, "merged_count", 1) == 2]
        self.assertEqual(len(merged), 1, "два замечания про 404 слиты в один дефект")
        self.assertEqual(len(merged[0].also_quotes), 1)
        self.assertTrue(0 < self.res.readiness() < 100)

    def test_recheck_restores_unproven(self):
        llm = FakeLLM()
        llm.reject_important = True
        res = pipeline.run(llm, loader.load(str(DOC)), log=lambda *_: None)
        # критичное замечание отклонено без цитаты-доказательства → возвращено в отчёт
        self.assertTrue(any(f.severity.value == "критичный" for _, f in res.all_findings()))
        self.assertTrue(all(d.original != "критичный" for d in res.dismissed))

    def test_fuzzy_quote(self):
        from validator.verify import Locator
        loc = Locator(loader.load(str(DOC)))
        self.assertIsNotNone(loc.find("Держатель активной карты KZT задает целый порог от 1000 до 1 000 000 тенге включительно"))
        self.assertIsNone(loc.find("Сервис обязан хранить номер карты целиком в открытом виде"))

    def test_rules(self):
        self.assertTrue(any("в течение минуты" in f.problem for f in self.res.rule_findings))
        self.assertTrue(any("Prod" in f.problem for f in rules.run(loader.load(str(DOC)))))

    def test_reports(self):
        md = report.build(self.res)
        for h in ["Паспорт прогона", "нарезал", "каждого требования", "всего набора", "Трассировка", "Сводка", "Итог",
                  "Индекс готовности", "Отклонённые"]:
            self.assertIn(h, md)
        self.assertNotIn("ЭТОЙ ЦИТАТЫ НЕТ В ДОКУМЕНТЕ»  ", md)
        data = report.to_json(self.res)
        html = html_report.build(data)
        self.assertIn("индекс готовности", html)
        self.assertIn("не покрыто", html)

    def test_eval(self):
        results = json.loads(report.to_json(self.res))
        gold = evaluate.parse_gold(str(ROOT / "gold" / "defects.md"))
        self.assertGreaterEqual(len(gold), 20)
        m, found, used = evaluate.evaluate(FakeLLM(), results, gold)
        self.assertEqual(sorted(found), [4, 9])
        self.assertIn("полнота", evaluate.render(results, gold, m, found, used))


class _Seg:
    """Заглушка только для нарезки: возвращает заданное число требований."""
    def __init__(self, reqs):
        self.reqs, self.calls, self.salts = reqs, 0, []
        self.chain, self.current, self.gone = ["a", "b", "c"], 0, set()

    def ask(self, system, prompt, schema, salt=""):
        self.calls += 1
        self.salts.append(salt)
        return Segmentation(document_title="ТЗ", requirements=list(self.reqs))


class SegmentationGuardTest(unittest.TestCase):
    """Регрессия 0.5.0: модель вернула 0 требований, и прогон пошёл дальше с пустым списком."""

    def test_empty_llm_falls_back_to_rules(self):
        llm = _Seg([])
        seg = pipeline.segment(llm, loader.load(str(DOC)), log=lambda *_: None)
        self.assertGreaterEqual(len(seg.requirements), 30)
        self.assertIn("код", seg._source)
        self.assertEqual(llm.calls, 3)                     # исходный запрос, повтор мимо кэша, другая модель
        self.assertEqual(llm.salts[1:], ["retry-1", "retry-2"])
        self.assertEqual(llm.current, 1)                   # переключились на следующую модель цепочки

    def test_good_llm_result_is_kept(self):
        from validator import segment_rules
        doc = loader.load(str(DOC))
        llm = _Seg(segment_rules.segment(doc).requirements)
        seg = pipeline.segment(llm, doc, log=lambda *_: None)
        self.assertEqual(llm.calls, 1)
        self.assertEqual(seg._source, "модель")

    def test_rules_on_real_docs(self):
        from validator import segment_rules
        from validator.verify import Locator
        for name in ("requirements.md", "requirements.pdf"):
            doc = loader.load(str(ROOT / "docs" / name))
            reqs = segment_rules.segment(doc).requirements
            loc = Locator(doc)
            ok = [r for r in reqs if loc.find(r.quote)]
            self.assertGreaterEqual(len(ok), 30, name)
            self.assertGreaterEqual(len(ok), len(reqs) - 2, name)

    def test_salt_changes_cache_key(self):
        from validator.llm import LLM
        k = LLM.__new__(LLM)
        k.chain, k.temperature = ["m"], 0.0
        self.assertNotEqual(k._key("s", "p", Segmentation), k._key("s", "p", Segmentation, "retry-1"))
        self.assertEqual(k._key("s", "p", Segmentation), k._key("s", "p", Segmentation, ""))


class _HarshCritic(FakeLLM):
    """Критик отклоняет все замечания без доказательств — предохранитель должен их вернуть."""
    def ask(self, system, prompt, schema):
        if schema is CriticReview:
            n = prompt.split("ЗАМЕЧАНИЯ:\n")[1].split("\n\nДОКУМЕНТ")[0].count("\n") + 1
            return CriticReview(decisions=[CriticDecision(index=i, keep=False, severity=Severity.minor,
                                                          reason="лишнее") for i in range(n)])
        return super().ask(system, prompt, schema)


class CriticGuardTest(unittest.TestCase):
    def test_harsh_critic_is_rechecked(self):
        logs = []
        res = pipeline.run(_HarshCritic(), loader.load(str(DOC)), log=logs.append)
        self.assertTrue(any("слишком строг" in str(l) for l in logs))
        self.assertEqual(len(res.dismissed), 0)          # доказательств нет — всё возвращено
        self.assertGreater(len(res.all_findings()), 0)


class UnionAndRulesTest(unittest.TestCase):
    def test_union_adds_sections_model_missed(self):
        from validator import segment_rules
        from validator.verify import Locator
        doc = loader.load(str(DOC))
        code = segment_rules.segment(doc).requirements
        only_f = [r.model_copy() for r in code if r.doc_id and r.doc_id.startswith("Ф")]
        llm = _Seg(only_f + [r.model_copy() for r in code[:20]])  # модель «забыла» тестирование, URL, статусы
        seg = pipeline.segment(llm, doc, log=lambda *_: None)
        quotes = " ".join(r.quote for r in seg.requirements)
        self.assertIn("заполняются тестировщиками", quotes)
        self.assertIn("Prod: —", quotes)
        self.assertIn("+ код", seg._source)
        self.assertEqual([r.report_id for r in seg.requirements][:2], ["R-01", "R-02"])

    def test_rule_merged_finding_is_protected(self):
        from validator.rules import RuleFinding

        class _Cl:
            def ask(self, system, prompt, schema):
                return Clustering(clusters=[Cluster(members=[0, 1], primary=0, severity=Severity.major)])

        class Box:
            findings = []
        a, b = Box(), Box()
        a.findings = [F("Prod: —")]
        b.findings = [RuleFinding(**F("Prod: —").model_dump(), rule="Prod")]
        pipeline._merge(_Cl(), loader.load(str(DOC)), [(a, a.findings[0]), (b, b.findings[0])], lambda *_: None)
        self.assertEqual(len(a.findings), 1)
        self.assertEqual(a.findings[0].rule, "Prod")
        self.assertEqual(report.source(a.findings[0]), "правило")


class DisplayTest(unittest.TestCase):
    def test_plain_quote_and_empty_question(self):
        from validator import html_report
        self.assertEqual(html_report.plain("| **REST type** | `GET` |"), "REST type | GET")
        data = json.loads(report.to_json(pipeline.run(FakeLLM(), loader.load(str(DOC)), log=lambda *_: None)))
        for f in data["findings"]:
            f["question"] = ""
        page = html_report.build(json.dumps(data, ensure_ascii=False))
        self.assertNotIn("Вопрос:", page)
        self.assertNotIn("**", page.split("<h2>Требования")[0].split("<h2>Замечания")[1])


class BudgetTest(unittest.TestCase):
    def test_economy_fits_daily_limit(self):
        """Экономный режим укладывается в ~15 запросов даже с повторами нарезки и дозапросами (лимит flash — 20 в сутки)."""
        eco, full = FakeLLM(), FakeLLM()
        pipeline.run(eco, loader.load(str(DOC)), log=lambda *_: None)
        pipeline.run(full, loader.load(str(DOC)), log=lambda *_: None, full=True)
        self.assertLessEqual(eco.calls, 16)
        self.assertLess(eco.calls * 2, full.calls)

    def test_lens_groups_cover_all_lenses(self):
        from validator import criteria
        self.assertEqual(sorted(n for g in criteria.LENS_GROUPS for n in g), sorted(criteria.LENSES))


class AdaptiveModeTest(unittest.TestCase):
    def test_lite_gets_full_mode_and_scenarios(self):
        lite, logs = FakeLLM(), []
        lite.model = "gemini-3.5-flash-lite"
        res = pipeline.run(lite, loader.load(str(DOC)), log=logs.append)
        self.assertTrue(any("режим: полный" in str(l) for l in logs))
        eco = FakeLLM()
        pipeline.run(eco, loader.load(str(DOC)), log=lambda *_: None)
        self.assertGreater(lite.calls, eco.calls + 8)
        self.assertTrue(any("прогон сценария" in f.criterion for _, f in res.all_findings()))


class EnsembleTest(unittest.TestCase):
    def test_second_model_adds_findings_and_failure_is_tolerated(self):
        single = pipeline.run(FakeLLM(), loader.load(str(DOC)), log=lambda *_: None)
        main, extra, logs = FakeLLM(), FakeLLM(), []
        extra.model = "extra"
        res = pipeline.run(main, loader.load(str(DOC)), log=logs.append, ensemble=[extra])
        self.assertGreater(extra.calls, 0, "вторая модель проверяла требования")
        self.assertTrue(any("extra: добавлено" in str(l) for l in logs))
        self.assertIn("ансамбль", res.model)
        self.assertGreaterEqual(len(res.all_findings()), len(single.all_findings()))

        class Broken(FakeLLM):
            model = "broken"

            def ask(self, system, prompt, schema):
                raise RuntimeError("недоступна")

        logs = []
        res2 = pipeline.run(FakeLLM(), loader.load(str(DOC)), log=logs.append, ensemble=[Broken()])
        self.assertTrue(any("не отработала" in str(l) for l in logs))
        self.assertGreater(len(res2.all_findings()), 0)


class CriterionTest(unittest.TestCase):
    def test_prompt_text_not_leaked_into_criterion(self):
        from validator.criteria import normalize_criterion as n
        self.assertEqual(n("ПЕРЕСЧИТАЙ каждый пример по правилам", "Дата, время и периоды"), "Дата, время и периоды")
        self.assertEqual(n("[Контракт API] есть ли Prod-адрес, спецификация (Swagger)", "x"), "Контракт API")
        self.assertEqual(n("Непротиворечивость", "x"), "Непротиворечивость")
        self.assertEqual(n("", "Однозначность"), "Однозначность")


class QuotePrefixTest(unittest.TestCase):
    def test_service_prefix_removed_from_quote(self):
        from validator.pipeline import clean_quote
        self.assertEqual(clean_quote('R-51 (без номера, 403. Unsuccessful — Forbidden, прочее): {"message":"x"}'),
                         '{"message":"x"}')
        self.assertEqual(clean_quote("R-07-2 (Ф-2, Бизнес-логика, функциональное): Месяц определяется"),
                         "Месяц определяется")
        self.assertEqual(clean_quote("Обычная цитата (без префикса): текст"), "Обычная цитата (без префикса): текст")


class TitleTest(unittest.TestCase):
    def test_markdown_heading_stripped_from_title(self):
        from validator.models import Segmentation
        self.assertEqual(Segmentation(document_title="# Уведомление о расходах", requirements=[]).document_title,
                         "Уведомление о расходах")


if __name__ == "__main__":
    unittest.main()


class LLMRobustnessTest(unittest.TestCase):
    def test_placeholder_key_is_ignored(self):
        import os
        from unittest import mock
        from validator import llm as llm_mod
        with mock.patch.dict(os.environ, {"CEREBRAS_API_KEY": "your_cerebras_api_key_here"}):
            self.assertIsNone(llm_mod.env_key("CEREBRAS_API_KEY"))
        with mock.patch.dict(os.environ, {"CEREBRAS_API_KEY": "csk-real"}):
            self.assertEqual(llm_mod.env_key("CEREBRAS_API_KEY"), "csk-real")

    def test_rejected_key_falls_through_instead_of_exit(self):
        from validator import llm as llm_mod
        m = llm_mod.LLM(models=["cerebras:a", "cerebras:b"], api_key=None, cache_dir=None, log=lambda *_: None)
        seen = []

        def gen(model, *a):
            seen.append(model)
            if model == "cerebras:a":
                raise llm_mod.HTTPProviderError(401, "bad key")
            return '{"document_title": "t", "requirements": []}'
        m._generate = gen
        out = m.ask("s", "p", Segmentation)  # не SystemExit: модель с плохим ключом выбывает
        self.assertEqual(out.document_title, "t")
        self.assertEqual(seen, ["cerebras:a", "cerebras:b"])
        self.assertIn("cerebras:a", m.gone)

    def test_agent_version_is_fixed_once(self):
        report.agent_version.cache_clear()
        v = report.agent_version()
        self.assertIs(report.agent_version(), v)
