"""Tests for AI Gateway <-> AGCore contract-synced token budgets."""
from app.core import runtime_config as rc


def test_contract_sync_preserves_custom_when_unchanged_and_resets_when_changed(tmp_path, monkeypatch):
    cfg_path = tmp_path / "runtime_config.json"
    monkeypatch.setattr(rc, "CONFIG_PATH_ENV", str(cfg_path))
    monkeypatch.setattr(rc, "_ACTIVE_CONFIG", rc.AgentRuntimeConfig())

    # 1. Initial sync from Gateway contract
    entry = rc.sync_route_contract("hd2ai-admin", {
        "context_tokens": 131072,
        "max_output_tokens": 32768,
        "reasoning_effort": "on",
        "revision": 5,
        "version": 2,
    })
    assert entry.is_customized is False
    assert entry.active.output_ceiling_tokens == 32768
    assert entry.active.reasoning_start_tokens == 16384
    assert entry.active.input_budget_tokens == 131072 - 32768 - 2048

    # 2. Customize AGCore budget while Gateway contract stays identical
    custom = rc.customize_route_budget("hd2ai-admin", {"write_max_tokens": 6000, "compose_max_tokens": 12000})
    assert custom.is_customized is True
    assert custom.active.write_max_tokens == 6000
    assert custom.active.compose_max_tokens == 12000

    # 3. Re-sync with SAME Gateway contract -> custom overrides must be preserved
    same = rc.sync_route_contract("hd2ai-admin", {
        "context_tokens": 131072,
        "max_output_tokens": 32768,
        "reasoning_effort": "on",
        "revision": 6,
        "version": 2,
    })
    assert same.is_customized is True
    assert same.active.write_max_tokens == 6000
    assert same.active.compose_max_tokens == 12000

    # 4. Re-sync with CHANGED Gateway contract -> reset to new recommended defaults
    changed = rc.sync_route_contract("hd2ai-admin", {
        "context_tokens": 32768,
        "max_output_tokens": 16384,
        "reasoning_effort": "on",
        "revision": 7,
        "version": 3,
    })
    assert changed.is_customized is False
    assert changed.active == changed.recommended
    assert changed.active.output_ceiling_tokens == 16384
    assert changed.active.reasoning_start_tokens == 8192
