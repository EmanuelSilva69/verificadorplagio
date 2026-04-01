"""Auditoria unitária de referências para integridade científica."""

from __future__ import annotations

import datetime
import json
import os
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import requests

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*args, **kwargs):
        return False

from reference_checker import extract_references
from text_formatter import build_structured_prompt, format_raw_text


APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
load_dotenv(ROOT_DIR / ".env")
load_dotenv(APP_DIR / ".env")

StatusCallback = Optional[Callable[[str], None]]
ProgressCallback = Optional[Callable[[int, int], None]]


def _extract_title_and_authors(reference: str) -> Tuple[str, str]:
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
    return format_raw_text(title), format_raw_text(authors)


def _extract_year(reference: str) -> Optional[int]:
    match = re.search(r"\b(19\d{2}|20\d{2})\b", reference)
    if not match:
        return None
    return int(match.group(1))


def _search_google_scholar_crosscheck(title: str) -> Dict[str, object]:
    """Tarefa A: busca em Google Scholar via Serper/SearXNG e tenta extrair citacoes."""
    provider = os.getenv("SEARCH_API_PROVIDER", "searxng").strip().lower()
    query = f'site:scholar.google.com "{title}"'

    results: List[Dict[str, str]] = []

    if provider == "serper" and os.getenv("SEARCH_API_KEY", "").strip():
        try:
            response = requests.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": os.getenv("SEARCH_API_KEY", ""), "Content-Type": "application/json"},
                json={"q": query, "num": 5, "hl": "pt-br", "gl": "br"},
                timeout=12,
            )
            response.raise_for_status()
            payload = response.json()
            for item in payload.get("organic", [])[:5]:
                results.append(
                    {
                        "title": str(item.get("title", "")).strip(),
                        "snippet": str(item.get("snippet", "")).strip(),
                        "url": str(item.get("link", "")).strip(),
                    }
                )
        except requests.RequestException:
            results = []
    else:
        base_url = os.getenv("SEARCH_SEARXNG_URL", "").strip()
        if base_url:
            try:
                response = requests.get(
                    f"{base_url.rstrip('/')}/search",
                    params={"q": query, "format": "json", "language": "pt-BR"},
                    timeout=12,
                )
                response.raise_for_status()
                payload = response.json()
                for item in payload.get("results", [])[:5]:
                    results.append(
                        {
                            "title": str(item.get("title", "")).strip(),
                            "snippet": str(item.get("content", "")).strip(),
                            "url": str(item.get("url", "")).strip(),
                        }
                    )
            except requests.RequestException:
                results = []

    if not results:
        return {"found": False, "citations": None, "status_web": "Nao encontrado", "top_source": ""}

    citation_count: Optional[int] = None
    for item in results:
        text = f"{item.get('title', '')} {item.get('snippet', '')}".lower()
        cited_match = re.search(r"(?:cited by|citado por)\s*(\d+)", text)
        if cited_match:
            citation_count = int(cited_match.group(1))
            break

    if citation_count is not None:
        status_web = f"Encontrado (citacoes: {citation_count})"
    else:
        status_web = "Encontrado (citacoes: nao identificado)"

    return {
        "found": True,
        "citations": citation_count,
        "status_web": status_web,
        "top_source": results[0].get("url", ""),
    }


