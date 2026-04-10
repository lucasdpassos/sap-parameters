"""
actions.py — Funções de screenshot, clique, digitação e teclado via xdotool/scrot
"""
import subprocess
import time
import os
import re
import base64
import logging
from datetime import datetime

log = logging.getLogger("sap-agent.actions")

DISPLAY = os.environ.get("DISPLAY", ":1")


def screenshot(path: str | None = None) -> str:
    """Captura screenshot da tela e retorna o caminho do arquivo."""
    if path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = f"/tmp/sap_screen_{ts}.png"
    result = subprocess.run(
        ["scrot", "-z", path],
        env={**os.environ, "DISPLAY": DISPLAY},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        result = subprocess.run(
            ["import", "-window", "root", path],
            env={**os.environ, "DISPLAY": DISPLAY},
            capture_output=True,
            text=True,
        )
    if not os.path.exists(path):
        raise RuntimeError(f"Screenshot falhou: {result.stderr}")
    return path


def screenshot_b64(path: str | None = None) -> tuple[str, str]:
    """Captura screenshot e retorna (caminho, base64)."""
    p = screenshot(path)
    with open(p, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode()
    return p, data


def click(x: int, y: int, button: int = 1, double: bool = False):
    """Clica na posição (x, y). button=1 esquerdo, 3 direito."""
    env = {**os.environ, "DISPLAY": DISPLAY}
    subprocess.run(["xdotool", "mousemove", str(x), str(y)], env=env, check=True)
    time.sleep(0.2)
    if double:
        subprocess.run(["xdotool", "click", "--repeat", "2", "--delay", "100", str(button)], env=env, check=True)
    else:
        subprocess.run(["xdotool", "click", str(button)], env=env, check=True)


def type_text(text: str, delay_ms: int = 50):
    """Digita texto usando xdotool type."""
    env = {**os.environ, "DISPLAY": DISPLAY}
    subprocess.run(
        ["xdotool", "type", "--clearmodifiers", f"--delay={delay_ms}", "--", text],
        env=env,
        check=True,
    )


def key_press(key: str):
    """Pressiona uma tecla (ex: Return, Tab, Escape, ctrl+s)."""
    env = {**os.environ, "DISPLAY": DISPLAY}
    subprocess.run(["xdotool", "key", "--clearmodifiers", key], env=env, check=True)


def wait(seconds: float = 1.5):
    """Aguarda N segundos."""
    time.sleep(seconds)


def get_screen_size() -> tuple[int, int]:
    """Retorna (width, height) da tela."""
    env = {**os.environ, "DISPLAY": DISPLAY}
    r = subprocess.run(["xdotool", "getdisplaygeometry"], env=env, capture_output=True, text=True)
    w, h = r.stdout.strip().split()
    return int(w), int(h)


# ──────────────────────────────────────────────────────────────────
# Navegação de menus via teclado
# ──────────────────────────────────────────────────────────────────

def keyboard_navigate_to(target: str, max_items: int = 12,
                          wait_s: float = 0.35) -> bool:
    """
    Navega por um menu/dropdown aberto usando ↓ até encontrar o item alvo.

    Estratégia:
      1. Pressiona ↓ para destacar o primeiro item.
      2. Tira screenshot e roda OCR na região destacada (colorida).
      3. Se o texto destacado bate com `target`, pressiona Enter e retorna True.
      4. Repete até `max_items` vezes. Se não encontrar, Escape + retorna False.

    `target` é comparado em lowercase sem espaços extras (fuzzy match).
    """
    from screen_context import ocr_highlighted_item

    target_clean = _clean(target)
    log.info(f"keyboard_navigate_to: procurando '{target}' (max {max_items} itens)")

    for i in range(1, max_items + 1):
        key_press("Down")
        time.sleep(wait_s)

        img_path = screenshot()
        highlighted = ocr_highlighted_item(img_path)

        if highlighted:
            log.info(f"  ↓ item {i}: '{highlighted}'")
            if _fuzzy_match(target_clean, _clean(highlighted)):
                log.info(f"  ✓ Encontrado! Pressionando Enter.")
                key_press("Return")
                time.sleep(0.5)
                return True
        else:
            log.debug(f"  ↓ item {i}: (sem highlight detectado)")

    log.warning(f"  ✗ '{target}' não encontrado em {max_items} itens. Fechando menu.")
    key_press("Escape")
    return False


def _clean(text: str) -> str:
    """Normaliza texto para comparação fuzzy."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _fuzzy_match(a: str, b: str) -> bool:
    """True se `a` está contido em `b` ou vice-versa (substring match)."""
    return a in b or b in a or _lcs_ratio(a, b) > 0.7


def _lcs_ratio(a: str, b: str) -> float:
    """Proporção do LCS em relação ao menor string."""
    if not a or not b:
        return 0.0
    m, n = len(a), len(b)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            dp[i][j] = dp[i-1][j-1] + 1 if a[i-1] == b[j-1] else max(dp[i-1][j], dp[i][j-1])
    return dp[m][n] / min(m, n)
