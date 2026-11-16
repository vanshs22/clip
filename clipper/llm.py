"""Optional LLM judging. Sends only compact candidate transcripts (never media), batched, cached on disk, under a HARD per-job budget.
The model returns WORD INDICES that must fall inside the candidate's own transcript window; timestamps stay deterministic.

modes: disabled (no LLM) | local_only (Ollama) | smart (OpenAI only for borderline candidates, Ollama if no key) | quality (OpenAI for all shortlisted)
Budget guarantee: before every call the worst case (estimated input + max output tokens, priced) is RESERVED; if it does not fit under the
remaining token/USD/call allowance the call is not made. Afterwards exact usage from the response replaces the reservation."""
import json, math, os, re, time, urllib.error, urllib.request
from pathlib import Path
from . import boundaries as B
from .score import dedupe
from .util import digest, jload, jsave, note_skipped, redact

# USD per 1M tokens (in, out). Verify against current OpenAI pricing; override with CLIPPER_PRICE="in,out".
PRICE = tuple(float(x) for x in os.environ.get("CLIPPER_PRICE", "0.15,0.60").split(","))
MODEL = os.environ.get("CLIPPER_OPENAI_MODEL", "gpt-4o-mini")
MAX_OUT = 900          # output tokens allowed per call (8 candidates x ~60 tokens + JSON overhead)
CALL_TOKEN_CAP = 12000  # per-call cap on input+output
SYSTEM = ("You judge clips from a transcript for short-form video. Words are written as [index]word. "
          "For each candidate return JSON {\"results\":[{\"id\":int,\"score\":0-10,\"standalone\":bool,"
          "\"start_word\":index,\"end_word\":index,\"title\":str}]}. start_word/end_word must be indices shown in that "
          "candidate, start at the beginning of the needed setup, end after the payoff or reaction. Score humour, "
          "surprise, story, strong opinion. Penalise clips that need earlier context.")


def est_tokens(text):
    """Deliberately pessimistic (English averages ~4 chars/token; we assume 3), so an estimate is never below the real count."""
    return math.ceil(len(text) / 3.0) + 16


class Transient(Exception):
    pass


class Budget:
    """Persistent (JSON file) so a live session and the final VOD pass share ONE allowance."""

    def __init__(self, max_calls=6, max_tokens=60000, max_usd=0.10, path=None):
        self.max_calls, self.max_tokens, self.max_usd, self.path = max_calls, max_tokens, max_usd, path
        self.calls = self.cached = self.tin = self.tout = self.rejected = self.fallbacks = 0
        self.reserved = (0, 0)

    @classmethod
    def load(cls, path, max_calls=6, max_tokens=60000, max_usd=0.10):
        b = cls(max_calls, max_tokens, max_usd, path)
        d = jload(path, {}) or {}
        for k in ("calls", "cached", "tin", "tout", "rejected", "fallbacks"):
            setattr(b, k, int(d.get(k, 0)))
        return b

    def save(self):
        if self.path:
            jsave(self.path, {k: getattr(self, k) for k in ("calls", "cached", "tin", "tout", "rejected", "fallbacks")})

    @staticmethod
    def usd_of(tin, tout):
        return tin / 1e6 * PRICE[0] + tout / 1e6 * PRICE[1]

    @property
    def usd(self):
        return self.usd_of(self.tin, self.tout)

    def can_reserve(self, t_in, t_out):
        if self.calls >= self.max_calls or t_in + t_out > CALL_TOKEN_CAP:
            return False
        return (self.tin + self.tout + t_in + t_out <= self.max_tokens) and (self.usd + self.usd_of(t_in, t_out) <= self.max_usd + 1e-12)

    def reserve(self, t_in, t_out):
        if not self.can_reserve(t_in, t_out):
            return False
        self.calls += 1  # an attempt counts even if it fails: retries can never loop past max_calls
        self.reserved = (t_in, t_out)
        self.tin, self.tout = self.tin + t_in, self.tout + t_out
        self.save()
        return True

    def settle(self, actual_in=None, actual_out=None, billed=True):
        """Replace the reservation with what the API reported (or release it if the request was never billed)."""
        r_in, r_out = self.reserved
        self.tin, self.tout = self.tin - r_in, self.tout - r_out
        if billed:
            self.tin += actual_in if actual_in is not None else r_in
            self.tout += actual_out if actual_out is not None else r_out
        self.reserved = (0, 0)
        self.save()

    def report(self):
        return {"calls": self.calls, "cache_hits": self.cached, "tokens_in": self.tin, "tokens_out": self.tout, "usd": round(self.usd, 5),
                "caps": {"calls": self.max_calls, "tokens": self.max_tokens, "usd": self.max_usd}, "rejected_responses": self.rejected, "local_fallbacks": self.fallbacks}


