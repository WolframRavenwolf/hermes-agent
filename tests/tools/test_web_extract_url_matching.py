"""web_extract pairs backend results to the URLs that were REQUESTED, never by list position (#97378).

Parallel appends failures after successes, Exa omits pages it could not fetch, and the keyless ring
inherits both shapes. Positional pairing cached one page's text under another URL's key and served it
for the whole cache TTL.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

import tools.web_result_cache as wrc
import tools.web_tools as web_tools
from tools.web_tools_extract import _pair_results

A, B, C = "https://a.example/page", "https://b.example/page", "https://c.example/page"


def _doc(url, text):
    return {"url": url, "title": url, "content": text, "raw_content": text, "metadata": {"sourceURL": url}}


def _fail(url, error="fetch failed"):
    return {"url": url, "title": "", "content": "", "error": error}


class _ScriptedProvider:
    name = "scripted"

    def __init__(self, results):
        self._results = results

    def extract(self, urls, **kwargs):
        if isinstance(self._results, Exception):
            raise self._results
        return self._results


@pytest.mark.asyncio
async def test_results_are_paired_by_url_not_position(tmp_path, monkeypatch):
    """A batch returned as [failure(B), document(A)] for the request [A, B] must yield A's document at
    A's position and cache A's text under A's key only — B stays a miss."""
    cache_dir = tmp_path / "cache" / "web"
    cache_dir.mkdir(parents=True)
    monkeypatch.setattr(wrc, "_cache_dir", lambda: cache_dir)
    monkeypatch.setattr(wrc, "_web_config", lambda: {})
    provider = _ScriptedProvider([_fail(B), _doc(A, "text of A")])
    monkeypatch.setattr(web_tools, "_get_extract_backend", lambda: provider.name)
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web_tools, "_resolve_extract_provider", lambda backend: (provider, None))

    async def _allow(url):
        return True

    monkeypatch.setattr(web_tools, "async_is_safe_url", _allow)

    results = json.loads(await web_tools.web_extract_tool([A, B]))["results"]

    assert [r["url"] for r in results] == [A, B]
    assert results[0]["content"] == "text of A" and not results[0].get("error")
    assert results[1]["error"] == "fetch failed"
    assert wrc.extract_cache_get(A, provider=provider.name)["content"] == "text of A"
    assert wrc.extract_cache_get(B, provider=provider.name) is None


@pytest.mark.parametrize(
    "urls, results, expected",
    [
        # A backend echoing a cosmetically different URL still pairs: trailing slash, http->https, apex->www.
        (["https://a.example/x"], [_doc("https://a.example/x/", "slash")], ["slash"]),
        (["http://a.example/x"], [_doc("https://a.example/x", "https")], ["https"]),
        (["https://a.example/x"], [_doc("https://www.a.example/x", "www")], ["www"]),
        # The keyless ring emits a "no content" stub NEXT TO the document when it rewrites a URL.
        (["https://a.example/x"], [_fail("https://a.example/x", "no content returned"), _doc("https://a.example/x/", "doc")], ["doc"]),
        # The query is part of the page: a missing ?page=1 never inherits ?page=2's document.
        (["https://a.example/x?page=1", "https://a.example/x?page=2"], [_doc("https://a.example/x?page=2", "p2")], [None, "p2"]),
        # A lone unmatched result cannot prove a redirect from the missing request.
        ([A, B], [_doc("https://cdn.b.example/home", "moved"), _doc(A, "a")], ["a", None]),
        # Several unmatched results are never guessed at: they are dropped, not attached.
        ([A, B], [_doc("https://x.example/1", "x1"), _doc("https://x.example/2", "x2")], [None, None]),
    ],
    ids=["trailing-slash", "scheme", "www", "document-beats-stub", "query-is-a-page", "lone-leftover", "no-guessing"],
)
def test_pair_results_matches_canonical_variants_only(urls, results, expected):
    paired = _pair_results(urls, results)
    assert [None if r.get("error") else r["content"] for r in paired] == expected


def test_redirect_with_original_request_provenance_retains_final_url():
    moved = _doc("https://cdn.b.example/home", "moved")
    moved["_request_url"] = B
    paired = _pair_results([A, B, B], [moved, _doc(A, "a")])
    assert [r["content"] for r in paired] == ["a", "moved", "moved"]
    assert [r["url"] for r in paired] == [A, moved["url"], moved["url"]]
    assert all(r["metadata"]["sourceURL"] == moved["url"] for r in paired[1:])


