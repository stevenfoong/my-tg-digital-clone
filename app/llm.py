"""Client for the self-hosted model (Ollama native API, or any OpenAI-compatible API)."""
from __future__ import annotations

import json
import logging
import re

import httpx

from .config import LLMConfig

log = logging.getLogger(__name__)


class LLMError(Exception):
    pass


def parse_drafts(raw: str, expected: int) -> list[str]:
    """Pull a list of drafts out of the model's reply, tolerating sloppy output."""
    text = raw.strip()
    # Drop a thinking block or markdown fences if the model added them.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()

    candidates = [text]
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        candidates.append(match.group(0))
    match = re.search(r"\[.*\]", text, flags=re.DOTALL)
    if match:
        candidates.append(match.group(0))

    for cand in candidates:
        try:
            data = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        items = None
        if isinstance(data, dict):
            for key in ("drafts", "replies", "suggestions", "options"):
                if isinstance(data.get(key), list):
                    items = data[key]
                    break
            if items is None and len(data) == 1 and isinstance(next(iter(data.values())), list):
                items = next(iter(data.values()))
        elif isinstance(data, list):
            items = data
        if items:
            drafts = []
            for item in items:
                if isinstance(item, str) and item.strip():
                    drafts.append(item.strip())
                elif isinstance(item, dict):
                    val = item.get("text") or item.get("draft") or item.get("reply")
                    if isinstance(val, str) and val.strip():
                        drafts.append(val.strip())
            if drafts:
                return drafts[:expected]

    # Last resort: numbered or bulleted lines.
    lines = [re.sub(r"^\s*(?:\d+[.)]|[-*•])\s*", "", ln).strip() for ln in text.splitlines()]
    lines = [ln.strip('"') for ln in lines if ln]
    if lines:
        return lines[:expected]
    raise LLMError("The model returned an empty reply")


class LLMClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(cfg.timeout, connect=15),
            headers={"Content-Type": "application/json", **cfg.headers},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def draft(self, system: str, user: str, expected: int) -> list[str]:
        raw = await self._chat(system, user)
        return parse_drafts(raw, expected)

    async def _chat(self, system: str, user: str) -> str:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        if self.cfg.api == "ollama":
            url = f"{self.cfg.base_url}/api/chat"
            body: dict = {
                "model": self.cfg.model,
                "messages": messages,
                "stream": False,
                "format": "json",
                "options": {"temperature": self.cfg.temperature},
            }
            if self.cfg.think is not None:
                body["think"] = self.cfg.think
        else:
            url = f"{self.cfg.base_url}/v1/chat/completions"
            body = {
                "model": self.cfg.model,
                "messages": messages,
                "temperature": self.cfg.temperature,
            }

        try:
            resp = await self._client.post(url, json=body)
        except httpx.TimeoutException as exc:
            raise LLMError(f"The model did not answer within {self.cfg.timeout}s") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"Could not reach the model server: {exc.__class__.__name__}") from exc

        if resp.status_code in (401, 403):
            raise LLMError(
                f"The model server refused the request (HTTP {resp.status_code}). "
                "Check the Cloudflare Access service token."
            )
        if resp.status_code >= 400:
            snippet = resp.text[:300].replace("\n", " ")
            raise LLMError(f"Model server error HTTP {resp.status_code}: {snippet}")
        ctype = resp.headers.get("content-type", "")
        if "json" not in ctype:
            # Cloudflare Access login pages come back as HTML with status 200.
            raise LLMError(
                "The model server returned a non-JSON page (often a Cloudflare Access "
                "login page). Check the service token and the Access policy."
            )
        data = resp.json()
        try:
            if self.cfg.api == "ollama":
                return data["message"]["content"]
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("Unexpected response format from the model server") from exc
