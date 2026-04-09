"""
parser.py — Lê instruções de texto ou PDF e retorna lista de steps
"""
import re
import sys
from pathlib import Path


def parse_text(text: str) -> list[str]:
    """
    Recebe uma string de instrução e retorna uma lista de steps.
    Tenta dividir por vírgulas, pontos ou numeração.
    """
    text = text.strip()

    # Se tiver numeração explícita: 1. xxx 2. xxx
    numbered = re.split(r'\d+\.\s+', text)
    numbered = [s.strip() for s in numbered if s.strip()]
    if len(numbered) > 1:
        return numbered

    # Divide por vírgula seguida de verbos de ação comuns (com pelo menos 5 chars depois)
    # Evita splits em "e na janela que abrir" ou fragmentos curtos
    action_verbs = r'(clique em|click em|abra o|open the|feche|close|digite|type |pressione|press |navegue|navigate|selecione|select|espere|wait|vá para|go to)'
    parts = re.split(r',\s*(?=' + action_verbs + r')', text, flags=re.IGNORECASE)
    # Filtra partes muito curtas (menos de 10 chars) que provavelmente são fragmentos
    parts = [p.strip() for p in parts if p.strip() and len(p.strip()) >= 10]
    if len(parts) > 1:
        return parts

    # Divide por ponto e vírgula
    parts = re.split(r';\s*', text)
    parts = [p.strip() for p in parts if p.strip() and len(p.strip()) >= 10]
    if len(parts) > 1:
        return parts

    # Divide por ponto final seguido de espaço e letra maiúscula
    parts = re.split(r'\.\s+(?=[A-ZÀ-Ú])', text)
    parts = [p.strip().rstrip('.') for p in parts if p.strip() and len(p.strip()) >= 10]
    if len(parts) > 1:
        return parts

    # Retorna tudo como step único
    return [text]


def parse_pdf(path: str) -> list[str]:
    """Lê um PDF e extrai steps de instrução."""
    try:
        import fitz  # pymupdf
    except ImportError:
        raise ImportError("pymupdf não instalado. Execute: pip install pymupdf")

    doc = fitz.open(path)
    full_text = ""
    for page in doc:
        full_text += page.get_text()
    doc.close()
    return parse_text(full_text)


def load_instructions(source: str) -> list[str]:
    """
    source pode ser:
      - caminho para arquivo .pdf
      - caminho para arquivo .txt
      - string de instrução direta
    Retorna lista de steps.
    """
    p = Path(source)
    if p.exists():
        if p.suffix.lower() == ".pdf":
            return parse_pdf(str(p))
        else:
            return parse_text(p.read_text(encoding="utf-8"))
    else:
        return parse_text(source)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python parser.py 'instrução ou caminho'")
        sys.exit(1)
    steps = load_instructions(sys.argv[1])
    for i, s in enumerate(steps, 1):
        print(f"Step {i}: {s}")
