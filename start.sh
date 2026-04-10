#!/usr/bin/env bash
# start.sh — Inicializa todo o ambiente do SAP Agent após reboot
#
# O que este script faz:
#   1. Sobe Xvfb :1 (display virtual)
#   2. Sobe x11vnc (acesso VNC na porta 5900)
#   3. Sobe noVNC/websockify (acesso web na porta 6080)
#   4. Abre o SAP GUI for Java
#   5. Imprime o status e fica pronto para rodar o agent
#
# Uso: cd ~/sap-agent && ./start.sh
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export DISPLAY=":1"
SAP_BIN="/opt/SAPClients/SAPGUI8.10rev6/bin/sapgui"
NOVNC_WEB="/usr/share/novnc"

echo "========================================"
echo "  SAP Agent — Environment Setup"
echo "========================================"

# ── 1. Xvfb ────────────────────────────────────────────────────────────────
if pgrep -x Xvfb > /dev/null; then
    echo "[✓] Xvfb already running on :1"
else
    echo "[~] Starting Xvfb :1 ..."
    Xvfb :1 -screen 0 1280x800x24 &
    sleep 1
    echo "[✓] Xvfb started"
fi

# ── 2. x11vnc ───────────────────────────────────────────────────────────────
if pgrep -x x11vnc > /dev/null; then
    echo "[✓] x11vnc already running (port 5900)"
else
    echo "[~] Starting x11vnc ..."
    x11vnc -display :1 -forever -shared -noxdamage -noxfixes -noshm \
           -rfbport 5900 -nopw -bg -o /tmp/x11vnc.log
    sleep 1
    echo "[✓] x11vnc started (port 5900)"
fi

# ── 3. noVNC / websockify ────────────────────────────────────────────────────
if pgrep -f "websockify.*6080" > /dev/null; then
    echo "[✓] noVNC already running (port 6080)"
else
    echo "[~] Starting noVNC ..."
    nohup websockify --web="$NOVNC_WEB" 6080 localhost:5900 \
          > /tmp/novnc.log 2>&1 &
    sleep 1
    echo "[✓] noVNC started (port 6080)"
fi

# ── 4. SAP GUI for Java ──────────────────────────────────────────────────────
if pgrep -f "GuiStartS.jar" > /dev/null; then
    echo "[✓] SAP GUI for Java already running"
else
    echo "[~] Starting SAP GUI for Java ..."
    DISPLAY=:1 nohup "$SAP_BIN" > /tmp/sapgui.log 2>&1 &
    echo "[~] Waiting for SAP GUI to load (10s)..."
    sleep 10
    if pgrep -f "GuiStartS.jar" > /dev/null; then
        echo "[✓] SAP GUI for Java started"
    else
        echo "[!] SAP GUI may have failed to start — check /tmp/sapgui.log"
    fi
fi

# ── 5. Summary ───────────────────────────────────────────────────────────────
echo ""
echo "========================================"
echo "  Environment ready!"
echo "========================================"
echo ""
echo "  VNC viewer  : <server-ip>:5900"
echo "  noVNC web   : http://<server-ip>:6080/vnc.html"
echo ""
echo "  Agent dir   : $SCRIPT_DIR"
echo ""
echo "  Run examples:"
echo "    Anthropic : export ANTHROPIC_API_KEY='sk-ant-...'"
echo "                ./run.sh 'open the scripts tab, click on scripting...'"
echo ""
echo "    Codemie   : export CODEMIE_USERNAME='...'"
echo "                export CODEMIE_PASSWORD='...'"
echo "                ./run.sh --codemie 'your instruction here'"
echo ""
echo "    MiniMax   : export MINIMAX_API_KEY='sk-api-...'"
echo "                ./run.sh --minimax 'your instruction here'"
echo ""
echo "  Already in the right directory. Ready!"
echo ""

cd "$SCRIPT_DIR"