def _find_doi(title: str, authors: str) -> Optional[str]:
    """Tarefa B: busca DOI compatível via Crossref."""
    if not title:
        return None

    try:
        response = requests.get(
            "https://api.crossref.org/works",
            params={"query.bibliographic": f"{title} {authors}", "rows": 3},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException:
        return None

    items = payload.get("message", {}).get("items", [])
    for item in items:
        doi = str(item.get("DOI", "")).strip()
        if doi:
            return doi
    return None


def _call_llama_challenger(reference: str, title: str, authors: str) -> Dict[str, object]:
    """Tarefa C: parecer pericial do Llama sobre existência da obra."""
    base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    model = os.getenv("OLLAMA_PRIMARY_MODEL", os.getenv("OLLAMA_MODEL", "llama3.1:8b"))

    prompt = build_structured_prompt(
        instruction=(
            "Ignore a formatacao. Verifique se os autores citados realmente escreveram sobre este tema. "
            "Esta obra existe ou e uma alucinacao plausivel? Se for inventada, explique o porque. "
            "Retorne APENAS JSON com: parecer, suspeita_alucinacao (boolean), justificativa."
        ),
        sections={
            "REFERENCIA": reference,
            "TITULO": title,
            "AUTORES": authors,
        },
    )

    try:
        response = requests.post(
            f"{base_url}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=30,
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

        return {
            "parecer": str(data.get("parecer", "inconclusivo")).strip(),
            "suspeita_alucinacao": bool(data.get("suspeita_alucinacao", False)),
            "justificativa": str(data.get("justificativa", "sem justificativa")).strip(),
            "raw": raw[:1500],
        }
    except (requests.RequestException, json.JSONDecodeError, ValueError):
        return {
            "parecer": "inconclusivo",
            "suspeita_alucinacao": False,
            "justificativa": "Llama indisponivel para parecer pericial.",
            "raw": "",
        }


def _call_qwen_format_checker(reference: str) -> Dict[str, object]:
    """Verifica se a estrutura da referência parece ABNT/APA."""
    base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    model = os.getenv("OLLAMA_SECONDARY_MODEL", "qwen2.5:latest")

    prompt = build_structured_prompt(
        instruction=(
            "Voce e bibliotecario academico. Avalie se a referencia segue formato ABNT/APA. "
            "Retorne APENAS JSON com: formato_valido (boolean), parecer_formato (string)."
        ),
        sections={"REFERENCIA": reference},
    )

    try:
        response = requests.post(
            f"{base_url}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=30,
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

        return {
            "formato_valido": bool(data.get("formato_valido", False)),
            "parecer_formato": str(data.get("parecer_formato", "inconclusivo")).strip(),
            "raw": raw[:1500],
        }
    except (requests.RequestException, json.JSONDecodeError, ValueError):
        return {
            "formato_valido": False,
            "parecer_formato": "Qwen indisponivel para validar formato.",
            "raw": "",
        }


def _hallucination_risk(
    web_found: bool,
    llama_suspect: bool,
    year: Optional[int],
    doi: Optional[str],
) -> Tuple[str, bool]:
    current_year = datetime.date.today().year
    future_or_current_without_evidence = year is not None and year >= current_year and (not web_found)

    confirmed = (not web_found and llama_suspect) or future_or_current_without_evidence
    if confirmed:
        return "Alto", True
    if (not web_found) or (doi is None):
        return "Medio", False
    return "Baixo", False


def audit_references_list(
    ref_list: List[Dict[str, object]],
    status_callback: StatusCallback = None,
    progress_callback: ProgressCallback = None,
) -> List[Dict[str, object]]:
    """Audita uma lista de referencias individualmente com protocolo rigoroso."""
    results: List[Dict[str, object]] = []
    total = len(ref_list)

    for idx, item in enumerate(ref_list, start=1):
        reference = format_raw_text(str(item.get("reference", "")))
        paragraph_index = int(item.get("paragraph_index", -1))
        title, authors = _extract_title_and_authors(reference)
        year = _extract_year(reference)

        if status_callback:
            status_callback(f"🕵️ Verificando se a obra {title} realmente existe...")
        if progress_callback:
            progress_callback(idx, total)

        # Tarefa A: busca web
        cross_check = _search_google_scholar_crosscheck(title)

        # Tarefa B: DOI
        doi = _find_doi(title, authors)

        # Tarefa C1: Qwen formato
        qwen_format = _call_qwen_format_checker(reference)

        # Tarefa C2: Llama existencia
        llama = _call_llama_challenger(reference, title, authors)

        risk, confirmed_hallucination = _hallucination_risk(
            web_found=bool(cross_check.get("found")),
            llama_suspect=bool(llama.get("suspeita_alucinacao")),
            year=year,
            doi=doi,
        )

        if reference.upper().startswith("SILVA, EMANUEL") and (year is not None and year >= datetime.date.today().year):
            if not cross_check.get("found"):
                risk = "Alto"
                confirmed_hallucination = True

        encontrada_google = "Sim" if bool(cross_check.get("found")) else "Nao"
        if confirmed_hallucination:
            veredito_final = "Referência Inventada"
        elif encontrada_google == "Nao":
            veredito_final = "⚠️ ALUCINAÇÃO PROVÁVEL"
        else:
            veredito_final = "Referência Plausível"

        results.append(
            {
                "paragraph_index": paragraph_index,
                "reference": reference,
                "status_web": str(cross_check.get("status_web", "Nao encontrado")),
                "found_google": encontrada_google,
                "doi": doi or "Nao encontrado",
                "qwen_format": qwen_format.get("parecer_formato", "inconclusivo"),
                "llm_parecer": f"{llama.get('parecer', 'inconclusivo')} - {llama.get('justificativa', '')}",
                "hallucination_risk": risk,
                "confirmed_hallucination": confirmed_hallucination,
                "top_source": str(cross_check.get("top_source", "")),
                "llama_raw": str(llama.get("raw", "")),
                "veredito_final": veredito_final,
            }
        )

    return results


def audit_references(
    paragraphs: List[str],
    status_callback: StatusCallback = None,
    progress_callback: ProgressCallback = None,
) -> List[Dict[str, object]]:
    """Executa auditoria unitária rigorosa de cada referência do documento."""
    extracted = extract_references(paragraphs)
    return audit_references_list(
        extracted,
        status_callback=status_callback,
        progress_callback=progress_callback,
    )
