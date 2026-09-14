"""Tests for toolsets.py — toolset resolution, validation, and composition."""

import pytest

import toolsets as toolsets_mod
from tools.registry import ToolRegistry
from toolsets import (
    TOOLSETS,
    _HERMES_CORE_TOOLS,
    get_toolset,
    resolve_toolset,
    resolve_multiple_toolsets,
    get_all_toolsets,
    validate_toolset,
    create_custom_toolset,
    get_toolset_info,
)


def _dummy_handler(args, **kwargs):
    return "{}"


def _make_schema(name: str, description: str = "test tool"):
    return {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": {}},
    }


class TestGetToolset:
    def test_known_toolset(self):
        ts = get_toolset("web")
        assert ts is not None
        assert "web_search" in ts["tools"]

    def test_x_search_toolset_marks_read_only_and_points_to_xurl(self):
        ts = get_toolset("x_search")
        assert ts is not None
        assert ts["tools"] == ["x_search"]
        description = ts["description"].lower()
        assert "read-only" in description
        assert "xurl" in description
        assert "authenticated" in description

    def test_merges_registry_tools_into_builtin_toolset(self, monkeypatch):
        reg = ToolRegistry()
        reg.register(
            name="web_search_plus",
            toolset="web",
            schema=_make_schema("web_search_plus", "Plugin web search"),
            handler=_dummy_handler,
        )

        monkeypatch.setattr("tools.registry.registry", reg)

        ts = get_toolset("web")
        assert ts is not None
        assert set(ts["tools"]) == {"web_search", "web_extract", "web_search_plus"}



class TestResolveToolset:
    def test_leaf_toolset(self):
        tools = resolve_toolset("web")
        assert set(tools) == {"web_search", "web_extract"}

    def test_composite_toolset(self):
        tools = resolve_toolset("debugging")
        assert "terminal" in tools
        assert "web_search" in tools
        assert "web_extract" in tools

    def test_cycle_detection(self):
        # Create a cycle: A includes B, B includes A
        TOOLSETS["_cycle_a"] = {"description": "test", "tools": ["t1"], "includes": ["_cycle_b"]}
        TOOLSETS["_cycle_b"] = {"description": "test", "tools": ["t2"], "includes": ["_cycle_a"]}
        try:
            tools = resolve_toolset("_cycle_a")
            # Should not infinite loop — cycle is detected
            assert "t1" in tools
            assert "t2" in tools
        finally:
            del TOOLSETS["_cycle_a"]
            del TOOLSETS["_cycle_b"]


    def test_plugin_toolset_uses_registry_snapshot(self, monkeypatch):
        reg = ToolRegistry()
        reg.register(
            name="plugin_b",
            toolset="plugin_example",
            schema=_make_schema("plugin_b", "B"),
            handler=_dummy_handler,
        )
        reg.register(
            name="plugin_a",
            toolset="plugin_example",
            schema=_make_schema("plugin_a", "A"),
            handler=_dummy_handler,
        )

        monkeypatch.setattr("tools.registry.registry", reg)

        assert resolve_toolset("plugin_example") == ["plugin_a", "plugin_b"]




class TestResolveMultipleToolsets:
    def test_combines_and_deduplicates(self):
        tools = resolve_multiple_toolsets(["web", "terminal"])
        assert "web_search" in tools
        assert "web_extract" in tools
        assert "terminal" in tools
        # No duplicates
        assert len(tools) == len(set(tools))



class TestValidateToolset:
    def test_valid(self):
        assert validate_toolset("web") is True
        assert validate_toolset("terminal") is True


    def test_invalid(self):
        assert validate_toolset("nonexistent") is False

    def test_mcp_alias_uses_live_registry(self, monkeypatch):
        reg = ToolRegistry()
        reg.register(
            name="mcp__dynserver__ping",
            toolset="mcp-dynserver",
            schema=_make_schema("mcp__dynserver__ping", "Ping"),
            handler=_dummy_handler,
        )
        reg.register_toolset_alias("dynserver", "mcp-dynserver")

        monkeypatch.setattr("tools.registry.registry", reg)

        assert validate_toolset("dynserver") is True
        assert validate_toolset("mcp-dynserver") is True
        assert "mcp__dynserver__ping" in resolve_toolset("dynserver")


