"""Обращение к LLM. По умолчанию Google Gemini (бесплатный ключ AI Studio).

Надёжность:
- повторы с экспоненциальной задержкой на 429/5xx (лимиты и «high demand»);
- автоматический переход на следующую модель из цепочки, если текущая недоступна;
  фактически использованные модели попадают в паспорт прогона;
- кэш ответов на диске (.cache/): повторный прогон воспроизводим и не тратит лимит.
"""
import hashlib
import json
import os
import random
import time
from pathlib import Path
from typing import List, Optional, Type, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

DEFAULT_CHAIN = ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"]
RETRYABLE = {408, 429, 500, 502, 503, 504}
MODEL_GONE = {400, 404, 413}  # модель не найдена / недоступна / запрос ей не по размеру — сразу следующая


# OpenAI-совместимые провайдеры. Модель в цепочке пишется с префиксом: "groq:openai/gpt-oss-120b",
# "openrouter:deepseek/deepseek-chat-v3.1:free", "cerebras:gpt-oss-120b", "openai:<model>".
# Без префикса модель считается моделью Gemini.
PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "cerebras": ("https://api.cerebras.ai/v1", "CEREBRAS_API_KEY"),
    "openai": (os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"), "OPENAI_API_KEY"),
}


class HTTPProviderError(Exception):
    def __init__(self, code: Optional[int], message: str, retry_after: Optional[float] = None):
        super().__init__(f"{code} {message}")
        self.code = code
        self.retry_after = retry_after


def _split(model: str):
    """'groq:llama' -> ('groq', 'llama'); 'gemini-3.5-flash' -> (None, 'gemini-3.5-flash')."""
    head, sep, tail = model.partition(":")
    if sep and head in PROVIDERS:
        return head, tail
    return None, model


