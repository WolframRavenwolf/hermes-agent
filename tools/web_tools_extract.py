"""web_extract helpers: URL validation, provider resolution, cache-aware dispatch.

Order of controls (each is a gate, never skipped by a cache hit): secret-URL
refusal -> SSRF filter (in web_tools.web_extract_tool) -> provider resolution
(strict selection) -> per-URL website policy -> disk cache -> vendor call with
one-shot keyless rescue. Logs under the origin (tools.web_tools) logger.
"""

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from tools.tool_backend_helpers import selection_error, selection_exists
from tools.url_safety import normalize_url_for_request, sensitive_query_param_name
from tools.web_tools_rescue import _policy_blocked_result, _rescue_eligible, _rescue_extract

logger = logging.getLogger("tools.web_tools")

_NO_RESULT_ERROR = "Extract backend returned no result for this URL"
_EXTRACT_BACKENDS_HINT = "firecrawl, tavily, keenable, exa, or parallel."
_INVALID_ITEM_ERROR = (
    "Invalid URL item at index {}: expected a URL string or an object with a string 'url' or 'href' field"
)


def _web_extract_url(value: Any) -> Optional[str]:
    """URL from a model-supplied extract item (str, or dict with ``url``/``href``); None if unusable.

    Models sometimes forward a whole search result instead of its URL, hence the dict form. Never
    stringify arbitrary objects into misleading fetch targets.
    """
    if isinstance(value, dict):
        value = value.get("url") or value.get("href")
    return (value.strip() or None) if isinstance(value, str) else None


def _disabled_plugin_error(capability: str, disabled_key: str) -> str:
    """Error text when the configured backend's bundled plugin is disabled in config."""
    vendor = disabled_key.split("/", 1)[-1]
    return (
        f"web.{capability}_backend is set to '{vendor}', but its plugin ('{disabled_key}') is disabled "
        f"in config. Re-enable it with `hermes plugins enable {disabled_key}` "
        "(or remove it from plugins.disabled)."
    )


def _no_provider_error(capability: str, fallback: str) -> str:
    """Error when no provider resolved: point at a disabled bundled plugin if that is the real cause."""
    from agent.web_search_registry import _disabled_web_plugin_for
    disabled_key = _disabled_web_plugin_for(capability=capability)
    return _disabled_plugin_error(capability, disabled_key) if disabled_key else fallback


def _strict_selection_error(capability: str, backend: str) -> str:
    """Error for a stored-but-unregistered backend: name the disabled plugin, else the bad selection.
    Strict selection never silently switches to whatever the availability walk finds."""
    failure = f"no registered web {capability} provider has that name"
    return _no_provider_error(capability, selection_error("web", f"'{backend}'", failure))


def _result_entry(url: str, error: Optional[str]) -> Dict[str, Any]:
    return {"url": url, "title": "", "content": "", "error": error, "_request_url": url}


def _extract_error_json(error: str) -> str:
    return json.dumps({"success": False, "error": error}, ensure_ascii=False)


def _refuse_all(error: str):
    """Whole-call refusal tuple for ``_validate_extract_urls`` (exfiltration prevention)."""
    return None, None, None, json.dumps({"success": False, "error": error})


def _public_results(results: List[dict]) -> List[dict]:
    """Remove private request identities from copies before serialization/truncation."""
    return [{k: v for k, v in result.items() if k != "_request_url"} for result in results]


def _merge_in_order(
    total: int, fixed: Dict[int, dict], fetch_positions: List[int], fetch_urls: List[str], results: List[dict]
) -> List[dict]:
    """Rebuild a ``total``-long result list: *fixed* entries by position, fetched *results* at
    *fetch_positions* (a short provider list yields ``_NO_RESULT_ERROR`` entries for the rest)."""
    merged = dict(fixed)
    for pos, position in enumerate(fetch_positions):
        missing = _result_entry(fetch_urls[pos], _NO_RESULT_ERROR)
        merged[position] = results[pos] if pos < len(results) else missing
    return _public_results([merged[i] for i in range(total)])


def _raw_userinfo(url: str) -> str:
    """Keep authority credentials verbatim, even without a scheme or with a malformed host."""
    authority = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", url).removeprefix("//")
    userinfo, separator, _ = re.split(r"[/?#]", authority, maxsplit=1)[0].rpartition("@")
    return userinfo + separator


