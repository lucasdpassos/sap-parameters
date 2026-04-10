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

def _preprocess_for_ocr(img_bgr, scale: int = 3):
    """
    Aplica preprocessing para melhorar OCR em UIs.
    scale=3 captura texto de 7-8px (dropdowns SAP) sem perder texto maior.
    """
    import cv2
    import numpy as np
    h, w = img_bgr.shape[:2]
    big  = cv2.resize(img_bgr, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(big, cv2.COLOR_BGR2GRAY)
    # OTSU binarization: melhor que sharpen simples para texto de UI
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return binary, float(scale)


def _detect_dropdown_regions(img_bgr) -> list[tuple]:
    """
    Detecta dropdowns/menus abertos por análise de blobs locais.
    Estratégia: divide a imagem em colunas de 30px e procura faixas
    horizontais com fundo claro E texto escuro local — ignora o fundo
    branco geral da tela.
    Retorna lista de (x, y, w, h).
    """
    import cv2
    import numpy as np

    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    ih, iw = gray.shape

    STEP = 30       # largura de cada coluna de análise
    MIN_TEXT = 3    # mínimo de pixels escuros por coluna para contar como "tem texto"
    MIN_H    = 8    # altura mínima de região
    MAX_H    = 250

    # Para cada "strip" vertical de STEP pixels, encontrar faixas y com texto
    col_segments: dict[int, list] = {}  # col_start → [(y_start, y_end)]

    for x_start in range(0, iw - STEP, STEP):
        strip = gray[:, x_start:x_start + STEP]
        # Por linha: tem fundo claro (>190) e texto escuro (<100)?
        row_has_light = strip.mean(axis=1) > 190
        row_has_dark  = (strip < 100).sum(axis=1) >= MIN_TEXT
        active = row_has_light & row_has_dark

        # Agrupar linhas ativas em segmentos
        in_seg = False
        seg_start = 0
        segs = []
        for y in range(ih):
            if active[y] and not in_seg:
                in_seg = True
                seg_start = y
            elif not active[y] and in_seg:
                in_seg = False
                seg_len = y - seg_start
                if MIN_H <= seg_len <= MAX_H:
                    segs.append((seg_start, y))
        if in_seg:
            seg_len = ih - seg_start
            if MIN_H <= seg_len <= MAX_H:
                segs.append((seg_start, ih))
        if segs:
            col_segments[x_start] = segs

    if not col_segments:
        return []

    # Mergear segmentos de colunas adjacentes que se sobrepõem
    # Resultado: lista de (x0, y0, x1, y1) representando regiões
    merged = []
    for x_start, segs in sorted(col_segments.items()):
        for (sy0, sy1) in segs:
            placed = False
            for m in merged:
                # Sobreposição vertical com região existente
                ov_start = max(m["y0"], sy0)
                ov_end   = min(m["y1"], sy1)
                if ov_end - ov_start >= MIN_H and abs(x_start - m["x1"]) <= STEP * 2:
                    # Expandir região
                    m["x1"] = max(m["x1"], x_start + STEP)
                    m["y0"] = min(m["y0"], sy0)
                    m["y1"] = max(m["y1"], sy1)
                    placed = True
                    break
            if not placed:
                merged.append({"x0": x_start, "y0": sy0, "x1": x_start + STEP, "y1": sy1})

    # Converter para (x, y, w, h) e filtrar ruído
    regions = []
    for m in merged:
        rw = m["x1"] - m["x0"]
        rh = m["y1"] - m["y0"]
        if rw >= 20 and rh >= MIN_H:
            regions.append((m["x0"], m["y0"], rw, rh))

    return regions


def _ocr_small_region(img_bgr, x: int, y: int, w: int, h: int,
                       padding: int = 4) -> list[dict]:
    """
    OCR de alta resolução (4x + OTSU) numa região específica.
    Para regiões largas (w > 400), divide em fatias horizontais de 18px
    e usa --psm 7 (single line) para melhor precisão em itens de menu.
    """
    try:
        import cv2
        import pytesseract
        from PIL import Image
    except ImportError:
        return []

    ih, iw = img_bgr.shape[:2]
    x0 = max(0, x - padding)
    y0 = max(0, y - padding)
    x1 = min(iw, x + w + padding)
    y1 = min(ih, y + h + padding)

    # Regiões largas: escanear em fatias horizontais de 18px (sem recursão)
    if (x1 - x0) > 400:
        all_elements = []
        SLICE_H = 18
        for sy in range(y0, y1, SLICE_H):
            sy_end = min(y1, sy + SLICE_H + 2)
            crop_s = img_bgr[sy:sy_end, x0:x1]
            if crop_s.size == 0:
                continue
            sc = 4
            big_s  = cv2.resize(crop_s, (crop_s.shape[1]*sc, crop_s.shape[0]*sc),
                                 interpolation=cv2.INTER_CUBIC)
            gray_s = cv2.cvtColor(big_s, cv2.COLOR_BGR2GRAY)
            _, bin_s = cv2.threshold(gray_s, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            try:
                data_s = pytesseract.image_to_data(
                    Image.fromarray(bin_s), lang="eng",
                    config="--psm 7 --oem 3",
                    output_type=pytesseract.Output.DICT,
                )
            except Exception:
                continue
            for i in range(len(data_s["text"])):
                txt  = data_s["text"][i].strip()
                conf = int(data_s["conf"][i])
                if txt and conf > 20:
                    ex  = x0 + int(data_s["left"][i]   / sc)
                    ey  = sy  + int(data_s["top"][i]    / sc)
                    ew  = int(data_s["width"][i]  / sc)
                    eh  = int(data_s["height"][i] / sc)
                    all_elements.append({
                        "text": txt, "x": ex, "y": ey,
                        "x1": ex+ew, "y1": ey+eh,
                        "cx": ex + ew//2, "cy": ey + eh//2,
                        "conf": conf, "source": "menu_ocr",
                    })
        return all_elements

    crop = img_bgr[y0:y1, x0:x1]
    if crop.size == 0:
        return []

    scale = 4
    big  = cv2.resize(crop, (crop.shape[1]*scale, crop.shape[0]*scale),
                      interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(big, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # Para fatias finas (menu items), --psm 7 (single line) é mais preciso
    crop_h = y1 - y0
    psm = "7" if crop_h <= 22 else "6"

    try:
        data = pytesseract.image_to_data(
            Image.fromarray(binary), lang="eng",
            config=f"--psm {psm} --oem 3",
            output_type=pytesseract.Output.DICT,
        )
    except Exception:
        return []

    elements = []
    for i in range(len(data["text"])):
        txt  = data["text"][i].strip()
        conf = int(data["conf"][i])
        if txt and conf > 20:
            ex  = x0 + int(data["left"][i]   / scale)
            ey  = y0 + int(data["top"][i]    / scale)
            ew  = int(data["width"][i]  / scale)
            eh  = int(data["height"][i] / scale)
            ecx = ex + ew // 2
            ecy = ey + eh // 2
            elements.append({
                "text": txt, "x": ex, "y": ey, "x1": ex + ew, "y1": ey + eh,
                "cx": ecx, "cy": ecy, "conf": conf, "source": "menu_ocr",
            })
    return elements


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

    # (grid_scan_menu_zone removed — keyboard navigation now handles menu items)

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


def ocr_highlighted_item(screenshot_path: str) -> str | None:
    """
    Detecta o item de menu atualmente destacado (hover/selecionado via ↓↑).

    Estratégia:
      1. Converte para HSV e procura regiões com saturação alta (S > 60)
         ou cor distinta do fundo branco/cinza — típico de seleção SAP.
      2. Se não encontrar cor saturada, busca por inversão de cor (texto
         branco em fundo escuro).
      3. Faz OCR 4x na região destacada encontrada.
      4. Retorna o texto OCR ou None se nada detectado.
    """
    try:
        import cv2
        import numpy as np
        import pytesseract
        from PIL import Image
    except ImportError:
        return None

    img = cv2.imread(screenshot_path)
    if img is None:
        return None

    ih, iw = img.shape[:2]
    hsv   = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

    # ── Máscara 1: saturação alta (azul SAP, verde, etc.) ─────────────
    # H: qualquer, S > 60, V > 60  →  cor não-cinza
    mask_color = cv2.inRange(hsv, (0, 60, 60), (180, 255, 255))

    # ── Máscara 2: texto branco em fundo escuro (inversão de contraste) ──
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    # Região escura com texto claro: média local < 80
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (80, 16))
    local_mean = cv2.blur(gray, (80, 16))
    mask_dark = (local_mean < 100).astype(np.uint8) * 255

    mask = cv2.bitwise_or(mask_color, mask_dark)

    # Limpar ruído
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,  k)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    best = None
    best_area = 0

    for cnt in contours:
        rx, ry, rw, rh = cv2.boundingRect(cnt)
        area = rw * rh
        # Filtro: tamanho plausível para um item de menu (largura > 30, altura 8-40)
        if rw < 30 or rh < 8 or rh > 50 or area < 300:
            continue
        # Preferir regiões na zona de menu (y < ih * 0.4)
        score = area * (1.5 if ry < ih * 0.4 else 1.0)
        if score > best_area:
            best_area = score
            best = (rx, ry, rw, rh)

    if best is None:
        return None

    rx, ry, rw, rh = best
    pad = 4
    x0 = max(0, rx - pad)
    y0 = max(0, ry - pad)
    x1 = min(iw, rx + rw + pad)
    y1 = min(ih, ry + rh + pad)
    crop = img[y0:y1, x0:x1]

    scale = 4
    big  = cv2.resize(crop, (crop.shape[1]*scale, crop.shape[0]*scale),
                      interpolation=cv2.INTER_CUBIC)
    gray2 = cv2.cvtColor(big, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray2, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    try:
        text = pytesseract.image_to_string(
            Image.fromarray(binary),
            config="--psm 7 --oem 3",
        ).strip()
    except Exception:
        return None

    # Limpar ruído OCR
    text = re.sub(r"[^\w\s\.\-]", "", text).strip()
    return text if len(text) >= 2 else None


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