class TestGetToolsetInfo:
    def test_leaf(self):
        info = get_toolset_info("web")
        assert info["name"] == "web"
        assert info["is_composite"] is False
        assert info["tool_count"] == 2

    def test_composite(self):
        info = get_toolset_info("debugging")
        assert info["is_composite"] is True
        assert info["tool_count"] > len(info["direct_tools"])



class TestCreateCustomToolset:
    def test_runtime_creation(self):
        create_custom_toolset(
            name="_test_custom",
            description="Test toolset",
            tools=["web_search"],
            includes=["terminal"],
        )
        try:
            tools = resolve_toolset("_test_custom")
            assert "web_search" in tools
            assert "terminal" in tools
            assert validate_toolset("_test_custom") is True
        finally:
            del TOOLSETS["_test_custom"]


class TestRegistryOwnedToolsets:
    def test_registry_membership_is_live(self, monkeypatch):
        reg = ToolRegistry()
        reg.register(
            name="test_live_toolset_tool",
            toolset="test-live-toolset",
            schema=_make_schema("test_live_toolset_tool", "Live"),
            handler=_dummy_handler,
        )

        monkeypatch.setattr("tools.registry.registry", reg)

        assert validate_toolset("test-live-toolset") is True
        assert get_toolset("test-live-toolset")["tools"] == ["test_live_toolset_tool"]
        assert resolve_toolset("test-live-toolset") == ["test_live_toolset_tool"]


class TestToolsetConsistency:
    """Verify structural integrity of the built-in TOOLSETS dict."""

    def test_all_toolsets_have_required_keys(self):
        for name, ts in TOOLSETS.items():
            assert "description" in ts, f"{name} missing description"
            assert "tools" in ts, f"{name} missing tools"
            assert "includes" in ts, f"{name} missing includes"


    def test_messaging_is_an_explicit_non_core_toolset(self):
        """Outbound sends must never ride along with every platform's core tools."""
        assert TOOLSETS["messaging"]["tools"] == ["send_message"]
        assert "send_message" not in _HERMES_CORE_TOOLS

    def test_hermes_platforms_share_core_tools(self):
        """All hermes-* platform toolsets share the same core tools.

        Platform-specific additions (e.g. ``discord`` / ``discord_admin``
        on hermes-discord, gated on DISCORD_BOT_TOKEN) are allowed on top —
        the invariant is that the core set is identical across platforms.
        """
        platforms = ["hermes-cli", "hermes-telegram", "hermes-discord", "hermes-whatsapp", "hermes-slack", "hermes-signal", "hermes-homeassistant"]
        tool_sets = [set(TOOLSETS[p]["tools"]) for p in platforms]
        # All platforms must contain the shared core; platform-specific
        # extras are OK (subset check, not equality).
        core = set.intersection(*tool_sets)
        for name, ts in zip(platforms, tool_sets):
            assert core.issubset(ts), f"{name} is missing core tools: {core - ts}"
        # Sanity: the shared core must be non-trivial (i.e. we didn't
        # silently let a platform diverge so far that nothing is shared).
        assert len(core) > 20, f"Suspiciously small shared core: {len(core)} tools"


class TestPluginToolsets:
    def test_get_all_toolsets_includes_plugin_toolset(self, monkeypatch):
        reg = ToolRegistry()
        reg.register(
            name="plugin_tool",
            toolset="plugin_bundle",
            schema=_make_schema("plugin_tool", "Plugin tool"),
            handler=_dummy_handler,
        )

        monkeypatch.setattr("tools.registry.registry", reg)

        all_toolsets = get_all_toolsets()
        assert "plugin_bundle" in all_toolsets
        assert all_toolsets["plugin_bundle"]["tools"] == ["plugin_tool"]


class TestDefaultPlatformWebSearchCoverage:
    def test_hermes_whatsapp_toolset_includes_web_search(self):
        assert "web_search" in resolve_toolset("hermes-whatsapp")



