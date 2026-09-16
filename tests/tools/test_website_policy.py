"""Website policy through the public extract wrapper, native providers and disk cache."""
import asyncio
import json
import socket
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from hermes_constants import get_hermes_home
from tests.tools.conftest import register_all_web_providers
from tools import web_result_cache as cache
from tools import website_policy
from tools.web_tools import web_extract_tool


ALLOWED = "https://8.8.8.8/page"
BLOCKED = "https://1.1.1.1/private"
OTHER = "https://8.8.4.4/other"
FORBIDDEN_TEXT = "blocked page body " * 2000


def _response(documents):
    return httpx.Response(200, json={"results": documents})


def _extract(urls, **kwargs):
    return json.loads(asyncio.run(web_extract_tool(urls, **kwargs)))["results"]


@pytest.fixture
def native_web(monkeypatch):
    """Only transport is mocked; config/policy/selection/normalization/cache stay real."""
    register_all_web_providers()
    config = {
        "web": {
            "extract_backend": "tavily", "cache_enabled": True,
            "keyless_rescue": True, "keyless_enabled": True,
            "provider_tier": {"exa": "paid", "firecrawl": "paid", "parallel": "paid"},
        },
        "security": {"website_blocklist": {"enabled": True, "domains": ["1.1.1.1"]}},
    }

    def configure(*, domains=None, policy_enabled=None, **web):
        config["web"].update(web)
        if domains is not None:
            config["security"]["website_blocklist"]["domains"] = domains
        if policy_enabled is not None:
            config["security"]["website_blocklist"]["enabled"] = policy_enabled
        (get_hermes_home() / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
        # Model a fresh native policy load after the existing policy TTL.
        website_policy._cached_policy = None

    configure()
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    primary = Mock(return_value=_response([{"url": ALLOWED, "raw_content": "allowed page"}]))
    rescue = Mock(side_effect=AssertionError("unexpected rescue request"))
    monkeypatch.setattr("plugins.web.tavily.provider.httpx.post", primary)
    monkeypatch.setattr("requests.post", rescue)
    monkeypatch.setattr("requests.get", rescue)
    yield configure, primary, rescue
    from agent.web_search_registry import _reset_for_tests
    _reset_for_tests()
    website_policy._cached_policy = None


def _assert_blocked(entry, url=BLOCKED):
    assert entry["url"] == url
    assert "_request_url" not in entry
    assert entry["blocked_by_policy"] == {"host": "1.1.1.1", "rule": "1.1.1.1", "source": "config"}
    assert "Blocked by website policy" in entry["error"]
    assert entry["title"] == entry["content"] == ""


@pytest.mark.parametrize("keyed", [False, True])
@pytest.mark.parametrize("backend", ["tavily", "brave"])
def test_all_blocked_precedes_resolution_and_transport(native_web, monkeypatch, keyed, backend):
    configure, primary, rescue = native_web
    configure(extract_backend=backend)
    if not keyed:
        monkeypatch.delenv("TAVILY_API_KEY")
    results = _extract([BLOCKED, BLOCKED])
    assert len(results) == 2
    for entry in results:
        _assert_blocked(entry)
    primary.assert_not_called()
    rescue.assert_not_called()
    assert cache._load_index() == {}


def test_mixed_invalid_ssrf_policy_cache_fetch_order_and_duplicates(native_web, monkeypatch):
    _, primary, rescue = native_web
    assert _extract([ALLOWED])[0]["content"] == "allowed page"
    primary.reset_mock()
    primary.return_value = _response([{"url": OTHER, "raw_content": "other page"}])
    resolver = Mock(wraps=socket.getaddrinfo)
    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    urls = [None, BLOCKED, "http://[bad", OTHER, "http://127.0.0.1/private", ALLOWED, BLOCKED, OTHER]
    response = json.loads(asyncio.run(web_extract_tool(urls)))
    assert "results" in response, response
    results = response["results"]
    assert [r["url"] for r in results] == ["", *urls[1:]]
    assert "Invalid URL item" in results[0]["error"]
    assert "Invalid URL" in results[2]["error"]
    assert "private or internal" in results[4]["error"]
    for index in (0, 2, 4):
        assert results[index]["title"] == results[index]["content"] == ""
    _assert_blocked(results[1])
    _assert_blocked(results[6])
    assert [results[i]["content"] for i in (3, 5, 7)] == ["other page", "allowed page", "other page"]
    assert {call.args[0] for call in resolver.call_args_list} == {"8.8.4.4", "127.0.0.1", "8.8.8.8"}
    primary.assert_called_once()
    assert primary.call_args.kwargs["json"]["urls"] == [OTHER, OTHER]
    rescue.assert_not_called()


@pytest.mark.parametrize("kind", ["http_error", "exception", "timeout", "all_error"])
@pytest.mark.parametrize("rescue_enabled", [False, True])
def test_full_failures_keep_blocked_and_allowed_slots(native_web, kind, rescue_enabled):
    configure, primary, rescue = native_web
    configure(keyless_rescue=rescue_enabled, extract_timeout=0.02 if kind == "timeout" else 120)
    if kind == "http_error":
        primary.return_value = httpx.Response(503, text="provider unavailable")
    elif kind == "exception":
        primary.side_effect = httpx.ConnectError("provider unavailable")
    elif kind == "timeout":
        def delayed(*args, **kwargs):
            time.sleep(0.1)
            return _response([{"url": ALLOWED, "raw_content": "late body"}])
        primary.side_effect = delayed
    else:
        primary.return_value = httpx.Response(200, json={"failed_urls": [ALLOWED, OTHER]})
    rescue.side_effect = None
    rescue.return_value = httpx.Response(503, text="rescue unavailable")
    results = _extract([BLOCKED, ALLOWED, BLOCKED, OTHER])
    assert [r["url"] for r in results] == [BLOCKED, ALLOWED, BLOCKED, OTHER]
    _assert_blocked(results[0])
    _assert_blocked(results[2])
    assert all(results[i]["error"] and not results[i]["content"] for i in (1, 3))
    if kind == "timeout":
        assert "timed out" in results[1]["error"]
    assert primary.call_args.kwargs["json"]["urls"] == [ALLOWED, OTHER]
    assert rescue.call_count == (2 if rescue_enabled else 0)
    if rescue_enabled:
        assert [call.kwargs["params"]["url"] for call in rescue.call_args_list] == [ALLOWED, OTHER]
    assert cache._load_index() == {}


def test_resolution_failure_keeps_policy_metadata(native_web):
    configure, primary, rescue = native_web
    configure(extract_backend="brave")
    results = _extract([BLOCKED, ALLOWED, BLOCKED])
    _assert_blocked(results[0])
    _assert_blocked(results[2])
    assert "no registered web extract provider has that name" in results[1]["error"]
    primary.assert_not_called()
    rescue.assert_not_called()


@pytest.mark.parametrize("route", ["normal", "http_error", "exception", "timeout", "all_error"])
@pytest.mark.parametrize("blocked_final", [False, True])
def test_final_policy_precedes_storage_and_rescue_stays_uncached(native_web, route, blocked_final):
    configure, primary, rescue = native_web
    final = BLOCKED if blocked_final else OTHER
    body = FORBIDDEN_TEXT if blocked_final else "allowed redirect body"
    documents = [{"url": final, "title": "private title" if blocked_final else "Page", "raw_content": body}]
    configure(extract_timeout=0.02 if route == "timeout" else 120)
    if route == "normal":
        primary.return_value = _response(documents)
    else:
        if route == "exception":
            primary.side_effect = httpx.ConnectError("provider unavailable")
        elif route == "timeout":
            def delayed(*args, **kwargs):
                time.sleep(0.1)
                return _response(documents)
            primary.side_effect = delayed
        elif route == "all_error":
            primary.return_value = httpx.Response(200, json={"failed_urls": [ALLOWED]})
        else:
            primary.return_value = httpx.Response(503, text="provider unavailable")
        rescue.side_effect = None
        rescue.return_value = httpx.Response(200, json={
            "url": final, "title": documents[0]["title"], "content": body,
        })
    for _ in range(2):
        results = _extract([ALLOWED], char_limit=2000)
        assert len(results) == 1
        if blocked_final:
            _assert_blocked(results[0], url=ALLOWED)
            assert "private title" not in json.dumps(results)
        else:
            assert results[0]["url"] == final
            assert results[0]["content"] == body
    assert primary.call_count == (1 if route == "normal" and not blocked_final else 2)
    assert rescue.call_count == (0 if route == "normal" else 2)
    if blocked_final or route != "normal":
        assert cache._load_index() == {}
    for path in (get_hermes_home() / "cache" / "web").glob("*.md"):
        assert "blocked page body" not in path.read_text()


@pytest.mark.parametrize("final_field", ["final_url", "url"])
def test_malformed_final_url_keeps_valid_sibling_and_caches_only_valid_content(native_web, final_field):
    from tools.web_tools_extract import _extract_safe_urls

    _, primary, rescue = native_web
    malformed = {
        "url": ALLOWED, "_request_url": ALLOWED,
        "title": "invalid page title", "content": "invalid page body",
        "raw_content": "invalid raw body", "metadata": {"body": "invalid metadata"},
        final_field: "http://[bad",
    }
    valid = {"url": OTHER, "title": "Valid page", "content": "valid page body"}
    provider = SimpleNamespace(name="tavily", extract=AsyncMock(return_value=[valid, malformed]))

    results = asyncio.run(_extract_safe_urls(provider, [ALLOWED, OTHER], None))

    assert results[1] == valid
    assert results[0] == {
        "url": ALLOWED, "title": "", "content": "",
        "error": "Invalid final URL: malformed authority",
    }
    provider.extract.assert_awaited_once_with([ALLOWED, OTHER], format=None)
    assert cache.extract_cache_get(ALLOWED, provider=provider.name) is None
    hit = cache.extract_cache_get(OTHER, provider=provider.name)
    assert hit is not None and hit["content"] == valid["content"]
    primary.assert_not_called()
    rescue.assert_not_called()


@pytest.mark.parametrize("policy_enabled", [False, True])
@pytest.mark.parametrize("malformed", [
    "http://[bad", "https://example.org:bad/", "https://example.org:65536/",
    "ftp://example.org/page", "https:///missing-host",
    "https://exa mple.org/page", "https://example.org :443/page",
    "https://exa\tmple.org/page", "https://exa\nmple.org/page",
    "https://exa\rmple.org/page", "https://exa\x00mple.org/page",
    "https://exa\x1fmple.org/page", "https://exa\x7fmple.org/page",
    "https://exa\x80mple.org/page", "https://exa\u00a0mple.org/page",
    "https://exa\u2003mple.org/page", "https://us\ter@example.org/page",
    " https://example.org/page", "\x00https://example.org/page",
])
def test_final_url_acceptance_and_cache_are_policy_independent(native_web, monkeypatch, policy_enabled, malformed):
    """Reported malformed authority never becomes output or reusable disk content."""
    configure, primary, rescue = native_web
    configure(extract_backend="keenable", policy_enabled=policy_enabled)
    monkeypatch.setenv("KEENABLE_API_KEY", "kn-test")
    unknown = "https://9.9.9.9/unknown"
    stale = "https://9.9.9.9/stale"
    bad_body = "malformed final body " * 2000
    fetched = []

    def fetch(endpoint, **kwargs):
        requested = kwargs["params"]["url"]
        fetched.append(requested)
        if requested == ALLOWED:
            data = {"url": malformed, "title": "malformed title", "content": bad_body}
        elif requested == unknown:
            data = {"title": "Unknown destination", "content": "unknown body"}
        else:
            assert requested in (OTHER, stale)
            data = {"url": requested, "title": "Valid page", "content": "valid body"}
        return httpx.Response(200, json=data)

    monkeypatch.setattr("requests.get", fetch)
    for _ in range(2):
        results = _extract([ALLOWED, OTHER, unknown], char_limit=2000)
        assert results[0] == {
            "url": ALLOWED, "title": "", "content": "",
            "error": "Invalid final URL: malformed authority",
        }
        assert results[1]["url"] == OTHER and results[1]["content"] == "valid body"
        assert not results[1].get("error")
        assert results[2]["url"] == unknown and results[2]["content"] == "unknown body"
        assert not results[2].get("error")
    assert fetched.count(ALLOWED) == fetched.count(unknown) == 2
    assert fetched.count(OTHER) == 1
    assert cache.extract_cache_get(ALLOWED, provider="keenable") is None
    assert cache.extract_cache_get(unknown, provider="keenable") is None
    index = cache._load_index()
    assert {entry["url"] for entry in index.values()} == {OTHER}

    # Direct writers must obey the same acceptance gate as provider results.
    cache.extract_cache_put(ALLOWED, bad_body, provider="keenable", final_url=malformed)
    assert cache._load_index() == index
    cache_dir = get_hermes_home() / "cache" / "web"
    for path in cache_dir.glob("*.md"):
        assert "malformed final body" not in path.read_text()

    # Craft an otherwise valid, unexpired older record whose final provenance is malformed.
    cache.extract_cache_put(stale, "stale malformed body", provider="keenable", final_url=stale)
    index = cache._load_index()
    entry = index[cache._url_digest(stale, None, "keenable")]
    entry["final_url"] = malformed
    cache._save_index(index)
    assert cache.extract_cache_get(stale, provider="keenable") is None
    refreshed = _extract([stale, OTHER])
    assert [result["content"] for result in refreshed] == ["valid body", "valid body"]
    assert fetched.count(stale) == fetched.count(OTHER) == 1
    hit = cache.extract_cache_get(stale, provider="keenable")
    assert hit is not None and hit["final_url"] == stale and hit["content"] == "valid body"
    primary.assert_not_called()
    rescue.assert_not_called()


@pytest.mark.parametrize("policy_enabled", [False, True])
@pytest.mark.parametrize("reported, malformed", [
    ({}, False), ({"url": None}, False), ({"url": ""}, False),
    ({"url": " "}, True), ({"url": "http://[bad"}, True),
    ({"url": "https://exa\tmple.org/page"}, True),
    ({"url": 0}, True), ({"url": False}, True),
    ({"url": "https://Example.org:8443/a b/%20?q=a b&next=https://other.org/#part"}, False),
    ({"url": "http://example.org:80/?q=value"}, False),
    ({"url": "https://[2001:4860:4860::8888]:443/a%20b?q=x%20y"}, False),
])
def test_tavily_final_provenance_survives_real_extract_and_cache(
    native_web, monkeypatch, policy_enabled, reported, malformed,
):
    """Empty provenance is usable but uncached; malformed reports never become unknown."""
    configure, primary, rescue = native_web
    configure(policy_enabled=policy_enabled)
    primary.return_value = _response([{"url": OTHER, "raw_content": "valid sibling"}])
    assert _extract([OTHER])[0]["content"] == "valid sibling"
    primary.reset_mock()
    put = Mock(wraps=cache.extract_cache_put)
    monkeypatch.setattr(cache, "extract_cache_put", put)
    body = "rejected Tavily body " * 2000 if malformed else "usable Tavily body"
    primary.return_value = _response([{**reported, "title": "Tavily page", "raw_content": body}])
    final_url = reported.get("url")
    unknown = final_url is None or final_url == ""
    for _ in range(2):
        results = _extract([ALLOWED, OTHER], char_limit=2000)
        assert results[1]["url"] == OTHER and results[1]["content"] == "valid sibling"
        assert not results[1].get("error")
        if malformed:
            assert results[0] == {
                "url": ALLOWED, "title": "", "content": "",
                "error": "Invalid final URL: malformed authority",
            }
        else:
            assert results[0]["url"] == (ALLOWED if unknown else final_url)
            assert results[0]["content"] == body and not results[0].get("error")
    assert primary.call_count == (2 if unknown or malformed else 1)
    assert all(call.kwargs["json"]["urls"] == [ALLOWED] for call in primary.call_args_list)
    hit = cache.extract_cache_get(ALLOWED, provider="tavily")
    if malformed:
        put.assert_not_called()
    else:
        assert all(call.kwargs["final_url"] == (None if unknown else final_url) for call in put.call_args_list)
    if unknown or malformed:
        assert hit is None
        assert {entry["url"] for entry in cache._load_index().values()} == {OTHER}
    else:
        assert hit is not None and hit["final_url"] == final_url and hit["content"] == body
    for path in (get_hermes_home() / "cache" / "web").glob("*.md"):
        assert "rejected Tavily body" not in path.read_text()
    rescue.assert_not_called()


@pytest.mark.parametrize("changed_host", ["8.8.8.8", "1.1.1.1"])
def test_cache_rechecks_requested_and_final_current_policy(native_web, changed_host):
    configure, primary, rescue = native_web
    configure(domains=[])
    primary.return_value = _response([{"url": BLOCKED, "raw_content": "old cached body"}])
    assert _extract([ALLOWED])[0]["content"] == "old cached body"
    assert _extract([ALLOWED])[0]["url"] == BLOCKED
    assert primary.call_count == 1
    entry = next(iter(cache._load_index().values()))
    assert entry["url"] == ALLOWED and entry["final_url"] == BLOCKED
    configure(domains=[changed_host])
    results = _extract([ALLOWED])
    assert results[0]["blocked_by_policy"]["host"] == changed_host
    assert not results[0]["content"]
    assert primary.call_count == (1 if changed_host == "8.8.8.8" else 2)
    rescue.assert_not_called()


@pytest.mark.parametrize("provenance", ["legacy", None, "", 42])
def test_public_wrapper_rejects_legacy_or_unknown_cache(native_web, provenance):
    _, primary, _ = native_web
    _extract([ALLOWED])
    index = cache._load_index()
    entry = next(iter(index.values()))
    if provenance == "legacy":
        entry.pop("final_url", None)
    else:
        entry["final_url"] = provenance
    cache._save_index(index)
    primary.return_value = _response([{"url": ALLOWED, "raw_content": "fresh body"}])
    assert _extract([ALLOWED])[0]["content"] == "fresh body"
    assert primary.call_count == 2


@pytest.fixture
def keyed_firecrawl(monkeypatch):
    """Replace the cached SDK transport; keep client routing and normalization native."""
    from tools import web_tools
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    scrape = Mock()
    monkeypatch.setattr(web_tools, "_firecrawl_client", SimpleNamespace(scrape=scrape), raising=False)
    monkeypatch.setattr(web_tools, "_firecrawl_client_config", ("direct", None, "fc-test"), raising=False)
    return scrape


@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.parametrize("rescue_enabled", [False, True])
def test_native_firecrawl_final_refusal_keeps_requested_slot(native_web, keyed_firecrawl, mixed, rescue_enabled):
    configure, primary, rescue = native_web
    configure(extract_backend="firecrawl", keyless_rescue=rescue_enabled)

    def scrape(*, url, formats):
        if url == OTHER:
            raise RuntimeError("provider unavailable")
        assert url == ALLOWED
        return {"data": {"metadata": {"sourceURL": BLOCKED, "title": "private title"}, "markdown": FORBIDDEN_TEXT}}

    keyed_firecrawl.side_effect = scrape
    rescue.side_effect = None
    rescue.return_value = httpx.Response(200, json={"url": OTHER, "title": "Recovered", "content": "recovered page"})
    urls = [OTHER, ALLOWED] if mixed else [ALLOWED]
    results = _extract(urls, char_limit=2000)
    assert [r["url"] for r in results] == urls
    _assert_blocked(results[-1], url=ALLOWED)
    assert "private title" not in json.dumps(results)
    assert "blocked page body" not in json.dumps(results)
    assert keyed_firecrawl.call_count == len(urls)
    primary.assert_not_called()
    if mixed and rescue_enabled:
        assert results[0]["content"] == "recovered page"
        rescue.assert_called_once()
        assert rescue.call_args.kwargs["params"]["url"] == OTHER
    else:
        rescue.assert_not_called()
        if mixed:
            assert "provider unavailable" in results[0]["error"]
            assert results[0]["content"] == ""
    assert cache._load_index() == {}
    assert list((get_hermes_home() / "cache" / "web").glob("*.md")) == []


@pytest.mark.parametrize("shape", ["canonical", "extra", "missing", "unattributable", "reordered", "empty"])
def test_native_parallel_rescue_preserves_policy_and_cached_slots(native_web, keyed_firecrawl, monkeypatch, shape):
    """Native Parallel can emit a canonical document plus a missing-request stub."""
    from plugins.web import keyless_mcp
    configure, primary, rescue = native_web
    configure(extract_backend="firecrawl", provider_tier={
        "firecrawl": "paid", "exa": "paid", "keenable": "paid", "parallel": "free",
    })
    cached_url = "https://9.9.9.9/cached"
    missing_url = "https://8.8.4.4/missing"
    second = shape in {"missing", "unattributable", "reordered", "empty"}

    def scrape(*, url, formats):
        if url == ALLOWED:
            return {"data": {"metadata": {"sourceURL": BLOCKED, "title": "private title"}, "markdown": FORBIDDEN_TEXT}}
        if url == cached_url:
            return {"data": {"metadata": {"sourceURL": url}, "markdown": "cached page"}}
        assert url in {OTHER, missing_url}
        raise RuntimeError("provider unavailable")

    keyed_firecrawl.side_effect = scrape
    assert _extract([cached_url])[0]["content"] == "cached page"
    cached_index = cache._load_index()
    assert len(cached_index) == 1
    rescued_url = OTHER if shape == "reordered" else OTHER + "/"
    documents = [{"url": rescued_url, "full_content": "recovered page"}]
    if shape in {"extra", "unattributable"}:
        documents.insert(0, {"url": ALLOWED, "full_content": "unrequested page"})
    if shape == "unattributable":
        documents.append({"url": "https://9.9.9.9/unrelated", "full_content": "unrequested page"})
    if shape == "reordered":
        documents.insert(0, {"url": missing_url, "full_content": "second recovered page"})
    if shape == "empty":
        documents = []

    def post(endpoint, **kwargs):
        assert endpoint == keyless_mcp.PARALLEL_MCP_URL
        assert kwargs["json"]["params"]["name"] == "web_fetch"
        assert kwargs["json"]["params"]["arguments"]["urls"] == [OTHER] + ([missing_url] if second else [])
        text = json.dumps({"results": documents, "errors": []})
        return httpx.Response(200, json={"result": {"content": [{"type": "text", "text": text}]}})

    mcp = Mock(side_effect=post)
    monkeypatch.setattr("requests.post", mcp)
    urls = [cached_url, OTHER, ALLOWED] + ([missing_url] if second else []) + [cached_url]
    for _ in range(2):
        results = _extract(urls, char_limit=2000)
        assert len(results) == len(urls)
        _assert_blocked(results[2], url=ALLOWED)
        expected_urls = list(urls)
        if shape not in {"empty", "missing", "unattributable"}:
            expected_urls[1] = rescued_url
        assert [r["url"] for r in results] == expected_urls
        assert results[0]["content"] == results[-1]["content"] == "cached page"
        if shape in {"empty", "missing", "unattributable"}:
            assert results[1]["error"] and results[1]["content"] == ""
        else:
            assert results[1]["content"] == "recovered page"
        if second:
            if shape == "reordered":
                assert results[3]["content"] == "second recovered page"
            else:
                assert results[3]["error"] and results[3]["content"] == ""
        assert all(text not in json.dumps(results) for text in ("private title", "blocked page body", "unrequested page"))
    assert keyed_firecrawl.call_count == 1 + 2 * (3 if second else 2)
    assert [call.kwargs["url"] for call in keyed_firecrawl.call_args_list].count(cached_url) == 1
    assert mcp.call_count == 2  # rescued content never becomes a cache hit
    assert cache._load_index() == cached_index
    for path in (get_hermes_home() / "cache" / "web").glob("*.md"):
        assert path.read_text() == "cached page"
    primary.assert_not_called()
    rescue.assert_not_called()


@pytest.mark.parametrize("backend", ["firecrawl", "keenable"])
@pytest.mark.parametrize("reported", [False, True])
def test_native_request_fallback_is_not_cache_provenance(native_web, keyed_firecrawl, monkeypatch, backend, reported):
    configure, primary, rescue = native_web
    configure(extract_backend=backend)
    monkeypatch.setenv("KEENABLE_API_KEY", "kn-test")
    if backend == "firecrawl":
        metadata = {"title": "Page", **({"sourceURL": ALLOWED} if reported else {})}
        keyed_firecrawl.return_value = {"data": {"metadata": metadata, "markdown": "native page"}}
        transport = keyed_firecrawl
    else:
        transport = Mock(return_value=httpx.Response(200, json={
            "title": "Page", "content": "native page", **({"url": ALLOWED} if reported else {}),
        }))
        monkeypatch.setattr("requests.get", transport)
    for _ in range(2):
        result = _extract([ALLOWED])[0]
        assert result["url"] == ALLOWED
        assert result["content"] == "native page"
    assert transport.call_count == (1 if reported else 2)
    if reported:
        entry = next(iter(cache._load_index().values()))
        assert entry["url"] == entry["final_url"] == ALLOWED
    else:
        assert cache._load_index() == {}
        assert list((get_hermes_home() / "cache" / "web").glob("*.cache.md")) == []
    primary.assert_not_called()
    rescue.assert_not_called()


@pytest.mark.parametrize("b_failed", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("duplicate_a", [False, True])
def test_native_keenable_redirect_keeps_source_and_cache_ownership(
    native_web, monkeypatch, b_failed, reverse, duplicate_a,
):
    configure, primary, rescue = native_web
    configure(extract_backend="keenable", keyless_rescue=False)
    monkeypatch.setenv("KEENABLE_API_KEY", "kn-test")

    def fetch(endpoint, **kwargs):
        assert endpoint == "https://api.keenable.ai/v1/fetch"
        requested = kwargs["params"]["url"]
        assert requested in {ALLOWED, OTHER}
        if requested == OTHER and b_failed:
            return httpx.Response(503, text="B unavailable")
        return httpx.Response(200, json={
            "url": OTHER, "title": "A" if requested == ALLOWED else "B",
            "content": "A redirect body" if requested == ALLOWED else "B own body",
        })

    transport = Mock(side_effect=fetch)
    monkeypatch.setattr("requests.get", transport)
    urls = [OTHER, ALLOWED] if reverse else [ALLOWED, OTHER]
    if duplicate_a:
        urls.append(ALLOWED)
    for _ in range(2):
        results = _extract(urls)
        assert len(results) == len(urls)
        for requested, result in zip(urls, results):
            assert result["url"] == OTHER
            if requested == OTHER and b_failed:
                assert "B unavailable" in result["error"]
                assert result["content"] == ""
            else:
                assert not result.get("error")
                assert result["content"] == ("A redirect body" if requested == ALLOWED else "B own body")
    # The second wrapper call must use A's cache and retry only B's failed fetch.
    assert [call.kwargs["params"]["url"] for call in transport.call_args_list] == urls + ([OTHER] if b_failed else [])
    stored = {entry["url"]: entry for entry in cache._load_index().values()}
    assert set(stored) == ({ALLOWED} if b_failed else {ALLOWED, OTHER})
    for requested, entry in stored.items():
        assert entry["final_url"] == OTHER
        hit = cache.extract_cache_get(requested, provider="keenable")
        assert hit is not None
        assert hit["content"] == ("A redirect body" if requested == ALLOWED else "B own body")
    primary.assert_not_called()
    rescue.assert_not_called()


@pytest.mark.parametrize("reverse", [False, True])
def test_distinct_trailing_slash_pages_keep_their_own_cached_bodies(native_web, reverse):
    _, primary, rescue = native_web
    bodies = {ALLOWED: "without slash", ALLOWED + "/": "with slash"}
    primary.return_value = _response([
        {"url": url, "raw_content": body} for url, body in bodies.items()
    ])
    urls = list(reversed(bodies)) if reverse else list(bodies)
    for _ in range(2):
        results = _extract(urls)
        assert [result["url"] for result in results] == urls
        assert [result["content"] for result in results] == [bodies[url] for url in urls]
    primary.assert_called_once()
    assert {entry["url"] for entry in cache._load_index().values()} == set(bodies)
    rescue.assert_not_called()


@pytest.mark.parametrize("shape", ["single", "duplicate", "ambiguous"])
def test_loose_rewrite_success_requires_one_exact_request_identity(native_web, shape):
    _, primary, rescue = native_web
    requested = "http://8.8.8.8/page"
    urls = [requested]
    if shape == "duplicate":
        urls.append(requested)
    elif shape == "ambiguous":
        urls.append(ALLOWED)
    rewritten = ALLOWED + "/"
    primary.return_value = httpx.Response(200, json={
        "results": [{"url": rewritten, "raw_content": "rewritten body"}],
        "failed_urls": urls,
    })
    results = _extract(urls)
    assert len(results) == len(urls)
    if shape == "ambiguous":
        assert [result["url"] for result in results] == urls
        assert all(result["error"] and not result["content"] for result in results)
        assert cache._load_index() == {}
    else:
        assert all(result["url"] == rewritten and result["content"] == "rewritten body" for result in results)
        assert _extract(urls) == results
        primary.assert_called_once()
    rescue.assert_not_called()


@pytest.mark.parametrize("rewritten", [False, True])
def test_authenticated_requests_keep_order_duplicates_and_cache(native_web, rewritten):
    _, primary, rescue = native_web
    urls = [f"https://{userinfo}@8.8.8.8/page" for userinfo in (
        "User:One", "user:One", "User:one", "Other:Two",
    )]
    finals = [url.replace("https://", "http://") + "/" if rewritten else url for url in urls]
    bodies = [f"account {i}" for i in range(len(urls))]
    primary.return_value = _response(list(reversed([
        {"url": url, "raw_content": body} for url, body in zip(finals, bodies)
    ])))
    for _ in range(2):
        results = _extract(urls + [urls[0]])
        assert [r["url"] for r in results] == finals + [finals[0]]
        assert [r["content"] for r in results] == bodies + [bodies[0]]
        assert all(not r.get("error") for r in results)
    primary.assert_called_once()
    for url, final, body in zip(urls, finals, bodies):
        hit = cache.extract_cache_get(url, provider="tavily")
        assert hit is not None
        assert hit["final_url"] == final and hit["content"] == body
    rescue.assert_not_called()


def test_credential_stripped_batch_rows_never_own_authenticated_requests(native_web):
    _, primary, rescue = native_web
    urls = [f"https://{userinfo}@8.8.8.8/page" for userinfo in ("User:One", "user:One")]
    primary.return_value = _response([{"url": ALLOWED, "raw_content": "unattributed body"}])
    for _ in range(2):
        results = _extract(urls + [urls[0]])
        assert [r["url"] for r in results] == urls + [urls[0]]
        assert all(r["error"] and r["content"] == "" for r in results)
    assert primary.call_count == 2
    assert cache._load_index() == {}
    rescue.assert_not_called()


@pytest.mark.parametrize("template", [
    "https://{}@example.com/report", "{}@example.com/report", "//{}@example.com/report",
    "https://{}@example.com:bad/report", "https://{}@[bad/report",
    "https://{}@bücher.de/report",
])
def test_pairing_keys_preserve_raw_case_sensitive_userinfo(template):
    from tools.web_tools_extract import _url_key
    for loose in (False, True):
        keys = [_url_key(template.format(userinfo), loose=loose) for userinfo in (
            "bücher.de:one", "xn--bcher-kva.de:one", "User:One", "user:One", "User:one",
        )]
        assert len(set(keys)) == len(keys)
        if template == "{}@example.com/report":
            raw = template.format("bücher.de:one")
            assert _url_key(raw, loose=loose) == _url_key("//" + raw, loose=loose)


def test_public_validation_rejects_userinfo_changed_by_native_idna(native_web, monkeypatch):
    _, primary, rescue = native_web
    original = "https://bücher.de:one@bücher.de/report"
    other = "https://xn--bcher-kva.de:one@bücher.de/report"
    normalized_other = "https://xn--bcher-kva.de:one@xn--bcher-kva.de/report"
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443)),
    ])
    primary.return_value = _response([{"url": normalized_other, "raw_content": "other account"}])
    for _ in range(2):
        results = _extract([original, other, original])
        for i in (0, 2):
            assert results[i]["url"] == original
            assert "userinfo" in results[i]["error"]
            assert results[i]["content"] == ""
        assert results[1]["url"] == normalized_other
        assert results[1]["content"] == "other account"
    primary.assert_called_once()
    assert primary.call_args.kwargs["json"]["urls"] == [normalized_other]
    assert {e["url"] for e in cache._load_index().values()} == {normalized_other}
    rescue.assert_not_called()


