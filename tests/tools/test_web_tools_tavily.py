"""Tests for Tavily web backend integration.

Coverage:
  _tavily_request() — keyed Bearer vs keyless header, attribution, error bodies.
  _normalize_tavily_search_results() — search response normalization.
  _normalize_tavily_documents() — extract response normalization, failed_results.
  web_search_tool / web_extract_tool — Tavily dispatch paths.
  auto-detect ranking — keyed paid-band; keyless only when Tavily is selected.
"""

import json
import os
import asyncio
import pytest
from unittest.mock import patch, MagicMock

from tests.tools.conftest import register_all_web_providers


def _ok_response(payload=None):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = payload if payload is not None else {"results": []}
    mock_response.text = json.dumps(mock_response.json.return_value)
    return mock_response


# ─── _tavily_request ─────────────────────────────────────────────────────────

class TestTavilyRequest:
    """Test suite for the _tavily_request helper."""

    def test_keyless_when_no_api_key(self):
        """No TAVILY_API_KEY → keyless header, no Authorization, no body key."""
        mock_response = _ok_response()

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TAVILY_API_KEY", None)
            with patch("plugins.web.tavily.provider.httpx.post", return_value=mock_response) as mock_post:
                from plugins.web.tavily.provider import _tavily_request
                _tavily_request("search", {"query": "test"})

                mock_post.assert_called_once()
                headers = mock_post.call_args.kwargs["headers"]
                payload = mock_post.call_args.kwargs["json"]
                assert headers["X-Client-Name"] == "hermes-agent"
                assert headers["X-Tavily-Access-Mode"] == "keyless"
                assert "Authorization" not in headers
                assert "api_key" not in payload
                assert payload["query"] == "test"
                assert "api.tavily.com/search" in mock_post.call_args.args[0]

    def test_keyed_uses_bearer_not_body(self):
        """TAVILY_API_KEY → Bearer auth, attribution, no body api_key."""
        mock_response = _ok_response()

        with patch.dict(os.environ, {"TAVILY_API_KEY": "tvly-test-key"}):
            with patch("plugins.web.tavily.provider.httpx.post", return_value=mock_response) as mock_post:
                from plugins.web.tavily.provider import _tavily_request
                _tavily_request("search", {"query": "hello"})

                mock_post.assert_called_once()
                headers = mock_post.call_args.kwargs["headers"]
                payload = mock_post.call_args.kwargs["json"]
                assert headers == {
                    "X-Client-Name": "hermes-agent",
                    "Authorization": "Bearer tvly-test-key",
                }
                assert "X-Tavily-Access-Mode" not in headers
                assert "api_key" not in payload
                assert payload["query"] == "hello"
                assert "api.tavily.com/search" in mock_post.call_args.args[0]

    def test_http_error_surfaces_response_body(self):
        """Non-2xx responses raise ValueError with Tavily's response body."""
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.text = "Rate limit hit. Sign up for a free API key at https://app.tavily.com"
        mock_response.json.return_value = {}

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TAVILY_API_KEY", None)
            with patch("plugins.web.tavily.provider.httpx.post", return_value=mock_response):
                from plugins.web.tavily.provider import _tavily_request
                with pytest.raises(ValueError, match="Rate limit hit"):
                    _tavily_request("search", {"query": "test"})


# ─── _normalize_tavily_search_results ─────────────────────────────────────────

class TestNormalizeTavilySearchResults:
    """Test search result normalization."""

    def test_basic_normalization(self):
        from plugins.web.tavily.provider import _normalize_tavily_search_results
        raw = {
            "results": [
                {"title": "Python Docs", "url": "https://docs.python.org", "content": "Official docs", "score": 0.9},
                {"title": "Tutorial", "url": "https://example.com", "content": "A tutorial", "score": 0.8},
            ]
        }
        result = _normalize_tavily_search_results(raw)
        assert result["success"] is True
        web = result["data"]["web"]
        assert len(web) == 2
        assert web[0]["title"] == "Python Docs"
        assert web[0]["url"] == "https://docs.python.org"
        assert web[0]["description"] == "Official docs"
        assert web[0]["position"] == 1
        assert web[1]["position"] == 2


    def test_missing_fields(self):
        from plugins.web.tavily.provider import _normalize_tavily_search_results
        result = _normalize_tavily_search_results({"results": [{}]})
        web = result["data"]["web"]
        assert web[0]["title"] == ""
        assert web[0]["url"] == ""
        assert web[0]["description"] == ""


