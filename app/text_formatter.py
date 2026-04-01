"""Utilitarios para normalizar texto bruto e montar prompts estruturados."""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, List


def _strip_control_chars(text: str) -> str:
    """Remove caracteres de controle nao-imprimiveis preservando quebras validas.

    Mantemos apenas caracteres de controle que ajudam na estrutura textual:
    - \n: quebra de linha
    - \r: retorno de carro
    - \t: tabulacao
    """
    return "".join(ch for ch in text if unicodedata.category(ch)[0] != "C" or ch in "\n\r\t")


def _remove_repetitive_headers_and_footers(text: str) -> str:
    """Remove cabecalhos e rodapes repetitivos quando um padrao e detectado.

    Estrategia:
    1. Analisa linhas nao vazias e conta repeticoes exatas normalizadas.
    2. Considera ruído linhas curtas que se repetem >= 3 vezes.
    3. Remove linhas com padroes tipicos de rodape (numero de pagina isolado,
       formatos como "Pagina 3", "p. 8", e titulos de periodico muito curtos).
    """
    lines = text.splitlines()
    normalized_counts: Dict[str, int] = {}

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        key = re.sub(r"\s+", " ", stripped).lower()
        normalized_counts[key] = normalized_counts.get(key, 0) + 1

    cleaned_lines: List[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            cleaned_lines.append("")
            continue

        key = re.sub(r"\s+", " ", stripped).lower()

        repeated_short_line = normalized_counts.get(key, 0) >= 3 and len(stripped) <= 80
        page_number_only = bool(re.fullmatch(r"\d{1,4}", stripped))
        page_marker = bool(re.fullmatch(r"(?:pagina|p\.)\s*\d{1,4}", key))
        journal_header = bool(
            re.fullmatch(
                r"(?:revista|journal|anais|proceedings|issn|doi).{0,70}",
                key,
            )
        )

        if repeated_short_line or page_number_only or page_marker or journal_header:
            continue

        cleaned_lines.append(line)

    return "\n".join(cleaned_lines)


def format_raw_text(text: str) -> str:
    """Normaliza texto bruto para reduzir ruído de extracao de PDF/DOCX.

    Pipeline de limpeza:
    1. Normalizacao Unicode NFKC para padronizar aspas, ligaturas e simbolos.
    2. Reconstrucao de palavras hifenizadas em fim de linha.
    3. Remocao de caracteres de controle nao-imprimiveis.
    4. Remocao de cabecalhos e rodapes repetitivos com padroes identificaveis.
    5. Preservacao de paragrafos: quebra dupla (\n\n) e mantida.
    6. Quebras simples no meio de frase viram espaco.
    7. Compactacao de espacos/tabulacoes redundantes.
    """
    if not text:
        return ""

    normalized = unicodedata.normalize("NFKC", text)

    # Reconstrucao de palavras quebradas por hifenacao de fim de linha.
    normalized = re.sub(r"(\w+)-\s*\n\s*(\w+)", r"\1\2", normalized)

    normalized = _strip_control_chars(normalized)
    normalized = _remove_repetitive_headers_and_footers(normalized)

    # Preserva paragrafos e remove quebras unicas em meio de sentencas.
    normalized = re.sub(r"\n\s*\n", "\n\n", normalized)
    normalized = re.sub(r"(?<!\n)\n(?!\n)", " ", normalized)

    # Limpa espacos excessivos em geral.
    normalized = re.sub(r"[ \t]+", " ", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)

    return normalized.strip()


def build_structured_prompt(instruction: str, sections: Dict[str, str]) -> str:
    """Monta prompt em blocos bem separados para reduzir ambiguidades nos LLMs."""
    parts = [
        instruction.strip(),
        "Retorne APENAS JSON valido, sem markdown, sem texto extra.",
    ]
    for title, content in sections.items():
        clean_content = format_raw_text(content)
        parts.append(f"[{title}]\n{clean_content}")
    return "\n\n".join(parts).strip()


if __name__ == "__main__":
    dirty_text = (
        "Revista Brasileira de Analise Textual\n"
        "Pagina 12\n"
        "A compara-\n"
        "cao entre modelos de linguagem exige metodologia\n"
        "consistente em datasets reais.\n\n"
        "Revista Brasileira de Analise Textual\n"
        "Pagina 13\n"
        "No entanto, a extração de PDF pode incluir\n"
        "quebras aleatorias e\t\tespacos excessivos.\n"
    )

    print("=== TEXTO ORIGINAL ===")
    print(dirty_text)
    print("\n=== TEXTO LIMPO ===")
    print(format_raw_text(dirty_text))