@pytest.mark.parametrize("interrupted", [False, True])
def test_public_cache_misses_body_b_published_under_index_a(native_web, monkeypatch, interrupted):
    from tools import spill_safety
    configure, primary, rescue = native_web
    configure(domains=[])
    with monkeypatch.context() as writes:
        if interrupted:
            cache.extract_cache_put(ALLOWED, "body A", provider="tavily", final_url=ALLOWED)
            writes.setattr(cache, "_save_index", Mock(side_effect=OSError("interrupted publication")))
            cache.extract_cache_put(ALLOWED, "body B", provider="tavily", final_url=BLOCKED)
        else:
            write = spill_safety.write_text_exclusive

            def interleave(path, content, **kwargs):
                write(path, content, **kwargs)
                if content == "body A":
                    cache.extract_cache_put(ALLOWED, "body B", provider="tavily", final_url=BLOCKED)

            writes.setattr(spill_safety, "write_text_exclusive", interleave)
            cache.extract_cache_put(ALLOWED, "body A", provider="tavily", final_url=ALLOWED)
    assert next(iter(cache._load_index().values()))["final_url"] == ALLOWED
    body_path = cache._entry_file_path(ALLOWED, None, "tavily")
    assert body_path is not None and body_path.read_text() == "body B"
    configure(domains=["1.1.1.1"])
    assert cache.extract_cache_get(ALLOWED, provider="tavily") is None
    primary.return_value = _response([{"url": ALLOWED, "raw_content": "fresh A"}])
    for _ in range(2):
        result = _extract([ALLOWED])[0]
        assert result["url"] == ALLOWED and result["content"] == "fresh A"
    primary.assert_called_once()
    rescue.assert_not_called()


