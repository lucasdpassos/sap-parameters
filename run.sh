#!/usr/bin/env bash
# run.sh — Entry point do SAP Agent
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Garantir DISPLAY
export DISPLAY="${DISPLAY:-:1}"

USE_CODEMIE=false
USE_MINIMAX=false
ARGS=()
for arg in "$@"; do
    if [ "$arg" = "--codemie" ]; then
        USE_CODEMIE=true
    elif [ "$arg" = "--minimax" ]; then
        USE_MINIMAX=true
    else
        ARGS+=("$arg")
    fi
done

if [ ${#ARGS[@]} -lt 1 ]; then
    echo "Uso: ./run.sh [--codemie|--minimax] 'instrução de texto'"
    echo "  ou: ./run.sh [--codemie|--minimax] /caminho/para/manual.pdf"
    exit 1
fi

if [ "$USE_CODEMIE" = true ] && [ "$USE_MINIMAX" = true ]; then
    echo "ERRO: use apenas --codemie ou --minimax, não os dois."
    exit 1
fi

if [ "$USE_CODEMIE" = true ]; then
    if [ -z "${CODEMIE_USERNAME:-}" ] || [ -z "${CODEMIE_PASSWORD:-}" ]; then
        echo "ERRO: defina CODEMIE_USERNAME e CODEMIE_PASSWORD para usar --codemie"
        exit 1
    fi
    echo "=== SAP GUI Automation Agent [Codemie backend] ==="
elif [ "$USE_MINIMAX" = true ]; then
    if [ -z "${MINIMAX_API_KEY:-}" ]; then
        echo "ERRO: defina MINIMAX_API_KEY para usar --minimax"
        echo "Execute: export MINIMAX_API_KEY='sk-api-...'"
        exit 1
    fi
    echo "=== SAP GUI Automation Agent [MiniMax backend] ==="
else
    if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
        echo "ERRO: ANTHROPIC_API_KEY não definida."
        echo "Execute: export ANTHROPIC_API_KEY='sk-ant-...'"
        echo "  ou use --codemie com CODEMIE_USERNAME e CODEMIE_PASSWORD"
        echo "  ou use --minimax com MINIMAX_API_KEY"
        exit 1
    fi
    echo "=== SAP GUI Automation Agent [Anthropic backend] ==="
fi

echo "Display   : $DISPLAY"
echo "Instrução : ${ARGS[0]}"
echo ""

if [ "$USE_CODEMIE" = true ]; then
    python3 agent.py --codemie "${ARGS[@]}"
elif [ "$USE_MINIMAX" = true ]; then
    python3 agent.py --minimax "${ARGS[@]}"
else
    python3 agent.py "${ARGS[@]}"
fi
