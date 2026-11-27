"""Локальная веб-оболочка для демо: загрузить ТЗ → смотреть шаги → открыть отчёт.

Запуск: python -m validator.web   (затем открыть http://127.0.0.1:8000)
Работает только на этом компьютере (127.0.0.1), только стандартная библиотека Python.
Внутри — тот же пайплайн, что и в CLI; отчёты пишутся в reports/web/.
"""
import html
import json
import threading
import time
import webbrowser
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dotenv import load_dotenv

from . import html_report, loader, pipeline, report
from . import llm as llm_mod

OUT = Path("reports/web")
STATE = {"running": False, "log": [], "report": None, "error": None, "started": None}
LOCK = threading.Lock()

PAGE = """<!doctype html><html lang=ru><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Агент проверки ТЗ</title><style>
:root{--bg:#f6f5f1;--card:#fbfbf8;--ink:#13202e;--mute:#4a5662;--line:#ccd4d1;--acc:#1f7a68}
@media (prefers-color-scheme:dark){:root{--bg:#13202e;--card:#1c2d3e;--ink:#e9eeec;--mute:#c9d8d4;--line:#2c4256;--acc:#6cc3ae}}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,Segoe UI,Roboto,sans-serif}
.w{max-width:860px;margin:0 auto;padding:40px 16px}h1{margin:0 0 6px;font-size:30px}.m{color:var(--mute)}
.c{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px;margin-top:20px}
button,.b{background:var(--acc);color:#fff;border:0;border-radius:8px;padding:10px 18px;font-size:16px;cursor:pointer;text-decoration:none;display:inline-block}
button:disabled{opacity:.5}pre{white-space:pre-wrap;font:14px/1.5 ui-monospace,Consolas,monospace;margin:0;max-height:420px;overflow:auto}
.err{color:#c62828}</style><body><div class=w>
<h1>Агент проверки ТЗ</h1><div class=m>ISO/IEC/IEEE 29148:2018 · IEEE 830-1998 · PDF, DOCX, MD</div>
<form class=c id=f><input type=file name=doc accept=".pdf,.docx,.md,.txt" required>
<label style="margin-left:12px"><input type=checkbox name=gold checked> сверить с эталоном</label>
<div style="margin-top:14px"><button id=go>Проверить</button></div></form>
<div class=c><b>Ход проверки</b><pre id=log class=m>Загрузите документ.</pre><div id=res style="margin-top:14px"></div></div>
</div><script>
const f=document.getElementById('f'),log=document.getElementById('log'),res=document.getElementById('res'),go=document.getElementById('go');
f.onsubmit=async e=>{e.preventDefault();go.disabled=true;res.innerHTML='';log.textContent='Отправка…';
 const r=await fetch('/run',{method:'POST',body:new FormData(f)});const j=await r.json();
 if(j.error){log.textContent=j.error;go.disabled=false;return}poll()};
async function poll(){const s=await (await fetch('/status')).json();log.textContent=s.log.join('\\n')||'…';log.scrollTop=1e9;
 if(s.error){res.innerHTML='<span class=err>'+s.error+'</span>';go.disabled=false;return}
 if(s.report){res.innerHTML='<a class=b href="/report" target=_blank>Открыть отчёт</a>';go.disabled=false;return}
 setTimeout(poll,1000)}
fetch('/status').then(r=>r.json()).then(s=>{if(s.running){go.disabled=true;poll()}});
</script>"""


def _log(msg: str):
    with LOCK:
        STATE["log"].append(msg)
    print(msg)


