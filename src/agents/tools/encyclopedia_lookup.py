"""EncyclopediaLookupTool - Naver encyclopedia raw evidence fetcher.

This tool deliberately does not decide commodity identity. It only fetches a
small set of Naver encyclopedia entries and preserves provenance. Product
Understanding may pass those entries to an identity distiller, but raw snippets
must stay evidence-only and must not be fed directly into DomainRouter.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional


NAVER_ENCYC_ENDPOINT = "https://openapi.naver.com/v1/search/encyc.json"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _load_local_env() -> None:
    project_root = Path(os.environ.get("ASAP_PROJECT_ROOT", Path(__file__).resolve().parents[3]))
    env_path = project_root / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_local_env()


def _strip(markup: str) -> str:
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub("", markup or ""))).strip()


def _hash_entry(title: str, description: str, link: str) -> str:
    raw = "\n".join([title or "", description or "", link or ""])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class EncyclopediaEntry:
    rank: int
    title: str
    description: str
    link: str
    thumbnail: str = ""
    content_hash: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EncyclopediaResult:
    query: str
    configured: bool
    found: bool
    entries: list[dict[str, Any]] = field(default_factory=list)
    source: str = "naver_encyc"
    error: str = ""
    quality_status: str = "raw_unchecked"
    quality_reasons: list[str] = field(default_factory=list)
    usable_for_routing: bool = False

    @property
    def title(self) -> str:
        return str((self.entries[0] or {}).get("title") or "") if self.entries else ""

    @property
    def description(self) -> str:
        return str((self.entries[0] or {}).get("description") or "") if self.entries else ""

    @property
    def link(self) -> str:
        return str((self.entries[0] or {}).get("link") or "") if self.entries else ""

    @property
    def content_hash(self) -> str:
        return str((self.entries[0] or {}).get("content_hash") or "") if self.entries else ""

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # Backward-compatible convenience fields. They are evidence metadata,
        # not routing input.
        data.update(
            {
                "title": self.title,
                "description": self.description,
                "link": self.link,
                "content_hash": self.content_hash,
                "lexical_grounding": {},
            }
        )
        return data

    def provenance(self) -> dict[str, Any]:
        return {
            "source_table": self.source,
            "source_id": self.title or self.query,
            "snippet": self.description[:160],
            "reason": f"raw encyclopedia evidence fetch (query={self.query!r})",
            "link": self.link,
            "content_hash": self.content_hash,
            "quality_status": self.quality_status,
            "quality_reasons": self.quality_reasons,
            "usable_for_routing": False,
        }


def is_configured() -> bool:
    return bool(os.environ.get("NAVER_CLIENT_ID") and os.environ.get("NAVER_CLIENT_SECRET"))


_cache: dict[str, EncyclopediaResult] = {}


def lookup(query: str, *, display: int = 3, timeout: float = 10.0, use_cache: bool = True) -> EncyclopediaResult:
    """Fetch raw Naver encyclopedia entries. Never raises."""
    q = (query or "").strip()
    if not q:
        return EncyclopediaResult(
            query=q,
            configured=is_configured(),
            found=False,
            quality_status="no_query",
            quality_reasons=["empty_query"],
        )
    cache_key = f"{q}|{max(1, min(display, 10))}"
    if use_cache and cache_key in _cache:
        return _cache[cache_key]
    if not is_configured():
        return EncyclopediaResult(
            query=q,
            configured=False,
            found=False,
            quality_status="unconfigured",
            quality_reasons=["naver_credentials_missing"],
            error="NAVER_CLIENT_ID/NAVER_CLIENT_SECRET not set",
        )

    url = NAVER_ENCYC_ENDPOINT + "?" + urllib.parse.urlencode({"query": q, "display": max(1, min(display, 10))})
    req = urllib.request.Request(
        url,
        headers={
            "X-Naver-Client-Id": os.environ["NAVER_CLIENT_ID"],
            "X-Naver-Client-Secret": os.environ["NAVER_CLIENT_SECRET"],
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - this tool must not break the pipeline
        return EncyclopediaResult(
            query=q,
            configured=True,
            found=False,
            quality_status="error",
            quality_reasons=["lookup_error"],
            error=f"{type(exc).__name__}: {exc}",
        )

    entries: list[dict[str, Any]] = []
    for rank, item in enumerate(data.get("items") or [], start=1):
        title = _strip(item.get("title") or "")
        description = _strip(item.get("description") or "")
        link = str(item.get("link") or "")
        thumbnail = str(item.get("thumbnail") or "")
        if not title and not description:
            continue
        entries.append(
            EncyclopediaEntry(
                rank=rank,
                title=title,
                description=description,
                link=link,
                thumbnail=thumbnail,
                content_hash=_hash_entry(title, description, link),
            ).as_dict()
        )

    if entries:
        res = EncyclopediaResult(
            query=q,
            configured=True,
            found=True,
            entries=entries,
            quality_status="raw_entries",
            quality_reasons=["raw_not_routing_input"],
        )
    else:
        res = EncyclopediaResult(
            query=q,
            configured=True,
            found=False,
            quality_status="no_result",
            quality_reasons=["no_items"],
        )
    if use_cache:
        _cache[cache_key] = res
    return res


def lookup_first(queries: list[str], **kw: Any) -> EncyclopediaResult:
    """Try candidate queries in order and return the first query with entries."""
    last = EncyclopediaResult(query="", configured=is_configured(), found=False)
    for q in queries:
        res = lookup(q, **kw)
        if res.found:
            return res
        last = res
        if res.configured:
            time.sleep(0.2)
    return last


if __name__ == "__main__":
    import sys

    print("configured:", is_configured())
    for term in (sys.argv[1:] or ["재첩", "어묵", "낙지", "멘보샤"]):
        r = lookup(term)
        print(f"\n[{term}] found={r.found} entries={len(r.entries)} title={r.title!r}")
        for entry in r.entries[:3]:
            print(f"  #{entry['rank']} {entry['title']}: {entry['description'][:160]}")
        if r.error:
            print("  error:", r.error)
