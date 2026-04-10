#!/usr/bin/env bash
# run.sh — Entry point do SAP Agent
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Garantir DISPLAY
export DISPLAY="${DISPLAY:-:1}"

USE_CODEMIE=false
ARGS=()
for arg in "$@"; do
    if [ "$arg" = "--codemie" ]; then
        USE_CODEMIE=true
    else
        ARGS+=("$arg")
    fi
done

if [ ${#ARGS[@]} -lt 1 ]; then
    echo "Uso: ./run.sh [--codemie] 'instrução de texto'"
    echo "  ou: ./run.sh [--codemie] /caminho/para/manual.pdf"
    exit 1
fi

if [ "$USE_CODEMIE" = true ]; then
    if [ -z "${CODEMIE_USERNAME:-}" ] || [ -z "${CODEMIE_PASSWORD:-}" ]; then
        echo "ERRO: defina CODEMIE_USERNAME e CODEMIE_PASSWORD para usar --codemie"
        exit 1
    fi
    echo "=== SAP GUI Automation Agent [Codemie backend] ==="
else
    if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
        echo "ERRO: ANTHROPIC_API_KEY não definida."
        echo "Execute: export ANTHROPIC_API_KEY='sk-ant-...'"
        echo "  ou use --codemie com CODEMIE_USERNAME e CODEMIE_PASSWORD"
        exit 1
    fi
    echo "=== SAP GUI Automation Agent [Anthropic backend] ==="
fi

echo "Display   : $DISPLAY"
echo "Instrução : ${ARGS[0]}"
echo ""

if [ "$USE_CODEMIE" = true ]; then
    python3 agent.py --codemie "${ARGS[@]}"
else
    python3 agent.py "${ARGS[@]}"
fi
