"""Modulo para carregar e normalizar documentos PDF, DOCX e TXT."""

from __future__ import annotations

import io
import re
from typing import Dict, List, Tuple

import fitz  # PyMuPDF
from docx import Document

from reference_handler import split_abnt_references
from text_formatter import format_raw_text


def _group_lines_into_paragraphs(lines: List[str]) -> List[str]:
    """Agrupa linhas em paragrafos respeitando quebras em branco."""
    paragraphs: List[str] = []
    buffer: List[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if buffer:
                paragraphs.append(" ".join(buffer).strip())
                buffer = []
            continue
        buffer.append(stripped)

    if buffer:
        paragraphs.append(" ".join(buffer).strip())

    return paragraphs


def clean_text_preserve_paragraphs(paragraphs: List[str]) -> List[str]:
    """Limpa ruido mantendo a estrutura por paragrafos."""
    cleaned: List[str] = []
    for paragraph in paragraphs:
        paragraph = format_raw_text(paragraph)
        # Remove espacos extras e caracteres de controle comuns.
        txt = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", paragraph)
        txt = re.sub(r"\s+", " ", txt).strip()
        if txt:
            cleaned.append(txt)
    return cleaned


def _is_reference_header_line(text: str) -> bool:
    return bool(
        re.match(
            r"^(?:\d+\.?\s*)?(REFER[ÊE]NCIAS|BIBLIOGRAFIA|REFERENCES)(?:\s+BIBLIOGR[ÁA]FICAS)?\s*:?$",
            text.strip(),
            flags=re.IGNORECASE | re.UNICODE,
        )
    )


def smart_split_references(text_block: str) -> List[str]:
    """Wrapper local para manter compatibilidade, delegando ao separador ABNT dedicado."""
    return split_abnt_references(format_raw_text(text_block))


def _apply_reference_split(paragraphs: List[str]) -> List[str]:
    """Após detectar seção de referências, preserva itemização por obra para evitar aglutinação."""
    if not paragraphs:
        return paragraphs

    start = -1
    for idx, paragraph in enumerate(paragraphs):
        if _is_reference_header_line(paragraph):
            start = idx + 1
            break

    if start < 0:
        return paragraphs

    updated: List[str] = []
    updated.extend(paragraphs[:start])

    for paragraph in paragraphs[start:]:
        if not paragraph.strip():
            continue
        split_items = smart_split_references(paragraph)
        if split_items:
            updated.extend(split_items)
        else:
            updated.append(paragraph)

    return updated


def _read_pdf(file_bytes: bytes) -> List[str]:
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    lines: List[str] = []
    for page in doc:
        page_text = page.get_text("text")
        lines.extend(format_raw_text(page_text).splitlines())
        lines.append("")
    doc.close()
    return _group_lines_into_paragraphs(lines)


def _read_docx(file_bytes: bytes) -> List[str]:
    document = Document(io.BytesIO(file_bytes))
    paragraphs = [format_raw_text(p.text).strip() for p in document.paragraphs]
    return [p for p in paragraphs if p]


def _read_txt(file_bytes: bytes) -> List[str]:
    try:
        content = file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        content = file_bytes.decode("latin-1", errors="ignore")
    return _group_lines_into_paragraphs(format_raw_text(content).splitlines())


def load_document(file_name: str, file_bytes: bytes) -> Tuple[List[str], str]:
    """Carrega documento e retorna (paragrafos_limpos, texto_unificado)."""
    ext = file_name.lower().rsplit(".", maxsplit=1)[-1]

    if ext == "pdf":
        paragraphs = _read_pdf(file_bytes)
    elif ext in {"docx", "doc"}:
        paragraphs = _read_docx(file_bytes)
    elif ext == "txt":
        paragraphs = _read_txt(file_bytes)
    else:
        raise ValueError("Formato nao suportado. Use PDF, DOCX ou TXT.")

    cleaned = clean_text_preserve_paragraphs(paragraphs)
    cleaned = _apply_reference_split(cleaned)
    unified = "\n\n".join(cleaned)
    return cleaned, unified


def _find_references_start(paragraphs: List[str]) -> int:
    """Detecta inicio da secao de referencias com regex flexivel."""
    header_pattern = re.compile(
        r"^(?:\d+\.?\s*)?(REFER[ÊE]NCIAS|BIBLIOGRAFIA|REFERENCES)(?:\s+BIBLIOGR[ÁA]FICAS)?\s*:?$",
        re.IGNORECASE | re.UNICODE,
    )
    for idx, paragraph in enumerate(paragraphs):
        if header_pattern.match(paragraph.strip()):
            return idx
    return -1


def _looks_like_abnt_reference(line: str) -> bool:
    """Heuristica para reconhecer referencia ABNT/APA no fim do documento."""
    txt = line.strip()
    if len(txt) < 20:
        return False

    has_author_comma = bool(
        re.search(r"^[A-ZÁÀÂÃÉÈÊÍÌÎÓÒÔÕÚÙÛÇ'\-\s]+,\s*[A-Z]", txt, flags=re.UNICODE)
    )
    has_year = bool(re.search(r"\b(19\d{2}|20\d{2})\b", txt, flags=re.UNICODE))
    has_separator = "." in txt
    return has_author_comma and has_year and has_separator


def extract_reference_candidates(paragraphs: List[str]) -> List[Dict[str, object]]:
    """Extrai referencias com fallback flexivel para listas ABNT sem cabecalho."""
    header_index = _find_references_start(paragraphs)

    if header_index >= 0:
        refs: List[Dict[str, object]] = []
        for idx in range(header_index + 1, len(paragraphs)):
            ref_text = paragraphs[idx].strip()
            if not ref_text:
                continue
            split_refs = split_abnt_references(ref_text)
            if split_refs:
                for ref in split_refs:
                    refs.append({"paragraph_index": idx, "reference": ref})
            else:
                refs.append({"paragraph_index": idx, "reference": ref_text})
        return refs

    trailing_refs: List[Dict[str, object]] = []
    for idx in range(len(paragraphs) - 1, -1, -1):
        paragraph = paragraphs[idx].strip()
        if not paragraph:
            if trailing_refs:
                break
            continue
        if _looks_like_abnt_reference(paragraph):
            split_refs = split_abnt_references(paragraph)
            if split_refs:
                for ref in reversed(split_refs):
                    trailing_refs.append({"paragraph_index": idx, "reference": ref})
            else:
                trailing_refs.append({"paragraph_index": idx, "reference": paragraph})
        elif trailing_refs:
            break

    trailing_refs.reverse()
    return trailing_refs
