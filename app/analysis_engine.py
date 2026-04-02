"""Motor de analise forense com scraping profundo e consenso de LLMs."""

from __future__ import annotations

import asyncio
import concurrent.futures
import gc
import hashlib
import json
import os
import re
import statistics
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Set

import requests
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*args, **kwargs):
        return False

from document_loader import extract_reference_candidates
from text_formatter import build_structured_prompt, format_raw_text
from web_scraper import fetch_page_text_selenium, fetch_urls_parallel


APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
load_dotenv(ROOT_DIR / ".env")
load_dotenv(APP_DIR / ".env")

SIMULATED_CORPUS = [
    "A inteligencia artificial generativa transformou a forma como estudantes produzem conteudo textual em ambientes digitais.",
    "A deteccao de plagio em trabalhos academicos depende da comparacao semantica entre documentos e fontes publicas indexadas.",
    "Ferramentas de similaridade textual usam vetorizacao e distancia de cosseno para estimar proximidade entre trechos.",
    "A avaliacao forense de documentos exige rastreabilidade das fontes e validacao de referencias bibliograficas.",
]

AI_PATTERN_TOKENS = [
    "em resumo",
    "e importante notar",
    "por outro lado",
    "ademais",
    "portanto",
    "em suma",
]

REGIONAL_MA_TOKENS = [
    "oxente",
    "pois sim",
    "visse",
    "caboco",
    "arretado",
    "danado",
    "uai",
]

HUMAN_BASELINE = {
    "uniformidade": 0.42,
    "repeticao": 0.27,
    "conectivos": 0.22,
}

# Cache de buscas única por parágrafo (hashing para evitar duplicatas)
_PARAGRAPH_SEARCH_CACHE: Dict[str, Set[str]] = {}

# Regex flags padrão: case-insensitive + Unicode-aware
REGEX_FLAGS = re.IGNORECASE | re.UNICODE

StatusCallback = Optional[Callable[[str], None]]
ProgressCallback = Optional[Callable[[float], None]]
ModelProgressCallback = Optional[Callable[[int, str, int, int, str], None]]
DebugCallback = Optional[Callable[[str], None]]
ReferenceProgressCallback = Optional[Callable[[int, int, str], None]]


@dataclass
class AIHit:
    paragraph_index: int
    reasons: List[str]


@dataclass
class PlagiarismHit:
    paragraph_index: int
    phrase: str
    phrase_type: str
    similarity: float
    source_title: str
    source_url: str
    source_engine: str
    source_excerpt: str
    scraped_text: str
    exact_phrase_match: bool
    classification: str
    llm_consensus: Dict[str, object]
    supporting_matches: List[Dict[str, object]]


@dataclass(frozen=True)
class HeuristicRule:
    name: str
    category: str
    weight: int
    pattern: re.Pattern[str]


REGEX_FAST_FLAGS = re.IGNORECASE | re.UNICODE | re.MULTILINE

CRITICAL_META_RULES: Tuple[HeuristicRule, ...] = (
    HeuristicRule(
        name="Meta: modelo de linguagem",
        category="critical_meta",
        weight=100,
        pattern=re.compile(r"\bcomo\s+um\s+modelo\s+de\s+linguagem\b|\bcomo\s+uma\s+intelig[êe]ncia\s+artificial\b", REGEX_FAST_FLAGS),
    ),
    HeuristicRule(
        name="Meta: entrega pronta",
        category="critical_meta",
        weight=100,
        pattern=re.compile(r"\bclaro!?\s*aqui\s+est[áa]\s+(?:o|a)\b|\baqui\s+est[áa]\s+o\s+resumo\s+solicitado\b", REGEX_FAST_FLAGS),
    ),
    HeuristicRule(
        name="Meta: recusa/limitação",
        category="critical_meta",
        weight=100,
        pattern=re.compile(r"\blamento,?\s+mas\s+n[ãa]o\s+posso\b|\bn[ãa]o\s+tenho\s+acesso\s+a\s+dados\s+em\s+tempo\s+real\b", REGEX_FAST_FLAGS),
    ),
    HeuristicRule(
        name="Meta: encerramento assistente",
        category="critical_meta",
        weight=100,
        pattern=re.compile(r"\bespero\s+que\s+isso\s+ajude\b|\bse\s+precisar\s+de\s+mais\s+alguma\s+coisa\b", REGEX_FAST_FLAGS),
    ),
)

MARKDOWN_LATEX_RULES: Tuple[HeuristicRule, ...] = (
    HeuristicRule("Markdown: negrito", "markdown_latex", 30, re.compile(r"\*\*[^*\n]{2,}\*\*", REGEX_FAST_FLAGS)),
    HeuristicRule("Markdown: itálico", "markdown_latex", 30, re.compile(r"(?<!\*)\*[^*\n]{2,}\*(?!\*)", REGEX_FAST_FLAGS)),
    HeuristicRule("Markdown: título", "markdown_latex", 30, re.compile(r"(?m)^\s*#{2,3}\s+", REGEX_FAST_FLAGS)),
    HeuristicRule("Markdown: lista com traço", "markdown_latex", 30, re.compile(r"(?m)^\s*-\s+", REGEX_FAST_FLAGS)),
    HeuristicRule("Markdown: lista com asterisco", "markdown_latex", 30, re.compile(r"(?m)^\s*\*\s+", REGEX_FAST_FLAGS)),
    HeuristicRule("Markdown: crases triplas", "markdown_latex", 30, re.compile(r"```", REGEX_FAST_FLAGS)),
    HeuristicRule("LaTeX: resíduos", "markdown_latex", 30, re.compile(r"\\textbf\{[^}]*\}|\\begin\{itemize\}|\\item\b|\\\[|\\\]", REGEX_FAST_FLAGS)),
    HeuristicRule("Markdown: linha horizontal", "markdown_latex", 30, re.compile(r"(?m)^\s*---+\s*$", REGEX_FAST_FLAGS)),
    HeuristicRule("Aspas duplas aninhadas", "markdown_latex", 30, re.compile(r'""[^"\n]{2,}""', REGEX_FAST_FLAGS)),
)

ROBOTIC_CONNECTOR_RULES: Tuple[HeuristicRule, ...] = (
    HeuristicRule("Conectivo: Ademais", "robotic_connectors", 15, re.compile(r"\bademais\b", REGEX_FAST_FLAGS)),
    HeuristicRule("Conectivo: Em suma", "robotic_connectors", 15, re.compile(r"\bem\s+suma\b", REGEX_FAST_FLAGS)),
    HeuristicRule("Conectivo: Em conclusão", "robotic_connectors", 15, re.compile(r"\bem\s+conclus[ãa]o\b", REGEX_FAST_FLAGS)),
    HeuristicRule("Conectivo: Por outro lado", "robotic_connectors", 15, re.compile(r"\bpor\s+outro\s+lado\b", REGEX_FAST_FLAGS)),
    HeuristicRule("Conectivo: É importante notar", "robotic_connectors", 15, re.compile(r"\b[ée]\s+importante\s+notar\s+que\b|\b[ée]\s+importante\s+notar\b", REGEX_FAST_FLAGS)),
    HeuristicRule("Conectivo: Vale ressaltar", "robotic_connectors", 15, re.compile(r"\bvale\s+ressaltar\s+que\b|\bvale\s+ressaltar\b", REGEX_FAST_FLAGS)),
    HeuristicRule("Conectivo: Neste contexto", "robotic_connectors", 15, re.compile(r"\bneste\s+contexto\b", REGEX_FAST_FLAGS)),
    HeuristicRule("Conectivo: Nesse sentido", "robotic_connectors", 15, re.compile(r"\bnesse\s+sentido\b", REGEX_FAST_FLAGS)),
    HeuristicRule("Conectivo: Em primeiro lugar", "robotic_connectors", 15, re.compile(r"\bem\s+primeiro\s+lugar\b", REGEX_FAST_FLAGS)),
    HeuristicRule("Conectivo: Em segundo lugar", "robotic_connectors", 15, re.compile(r"\bem\s+segundo\s+lugar\b", REGEX_FAST_FLAGS)),
    HeuristicRule("Conectivo: Consequentemente", "robotic_connectors", 15, re.compile(r"\bconsequentemente\b", REGEX_FAST_FLAGS)),
    HeuristicRule("Conectivo: Portanto", "robotic_connectors", 15, re.compile(r"\bportanto\b", REGEX_FAST_FLAGS)),
    HeuristicRule("Conectivo: Em resumo", "robotic_connectors", 15, re.compile(r"\bem\s+resumo\b", REGEX_FAST_FLAGS)),
)

