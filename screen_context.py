"""
screen_context.py — Analisa a tela com múltiplas bibliotecas auxiliares e
retorna um "mapa textual" rico para o LLM quando vision direta não está disponível.

Camadas (em ordem de prioridade):
  1. Janelas ativas via wmctrl/xdotool  → título, posição, z-order
  2. OCR (pytesseract + OpenCV preprocess) → texto visível com coordenadas
  3. Detecção de regiões UI (OpenCV contours) → botões, campos, menus
  4. Síntese → correlaciona texto com regiões, infere tipo de elemento
"""

import os
import re
import base64
import logging
import subprocess
import tempfile
from typing import Optional

log = logging.getLogger("sap-agent.ctx")

DISPLAY = os.environ.get("DISPLAY", ":1")
_ENV    = {**os.environ, "DISPLAY": DISPLAY}


# ══════════════════════════════════════════════════════════════════════
# CAMADA 1 — Janelas ativas (wmctrl / xdotool)
# ══════════════════════════════════════════════════════════════════════

def _get_window_info() -> dict:
    """Retorna info sobre a janela ativa e todas as janelas abertas."""
    result = {"active": None, "all": [], "screen_size": (1280, 800)}

    # Tamanho da tela
    try:
        r = subprocess.run(["xdotool", "getdisplaygeometry"], capture_output=True, text=True, env=_ENV)
        w, h = r.stdout.strip().split()
        result["screen_size"] = (int(w), int(h))
    except Exception:
        pass

    # Janela ativa
    try:
        wid = subprocess.run(["xdotool", "getactivewindow"], capture_output=True, text=True, env=_ENV).stdout.strip()
        name = subprocess.run(["xdotool", "getwindowname", wid], capture_output=True, text=True, env=_ENV).stdout.strip()
        geo  = subprocess.run(["xdotool", "getwindowgeometry", "--shell", wid],
                              capture_output=True, text=True, env=_ENV).stdout
        props = dict(line.split("=", 1) for line in geo.strip().splitlines() if "=" in line)
        result["active"] = {
            "id": wid, "title": name,
            "x": int(props.get("X", 0)), "y": int(props.get("Y", 0)),
            "w": int(props.get("WIDTH", 0)), "h": int(props.get("HEIGHT", 0)),
        }
    except Exception:
        pass

    # Todas as janelas
    try:
        lines = subprocess.run(["wmctrl", "-l"], capture_output=True, text=True, env=_ENV).stdout.strip().splitlines()
        for line in lines:
            parts = line.split(None, 3)
            if len(parts) >= 4:
                result["all"].append({"id": parts[0], "title": parts[3]})
    except Exception:
        pass

    # Posição do mouse
    try:
        r = subprocess.run(["xdotool", "getmouselocation", "--shell"],
                           capture_output=True, text=True, env=_ENV)
        mp = dict(line.split("=", 1) for line in r.stdout.strip().splitlines() if "=" in line)
        result["mouse"] = (int(mp.get("X", 0)), int(mp.get("Y", 0)))
    except Exception:
        result["mouse"] = (0, 0)

    return result


# ══════════════════════════════════════════════════════════════════════
# CAMADA 2 — OCR (pytesseract + OpenCV preprocessing)
# ══════════════════════════════════════════════════════════════════════

def _preprocess_for_ocr(img_bgr):
    """Aplica preprocessing para melhorar OCR em UIs."""
    import cv2
    import numpy as np
    # Escala 2x para texto pequeno de UI
    h, w = img_bgr.shape[:2]
    big = cv2.resize(img_bgr, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(big, cv2.COLOR_BGR2GRAY)
    # Sharpen
    kernel = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]])
    sharp = cv2.filter2D(gray, -1, kernel)
    return sharp, 2.0  # retorna imagem e scale factor