@pytest.mark.asyncio
@pytest.mark.parametrize("raised", [False, True], ids=["all-failed", "exception"])
@pytest.mark.parametrize("rescued, expected", [
    ([_doc(B, "b"), _doc(A, "a"), _doc(C, "c")], ["a", "c", "b"]),
    ([_doc(B, "b")], [None, None, "b"]),
    ([_doc(B, "b"), _doc("", "UNATTRIBUTED"), _doc(C, "c")], [None, "c", "b"]),
], ids=["shuffled", "sparse", "unlabeled-first"])
async def test_rescue_pairs_results_without_caching(tmp_path, monkeypatch, raised, rescued, expected):
    """Use the real dispatch/rescue chain; only the provider and ring transport are scripted."""
    cache_dir = tmp_path / "cache" / "web"
    cache_dir.mkdir(parents=True)
    monkeypatch.setattr(wrc, "_cache_dir", lambda: cache_dir)
    monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"cache_enabled": True, "keyless_rescue": True})
    monkeypatch.setattr("agent.web_search_registry._keyless_tier_enabled", lambda: True)
    monkeypatch.setattr("tools.url_safety.socket.getaddrinfo",
                        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))])
    urls = [A, C, B]
    provider = _ScriptedProvider(RuntimeError("backend down") if raised else [_fail(u, "backend down") for u in urls])
    monkeypatch.setattr(web_tools, "_get_extract_backend", lambda: provider.name)
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web_tools, "_resolve_extract_provider", lambda backend: (provider, None))
    with patch("plugins.web.keyless_mcp.extract_with_failover", side_effect=lambda *args: json.loads(json.dumps(rescued))) as ring, \
         patch.object(provider, "extract", wraps=provider.extract) as extract, \
         patch.object(wrc, "extract_cache_put", wraps=wrc.extract_cache_put) as cache_put:
        batches = [json.loads(await web_tools.web_extract_tool(urls))["results"] for _ in range(2)]
    cache_put.assert_not_called()
    assert all(wrc.extract_cache_get(u, provider=provider.name) is None for u in urls)
    assert extract.call_count == ring.call_count == 2
    assert all(call.args == (provider.name, urls) for call in ring.call_args_list)
    for rows in batches:
        assert [r["url"] for r in rows] == urls
        assert [None if r.get("error") else r["content"] for r in rows] == expected
        for row, text in zip(rows, expected):
            if text is None:
                assert not row["content"] and "no result" in row["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["exa", "parallel"])
@pytest.mark.parametrize("with_cache_hit", [False, True], ids=["single-request", "one-cache-miss"])
async def test_single_fetch_redirect_preserves_provider_output_and_request_cache(
    tmp_path, monkeypatch, backend, with_cache_hit,
):
    """The actual SDK request, not the public batch size, supplies single-fetch provenance."""
    from plugins.web.exa.provider import ExaWebSearchProvider
    from plugins.web.parallel.provider import ParallelWebSearchProvider

    final_url, title, text = "https://publisher.example/articles/42", "Redirected article", "article text"
    page = SimpleNamespace(url=final_url, title=title, text=text, full_content=text)
    response = SimpleNamespace(results=[page], errors=[])
    if backend == "exa":
        provider = ExaWebSearchProvider()
        transport = Mock(return_value=response)
        monkeypatch.setattr(web_tools, "_exa_client", SimpleNamespace(get_contents=transport))
    else:
        provider = ParallelWebSearchProvider()
        transport = AsyncMock(return_value=response)
        monkeypatch.setattr(web_tools, "_async_parallel_client", SimpleNamespace(beta=SimpleNamespace(extract=transport)))
    monkeypatch.setenv(provider.KEY_ENV, "test-key")
    cache_dir = tmp_path / "cache" / "web"
    cache_dir.mkdir(parents=True)
    monkeypatch.setattr(wrc, "_cache_dir", lambda: cache_dir)
    monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"cache_enabled": True, "keyless_rescue": False})
    monkeypatch.setattr(web_tools, "_get_extract_backend", lambda: provider.name)
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web_tools, "_resolve_extract_provider", lambda backend: (provider, None))
    monkeypatch.setattr("tools.url_safety.socket.getaddrinfo",
                        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))])
    urls = [A, B] if with_cache_hit else [B]
    if with_cache_hit:
        wrc.extract_cache_put(A, "cached A", provider=provider.name)
    assert wrc.extract_cache_get(B, provider=provider.name) is None

    with patch.object(wrc, "extract_cache_put", wraps=wrc.extract_cache_put) as cache_put:
        rows = json.loads(await web_tools.web_extract_tool(urls))["results"]
        if backend == "exa":
            transport.assert_called_once_with([B], text=True)
        else:
            transport.assert_awaited_once_with(urls=[B], full_content=True)
        assert [r["url"] for r in rows] == ([A, final_url] if with_cache_hit else [final_url])
        assert rows[-1]["content"] == text and rows[-1]["title"] == title and not rows[-1].get("error")
        if with_cache_hit:
            assert rows[0]["content"] == "cached A" and not rows[0].get("error")
        cached_rows = json.loads(await web_tools.web_extract_tool(urls))["results"]
    cache_put.assert_called_once_with(B, text, title, format=None, provider=provider.name)
    assert transport.call_count == 1
    assert [r["url"] for r in cached_rows] == urls
    assert cached_rows[-1]["content"] == text and not cached_rows[-1].get("error")
    hit = wrc.extract_cache_get(B, provider=provider.name)
    assert hit is not None and hit["content"] == text
    assert wrc.extract_cache_get(final_url, provider=provider.name) is None
    if with_cache_hit:
        hit = wrc.extract_cache_get(A, provider=provider.name)
        assert hit is not None and hit["content"] == "cached A"


