"""Native provider, URL/cache identity and pre-dispatch policy regressions."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def native_parallel(monkeypatch, tmp_path, web_registry_populated):
    import agent.web_search_provider as env
    import plugins.web.parallel.provider as parallel
    import tools.web_result_cache as cache
    import tools.web_tools as web
    import tools.website_policy as policy

    directory = tmp_path / "web-cache"
    directory.mkdir()
    monkeypatch.setattr(cache, "_cache_dir", lambda: directory)
    monkeypatch.setattr(cache, "_web_config", lambda: {})
    monkeypatch.setattr(web, "_get_extract_backend", lambda: "parallel")
    monkeypatch.setattr(web, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web, "async_is_safe_url", AsyncMock(return_value=True))
    monkeypatch.setattr(env, "get_provider_env", lambda name: "synthetic-credential")
    monkeypatch.setattr(policy, "check_website_access", lambda url: None)
    calls = []
    first = "https://first.example.test/page"
    second = "https://second.example.test/page"

    async def extract(*, urls, full_content):
        calls.append(tuple(urls))
        documents = [SimpleNamespace(url=second, full_content="second body", excerpts=[], title="Second")] if second in urls else []
        errors = [SimpleNamespace(url=first, content="first unavailable", error_type="unavailable")] if first in urls else []
        return SimpleNamespace(results=documents, errors=errors)

    monkeypatch.setattr(parallel, "_get_async_client", lambda: SimpleNamespace(beta=SimpleNamespace(extract=extract)))
    return web, cache, policy, calls, first, second


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True])
async def test_parallel_success_order_cannot_poison_another_url_cache(native_parallel, reverse):
    web, cache, policy, calls, first, second = native_parallel
    urls = [second, first] if reverse else [first, second]
    initial = json.loads(await web.web_extract_tool(urls))["results"]
    later = json.loads(await web.web_extract_tool([first]))["results"]
    assert later[0].get("error") == "first unavailable"
    assert not later[0].get("content")
    assert cache.extract_cache_get(first, provider="parallel") is None
    assert calls == [tuple(urls), (first,)]
    assert [item["url"] for item in initial] == urls
    assert next(item for item in initial if item["url"] == second)["content"] == "second body"


@pytest.mark.asyncio
async def test_parallel_policy_block_happens_before_sdk_and_cache(native_parallel, monkeypatch):
    web, cache, policy, calls, first, second = native_parallel
    block = {"host": "second.example.test", "rule": "block", "source": "test", "message": "Blocked by website policy"}
    monkeypatch.setattr(policy, "check_website_access", lambda url: block if url == second else None)
    results = json.loads(await web.web_extract_tool([first, second]))["results"]
    assert calls == [(first,)]
    assert results[1].get("blocked_by_policy")
    assert not results[1].get("content")
    assert cache.extract_cache_get(second, provider="parallel") is None


@pytest.fixture
def native_firecrawl(monkeypatch, tmp_path, web_registry_populated):
    import plugins.web.firecrawl.provider as firecrawl
    import tools.web_result_cache as cache
    import tools.web_tools as web
    import tools.website_policy as policy

    directory = tmp_path / "firecrawl-cache"
    directory.mkdir()
    monkeypatch.setattr(cache, "_cache_dir", lambda: directory)
    monkeypatch.setattr(cache, "_web_config", lambda: {})
    monkeypatch.setattr(web, "_get_extract_backend", lambda: "firecrawl")
    monkeypatch.setattr(web, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web, "async_is_safe_url", AsyncMock(return_value=True))
    monkeypatch.setattr(firecrawl, "_use_keyless_ring", lambda: False)
    monkeypatch.setattr(firecrawl, "is_safe_url", lambda url: True)
    monkeypatch.setattr(policy, "check_website_access", lambda url: None)
    monkeypatch.setattr(firecrawl, "check_website_access", lambda url: policy.check_website_access(url))
    first = "https://first.example.test/page"
    second = "https://second.example.test/page"
    third = "https://third.example.test/page"
    payloads = {}
    calls = []

    def scrape(*, url, formats):
        calls.append((url, formats))
        return payloads[url]

    monkeypatch.setattr(firecrawl, "_get_firecrawl_client", lambda: SimpleNamespace(scrape=scrape))
    return web, cache, policy, firecrawl, payloads, calls, first, second, third


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("redirect", ["collision", "unrelated"])
async def test_firecrawl_redirect_keeps_request_content_and_cache_owner(native_firecrawl, reverse, redirect):
    web, cache, policy, firecrawl, payloads, calls, first, second, third = native_firecrawl
    final = second if redirect == "collision" else third
    payloads.update({
        first: {"markdown": "first request body", "metadata": {"title": "First", "sourceURL": final}},
        second: {"markdown": "second request body", "metadata": {"title": "Second", "sourceURL": second}},
    })
    before = json.dumps(payloads, sort_keys=True)
    urls = [second, first] if reverse else [first, second]
    initial = json.loads(await web.web_extract_tool(urls))["results"]
    expected = {first: "first request body", second: "second request body"}
    assert [item.get("content") for item in initial] == [expected[url] for url in urls]
    assert not any(item.get("error") for item in initial)
    first_result = initial[urls.index(first)]
    assert first_result["url"] == final
    assert json.dumps(payloads, sort_keys=True) == before
    for url in urls:
        hit = cache.extract_cache_get(url, provider="firecrawl")
        assert hit["content"] == expected[url]
        assert hit["final_url"] == (final if url == first else second)
    if redirect == "unrelated":
        assert cache.extract_cache_get(third, provider="firecrawl") is None
    later = json.loads(await web.web_extract_tool(list(reversed(urls))))["results"]
    assert [item["content"] for item in later] == [expected[url] for url in reversed(urls)]
    assert calls == [(url, ["markdown", "html"]) for url in urls]


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["policy", "unsafe"])
async def test_firecrawl_redirect_refusal_stays_with_its_request(native_firecrawl, monkeypatch, refusal):
    web, cache, policy, firecrawl, payloads, calls, first, second, third = native_firecrawl
    payloads.update({
        first: {"markdown": "blocked first body", "metadata": {"sourceURL": third}},
        second: {"markdown": "second request body", "metadata": {"sourceURL": second}},
    })
    if refusal == "unsafe":
        monkeypatch.setattr(firecrawl, "is_safe_url", lambda url: url != third)
    else:
        block = {"host": "third.example.test", "rule": "block", "source": "test", "message": "Blocked by website policy"}
        monkeypatch.setattr(policy, "check_website_access", lambda url: block if url == third else None)
    results = json.loads(await web.web_extract_tool([first, second]))["results"]
    assert results[0]["url"] == first
    assert results[0].get("error")
    assert not results[0].get("content")
    assert "Blocked" in results[0]["error"]
    assert results[1]["content"] == "second request body"
    assert cache.extract_cache_get(first, provider="firecrawl") is None
    assert cache.extract_cache_get(second, provider="firecrawl")["content"] == "second request body"


@pytest.mark.asyncio
async def test_firecrawl_ipv6_port_and_address_remain_distinct_cache_owners(native_firecrawl):
    web, cache, policy, firecrawl, payloads, calls, *_ = native_firecrawl
    with_port = "https://[2606:4700::1111]:8443/page"
    without_port = "https://[2606:4700::1111:8443]/page"
    payloads.update({
        with_port: {"markdown": "port endpoint", "metadata": {"sourceURL": with_port}},
        without_port: {"markdown": "address endpoint", "metadata": {"sourceURL": without_port}},
    })
    results = json.loads(await web.web_extract_tool([with_port, without_port]))["results"]
    assert [item["content"] for item in results] == ["port endpoint", "address endpoint"]
    for url, expected in [(with_port, "port endpoint"), (without_port, "address endpoint")]:
        assert cache.extract_cache_get(url, provider="firecrawl")["content"] == expected
    for loose in (False, True):
        assert web._url_key(with_port, loose=loose) != web._url_key(without_port, loose=loose)
        assert web._url_key("https://[2606:4700::1111]:443/page", loose=loose) == web._url_key(
            "https://[2606:4700::1111]/page", loose=loose
        )
    again = json.loads(await web.web_extract_tool([without_port, with_port]))["results"]
    assert [item["content"] for item in again] == ["address endpoint", "port endpoint"]
    assert len(calls) == 2