@pytest.mark.parametrize("vendor,final", [
    ("exa", None),
    ("parallel", ALLOWED),
    ("firecrawl", None), ("firecrawl", ALLOWED), ("firecrawl", BLOCKED),
    ("keenable", None), ("keenable", ALLOWED), ("keenable", BLOCKED),
])
def test_keyless_ring_preserves_serving_vendor_provenance(native_web, monkeypatch, vendor, final):
    """Exa dispatch may be served by another ring vendor; only its reported URL counts."""
    from plugins.web import keyless_mcp
    from plugins.web.firecrawl.provider import _KeylessFirecrawlClient
    configure, primary, rescue = native_web
    tiers = {v: "paid" for v in ("parallel", "firecrawl", "keenable")}
    tiers.update({"exa": "free", vendor: "free"})
    configure(extract_backend="exa", provider_tier=tiers)
    body = "native ring page"

    def post(endpoint, **kwargs):
        if vendor == "exa":
            assert endpoint == keyless_mcp.EXA_MCP_URL
            text = f"# Page\nURL: {ALLOWED}\n{body}"
        elif vendor == "parallel" and endpoint == keyless_mcp.PARALLEL_MCP_URL:
            text = json.dumps({"results": [{"url": final, "title": "Page", "full_content": body}]})
        else:
            assert endpoint == keyless_mcp.EXA_MCP_URL
            return httpx.Response(429, text="rate limit")
        return httpx.Response(200, json={"result": {"content": [{"type": "text", "text": text}]}})

    mcp = Mock(side_effect=post)
    scrape = Mock(return_value={"data": {
        "metadata": {"title": "Page", **({"sourceURL": final} if final else {})}, "markdown": body,
    }})
    fetch = Mock(return_value=httpx.Response(200, json={
        "title": "Page", "content": body, **({"url": final} if final else {}),
    }))
    monkeypatch.setattr("requests.post", mcp)
    monkeypatch.setattr("requests.get", fetch)
    monkeypatch.setattr(_KeylessFirecrawlClient, "scrape", scrape)
    for _ in range(2):
        entry = _extract([ALLOWED])[0]
        if final == BLOCKED:
            _assert_blocked(entry, url=ALLOWED)
        else:
            assert entry["url"] == ALLOWED and body in entry["content"]
    cached = final == ALLOWED
    attempts = 1 if cached else 2
    assert mcp.call_count == attempts * (2 if vendor == "parallel" else 1)
    assert scrape.call_count == (attempts if vendor == "firecrawl" else 0)
    assert fetch.call_count == (attempts if vendor == "keenable" else 0)
    if cached:
        stored = next(iter(cache._load_index().values()))
        assert stored["url"] == stored["final_url"] == ALLOWED
    else:
        assert cache._load_index() == {}
        assert list((get_hermes_home() / "cache" / "web").glob("*.cache.md")) == []
    primary.assert_not_called()
    rescue.assert_not_called()