def _key():
    k = os.environ.get("OPENAI_API_KEY")
    f = os.environ.get("OPENAI_API_KEY_FILE")
    if not k and f and Path(f).is_file():
        k = Path(f).read_text().strip()
    return k


def _post(url, body, headers, timeout):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise (Transient if e.code in (408, 409, 429) or e.code >= 500 else RuntimeError)(f"HTTP {e.code}")  # never include headers/body: they can echo the key
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise Transient(redact(type(e).__name__))


def openai_chat(prompt, max_out=MAX_OUT, timeout=60):
    r = _post("https://api.openai.com/v1/chat/completions",
              {"model": MODEL, "temperature": 0, "max_tokens": max_out, "response_format": {"type": "json_object"},
               "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]},
              {"Authorization": f"Bearer {_key()}"}, timeout)
    u = r.get("usage", {})
    return r["choices"][0]["message"]["content"], u.get("prompt_tokens"), u.get("completion_tokens")


def ollama_chat(prompt, max_out=MAX_OUT, timeout=180):
    r = _post(os.environ.get("OLLAMA_URL", "http://localhost:11434") + "/api/chat",
              {"model": os.environ.get("OLLAMA_MODEL", "qwen2.5:7b"), "stream": False, "format": "json", "options": {"num_predict": max_out},
               "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]}, {}, timeout)
    return r["message"]["content"], None, None


def ollama_ok(timeout=1.0):
    try:
        urllib.request.urlopen(os.environ.get("OLLAMA_URL", "http://localhost:11434") + "/api/tags", timeout=timeout).read(1)
        return True
    except Exception:
        return False


def provider_for(mode):
    """-> chat function or None (local scoring only). Order for smart/quality: OpenAI (key present) -> Ollama (reachable) -> none."""
    if mode == "local_only":
        if ollama_ok():
            return ollama_chat
        note_skipped("LLM judging", "local_only requested but Ollama is not reachable")
        return None
    if mode in ("smart", "quality"):
        if _key():
            return openai_chat
        if ollama_ok():
            return ollama_chat
        note_skipped("LLM judging", "no OPENAI_API_KEY and no Ollama: using local scoring only")
    return None


def _prompt(words, batch, pad=12):
    parts, ranges = [], []
    for k, c in enumerate(batch):
        a, b = c["final_words"]
        lo, hi = max(0, a - pad), min(len(words) - 1, b + pad)
        ranges.append((lo, hi))
        parts.append(f"## candidate {k}\n" + " ".join(f"[{i}]{words[i]['w']}" for i in range(lo, hi + 1)))
    return "\n\n".join(parts), ranges


def validate_response(res, n, ranges):
    """-> list of clean items, or None if the response is structurally unusable. An item is dropped (not repaired) if anything is off:
    ids must be unique ints in range; indices ints inside THAT candidate's window (so the model cannot pick unrelated transcript)."""
    if not isinstance(res, dict) or not isinstance(res.get("results"), list):
        return None
    out, seen = [], set()
    for r in res["results"]:
        if not isinstance(r, dict):
            continue
        i = r.get("id")
        if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < n or i in seen:
            continue
        sc = r.get("score")
        if isinstance(sc, bool) or not isinstance(sc, (int, float)) or not 0 <= sc <= 10:
            continue
        item = {"id": i, "score": float(sc) / 10, "standalone": r.get("standalone") if isinstance(r.get("standalone"), bool) else True,
                "title": re.sub(r"[\x00-\x1f]", " ", str(r.get("title", "")))[:100]}
        s, e = r.get("start_word"), r.get("end_word")
        lo, hi = ranges[i]
        if all(isinstance(x, int) and not isinstance(x, bool) for x in (s, e)) and lo <= s <= e <= hi:
            item["span"] = (s, e)
        seen.add(i)
        out.append(item)
    return out


def judge(words, cands, chat_fn, budget, cache_dir, batch_size=8, sleep=time.sleep):
    """Annotate candidates in place with llm_score/title/standalone and, if valid, re-snapped boundaries. Never raises on provider trouble."""
    sents = B.sentences(words)
    cache_dir = Path(cache_dir)
    for i in range(0, len(cands), batch_size):
        batch = [c for c in cands[i:i + batch_size] if c.get("kind") != "event"]  # event clips have no transcript to judge
        if not batch:
            continue
        prompt, ranges = _prompt(words, batch)
        f = cache_dir / f"{digest(prompt, chat_fn.__name__, MODEL, 'v2')}.json"
        items = validate_response(jload(f), len(batch), ranges) if f.exists() else None
        if items is not None:
            budget.cached += 1
        else:
            t_in = est_tokens(SYSTEM + prompt)
            for attempt in range(3):  # 1 try + 2 retries for transient errors / malformed output
                if not budget.reserve(t_in, MAX_OUT):
                    note_skipped("LLM judging (part)", "budget exhausted: remaining candidates use local scoring")
                    break
                try:
                    txt, tin, tout = chat_fn(prompt, MAX_OUT)
                    res = json.loads(txt)
                except Transient as e:
                    budget.settle(billed=False)
                    sleep(min(8, 2 ** attempt))
                    continue
                except (RuntimeError, ValueError, KeyError) as e:  # permanent error (auth, bad request) or unparseable JSON
                    budget.settle(None, None, billed=True)
                    budget.rejected += isinstance(e, ValueError)
                    if isinstance(e, RuntimeError):
                        note_skipped("LLM judging", redact(str(e)))
                        break
                    continue
                budget.settle(tin, tout)
                items = validate_response(res, len(batch), ranges)
                if items is None:
                    budget.rejected += 1
                    continue
                jsave(f, res)
                break
            if items is None:
                budget.fallbacks += 1
                continue
        for r in items:
            c = batch[r["id"]]
            c["llm_score"], c["standalone"], c["title"] = r["score"], r["standalone"], r["title"]
            if "span" in r:
                c.update({k: v for k, v in B.refine_checked(words, *r["span"], sents).items() if k not in ("core_start", "core_end")})
    budget.save()
    return cands


def select(words, cands, n, mode, cache_dir, budget=None, margin=0.12):
    """Rank. Local score decides clear cases; the LLM only sees borderline ones in `smart` mode."""
    budget = budget or Budget()
    short = sorted(cands, key=lambda c: -c["score"])[:max(n * 2, n + 4)]
    fn = provider_for(mode)
    if fn and short:
        cut = short[min(n, len(short)) - 1]["score"]
        todo = short if mode == "quality" or fn is ollama_chat else [c for c in short if abs(c["score"] - cut) <= margin]
        judge(words, todo, fn, budget, cache_dir)
    for c in short:
        c["final_score"] = round(0.4 * c["score"] + 0.6 * c["llm_score"], 4) if "llm_score" in c else c["score"]
        if c.get("standalone") is False:
            c["final_score"] *= 0.8
    rep = budget.report()
    rep["mode"], rep["provider"] = mode, (fn.__name__ if fn else None)
    return dedupe(sorted(short, key=lambda c: -c["final_score"]))[:n], rep
