"""Validacao de referencias com APIs publicas e consenso de LLMs no Ollama."""

from __future__ import annotations

import json
import os
import re
import gc
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import requests

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*args, **kwargs):
        return False


APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
load_dotenv(ROOT_DIR / ".env")
load_dotenv(APP_DIR / ".env")

from text_formatter import build_structured_prompt, format_raw_text

StatusCallback = Optional[Callable[[str], None]]


def find_references_start(paragraphs: List[str]) -> int:
    """Busca o inicio da secao de referencias com maior flexibilidade."""
    ref_pattern = re.compile(
        r"^(?:\d+\.?\s*)?(REFER[ÊE]NCIAS|BIBLIOGRAFIA|REFERENCES)(?:\s+BIBLIOGR[ÁA]FICAS)?\s*$",
        re.IGNORECASE,
    )

    for idx, paragraph in enumerate(paragraphs):
        if ref_pattern.match(paragraph.strip()):
            return idx
    return -1


def _looks_like_abnt_reference(line: str) -> bool:
    """Heuristica para detectar linha de referencia estilo ABNT/APA."""
    txt = line.strip()
    if len(txt) < 20:
        return False

    # Exemplo comum: SILVA, E. Titulo... 2020
    has_author_comma = bool(re.search(r"^[A-ZÁÀÂÃÉÈÊÍÌÎÓÒÔÕÚÙÛÇ'\-\s]+,\s*[A-Z]", txt))
    has_year = bool(re.search(r"\b(19\d{2}|20\d{2})\b", txt))
    has_separator = "." in txt
    return has_author_comma and has_year and has_separator


def extract_references(paragraphs: List[str]) -> List[Dict[str, object]]:
    """Extrai referencias da secao final de bibliografia."""
    header_index = find_references_start(paragraphs)

    if header_index < 0:
        # Fallback: detecta bloco final com padrao ABNT/APA mesmo sem cabecalho.
        trailing_refs: List[Dict[str, object]] = []
        for idx in range(len(paragraphs) - 1, -1, -1):
            paragraph = paragraphs[idx].strip()
            if not paragraph:
                if trailing_refs:
                    break
                continue
            if _looks_like_abnt_reference(paragraph):
                trailing_refs.append({"paragraph_index": idx, "reference": paragraph})
            elif trailing_refs:
                break

        trailing_refs.reverse()
        return trailing_refs

    references: List[Dict[str, object]] = []
    for idx in range(header_index + 1, len(paragraphs)):
        ref_text = paragraphs[idx].strip()
        if not ref_text:
            continue
        references.append({"paragraph_index": idx, "reference": ref_text})
    return references


def _extract_title_and_authors(reference: str) -> Tuple[str, str]:
    """Separa autores e titulo por heuristica textual."""
    quoted = re.findall(r'"([^"]+)"', reference)
    if quoted:
        title = quoted[0]
    else:
        chunks = [chunk.strip() for chunk in reference.split(".") if chunk.strip()]
        if len(chunks) >= 2:
            title = max(chunks[1:], key=len)
        elif chunks:
            title = chunks[0]
        else:
            title = reference.strip()

    authors = reference.split(".", maxsplit=1)[0].strip()
    return title, authors


def _crossref_lookup(title: str) -> Tuple[bool, str]:
    if not title:
        return False, "titulo vazio para consulta"

    try:
        response = requests.get(
            "https://api.crossref.org/works",
            params={"query.title": title, "rows": 1},
            timeout=8,
        )
        response.raise_for_status()
        payload = response.json()
        total = payload.get("message", {}).get("total-results", 0)
        return total > 0, "crossref"
    except requests.RequestException:
        return False, "crossref indisponivel"


def _google_books_lookup(title: str) -> Tuple[bool, str]:
    if not title:
        return False, "titulo vazio para consulta"

    try:
        response = requests.get(
            "https://www.googleapis.com/books/v1/volumes",
            params={"q": f"intitle:{title}", "maxResults": 1},
            timeout=8,
        )
        response.raise_for_status()
        payload = response.json()
        total = payload.get("totalItems", 0)
        return total > 0, "google_books"
    except requests.RequestException:
        return False, "google books indisponivel"