def _run(path: Path, use_gold: bool):
    try:
        report.agent_version.cache_clear()  # сервер живёт долго: версия на момент старта этого прогона
        report.agent_version()
        doc = loader.load(str(path))
        llm = llm_mod.from_env()
        llm.log = _log
        _log(f"Документ: {path.name}, строк: {len(doc.lines)}; модели: {', '.join(llm.chain)}")
        extras = llm_mod.ensemble_from_env(exclude=llm.chain[0], log=_log)
        res = pipeline.run(llm, doc, log=_log, ensemble=extras)
        data = report.to_json(res)
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "report.md").write_text(report.build(res), encoding="utf-8")
        (OUT / "report.json").write_text(data, encoding="utf-8")
        (OUT / "report.html").write_text(html_report.build(data), encoding="utf-8")
        _log(f"Модель: {res.model}")
        _log(f"Готово за {time.time() - STATE['started']:.0f} c. Индекс готовности {res.readiness()}/100, "
             f"уникальных дефектов {len(res.all_findings())}.")
        gold = Path("gold/defects.md")
        if use_gold and gold.exists():
            from . import evaluate
            g = evaluate.parse_gold(str(gold))
            m, found, used = evaluate.evaluate(llm, json.loads(data), g)
            (OUT / "report_eval.md").write_text(evaluate.render(json.loads(data), g, m, found, used), encoding="utf-8")
            _log(f"Полнота против эталона: {len(found)}/{len(g)}.")
        with LOCK:
            STATE["report"] = str(OUT / "report.html")
    except BaseException as e:  # SystemExit из from_env тоже показываем в браузере
        with LOCK:
            STATE["error"] = html.escape(str(e) or type(e).__name__)
    finally:
        with LOCK:
            STATE["running"] = False


class H(BaseHTTPRequestHandler):
    def _send(self, code, body: bytes, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        if self.path == "/":
            self._send(200, PAGE.encode("utf-8"))
        elif self.path == "/status":
            with LOCK:
                self._json({k: STATE[k] for k in ("running", "log", "report", "error")})
        elif self.path == "/report" and STATE["report"]:
            self._send(200, Path(STATE["report"]).read_bytes())
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path != "/run":
            return self._send(404, b"not found", "text/plain")
        with LOCK:
            if STATE["running"]:
                return self._json({"error": "Проверка уже идёт"}, 409)
            STATE["running"] = True  # занять сразу: два одновременных POST не запустят два прогона
        started = False
        try:
            started = self._accept()
        finally:
            if not started:
                with LOCK:
                    STATE["running"] = False

    def _accept(self) -> bool:
        """Принять файл и запустить прогон. False — запрос отклонён (ответ уже отправлен)."""
        n = int(self.headers.get("Content-Length", 0))
        if n > 50 * 1024 * 1024:
            self._json({"error": "Файл больше 50 МБ"}, 413)
            return False
        raw = b"Content-Type: " + self.headers.get("Content-Type", "").encode() + b"\r\n\r\n" + self.rfile.read(n)
        msg = BytesParser(policy=email_policy).parsebytes(raw)
        fileobj, use_gold = None, False
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if name == "doc":
                fileobj = (part.get_filename() or "doc", part.get_payload(decode=True))
            elif name == "gold":
                use_gold = True
        if not fileobj or not fileobj[1]:
            self._json({"error": "Файл не получен"}, 400)
            return False
        fname = Path(fileobj[0]).name
        if Path(fname).suffix.lower() not in (".pdf", ".docx", ".md", ".txt", ".markdown"):
            self._json({"error": "Нужен PDF, DOCX, MD или TXT"}, 400)
            return False
        OUT.mkdir(parents=True, exist_ok=True)
        # сохраняем под исходным именем (оно попадает в шапку отчёта); опасные символы заменяем
        safe = "".join(ch if ch.isalnum() or ch in "-_. ()" else "_" for ch in Path(fname).stem).strip(" .") or "document"
        dest = OUT / (safe[:80] + Path(fname).suffix.lower())
        dest.write_bytes(fileobj[1])
        with LOCK:
            STATE.update(running=True, log=[], report=None, error=None, started=time.time())
        threading.Thread(target=_run, args=(dest, use_gold), daemon=True).start()
        self._json({"ok": True})
        return True

    def log_message(self, *a):
        pass


def main(port: int = 8000):
    load_dotenv()
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    url = f"http://127.0.0.1:{port}"
    print(f"Веб-оболочка: {url}  (Ctrl+C — остановить)")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    srv.serve_forever()


if __name__ == "__main__":
    main()
