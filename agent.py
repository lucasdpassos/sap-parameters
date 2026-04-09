"""
agent.py — Loop principal: percepção → decisão (Claude Vision) → ação
"""
import os
import sys
import json
import time
import logging

import anthropic

from parser import load_instructions
from actions import screenshot_b64, click, type_text, key_press, wait

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("sap-agent")

# ──────────────────────────────────────────────
# Configuração
# ──────────────────────────────────────────────
MAX_ACTIONS_PER_STEP = 15   # quantas ações sequenciais para completar 1 step
MAX_STUCK_RETRIES   = 3     # quantas vezes pode repetir a MESMA ação sem progresso antes de desistir
WAIT_AFTER_ACTION   = 1.0   # segundos de espera padrão após cada ação
MODEL = "claude-opus-4-6"

# Ações que tipicamente precisam de mais tempo para o SAP responder
SLOW_ACTIONS = {"click"}     # menus e janelas levam ~1s extra na primeira vez

SYSTEM_PROMPT = """You are an automation agent for SAP GUI for Java running on Linux.
You receive screenshots of the screen and must decide the next action to complete the requested step.

IMPORTANT RULES:
- A step may require MULTIPLE sequential actions (e.g. click menu → click item → click field → type text).
  Each response is ONE action. You will keep receiving new screenshots after each action.
- Only set step_complete=true when the step goal is FULLY achieved (e.g. text actually typed AND visible on screen).
- If you just clicked to focus a field and haven't typed yet, step_complete must be false.
- Coordinates are absolute pixels on the full screen (not relative to any window).

Always respond in JSON with this exact format:
{
  "observation": "What you see on screen right now",
  "step_complete": false,
  "action": {
    "type": "click|type|key|wait|none",
    "x": 100,
    "y": 200,
    "text": "text to type",
    "key": "Return",
    "seconds": 1.0,
    "double_click": false,
    "wait_after": 1.0
  },
  "reasoning": "Why this action and what comes next",
  "confidence": 0.9,
  "progress": "short description of what has been done so far toward the goal"
}

Action types:
- click: click at (x, y). Use double_click: true for double-click.
- type: type the text in "text" field (field must already be focused)
- key: press key named in "key" (e.g. Return, Tab, Escape, ctrl+s, alt+F10)
- wait: wait "seconds" seconds (use only when animation/loading expected)
- none: nothing to do (step is already complete — set step_complete=true)

The "wait_after" field overrides the default wait after this action (in seconds).
Use lower values (0.3-0.5) for fast UI responses, higher (1.5-2.0) for dialogs/windows opening.

If an element is not visible, set confidence < 0.5 and explain in observation."""


def ask_claude(client: anthropic.Anthropic, step: str, img_b64: str, history: list) -> dict:
    """Sends screenshot + step to Claude and returns the decision."""
    messages = history + [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": img_b64,
                    },
                },
                {
                    "type": "text",
                    "text": (
                        f"Step goal: {step}\n\n"
                        "Analyze the current screen state and decide the NEXT single action to make progress toward the goal. "
                        "Remember: only set step_complete=true when the goal is FULLY done. Respond in JSON."
                    ),
                },
            ],
        }
    ]

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=messages,
    )

    raw = response.content[0].text.strip()
    # Extract JSON even if wrapped in ```json ... ```
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.warning(f"Non-JSON response: {raw[:200]}")
        return {
            "observation": raw, "step_complete": False,
            "action": {"type": "none"}, "reasoning": "parse error", "confidence": 0,
            "progress": "parse error"
        }


def execute_action(decision: dict) -> bool:
    """Executes the action decided by Claude. Returns True if step_complete."""
    if decision.get("step_complete"):
        return True

    action = decision.get("action", {})
    atype = action.get("type", "none")
    wait_after = float(action.get("wait_after", WAIT_AFTER_ACTION))

    if atype == "click":
        x, y = int(action["x"]), int(action["y"])
        double = action.get("double_click", False)
        log.info(f"  → CLICK {'double ' if double else ''}at ({x}, {y})  [wait {wait_after}s]")
        click(x, y, double=double)

    elif atype == "type":
        text = action.get("text", "")
        log.info(f"  → TYPE '{text}'  [wait {wait_after}s]")
        type_text(text)

    elif atype == "key":
        key = action.get("key", "Return")
        log.info(f"  → KEY '{key}'  [wait {wait_after}s]")
        key_press(key)

    elif atype == "wait":
        secs = float(action.get("seconds", 1.0))
        log.info(f"  → WAIT {secs}s (explicit)")
        wait(secs)
        return False  # skip the wait_after below since we already waited

    elif atype == "none":
        log.info("  → NO ACTION")

    wait(wait_after)
    return False