# ─── _normalize_tavily_documents ──────────────────────────────────────────────

class TestNormalizeTavilyDocuments:
    """Test extract document normalization."""

    def test_basic_document(self):
        from plugins.web.tavily.provider import _normalize_tavily_documents
        raw = {
            "results": [{
                "url": "https://example.com",
                "title": "Example",
                "raw_content": "Full page content here",
            }]
        }
        docs = _normalize_tavily_documents(raw)
        assert len(docs) == 1
        assert docs[0]["url"] == "https://example.com"
        assert docs[0]["title"] == "Example"
        assert docs[0]["content"] == "Full page content here"
        assert docs[0]["raw_content"] == "Full page content here"
        assert docs[0]["metadata"]["sourceURL"] == "https://example.com"


    @pytest.mark.parametrize("urls, expected_url", [
        (["https://fallback.com"], "https://fallback.com"),
        (["https://fallback.com", "https://other.example"], ""),
    ], ids=["single-request", "multi-request"])
    def test_fallback_url(self, urls, expected_url):
        from plugins.web.tavily.provider import TavilyWebSearchProvider
        raw = {"results": [{"content": "data"}]}
        with patch.dict(os.environ, {"TAVILY_API_KEY": "tvly-test"}), \
             patch("plugins.web.tavily.provider.httpx.post", return_value=_ok_response(raw)):
            docs = TavilyWebSearchProvider().extract(urls)
        assert docs[0]["url"] == expected_url
        assert docs[0]["metadata"]["sourceURL"] == expected_url


# ─── availability / auto-detect ───────────────────────────────────────────────

class TestTavilyAvailability:
    """Keyed Tavily stays in the paid band; keyless only when selected."""

    def test_is_available_without_key(self):
        from plugins.web.tavily.provider import TavilyWebSearchProvider
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TAVILY_API_KEY", None)
            assert TavilyWebSearchProvider().is_available() is False

    def test_is_backend_available_without_key(self):
        from tools.web_tools import _is_backend_available
        with patch("tools.web_tools._load_web_config", return_value={}), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TAVILY_API_KEY", None)
            assert _is_backend_available("tavily") is False

    def test_is_backend_available_when_configured_without_key(self):
        from tools.web_tools import _is_backend_available
        with patch("tools.web_tools._load_web_config", return_value={"backend": "tavily"}), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TAVILY_API_KEY", None)
            assert _is_backend_available("tavily") is True

    def test_keyless_does_not_preempt_managed_firecrawl(self):
        """No TAVILY_API_KEY + Nous gateway ready → firecrawl, not keyless tavily."""
        from tools.web_tools import _get_backend
        with patch("tools.web_tools._load_web_config", return_value={}), \
             patch("tools.web_tools._is_tool_gateway_ready", return_value=True), \
             patch("tools.web_tools._ddgs_package_importable", return_value=False):
            os.environ.pop("TAVILY_API_KEY", None)
            assert _get_backend() == "firecrawl"

    def test_keyless_does_not_preempt_ddgs(self):
        from tools.web_tools import _get_backend
        with patch("tools.web_tools._load_web_config", return_value={}), \
             patch("tools.web_tools._is_tool_gateway_ready", return_value=False), \
             patch("tools.web_tools._ddgs_package_importable", return_value=True):
            os.environ.pop("TAVILY_API_KEY", None)
            assert _get_backend() == "ddgs"

    def test_no_keys_defaults_to_firecrawl(self):
        """Keyless tier disabled: zero-credential resolve hits the legacy
        firecrawl sentinel. (With the tier on — the default — it resolves
        to the Exa/Parallel keyless split; see test_web_keyless_fallback.py.)
        """
        from tools.web_tools import _get_backend
        with patch("tools.web_tools._load_web_config", return_value={}), \
             patch("tools.web_tools._is_tool_gateway_ready", return_value=False), \
             patch("tools.web_tools._ddgs_package_importable", return_value=False), \
             patch("tools.web_tools._list_registered_web_providers", return_value=[]), \
             patch("agent.web_search_registry._keyless_tier_enabled", return_value=False):
            os.environ.pop("TAVILY_API_KEY", None)
            assert _get_backend() == "firecrawl"

    def test_explicit_search_backend_tavily_without_key(self):
        """web.search_backend=tavily sticks even with no TAVILY_API_KEY."""
        from tools.web_tools import _get_search_backend
        with patch("tools.web_tools._load_web_config",
                   return_value={"backend": "firecrawl", "search_backend": "tavily"}), \
             patch("tools.web_tools._is_tool_gateway_ready", return_value=True):
            os.environ.pop("TAVILY_API_KEY", None)
            assert _get_search_backend() == "tavily"

    def test_check_web_api_key_when_tavily_configured_without_key(self):
        from tools.web_tools import check_web_api_key
        with patch("tools.web_tools._load_web_config", return_value={"backend": "tavily"}), \
             patch("tools.web_tools._is_tool_gateway_ready", return_value=False), \
             patch("tools.web_tools.check_firecrawl_api_key", return_value=False), \
             patch("tools.web_tools._ddgs_package_importable", return_value=False), \
             patch("agent.web_search_registry.get_active_search_provider", return_value=None), \
             patch("agent.web_search_registry.get_active_extract_provider", return_value=None):
            os.environ.pop("TAVILY_API_KEY", None)
            assert check_web_api_key() is True


