"""Scraper web com aiohttp/BeautifulSoup (rápido) + Selenium fallback (JS-heavy)."""

from __future__ import annotations

import asyncio
import os
import random
import re
import logging
from typing import Optional, List
from bs4 import BeautifulSoup

import aiohttp
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

LOGGER = logging.getLogger(__name__)
MAX_FETCH_SIZE = 7000  # Limita tamanho máximo extraído por URL




async def fetch_page_text_aiohttp(url: str, timeout: int = 10, max_chars: int = 7000) -> str:
    """Extrai texto visivel usando aiohttp + BeautifulSoup (rápido para HTML simples)."""
    if not url.strip():
        return ""

    try:
        timeout_obj = aiohttp.ClientTimeout(total=timeout)
        headers = {"User-Agent": random.choice(USER_AGENTS)}
        
        async with aiohttp.ClientSession(timeout=timeout_obj) as session:
            async with session.get(url, ssl=False, headers=headers) as response:
                if response.status != 200:
                    return ""
                
                text = await response.text(errors="ignore")
                soup = BeautifulSoup(text, "html.parser")
                
                # Remove scripts e styles
                for tag in soup(["script", "style", "noscript"]):
                    tag.decompose()
                
                # Extrai texto visível
                body_text = soup.get_text(separator=" ", strip=True)
                normalized = re.sub(r"\s+", " ", body_text).strip()
                return normalized[:max_chars]
    except asyncio.TimeoutError:
        LOGGER.debug("Timeout ao buscar %s", url)
        return ""
    except Exception as exc:
        LOGGER.debug("Erro aiohttp para URL %s: %s", url, exc)
        return ""


def _build_driver() -> webdriver.Chrome:
    """Configura Chrome em modo headless para ambientes locais e Docker."""
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--log-level=3")
    options.add_argument("--silent")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(f"--user-agent={random.choice(USER_AGENTS)}")

    chrome_bin = os.getenv("CHROME_BIN", "").strip()
    if chrome_bin:
        options.binary_location = chrome_bin

    explicit_driver_path = os.getenv("CHROMEDRIVER_PATH", "").strip()
    if explicit_driver_path:
        service = Service(executable_path=explicit_driver_path)
    else:
        service = Service(executable_path=ChromeDriverManager().install())

    return webdriver.Chrome(service=service, options=options)


def fetch_page_text_selenium(url: str, timeout: int = 15, max_chars: int = 7000) -> str:
    """Extrai texto visivel usando Selenium (fallback para sites com JavaScript pesado)."""
    if not url.strip():
        return ""

    driver: Optional[webdriver.Chrome] = None
    try:
        driver = _build_driver()
        driver.set_page_load_timeout(timeout)
        driver.get(url)
        body_text = driver.find_element("tag name", "body").text
        normalized = re.sub(r"\s+", " ", body_text).strip()
        return normalized[:max_chars]
    except (WebDriverException, Exception) as exc:
        LOGGER.debug("Falha Selenium para URL %s: %s", url, exc)
        return ""
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


async def fetch_page_text_smart(url: str, timeout: int = 10, max_chars: int = 7000) -> str:
    """Tenta aiohttp primeiro (rápido), cai para Selenium se houver erro de parsing."""
    # Tenta aiohttp (rápido para a maioria dos sites)
    text = await fetch_page_text_aiohttp(url, timeout, max_chars)
    if text:
        return text
    
    # Fallback: Selenium para sites JS-heavy
    return fetch_page_text_selenium(url, timeout, max_chars)


async def fetch_urls_parallel(urls: List[str], timeout: int = 10, max_chars: int = 7000) -> dict[str, str]:
    """Busca múltiplas URLs em paralelo usando aiohttp + Selenium fallback."""
    tasks = [fetch_page_text_smart(url, timeout, max_chars) for url in urls]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    url_texts = {}
    for url, result in zip(urls, results):
        if isinstance(result, str):
            url_texts[url] = result
        else:
            url_texts[url] = ""
    
    return url_texts