@pytest.mark.asyncio
@pytest.mark.parametrize("raised", [False, True], ids=["all-failed", "exception"])
@pytest.mark.parametrize("extra_results, expected_error", [
    ([], None),
    ([_doc(C, "another document")], "no result"),
    ([_fail(C)], "no result"),
    ([_fail(B, "matched failure")], "matched failure"),
], ids=["single-success", "ambiguous-successes", "mixed-leftovers", "already-matched"])
async def test_single_fetch_rescue_redirect_requires_unambiguous_success_without_caching(
    tmp_path, monkeypatch, raised, extra_results, expected_error,
):
    """Real exception/all-failed rescue retains a sole redirect, never overrides a keyed result."""
    cache_dir = tmp_path / "cache" / "web"
    cache_dir.mkdir(parents=True)
    monkeypatch.setattr(wrc, "_cache_dir", lambda: cache_dir)
    monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"cache_enabled": True, "keyless_rescue": True})
    monkeypatch.setattr("agent.web_search_registry._keyless_tier_enabled", lambda: True)
    monkeypatch.setattr("tools.url_safety.socket.getaddrinfo",
                        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))])
    provider = _ScriptedProvider(RuntimeError("backend down") if raised else [_fail(B, "backend down")])
    monkeypatch.setattr(web_tools, "_get_extract_backend", lambda: provider.name)
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web_tools, "_resolve_extract_provider", lambda backend: (provider, None))
    final_url, text = "https://publisher.example/articles/42", "rescued article"
    rescued = [_doc(final_url, text), *extra_results]
    with patch("plugins.web.keyless_mcp.extract_with_failover", side_effect=lambda *args: json.loads(json.dumps(rescued))) as ring, \
         patch.object(provider, "extract", wraps=provider.extract) as extract, \
         patch.object(wrc, "extract_cache_put", wraps=wrc.extract_cache_put) as cache_put:
        batches = [json.loads(await web_tools.web_extract_tool([B]))["results"] for _ in range(2)]
    cache_put.assert_not_called()
    assert extract.call_count == ring.call_count == 2
    assert all(call.args == ([B],) for call in extract.call_args_list)
    assert all(call.args == (provider.name, [B]) for call in ring.call_args_list)
    assert all(wrc.extract_cache_get(u, provider=provider.name) is None for u in (B, C, final_url))
    assert not list(cache_dir.iterdir())
    for rows in batches:
        assert len(rows) == 1
        if expected_error is None:
            assert rows[0]["url"] == final_url and rows[0]["content"] == text and not rows[0].get("error")
        else:
            assert rows[0]["url"] == B and not rows[0]["content"] and expected_error in rows[0]["error"]


@pytest.fixture
def native_extract(monkeypatch, web_registry_populated):
    """Use the native providers and disk cache with isolated configuration and DNS."""
    from hermes_constants import get_hermes_home
    from tools import website_policy

    def configure(backend):
        config = {"web": {
            "backend": backend, "cache_enabled": True, "keyless_rescue": False,
            "provider_tier": {backend: "paid"},
        }}
        (get_hermes_home() / "config.yaml").write_text(json.dumps(config))
        monkeypatch.setenv(f"{backend.upper()}_API_KEY", "synthetic-test")
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: config["web"])
        with website_policy._cache_lock:
            website_policy._cached_policy = None

    monkeypatch.setattr("tools.url_safety.socket.getaddrinfo",
                        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))])
    yield configure
    with website_policy._cache_lock:
        website_policy._cached_policy = None


