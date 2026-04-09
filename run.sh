#!/usr/bin/env bash
# run.sh — Entry point do SAP Agent
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Garantir DISPLAY
export DISPLAY="${DISPLAY:-:1}"

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo "ERRO: ANTHROPIC_API_KEY não definida."
    echo "Execute: export ANTHROPIC_API_KEY='sk-ant-...'"
    exit 1
fi

if [ $# -lt 1 ]; then
    echo "Uso: ./run.sh 'instrução de texto'"
    echo "  ou: ./run.sh /caminho/para/manual.pdf"
    exit 1
fi

echo "=== SAP GUI Automation Agent ==="
echo "Display: $DISPLAY"
echo "Instrução: $1"
echo ""

python3 agent.py "$1"