STEREOTYPED_VOCAB_RULES: Tuple[HeuristicRule, ...] = (
    HeuristicRule("Vocabulário: crucial", "stereotyped_vocab", 5, re.compile(r"\bcrucial\b", REGEX_FAST_FLAGS)),
    HeuristicRule("Vocabulário: fundamental", "stereotyped_vocab", 5, re.compile(r"\bfundamental\b", REGEX_FAST_FLAGS)),
    HeuristicRule("Vocabulário: tapeçaria", "stereotyped_vocab", 5, re.compile(r"\btape[çc]aria\b", REGEX_FAST_FLAGS)),
    HeuristicRule("Vocabulário: mergulhar", "stereotyped_vocab", 5, re.compile(r"\bmergulhar\b", REGEX_FAST_FLAGS)),
    HeuristicRule("Vocabulário: navegar", "stereotyped_vocab", 5, re.compile(r"\bnavegar\b", REGEX_FAST_FLAGS)),
    HeuristicRule("Vocabulário: desbloquear", "stereotyped_vocab", 5, re.compile(r"\bdesbloquear\b", REGEX_FAST_FLAGS)),
    HeuristicRule("Vocabulário: paisagem", "stereotyped_vocab", 5, re.compile(r"\bpaisagem\b", REGEX_FAST_FLAGS)),
    HeuristicRule("Vocabulário: multifacetado", "stereotyped_vocab", 5, re.compile(r"\bmultifacetad[oa]\b", REGEX_FAST_FLAGS)),
    HeuristicRule("Vocabulário: robusto", "stereotyped_vocab", 5, re.compile(r"\brobust[oa]\b", REGEX_FAST_FLAGS)),
    HeuristicRule("Vocabulário: catalisador", "stereotyped_vocab", 5, re.compile(r"\bcatalisador\b", REGEX_FAST_FLAGS)),
    HeuristicRule("Vocabulário: paradigma", "stereotyped_vocab", 5, re.compile(r"\bparadigma\b", REGEX_FAST_FLAGS)),
    HeuristicRule("Vocabulário: abraçar", "stereotyped_vocab", 5, re.compile(r"\babra[çc]ar\b", REGEX_FAST_FLAGS)),
    HeuristicRule("Vocabulário: tapeçaria rica", "stereotyped_vocab", 5, re.compile(r"\btape[çc]aria\s+rica\b", REGEX_FAST_FLAGS)),
    HeuristicRule("Vocabulário: mergulhar neste", "stereotyped_vocab", 5, re.compile(r"\bmergulhar\s+neste\b", REGEX_FAST_FLAGS)),
    HeuristicRule("Vocabulário: desbloquear o potencial", "stereotyped_vocab", 5, re.compile(r"\bdesbloquear\s+o\s+potencial\b", REGEX_FAST_FLAGS)),
    HeuristicRule("Vocabulário: paisagem tecnológica", "stereotyped_vocab", 5, re.compile(r"\bpaisagem\s+tecnol[óo]gica\b", REGEX_FAST_FLAGS)),
    HeuristicRule("Vocabulário: abordagem multifacetada", "stereotyped_vocab", 5, re.compile(r"\babordagem\s+multifacetada\b", REGEX_FAST_FLAGS)),
)

ALL_WEIGHTED_HEURISTIC_RULES: Tuple[HeuristicRule, ...] = (
    MARKDOWN_LATEX_RULES + ROBOTIC_CONNECTOR_RULES + STEREOTYPED_VOCAB_RULES
)

NON_VOCAB_WEIGHTED_HEURISTIC_RULES: Tuple[HeuristicRule, ...] = (
    MARKDOWN_LATEX_RULES + ROBOTIC_CONNECTOR_RULES
)


def _paragraph_hash(text: str) -> str:
    """Gera hash único para um parágrafo (para evitar buscas duplicadas)."""
    normalized = re.sub(r"\s+", " ", text.strip(), flags=REGEX_FLAGS)
    return hashlib.md5(normalized.encode()).hexdigest()


def _word_count(sentence: str) -> int:
    """Conta palavras em uma sentença usando Unicode."""
    return len(re.findall(r"\b\w+\b", sentence, flags=REGEX_FLAGS))


def _skip_paragraph(text: str, word_threshold: int = 25) -> bool:
    """Retorna True se parágrafo deve ser ignorado (muito curto ou vazio)."""
    words = _word_count(text)
    return words < word_threshold


def _has_searched_phrase(paragraph_hash: str, phrase_hash: str) -> bool:
    """Verifica se esta frase já foi pesquisada para este parágrafo."""
    return phrase_hash in _PARAGRAPH_SEARCH_CACHE.get(paragraph_hash, set())


def _mark_phrase_searched(paragraph_hash: str, phrase_hash: str) -> None:
    """Marca uma frase como já pesquisada para este parágrafo."""
    if paragraph_hash not in _PARAGRAPH_SEARCH_CACHE:
        _PARAGRAPH_SEARCH_CACHE[paragraph_hash] = set()
    _PARAGRAPH_SEARCH_CACHE[paragraph_hash].add(phrase_hash)


def _split_sentences(text: str) -> List[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text, flags=REGEX_FLAGS) if s.strip()]


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text, flags=REGEX_FLAGS).strip()


def _extract_significant_phrases(paragraph: str, max_phrases: int = 3) -> List[Tuple[str, str]]:
    """Extrai trechos significativos para busca de citacoes diretas/indiretas."""
    paragraph = format_raw_text(paragraph)
    sentences = _split_sentences(paragraph)
    if not sentences:
        return []

    ranked: List[Tuple[str, str, float]] = []
    quoted = re.findall(r'"([^"]{20,})"', paragraph, flags=REGEX_FLAGS)
    for q in quoted:
        txt = _clean_text(q)
        if txt:
            ranked.append((txt, "direta", 1000.0))

    for sentence in sentences:
        words = re.findall(r"\w+", sentence, flags=REGEX_FLAGS)
        if len(words) < 7:
            continue
        unique_ratio = len(set(w.lower() for w in words)) / max(1, len(words))
        length_score = min(len(words), 30) / 30.0
        ranked.append((_clean_text(sentence), "indireta", unique_ratio * 0.7 + length_score * 0.3))

    deduped: List[Tuple[str, str]] = []
    seen = set()
    for phrase, p_type, _ in sorted(ranked, key=lambda item: item[2], reverse=True):
        key = phrase.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append((phrase, p_type))
        if len(deduped) >= max_phrases:
            break
    return deduped