class TestResolveToolsetIncludeRegistry:
    """include_registry flag exposes the static (pre-registry-merge) view used
    by platform reverse-mapping. Regression harness for issue #49622."""

    def test_include_registry_false_excludes_registry_tools(self):
        from tools.registry import discover_builtin_tools, registry
        discover_builtin_tools()

        # Register a tool into `terminal` at runtime, the way plugins and MCP
        # servers do, so the split is exercised on the mechanism rather than on
        # whichever built-in currently happens to live where.
        registry.register(
            name="__probe_registry_only_tool__",
            toolset="terminal",
            schema={"name": "__probe_registry_only_tool__", "parameters": {"type": "object", "properties": {}}},
            handler=lambda args, **kw: "",
        )
        try:
            merged = set(resolve_toolset("terminal"))
            static = set(resolve_toolset("terminal", include_registry=False))
        finally:
            registry.deregister("__probe_registry_only_tool__")

        assert static == {"terminal", "process"}, static
        # Registered into 'terminal' but not part of the static definition — it
        # must only appear in the merged view.
        assert "__probe_registry_only_tool__" in merged
        assert "__probe_registry_only_tool__" not in static


    def test_static_view_threads_through_includes(self):
        # 'debugging' has direct tools [terminal, process] and includes [web, file]
        static = set(resolve_toolset("debugging", include_registry=False))
        assert {"terminal", "process"} <= static
        assert "web_search" in static
        assert "read_file" in static


    def test_registry_only_toolset_static_view_is_empty(self):
        assert resolve_toolset("__definitely_not_a_real_toolset__", include_registry=False) == []


class TestResolveToolsetMemo:
    """Measured-work pins for the generation-keyed resolution memo."""

    def test_second_resolution_is_cached(self, monkeypatch):
        """Repeated resolves of the same toolset must not re-walk the registry.

        resolve_toolset is called dozens of times per _get_platform_tools()
        (every /tools completion keystroke). The memo keyed on the registry
        generation makes repeat calls a dict lookup instead of a full
        includes-walk + registry snapshot.
        """
        from tools.registry import registry

        toolsets_mod._resolve_toolset_memo.clear()
        get_toolset_calls = {"n": 0}

        orig_get_toolset = toolsets_mod.get_toolset

        def counting_get_toolset(name, *, include_registry=True):
            get_toolset_calls["n"] += 1
            return orig_get_toolset(name, include_registry=include_registry)

        monkeypatch.setattr(toolsets_mod, "get_toolset", counting_get_toolset)

        registry_id = id(registry)
        generation = registry._generation

        first = resolve_toolset("hermes-cli")
        second = resolve_toolset("hermes-cli")

        assert first == second
        assert get_toolset_calls["n"] == 1, (
            "second resolution must be a memo hit (no get_toolset re-walk), "
            f"got {get_toolset_calls['n']} calls"
        )
        assert (
            "hermes-cli", True, registry_id, generation
        ) in toolsets_mod._resolve_toolset_memo

    def test_generation_bump_invalidates_memo(self, monkeypatch):
        """A registry mutation (generation bump) must force a fresh resolve."""
        from tools.registry import registry

        toolsets_mod._resolve_toolset_memo.clear()
        get_toolset_calls = {"n": 0}

        orig_get_toolset = toolsets_mod.get_toolset

        def counting_get_toolset(name, *, include_registry=True):
            get_toolset_calls["n"] += 1
            return orig_get_toolset(name, include_registry=include_registry)

        monkeypatch.setattr(toolsets_mod, "get_toolset", counting_get_toolset)

        resolve_toolset("hermes-cli")
        assert get_toolset_calls["n"] == 1

        # Simulate a registry mutation bumping the generation.
        registry._generation += 1
        resolve_toolset("hermes-cli")
        assert get_toolset_calls["n"] == 2, (
            "generation bump must invalidate the memo and re-resolve"
        )

    def test_memo_result_matches_fresh_resolution(self):
        """The memo must never change the resolved result."""
        toolsets_mod._resolve_toolset_memo.clear()
        first = resolve_toolset("hermes-cli", include_registry=False)
        second = resolve_toolset("hermes-cli", include_registry=False)
        assert first == second
        assert first  # non-empty sanity


@pytest.fixture
def isolated_tool_definitions(monkeypatch, tmp_path):
    """Use real selection/assembly with a private registry and cache state."""
    import model_tools
    import tools.registry as registry_mod

    messaging_entry = registry_mod.registry.get_entry("send_message")
    reg = ToolRegistry()
    monkeypatch.setattr(registry_mod, "registry", reg)
    monkeypatch.setattr(model_tools, "registry", reg)
    monkeypatch.setattr(model_tools, "_tool_defs_cache", {})
    monkeypatch.setattr(model_tools, "_last_resolved_tool_names", [])
    monkeypatch.setattr(toolsets_mod, "_resolve_toolset_memo", {})
    monkeypatch.setattr(registry_mod, "_check_fn_cache", {})
    monkeypatch.setattr(registry_mod, "_check_fn_last_good", {})
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Exercise native tool-search assembly with a local context length so
    # schema discovery never needs model-metadata network access.
    (tmp_path / "config.yaml").write_text(
        "model:\n  context_length: 200000\n"
        "tools:\n  tool_search:\n    enabled: auto\n"
    )
    return model_tools, reg, messaging_entry