# ─── web_search_tool (Tavily dispatch) ────────────────────────────────────────

class TestWebSearchTavily:
    """Test web_search_tool dispatch to Tavily."""

    _register_providers = staticmethod(register_all_web_providers)

    @pytest.fixture(autouse=True)
    def _populate_web_registry(self):
        self._register_providers()
        yield
        from agent.web_search_registry import _reset_for_tests
        _reset_for_tests()

    def test_search_dispatches_to_tavily(self):
        mock_response = _ok_response({
            "results": [{"title": "Result", "url": "https://r.com", "content": "desc", "score": 0.9}]
        })

        with patch("tools.web_tools._get_backend", return_value="tavily"), \
             patch.dict(os.environ, {"TAVILY_API_KEY": "tvly-test"}), \
             patch("plugins.web.tavily.provider.httpx.post", return_value=mock_response), \
             patch("tools.interrupt.is_interrupted", return_value=False):
            from tools.web_tools import web_search_tool
            result = json.loads(web_search_tool("test query", limit=3))
            assert result["success"] is True
            assert len(result["data"]["web"]) == 1
            assert result["data"]["web"][0]["title"] == "Result"

    def test_search_keyless_dispatch(self):
        """Opt-in keyless Tavily hits Tavily's own endpoint, not the ring."""
        mock_response = _ok_response({
            "results": [{"title": "Result", "url": "https://r.com", "content": "desc"}]
        })

        with patch("tools.web_tools._get_backend", return_value="tavily"), \
             patch("plugins.web.tavily.provider.httpx.post", return_value=mock_response) as mock_post, \
             patch("tools.interrupt.is_interrupted", return_value=False):
            os.environ.pop("TAVILY_API_KEY", None)
            from tools.web_tools import web_search_tool
            result = json.loads(web_search_tool("test query"))
            assert result["success"] is True
            headers = mock_post.call_args.kwargs["headers"]
            assert headers["X-Tavily-Access-Mode"] == "keyless"
            assert headers["X-Client-Name"] == "hermes-agent"
            assert "Authorization" not in headers
            assert "api.tavily.com/search" in mock_post.call_args.args[0]

    def test_tavily_is_not_in_keyless_ring(self):
        from plugins.web.keyless_mcp import _KEYLESS_RING, _KEYLESS_SEARCHERS, _KEYLESS_EXTRACTORS
        assert "tavily" not in _KEYLESS_RING
        assert "tavily" not in _KEYLESS_SEARCHERS
        assert "tavily" not in _KEYLESS_EXTRACTORS