@pytest.mark.parametrize("backend", ["exa", "parallel"])
def test_native_reported_url_without_marker_still_caches(native_web, monkeypatch, backend):
    from tools import web_tools
    configure, primary, rescue = native_web
    configure(extract_backend=backend)
    monkeypatch.setenv(f"{backend.upper()}_API_KEY", "sdk-test")
    document = SimpleNamespace(url=ALLOWED, title="Page", text="SDK page", full_content="SDK page")
    payload = SimpleNamespace(results=[document], errors=[])
    if backend == "exa":
        transport = Mock(return_value=payload)
        monkeypatch.setattr(web_tools, "_exa_client", SimpleNamespace(get_contents=transport), raising=False)
    else:
        transport = AsyncMock(return_value=payload)
        client = SimpleNamespace(beta=SimpleNamespace(extract=transport))
        monkeypatch.setattr(web_tools, "_async_parallel_client", client, raising=False)
    for _ in range(2):
        assert _extract([ALLOWED])[0]["content"] == "SDK page"
    transport.assert_called_once()
    entry = next(iter(cache._load_index().values()))
    assert entry["url"] == entry["final_url"] == ALLOWED
    primary.assert_not_called()
    rescue.assert_not_called()



def test_generated_final_policy_refusal_keeps_proven_request_owner(native_web):
    from tools.web_tools_extract import _pair_and_check_results, _pair_results
    configure, primary, rescue = native_web
    rows = [{"url": BLOCKED, "final_url": BLOCKED, "content": FORBIDDEN_TEXT, "_request_url": ALLOWED},
            {"url": OTHER, "content": "other", "_request_url": OTHER}]
    paired = _pair_and_check_results([OTHER, ALLOWED], rows)
    assert paired[1]["_request_url"] == ALLOWED
    assert paired[1]["blocked_by_policy"] and not paired[1]["content"]
    again = _pair_results([ALLOWED, OTHER], list(reversed(paired)))
    assert again[0]["blocked_by_policy"] and again[1]["content"] == "other"
    assert rows[0]["content"] == FORBIDDEN_TEXT
    primary.assert_not_called()
    rescue.assert_not_called()
