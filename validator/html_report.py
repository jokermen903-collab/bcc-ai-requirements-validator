"""Наглядный HTML-отчёт (один самодостаточный файл) из JSON-результата."""
import json
from html import escape as e

SEV_CLASS = {"критичный": "crit", "существенный": "major", "незначительный": "minor"}

CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1c1d1f;--mute:#62656b;--line:#e3e3df;--crit:#c62828;--major:#d97706;
--minor:#5b6b7a;--ok:#2e7d32;--accent:#0b5cad;--chip:#eef1f4}
@media (prefers-color-scheme:dark){:root{--bg:#141517;--card:#1d1f22;--ink:#e8e8e6;--mute:#a0a4aa;--line:#2f3236;
--crit:#ef5350;--major:#f0a63a;--minor:#9aa8b6;--ok:#66bb6a;--accent:#6aa8ec;--chip:#272a2e}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:24px 16px 64px}h1{font-size:26px;margin:0 0 4px}h2{font-size:19px;margin:36px 0 12px}
.sub{color:var(--mute);font-size:13px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-top:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.kpi b{display:block;font-size:28px;font-variant-numeric:tabular-nums}.kpi span{color:var(--mute);font-size:13px}
.verdict{margin-top:16px;border-left:4px solid var(--crit)}.verdict.ok{border-left-color:var(--ok)}
.bar{display:flex;height:10px;border-radius:5px;overflow:hidden;margin-top:10px;background:var(--chip)}
.bar i{display:block}.crit-bg{background:var(--crit)}.major-bg{background:var(--major)}.minor-bg{background:var(--minor)}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0 12px}.chip{border:1px solid var(--line);background:var(--chip);color:var(--ink);
border-radius:999px;padding:4px 12px;font-size:13px;cursor:pointer}.chip.on{background:var(--accent);border-color:var(--accent);color:#fff}
.f{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--minor);border-radius:8px;padding:12px 14px;margin-bottom:10px}
.f.crit{border-left-color:var(--crit)}.f.major{border-left-color:var(--major)}
.f h3{font-size:15px;margin:0 0 6px}.tag{display:inline-block;font-size:12px;border-radius:4px;padding:1px 6px;margin-right:6px;background:var(--chip);color:var(--mute)}
.tag.crit{color:#fff;background:var(--crit)}.tag.major{color:#fff;background:var(--major)}
blockquote{margin:6px 0;padding:6px 10px;border-left:3px solid var(--line);color:var(--mute);font-size:14px}
.f p{margin:4px 0}.tbl{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:14px;background:var(--card)}
th,td{border-bottom:1px solid var(--line);padding:7px 9px;text-align:left;vertical-align:top}th{color:var(--mute);font-weight:600}
.no{color:var(--crit);font-weight:600}ol li{margin-bottom:6px}.mute{color:var(--mute)}
"""

JS = """
document.querySelectorAll('.chip').forEach(c=>c.onclick=()=>{
 document.querySelectorAll('.chip').forEach(x=>x.classList.remove('on'));c.classList.add('on');
 const s=c.dataset.s;document.querySelectorAll('.f').forEach(f=>{f.style.display=(s==='all'||f.dataset.s===s||f.dataset.src===s)?'':'none'})});
"""


def build(data_json: str) -> str:
    d = json.loads(data_json)
    fs = d["findings"]
    cnt = {k: sum(1 for f in fs if f["severity"] == k) for k in SEV_CLASS}
    total = max(1, len(fs))
    cov = d.get("coverage") or []
    covered = sum(1 for c in cov if c["covered_by"])
    v = d["verdict"]
    ok = v["ready_for_development"]
    reqs = {r["report_id"]: r for r in d["requirements"]}
    per_req = {}
    for f in fs:
        per_req[f["owner"]] = per_req.get(f["owner"], 0) + 1

    h = []
    a = h.append
    a(f"<!doctype html><html lang=ru><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
      f"<title>Отчёт о проверке требований</title><style>{CSS}</style><body><div class=wrap>")
    a(f"<h1>{e(d['title'])}</h1><div class=sub>Файл {e(d['document'])} · модель {e(d['model'])} · агент {e(d['agent_version'])}"
      f" · ISO/IEC/IEEE 29148:2018, IEEE 830-1998</div>")
    a("<div class=grid>")
    for val, lab in [(f"{d['readiness']}/100", "индекс готовности"), (len(d["requirements"]), "требований"),
                     (len(fs), "уникальных дефектов"), (cnt["критичный"], "критичных"),
                     (f"{covered}/{len(cov)}" if cov else "—", "покрыто критериями приёмки"),
                     (len(d["questions"]), "уточняющих вопросов")]:
        a(f"<div class='card kpi'><b>{e(str(val))}</b><span>{lab}</span></div>")
    a("</div>")
    a("<div class=bar>" + "".join(
        f"<i class={SEV_CLASS[k]}-bg style='width:{100 * cnt[k] / total:.1f}%' title='{k}: {cnt[k]}'></i>" for k in SEV_CLASS) + "</div>")
    a(f"<div class='card verdict {'ok' if ok else ''}'><b>Разработку начинать: {'да' if ok else 'нет'}. "
      f"Тестирование: {'да' if v['ready_for_testing'] else 'нет'}.</b><p>{e(v['rationale'])}</p>"
      "<b>Исправить в первую очередь</b><ol>" + "".join(f"<li>{e(t)}</li>" for t in v["top_fixes"]) + "</ol></div>")

    a("<h2>Замечания</h2><div class=chips>")
    for s, lab in [("all", f"Все {len(fs)}"), ("crit", f"Критичные {cnt['критичный']}"), ("major", f"Существенные {cnt['существенный']}"),
                   ("minor", f"Незначительные {cnt['незначительный']}"), ("правило", "Найдено правилами")]:
        a(f"<button class='chip{' on' if s == 'all' else ''}' data-s='{s}'>{e(lab)}</button>")
    a("</div>")
    for f in fs:
        sc = SEV_CLASS.get(f["severity"], "minor")
        owner = f["owner"]
        r = reqs.get(owner)
        title = "" if owner == "правило" else f"{owner}" + (f" ({r['doc_id']})" if r and r.get("doc_id") else "")
        a(f"<div class='f {sc}' data-s='{sc}' data-src='{e(f['source'])}'><h3><span class='tag {sc}'>{e(f['severity'])}</span>"
          f"{e(f['id'])} · {e(f['criterion'])} <span class=mute>{' · ' + e(title) if title else ''} · {e(f['location'])} · {e(f['source'])}</span></h3>"
          f"<blockquote>{e(f['quote'])}</blockquote>"
          + "".join(f"<blockquote>{e(x['quote'])} <span class=mute>· {e(x['location'])}</span></blockquote>" for x in f.get("also", []))
          + f"<p>{e(f['problem'])}</p><p><b>Исправить:</b> {e(f['fix'])}</p>"
          f"<p class=mute><b>Вопрос:</b> {e(f['question'])}</p></div>")

    a("<h2>Требования</h2><div class=tbl><table><tr><th>ID</th><th>В документе</th><th>Тип</th><th>Кратко</th><th>Замечаний</th></tr>")
    for r in d["requirements"]:
        n = per_req.get(r["report_id"], 0)
        a(f"<tr><td>{e(r['report_id'])}</td><td>{e(r.get('doc_id') or '—')}</td><td>{e(r['kind'])}</td>"
          f"<td>{e(r['summary'])}</td><td>{n or ''}</td></tr>")
    a("</table></div>")

    if cov:
        a("<h2>Трассировка на критерии приёмки</h2><div class=tbl><table><tr><th>Требование</th><th>Кратко</th><th>Критерии приёмки</th></tr>")
        for c in cov:
            r = reqs.get(c["report_id"], {})
            cb = ", ".join(c["covered_by"]) if c["covered_by"] else "<span class=no>не покрыто</span>"
            a(f"<tr><td>{e(c['report_id'])}</td><td>{e(r.get('summary', ''))}</td><td>{cb if not c['covered_by'] else e(cb)}</td></tr>")
        a("</table></div>")

    if d.get("missing_topics"):
        a("<h2>Пропуски в наборе</h2><ul>" + "".join(f"<li>{e(m)}</li>" for m in d["missing_topics"]) + "</ul>")
    a("<h2>Уточняющие вопросы к заказчику</h2><ol>" + "".join(
        f"<li>{e(q)}<br><span class=mute>Ответ: {e(ans or 'не получен')}</span></li>" for q, ans in d["questions"].items()) + "</ol>")
    a(f"<p class=mute>Контроль качества: отброшено верификатором цитат {len(d['rejected'])}, отклонено критиком {len(d['dismissed'])}. "
      f"Обращений к модели {d['stats'].get('llm_calls', 0)}, из кэша {d['stats'].get('cache_hits', 0)}.</p>")
    a(f"<script>{JS}</script></div></body></html>")
    return "".join(h)