def _get_ocr_elements(img_bgr) -> list[dict]:
    """
    Executa OCR e retorna lista de elementos com texto e posição.
    Agrupa palavras em linhas por proximidade vertical.
    """
    try:
        import pytesseract
        from PIL import Image
        import cv2
        import numpy as np
    except ImportError as e:
        log.warning(f"OCR library missing: {e}")
        return []

    preprocessed, scale = _preprocess_for_ocr(img_bgr)
    pil_img = Image.fromarray(preprocessed)

    try:
        data = pytesseract.image_to_data(
            pil_img,
            lang="eng",
            config="--psm 11 --oem 3",
            output_type=pytesseract.Output.DICT,
        )
    except Exception as e:
        log.warning(f"OCR failed: {e}")
        return []

    # Coletar palavras com confiança suficiente
    words = []
    n = len(data["text"])
    for i in range(n):
        txt = data["text"][i].strip()
        conf = int(data["conf"][i])
        if txt and conf > 25:
            words.append({
                "text": txt,
                "x":  int(data["left"][i]   / scale),
                "y":  int(data["top"][i]    / scale),
                "w":  int(data["width"][i]  / scale),
                "h":  int(data["height"][i] / scale),
                "conf": conf,
            })

    if not words:
        return []

    # Agrupar em linhas (palavras com y similar dentro de ±10px)
    words.sort(key=lambda w: (w["y"], w["x"]))
    lines = []
    current_line = [words[0]]
    for w in words[1:]:
        if abs(w["y"] - current_line[-1]["y"]) <= 12:
            current_line.append(w)
        else:
            lines.append(current_line)
            current_line = [w]
    lines.append(current_line)

    elements = []
    for line in lines:
        line.sort(key=lambda w: w["x"])
        text = " ".join(w["text"] for w in line)
        x0 = min(w["x"] for w in line)
        y0 = min(w["y"] for w in line)
        x1 = max(w["x"] + w["w"] for w in line)
        y1 = max(w["y"] + w["h"] for w in line)
        avg_conf = sum(w["conf"] for w in line) // len(line)
        cx = (x0 + x1) // 2
        cy = (y0 + y1) // 2
        elements.append({
            "text": text, "x": x0, "y": y0,
            "x1": x1, "y1": y1, "cx": cx, "cy": cy,
            "conf": avg_conf,
        })

    return elements


# ══════════════════════════════════════════════════════════════════════
# CAMADA 3 — Detecção de regiões UI (OpenCV)
# ══════════════════════════════════════════════════════════════════════

def _classify_region(x, y, w, h, sw, sh) -> str:
    """Classifica uma região detectada por posição e proporção."""
    aspect = w / h if h > 0 else 0
    area_ratio = (w * h) / (sw * sh)

    # Menu bar: faixa horizontal estreita no topo
    if y < sh * 0.1 and aspect > 8 and h < 35:
        return "MENU_BAR"
    # Toolbar: faixa horizontal um pouco mais abaixo
    if sh * 0.05 < y < sh * 0.15 and aspect > 5 and h < 40:
        return "TOOLBAR"
    # Botão: pequeno retângulo com texto
    if 40 < w < 250 and 15 < h < 45 and 1.5 < aspect < 12:
        return "BUTTON"
    # Campo de texto: retângulo maior, mais largo que alto
    if w > 80 and h > 15 and aspect > 3:
        return "TEXT_FIELD"
    # Área de texto/editor: grande retângulo
    if area_ratio > 0.02 and aspect < 5:
        return "TEXT_AREA"
    # Painel/container
    return "PANEL"


