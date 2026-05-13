"""
llm_backend.py — Ollama HTTP adapter for the QOFT Local Observatory.

Pure `requests` adapter. No langchain, no wrappers. The contract is:
  - preflight() raises BackendError if Ollama is unreachable or the model is
    not pulled. No silent fallbacks.
  - generate_json(prompt) returns a parsed JSON dict from the model's reply.
    If the model emits non-JSON, we attempt one tolerant extraction pass and
    raise LLMOutputError if that still fails.

The annotator relies on hard failure here so a misconfigured environment
never produces silently-bogus predictions.

Model: qwen2.5:7b is the canonical model tag for the Qwen Local Observatory
suite (Layers 1-6 + L6b). Per the Qwen revision spec: "Hard rule: do not
change model mid-layer. All layers in this suite must use the same model tag."
The original llama3.2 default has been retired; legacy Llama runs are
referenced via their closed CSVs (Layers 3.5, 4, 5), not re-executed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests


DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5:7b"
DEFAULT_TIMEOUT_S = 120


class BackendError(RuntimeError):
    """Raised when the Ollama server is unreachable or misconfigured."""


class LLMOutputError(RuntimeError):
    """Raised when the model produced a response we couldn't parse as JSON."""


@dataclass
class OllamaBackend:
    """Thin synchronous Ollama client."""

    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    timeout_s: int = DEFAULT_TIMEOUT_S
    temperature: float = 0.0          # deterministic for evaluation
    seed: int = 0
    num_predict: int = 256            # cap output length

    # ── preflight ────────────────────────────────────────────────────────────

    def preflight(self) -> None:
        """
        Verify (a) the server is reachable and (b) self.model is pulled.
        Raises BackendError with a clear message on failure.
        """
        tags_url = f"{self.base_url.rstrip('/')}/api/tags"
        try:
            resp = requests.get(tags_url, timeout=5)
        except requests.exceptions.RequestException as e:
            raise BackendError(
                f"Ollama unreachable at {self.base_url}.\n"
                f"  Network/connection error: {e}\n"
                f"  Fix: start Ollama (`ollama serve`) and confirm it's "
                f"listening on {self.base_url}."
            ) from e

        if resp.status_code != 200:
            raise BackendError(
                f"Ollama responded {resp.status_code} to GET {tags_url}.\n"
                f"  Body: {resp.text[:200]}"
            )

        try:
            payload = resp.json()
        except ValueError as e:
            raise BackendError(
                f"Ollama /api/tags returned non-JSON: {resp.text[:200]}"
            ) from e

        models = payload.get("models", [])
        names = {m.get("name", "") for m in models}
        # Ollama tags include the version tag (e.g. "qwen2.5:7b").
        # Match the bare model name OR any tagged variant.
        wanted = self.model
        matched = (
            wanted in names
            or any(n.split(":", 1)[0] == wanted for n in names)
            or any(n == wanted for n in names)
        )
        if not matched:
            raise BackendError(
                f"Model '{wanted}' is not pulled on this Ollama instance.\n"
                f"  Available: {sorted(names) or '(none)'}\n"
                f"  Fix: `ollama pull {wanted}`"
            )

    # ── generation ───────────────────────────────────────────────────────────

    def generate_json(self, prompt: str) -> Dict[str, Any]:
        """
        Send `prompt` to Ollama, ask for JSON, return parsed dict.
        Uses Ollama's `format=json` mode to constrain decoding.
        """
        url = f"{self.base_url.rstrip('/')}/api/generate"
        body = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": self.temperature,
                "seed": self.seed,
                "num_predict": self.num_predict,
            },
        }
        try:
            resp = requests.post(url, json=body, timeout=self.timeout_s)
        except requests.exceptions.RequestException as e:
            raise BackendError(f"Ollama POST {url} failed: {e}") from e

        if resp.status_code != 200:
            raise BackendError(
                f"Ollama returned {resp.status_code} from {url}: "
                f"{resp.text[:300]}"
            )

        try:
            envelope = resp.json()
        except ValueError as e:
            raise BackendError(
                f"Ollama envelope not JSON: {resp.text[:200]}"
            ) from e

        text = envelope.get("response", "")
        if not text:
            raise LLMOutputError(
                f"Ollama returned empty response. Envelope keys: {list(envelope.keys())}"
            )

        return _parse_json_lenient(text)


# ── helpers ──────────────────────────────────────────────────────────────────

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json_lenient(text: str) -> Dict[str, Any]:
    """
    Try strict JSON parse first; if that fails, extract the first {...} blob
    and parse that. Raise LLMOutputError if neither works.
    """
    text = text.strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_OBJECT_RE.search(text)
        if not m:
            raise LLMOutputError(
                f"No JSON object in model output. First 200 chars: {text[:200]!r}"
            )
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError as e:
            raise LLMOutputError(
                f"Model output not valid JSON. Error: {e}. "
                f"First 200 chars: {text[:200]!r}"
            ) from e

    if not isinstance(obj, dict):
        raise LLMOutputError(
            f"Model output parsed to {type(obj).__name__}, expected object."
        )
    return obj


# ── CLI smoke test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    backend = OllamaBackend()
    print(f"[llm_backend] Preflight: {backend.base_url} model={backend.model}")
    backend.preflight()
    print("[llm_backend] Server reachable, model pulled.")

    print("[llm_backend] Smoke test: asking for a JSON object...")
    out = backend.generate_json(
        'Reply ONLY with this JSON: {"ok": true, "answer": 42}'
    )
    print(f"[llm_backend] Got: {out}")