@pytest.fixture(params=["_test_optin_leaf", "_test_optin_composite"])
def default_off_catalog(request, monkeypatch, isolated_tool_definitions):
    model_tools, reg, _ = isolated_tool_definitions
    # Registry membership supplies the leaf's tool. The composite can also
    # own default_off, proving exclusion resolves includes rather than just
    # subtracting the static direct-tools list.
    for name, includes in (
        ("_test_optin_leaf", []),
        ("_test_optin_composite", ["_test_optin_leaf"]),
    ):
        monkeypatch.setitem(TOOLSETS, name, {
            "description": "Test opt-in toolset",
            "tools": [],
            "includes": includes,
            "default_off": name == request.param,
        })
    reg.register(
        name="test_regular_tool",
        toolset="_test_regular",
        schema=_make_schema("test_regular_tool"),
        handler=_dummy_handler,
    )
    return model_tools, reg


def _test_catalog_names(definitions, skip_tool_search_assembly):
    names = {td["function"]["name"] for td in definitions}
    if not skip_tool_search_assembly and definitions:
        # Native assembly defers synthetic non-core tools. Inspect its real
        # model-facing catalog listing instead of bypassing the bridge.
        assert names == {"tool_search", "tool_describe", "tool_call"}
        listing = next(td["function"]["description"] for td in definitions
                       if td["function"]["name"] == "tool_search")
        return {name for name in ("test_regular_tool", "test_optin_tool")
                if name in listing}
    return names


@pytest.mark.parametrize("skip_tool_search_assembly", [False, True])
@pytest.mark.parametrize(
    "enabled, disabled, available, expected",
    [
        (None, None, True, {"test_regular_tool"}),
        (["_test_optin_leaf"], None, True, {"test_optin_tool"}),
        (["_test_optin_composite"], None, True, {"test_optin_tool"}),
        (["_test_optin_leaf"], ["_test_optin_leaf"], True, set()),
        (["_test_optin_composite"], ["_test_optin_leaf"], True, set()),
        (["_test_optin_leaf"], ["_test_optin_composite"], True, set()),
        (["_test_optin_leaf"], None, False, set()),
        (["_test_optin_composite"], None, False, set()),
        (None, ["_test_optin_leaf"], True, {"test_regular_tool"}),
    ],
    ids=["implicit", "leaf", "composite", "disable-leaf",
         "disable-through-composite", "disable-composite", "unavailable-leaf",
         "unavailable-composite", "implicit-disabled"],
)
def test_default_off_tool_definitions(
    default_off_catalog, skip_tool_search_assembly,
    enabled, disabled, available, expected,
):
    model_tools, reg = default_off_catalog
    reg.register(
        name="test_optin_tool",
        toolset="_test_optin_leaf",
        schema=_make_schema("test_optin_tool"),
        handler=_dummy_handler,
        check_fn=lambda: available,
    )

    definitions = model_tools.get_tool_definitions(
        enabled_toolsets=enabled,
        disabled_toolsets=disabled,
        skip_tool_search_assembly=skip_tool_search_assembly,
    )

    assert _test_catalog_names(definitions, skip_tool_search_assembly) == expected


@pytest.mark.parametrize("skip_tool_search_assembly", [False, True])
@pytest.mark.parametrize("explicit", ["_test_optin_leaf", "_test_optin_composite"])
def test_default_off_quiet_cache_keeps_selections_separate(
    default_off_catalog, skip_tool_search_assembly, explicit,
):
    model_tools, reg = default_off_catalog
    reg.register(
        name="test_optin_tool",
        toolset="_test_optin_leaf",
        schema=_make_schema("test_optin_tool"),
        handler=_dummy_handler,
        check_fn=lambda: True,
    )
    results = []
    for enabled in (None, [explicit], None):
        definitions = model_tools.get_tool_definitions(
            enabled_toolsets=enabled,
            quiet_mode=True,
            skip_tool_search_assembly=skip_tool_search_assembly,
        )
        results.append(_test_catalog_names(definitions, skip_tool_search_assembly))

    assert model_tools._tool_defs_cache, "quiet calls must exercise memoization"
    assert results == [
        {"test_regular_tool"}, {"test_optin_tool"}, {"test_regular_tool"},
    ]


