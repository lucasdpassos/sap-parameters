"""
actions.py — Funções de screenshot, clique, digitação e teclado via xdotool/scrot
"""
import subprocess
import time
import os
import base64
import tempfile
from datetime import datetime

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
        # fallback: import do ImageMagick
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
