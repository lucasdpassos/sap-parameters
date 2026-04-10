"""
codemie_client.py — Cliente para a API Codemie (EPAM) como substituto do anthropic SDK.

Suporta duas estratégias:
  1. /chat/completions  (OpenAI-compatible, vision nativa) — preferida
  2. /assistants/{id}/model  (assistant agentic) — fallback sem vision direta

Uso drop-in no agent.py via CodemieClient.messages.create(...)
"""

import os
import time
import logging
import base64
import requests

from screen_context import build_context_from_b64

log = logging.getLogger("sap-agent.codemie")

# ── Configuração ────────────────────────────────────────────────────────────
AUTH_URL      = "https://auth.codemie.lab.epam.com/realms/codemie-prod/protocol/openid-connect/token"
BASE_URL      = "https://codemie.lab.epam.com/code-assistant-api/v1"
ASSISTANT_ID  = "0a04ddd7-b499-4b7a-b4e5-c497fb6d639a"
CLIENT_ID     = "codemie-sdk"
DEFAULT_MODEL = "claude-sonnet-4-6"    # modelo com vision na Codemie


class _FakeContent:
    """Imita anthropic.types.ContentBlock para compatibilidade com agent.py."""
    def __init__(self, text: str):
        self.text = text


class _FakeMessage:
    """Imita anthropic.types.Message."""
    def __init__(self, text: str):
        self.content = [_FakeContent(text)]


class _MessagesAPI:
    def __init__(self, client: "CodemieClient"):
        self._c = client

    def create(self, *, model: str, max_tokens: int, system: str, messages: list) -> _FakeMessage:
        return self._c._create(model=model, max_tokens=max_tokens, system=system, messages=messages)


