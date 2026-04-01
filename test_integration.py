"""Teste de integracao: busca web + respostas de dois modelos Ollama.

Uso:
    python test_integration.py
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Tuple

import requests

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*args, **kwargs):
        return False


ROOT_DIR = Path(__file__).resolve().parent
APP_DIR = ROOT_DIR / "app"
load_dotenv(ROOT_DIR / ".env")
load_dotenv(APP_DIR / ".env")


def _print_result(name: str, ok: bool, details: str) -> None:
    status = "OK" if ok else "FALHA"
    print(f"[{status}] {name}: {details}")


def _search_web_smoke_test() -> Tuple[bool, str]:
    provider = os.getenv("SEARCH_API_PROVIDER", "searxng").strip().lower()
    query = "plagio academico deteccao"

    if provider == "serper":
        api_key = os.getenv("SEARCH_API_KEY", "").strip()
        if not api_key:
            return False, "SEARCH_API_KEY ausente para Serper"
        try:
            response = requests.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                json={"q": query, "num": 3},
                timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
            items = payload.get("organic", [])
            return len(items) > 0, f"provider=serper | resultados={len(items)}"
        except requests.RequestException as exc:
            return False, f"erro serper: {exc}"

    if provider == "tavily":
        api_key = os.getenv("SEARCH_API_KEY", "").strip()
        if not api_key:
            return False, "SEARCH_API_KEY ausente para Tavily"
        try:
            response = requests.post(
                "https://api.tavily.com/search",
                json={"api_key": api_key, "query": query, "max_results": 3},
                timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
            items = payload.get("results", [])
            return len(items) > 0, f"provider=tavily | resultados={len(items)}"
        except requests.RequestException as exc:
            return False, f"erro tavily: {exc}"

    searxng_url = os.getenv("SEARCH_SEARXNG_URL", "").strip()
    if not searxng_url:
        # Fallback para validar conectividade basica mesmo sem SearXNG configurado.
        try:
            response = requests.get(
                "https://api.duckduckgo.com/",
                params={"q": query, "format": "json", "no_html": 1},
                timeout=10,
            )
            response.raise_for_status()
            return True, "fallback=duckduckgo (SEARCH_SEARXNG_URL nao configurado)"
        except requests.RequestException as exc:
            return False, f"erro fallback duckduckgo: {exc}"

    try:
        response = requests.get(
            f"{searxng_url.rstrip('/')}/search",
            params={"q": query, "format": "json", "language": "pt-BR"},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        items = payload.get("results", [])
        return len(items) > 0, f"provider=searxng | resultados={len(items)}"
    except requests.RequestException as exc:
        return False, f"erro searxng: {exc}"


def _call_ollama(prompt: str, model: str, base_url: str) -> Dict[str, object]:
    try:
        response = requests.post(
            f"{base_url}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        raw_response = str(payload.get("response", ""))
    except requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}

    try:
        data = json.loads(raw_response)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw_response, flags=re.DOTALL)
        if not match:
            if raw_response.strip():
                return {"ok": True, "data": {"texto_livre": raw_response[:240]}, "raw_only": True}
            return {"ok": False, "error": "json_invalido", "raw": raw_response[:180]}
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            if raw_response.strip():
                return {"ok": True, "data": {"texto_livre": raw_response[:240]}, "raw_only": True}
            return {"ok": False, "error": "json_invalido", "raw": raw_response[:180]}

    return {"ok": True, "data": data}


def _ollama_dual_model_test() -> Tuple[bool, str]:
    base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    llama_model = os.getenv("OLLAMA_PRIMARY_MODEL", os.getenv("OLLAMA_MODEL", "llama3.1:8b"))
    qwen_model = os.getenv("OLLAMA_SECONDARY_MODEL", "qwen2.5:latest")

    prompt = (
        "Responda SOMENTE JSON com as chaves veredito e justificativa. "
        "Veredito deve ser: plausivel, duvidosa ou inconclusiva.\n\n"
        "Referencia: Silva, J. Metodos de Analise Textual. 2022."
    )

    qwen_out = _call_ollama(prompt, qwen_model, base_url)
    llama_out = _call_ollama(prompt, llama_model, base_url)

    if not qwen_out.get("ok") or not llama_out.get("ok"):
        return False, f"qwen={qwen_out} | llama={llama_out}"

    qwen_data = qwen_out.get("data", {})
    llama_data = llama_out.get("data", {})

    qwen_ok = bool(qwen_data)
    llama_ok = bool(llama_data)

    ok = qwen_ok and llama_ok
    details = (
        f"qwen_veredito={qwen_data.get('veredito', qwen_data.get('plausivel'))} | "
        f"llama_veredito={llama_data.get('veredito', llama_data.get('plausivel'))}"
    )
    return ok, details


def main() -> None:
    search_ok, search_details = _search_web_smoke_test()
    _print_result("Busca Web", search_ok, search_details)

    ollama_ok, ollama_details = _ollama_dual_model_test()
    _print_result("Ollama Qwen+Llama", ollama_ok, ollama_details)

    if not (search_ok and ollama_ok):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
