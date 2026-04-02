"""Separacao cirurgica de referencias ABNT/Bibliografia em itens individuais."""

from __future__ import annotations

import re
from typing import List


_REFERENCE_HEADER_RE = re.compile(
    r"(?im)^\s*(?:\d+\.?\s*)?(?:REFER[ÊE]NCIAS(?:\s+BIBLIOGR[ÁA]FICAS)?|BIBLIOGRAFIA)\s*: ?\s*$"
)

_ABNT_SPLIT_RE = re.compile(
    r"(?:(?<=\.)|(?<=\d{4})|(?<=\))|(?<=\]))\s+(?=[A-ZÀ-Ÿ]{2,},\s+[A-Z])",
    flags=re.UNICODE,
)

_RE_GLUE_BETWEEN_REFERENCES_RE = re.compile(
    r"(?<=[\d\)\]])\s+(?=[A-ZÀ-Ÿ]{2,},\s+[A-Z])",
    flags=re.UNICODE,
)


def split_abnt_references(raw_text: str) -> List[str]:
    """Separa referencias aglutinadas em obras individuais.

    Regras:
    - remove cabecalhos como REFERÊNCIAS/BIBLIOGRAFIA;
    - colapsa quebras e espacos em um unico espaco;
    - aplica split cirurgico ABNT com lookbehind por ponto final ou ano;
    - retorna apenas itens com mais de 15 caracteres.
    """
    if not raw_text:
        return []

    text = _REFERENCE_HEADER_RE.sub(" ", raw_text)
    text = re.sub(r"\s+", " ", text).strip()

    # Quando OCR/PDF cola duas obras sem ponto final, criamos uma fronteira
    # antes do próximo sobrenome em caixa alta. Isso nao mexe com coautores,
    # porque a quebra depende de fim forte de referencia anterior.
    text = _RE_GLUE_BETWEEN_REFERENCES_RE.sub(". ", text)

    if not text:
        return []

    parts = _ABNT_SPLIT_RE.split(text)
    cleaned: List[str] = []
    for part in parts:
        item = re.sub(r"\s+", " ", part).strip(" ;\n\r\t")
        if len(item) > 15:
            cleaned.append(item)

    if not cleaned and len(text) > 15:
        return [text]

    return cleaned
