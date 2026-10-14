"""LLM provider abstraction.

Default is "offline": no network, no cost. Agents still work because every
decision is made by deterministic rules over ontology evidence, and narratives
fall back to templates. Plug in a provider to get model-written narratives:

  IRONWRIGHT_LLM=anthropic   ANTHROPIC_API_KEY=...       (optional IRONWRIGHT_LLM_MODEL)
  IRONWRIGHT_LLM=openai      OPENAI_API_KEY=...
  IRONWRIGHT_LLM=ollama      (local, free)              IRONWRIGHT_OLLAMA_URL
  IRONWRIGHT_LLM=cloudflare  CF_ACCOUNT_ID=... CF_API_TOKEN=...   (Workers AI free tier)
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

import httpx

from .config import setting


@dataclass
class LLMResult:
    text: str | None
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: float = 0.0


class LLM:
    def __init__(self, provider: str | None = None, model: str | None = None):
        self.provider = (provider or setting("LLM", "offline") or "offline").lower()
        self.model = model or setting("LLM_MODEL") or {
            "anthropic": "claude-haiku-4-5-20251001",
            "openai": "gpt-4o-mini",
            "ollama": "llama3.1",
            "cloudflare": "@cf/meta/llama-3.1-8b-instruct",
        }.get(self.provider, "offline-template")

    @property
    def offline(self) -> bool:
        return self.provider == "offline"

    def complete(self, system: str, prompt: str, max_tokens: int = 600) -> LLMResult:
        if self.offline:
            return LLMResult(text=None, model=self.model)
        t0 = time.time()
        try:
            fn = getattr(self, f"_{self.provider}")
            res = fn(system, prompt, max_tokens)
        except Exception as e:  # never let a provider outage break the pipeline
            return LLMResult(text=None, model=f"{self.model} (error: {type(e).__name__})")
        res.latency_ms = round((time.time() - t0) * 1000, 1)
        return res

    def _anthropic(self, system: str, prompt: str, max_tokens: int) -> LLMResult:
        r = httpx.post("https://api.anthropic.com/v1/messages", timeout=60, headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
            "content-type": "application/json"}, json={
            "model": self.model, "max_tokens": max_tokens, "system": system,
            "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        j = r.json()
        text = "".join(b.get("text", "") for b in j.get("content", []))
        u = j.get("usage", {})
        return LLMResult(text, self.model, u.get("input_tokens", 0), u.get("output_tokens", 0))

    def _openai(self, system: str, prompt: str, max_tokens: int) -> LLMResult:
        r = httpx.post("https://api.openai.com/v1/chat/completions", timeout=60, headers={
            "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}, json={
            "model": self.model, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]})
        r.raise_for_status()
        j = r.json()
        u = j.get("usage", {})
        return LLMResult(j["choices"][0]["message"]["content"], self.model,
                         u.get("prompt_tokens", 0), u.get("completion_tokens", 0))

    def _ollama(self, system: str, prompt: str, max_tokens: int) -> LLMResult:
        url = setting("OLLAMA_URL", "http://localhost:11434")
        r = httpx.post(f"{url}/api/chat", timeout=120, json={
            "model": self.model, "stream": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]})
        r.raise_for_status()
        j = r.json()
        return LLMResult(j["message"]["content"], self.model,
                         j.get("prompt_eval_count", 0), j.get("eval_count", 0))

    def _cloudflare(self, system: str, prompt: str, max_tokens: int) -> LLMResult:
        acct, token = os.environ["CF_ACCOUNT_ID"], os.environ["CF_API_TOKEN"]
        r = httpx.post(f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/{self.model}",
                       timeout=60, headers={"Authorization": f"Bearer {token}"}, json={
                           "max_tokens": max_tokens,
                           "messages": [{"role": "system", "content": system},
                                        {"role": "user", "content": prompt}]})
        r.raise_for_status()
        return LLMResult(r.json()["result"]["response"], self.model)