def _openai_compatible(provider: str, model: str, system: str, prompt: str, schema, temperature: float) -> str:
    import urllib.error
    import urllib.request

    base, key_env = PROVIDERS[provider]
    key = os.getenv(key_env)
    if not key:
        raise HTTPProviderError(404, f"нет {key_env} в .env")
    sys_msg = (system + "\n\nОтвечай строго одним JSON-объектом (json) без пояснений, по этой JSON Schema:\n"
               + json.dumps(schema.model_json_schema(), ensure_ascii=False))
    body = {
        "model": model,
        "temperature": temperature,
        "messages": [{"role": "system", "content": sys_msg}, {"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "User-Agent": "bcc-validator"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        ra = e.headers.get("retry-after") if e.headers else None
        try:
            ra = float(ra) if ra else None
        except ValueError:
            ra = None
        raise HTTPProviderError(e.code, e.read().decode("utf-8", "replace")[:500], ra) from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise HTTPProviderError(None, str(e)) from None
    text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    text = text.strip()
    if text.startswith("```"):  # некоторые модели оборачивают JSON в ```json ... ```
        text = text.strip("`")
        text = text[4:] if text.lower().startswith("json") else text
    return text.strip()


def _code(e: Exception) -> Optional[int]:
    c = getattr(e, "code", None) or getattr(e, "status_code", None)
    try:
        return int(c)
    except (TypeError, ValueError):
        return None


class LLM:
    def __init__(self, models: List[str], api_key: Optional[str], temperature: float = 0.1,
                 cache_dir: Optional[str] = ".cache", retries_per_model: int = 3, log=print):
        self.chain = [m for m in dict.fromkeys(m.strip() for m in models) if m]
        self.current = 0                 # индекс рабочей модели
        self._auto_current = 0           # что выбрал сам ask(); если pipeline сменил модель явно — уважаем это
        # временно перегруженные модели (503/429 и т.п.): модель -> время, после которого пробуем её снова.
        # Каждый новый запрос начинается с самой сильной модели, чей «перерыв» уже истёк.
        self.cooldown: dict = {}
        self.cooldown_sec = float(os.getenv("GEMINI_RETRY_PRIMARY_SEC", "90"))
        self.temperature = temperature
        self.cache = Path(cache_dir) if cache_dir else None
        self.retries = retries_per_model
        self.gone = set()        # модели, которых нет для этого ключа (404/400)
        self.rounds = 3          # кругов по цепочке моделей, прежде чем сдаться
        self.round_pause = 45
        self.log = log
        self.calls = 0
        self.cache_hits = 0
        self.used: List[str] = []        # модели, давшие ответы, в порядке появления
        self.skipped: dict = {}          # модель -> причина отказа
        self._client = None
        if api_key and any(_split(m)[0] is None for m in self.chain):
            from google import genai
            self._client = genai.Client(api_key=api_key)
        elif any(_split(m)[0] is None for m in self.chain):
            # моделей Gemini без ключа нет смысла пробовать
            self.gone.update(m for m in self.chain if _split(m)[0] is None)

    def _generate(self, model: str, system: str, prompt: str, schema, cfg) -> str:
        provider, name = _split(model)
        if provider:
            return _openai_compatible(provider, name, system, prompt, schema, self.temperature)
        resp = self._client.models.generate_content(model=name, contents=prompt, config=cfg)
        return resp.text or ""

    @property
    def model(self) -> str:
        return self.chain[self.current]

    def label(self) -> str:
        """Строка для паспорта прогона."""
        used = ", ".join(self.used) or self.model
        if not self.skipped:
            return used
        why = "; ".join(f"{m}: {r}" + (" временами" if m in self.used else "") for m, r in self.skipped.items())
        return f"{used} (недоступны: {why})"

    def _note(self, model: str):
        if model not in self.used:
            self.used.append(model)

    def _key(self, system: str, prompt: str, schema, salt: str = "") -> str:
        # ключ включает основную модель цепочки: прогон с другой --model не возьмёт чужой кэш
        raw = json.dumps([self.chain[0], self.temperature, system, prompt, schema.__name__, schema.model_json_schema()]
                         + ([salt] if salt else []),
                         ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def ask(self, system: str, prompt: str, schema: Type[T], salt: str = "") -> T:
        """salt — повторный запрос мимо прежней записи кэша (например, если ответ оказался пустым)."""
        from google.genai import types

        path = self.cache / f"{self._key(system, prompt, schema, salt)}.json" if self.cache else None
        if path and path.exists():
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
                obj = schema.model_validate_json(rec["text"])
                self.cache_hits += 1
                self._note(rec["model"])
                return obj
            except (ValidationError, KeyError, ValueError):
                path.unlink(missing_ok=True)

        cfg = types.GenerateContentConfig(
            system_instruction=system,
            temperature=self.temperature,
            response_mime_type="application/json",
            response_schema=schema,
        )
        last = None
        # вернуться к самой сильной модели, у которой истёк перерыв после перегрузки
        # (если модель только что явно сменил вызывающий код, например нарезка «на следующую», — не трогаем)
        now = time.time()
        if self.current == self._auto_current:
            for i, m in enumerate(self.chain):
                if m in self.gone:
                    continue
                if self.cooldown.get(m, 0) <= now:
                    if i < self.current:
                        self.log(f"     пробую снова {m}")
                    self.current = i
                    break
        for rnd in range(self.rounds):
            if rnd:
                # все модели были перегружены: пауза и ещё один круг по цепочке с начала
                self.log(f"     все модели недоступны, пауза {self.round_pause} c и новый круг")
                time.sleep(self.round_pause)
                self.current = 0
            while self.current < len(self.chain):
                model = self.model
                if model in self.gone:
                    self.current += 1
                    continue
                # модель, которую пробуем после перерыва, получает одну попытку: если всё ещё перегружена,
                # быстро уходим на запасную, не тратя ~30 c на повторы на каждом шаге
                tries = 1 if model in self.cooldown else self.retries
                for attempt in range(tries):
                    try:
                        self.calls += 1
                        text = self._generate(model, system, prompt, schema, cfg)
                        obj = schema.model_validate_json(text)
                        self._note(model)
                        self._auto_current = self.current
                        if path:
                            path.parent.mkdir(parents=True, exist_ok=True)
                            path.write_text(json.dumps({"model": model, "text": text}, ensure_ascii=False),
                                            encoding="utf-8")
                        return obj
                    except ValidationError as e:  # невалидный JSON — просто повторяем
                        last = e
                        self.log(f"     {model}: ответ не по схеме, повтор")
                    except Exception as e:
                        last = e
                        code = _code(e)
                        if code in MODEL_GONE or code in (401, 403):
                            if code in (401, 403):
                                prov = _split(model)[0]
                                var = PROVIDERS[prov][1] if prov else "GEMINI_API_KEY"
                                raise SystemExit(f"Ключ отклонён ({code}) для {model}. Проверьте {var} в .env"
                                                 + ("" if prov else " и обновите google-genai: "
                                                    "pip install -U -r requirements.txt")) from e
                            self.gone.add(model)
                            break  # модель не существует или недоступна ключу — к следующей
                        if code is not None and code not in RETRYABLE:
                            raise
                        if code == 429 and "per day" in str(e).lower():
                            # дневной лимит бесплатного тарифа: до конца прогона не вернётся
                            self.gone.add(model)
                            break
                        if attempt + 1 >= tries:
                            break
                        wait = min(40, 4 * 2 ** attempt) + random.uniform(0, 2)
                        ra = getattr(e, "retry_after", None)
                        if ra:  # провайдер сам сказал, сколько ждать (лимит в минуту)
                            wait = min(max(wait, ra + 1), 90)
                        self.log(f"     {model}: ошибка {code or type(e).__name__}, повтор через {wait:.0f} c")
                        time.sleep(wait)
                self.skipped[model] = str(_code(last) or type(last).__name__)
                if model not in self.gone:
                    self.cooldown[model] = time.time() + self.cooldown_sec
                if self.current + 1 < len(self.chain):
                    self.log(f"     {model} недоступна, переключаюсь на {self.chain[self.current + 1]}")
                self.current += 1
            self.current = len(self.chain) - 1
        raise RuntimeError(f"Ни одна модель из цепочки {self.chain} не ответила: {last}")


def ensemble_from_env(spec: Optional[str] = None, exclude: str = "", cache: bool = True, log=print) -> List[LLM]:
    """Дополнительные модели ансамбля: --ensemble или ENSEMBLE_MODELS. Модель без нужного ключа пропускается
    с предупреждением, так что с одним ключом агент работает как раньше."""
    spec = spec if spec is not None else os.getenv("ENSEMBLE_MODELS", "")
    key = os.getenv("GEMINI_API_KEY")
    if key and key.startswith("your_"):
        key = None
    out = []
    for m in [x.strip() for x in spec.split(",") if x.strip()]:
        if m == exclude:
            continue
        prov = _split(m)[0]
        if (prov and not os.getenv(PROVIDERS[prov][1])) or (not prov and not key):
            log(f"     ансамбль: пропускаю {m} — нет ключа в .env")
            continue
        out.append(LLM(models=[m], api_key=key, cache_dir=".cache" if cache else None))
    return out


def from_env(model_override: Optional[str] = None, cache: bool = True) -> LLM:
    """Цепочка моделей: --model (или GEMINI_MODEL), затем GEMINI_FALLBACK_MODELS.
    Модель с префиксом провайдера (groq:, openrouter:, cerebras:, openai:) идёт через OpenAI-совместимый API
    с ключом из соответствующей переменной .env; модели Gemini остаются запасными."""
    key = os.getenv("GEMINI_API_KEY")
    if key and key.startswith("your_"):
        key = None
    primary = model_override or os.getenv("GEMINI_MODEL", DEFAULT_CHAIN[0])
    fallback = os.getenv("GEMINI_FALLBACK_MODELS", ",".join(DEFAULT_CHAIN[1:])).split(",")
    models = [primary, *fallback]
    has_other = any(_split(m)[0] and os.getenv(PROVIDERS[_split(m)[0]][1]) for m in models)
    if not key and not has_other:
        raise SystemExit("Нет ключа модели. Укажите GEMINI_API_KEY (или GROQ_API_KEY / OPENROUTER_API_KEY / "
                         "CEREBRAS_API_KEY с --model провайдер:модель) в .env, см. README.")
    return LLM(models=models, api_key=key, cache_dir=".cache" if cache else None)
