"""Design assistant (Capella) tests — pure prompt builder + generation +
tick, with a stubbed lightsei module and a fake Anthropic client factory.
"""
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


DESIGN_BOT_PATH = (
    Path(__file__).parent.parent.parent / "agents" / "design" / "bot.py"
).resolve()


@pytest.fixture()
def fake_lightsei(monkeypatch):
    fake = types.ModuleType("lightsei")
    fake.claim_command = MagicMock(return_value=None)
    fake.complete_command = MagicMock(return_value={"id": "cmd-x"})
    fake.send_command = MagicMock(return_value={"id": "cmd-out"})
    fake.emit = MagicMock()
    fake.init = MagicMock()
    monkeypatch.setitem(sys.modules, "lightsei", fake)
    monkeypatch.delitem(sys.modules, "design_bot", raising=False)
    spec = importlib.util.spec_from_file_location("design_bot", str(DESIGN_BOT_PATH))
    bot = importlib.util.module_from_spec(spec)
    sys.modules["design_bot"] = bot
    spec.loader.exec_module(bot)
    return fake, bot


def _factory(text):
    block = types.SimpleNamespace(type="text", text=text)
    usage = types.SimpleNamespace(input_tokens=12, output_tokens=34)
    resp = types.SimpleNamespace(content=[block], usage=usage)
    return lambda api_key: types.SimpleNamespace(
        messages=types.SimpleNamespace(create=lambda **kw: resp))


def _cmd(*, cmd_id="c1", kind="design.format", payload=None):
    return {"id": cmd_id, "agent_name": "design", "kind": kind, "payload": payload or {}}


# ---------- prompt (pure) ---------- #


def test_build_prompt_page_asks_for_html(fake_lightsei):
    _, bot = fake_lightsei
    system, user = bot.build_prompt({"content": "<h1>Hi</h1>", "content_type": "page"})
    assert "responsive HTML" in user and "<style>" in user
    assert "<h1>Hi</h1>" in user
    assert "design specialist" in system


def test_build_prompt_email_and_social_and_generic(fake_lightsei):
    _, bot = fake_lightsei
    assert "email-safe" in bot.build_prompt({"content": "x", "content_type": "email"})[1]
    assert "social post" in bot.build_prompt({"content": "x", "content_type": "social"})[1]
    assert "formatting" in bot.build_prompt({"content": "x", "content_type": "generic"})[1]


def test_build_prompt_unknown_type_falls_back_generic(fake_lightsei):
    _, bot = fake_lightsei
    _, user = bot.build_prompt({"content": "x", "content_type": "banana"})
    assert "formatting" in user  # generic ask


def test_build_prompt_includes_accent_and_instructions(fake_lightsei):
    _, bot = fake_lightsei
    _, user = bot.build_prompt({
        "content": "x", "content_type": "page",
        "accent_color": "#0a7", "instructions": "keep it minimal"})
    assert "#0a7" in user and "keep it minimal" in user


# ---------- generation ---------- #


def test_generate_design_returns_output(fake_lightsei):
    _, bot = fake_lightsei
    out = bot.generate_design({"content": "<h1>Hi</h1>", "content_type": "page"},
                              factory=_factory("<!doctype html>..."), api_key="sk")
    assert out["output"].startswith("<!doctype html>")
    assert out["input_tokens"] == 12 and out["output_tokens"] == 34


def test_generate_design_strips_code_fence(fake_lightsei):
    _, bot = fake_lightsei
    fenced = "```html\n<div>x</div>\n```"
    out = bot.generate_design({"content": "x"}, factory=_factory(fenced), api_key="sk")
    assert out["output"] == "<div>x</div>"


def test_generate_design_empty_content_raises(fake_lightsei):
    _, bot = fake_lightsei
    with pytest.raises(bot.DesignError):
        bot.generate_design({"content": "  "}, factory=_factory("x"), api_key="sk")


# ---------- tick ---------- #


def test_tick_formats_and_emits(fake_lightsei, monkeypatch):
    fake, bot = fake_lightsei
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    fake.claim_command.return_value = _cmd(payload={"content": "<h1>Hi</h1>", "content_type": "page"})
    bot.tick(fake, factory=_factory("<!doctype html><body>styled</body>"), hermes_channel="c")
    assert fake.emit.call_args.args[0] == "design.formatted"
    out = fake.emit.call_args.args[1]
    assert out["content_type"] == "page" and "styled" in out["output"]
    fake.send_command.assert_called_once()  # "polished" note to hermes


def test_tick_without_key_crashes_cleanly(fake_lightsei, monkeypatch):
    fake, bot = fake_lightsei
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake.claim_command.return_value = _cmd(payload={"content": "x"})
    bot.tick(fake)
    assert fake.emit.call_args.args[0] == "design.crash"
    assert "ANTHROPIC_API_KEY" in fake.complete_command.call_args.kwargs["error"]


def test_tick_unknown_kind_fails(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _cmd(kind="design.dance")
    bot.tick(fake)
    assert "does not handle" in fake.complete_command.call_args.kwargs["error"]


def test_tick_empty_queue_returns_none(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = None
    assert bot.tick(fake) is None
    fake.emit.assert_not_called()


def test_design_in_roster_and_identity():
    import builtin_personas
    import assistant_identity
    assert "design" in builtin_personas.BUILTIN_PERSONAS
    assert "design" in builtin_personas.LLM_PERSONAS
    assert assistant_identity.display_label("design") == "Capella · Design"


# ---------- component mode (match a repo page template) ---------- #


def test_build_prompt_component_uses_template(fake_lightsei):
    _, bot = fake_lightsei
    system, user = bot.build_prompt({
        "content_type": "component",
        "template": "import Layout from './Layout';\nexport default function X(){return <Layout><h1>Old</h1></Layout>}",
        "content": "<h1>New page</h1><p>body</p>"})
    assert "TEMPLATE" in user and "NEW CONTENT" in user
    assert "import Layout" in user           # the template source is included
    assert "New page" in user                # the new content is included
    assert "front-end engineer" in system    # uses the component system prompt


def test_generate_design_component(fake_lightsei):
    _, bot = fake_lightsei
    out = bot.generate_design(
        {"content_type": "component", "template": "export default function A(){}",
         "content": "<h1>Hi</h1>"},
        factory=_factory("export default function NewPage(){ return null }"),
        api_key="sk")
    assert "NewPage" in out["output"]


def test_tick_component_emits_with_more_tokens(fake_lightsei, monkeypatch):
    fake, bot = fake_lightsei
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk")
    fake.claim_command.return_value = _cmd(payload={
        "content_type": "component", "template": "export default function A(){}",
        "content": "<h1>Hi</h1>"})
    bot.tick(fake, factory=_factory("export default function NewPage(){}"))
    out = fake.emit.call_args.args[1]
    assert out["content_type"] == "component"
    assert "NewPage" in out["output"]
