"""Optional LLM judging. Sends only compact candidate transcripts (never media), batched,
cached on disk, capped by a per-job budget. The model returns WORD INDICES; timestamps stay deterministic.

modes: disabled (no LLM) | local_only (Ollama) | smart (OpenAI only for borderline candidates) | quality (OpenAI for all shortlisted)
"""
import json, os, urllib.request
from pathlib import Path
from . import boundaries as B
from .score import dedupe
from .util import digest, jload, jsave

# USD per 1M tokens (in, out). Verify against current OpenAI pricing; override with CLIPPER_PRICE="in,out".
PRICE = tuple(float(x) for x in os.environ.get("CLIPPER_PRICE", "0.15,0.60").split(","))
SYSTEM = ("You judge clips from a transcript for short-form video. Words are written as [index]word. "
          "For each candidate return JSON {\"results\":[{\"id\":int,\"score\":0-10,\"standalone\":bool,"
          "\"start_word\":index,\"end_word\":index,\"title\":str}]}. start_word/end_word must be indices shown in that "
          "candidate, start at the beginning of the needed setup, end after the payoff or reaction. Score humour, "
          "surprise, story, strong opinion. Penalise clips that need earlier context.")


class Budget:
    def __init__(self, max_calls=6, max_tokens=60000):
        self.max_calls, self.max_tokens, self.calls, self.cached, self.tin, self.tout = max_calls, max_tokens, 0, 0, 0, 0

    def ok(self):
        return self.calls < self.max_calls and self.tin + self.tout < self.max_tokens

    def report(self):
        return {"calls": self.calls, "cache_hits": self.cached, "tokens_in": self.tin, "tokens_out": self.tout,
                "usd": round(self.tin / 1e6 * PRICE[0] + self.tout / 1e6 * PRICE[1], 5)}


def _post(url, body, headers):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def openai_chat(prompt, model="gpt-4o-mini"):
    key = os.environ["OPENAI_API_KEY"]
    r = _post("https://api.openai.com/v1/chat/completions",
              {"model": model, "temperature": 0, "response_format": {"type": "json_object"},
               "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]},
              {"Authorization": f"Bearer {key}"})
    u = r.get("usage", {})
    return r["choices"][0]["message"]["content"], u.get("prompt_tokens", 0), u.get("completion_tokens", 0)


def ollama_chat(prompt, model=None):
    r = _post(os.environ.get("OLLAMA_URL", "http://localhost:11434") + "/api/chat",
              {"model": model or os.environ.get("OLLAMA_MODEL", "qwen2.5:7b"), "stream": False, "format": "json",
               "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]}, {})
    return r["message"]["content"], r.get("prompt_eval_count", 0), r.get("eval_count", 0)


def provider_for(mode):
    if mode == "local_only":
        return ollama_chat
    if mode in ("smart", "quality") and os.environ.get("OPENAI_API_KEY"):
        return openai_chat
    return None  # no key / disabled -> local scoring only (never a hard failure)


def _prompt(words, batch):
    parts = []
    for k, c in enumerate(batch):
        a, b = c["final_words"]
        a, b = max(0, a - 12), min(len(words) - 1, b + 12)
        parts.append(f"## candidate {k}\n" + " ".join(f"[{i}]{words[i]['w']}" for i in range(a, b + 1)))
    return "\n\n".join(parts)


def judge(words, cands, chat_fn, budget, cache_dir, batch_size=8):
    """Annotate candidates in place with llm_score/title/standalone and, if valid, re-snapped boundaries."""
    sents = B.sentences(words)
    cache_dir = Path(cache_dir)
    for i in range(0, len(cands), batch_size):
        batch = cands[i:i + batch_size]
        prompt = _prompt(words, batch)
        f = cache_dir / f"{digest(prompt, chat_fn.__name__)}.json"
        res = jload(f)
        if res is not None:
            budget.cached += 1
        elif budget.ok():
            try:
                txt, tin, tout = chat_fn(prompt)
                res = json.loads(txt)
            except Exception as e:  # network/parse error: keep local result, do not abort the job
                print(f"llm: batch {i // batch_size} skipped ({e})")
                continue
            budget.calls += 1
            budget.tin, budget.tout = budget.tin + tin, budget.tout + tout
            jsave(f, res)
        else:
            continue
        for r in res.get("results", []):
            if not isinstance(r.get("id"), int) or not 0 <= r["id"] < len(batch):
                continue
            c = batch[r["id"]]
            c["llm_score"] = max(0.0, min(10.0, float(r.get("score", 0)))) / 10
            c["standalone"] = bool(r.get("standalone", True))
            c["title"] = str(r.get("title", ""))[:100]
            s, e = r.get("start_word"), r.get("end_word")
            if isinstance(s, int) and isinstance(e, int) and 0 <= s <= e < len(words):
                c.update({k: v for k, v in B.refine_checked(words, s, e, sents).items() if k not in ("core_start", "core_end")})
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
    return dedupe(sorted(short, key=lambda c: -c["final_score"]))[:n], budget.report()