@pytest.mark.parametrize("skip_tool_search_assembly", [False, True])
@pytest.mark.parametrize(
    "enabled, disabled, available, expected",
    [
        (None, None, True, set()),
        ([], None, True, set()),
        (["all"], None, True, {"send_message"}),
        (["*"], None, True, {"send_message"}),
        (["messaging"], None, True, {"send_message"}),
        (["messaging"], ["messaging"], True, set()),
        (["messaging"], None, False, set()),
    ],
    ids=["implicit", "empty", "all", "wildcard", "explicit", "disabled", "unavailable"],
)
def test_messaging_schema_requires_opt_in(
    isolated_tool_definitions, monkeypatch, skip_tool_search_assembly,
    enabled, disabled, available, expected,
):
    from tools.send_message_tool import SEND_MESSAGE_SCHEMA, _check_send_message

    model_tools, reg, entry = isolated_tool_definitions
    assert entry is not None
    assert entry.toolset == "messaging"
    assert entry.schema == SEND_MESSAGE_SCHEMA
    assert entry.check_fn is _check_send_message
    # Preserve the contributor's schema, handler and availability guard. Only
    # its requirements are fixture-controlled; no handler or transport runs.
    reg.register(
        name=entry.name, toolset=entry.toolset, schema=entry.schema,
        handler=entry.handler, check_fn=entry.check_fn,
    )
    monkeypatch.setattr(
        "gateway.session_context.get_session_env",
        lambda name, default="": "telegram" if available else "local",
    )
    monkeypatch.setattr("gateway.status.is_gateway_running", lambda: False)

    definitions = model_tools.get_tool_definitions(
        enabled_toolsets=enabled,
        disabled_toolsets=disabled,
        quiet_mode=True,
        skip_tool_search_assembly=skip_tool_search_assembly,
    )

    assert {td["function"]["name"] for td in definitions} == expected
    if expected:
        assert definitions[0]["function"]["parameters"]["properties"] == (
            SEND_MESSAGE_SCHEMA["parameters"]["properties"]
        )


@pytest.fixture
def messaging_catalog(isolated_tool_definitions, monkeypatch):
    from agent.delegation_context import KANBAN_ENV_KEYS, DELEGATED_CHILD_ENV_MARKER
    from tools.send_message_tool import SEND_MESSAGE_SCHEMA, _check_send_message

    model_tools, reg, entry = isolated_tool_definitions
    assert entry.schema == SEND_MESSAGE_SCHEMA
    assert entry.check_fn is _check_send_message
    reg.register(
        name=entry.name, toolset=entry.toolset, schema=entry.schema,
        handler=entry.handler, check_fn=entry.check_fn,
    )
    for name in (*KANBAN_ENV_KEYS, DELEGATED_CHILD_ENV_MARKER):
        monkeypatch.delenv(name, raising=False)
    available = {"value": True}
    monkeypatch.setattr(
        "gateway.session_context.get_session_env",
        lambda name, default="": "telegram" if available["value"] else "local",
    )
    monkeypatch.setattr("gateway.status.is_gateway_running", lambda: False)
    return model_tools, reg, available


def _resolve_explicit_all(adapter, value, monkeypatch):
    if adapter == "oneshot":
        from hermes_cli.oneshot import _normalize_toolsets, _validate_explicit_toolsets

        resolved, error = _validate_explicit_toolsets(value)
        assert error is None
        # _run_agent normalizes the validator result once more.
        return _normalize_toolsets(resolved)

    from tui_gateway.server import _load_enabled_toolsets

    monkeypatch.setenv("HERMES_TUI_TOOLSETS", value)
    return _load_enabled_toolsets()