# ─── web_extract_tool (Tavily dispatch) ───────────────────────────────────────

class TestWebExtractTavily:
    """Test web_extract_tool dispatch to Tavily."""

    _register_providers = staticmethod(register_all_web_providers)

    @pytest.fixture(autouse=True)
    def _populate_web_registry(self):
        self._register_providers()
        yield
        from agent.web_search_registry import _reset_for_tests
        _reset_for_tests()

    def test_extract_dispatches_to_tavily(self):
        mock_response = _ok_response({
            "results": [{"url": "https://example.com", "raw_content": "Extracted content", "title": "Page"}]
        })

        async def _allow_ssrf(_url: str) -> bool:
            return True

        with patch("tools.web_tools._get_backend", return_value="tavily"), \
             patch.dict(os.environ, {"TAVILY_API_KEY": "tvly-test"}), \
             patch("plugins.web.tavily.provider.httpx.post", return_value=mock_response), \
             patch("tools.web_tools.async_is_safe_url", _allow_ssrf):
            from tools.web_tools import web_extract_tool
            result = json.loads(asyncio.get_event_loop().run_until_complete(
                web_extract_tool(["https://example.com"])
            ))
            assert "results" in result
            assert len(result["results"]) == 1
            assert result["results"][0]["url"] == "https://example.com"
            assert "Extracted content" in result["results"][0]["content"]