def _action_signature(decision: dict) -> str:
    """Returns a string that identifies the action (to detect being stuck)."""
    a = decision.get("action", {})
    t = a.get("type", "none")
    if t == "click":
        return f"click:{a.get('x')},{a.get('y')}"
    if t == "type":
        return f"type:{a.get('text', '')[:30]}"
    if t == "key":
        return f"key:{a.get('key', '')}"
    return t


def run_step(client: anthropic.Anthropic, step: str, step_num: int) -> bool:
    """
    Runs a step with up to MAX_ACTIONS_PER_STEP sequential actions.
    Aborts early if stuck repeating the same action MAX_STUCK_RETRIES times.
    Returns True if completed, False if failed.
    """
    log.info(f"\n{'='*60}")
    log.info(f"STEP {step_num}: {step}")
    log.info(f"{'='*60}")

    history = []
    last_action_sig = None
    stuck_count = 0

    for action_num in range(1, MAX_ACTIONS_PER_STEP + 1):
        log.info(f"  [Action {action_num}/{MAX_ACTIONS_PER_STEP}]")

        # 1. Screenshot
        screen_path, img_b64 = screenshot_b64()
        log.info(f"  → Screenshot: {screen_path}")

        # 2. Ask Claude
        log.info("  → Consulting Claude Vision...")
        decision = ask_claude(client, step, img_b64, history)

        obs       = decision.get("observation", "")
        reasoning = decision.get("reasoning", "")
        progress  = decision.get("progress", "")
        confidence = decision.get("confidence", 1.0)

        log.info(f"  Observation : {obs[:120]}")
        log.info(f"  Progress    : {progress[:100]}")
        log.info(f"  Reasoning   : {reasoning[:120]}")
        log.info(f"  Confidence  : {confidence:.0%}")

        if confidence < 0.5:
            log.warning(f"  ⚠ Low confidence ({confidence:.0%}) — target element may not be visible")

        # 3. Detect stuck (same action repeated)
        sig = _action_signature(decision)
        if sig == last_action_sig and sig not in ("none", "wait"):
            stuck_count += 1
            log.warning(f"  ⚠ Same action repeated ({stuck_count}/{MAX_STUCK_RETRIES}): {sig}")
            if stuck_count >= MAX_STUCK_RETRIES:
                log.error(f"  ✗ Stuck repeating '{sig}' — aborting step {step_num}")
                return False
        else:
            stuck_count = 0
        last_action_sig = sig

        # 4. Execute action
        completed = execute_action(decision)
        if completed:
            log.info(f"  ✓ Step {step_num} COMPLETE!")
            return True

        # 5. Append to history (keep last 6 turns to limit token usage)
        history.append({
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                {"type": "text", "text": f"Step goal: {step}"},
            ]
        })
        history.append({
            "role": "assistant",
            "content": json.dumps(decision, ensure_ascii=False),
        })
        if len(history) > 12:  # keep last 6 turns (12 messages)
            history = history[-12:]

    log.error(f"  ✗ Step {step_num} FAILED — reached max {MAX_ACTIONS_PER_STEP} actions without completing")
    return False


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log.error("ANTHROPIC_API_KEY not set. Export the variable before running.")
        sys.exit(1)

    if len(sys.argv) < 2:
        print("Usage: python agent.py 'instruction or path/to/file.pdf'")
        sys.exit(1)

    instruction_source = sys.argv[1]
    log.info(f"Loading instructions from: {instruction_source[:80]}")

    steps = load_instructions(instruction_source)
    log.info(f"Steps identified: {len(steps)}")
    for i, s in enumerate(steps, 1):
        log.info(f"  {i}. {s}")

    client = anthropic.Anthropic(api_key=api_key)

    # Initial screenshot to confirm SAP is visible
    log.info("\n--- Initial screenshot ---")
    try:
        path, _ = screenshot_b64()
        log.info(f"Saved: {path}")
    except Exception as e:
        log.error(f"Screenshot failed: {e}")
        sys.exit(1)

    # Execute all steps
    results = []
    for i, step in enumerate(steps, 1):
        ok = run_step(client, step, i)
        results.append((i, step, ok))
        if not ok:
            log.error(f"\nAborting — Step {i} failed.")
            break
        wait(0.8)  # brief pause between steps

    # Final report
    log.info(f"\n{'='*60}")
    log.info("FINAL REPORT")
    log.info(f"{'='*60}")
    for i, step, ok in results:
        status = "✓ OK" if ok else "✗ FAILED"
        log.info(f"  Step {i}: {status} — {step[:70]}")

    if any(not r[2] for r in results):
        sys.exit(1)
    else:
        log.info("\nAll steps completed successfully!")


if __name__ == "__main__":
    main()
