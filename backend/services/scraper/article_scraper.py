"""Clean article text scraper using trafilatura with BeautifulSoup lxml fallback."""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from backend.core.exceptions import ProviderError, ValidationError
from backend.core.logging import get_logger

logger = get_logger(__name__)

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
}


class ScrapedArticle(BaseModel):
    """Normalized article content extracted from a web URL."""

    url: str
    title: str
    text: str
    word_count: int = Field(..., ge=0)


def _normalize_whitespace(raw_text: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw_text.splitlines()]
    non_empty = [line for line in lines if line]
    return "\n".join(non_empty).strip()


def _extract_with_trafilatura(html: str, url: str) -> tuple[str | None, str | None]:
    try:
        import trafilatura  # noqa: PLC0415
    except ImportError:
        logger.warning("trafilatura is not installed; falling back to BeautifulSoup")
        return None, None

    text = trafilatura.extract(
        html,
        url=url,
        include_comments=False,
        include_tables=False,
        favor_precision=False,
        output_format="txt",
    )
    title: str | None = None
    try:
        metadata = trafilatura.extract_metadata(html, default_url=url)
        if metadata is not None and getattr(metadata, "title", None):
            title = str(metadata.title).strip() or None
    except Exception:
        title = None

    return (text.strip() if text else None), title


def _extract_with_beautifulsoup(html: str) -> tuple[str, str]:
    try:
        from bs4 import BeautifulSoup  # noqa: PLC0415
    except ImportError as exc:
        raise ProviderError(
            "article_scraper",
            "Neither trafilatura nor beautifulsoup4 is available to parse HTML",
        ) from exc

    soup = BeautifulSoup(html, "lxml")

    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    if not title:
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(" ", strip=True)

    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "aside", "form", "svg"]):
        tag.decompose()

    container = soup.find("article") or soup.find("main") or soup.body or soup
    paragraphs = [p.get_text(" ", strip=True) for p in container.find_all(["p", "h2", "h3", "li"])]
    paragraphs = [p for p in paragraphs if len(p) >= 15]

    if paragraphs:
        text = "\n".join(paragraphs)
    else:
        text = container.get_text("\n", strip=True)

    return _normalize_whitespace(text), title


class ArticleScraper:
    """Async web article extractor for the AI Faceless Video Generator pipeline."""

    def __init__(self, timeout_seconds: float = 20.0) -> None:
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _validate_url(url: str) -> str:
        cleaned = (url or "").strip()
        if not cleaned:
            raise ValidationError("Article URL cannot be empty")
        parsed = urlparse(cleaned)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise ValidationError(
                f"Invalid article URL '{url}': expected http:// or https:// URL",
                details={"url": url},
            )
        return cleaned

    async def scrape(self, url: str) -> ScrapedArticle:
        """Fetch and extract clean article title & body text from ``url``."""
        clean_url = self._validate_url(url)
        logger.info("Scraping article URL: %s", clean_url)

        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=True,
                headers=_DEFAULT_HEADERS,
            ) as client:
                response = await client.get(clean_url)
        except httpx.TimeoutException as exc:
            raise ProviderError(
                "article_scraper",
                f"Timed out fetching article URL '{clean_url}'",
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                "article_scraper",
                f"Failed to reach article URL '{clean_url}': {exc}",
                retryable=True,
            ) from exc

        if response.status_code >= 400:
            raise ProviderError(
                "article_scraper",
                f"Article URL returned HTTP {response.status_code}",
                retryable=response.status_code >= 500,
                details={"status_code": response.status_code, "url": str(response.url)},
            )

        html = response.text or ""
        if not html.strip():
            raise ValidationError(
                f"Article URL returned an empty HTML response: '{clean_url}'",
                details={"url": clean_url},
            )

        final_url = str(response.url)
        text, title = await asyncio.to_thread(_extract_with_trafilatura, html, final_url)

        if not text or len(text.split()) < 15:
            bs_text, bs_title = await asyncio.to_thread(_extract_with_beautifulsoup, html)
            if bs_text and (not text or len(bs_text.split()) > len(text.split())):
                text = bs_text
            if not title and bs_title:
                title = bs_title
        elif not title:
            _, bs_title = await asyncio.to_thread(_extract_with_beautifulsoup, html)
            title = bs_title

        normalized_text = _normalize_whitespace(text or "")
        if not normalized_text:
            raise ValidationError(
                f"Could not extract readable article content from '{final_url}'",
                details={"url": final_url},
            )

        resolved_title = (title or "").strip() or urlparse(final_url).netloc or "Untitled Article"
        word_count = len(normalized_text.split())

        logger.info(
            "Scraped article '%s' (%d words) from %s",
            resolved_title,
            word_count,
            final_url,
        )
        return ScrapedArticle(
            url=final_url,
            title=resolved_title,
            text=normalized_text,
            word_count=word_count,
        )