class CodemieClient:
    """
    Drop-in para anthropic.Anthropic.
    Uso: client = CodemieClient(username=..., password=...)
         client.messages.create(model=..., max_tokens=..., system=..., messages=[...])
    """

    def __init__(self, username: str, password: str, model: str = DEFAULT_MODEL):
        self.username = username
        self.password = password
        self.model    = model
        self._token: str = ""
        self._token_exp: float = 0.0
        self.messages = _MessagesAPI(self)

    # ── Auth ────────────────────────────────────────────────────────────────

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_exp - 30:
            return self._token
        log.info("Codemie: refreshing auth token...")
        r = requests.post(AUTH_URL, data={
            "grant_type": "password",
            "client_id":  CLIENT_ID,
            "username":   self.username,
            "password":   self.password,
        }, timeout=15)
        r.raise_for_status()
        d = r.json()
        self._token     = d["access_token"]
        self._token_exp = time.time() + d.get("expires_in", 300)
        log.info(f"Codemie: token valid for {d.get('expires_in',300)}s")
        return self._token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type":  "application/json",
        }

    # ── Message conversion ──────────────────────────────────────────────────

    @staticmethod
    def _to_openai_messages(system: str, messages: list) -> list:
        """
        Converte o formato Anthropic SDK para o formato OpenAI chat/completions.
        Suporta content blocks com imagens base64.
        """
        result = []
        if system:
            result.append({"role": "system", "content": system})

        for msg in messages:
            role    = msg["role"]
            content = msg["content"]

            # content pode ser string simples
            if isinstance(content, str):
                result.append({"role": role, "content": content})
                continue

            # content é lista de blocks (Anthropic SDK style)
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

    # ── API calls ───────────────────────────────────────────────────────────

    def _create_via_completions(self, model: str, max_tokens: int,
                                 system: str, messages: list) -> str:
        """Usa /chat/completions (OpenAI-compatible) — suporta vision."""
        oai_messages = self._to_openai_messages(system, messages)
        payload = {
            "model":      model,
            "messages":   oai_messages,
            "max_tokens": max_tokens,
            "stream":     False,
        }
        r = requests.post(
            f"{BASE_URL}/chat/completions",
            headers=self._headers(),
            json=payload,
            timeout=60,
        )

        if not r.text.strip():
            raise RuntimeError("budget_exceeded: empty response from /chat/completions")

        try:
            d = r.json()
        except Exception:
            raise RuntimeError(f"budget_exceeded: non-JSON response ({r.status_code}): {r.text[:100]}")

        if r.status_code != 200:
            err = d.get("error", {})
            msg = str(err)
            if "budget" in msg.lower() or "ExceededBudget" in msg:
                raise RuntimeError(f"budget_exceeded: {msg}")
            raise RuntimeError(f"Codemie /chat/completions error {r.status_code}: {err}")

        return d["choices"][0]["message"]["content"]

    def _create_via_assistant(self, model: str, max_tokens: int,
                               system: str, messages: list) -> str:
        """
        Usa /assistants/{id}/model — sem vision direta.
        Substitui imagens por contexto textual rico gerado por
        screen_context (OCR + OpenCV + xdotool).
        """
        # Identificar qual é a última mensagem do usuário (screenshot mais recente)
        last_user_idx = max(
            (i for i, m in enumerate(messages) if m["role"] == "user"),
            default=-1
        )

        parts = [f"[SYSTEM]: {system}"] if system else []

        for msg_idx, msg in enumerate(messages):
            role    = msg["role"].upper()
            content = msg["content"]
            is_latest_user = (msg_idx == last_user_idx)

            if isinstance(content, str):
                parts.append(f"[{role}]: {content}")
                continue

            text_parts  = []
            image_parts = []
            for block in content:
                btype = block.get("type", "text")
                if btype == "text":
                    text_parts.append(block["text"])
                elif btype == "image":
                    src = block.get("source", {})
                    if src.get("type") == "base64":
                        image_parts.append((src["data"], src.get("media_type", "image/png")))

            for b64_data, media_type in image_parts:
                if is_latest_user:
                    # Analisar o screenshot mais recente com OCR + OpenCV
                    log.info("screen_context: analyzing latest screenshot...")
                    try:
                        ctx = build_context_from_b64(b64_data, media_type)
                        text_parts.insert(0, ctx)
                        log.info("screen_context: context built successfully")
                    except Exception as e:
                        log.warning(f"screen_context: failed ({e})")
                        text_parts.insert(0, "[Screenshot analysis failed]")
                else:
                    # Histórico: omitir imagens antigas para economizar tokens
                    text_parts.insert(0, "[previous screenshot — omitted from history]")

            combined = "\n\n".join(text_parts)
            parts.append(f"[{role}]: {combined}")

        text = "\n\n".join(parts)
        payload = {
            "text":     text,
            "llmModel": model,
            "stream":   False,
        }
        r = requests.post(
            f"{BASE_URL}/assistants/{ASSISTANT_ID}/model",
            headers=self._headers(),
            json=payload,
            timeout=60,
        )
        d = r.json()

        if not d.get("success"):
            raise RuntimeError(f"Codemie assistant error: {d.get('agentError') or d}")

        return d.get("generated", "")

    def _create(self, *, model: str, max_tokens: int, system: str, messages: list) -> _FakeMessage:
        model = model or self.model

        # Tentar /chat/completions primeiro (vision)
        try:
            log.info(f"Codemie: calling /chat/completions model={model}")
            text = self._create_via_completions(model, max_tokens, system, messages)
            log.info("Codemie: /chat/completions OK")
            return _FakeMessage(text)
        except RuntimeError as e:
            if "budget_exceeded" in str(e).lower() or "ExceededBudget" in str(e):
                log.warning(f"Codemie: budget exceeded on /chat/completions — falling back to assistant endpoint (no vision)")
            else:
                log.warning(f"Codemie: /chat/completions failed ({e}) — falling back to assistant endpoint")

        # Fallback: assistant endpoint com screen_context (OCR + OpenCV)
        log.warning("Codemie: /chat/completions budget exceeded — using assistant + screen_context fallback")
        log.info("Codemie: screenshots will be analyzed via OCR+OpenCV and injected as text context")
        log.info(f"Codemie: calling /assistants/{ASSISTANT_ID}/model model={model}")
        text = self._create_via_assistant(model, max_tokens, system, messages)
        log.info("Codemie: assistant endpoint OK")
        return _FakeMessage(text)