def _get_ui_regions(img_bgr, ocr_elements: list[dict]) -> list[dict]:
    """
    Detecta regiões clicáveis via contornos OpenCV.
    Anota cada região com texto OCR próximo ao seu centro.
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        return []

    h, w = img_bgr.shape[:2]
    gray  = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)

    # Detectar bordas e contornos
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    edges   = cv2.Canny(blurred, 30, 100)
    kernel  = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    dilated = cv2.dilate(edges, kernel, iterations=1)

    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    regions = []
    seen = set()

    for cnt in contours:
        rx, ry, rw, rh = cv2.boundingRect(cnt)
        area = rw * rh
        # Filtrar por tamanho mínimo/máximo razoável
        if rw < 15 or rh < 8 or area < 150 or area > w * h * 0.6:
            continue
        # Deduplicar regiões muito parecidas
        key = (rx // 5, ry // 5, rw // 5, rh // 5)
        if key in seen:
            continue
        seen.add(key)

        rtype = _classify_region(rx, ry, rw, rh, w, h)
        cx, cy = rx + rw // 2, ry + rh // 2

        # Encontrar texto OCR mais próximo do centro da região
        label = ""
        min_dist = float("inf")
        for el in ocr_elements:
            dist = abs(el["cx"] - cx) + abs(el["cy"] - cy)
            # Texto deve estar dentro ou muito próximo da região
            in_region = (rx <= el["cx"] <= rx + rw) and (ry - 5 <= el["cy"] <= ry + rh + 5)
            if in_region and dist < min_dist:
                min_dist = dist
                label = el["text"]

        regions.append({
            "type": rtype, "x": rx, "y": ry, "w": rw, "h": rh,
            "cx": cx, "cy": cy, "label": label,
        })

    # Ordenar por área decrescente e limitar
    regions.sort(key=lambda r: r["w"] * r["h"], reverse=True)
    return regions[:40]


# ══════════════════════════════════════════════════════════════════════
# CAMADA 4 — Síntese e formatação do contexto
# ══════════════════════════════════════════════════════════════════════

def _get_word_level_ocr(img_bgr) -> list[dict]:
    """OCR nível palavra (não agrupado em linhas) para detecção de menu items."""
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return []

    preprocessed, scale = _preprocess_for_ocr(img_bgr)
    pil_img = Image.fromarray(preprocessed)
    try:
        data = pytesseract.image_to_data(
            pil_img, lang="eng", config="--psm 11 --oem 3",
            output_type=pytesseract.Output.DICT,
        )
    except Exception:
        return []

    words = []
    for i in range(len(data["text"])):
        txt  = data["text"][i].strip()
        conf = int(data["conf"][i])
        if txt and conf > 30:
            words.append({
                "text": txt,
                "x":  int(data["left"][i]   / scale),
                "y":  int(data["top"][i]    / scale),
                "w":  int(data["width"][i]  / scale),
                "h":  int(data["height"][i] / scale),
                "cx": int((data["left"][i]  + data["width"][i]  / 2) / scale),
                "cy": int((data["top"][i]   + data["height"][i] / 2) / scale),
                "conf": conf,
            })
    return words


def _infer_menu_bar(img_bgr, win_info: dict) -> list[dict]:
    """
    Identifica itens individuais do menu bar usando OCR nível palavra.
    Procura palavras na faixa y=20..90 com altura típica de menu (8..25px).
    """
    words = _get_word_level_ocr(img_bgr)
    menu_items = []
    for w in words:
        if 20 <= w["y"] <= 90 and 8 <= w["h"] <= 28 and w["conf"] >= 35:
            # Filtrar ruído: só palavras alfabéticas ou comuns de menu
            clean = re.sub(r"[^a-zA-Z0-9&_\-]", "", w["text"])
            if len(clean) >= 2:
                menu_items.append({"label": w["text"], "cx": w["cx"], "cy": w["cy"]})
    # Deduplicar por faixa x (±20px)
    menu_items.sort(key=lambda m: m["cx"])
    unique = []
    last_cx = -999
    for m in menu_items:
        if m["cx"] - last_cx > 20:
            unique.append(m)
            last_cx = m["cx"]
    return unique


def build_context(screenshot_path: str) -> str:
    """
    Ponto de entrada principal. Analisa o screenshot e retorna
    um mapa textual estruturado da tela para uso em prompts LLM.
    """
    lines = ["=== SCREEN CONTEXT (multi-library analysis) ===\n"]

    # ── Carregar imagem ──────────────────────────────────────────────
    try:
        import cv2
        img = cv2.imread(screenshot_path)
        if img is None:
            raise ValueError("cv2.imread returned None")
        img_h, img_w = img.shape[:2]
    except Exception as e:
        lines.append(f"[Image load error: {e}]")
        return "\n".join(lines)

    # ── Camada 1: Janelas ───────────────────────────────────────────
    win_info = _get_window_info()
    sw, sh   = win_info["screen_size"]
    mx, my   = win_info.get("mouse", (0, 0))
    lines.append(f"SCREEN: {sw}x{sh}  |  MOUSE: ({mx},{my})")

    active = win_info.get("active")
    if active:
        lines.append(
            f"ACTIVE WINDOW: \"{active['title']}\""
            f"  pos=({active['x']},{active['y']})"
            f"  size={active['w']}x{active['h']}"
        )

    other_wins = [w["title"] for w in win_info.get("all", []) if w["title"] not in ("", "xfce4-panel")]
    if other_wins:
        lines.append(f"OPEN WINDOWS: {' | '.join(other_wins[:6])}")

    lines.append("")

    # ── Camada 2: OCR ───────────────────────────────────────────────
    ocr_elements = _get_ocr_elements(img)

    # Menu bar (usa OCR nível palavra para melhor precisão)
    menu_items = _infer_menu_bar(img, win_info)
    if menu_items:
        parts = [f"\"{m['label']}\" →click({m['cx']},{m['cy']})" for m in menu_items]
        lines.append("MENU BAR:")
        lines.append("  " + "   ".join(parts))
        lines.append("")

    # Texto agrupado por faixa vertical
    if ocr_elements:
        lines.append("VISIBLE TEXT (with click coordinates):")
        # Agrupar em blocos verticais a cada ~40px
        buckets: dict[int, list] = {}
        for el in ocr_elements:
            bucket = (el["y"] // 40) * 40
            buckets.setdefault(bucket, []).append(el)
        for bucket_y in sorted(buckets):
            group = sorted(buckets[bucket_y], key=lambda e: e["x"])
            for el in group:
                conf_tag = "" if el["conf"] >= 60 else f" [conf={el['conf']}%]"
                lines.append(
                    f"  \"{el['text']}\""
                    f"  at ({el['x']},{el['y']})"
                    f"  click→({el['cx']},{el['cy']}){conf_tag}"
                )
        lines.append("")

    # ── Camada 3: UI Regions ────────────────────────────────────────
    regions = _get_ui_regions(img, ocr_elements)
    if regions:
        lines.append("DETECTED UI ELEMENTS:")
        # Agrupar por tipo
        by_type: dict[str, list] = {}
        for r in regions:
            by_type.setdefault(r["type"], []).append(r)

        priority = ["MENU_BAR", "TOOLBAR", "BUTTON", "TEXT_FIELD", "TEXT_AREA", "PANEL"]
        for rtype in priority:
            group = by_type.get(rtype, [])
            if not group:
                continue
            lines.append(f"  {rtype}s:")
            for r in group[:8]:  # max 8 por tipo
                label_part = f" \"{r['label']}\"" if r["label"] else ""
                lines.append(
                    f"    {r['x']},{r['y']} {r['w']}x{r['h']}"
                    f"{label_part}"
                    f"  →click({r['cx']},{r['cy']})"
                )
        lines.append("")

    # ── Dica de uso ─────────────────────────────────────────────────
    lines.append(
        "NOTE: This context was generated from screenshot analysis (OCR + UI detection).\n"
        "Use the click coordinates above to interact with elements.\n"
        "Coordinates are absolute screen pixels (DISPLAY=:1)."
    )

    return "\n".join(lines)


def build_context_from_b64(b64_data: str, media_type: str = "image/png") -> str:
    """Wrapper que salva o b64 em arquivo temporário e chama build_context."""
    suffix = ".png" if "png" in media_type else ".jpg"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(base64.b64decode(b64_data))
        tmp_path = f.name
    try:
        return build_context(tmp_path)
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


# ── CLI rápido para testar ───────────────────────────────────────────
if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/sap_ctx_test.png"
    print(build_context(path))
