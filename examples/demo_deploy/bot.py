"""Phase 5 deploy demo bot.

Wraps the four calls from examples/demo_bot.py in a sleep loop so the
deployment stays in `running` long enough to demonstrate heartbeats,
log streaming, stop, and redeploy. Falls back to a plain heartbeat
emit when OPENAI_API_KEY or ANTHROPIC_API_KEY is not set as a
workspace secret, so the demo is observable even without provider
keys.
"""

import os
import sys
import time
import traceback

import lightsei


SLEEP_S = float(os.environ.get("DEMO_SLEEP_S", "20"))


def _make_oai_call() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        return
    import openai

    oai = openai.OpenAI()
    resp = oai.chat.completions.create(
        model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        messages=[{"role": "user", "content": "Say hello in five words."}],
    )
    print(f"openai: {resp.choices[0].message.content}", flush=True)


def _make_ant_call() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return
    import anthropic

    ant = anthropic.Anthropic()
    msg = ant.messages.create(
        model=os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5"),
        max_tokens=40,
        messages=[{"role": "user", "content": "Say hello in five words."}],
    )
    print(f"anthropic: {msg.content[0].text}", flush=True)


@lightsei.track
def heartbeat_run(iteration: int) -> None:
    lightsei.emit("iteration", {"n": iteration})
    print(f"iteration {iteration}: provider calls", flush=True)
    try:
        _make_oai_call()
    except Exception as exc:
        print(f"openai call failed: {exc}", flush=True)
    try:
        _make_ant_call()
    except Exception as exc:
        print(f"anthropic call failed: {exc}", flush=True)
    lightsei.emit("iteration_done", {"n": iteration})


def main() -> None:
    api_key = os.environ.get("LIGHTSEI_API_KEY")
    base_url = os.environ.get("LIGHTSEI_BASE_URL", "https://api.lightsei.com")
    agent_name = os.environ.get("LIGHTSEI_AGENT_NAME", "demo-deploy")

    if not api_key:
        print("LIGHTSEI_API_KEY not set; the bot can't ingest events", flush=True)
        sys.exit(1)

    lightsei.init(
        api_key=api_key,
        agent_name=agent_name,
        version="0.1.0",
        base_url=base_url,
    )

    print(f"bot up: agent={agent_name} base_url={base_url}", flush=True)

    n = 0
    while True:
        n += 1
        try:
            heartbeat_run(n)
        except Exception:
            print(f"iteration {n} crashed:\n{traceback.format_exc()}", flush=True)
        lightsei.flush(timeout=2.0)
        time.sleep(SLEEP_S)


if __name__ == "__main__":
    main()