def _validate_extract_urls(urls: List[Any]):
    """Normalize model-supplied items and block URLs carrying secrets (percent-encoded forms are unquoted
    and checked too). Returns ``(normalized_urls, normalized_indices, invalid_urls, blocked_json)``;
    ``blocked_json`` is a whole-call refusal (exfiltration prevention) or None."""
    from agent.redact import _PREFIX_RE
    from urllib.parse import unquote

    normalized_urls, normalized_indices, invalid_urls = [], [], {}
    for index, item in enumerate(urls):
        _url = _web_extract_url(item)
        if _url is None:
            invalid_urls[index] = _result_entry("", _INVALID_ITEM_ERROR.format(index))
            continue
        userinfo = _raw_userinfo(_url)
        normalized_url = normalize_url_for_request(_url)
        if any(_PREFIX_RE.search(c) for c in (_url, unquote(_url), normalized_url, unquote(normalized_url))):
            return _refuse_all(
                "Blocked: URL contains what appears to be an API key or token. "
                "Secrets must not be sent in URLs."
            )
        if sensitive_query_key := sensitive_query_param_name(normalized_url):
            return _refuse_all(
                "Blocked: URL contains a credential-like query parameter "
                f"({sensitive_query_key}). Web extract backends are third-party "
                "readers; remove the sensitive query parameter or use a local "
                "browser session when this access is explicitly required."
            )
        # The shared IDNA normalizer can replace a hostname occurrence in the username.
        if _raw_userinfo(normalized_url) != userinfo:
            invalid_urls[index] = _result_entry(_url, "Invalid URL: normalization changes userinfo")
            continue
        normalized_urls.append(normalized_url)
        normalized_indices.append(index)
    return normalized_urls, normalized_indices, invalid_urls, None


def _resolve_extract_provider(backend: str):
    """Resolve the extract provider for *backend*; returns ``(provider, error_json)``.

    A registered search-only backend is a typed error (never a silent switch). An unregistered name with
    a stored web selection is a strict-selection error; with no selection, fall through to the walk.
    """
    from agent.web_search_registry import get_active_extract_provider, get_provider as _wsp_get_provider
    provider = _wsp_get_provider(backend) if backend else None
    if provider is not None and provider.supports_extract():
        return provider, None
    if provider is not None:
        return None, _extract_error_json(
            f"{provider.display_name} is a search-only backend and cannot extract URL content. "
            "Set web.extract_backend to " + _EXTRACT_BACKENDS_HINT
        )
    if backend and selection_exists("web"):
        return None, _extract_error_json(_strict_selection_error("extract", backend))
    provider = get_active_extract_provider()
    if provider is None:
        fallback = "No web extract provider configured. Set web.extract_backend to " + _EXTRACT_BACKENDS_HINT
        return None, _extract_error_json(_no_provider_error("extract", fallback))
    return provider, None


def _url_key(url: Any, *, loose: bool = False) -> str:
    """Exact pairing preserves raw case-sensitive userinfo, non-root path slashes and
    the query. Host/scheme case, default ports, root slash and fragment are folded.
    ``loose`` also folds scheme, leading ``www.`` and trailing slashes for backends
    that report a rewritten URL; the caller must establish unambiguous ownership."""
    if not isinstance(url, str) or not url.strip():
        return ""
    raw = url.strip()
    userinfo = _raw_userinfo(raw)
    # Parse missing-scheme credentials as authority, not a username-shaped scheme.
    if userinfo and "://" not in raw and not raw.startswith("//"):
        raw = "//" + raw
    try:
        parts = urlsplit(normalize_url_for_request(raw))
        scheme, host, port = parts.scheme.lower(), (parts.hostname or "").lower(), parts.port
    except ValueError:
        return url.strip()
    if port == {"http": 80, "https": 443}.get(scheme):
        port = None
    if loose and host.startswith("www."):
        host = host[4:]
    prefix = "" if loose else f"{scheme}://"
    query = f"?{parts.query}" if parts.query else ""
    path = parts.path.rstrip("/") if loose else parts.path
    if path == "/":
        path = ""
    return f"{prefix}{userinfo}{host}{f':{port}' if port else ''}{path}{query}"


def _pair_results(urls: List[str], results: List[dict]) -> List[dict]:
    """Prefer exact local request proof; untagged URL aliases are compatibility only.

    Vendor metadata (including Firecrawl's final sourceURL) is never ownership.
    Collect competitors before selection, so ordering cannot hide an observable
    collision. URL-only batches still cannot prove silent redirects or swaps.
    """
    request_keys = [_url_key(url) for url in urls]
    raw_owners = dict(zip(urls, request_keys))
    exact_requests = set(request_keys)
    loose_owners: Dict[str, set[str]] = {}
    for url, key in zip(urls, request_keys):
        loose_owners.setdefault(_url_key(url, loose=True), set()).add(key)

    proven: Dict[str, List[dict]] = {}
    compatible: Dict[str, List[tuple]] = {}
    leftover: List[dict] = []
    rejected = 0
    for entry in results:
        if not isinstance(entry, dict):
            continue
        if "_request_url" in entry:
            marker = entry["_request_url"]
            # No normalization/loose lookup here, even for a cosmetically valid alias.
            if isinstance(marker, str) and marker in raw_owners:
                proven.setdefault(raw_owners[marker], []).append(entry)
            else:
                rejected += 1
            continue
        key = _url_key(entry.get("url"))
        exact = key in exact_requests
        if not exact:
            owners = loose_owners.get(_url_key(entry.get("url"), loose=True), set())
            key = next(iter(owners)) if len(owners) == 1 else None
        if key is None:
            leftover.append(entry)
        else:
            compatible.setdefault(key, []).append((not exact, entry))

    paired: Dict[str, dict] = {}
    # Repeated exact identities represent the same request owner. A compatible
    # singleton alias can upgrade a stub; an unknown redirect cannot do so.
    sole_owner = len(exact_requests) == 1
    for url, key in zip(urls, request_keys):
        if key in paired:
            continue
        owned = proven.get(key, [])
        aliases = [entry for _, entry in sorted(compatible.get(key, []), key=lambda pair: pair[0])]
        owned_success = next((r for r in owned if not r.get("error")), None)
        if owned_success is not None:
            paired[key] = owned_success
            continue
        successes = [r for r in aliases if not r.get("error")]
        errors = [r for r in aliases if r.get("error")]
        if owned and not (sole_owner and successes):
            paired[key] = owned[0]
        elif successes and any(r != successes[0] for r in successes[1:]):
            paired[key] = _result_entry(url, "Ambiguous extract results for this URL")
        elif successes and (sole_owner or not errors):
            paired[key] = successes[0]
        elif errors:
            paired[key] = errors[0]

    # Preserve the historical sole-leftover exception only for an unmatched
    # actual single-URL dispatch, never to overwrite a matched error/refusal.
    if len(urls) == 1 and request_keys[0] not in paired and len(leftover) == 1 and not leftover[0].get("error"):
        paired[request_keys[0]] = leftover.pop()
    if leftover or rejected:
        logger.warning("web_extract: dropping %d unowned result(s)", len(leftover) + rejected)
    return [paired.get(key) or _result_entry(url, _NO_RESULT_ERROR) for url, key in zip(urls, request_keys)]