def _call_ollama_json(
    prompt: str,
    model_name: str,
    base_url: str,
    status_callback: StatusCallback,
) -> Dict[str, object]:
    """Executa uma chamada ao Ollama e devolve saida padronizada."""
    if status_callback:
        status_callback(f"Carregando na GPU: {model_name}")

    try:
        response = requests.post(
            f"{base_url}/api/generate",
            json={"model": model_name, "prompt": prompt, "stream": False},
            timeout=25,
        )
        response.raise_for_status()
        payload = response.json()
        raw = str(payload.get("response", ""))
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
            if not match:
                raise
            data = json.loads(match.group(0))
    except (requests.RequestException, json.JSONDecodeError, ValueError):
        return {"model": model_name, "ok": False, "error": "indisponivel"}
    finally:
        gc.collect()

    verdict = str(data.get("veredito", "inconclusivo")).strip()
    reason = str(data.get("justificativa", "sem justificativa")).strip()
    return {
        "model": model_name,
        "ok": True,
        "veredito": verdict,
        "justificativa": reason,
    }


def _llm_reference_consensus(
    reference: str,
    title: str,
    authors: str,
    status_callback: StatusCallback,
) -> Dict[str, object]:
    """Aplica consenso entre Qwen e Llama para plausibilidade de referencia."""
    base_url = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434")
    llama_model = os.getenv("OLLAMA_PRIMARY_MODEL", os.getenv("OLLAMA_MODEL", "llama3.1:8b"))
    qwen_model = os.getenv("OLLAMA_SECONDARY_MODEL", "qwen2.5:latest")

    qwen_prompt = build_structured_prompt(
        instruction=(
            "Voce analisa coerencia de texto em portugues e foco em fluidez/gramatica. "
            "Retorne JSON com chaves obrigatorias: veredito, justificativa. "
            "Veredito permitido: plausivel, duvidosa, inconclusiva."
        ),
        sections={
            "REFERENCIA": reference,
            "AUTORES": authors,
            "TITULO": title,
        },
    )

    llama_prompt = build_structured_prompt(
        instruction=(
            "Voce analisa fatos e logica bibliografica. "
            "Retorne JSON com chaves obrigatorias: veredito, justificativa. "
            "Veredito permitido: plausivel, duvidosa, inconclusiva."
        ),
        sections={
            "REFERENCIA": reference,
            "AUTORES": authors,
            "TITULO": title,
        },
    )

    qwen_output = _call_ollama_json(qwen_prompt, qwen_model, base_url, status_callback)
    llama_output = _call_ollama_json(llama_prompt, llama_model, base_url, status_callback)

    confidence = "Inconsistente"
    if qwen_output.get("ok") and llama_output.get("ok"):
        if qwen_output.get("veredito") == llama_output.get("veredito"):
            confidence = "Critico"

    return {
        "confidence": confidence,
        "qwen": qwen_output,
        "llama": llama_output,
    }


def validate_references(
    paragraphs: List[str],
    status_callback: StatusCallback = None,
) -> List[Dict[str, object]]:
    """Valida referencias por APIs publicas e consenso de LLMs."""
    extracted = extract_references(paragraphs)
    results: List[Dict[str, object]] = []

    for item in extracted:
        reference = format_raw_text(str(item["reference"]))
        paragraph_index = int(item["paragraph_index"])
        title, authors = _extract_title_and_authors(reference)

        crossref_ok, crossref_source = _crossref_lookup(title)
        books_ok, books_source = _google_books_lookup(title)
        llm_info = _llm_reference_consensus(reference, title, authors, status_callback)

        qwen_verdict = llm_info.get("qwen", {}).get("veredito")
        llama_verdict = llm_info.get("llama", {}).get("veredito")

        if qwen_verdict == "duvidosa" and llama_verdict == "duvidosa":
            status = "dubious"
            reason = "Qwen e Llama concordam em alta chance de alucinacao"
        elif crossref_ok or books_ok:
            status = "ok"
            reason = "Referencia com indicios de existencia em fontes externas"
        elif llm_info.get("confidence") == "Critico" and qwen_verdict == "plausivel":
            status = "ok"
            reason = "Consenso de plausibilidade entre Qwen e Llama"
        else:
            status = "unknown"
            reason = "Validacao inconclusiva"

        sources = [src for ok, src in [(crossref_ok, crossref_source), (books_ok, books_source)] if ok]

        results.append(
            {
                "paragraph_index": paragraph_index,
                "reference": reference,
                "title": title,
                "authors": authors,
                "status": status,
                "reason": reason,
                "evidence_sources": sources,
                "llm_consensus": llm_info,
            }
        )

    return results
