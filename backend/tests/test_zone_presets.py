"""Phase 16.7: tests for zone_presets module + endpoint.

Two surfaces:

1. Pure module (`backend/zone_presets.py`): the three presets are
   consistent, role normalization handles aliases, apply_preset is
   robust to bad input, list_presets returns the expected shape +
   ordering.

2. `GET /workspaces/me/zone-presets` endpoint contract.

The actual application of presets at deploy time is dashboard
work (Phase 16.7 part B) — covered there.
"""
from __future__ import annotations

import pytest

import zone_presets as zp
from tests.conftest import auth_headers


# ---------- Preset definitions ---------- #


def test_valid_presets_set_matches_metadata_keys():
    """Defense against the metadata dict drifting from the
    canonical preset set."""
    assert zp.VALID_PRESETS == set(zp.PRESET_METADATA.keys())
    assert zp.VALID_PRESETS == set(zp.ZONE_PRESETS.keys())


def test_default_preset_is_in_valid_set():
    assert zp.DEFAULT_PRESET in zp.VALID_PRESETS


def test_each_preset_has_all_three_roles():
    """Imported at module top — the module asserts this at import
    time too. Belt-and-suspenders here so a future change that
    removes the assert doesn't silently regress."""
    required_roles = {"orchestrator", "specialist", "messenger"}
    for name in zp.VALID_PRESETS:
        assert set(zp.ZONE_PRESETS[name].keys()) == required_roles, (
            f"preset {name} role keys: {sorted(zp.ZONE_PRESETS[name].keys())}"
        )


def test_each_role_config_has_the_three_required_fields():
    """The downstream PATCH endpoints expect this shape — guard
    against a preset dropping a field by accident."""
    required = {"sensitivity_level", "capabilities", "dispatches_cross_zone"}
    for name, by_role in zp.ZONE_PRESETS.items():
        for role, cfg in by_role.items():
            assert set(cfg.keys()) == required, (
                f"{name}/{role} fields: {sorted(cfg.keys())}"
            )


# ---------- Open team semantics ---------- #


def test_open_team_grants_everything_to_every_role():
    """Developer convenience preset — no friction. Every role gets
    internet + send_command + dispatches_cross_zone."""
    for role in ("orchestrator", "specialist", "messenger"):
        cfg = zp.ZONE_PRESETS[zp.OPEN_TEAM][role]
        assert cfg["sensitivity_level"] == "public"
        assert "internet" in cfg["capabilities"]
        assert "send_command" in cfg["capabilities"]
        assert cfg["dispatches_cross_zone"] is True


# ---------- Standard team semantics ---------- #


def test_standard_team_is_internal_zone_no_cross_zone():
    """SMB defaults — internal zone everywhere, cross-zone off
    (no boundaries crossed because everyone's in the same zone)."""
    for role in ("orchestrator", "specialist", "messenger"):
        cfg = zp.ZONE_PRESETS[zp.STANDARD_TEAM][role]
        assert cfg["sensitivity_level"] == "internal"
        assert cfg["dispatches_cross_zone"] is False


def test_standard_team_orchestrator_does_not_get_internet():
    """Orchestrator coordinates; its specialists are the ones that
    make outbound calls. Reserving internet for who actually needs
    it tightens the blast radius if the orchestrator gets prompt-
    injected."""
    cfg = zp.ZONE_PRESETS[zp.STANDARD_TEAM]["orchestrator"]
    assert "internet" not in cfg["capabilities"]
    assert "send_command" in cfg["capabilities"]


def test_standard_team_messenger_is_a_leaf():
    """Messengers send outbound; they shouldn't be able to dispatch
    further (would let a compromised messenger fan out into the
    constellation)."""
    cfg = zp.ZONE_PRESETS[zp.STANDARD_TEAM]["messenger"]
    assert "send_command" not in cfg["capabilities"]
    assert "internet" in cfg["capabilities"]


# ---------- Compliance team semantics: THE WEDGE ---------- #


def test_compliance_specialist_is_pii_with_no_dispatch_no_internet():
    """The canonical CRM-bot. A prompt-injected specialist literally
    cannot exfiltrate: no internet to call out, no send_command to
    dispatch to a public bot. Only paths off the pii zone are blocked.
    This is Phase 16's proof point against Viktor."""
    cfg = zp.ZONE_PRESETS[zp.COMPLIANCE_TEAM]["specialist"]
    assert cfg["sensitivity_level"] == "pii"
    assert cfg["capabilities"] == []
    assert cfg["dispatches_cross_zone"] is False


def test_compliance_messenger_is_public_with_internet_but_no_dispatch_back():
    """Messengers in the compliance preset are the outbound side
    (public + internet) but can't dispatch BACK into the
    constellation (no send_command), so a compromised internet-side
    bot can't pivot inward."""
    cfg = zp.ZONE_PRESETS[zp.COMPLIANCE_TEAM]["messenger"]
    assert cfg["sensitivity_level"] == "public"
    assert "internet" in cfg["capabilities"]
    assert "send_command" not in cfg["capabilities"]