@pytest.mark.asyncio
@pytest.mark.parametrize("urls", [[A, B], [B, A], [A, B, A]], ids=["a-first", "b-first", "duplicate-a"])
@pytest.mark.parametrize("b_fails", [False, True], ids=["distinct-b-body", "b-error"])
async def test_native_keenable_request_owns_redirect_before_final_alias(native_extract, urls, b_fails):
    """Keenable's A-to-B document belongs to A; B retains its own body or error."""
    native_extract("keenable")

    def response(endpoint, *, params, **kwargs):
        url = params["url"]
        if url == B and b_fails:
            return SimpleNamespace(status_code=503, text="B unavailable")
        data = {"url": B, "title": url, "content": "A body" if url == A else "B body"}
        return SimpleNamespace(status_code=200, json=lambda: data)

    with patch("requests.get", side_effect=response) as get:
        first = json.loads(await web_tools.web_extract_tool(urls))["results"]
        again = json.loads(await web_tools.web_extract_tool(urls))["results"]
    expected = [None if url == B and b_fails else "A body" if url == A else "B body" for url in urls]
    for batch in (first, again):
        assert [None if row.get("error") else row["content"] for row in batch] == expected
        for url, row in zip(urls, batch):
            if url == B and b_fails:
                assert row["error"] == "Keenable extract failed: B unavailable" and not row["content"]
    assert [row["url"] for row in first] == [B] * len(urls)
    assert [row["url"] for row in again] == urls
    cached_a = wrc.extract_cache_get(A, provider="keenable")
    assert cached_a is not None and cached_a["content"] == "A body"
    cached_b = wrc.extract_cache_get(B, provider="keenable")
    if b_fails:
        assert cached_b is None
    else:
        assert cached_b is not None and cached_b["content"] == "B body"
    assert [call.kwargs["params"]["url"] for call in get.call_args_list] == urls + ([B] if b_fails else [])