class TestTavilyExtractContracts:
    """Exercise Tavily response provenance through the real dispatcher and disk cache."""

    @pytest.fixture(autouse=True)
    def _isolated_tavily(self, monkeypatch):
        from hermes_constants import get_hermes_home
        from tools import website_policy

        register_all_web_providers()
        config = {"web": {
            "backend": "tavily", "cache_enabled": True,
            "keyless_fallback": True, "keyless_rescue": False,
        }}
        (get_hermes_home() / "config.yaml").write_text(json.dumps(config))
        monkeypatch.setenv("TAVILY_API_KEY", "synthetic-test")
        monkeypatch.setattr("tools.web_tools._load_web_config", lambda: config["web"])
        monkeypatch.setattr("tools.url_safety.socket.getaddrinfo",
                            lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))])
        with website_policy._cache_lock:
            website_policy._cached_policy = None
        yield
        with website_policy._cache_lock:
            website_policy._cached_policy = None
        from agent.web_search_registry import _reset_for_tests
        _reset_for_tests()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure_field", ["failed_results", "failed_urls"])
    @pytest.mark.parametrize("unlabeled", [False, True], ids=["labeled-only", "unlabeled-success"])
    async def test_mixed_batch_does_not_cache_success_under_failed_url(self, failure_field, unlabeled):
        from tools.web_tools import web_extract_tool
        from tools.web_result_cache import extract_cache_get

        a, b = "https://example.com/failed", "https://example.com/success"
        failures = [{"url": a, "error": "unavailable"}] if failure_field == "failed_results" else [a]
        raw = {"results": [{"url": b, "raw_content": "ONLY_PAGE_B"}], failure_field: failures}
        if unlabeled:
            raw["results"].insert(0, {"raw_content": "UNATTRIBUTED"})
        with patch("plugins.web.tavily.provider.httpx.post", side_effect=[
            _ok_response(raw), _ok_response({failure_field: failures}),
        ]) as post:
            first = json.loads(await web_extract_tool([a, b]))["results"]
            later_a = json.loads(await web_extract_tool([a]))["results"][0]
            later_b = json.loads(await web_extract_tool([b]))["results"][0]
        assert [row["url"] for row in first] == [a, b]
        assert first[0].get("error") and not first[0]["content"]
        assert first[1]["content"] == "ONLY_PAGE_B"
        assert later_a.get("error") and not later_a["content"]
        assert extract_cache_get(a, provider="tavily") is None
        cached_b = extract_cache_get(b, provider="tavily")
        assert cached_b is not None and cached_b["content"] == "ONLY_PAGE_B"
        assert later_b["content"] == "ONLY_PAGE_B"
        assert [call.kwargs["json"]["urls"] for call in post.call_args_list] == [[a, b], [a]]

    @pytest.mark.asyncio
    async def test_sparse_shuffled_response_preserves_requested_slots(self):
        from tools.web_tools import web_extract_tool
        from tools.web_result_cache import extract_cache_get

        a, b, c = [f"https://example.com/{name}" for name in "abc"]
        raw = {"results": [
            {"url": b, "raw_content": "B"},
            {"url": "https://example.com/unrequested", "raw_content": "EXTRA"},
            {"url": a, "raw_content": "A"},
            {"raw_content": "UNATTRIBUTED"},
        ]}
        with patch("plugins.web.tavily.provider.httpx.post", side_effect=[
            _ok_response(raw), _ok_response({"failed_urls": [c]}),
        ]) as post:
            rows = json.loads(await web_extract_tool([a, c, b, a]))["results"]
            again = json.loads(await web_extract_tool([a, c, b, a]))["results"]
        for batch in (rows, again):
            assert [row["url"] for row in batch] == [a, c, b, a]
            assert batch[1].get("error") and not batch[1]["content"]
            assert [batch[i]["content"] for i in (0, 2, 3)] == ["A", "B", "A"]
        assert extract_cache_get(c, provider="tavily") is None
        assert [call.kwargs["json"]["urls"] for call in post.call_args_list] == [[a, c, b, a], [c]]

    @pytest.mark.asyncio
    async def test_unlabeled_result_cannot_fill_or_cache_missing_first_request(self):
        from tools.web_tools import web_extract_tool
        from tools.web_result_cache import extract_cache_get

        a, b = "https://example.com/a", "https://example.com/b"
        raw = {"results": [{"raw_content": "UNATTRIBUTED"}, {"url": b, "raw_content": "B"}]}
        with patch("plugins.web.tavily.provider.httpx.post", side_effect=[
            _ok_response(raw), _ok_response({"results": [{"raw_content": "A"}]}),
        ]) as post:
            rows = json.loads(await web_extract_tool([a, b]))["results"]
            assert [row["url"] for row in rows] == [a, b]
            assert rows[0].get("error") and not rows[0]["content"]
            assert rows[1]["content"] == "B"
            assert extract_cache_get(a, provider="tavily") is None
            cached_b = extract_cache_get(b, provider="tavily")
            assert cached_b is not None and cached_b["content"] == "B"
            later_a = json.loads(await web_extract_tool([a]))["results"][0]
        assert later_a["content"] == "A" and not later_a.get("error")
        assert [call.kwargs["json"]["urls"] for call in post.call_args_list] == [[a, b], [a]]


@pytest.mark.parametrize("reverse", [False, True])
def test_native_tavily_observable_collision_is_ambiguous_without_stamping_vendor_marker(monkeypatch, reverse):
    from plugins.web.tavily.provider import TavilyWebSearchProvider
    from tools.web_tools_extract import _pair_results
    urls = ["https://a.example", "https://b.example"]
    raw = {"results": [
        {"url": urls[1], "raw_content": "A redirected body", "_request_url": urls[0]},
        {"url": urls[1], "raw_content": "B body"},
    ]}
    if reverse:
        raw["results"].reverse()
    with patch("plugins.web.tavily.provider.httpx.post", return_value=_ok_response(raw)) as post:
        rows = TavilyWebSearchProvider().extract(urls)
    assert all("_request_url" not in r for r in rows)
    paired = _pair_results(urls, rows)
    assert paired[0].get("error") and "ambiguous" in paired[1]["error"].lower()
    assert all(not r["content"] for r in paired)
    post.assert_called_once()