def _search_via_searxng(query: str, max_results: int) -> List[Dict[str, str]]:
    base_url = os.getenv("SEARCH_SEARXNG_URL", "").strip()
    if not base_url:
        return []

    try:
        response = requests.get(
            f"{base_url.rstrip('/')}/search",
            params={"q": query, "format": "json", "language": "pt-BR"},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException:
        return []

    results: List[Dict[str, str]] = []
    for item in payload.get("results", [])[:max_results]:
        results.append(
            {
                "engine": f"searxng:{item.get('engine', 'unknown')}",
                "title": str(item.get("title", "")).strip(),
                "url": str(item.get("url", "")).strip(),
                "snippet": _clean_text(str(item.get("content", ""))),
            }
        )
    return results


def _search_via_serper(query: str, max_results: int) -> List[Dict[str, str]]:
    api_key = os.getenv("SEARCH_API_KEY", "").strip()
    if not api_key:
        return []

    try:
        response = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": max_results, "hl": "pt-br", "gl": "br"},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException:
        return []

    results: List[Dict[str, str]] = []
    for item in payload.get("organic", [])[:max_results]:
        results.append(
            {
                "engine": "serper:google",
                "title": str(item.get("title", "")).strip(),
                "url": str(item.get("link", "")).strip(),
                "snippet": _clean_text(str(item.get("snippet", ""))),
            }
        )
    return results


def _search_via_tavily(query: str, max_results: int) -> List[Dict[str, str]]:
    api_key = os.getenv("SEARCH_API_KEY", "").strip()
    if not api_key:
        return []

    try:
        response = requests.post(
            "https://api.tavily.com/search",
            json={"api_key": api_key, "query": query, "max_results": max_results, "search_depth": "advanced"},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException:
        return []

    results: List[Dict[str, str]] = []
    for item in payload.get("results", [])[:max_results]:
        results.append(
            {
                "engine": "tavily",
                "title": str(item.get("title", "")).strip(),
                "url": str(item.get("url", "")).strip(),
                "snippet": _clean_text(str(item.get("content", ""))),
            }
        )
    return results


def _search_via_duckduckgo(query: str, max_results: int) -> List[Dict[str, str]]:
    try:
        response = requests.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
            timeout=8,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException:
        return []

    results: List[Dict[str, str]] = []
    abstract = _clean_text(str(payload.get("AbstractText", "")))
    if abstract:
        results.append(
            {
                "engine": "duckduckgo",
                "title": str(payload.get("Heading", "DuckDuckGo")).strip(),
                "url": str(payload.get("AbstractURL", "")).strip(),
                "snippet": abstract,
            }
        )

    for item in payload.get("RelatedTopics", [])[:max_results]:
        if not isinstance(item, dict):
            continue
        txt = _clean_text(str(item.get("Text", "")))
        if txt:
            results.append(
                {
                    "engine": "duckduckgo",
                    "title": "DuckDuckGo Related",
                    "url": str(item.get("FirstURL", "")).strip(),
                    "snippet": txt,
                }
            )
    return results[:max_results]


def _search_web(query: str, max_results: int = 5) -> List[Dict[str, str]]:
    provider = os.getenv("SEARCH_API_PROVIDER", "searxng").strip().lower()
    if provider == "serper":
        results = _search_via_serper(query, max_results)
    elif provider == "tavily":
        results = _search_via_tavily(query, max_results)
    else:
        results = _search_via_searxng(query, max_results)

    if not results:
        results = _search_via_duckduckgo(query, max_results)

    deduped: List[Dict[str, str]] = []
    seen = set()
    for item in results:
        key = (item.get("url", "").lower(), item.get("snippet", "").lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _cosine_similarity_score(text_a: str, text_b: str) -> float:
    if not text_a.strip() or not text_b.strip():
        return 0.0
    tfidf = TfidfVectorizer(ngram_range=(1, 2), lowercase=True).fit_transform([text_a, text_b])
    return float(cosine_similarity(tfidf[0:1], tfidf[1:2]).flatten()[0])


def _has_exact_phrase_overlap(phrase: str, text: str, ngram_size: int = 7) -> bool:
    phrase_words = re.findall(r"\w+", phrase.lower(), flags=REGEX_FLAGS)
    text_lower = text.lower()
    if len(phrase_words) < ngram_size:
        return False

    for idx in range(0, len(phrase_words) - ngram_size + 1):
        ngram = " ".join(phrase_words[idx : idx + ngram_size])
        if ngram in text_lower:
            return True
    return False


def _safe_json(raw_response: str) -> Optional[Dict[str, object]]:
    try:
        return json.loads(raw_response)
    except (TypeError, json.JSONDecodeError):
        match = re.search(r"\{.*\}", raw_response or "", flags=re.DOTALL | REGEX_FLAGS)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None


def _call_ollama_single(prompt: str, model: str, base_url: str) -> Dict[str, object]:

    try:
        response = requests.post(
            f"{base_url}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=25,
        )
        response.raise_for_status()
        payload = response.json()
        raw_response = str(payload.get("response", ""))
    except requests.RequestException:
        return {"model": model, "ok": False, "error": "indisponivel"}
    finally:
        gc.collect()

    data = _safe_json(raw_response)
    if not data:
        return {
            "model": model,
            "ok": False,
            "error": "json_invalido",
            "raw": raw_response[:300],
            "raw_response": raw_response[:3000],
        }

    return {
        "model": model,
        "ok": True,
        "veredito": str(data.get("veredito", data.get("plagio_probavel", "inconclusivo"))).strip(),
        "justificativa": str(data.get("justificativa", data.get("motivo", "sem justificativa"))).strip(),
        "confianca_ia": data.get("confianca_ia"),
        "sinais_detectados": data.get("sinais_detectados", []),
        "pensamento_forense": data.get("pensamento_forense", ""),
        "raw_response": raw_response[:3000],
    }


def _section_for_index(index: int, total: int) -> str:
    if total <= 1:
        return "Introducao"
    if index < max(1, total // 3):
        return "Introducao"
    if index < max(1, (2 * total) // 3):
        return "Desenvolvimento"
    return "Conclusao"


async def _fetch_and_score_urls(
    urls: List[str],
    paragraph: str,
    phrase: str,
    phrase_type: str,
) -> List[Dict[str, object]]:
    """Busca múltiplas URLs em paralelo e retorna resultados com scores."""
    url_texts = await fetch_urls_parallel(urls, timeout=10, max_chars=2000)
    
    scored_rows: List[Dict[str, object]] = []
    for url, scraped_text in url_texts.items():
        compare_text = scraped_text if scraped_text else ""
        score = _cosine_similarity_score(paragraph, format_raw_text(str(compare_text))) if compare_text else 0.0
        exact_overlap = _has_exact_phrase_overlap(phrase, str(compare_text)) if compare_text else False
        
        scored_rows.append({
            "url": url,
            "scraped_text": scraped_text,
            "score": score,
            "exact_overlap": exact_overlap,
        })
    
    return scored_rows


def analyze_paragraph_consensus(
    paragraph: str,
    base_url: str,
    qwen_model: str,
    llama_model: str,
) -> Dict[str, object]:
    """Executa consenso forense por paragrafo com prompt endurecido para IA sintetica.

    Prompt endurecido aplicado aos dois modelos:
    - foco em conectivos roboticos
    - foco em frases de tamanho uniforme
    - foco em estrutura excessivamente equilibrada
    - exigencia de JSON com confianca_ia numerica
    """
    strict_instruction = (
        "Voce e um perito forense rigoroso. Analise o texto em busca de padroes sinteticos. "
        "Se o texto apresentar conectivos roboticos (Ademais, Em suma, Por outro lado), "
        "frases de tamanho uniforme ou estrutura perfeitamente equilibrada, voce DEVE marcar como IA. "
        "Seja critico. Se detectar esses sinais, aumente a confianca_ia. "
        "Retorne APENAS JSON com as chaves: veredito, confianca_ia, justificativa, sinais_detectados, pensamento_forense."
    )

    qwen_prompt = build_structured_prompt(
        instruction=strict_instruction,
        sections={
            "PAPEL_MODELO": "Qwen 2.5 - foco em fluidez e gramatica do Portugues",
            "TEXTO": paragraph,
        },
    )
    qwen_out = _call_ollama_single(qwen_prompt, qwen_model, base_url)

    llama_prompt = build_structured_prompt(
        instruction=strict_instruction,
        sections={
            "PAPEL_MODELO": "Llama 3.1 - foco em consistencia logica e factual",
            "TEXTO": paragraph,
            "INSIGHT_QWEN": (
                f"veredito={qwen_out.get('veredito', 'n/a')}; "
                f"confianca_ia={qwen_out.get('confianca_ia', 'n/a')}; "
                f"justificativa={qwen_out.get('justificativa', '')}"
            ),
        },
    )
    llama_out = _call_ollama_single(llama_prompt, llama_model, base_url)

    def _model_confidence(item: Dict[str, object]) -> float:
        raw_conf = item.get("confianca_ia")
        if isinstance(raw_conf, (float, int)):
            return max(0.0, min(1.0, float(raw_conf)))
        verdict = str(item.get("veredito", "")).lower()
        if "ia" in verdict or "plagio" in verdict or "alto" in verdict:
            return 0.85
        if "possivel" in verdict or "medio" in verdict:
            return 0.60
        if "improvavel" in verdict or "baixo" in verdict or "humano" in verdict:
            return 0.20
        return 0.45

    qwen_prob = _model_confidence(qwen_out)
    llama_prob = _model_confidence(llama_out)
    consensus_prob = (qwen_prob + llama_prob) / 2.0
    consensus_label = "provavel_ia" if consensus_prob >= 0.60 else "indefinido" if consensus_prob >= 0.45 else "provavel_humano"

    return {
        "qwen": qwen_out,
        "llama": llama_out,
        "qwen_probability": qwen_prob,
        "llama_probability": llama_prob,
        "consensus_probability": consensus_prob,
        "consensus_label": consensus_label,
    }


def _batch_progress_by_section(
    model_pos: int,
    model_name: str,
    done: int,
    total: int,
    item_index: int,
    model_progress_callback: ModelProgressCallback,
) -> None:
    if not model_progress_callback:
        return
    section = _section_for_index(item_index, total)
    model_progress_callback(model_pos, model_name, done, total, section)


def _batch_plagiarism_llm_consensus(
    paragraphs: List[str],
    plagiarism_hits: List[PlagiarismHit],
    status_callback: StatusCallback,
    model_progress_callback: ModelProgressCallback,
    debug_callback: DebugCallback,
) -> None:
    if not plagiarism_hits:
        return

    base_url = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434")
    qwen_model = os.getenv("OLLAMA_SECONDARY_MODEL", "qwen2.5:latest")
    llama_model = os.getenv("OLLAMA_PRIMARY_MODEL", os.getenv("OLLAMA_MODEL", "llama3.1:8b"))

    if status_callback:
        status_callback("🤖 [Modelo 1/2] Qwen analisando lote de trechos suspeitos...")

    qwen_outputs: Dict[int, Dict[str, object]] = {}
    total_hits = len(plagiarism_hits)
    for idx, hit in enumerate(plagiarism_hits):
        paragraph = paragraphs[hit.paragraph_index]
        qwen_prompt = build_structured_prompt(
            instruction=(
                "Voce analisa fluidez e gramatica em PT-BR. "
                "Retorne JSON com chaves obrigatorias: veredito, justificativa. "
                "Veredito permitido: plagio, possivel_plagio, improvavel."
            ),
            sections={
                "PARAGRAFO": paragraph,
                "TRECHO_SUSPEITO": hit.phrase,
                "TIPO_DE_TRECHO": hit.phrase_type,
                "FONTE_WEB": hit.source_excerpt,
                "SIMILARIDADE": f"{hit.similarity:.4f}",
            },
        )
        qwen_outputs[idx] = _call_ollama_single(qwen_prompt, qwen_model, base_url)
        _batch_progress_by_section(1, "Qwen", idx + 1, total_hits, hit.paragraph_index, model_progress_callback)

    if debug_callback:
        debug_callback("Liberando memoria para carregar Llama 3.1...")
    gc.collect()

    if status_callback:
        status_callback("🧠 [Modelo 2/2] Llama verificando consistencia logica com apoio dos insights do Qwen...")

    for idx, hit in enumerate(plagiarism_hits):
        paragraph = paragraphs[hit.paragraph_index]
        qwen_insight = qwen_outputs.get(idx, {})
        llama_prompt = build_structured_prompt(
            instruction=(
                "Voce analisa fatos e logica textual. "
                "Retorne JSON com chaves obrigatorias: veredito, justificativa. "
                "Veredito permitido: plagio, possivel_plagio, improvavel."
            ),
            sections={
                "PARAGRAFO": paragraph,
                "TRECHO_SUSPEITO": hit.phrase,
                "TIPO_DE_TRECHO": hit.phrase_type,
                "FONTE_WEB": hit.source_excerpt,
                "SIMILARIDADE": f"{hit.similarity:.4f}",
                "INSIGHT_QWEN": f"{qwen_insight.get('veredito', 'n/a')} | {qwen_insight.get('justificativa', '')}",
            },
        )
        llama_out = _call_ollama_single(llama_prompt, llama_model, base_url)

        qwen_out = qwen_outputs.get(idx, {"model": qwen_model, "ok": False, "error": "sem_saida"})
        confidence = "Inconsistente"
        if qwen_out.get("ok") and llama_out.get("ok") and qwen_out.get("veredito") == llama_out.get("veredito"):
            confidence = "Critico"

        hit.llm_consensus = {"confidence": confidence, "qwen": qwen_out, "llama": llama_out}
        _batch_progress_by_section(2, "Llama", idx + 1, total_hits, hit.paragraph_index, model_progress_callback)


def _llm_ai_probability_per_paragraph(
    paragraphs: List[str],
    status_callback: StatusCallback,
    model_progress_callback: ModelProgressCallback,
    debug_callback: DebugCallback,
) -> List[Dict[str, object]]:
    """Gera score de IA por paragrafo com consenso forense endurecido."""
    base_url = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434")
    qwen_model = os.getenv("OLLAMA_SECONDARY_MODEL", "qwen2.5:latest")
    llama_model = os.getenv("OLLAMA_PRIMARY_MODEL", os.getenv("OLLAMA_MODEL", "llama3.1:8b"))

    rows: List[Dict[str, object]] = []
    total = max(1, len(paragraphs))
    for idx, paragraph in enumerate(paragraphs):
        if status_callback:
            status_callback(
                "Voce e um perito forense rigoroso. Rodando consenso IA com prompt endurecido..."
            )

        consensus = analyze_paragraph_consensus(
            paragraph=paragraph,
            base_url=base_url,
            qwen_model=qwen_model,
            llama_model=llama_model,
        )

        rows.append(
            {
            "paragraph_index": idx,
            "qwen_probability": float(consensus["qwen_probability"]),
            "llama_probability": float(consensus["llama_probability"]),
            "qwen_raw_response": str(consensus["qwen"].get("raw_response", "")),
            "llama_raw_response": str(consensus["llama"].get("raw_response", "")),
            "consensus_probability": float(consensus["consensus_probability"]),
            "consensus_label": str(consensus["consensus_label"]),
            }
        )
        _batch_progress_by_section(2, "Llama", idx + 1, total, idx, model_progress_callback)

    return rows


def detect_plagiarism(
    paragraphs: List[str],
    threshold: float = 0.58,
    weak_threshold: float = 0.42,
    status_callback: StatusCallback = None,
    progress_callback: ProgressCallback = None,
) -> Tuple[float, List[PlagiarismHit], Dict[str, object]]:
    """Detecta plagio web com busca profunda, scraping paralelo e consenso de LLMs."""
    hits: List[PlagiarismHit] = []
    paragraph_best_scores: List[float] = []

    search_stats: Dict[str, object] = {
        "provider": os.getenv("SEARCH_API_PROVIDER", "searxng").lower(),
        "queries_executed": 0,
        "web_results_total": 0,
        "scraped_pages": 0,
        "paragraphs_skipped": 0,
        "phrases_cached": 0,
    }

    total_paragraphs = max(1, len(paragraphs))

    for paragraph_index, paragraph in enumerate(paragraphs):
        # Skip logic: ignorar parágrafos muito curtos
        if _skip_paragraph(paragraph):
            search_stats["paragraphs_skipped"] = int(search_stats["paragraphs_skipped"]) + 1
            if progress_callback:
                progress_callback((paragraph_index + 1) / total_paragraphs)
            continue
        
        if status_callback:
            status_callback(
                f"🌐 Pesquisando paragrafo {paragraph_index + 1}/{total_paragraphs} no Google/Brave via Selenium..."
            )

        paragraph = format_raw_text(paragraph)
        phrases = _extract_significant_phrases(paragraph)
        if not phrases:
            if progress_callback:
                progress_callback((paragraph_index + 1) / total_paragraphs)
            continue

        paragraph_hash = _paragraph_hash(paragraph)
        local_sources = [
            {
                "engine": "local_corpus",
                "title": "Base local simulada",
                "url": "",
                "snippet": sample,
                "scraped": sample,
            }
            for sample in SIMULATED_CORPUS
        ]

        scored_rows: List[Dict[str, object]] = []

        for phrase, phrase_type in phrases:
            phrase_hash = _paragraph_hash(phrase)
            
            # Verificar cache de buscas duplicadas
            if _has_searched_phrase(paragraph_hash, phrase_hash):
                search_stats["phrases_cached"] = int(search_stats["phrases_cached"]) + 1
                continue
            
            _mark_phrase_searched(paragraph_hash, phrase_hash)
            
            query = format_raw_text(phrase)[:220]
            web_results = _search_web(query, max_results=5)
            search_stats["queries_executed"] = int(search_stats["queries_executed"]) + 1
            search_stats["web_results_total"] = int(search_stats["web_results_total"]) + len(web_results)

            # Busca assíncrona de múltiplas URLs em paralelo
            urls_to_fetch = [item.get("url", "") for item in web_results if item.get("url", "").strip()]
            if urls_to_fetch:
                try:
                    async_results = asyncio.run(_fetch_and_score_urls(
                        urls_to_fetch,
                        paragraph,
                        phrase,
                        phrase_type,
                    ))
                    
                    for web_item, async_result in zip(web_results, async_results):
                        scraped_text = async_result.get("scraped_text", "")
                        if scraped_text:
                            search_stats["scraped_pages"] = int(search_stats["scraped_pages"]) + 1
                        
                        score = async_result.get("score", 0.0)
                        exact_overlap = async_result.get("exact_overlap", False)
                        
                        scored_rows.append({
                            "phrase": phrase,
                            "phrase_type": phrase_type,
                            "source": {**web_item, "scraped": scraped_text},
                            "score": score,
                            "exact_overlap": exact_overlap,
                            "classification": "citacao_direta" if exact_overlap else "citacao_indireta",
                        })
                except Exception:
                    # Fallback: usar método síncrono se async falhar
                    for item in web_results:
                        scraped_text = ""
                        try:
                            scraped_text = fetch_page_text_selenium(item.get("url", ""))
                        except Exception:
                            scraped_text = ""
                        
                        if scraped_text:
                            search_stats["scraped_pages"] = int(search_stats["scraped_pages"]) + 1
                        
                        compare_text = scraped_text or item.get("snippet", "")
                        score = _cosine_similarity_score(paragraph, format_raw_text(str(compare_text)))
                        exact_overlap = _has_exact_phrase_overlap(phrase, str(compare_text))
                        
                        scored_rows.append({
                            "phrase": phrase,
                            "phrase_type": phrase_type,
                            "source": {**item, "scraped": scraped_text},
                            "score": score,
                            "exact_overlap": exact_overlap,
                            "classification": "citacao_direta" if exact_overlap else "citacao_indireta",
                        })

        # Avaliar contra corpus local
        for source in local_sources:
            compare_text = source.get("scraped") or source.get("snippet", "")
            score = _cosine_similarity_score(paragraph, format_raw_text(str(compare_text)))
            exact_overlap = _has_exact_phrase_overlap(phrase, str(compare_text)) if phrases else False
            scored_rows.append({
                "phrase": phrases[0][0] if phrases else "",
                "phrase_type": phrases[0][1] if phrases else "indireta",
                "source": source,
                "score": score,
                "exact_overlap": exact_overlap,
                "classification": "citacao_direta" if exact_overlap else "citacao_indireta",
            })

        if not scored_rows:
            continue

        scored_rows.sort(key=lambda row: float(row["score"]), reverse=True)
        best = scored_rows[0]
        best_score = float(best["score"])
        paragraph_best_scores.append(best_score)

        if best_score < threshold:
            continue

        source = best["source"]
        supporting: List[Dict[str, object]] = []
        for item in scored_rows[:5]:
            if float(item["score"]) < weak_threshold:
                continue
            src = item["source"]
            supporting.append(
                {
                    "engine": src.get("engine", "unknown"),
                    "title": src.get("title", ""),
                    "url": src.get("url", ""),
                    "similarity": float(item["score"]),
                }
            )

        hits.append(
            PlagiarismHit(
                paragraph_index=paragraph_index,
                phrase=str(best["phrase"]),
                phrase_type=str(best["phrase_type"]),
                similarity=best_score,
                source_title=str(source.get("title", "")),
                source_url=str(source.get("url", "")),
                source_engine=str(source.get("engine", "unknown")),
                source_excerpt=str(source.get("snippet", ""))[:1200],
                scraped_text=str(source.get("scraped", ""))[:2000],
                exact_phrase_match=bool(best["exact_overlap"]),
                classification=str(best["classification"]),
                llm_consensus={"confidence": "Inconsistente", "qwen": {}, "llama": {}},
                supporting_matches=supporting,
            )
        )

        if progress_callback:
            progress_callback((paragraph_index + 1) / total_paragraphs)

    percentage = (sum(paragraph_best_scores) / len(paragraph_best_scores) * 100.0) if paragraph_best_scores else 0.0
    return min(100.0, percentage), hits, search_stats


def _paragraph_uniformity_score(paragraph: str) -> float:
    sentences = _split_sentences(paragraph)
    if len(sentences) < 3:
        return 0.0

    lengths = [_word_count(s) for s in sentences if _word_count(s) > 0]
    if len(lengths) < 3:
        return 0.0

    mean_len = statistics.mean(lengths)
    std_len = statistics.pstdev(lengths)
    rel_std = std_len / (mean_len + 1e-6)
    return max(0.0, min(1.0, 1.0 - rel_std))


def _paragraph_repetition_score(paragraph: str) -> float:
    words = [w.lower() for w in re.findall(r"\b\w+\b", paragraph, flags=REGEX_FLAGS)]
    if len(words) < 8:
        return 0.0
    unique_ratio = len(set(words)) / len(words)
    return max(0.0, min(1.0, 1.0 - unique_ratio))


def fast_ai_artifact_detection(text_block: str) -> Dict[str, object]:
    """Triagem regex de alta performance com score cumulativo por evidência."""
    cleaned = format_raw_text(text_block)
    words = re.findall(r"\b\w+\b", cleaned, flags=REGEX_FAST_FLAGS)
    word_count = max(1, len(words))

    evidence: List[str] = []
    matched_rules: List[Dict[str, object]] = []
    category_counts = {
        "critical_meta": 0,
        "markdown_latex": 0,
        "robotic_connectors": 0,
        "stereotyped_vocab": 0,
    }

    heuristic_score = 0
    critical_triggered = False
    for rule in CRITICAL_META_RULES:
        count = sum(1 for _ in rule.pattern.finditer(cleaned))
        if count <= 0:
            continue
        critical_triggered = True
        category_counts[rule.category] += count
        matched_rules.append(
            {
                "rule": rule.name,
                "category": rule.category,
                "count": count,
                "weight": rule.weight,
                "score_delta": 100,
            }
        )
        evidence.append(f"Gatilho crítico: {rule.name} ({count} ocorrência(s))")

    if critical_triggered:
        heuristic_score = 100

    for rule in NON_VOCAB_WEIGHTED_HEURISTIC_RULES:
        count = sum(1 for _ in rule.pattern.finditer(cleaned))
        if count <= 0:
            continue
        category_counts[rule.category] += count
        delta = rule.weight * count
        if not critical_triggered:
            heuristic_score += delta
        matched_rules.append(
            {
                "rule": rule.name,
                "category": rule.category,
                "count": count,
                "weight": rule.weight,
                "score_delta": delta,
            }
        )
        evidence.append(f"{rule.name} ({count} ocorrência(s), +{delta})")

    vocab_matches_total = 0
    vocab_pending: List[Tuple[HeuristicRule, int]] = []
    for rule in STEREOTYPED_VOCAB_RULES:
        count = sum(1 for _ in rule.pattern.finditer(cleaned))
        if count <= 0:
            continue
        category_counts[rule.category] += count
        vocab_matches_total += count
        vocab_pending.append((rule, count))

    vocab_context_signal = (
        category_counts["markdown_latex"] > 0
        or category_counts["robotic_connectors"] > 0
    )
    apply_vocab_score = vocab_context_signal or vocab_matches_total >= 2

    for rule, count in vocab_pending:
        delta = rule.weight * count
        if apply_vocab_score and not critical_triggered:
            heuristic_score += delta
        matched_rules.append(
            {
                "rule": rule.name,
                "category": rule.category,
                "count": count,
                "weight": rule.weight,
                "score_delta": delta if apply_vocab_score else 0,
                "cooccurrence_applied": apply_vocab_score,
            }
        )
        if apply_vocab_score:
            evidence.append(f"{rule.name} ({count} ocorrência(s), +{delta})")
        else:
            evidence.append(
                f"{rule.name} ({count} ocorrência(s), ignorado por vocabulário isolado)"
            )

    heuristic_score = min(100, heuristic_score)
    density_per_100_words = (sum(category_counts.values()) / word_count) * 100.0
    is_suspicious = heuristic_score > 50

    return {
        "word_count": word_count,
        "markdown_hits": category_counts["markdown_latex"],
        "artificial_list_hits": 0,
        "latex_hits": 0,
        "connector_hits": category_counts["robotic_connectors"],
        "total_hits": int(sum(category_counts.values())),
        "density_per_100_words": round(density_per_100_words, 4),
        "weighted_score": round(heuristic_score / 100.0, 4),
        "heuristic_score": heuristic_score,
        "critical_triggered": critical_triggered,
        "category_counts": category_counts,
        "is_suspicious": is_suspicious,
        "reasons": evidence,
        "evidence": evidence,
        "matched_rules": matched_rules,
    }


def detect_ai_patterns(paragraphs: List[str]) -> Tuple[float, List[AIHit], Dict[str, float]]:
    hits: List[AIHit] = []
    full_text = "\n\n".join(paragraphs)

    token_hits = 0
    connector_repetition = 0
    uniformity_scores: List[float] = []
    repetition_scores: List[float] = []

    for idx, paragraph in enumerate(paragraphs):
        p_lower = paragraph.lower()
        reasons: List[str] = []

        for token in AI_PATTERN_TOKENS:
            count = p_lower.count(token)
            token_hits += count
            connector_repetition += count
            if count > 0:
                reasons.append(f"Conectivo recorrente detectado: '{token}'")

        if "--" in paragraph:
            reasons.append("Presenca de traco duplo '--' incomum em texto academico")
        if paragraph.count("**") % 2 != 0 or re.search(r"^#{1,6}\S", paragraph, flags=REGEX_FLAGS):
            reasons.append("Padrao de Markdown inconsistente")

        uniformity = _paragraph_uniformity_score(paragraph)
        repetition = _paragraph_repetition_score(paragraph)
        uniformity_scores.append(uniformity)
        repetition_scores.append(repetition)

        if uniformity >= 0.72:
            reasons.append("Frases com alta uniformidade de tamanho")
        if repetition >= 0.45:
            reasons.append("Repeticao lexical acima do esperado")
        if reasons:
            hits.append(AIHit(paragraph_index=idx, reasons=sorted(set(reasons))))

    sentence_count = max(1, len(_split_sentences(full_text)))
    token_density = min(1.0, token_hits / sentence_count)
    connector_ratio = min(1.0, connector_repetition / sentence_count)
    avg_uniformity = sum(uniformity_scores) / len(uniformity_scores) if uniformity_scores else 0.0
    avg_repetition = sum(repetition_scores) / len(repetition_scores) if repetition_scores else 0.0

    # Recalibracao anti-falso-negativo: mais peso em uniformidade e conectivos.
    ai_score = (
        0.18 * token_density
        + 0.42 * avg_uniformity
        + 0.10 * avg_repetition
        + 0.30 * connector_ratio
    )

    # Escalada agressiva quando padroes sinteticos ficam claros.
    if avg_uniformity >= 0.78:
        ai_score += 0.18
    if connector_ratio >= 0.28:
        ai_score += 0.10
    if avg_uniformity >= 0.70 and connector_ratio >= 0.22:
        ai_score += 0.12

    metrics = {
        "token_density": token_density,
        "uniformity": avg_uniformity,
        "repetition": avg_repetition,
        "connector_ratio": connector_ratio,
    }
    return min(100.0, ai_score * 100.0), hits, metrics


def _normalize_for_match(text: str) -> str:
    no_accents = "".join(
        ch for ch in unicodedata.normalize("NFD", text) if unicodedata.category(ch) != "Mn"
    )
    cleaned = re.sub(r"[^\w\s]", " ", no_accents, flags=REGEX_FLAGS)
    return re.sub(r"\s+", " ", cleaned, flags=REGEX_FLAGS).strip().lower()


def _is_reference_section_header(text: str) -> bool:
    return bool(
        re.match(
            r"^(?:\d+\.?\s*)?(REFER[ÊE]NCIAS|BIBLIOGRAFIA|REFERENCES)(?:\s+BIBLIOGR[ÁA]FICAS)?\s*:?$",
            text.strip(),
            flags=REGEX_FLAGS,
        )
    )


def _looks_like_reference_line(text: str) -> bool:
    txt = text.strip()
    if len(txt) < 20:
        return False
    has_author = bool(
        re.search(r"^[A-ZÁÀÂÃÉÈÊÍÌÎÓÒÔÕÚÙÛÇ'\-\s]+,\s*[A-Z]", txt, flags=re.UNICODE)
    )
    has_year = bool(re.search(r"\b(19\d{2}|20\d{2})\b", txt, flags=re.UNICODE))
    return has_author and has_year and "." in txt


def _reference_paragraph_indices(paragraphs: List[str]) -> Set[int]:
    """Identifica índices de referências via regex flexível e fallback ABNT."""
    start = -1
    for idx, paragraph in enumerate(paragraphs):
        if _is_reference_section_header(paragraph):
            start = idx + 1
            break

    if start >= 0:
        return {i for i in range(start, len(paragraphs)) if paragraphs[i].strip()}

    trailing: Set[int] = set()
    for idx in range(len(paragraphs) - 1, -1, -1):
        p = paragraphs[idx].strip()
        if not p:
            if trailing:
                break
            continue
        if _looks_like_reference_line(p):
            trailing.add(idx)
        elif trailing:
            break
    return trailing


def detect_synthetic_perfection(paragraph: str) -> Dict[str, object]:
    """Heurística de baixa perplexidade: perfeição gramatical suspeita eleva risco de IA."""
    sentences = _split_sentences(paragraph)
    words = re.findall(r"\b\w+\b", paragraph, flags=REGEX_FLAGS)
    if not words:
        return {
            "score": 0.0,
            "is_suspicious": False,
            "signals": [],
            "message": "Sem texto para avaliar perfeição sintética.",
        }

    typo_count = len(re.findall(r"\b\w*[0-9_]\w*\b", paragraph, flags=REGEX_FLAGS))
    punctuation_noise = len(re.findall(r"[!?]{2,}|\.{3,}", paragraph, flags=REGEX_FLAGS))
    regional_count = sum(paragraph.lower().count(tok) for tok in REGIONAL_MA_TOKENS)
    uniformity = _paragraph_uniformity_score(paragraph)
    repetition = _paragraph_repetition_score(paragraph)

    clean_factor = 1.0 if typo_count == 0 and punctuation_noise == 0 else 0.0
    regional_absence = 1.0 if regional_count == 0 else 0.0

    score = (
        0.38 * uniformity
        + 0.26 * clean_factor
        + 0.18 * regional_absence
        + 0.18 * max(0.0, 1.0 - repetition)
    )

    signals: List[str] = []
    if uniformity >= 0.82:
        signals.append("Baixa perplexidade: ritmo excessivamente uniforme")
    if clean_factor >= 1.0:
        signals.append("Estilo excessivamente polido para autoria humana padrão")
    if regional_absence >= 1.0 and len(words) >= 40:
        signals.append("Ausência de marcas idiomáticas regionais em texto longo")

    is_suspicious = score >= 0.72
    return {
        "score": float(max(0.0, min(1.0, score))),
        "is_suspicious": is_suspicious,
        "signals": signals,
        "message": "🕵️ Desconfiando da perfeição gramatical..." if is_suspicious else "Texto com variações humanas detectáveis.",
    }


def _extract_reference_title_authors(reference: str) -> Tuple[str, str]:
    quoted = re.findall(r'"([^"]+)"', reference, flags=REGEX_FLAGS)
    if quoted:
        title = quoted[0]
    else:
        chunks = [chunk.strip() for chunk in reference.split(".") if chunk.strip()]
        title = max(chunks[1:], key=len) if len(chunks) >= 2 else (chunks[0] if chunks else reference)
    authors = reference.split(".", maxsplit=1)[0].strip()
    return format_raw_text(title), format_raw_text(authors)


def _find_doi_quick(title: str, authors: str) -> str:
    if not title:
        return "Nao encontrado"
    try:
        response = requests.get(
            "https://api.crossref.org/works",
            params={"query.bibliographic": f"{title} {authors}", "rows": 3},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        for item in payload.get("message", {}).get("items", []):
            doi = str(item.get("DOI", "")).strip()
            if doi:
                return doi
    except requests.RequestException:
        return "Nao encontrado"
    return "Nao encontrado"


async def _audit_reference_web_async(
    refs: List[Dict[str, object]],
    status_callback: StatusCallback = None,
    progress_hook: Optional[Callable[[], None]] = None,
) -> List[Dict[str, object]]:
    """Dispara validacao web de referencias em paralelo via asyncio.gather."""

    async def _one(item: Dict[str, object]) -> Dict[str, object]:
        reference = format_raw_text(str(item.get("reference", "")))
        paragraph_index = int(item.get("paragraph_index", -1))
        title, authors = _extract_reference_title_authors(reference)

        results = await asyncio.to_thread(_search_web, f'"{title}"', 5)

        normalized_title = _normalize_for_match(title)
        exact_found = False
        for result in results:
            title_candidate = _normalize_for_match(str(result.get("title", "")))
            snippet_candidate = _normalize_for_match(str(result.get("snippet", "")))
            if normalized_title and (
                normalized_title in title_candidate
                or normalized_title in snippet_candidate
                or title_candidate in normalized_title
            ):
                exact_found = True
                break

        found_google = "Sim" if exact_found else "Nao"
        top_source = results[0].get("url", "") if results else ""
        doi = await asyncio.to_thread(_find_doi_quick, title, authors)

        return {
            "paragraph_index": paragraph_index,
            "reference": reference,
            "title": title,
            "authors": authors,
            "found_google": found_google,
            "status_web": "Encontrado" if found_google == "Sim" else "Nao encontrado",
            "exact_title_match": exact_found,
            "doi": doi,
            "top_source": top_source,
            "pipeline_status": "auditado_web",
        }

    async def _indexed(idx: int, item: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
        return idx, await _one(item)

    tasks = [_indexed(idx, item) for idx, item in enumerate(refs)]
    if not tasks:
        return []

    ordered: List[Optional[Dict[str, object]]] = [None] * len(tasks)
    for done in asyncio.as_completed(tasks):
        idx, row = await done
        ordered[idx] = row
        if progress_hook:
            progress_hook()

    return [row for row in ordered if row is not None]


def _reference_sanity_check(
    web_rows: List[Dict[str, object]],
    status_callback: StatusCallback = None,
    reference_progress_callback: ReferenceProgressCallback = None,
) -> List[Dict[str, object]]:
    """Qwen avalia todas as referências; limpa VRAM; Llama faz contra-parecer das não encontradas."""
    base_url = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434")
    qwen_model = os.getenv("OLLAMA_SECONDARY_MODEL", "qwen2.5:latest")
    llama_model = os.getenv("OLLAMA_PRIMARY_MODEL", os.getenv("OLLAMA_MODEL", "llama3.1:8b"))

    final_rows: List[Dict[str, object]] = []
    llama_targets = [row for row in web_rows if str(row.get("found_google", "Nao")) == "Nao"]
    llama_done = 0

    for idx, row in enumerate(web_rows):
        reference = str(row.get("reference", ""))
        found_google = str(row.get("found_google", "Nao"))
        doi = str(row.get("doi", "Nao encontrado"))
        exact_title_match = bool(row.get("exact_title_match", False))

        if reference_progress_callback:
            reference_progress_callback(idx + 1, len(web_rows), "qwen")
        if status_callback:
            status_callback(f"🕵️ Auditando referência {idx + 1}/{len(web_rows)} com Qwen...")

        qwen_prompt = build_structured_prompt(
            instruction=(
                "Você é um bibliotecário verificador rigoroso. Analise esta citação. "
                "1. O autor é uma autoridade real no tema? "
                "2. O título segue a lógica de publicações reais ou parece uma combinação genérica de palavras-chave? "
                "3. A data é consistente com a indexação atual? "
                "Retorne APENAS JSON com as chaves: alucinada, analise_bibliografica, veredito."
            ),
            sections={"CITACAO": reference},
        )
        qwen = _call_ollama_single(qwen_prompt, qwen_model, base_url)
        gc.collect()

        llama: Dict[str, object] = {
            "ok": True,
            "veredito": "offline_plausivel",
            "justificativa": "Obra localizada na web; contra-parecer Llama não necessário.",
        }
        if found_google != "Sim":
            llama_done += 1
            if reference_progress_callback:
                reference_progress_callback(llama_done, max(1, len(llama_targets)), "llama")
            if status_callback:
                status_callback(f"Auditando obra individual {llama_done} com Llama...")

            title = str(row.get("title", ""))
            authors = str(row.get("authors", ""))

            llama_prompt = build_structured_prompt(
                instruction=(
                    "A obra não foi localizada na web. Julgue se parece alucinação de IA ou documento offline legítimo. "
                    "Retorne APENAS JSON com as chaves: veredito, justificativa. "
                    "Veredito permitido: alucinacao, offline_plausivel, inconclusivo."
                ),
                sections={
                    "REFERENCIA": reference,
                    "TITULO": title,
                    "AUTORES": authors,
                    "DOI": doi,
                },
            )
            llama = _call_ollama_single(llama_prompt, llama_model, base_url)
            gc.collect()

        qwen_veredito = str(qwen.get("veredito", "")).lower()
        llama_veredito = str(llama.get("veredito", "")).lower()

        # Regra de Ouro (Search-First veto): sem match exato, nunca pode virar Safe.
        if not exact_title_match:
            if status_callback:
                status_callback("🌐 Obra não localizada na Web, marcando como suspeita...")
            status = "dubious"
            veredito_final = "Reference (Duvidosa)"
            confirmed_hallucination = True
        elif found_google == "Sim" or doi != "Nao encontrado":
            status = "ok"
            veredito_final = "Referência Plausível"
            confirmed_hallucination = False
        elif "alucinacao" in llama_veredito or "duvid" in qwen_veredito:
            status = "dubious"
            veredito_final = "Reference (Duvidosa)"
            confirmed_hallucination = True
        else:
            status = "unknown"
            veredito_final = "Inconclusivo (possível offline)"
            confirmed_hallucination = False

        final_rows.append(
            {
                **row,
                "status": status,
                "veredito_final": veredito_final,
                "confirmed_hallucination": confirmed_hallucination,
                "qwen_format": str(qwen.get("justificativa", "")),
                "llm_parecer": str(llama.get("justificativa", "")),
                "llm_consensus": {"qwen": qwen, "llama": llama},
                "pipeline_status": "concluido",
            }
        )

    return final_rows


async def async_verify_bibliography(
    paragraphs: List[str],
    status_callback: StatusCallback = None,
) -> List[Dict[str, object]]:
    """Executa auditoria bibliográfica completa com Search-First e veto de Safe."""
    ref_indices = _reference_paragraph_indices(paragraphs)
    refs = [
        {"paragraph_index": idx, "reference": paragraphs[idx]}
        for idx in sorted(ref_indices)
        if paragraphs[idx].strip()
    ]

    web_rows = await _audit_reference_web_async(refs, status_callback=None)
    return await asyncio.to_thread(_reference_sanity_check, web_rows, status_callback)


def analyze_document(
    paragraphs: List[str],
    status_callback: StatusCallback = None,
    enable_deep_ai: bool = False,
) -> Dict[str, object]:
    """Executa pipeline de analise completa com retorno de dados para dashboards."""
    progress_callback: ProgressCallback = None
    model_progress_callback: ModelProgressCallback = None
    debug_callback: DebugCallback = None
    reference_progress_callback: ReferenceProgressCallback = None
    if status_callback:
        status_callback("🔍 Extraindo e formatando texto do documento...")

    # Permite passar callback de progresso via atributo anexado dinamicamente pelo chamador.
    if hasattr(status_callback, "progress_callback"):
        progress_callback = getattr(status_callback, "progress_callback")
    if hasattr(status_callback, "model_progress_callback"):
        model_progress_callback = getattr(status_callback, "model_progress_callback")
    if hasattr(status_callback, "debug_callback"):
        debug_callback = getattr(status_callback, "debug_callback")
    if hasattr(status_callback, "reference_progress_callback"):
        reference_progress_callback = getattr(status_callback, "reference_progress_callback")

    normalized_paragraphs: List[str] = []
    for paragraph in paragraphs:
        normalized = format_raw_text(paragraph)
        if normalized:
            normalized_paragraphs.append(normalized)

    paragraphs = normalized_paragraphs

    extracted_refs = extract_reference_candidates(paragraphs)
    reference_checks_preliminary = [
        {
            "paragraph_index": int(item.get("paragraph_index", -1)),
            "reference": str(item.get("reference", "")),
            "status": "auditando",
            "found_google": "Auditando...",
            "doi": "Auditando...",
            "veredito_final": "Auditando...",
            "pipeline_status": "auditando",
            "confirmed_hallucination": False,
        }
        for item in extracted_refs
    ]

    web_audit_future: Optional[concurrent.futures.Future] = None
    audit_executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
    reference_progress_state: Dict[str, int] = {
        "web_done": 0,
        "web_total": len(extracted_refs),
    }

    def _web_progress_hook() -> None:
        reference_progress_state["web_done"] = int(reference_progress_state["web_done"]) + 1

    if extracted_refs:
        if status_callback:
            status_callback("🔎 Disparando auditoria assíncrona de referências em segundo plano...")
        audit_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        web_audit_future = audit_executor.submit(
            lambda: asyncio.run(
                _audit_reference_web_async(
                    extracted_refs,
                    status_callback=None,
                    progress_hook=_web_progress_hook,
                )
            )
        )

    if progress_callback:
        progress_callback(0.05)

    fast_heuristic_future: Optional[concurrent.futures.Future] = None
    fast_heuristic_executor: Optional[concurrent.futures.ThreadPoolExecutor] = None

    if status_callback:
        status_callback("⚡ Iniciando triagem heurística rápida em paralelo com a busca web...")

    def _run_fast_heuristics() -> Tuple[List[Dict[str, object]], List[Dict[str, object]]]:
        rows: List[Dict[str, object]] = []
        hits: List[Dict[str, object]] = []
        for idx, paragraph in enumerate(paragraphs):
            row = fast_ai_artifact_detection(paragraph)
            normalized_row = {"paragraph_index": idx, **row}
            rows.append(normalized_row)
            if bool(row.get("is_suspicious", False)):
                hits.append(normalized_row)
        return rows, hits

    fast_heuristic_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    fast_heuristic_future = fast_heuristic_executor.submit(_run_fast_heuristics)

    plagiarism_percentage, plagiarism_hits, search_stats = detect_plagiarism(
        paragraphs,
        status_callback=status_callback,
        progress_callback=(
            (lambda x: progress_callback(min(0.05 + (x * 0.65), 0.70))) if progress_callback else None
        ),
    )

    fast_heuristic_rows: List[Dict[str, object]] = []
    fast_heuristic_hits: List[Dict[str, object]] = []
    if fast_heuristic_future:
        try:
            fast_heuristic_rows, fast_heuristic_hits = fast_heuristic_future.result(timeout=90)
        except Exception:  # noqa: BLE001
            fast_heuristic_rows, fast_heuristic_hits = _run_fast_heuristics()
        finally:
            if fast_heuristic_executor:
                fast_heuristic_executor.shutdown(wait=False)

    if progress_callback:
        progress_callback(0.75)

    if status_callback and enable_deep_ai:
        status_callback("🤖 Preparando processamento em lote por modelo...")

    if enable_deep_ai:
        _batch_plagiarism_llm_consensus(
            paragraphs=paragraphs,
            plagiarism_hits=plagiarism_hits,
            status_callback=status_callback,
            model_progress_callback=model_progress_callback,
            debug_callback=debug_callback,
        )
    else:
        for hit in plagiarism_hits:
            hit.llm_consensus = {
                "confidence": "Análise profunda desativada pelo usuário.",
                "qwen": {"veredito": "desativado", "justificativa": "Modelo desativado"},
                "llama": {"veredito": "desativado", "justificativa": "Modelo desativado"},
            }

    if status_callback and enable_deep_ai:
        status_callback("🤖 Qwen -> Llama: calculando probabilidade de IA por bloco de secao...")

    ai_probability, ai_hits, ai_metrics = detect_ai_patterns(paragraphs)
    heuristic_ratio = len(fast_heuristic_hits) / max(1, len(paragraphs))
    heuristic_ai_probability = min(100.0, heuristic_ratio * 100.0)

    if enable_deep_ai:
        ai_llm_scores = _llm_ai_probability_per_paragraph(
            paragraphs,
            status_callback=status_callback,
            model_progress_callback=model_progress_callback,
            debug_callback=debug_callback,
        )
    else:
        ai_llm_scores = []
        ai_probability = max(ai_probability, heuristic_ai_probability)

    if progress_callback:
        progress_callback(0.90)

    if status_callback:
        if enable_deep_ai:
            status_callback("🧠 Consolidando auditoria de referências (web + Qwen + Llama)...")
        else:
            status_callback("🧠 Consolidando auditoria de referências (somente busca web, IA profunda desativada)...")

    references: List[Dict[str, object]] = []
    if web_audit_future:
        try:
            while not web_audit_future.done():
                if reference_progress_callback and int(reference_progress_state["web_total"]) > 0:
                    reference_progress_callback(
                        int(reference_progress_state["web_done"]),
                        int(reference_progress_state["web_total"]),
                        "web",
                    )
                time.sleep(0.15)

            web_rows = web_audit_future.result(timeout=180)
            if enable_deep_ai:
                references = _reference_sanity_check(
                    web_rows,
                    status_callback=status_callback,
                    reference_progress_callback=reference_progress_callback,
                )
            else:
                references = []
                for row in web_rows:
                    found_google = str(row.get("found_google", "Nao"))
                    doi = str(row.get("doi", "Nao encontrado"))
                    exact_title_match = bool(row.get("exact_title_match", False))
                    if found_google == "Sim" or doi != "Nao encontrado" or exact_title_match:
                        status = "ok"
                        veredito = "Referência Plausível (web)"
                        confirmed = False
                    else:
                        status = "dubious"
                        veredito = "Reference (Duvidosa)"
                        confirmed = True

                    references.append(
                        {
                            **row,
                            "status": status,
                            "veredito_final": veredito,
                            "confirmed_hallucination": confirmed,
                            "qwen_format": "Análise profunda desativada pelo usuário.",
                            "llm_parecer": "Análise profunda desativada pelo usuário.",
                            "llm_consensus": {
                                "qwen": {"veredito": "desativado"},
                                "llama": {"veredito": "desativado"},
                            },
                            "pipeline_status": "concluido_web",
                        }
                    )
        except Exception:  # noqa: BLE001
            references = []
        finally:
            if audit_executor:
                audit_executor.shutdown(wait=False)

    ref_indices_all = _reference_paragraph_indices(paragraphs)
    synthetic_rows: List[Dict[str, object]] = []
    for idx, paragraph in enumerate(paragraphs):
        if idx in ref_indices_all:
            continue
        synthetic = detect_synthetic_perfection(paragraph)
        synthetic_rows.append(
            {
                "paragraph_index": idx,
                "synthetic_perfection_score": float(synthetic.get("score", 0.0)),
                "synthetic_perfection_suspicious": bool(synthetic.get("is_suspicious", False)),
                "synthetic_perfection_signals": synthetic.get("signals", []),
            }
        )
        if bool(synthetic.get("is_suspicious", False)) and status_callback:
            status_callback("🕵️ Desconfiando da perfeição gramatical...")

    if progress_callback:
        progress_callback(0.98)

    if status_callback:
        status_callback("✅ Finalizando relatorio e gerando graficos...")

    labels_by_paragraph: Dict[int, List[str]] = {}
    for hit in plagiarism_hits:
        labels_by_paragraph.setdefault(hit.paragraph_index, []).append("plagiarism")
    for hit in ai_hits:
        labels_by_paragraph.setdefault(hit.paragraph_index, []).append("ai")
    for row in fast_heuristic_hits:
        labels_by_paragraph.setdefault(int(row["paragraph_index"]), []).append("formatting_alert")
    for ref in references:
        if ref.get("status") == "dubious":
            labels_by_paragraph.setdefault(int(ref["paragraph_index"]), []).append("reference")

    paragraph_count = max(1, len(paragraphs))
    plag_indices = {hit.paragraph_index for hit in plagiarism_hits}
    ai_indices = {hit.paragraph_index for hit in ai_hits}
    original_count = max(0, paragraph_count - len(plag_indices.union(ai_indices)))

    heatmap_labels = [
        f"P{hit.paragraph_index + 1}"
        for hit in plagiarism_hits[:5]
    ]
    heatmap_sources = [
        (hit.source_title or hit.source_url or f"Fonte {idx + 1}")[:55]
        for idx, hit in enumerate(plagiarism_hits[:5])
    ]
    heatmap_matrix: List[List[float]] = []
    for p_idx in range(min(5, len(paragraphs))):
        row: List[float] = []
        base_text = paragraphs[p_idx]
        for hit in plagiarism_hits[:5]:
            compare_text = hit.scraped_text or hit.source_excerpt
            row.append(_cosine_similarity_score(base_text, compare_text))
        heatmap_matrix.append(row)

    return {
        "plagiarism_percentage": plagiarism_percentage,
        "ai_probability": ai_probability,
        "heuristic_ai_probability": heuristic_ai_probability,
        "deep_ai_enabled": enable_deep_ai,
        "ai_metrics": ai_metrics,
        "human_baseline": HUMAN_BASELINE,
        "ai_llm_scores": ai_llm_scores,
        "fast_heuristic_rows": fast_heuristic_rows,
        "fast_heuristic_hits": fast_heuristic_hits,
        "synthetic_perfection_scores": synthetic_rows,
        "plagiarism_hits": [
            {
                "paragraph_index": hit.paragraph_index,
                "phrase": hit.phrase,
                "phrase_type": hit.phrase_type,
                "similarity": hit.similarity,
                "source_title": hit.source_title,
                "source_url": hit.source_url,
                "source_engine": hit.source_engine,
                "source_excerpt": hit.source_excerpt,
                "scraped_text": hit.scraped_text,
                "exact_phrase_match": hit.exact_phrase_match,
                "classification": hit.classification,
                "llm_consensus": hit.llm_consensus,
                "supporting_matches": hit.supporting_matches,
            }
            for hit in plagiarism_hits
        ],
        "ai_hits": [{"paragraph_index": hit.paragraph_index, "reasons": hit.reasons} for hit in ai_hits],
        "reference_checks_preliminary": reference_checks_preliminary,
        "reference_checks": references,
        "labels_by_paragraph": labels_by_paragraph,
        "search_stats": search_stats,
        "distribution": {
            "original": original_count,
            "ai": len(ai_indices),
            "web_plagiarism": len(plag_indices),
        },
        "similarity_heatmap": {
            "paragraph_labels": [f"P{idx + 1}" for idx in range(min(5, len(paragraphs)))],
            "source_labels": heatmap_sources,
            "matrix": heatmap_matrix,
        },
    }