@pytest.mark.asyncio
async def test_native_slash_paths_keep_distinct_bodies_across_cache_hits(native_extract):
    native_extract("tavily")
    urls = [A, A + "/", A]
    raw = {"results": [
        {"url": A + "/", "raw_content": "slash body"},
        {"url": A, "raw_content": "plain body"},
    ]}
    response = SimpleNamespace(status_code=200, json=lambda: raw)
    with patch("plugins.web.tavily.provider.httpx.post", return_value=response) as post:
        batches = [json.loads(await web_tools.web_extract_tool(urls))["results"] for _ in range(2)]
    for batch in batches:
        assert [row["url"] for row in batch] == urls
        assert [row["content"] for row in batch] == ["plain body", "slash body", "plain body"]
        assert not any(row.get("error") for row in batch)
    post.assert_called_once()
    for url, expected in [(A, "plain body"), (A + "/", "slash body")]:
        cached = wrc.extract_cache_get(url, provider="tavily")
        assert cached is not None and cached["content"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("urls, expected", [
    ([A], ["rewritten"]),
    ([A, A], ["rewritten", "rewritten"]),
    ([A, "http://a.example/page"], [None, None]),
], ids=["single-owner", "duplicate-owner", "competing-owners"])
async def test_native_loose_success_upgrades_only_an_unambiguous_owner(native_extract, urls, expected):
    native_extract("tavily")
    raw = {
        "results": [{"url": "https://www.a.example/page/", "raw_content": "rewritten"}],
        "failed_results": [{"url": A, "error": "no content"}],
    }
    response = SimpleNamespace(status_code=200, json=lambda: raw)
    with patch("plugins.web.tavily.provider.httpx.post", return_value=response):
        rows = json.loads(await web_tools.web_extract_tool(urls))["results"]
    assert [None if row.get("error") else row["content"] for row in rows] == expected
    if expected[0] is None:
        assert rows[0]["error"] == "no content"
        assert "no result" in rows[1]["error"]
        assert all(wrc.extract_cache_get(url, provider="tavily") is None for url in urls)
    else:
        cached = wrc.extract_cache_get(A, provider="tavily")
        assert cached is not None and cached["content"] == "rewritten"


@pytest.mark.asyncio
@pytest.mark.parametrize("rewritten", [False, True], ids=["exact", "loose"])
@pytest.mark.parametrize("reverse", [False, True], ids=["forward-requests", "reverse-requests"])
async def test_native_authenticated_requests_keep_order_duplicates_and_cache(native_extract, rewritten, reverse):
    native_extract("tavily")
    urls = [f"https://{userinfo}@a.example/page" for userinfo in (
        "User:One", "user:One", "User:one", "Other:Two",
    )]
    bodies = {url: f"account {index}" for index, url in enumerate(urls)}
    finals = {url: url.replace("https://", "http://").replace("@a.", "@www.a.") + "/"
              if rewritten else url for url in urls}
    raw = {"results": [
        {"url": finals[url], "raw_content": bodies[url]} for url in reversed(urls)
    ]}
    response = SimpleNamespace(status_code=200, json=lambda: raw)
    requested = list(reversed(urls)) if reverse else urls[:]
    requested.append(requested[0])
    with patch("plugins.web.tavily.provider.httpx.post", return_value=response) as post, \
         patch("plugins.web.keyless_mcp.extract_with_failover") as rescue:
        first = json.loads(await web_tools.web_extract_tool(requested))["results"]
        again = json.loads(await web_tools.web_extract_tool(requested))["results"]
    assert [row["url"] for row in first] == [finals[url] for url in requested]
    assert [row["url"] for row in again] == requested
    for batch in (first, again):
        assert [row["content"] for row in batch] == [bodies[url] for url in requested]
        assert not any(row.get("error") for row in batch)
    post.assert_called_once()
    assert post.call_args.kwargs["json"]["urls"] == requested
    for url, body in bodies.items():
        hit = wrc.extract_cache_get(url, provider="tavily")
        assert hit is not None and hit["content"] == body
    assert {entry["url"] for entry in wrc._load_index().values()} == set(urls)
    rescue.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("rewritten", [False, True], ids=["exact", "loose"])
async def test_native_credential_stripped_batch_rows_never_own_authenticated_requests(native_extract, rewritten):
    native_extract("tavily")
    urls = [f"https://{userinfo}@a.example/page" for userinfo in ("User:One", "user:One")]
    requested = urls + [urls[0]]
    stripped = "http://www.a.example/page/" if rewritten else A
    raw = {"results": [{"url": stripped, "raw_content": "unattributed body"}]}
    response = SimpleNamespace(status_code=200, json=lambda: raw)
    with patch("plugins.web.tavily.provider.httpx.post", return_value=response) as post, \
         patch("plugins.web.keyless_mcp.extract_with_failover") as rescue:
        batches = [json.loads(await web_tools.web_extract_tool(requested))["results"] for _ in range(2)]
    for batch in batches:
        assert [row["url"] for row in batch] == requested
        assert all("no result" in row["error"] and row["content"] == "" for row in batch)
    assert post.call_count == 2
    assert all(call.kwargs["json"]["urls"] == requested for call in post.call_args_list)
    assert wrc._load_index() == {}
    rescue.assert_not_called()


@pytest.mark.asyncio
async def test_native_validation_rejects_userinfo_changed_by_idna_in_original_slots(native_extract):
    native_extract("tavily")
    original = "https://bücher.de:one@bücher.de/report"
    other = "https://xn--bcher-kva.de:one@bücher.de/report"
    normalized_other = "https://xn--bcher-kva.de:one@xn--bcher-kva.de/report"
    raw = {"results": [{"url": normalized_other, "raw_content": "other account"}]}
    response = SimpleNamespace(status_code=200, json=lambda: raw)
    with patch("plugins.web.tavily.provider.httpx.post", return_value=response) as post, \
         patch("plugins.web.keyless_mcp.extract_with_failover") as rescue, \
         patch.object(wrc, "extract_cache_get", wraps=wrc.extract_cache_get) as cache_get:
        batches = [json.loads(await web_tools.web_extract_tool([original, other, original]))["results"]
                   for _ in range(2)]
    for batch in batches:
        assert len(batch) == 3
        for index in (0, 2):
            assert batch[index]["url"] == original
            assert "userinfo" in batch[index]["error"] and batch[index]["content"] == ""
        assert batch[1]["url"] == normalized_other
        assert batch[1]["content"] == "other account" and not batch[1].get("error")
    post.assert_called_once()
    assert post.call_args.kwargs["json"]["urls"] == [normalized_other]
    assert all(call.args[0] == normalized_other for call in cache_get.call_args_list)
    assert {entry["url"] for entry in wrc._load_index().values()} == {normalized_other}
    rescue.assert_not_called()


@pytest.mark.asyncio
async def test_native_changed_userinfo_rejection_precedes_legacy_cache_and_egress(native_extract):
    native_extract("tavily")
    original = "https://bücher.de:one@bücher.de/report"
    legacy_alias = "https://xn--bcher-kva.de:one@bücher.de/report"
    wrc.extract_cache_put(legacy_alias, "legacy other-account body", provider="tavily")
    assert wrc.extract_cache_get(legacy_alias, provider="tavily") is not None
    with patch("plugins.web.tavily.provider.httpx.post") as post, \
         patch("plugins.web.keyless_mcp.extract_with_failover") as rescue, \
         patch("tools.url_safety.socket.getaddrinfo", return_value=[
             (2, 1, 6, "", ("93.184.216.34", 443)),
         ]) as dns, \
         patch.object(wrc, "extract_cache_get", wraps=wrc.extract_cache_get) as cache_get:
        batches = [json.loads(await web_tools.web_extract_tool([original]))["results"] for _ in range(2)]
    for batch in batches:
        assert len(batch) == 1 and batch[0]["url"] == original
        assert "userinfo" in batch[0]["error"] and batch[0]["content"] == ""
    cache_get.assert_not_called()
    dns.assert_not_called()
    post.assert_not_called()
    rescue.assert_not_called()
    hit = wrc.extract_cache_get(legacy_alias, provider="tavily")
    assert hit is not None and hit["content"] == "legacy other-account body"


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

@pytest.mark.asyncio
@pytest.mark.parametrize("order", [[0, 1], [1, 0], [0, 1, 0]])
@pytest.mark.parametrize("b_fails", [False, True])
@pytest.mark.parametrize("authenticated", [False, True])
async def test_native_firecrawl_final_collision_stays_in_request_cache(
    native_extract, monkeypatch, order, b_fails, authenticated,
):
    from copy import deepcopy
    from plugins.web.firecrawl import provider as fc
    from hermes_constants import get_hermes_home
    native_extract("firecrawl")
    # Exercise the native REST client and keyed provider path; only HTTP is mocked.
    monkeypatch.setattr(fc, "Firecrawl", lambda **kw: fc._KeylessFirecrawlClient())
    monkeypatch.setattr(web_tools, "_firecrawl_client", None)
    request_a = "https://User:One@b.example/page" if authenticated else A
    urls = [[request_a, B][i] for i in order]
    payloads = {u: {"data": {
        "metadata": {"sourceURL": B, "title": "Final page"},
        "markdown": "A body" if u == request_a else "B body",
        "_request_url": C,  # A vendor field is never local ownership evidence.
    }} for u in (request_a, B)}
    before = deepcopy(payloads)

    def post(endpoint, *, json, **kwargs):
        assert endpoint.endswith("/v2/scrape")
        u = json["url"]
        if u == B and b_fails:
            raise RuntimeError("B unavailable")
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payloads[u])

    pre_trim = []
    trim_results = web_tools._trim_results

    def capture_trim_results(results):
        # Native metadata is intentionally omitted from the public wire result.
        pre_trim.append(deepcopy(results))
        return trim_results(results)

    with patch.object(fc.httpx, "post", side_effect=post) as transport, \
         patch.object(web_tools, "_trim_results", side_effect=capture_trim_results):
        first = json.loads(await web_tools.web_extract_tool(urls))["results"]
        warm = json.loads(await web_tools.web_extract_tool(urls))["results"]
    for rows in (first, warm):
        assert [None if r.get("error") else r["content"] for r in rows] == [
            None if u == B and b_fails else "A body" if u == request_a else "B body" for u in urls
        ]
        assert all("_request_url" not in r for r in rows)
    assert len(pre_trim) == 2 and len(pre_trim[0]) == len(urls)
    assert all(r["metadata"]["sourceURL"] == B for r in pre_trim[0] if not r.get("error"))
    assert all(r["url"] == B for r in first if not r.get("error"))
    assert [c.kwargs["json"]["url"] for c in transport.call_args_list] == urls + ([B] if b_fails else [])
    assert wrc.extract_cache_get(request_a, provider="firecrawl")["content"] == "A body"
    cached_b = wrc.extract_cache_get(B, provider="firecrawl")
    assert cached_b is None if b_fails else cached_b["content"] == "B body"
    assert payloads == before
    for p in (get_hermes_home() / "cache" / "web").rglob("*"):
        if p.is_file():
            assert "_request_url" not in p.read_text()