@pytest.mark.parametrize("adapter, value", [
    ("oneshot", "all"), ("oneshot", "*"), ("oneshot", "all,*"),
    ("oneshot", " , all , web, _unknown_all_test , "),
    ("oneshot", ["all", "*", "web,_unknown_all_test"]),
    ("oneshot", (" * ", "all", "web", "_unknown_all_test")),
    ("tui", "all"), ("tui", "*"), ("tui", "all,*"),
    ("tui", " , * , web, _unknown_all_test , "),
])
@pytest.mark.parametrize("skip_tool_search_assembly", [False, True])
@pytest.mark.parametrize("disabled, available", [
    (None, True), (["messaging"], True), (None, False),
    (["all"], True), (["*"], True),
])
def test_explicit_all_resolvers_preserve_messaging(
    messaging_catalog, monkeypatch, capsys, adapter, value,
    skip_tool_search_assembly, disabled, available,
):
    model_tools, _, requirements = messaging_catalog
    requirements["value"] = available
    resolved = _resolve_explicit_all(adapter, value, monkeypatch)
    if "_unknown_all_test" in str(value):
        assert "ignoring additional entries: web, _unknown_all_test" in capsys.readouterr().err

    definitions = model_tools.get_tool_definitions(
        enabled_toolsets=resolved, disabled_toolsets=disabled,
        quiet_mode=True, skip_tool_search_assembly=skip_tool_search_assembly,
    )

    names = {td["function"]["name"] for td in definitions}
    assert ("send_message" in names) is (available and disabled is None)
    if disabled in (["all"], ["*"]):
        assert definitions == []


@pytest.mark.parametrize("adapter", ["oneshot", "tui"])
@pytest.mark.parametrize("value", ["all", "*"])
@pytest.mark.parametrize("skip_tool_search_assembly", [False, True])
def test_explicit_all_resolvers_cache_and_late_registration(
    messaging_catalog, monkeypatch, adapter, value, skip_tool_search_assembly,
):
    model_tools, reg, _ = messaging_catalog
    resolved = _resolve_explicit_all(adapter, value, monkeypatch)
    plugin_names = set()
    for suffix in ("before_lookup", "after_cached_lookup"):
        name = "test_late_" + suffix
        toolset = "_late_" + suffix
        # Native registration after adapter resolution, then after a cache hit.
        reg.register(
            name=name, toolset=toolset, schema=_make_schema(name),
            handler=_dummy_handler,
        )
        plugin_names.add(name)
        assert {"messaging", toolset} <= set(toolsets_mod.get_toolset_names())
        assert {"messaging", toolset} <= set(get_all_toolsets())
        for enabled, explicit in ((None, False), (resolved, True),
                                  (None, False), (resolved, True)):
            definitions = model_tools.get_tool_definitions(
                enabled_toolsets=enabled, quiet_mode=True,
                skip_tool_search_assembly=skip_tool_search_assembly,
            )
            names = {td["function"]["name"] for td in definitions}
            expected = plugin_names | ({"send_message"} if explicit else set())
            if skip_tool_search_assembly:
                assert names == expected
            else:
                assert ("send_message" in names) is explicit
                listing = next(td["function"]["description"] for td in definitions
                               if td["function"]["name"] == "tool_search")
                assert {name for name in plugin_names if name in listing} == plugin_names
        assert model_tools._tool_defs_cache, "quiet calls must exercise memoization"


@pytest.mark.parametrize("skip_tool_search_assembly", [False, True])
def test_default_resolvers_keep_messaging_implicit(
    messaging_catalog, monkeypatch, skip_tool_search_assembly,
):
    from hermes_cli.oneshot import _normalize_toolsets, _validate_explicit_toolsets
    from tui_gateway.server import _load_enabled_toolsets

    model_tools, _, _ = messaging_catalog
    monkeypatch.delenv("HERMES_TUI_TOOLSETS", raising=False)
    selections = [_load_enabled_toolsets()]
    for value in (None, [], " , "):
        resolved, error = _validate_explicit_toolsets(value)
        assert error is None
        assert _normalize_toolsets(value) is None  # native config-fallback signal
        selections.append(_normalize_toolsets(resolved))
    defaults = model_tools.get_tool_definitions(
        quiet_mode=True, skip_tool_search_assembly=skip_tool_search_assembly,
    )
    assert "send_message" not in {td["function"]["name"] for td in defaults}
    for enabled in selections:
        definitions = model_tools.get_tool_definitions(
            enabled_toolsets=enabled, quiet_mode=True,
            skip_tool_search_assembly=skip_tool_search_assembly,
        )
        assert "send_message" not in {td["function"]["name"] for td in definitions}

