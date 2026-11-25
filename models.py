"""The common document format (a small Pydantic model) and the helpers that build it.

Every source, whatever its website looks like, ends up as the same Article shape.
"""
import hashlib
import re
from datetime import date
from typing import Any, Literal, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = 1
EXCERPT_CHARS = 500   # length of the stored excerpt when a source's terms do not permit full text
TAG_WINDOW = 1500     # tags look at the headline, description and the lead of the article only

# Simple keyword rules. Press releases put the main news first, so we only scan the lead:
# otherwise a funding acknowledgement at the bottom of a study would tag it "investment".
TAG_PATTERNS = {
    "ai": r"(?-i:\bAI\b)|artificial intelligence|machine learning|deep learning|large language model|\balgorithms?\b",
    "automation": r"\bautomat(?:ion|ed|es|ing)\b|\brobotic process automation\b",
    "partnership": r"\bpartnerships?\b|\bpartner(?:s|ed|ing)? with\b|\bjoint venture\b|\bstrategic alliance\b|\bteam(?:s|ed)? up with\b",
    "expansion": r"\bexpan(?:d|ds|ded|ding|sion)\b|\bnew (?:hospital|facility|clinic|campus|location|building|tower)\b|\bbr(?:eaks?|oke) ground\b",
    "investment": r"\binvest(?:s|ed|ing|ment|ments)?\b|\bfunding\b|\$\s?\d[\d,.]*\s?(?:million|billion)\b",
    # Bare "acquired" is left out on purpose: in medicine it usually means "acquired resistance/infection".
    "acquisition": r"\bacquisitions?\b|\bacquir(?:e|es|ing)\b|\bha(?:s|ve) acquired\b|\bmergers?\b|\bmerg(?:e|es|ed|ing) with\b",
    "leadership": r"\b(?:appoint(?:s|ed|ment)?|named|names)\b[^.]{0,60}\b(?:president|ceo|chief|chair|dean|director)\b|\bsteps? down\b|\bnew (?:ceo|president|chief)\b",
    "research": r"\bstud(?:y|ies)\b|\bresearch(?:ers)?\b|\bclinical trials?\b|\bpublished in\b",
}
TAG_REGEXES = {tag: re.compile(pattern, re.IGNORECASE) for tag, pattern in TAG_PATTERNS.items()}


class SourceInfo(BaseModel):
    id: str
    name: str
    domain: str
    industry: str


class Article(BaseModel):
    """One collected document. Database timestamps are added in database.py, not here."""

    model_config = ConfigDict(str_strip_whitespace=True)

    schema_version: int = SCHEMA_VERSION
    canonical_url: str
    source: SourceInfo
    document_type: str = "article"
    title: str = Field(min_length=5, max_length=300)
    description: Optional[str] = None
    content: str = Field(min_length=200)
    content_scope: Literal["full", "excerpt"]
    published_date: Optional[date] = None   # exported as "YYYY-MM-DD"; None when the page has no date
    author: Optional[str] = None
    language: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    organizations: list[str] = Field(default_factory=list)   # reserved for future entity extraction
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    metadata: dict[str, Any] = Field(default_factory=dict)  # extra, source-specific details

    @field_validator("canonical_url")
    @classmethod
    def must_be_absolute_http_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("must be an absolute http(s) URL")
        return value


def build_article(source: dict, extracted: dict) -> Article:
    """Convert fields from extract.py into a validated Article (raises pydantic.ValidationError)."""
    scope = source.get("extraction", {}).get("content_scope", "excerpt")  # conservative default
    title, text = extracted["title"], extracted["text"]
    description = extracted.get("description")
    lead = "\n".join([title, description or "", text[:TAG_WINDOW]])
    return Article(
        canonical_url=extracted["canonical_url"],
        source=SourceInfo(
            id=source["id"],
            name=source["name"],
            domain=urlsplit(extracted["canonical_url"]).hostname or "",
            industry=source["industry"],
        ),
        title=title,
        description=description,
        content=text if scope == "full" else make_excerpt(text),
        content_scope=scope,
        published_date=extracted.get("published_date"),
        author=extracted.get("author"),
        language=extracted.get("language"),
        tags=find_tags(lead),
        # The hash covers the full text even when we only store an excerpt, so edits anywhere are noticed.
        content_hash=compute_content_hash(title, text),
        metadata={
            "extraction_method": "trafilatura",
            "discovery_method": source["discovery"]["method"],
            "fetched_url": extracted.get("fetched_url"),
            "word_count": len(text.split()),
        },
    )


def make_excerpt(text: str, max_chars: int = EXCERPT_CHARS) -> str:
    """The start of the article, cut at a sentence end (or else a word boundary) near max_chars."""
    if len(text) <= max_chars:
        return text
    window = text[:max_chars]
    sentence_end = max(window.rfind(". "), window.rfind(".\n"), window.rfind("? "), window.rfind("! "))
    if sentence_end >= max_chars // 2:
        return window[: sentence_end + 1]
    return window.rsplit(" ", 1)[0].rstrip(",;:") + " …"


def normalize_for_hash(value: str) -> str:
    return " ".join(value.split()).lower()


def compute_content_hash(title: str, text: str) -> str:
    """SHA-256 of the normalized title and text, so whitespace/case-only differences don't count as changes."""
    normalized = normalize_for_hash(title) + "\n" + normalize_for_hash(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def find_tags(text: str) -> list[str]:
    """Keyword-based topic tags. An empty list is a perfectly fine answer."""
    return [tag for tag, regex in TAG_REGEXES.items() if regex.search(text)]