@pytest.mark.parametrize("marker", [None, 42, [], {}, "", A + "/", "http://a.example/page", C])
@pytest.mark.parametrize("urls", [[A], [A, B]])
def test_present_invalid_marker_never_enters_alias_or_singleton_fallback(marker, urls, caplog):
    row = {**_doc(A, "must not attach"), "_request_url": marker}
    rows = _pair_results(urls, [row])
    assert all(r.get("error") and not r["content"] for r in rows)
    assert "must not attach" not in caplog.text


def test_reported_source_url_never_asserts_request_ownership():
    row = _doc(B, "B-compatible body")
    row["metadata"]["sourceURL"] = A
    rows = _pair_results([A, B], [row])
    assert rows[0].get("error") and rows[1]["content"] == "B-compatible body"
    # This compatibility match makes no claim about hidden vendor batch correlation.


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("loose", [False, True])
def test_distinct_untagged_successes_are_content_free_ambiguity(reverse, loose):
    rows = [_doc(B, "first body"), _doc("http://www.b.example/page/" if loose else B, "second body")]
    if reverse:
        rows.reverse()
    result = _pair_results([A, B], rows)[1]
    assert "ambiguous" in result["error"].lower()
    assert result["content"] == "" and result.get("raw_content", "") == ""
    assert "first body" not in json.dumps(result) and "second body" not in json.dumps(result)


def test_identical_untagged_duplicates_coalesce_without_guessing_other_owner():
    row = _doc(B, "same body")
    rows = _pair_results([A, B, B], [row, dict(row)])
    assert rows[0].get("error") and [r["content"] for r in rows[1:]] == ["same body", "same body"]