def test_compliance_orchestrator_is_internal_no_cross_zone():
    """Orchestrator coordinates but doesn't itself hold pii or
    have internet. Cross-zone dispatch disabled across the whole
    preset — the ONLY way data crosses zones in compliance is via
    the human-mediated handoff_span from 16.5."""
    cfg = zp.ZONE_PRESETS[zp.COMPLIANCE_TEAM]["orchestrator"]
    assert cfg["sensitivity_level"] == "internal"
    assert cfg["dispatches_cross_zone"] is False


def test_compliance_team_disables_cross_zone_for_every_role():
    """Belt-and-suspenders on the load-bearing security claim."""
    for role in ("orchestrator", "specialist", "messenger"):
        cfg = zp.ZONE_PRESETS[zp.COMPLIANCE_TEAM][role]
        assert cfg["dispatches_cross_zone"] is False, role


# ---------- apply_preset + role normalization ---------- #


def test_apply_preset_returns_copy_not_reference():
    """Mutating the returned dict shouldn't poison the next call's
    preset config."""
    a = zp.apply_preset(zp.STANDARD_TEAM, "specialist")
    a["capabilities"].append("hacked")
    b = zp.apply_preset(zp.STANDARD_TEAM, "specialist")
    assert "hacked" not in b["capabilities"]


def test_apply_preset_normalizes_role_aliases():
    """team_planner uses {orchestrator, specialist, messenger}; the
    agents table also has {executor, notifier}. The preset has to
    handle both vocabularies cleanly."""
    # executor → treated as specialist
    spec = zp.apply_preset(zp.STANDARD_TEAM, "specialist")
    exec_cfg = zp.apply_preset(zp.STANDARD_TEAM, "executor")
    assert spec == exec_cfg

    # notifier → treated as messenger
    msg = zp.apply_preset(zp.STANDARD_TEAM, "messenger")
    notif_cfg = zp.apply_preset(zp.STANDARD_TEAM, "notifier")
    assert msg == notif_cfg


def test_apply_preset_falls_back_to_specialist_for_unknown_role():
    """Defense against a future role getting through without an
    alias entry — fall back to the middle ground rather than crash."""
    unknown = zp.apply_preset(zp.STANDARD_TEAM, "bogus_role")
    spec = zp.apply_preset(zp.STANDARD_TEAM, "specialist")
    assert unknown == spec


def test_apply_preset_handles_none_role():
    """None / non-string falls through to specialist via
    _normalize_role."""
    out = zp.apply_preset(zp.STANDARD_TEAM, None)
    assert out["sensitivity_level"] == "internal"


def test_apply_preset_falls_back_to_default_for_unknown_preset():
    """Typo'd preset name → default rather than KeyError so a
    dashboard bug doesn't break deploys."""
    out = zp.apply_preset("not_a_real_preset", "specialist")
    expected = zp.apply_preset(zp.DEFAULT_PRESET, "specialist")
    assert out == expected


# ---------- list_presets ---------- #


def test_list_presets_orders_least_to_most_restrictive():
    """Picker reads as a slider: open → standard → compliance."""
    names = [p["name"] for p in zp.list_presets()]
    assert names == [zp.OPEN_TEAM, zp.STANDARD_TEAM, zp.COMPLIANCE_TEAM]


def test_list_presets_marks_the_default():
    presets = zp.list_presets()
    defaults = [p for p in presets if p["is_default"]]
    assert len(defaults) == 1
    assert defaults[0]["name"] == zp.DEFAULT_PRESET


def test_list_presets_includes_full_by_role_config():
    """Picker can render a preview without a follow-up fetch."""
    for p in zp.list_presets():
        for role in ("orchestrator", "specialist", "messenger"):
            cfg = p["by_role"][role]
            assert "sensitivity_level" in cfg
            assert "capabilities" in cfg
            assert "dispatches_cross_zone" in cfg


def test_list_presets_includes_human_metadata():
    for p in zp.list_presets():
        assert p["label"]
        assert p["summary"]
        assert p["tradeoff"]


# ---------- Endpoint ---------- #


def test_get_zone_presets_endpoint_returns_three_presets(client, alice):
    api_key = alice["api_key"]["plaintext"]
    r = client.get(
        "/workspaces/me/zone-presets",
        headers=auth_headers(api_key),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "presets" in body
    names = [p["name"] for p in body["presets"]]
    assert names == ["open_team", "standard_team", "compliance_team"]


def test_get_zone_presets_endpoint_unauthenticated_401(client):
    r = client.get("/workspaces/me/zone-presets")
    assert r.status_code == 401


def test_get_zone_presets_response_is_renderable_by_dashboard(client, alice):
    """Smoke test: each preset has the fields the picker reads."""
    api_key = alice["api_key"]["plaintext"]
    r = client.get(
        "/workspaces/me/zone-presets",
        headers=auth_headers(api_key),
    )
    assert r.status_code == 200
    for p in r.json()["presets"]:
        assert {"name", "label", "summary", "tradeoff", "by_role", "is_default"}.issubset(p.keys())
        assert isinstance(p["by_role"], dict)
        for role_cfg in p["by_role"].values():
            assert isinstance(role_cfg["capabilities"], list)
            assert isinstance(role_cfg["dispatches_cross_zone"], bool)
