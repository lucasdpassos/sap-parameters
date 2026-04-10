"""
minimax_client.py — Cliente para a API MiniMax via endpoint OpenAI-compatible.

MiniMax expõe dois endpoints:
  1. /anthropic          (Anthropic-compatible) — recebemos 200 OK mas sem vision real
  2. /v1/chat/completions (OpenAI-compatible)   — suporta image_url base64 → testado aqui

Fallback: se vision falhar, usa screen_context (OCR + OpenCV), igual ao Codemie.
"""

import os
import logging
import requests

from screen_context import build_context_from_b64

log = logging.getLogger("sap-agent.minimax")

MINIMAX_BASE    = "https://api.minimax.io"
CHAT_URL        = f"{MINIMAX_BASE}/v1/chat/completions"
DEFAULT_MODEL   = "MiniMax-M2.7"


class _FakeContent:
    def __init__(self, text: str):
        self.text = text


class _FakeMessage:
    def __init__(self, text: str):
        self.content = [_FakeContent(text)]


class _MessagesAPI:
    def __init__(self, client: "MiniMaxClient"):
        self._c = client

    def create(self, *, model: str, max_tokens: int, system: str, messages: list) -> _FakeMessage:
        return self._c._create(model=model, max_tokens=max_tokens, system=system, messages=messages)


class MiniMaxClient:
    """
    Drop-in para anthropic.Anthropic usando o endpoint OpenAI-compatible da MiniMax.
    Tenta vision nativa via /v1/chat/completions; fallback para screen_context.
    """

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL):
        self.api_key = api_key
        self.model   = model
        self.messages = _MessagesAPI(self)

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type":  "application/json",
        }

    # ── Message conversion (Anthropic → OpenAI) ────────────────────────────

    @staticmethod
    def _to_openai_messages(system: str, messages: list) -> list:
        result = []
        if system:
            result.append({"role": "system", "content": system})

        for msg in messages:
            role    = msg["role"]
            content = msg["content"]

            if isinstance(content, str):
                result.append({"role": role, "content": content})
                continue

            parts = []
            for block in content:
                btype = block.get("type", "text")
                if btype == "text":
                    parts.append({"type": "text", "text": block["text"]})
                elif btype == "image":
                    src = block.get("source", {})
                    if src.get("type") == "base64":
                        mime = src.get("media_type", "image/png")
                        data = src["data"]
                        parts.append({
                            "type":      "image_url",
                            "image_url": {"url": f"data:{mime};base64,{data}"},
                        })
            result.append({"role": role, "content": parts})

        return result

    # ── Vision via OpenAI-compatible completions ───────────────────────────

    def _create_via_completions(self, model: str, max_tokens: int,
                                 system: str, messages: list) -> str:
        oai_messages = self._to_openai_messages(system, messages)
        payload = {
            "model":      model,
            "messages":   oai_messages,
            "max_tokens": max_tokens,
            "stream":     False,
        }
        r = requests.post(CHAT_URL, headers=self._headers(), json=payload, timeout=90)

        if not r.text.strip():
            raise RuntimeError("empty response from MiniMax /v1/chat/completions")

        try:
            d = r.json()
        except Exception:
            raise RuntimeError(f"non-JSON from MiniMax ({r.status_code}): {r.text[:120]}")

        if r.status_code != 200:
            raise RuntimeError(f"MiniMax /v1/chat/completions error {r.status_code}: {d}")

        return d["choices"][0]["message"]["content"]

    # ── Fallback: screen_context text ─────────────────────────────────────

    def _create_via_screen_context(self, model: str, max_tokens: int,
                                    system: str, messages: list) -> str:
        last_user_idx = max(
            (i for i, m in enumerate(messages) if m["role"] == "user"),
            default=-1
        )

        parts = [f"[SYSTEM]: {system}"] if system else []

        for msg_idx, msg in enumerate(messages):
            role    = msg["role"].upper()
            content = msg["content"]
            is_latest = (msg_idx == last_user_idx)

            if isinstance(content, str):
                parts.append(f"[{role}]: {content}")
                continue

            text_parts, image_parts = [], []
            for block in content:
                btype = block.get("type", "text")
                if btype == "text":
                    text_parts.append(block["text"])
                elif btype == "image":
                    src = block.get("source", {})
                    if src.get("type") == "base64":
                        image_parts.append((src["data"], src.get("media_type", "image/png")))

            for b64_data, media_type in image_parts:
                if is_latest:
                    log.info("screen_context: analyzing latest screenshot for MiniMax fallback...")
                    try:
                        ctx = build_context_from_b64(b64_data, media_type)
                        text_parts.insert(0, ctx)
                    except Exception as e:
                        log.warning(f"screen_context failed: {e}")
                        text_parts.insert(0, "[Screenshot analysis failed]")
                else:
                    text_parts.insert(0, "[previous screenshot — omitted]")

            parts.append(f"[{role}]: " + "\n\n".join(text_parts))

        text = "\n\n".join(parts)
        oai_messages = [{"role": "user", "content": text}]
        payload = {
            "model":      model,
            "messages":   oai_messages,
            "max_tokens": max_tokens,
            "stream":     False,
        }
        r = requests.post(CHAT_URL, headers=self._headers(), json=payload, timeout=90)
        d = r.json()
        return d["choices"][0]["message"]["content"]

    # ── Entry point ────────────────────────────────────────────────────────

    def _create(self, *, model: str, max_tokens: int, system: str, messages: list) -> _FakeMessage:
        model = model or self.model

        try:
            log.info(f"MiniMax: calling {CHAT_URL} model={model} (vision)")
            text = self._create_via_completions(model, max_tokens, system, messages)
            log.info("MiniMax: vision OK")
            return _FakeMessage(text)
        except Exception as e:
            log.warning(f"MiniMax: vision failed ({e}) — falling back to screen_context")

        log.info("MiniMax: using screen_context fallback")
        text = self._create_via_screen_context(model, max_tokens, system, messages)
        log.info("MiniMax: screen_context fallback OK")
        return _FakeMessage(text)