@pytest.mark.parametrize("proven_success", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_success_upgrade_requires_common_request_proof(proven_success, reverse):
    error = {**_fail(B, "owned failure"), "_request_url": B}
    success = _doc(B, "success body")
    if proven_success:
        success["_request_url"] = B
    rows = [error, success]
    if reverse:
        rows.reverse()
    result = _pair_results([A, B], rows)[1]
    if proven_success:
        assert result["content"] == "success body" and not result.get("error")
    else:
        assert result["error"] == "owned failure" and result["content"] == ""


def test_singleton_alias_can_upgrade_stub_but_unknown_redirect_cannot():
    error = {**_fail(A, "owned failure"), "_request_url": A}
    assert _pair_results([A], [error, _doc(A + "/", "alias")])[0]["content"] == "alias"
    assert _pair_results([A], [error, _doc(C, "unknown")])[0]["error"] == "owned failure"


@pytest.mark.asyncio
@pytest.mark.parametrize("cache_mode", ["cold", "mixed", "warm"])
async def test_private_marker_removed_from_copies_on_every_safe_return(monkeypatch, cache_mode):
    from copy import deepcopy
    from tools import web_tools_extract as extract
    cached = {**_doc(A, "cached"), "_request_url": A}
    fetched = [{**_doc(B, "fetched"), "_request_url": B}]
    before = deepcopy((cached, fetched))
    monkeypatch.setattr(wrc, "extract_cache_get", lambda u, **kw: cached if cache_mode == "warm" or (cache_mode == "mixed" and u == A) else None)
    monkeypatch.setattr("tools.website_policy.check_website_access", lambda u: None)
    dispatch = AsyncMock(return_value=fetched)
    monkeypatch.setattr(extract, "_dispatch_extract", dispatch)
    urls = [B] if cache_mode == "cold" else [A, B]
    rows = await extract._extract_safe_urls(_ScriptedProvider([]), urls, None)
    assert all("_request_url" not in r for r in rows)
    assert (cached, fetched) == before
    assert all(r is not cached and r is not fetched[0] for r in rows)
    assert dispatch.call_count == (0 if cache_mode == "warm" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["exa", "parallel"])
async def test_native_batch_reordering_keeps_url_only_compatibility(native_extract, monkeypatch, backend):
    native_extract(backend)
    page = lambda u: SimpleNamespace(url=u, title=u, text=u, full_content=u, excerpts=[])
    response = SimpleNamespace(results=[page(B), page(A)], errors=[])
    if backend == "exa":
        transport = Mock(return_value=response)
        monkeypatch.setattr(web_tools, "_exa_client", SimpleNamespace(get_contents=transport))
    else:
        transport = AsyncMock(return_value=response)
        monkeypatch.setattr(web_tools, "_async_parallel_client", SimpleNamespace(beta=SimpleNamespace(extract=transport)))
    for _ in range(2):
        rows = json.loads(await web_tools.web_extract_tool([A, B, A]))["results"]
        assert [r["content"] for r in rows] == [A, B, A]
        assert all("_request_url" not in r for r in rows)
    assert transport.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unsafe", "input-policy", "final-policy", "timeout", "exception", "interrupt", "mid-interrupt"])
async def test_firecrawl_local_early_errors_keep_request_owner(monkeypatch, failure):
    from plugins.web.firecrawl import provider as fc
    block = {"host": "b.example", "rule": "b.example", "source": "config", "message": "Blocked by website policy"}
    monkeypatch.setattr(fc, "_use_keyless_ring", lambda: False)
    monkeypatch.setattr(fc, "is_safe_url", lambda u: failure != "unsafe")
    monkeypatch.setattr(fc, "check_website_access", lambda u: block if (failure == "input-policy" and u == A) or (failure == "final-policy" and u == B) else None)
    interruptions = iter([False, True])
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: next(interruptions) if failure == "mid-interrupt" else failure == "interrupt")
    scrape = Mock(return_value={"data": {"metadata": {"sourceURL": B}, "markdown": "sensitive body"}})
    if failure == "exception":
        scrape.side_effect = RuntimeError("unavailable")
    if failure == "timeout":
        scrape.side_effect = TimeoutError()
    monkeypatch.setattr(fc, "_get_firecrawl_client", lambda: SimpleNamespace(scrape=scrape))
    result = (await fc.FirecrawlWebSearchProvider().extract([A]))[0]
    assert result["_request_url"] == A and result.get("error")
    assert not result.get("content")
    assert scrape.call_count == (0 if failure in {"input-policy", "interrupt", "mid-interrupt"} else 1)