async def _dispatch_extract(provider, fetch_urls: List[str], format: Optional[str]) -> List[dict]:
    """Call ``provider.extract`` (async or sync-in-thread), with one-shot keyless rescue.

    Rescue fires on a raised exception or when the WHOLE batch failed (backend outage, not per-page
    problems). Rescued batches are never cached. Every batch, rescued or not, is paired to
    ``fetch_urls`` by URL before it is returned or cached.
    """
    import inspect
    from tools.web_result_cache import extract_cache_put
    try:
        if inspect.iscoroutinefunction(provider.extract):
            results = await provider.extract(fetch_urls, format=format)
        else:  # sync extract() runs in a thread so network I/O never blocks the loop
            results = await asyncio.to_thread(provider.extract, fetch_urls, format=format)
    except Exception as exc:  # noqa: BLE001 — candidate for rescue
        if not _rescue_eligible(provider):
            raise
        results = [_result_entry(u, str(exc)) for u in fetch_urls]
    paired = _pair_results(fetch_urls, results)
    if results and all(r.get("error") for r in results) and _rescue_eligible(provider):
        # Positional rescue subsets are safe only after ownership pairing. Preserve
        # policy slots even when the rescue returns extra or missing rows.
        positions = [i for i, result in enumerate(paired) if not _policy_blocked_result(result)]
        if positions:
            rescue_urls = [fetch_urls[i] for i in positions]
            failed = [paired[i] for i in positions]
            rescued = await asyncio.to_thread(_rescue_extract, provider.name, rescue_urls, failed)
            for position, result in zip(positions, _pair_results(rescue_urls, rescued)):
                paired[position] = result
        return paired

    results = paired
    # Cache each successful fetch's full clean text under the REQUESTED url (best-effort; oversized skipped).
    for url, fetched in zip(fetch_urls, results):
        _content = fetched.get("raw_content", "") or fetched.get("content", "")
        if _content and not fetched.get("error"):
            extract_cache_put(url, _content, fetched.get("title", ""), format=format, provider=provider.name)
    return results


async def _extract_safe_urls(provider, safe_urls: List[str], format: Optional[str]) -> List[dict]:
    """Serve cache hits, fetch the rest, and merge back in ``safe_urls`` order.

    The disk cache (tools/web_result_cache.py) sits AFTER the secret-URL gate, SSRF gate, and provider
    resolution, and is gated per-URL on the website policy — a hit skips only the vendor call, never a
    control; policy-blocked URLs are cache misses. Keys include provider and format, so switching either
    within the TTL never serves the other's content."""
    from tools.web_result_cache import extract_cache_get
    from tools.website_policy import check_website_access as _check_site
    cached_results, fetch_urls, fetch_positions = {}, [], []
    for position, url in enumerate(safe_urls):
        try:
            _policy_block = _check_site(url)
        except Exception:  # noqa: BLE001 — policy errors fail open like dispatch
            _policy_block = None
        hit = extract_cache_get(url, format=format, provider=provider.name) if _policy_block is None else None
        if hit is not None:
            cached_results[position] = hit
        else:
            fetch_urls.append(url)
            fetch_positions.append(position)

    if not fetch_urls:
        return _public_results([cached_results[i] for i in range(len(safe_urls))])
    logger.info("Web extract via %s: %d URL(s)", provider.name, len(fetch_urls))
    results = await _dispatch_extract(provider, fetch_urls, format)
    if not cached_results:
        return _public_results(results)
    return _merge_in_order(len(safe_urls), cached_results, fetch_positions, fetch_urls, results)