@pytest.mark.asyncio
async def test_reordered_owned_policy_error_is_preserved_before_rescue(native_extract, monkeypatch):
    from plugins.web import keyless_mcp
    native_extract("scripted")
    monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"keyless_rescue": True, "cache_enabled": True})
    monkeypatch.setattr("agent.web_search_registry._keyless_tier_enabled", lambda: True)
    block = {**_fail(B, "Blocked by website policy"), "_request_url": A,
             "blocked_by_policy": {"host": "b.example", "rule": "b.example", "source": "config"}}
    failed = {**_fail(A, "backend down"), "_request_url": B}
    provider = _ScriptedProvider([failed, block])
    monkeypatch.setattr(web_tools, "_resolve_extract_provider", lambda backend: (provider, None))
    monkeypatch.setattr(web_tools, "_get_extract_backend", lambda: provider.name)
    with patch.object(keyless_mcp, "extract_with_failover", return_value=[{**_doc(C, "rescued B"), "_request_url": B}]) as ring:
        rows = json.loads(await web_tools.web_extract_tool([A, B]))["results"]
    ring.assert_called_once_with(provider.name, [B])
    assert rows[0]["blocked_by_policy"] and rows[0]["content"] == ""
    assert rows[1]["content"] == "rescued B"
    assert all("_request_url" not in r for r in rows)
    assert wrc.extract_cache_get(B, provider=provider.name) is None


@pytest.mark.asyncio
async def test_invalid_and_unsafe_public_slots_never_serialize_private_markers(native_extract):
    native_extract("tavily")
    result = await web_tools.web_extract_tool([None, "http://127.0.0.1/private"])
    assert "_request_url" not in result
    rows = json.loads(result)["results"]
    assert len(rows) == 2 and all(r.get("error") for r in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True], ids=["authenticated-first", "anonymous-first"])
async def test_native_firecrawl_paging_files_keep_result_content(native_extract, monkeypatch, reverse):
    from pathlib import Path
    from plugins.web.firecrawl import provider as fc

    native_extract("firecrawl")
    # Explicit selection without an API key uses the native REST client; the paid
    # tier in native_extract keeps dispatch out of the separate keyless ring.
    monkeypatch.delenv("FIRECRAWL_API_KEY")
    monkeypatch.setattr(web_tools, "_firecrawl_client", None)
    request_a = "https://User:One@b.example/page"
    urls = [request_a, B]
    if reverse:
        urls.reverse()
    bodies = {u: (label + " paragraph\n") * 400 for u, label in (
        (request_a, "authenticated A"), (B, "anonymous B"), (C, "later C"),
    )}

    def post(endpoint, *, json, **kwargs):
        assert endpoint.endswith("/v2/scrape")
        data = {"data": {"metadata": {"sourceURL": B, "title": "Final page"},
                         "markdown": bodies[json["url"]]}}
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: data)

    with patch.object(fc.httpx, "post", side_effect=post) as transport:
        first = json.loads(await web_tools.web_extract_tool(urls, char_limit=2000))["results"]
        assert [row["url"] for row in first] == [B, B]
        first_paths = [Path(row["content"].split("Full text saved to: ", 1)[1].splitlines()[0]) for row in first]
        expected = [bodies[u] for u in urls]
        # Read emitted paths only after both writes: the first must not become
        # the second result's body merely because their final URL is identical.
        assert [p.read_text() for p in first_paths] == expected
        assert len(set(first_paths)) == len(first_paths)

        warm = json.loads(await web_tools.web_extract_tool(urls, char_limit=2000))["results"]
        warm_paths = [Path(row["content"].split("Full text saved to: ", 1)[1].splitlines()[0]) for row in warm]
        assert [p.read_text() for p in warm_paths] == expected
        assert [call.kwargs["json"]["url"] for call in transport.call_args_list] == urls
        for u in urls:
            cached = wrc.extract_cache_get(u, provider="firecrawl")
            assert cached is not None and cached["content"] == bodies[u]

        later = json.loads(await web_tools.web_extract_tool([C], char_limit=2000))["results"]
        later_path = Path(later[0]["content"].split("Full text saved to: ", 1)[1].splitlines()[0])
        assert later[0]["url"] == B and later_path.read_text() == bodies[C]
        assert [call.kwargs["json"]["url"] for call in transport.call_args_list] == [*urls, C]

    assert [p.read_text() for p in first_paths] == expected
    assert [p.read_text() for p in warm_paths] == expected
    assert len(set(first_paths + warm_paths + [later_path])) == 5
    for row in first + warm + later:
        assert set(row) == {"url", "title", "content", "error"} and row["error"] is None
        assert "[TRUNCATED]" in row["content"]


def test_paging_file_collision_refuses_to_replace_earlier_content(native_extract, monkeypatch):
    from pathlib import Path
    import uuid
    from tools.web_tools_truncate import _truncate_with_footer

    native_extract("firecrawl")
    monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(int=1))
    first_body, later_body = "first paragraph\n" * 400, "later paragraph\n" * 400
    first, truncated = _truncate_with_footer(first_body, B, 2000)
    assert truncated
    path = Path(first.split("Full text saved to: ", 1)[1].splitlines()[0])
    assert path.read_text() == first_body

    later, truncated = _truncate_with_footer(later_body, B, 2000)
    assert path.read_text() == first_body
    assert truncated and "later paragraph" in later
    assert "Full text could not be stored" in later
    assert "Full text saved to:" not in later
