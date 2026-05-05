"use client";

import Link from "next/link";

const SAMPLE_BOT_PY = `import lightsei
import os
import time

# Lightsei reads your workspace API key from env (the worker injects
# it automatically when this bot is deployed via the dashboard or CLI).
lightsei.init(
    api_key=os.environ["LIGHTSEI_API_KEY"],
    agent_name="my-first-bot",
    version="0.1.0",
)


@lightsei.track
def do_some_work():
    # Anything you call inside a @lightsei.track function shows up as
    # a "run" on the dashboard. The OpenAI / Anthropic / Gemini SDKs
    # are auto-instrumented if installed — every LLM call gets
    # captured (model, tokens, cost) without code changes.
    print("hello from my bot")
    lightsei.emit("custom_event", {"note": "anything you want here"})


def main():
    while True:
        do_some_work()
        time.sleep(60)


if __name__ == "__main__":
    main()
`;

const SAMPLE_REQUIREMENTS = `lightsei>=0.1.3
`;

function CodeBlock({ code, lang }: { code: string; lang?: string }) {
  return (
    <pre className="font-mono text-[12px] bg-gray-50 border border-gray-200 rounded p-3 overflow-x-auto">
      {code}
    </pre>
  );
}

function Step({
  n,
  title,
  children,
}: {
  n: number;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="mb-10">
      <div className="flex items-center gap-3 mb-3">
        <span className="inline-flex items-center justify-center w-7 h-7 rounded-full bg-accent-600 text-white text-sm font-semibold">
          {n}
        </span>
        <h2 className="text-lg font-semibold tracking-tight">{title}</h2>
      </div>
      <div className="ml-10 text-sm text-gray-700 leading-relaxed space-y-3">
        {children}
      </div>
    </section>
  );
}

export default function GettingStartedPage() {
  return (
    <main className="px-8 py-10 max-w-3xl mx-auto">
      <h1 className="text-3xl font-semibold tracking-tight mb-3">
        Get started with Lightsei
      </h1>
      <p className="text-gray-600 mb-10 leading-relaxed">
        Lightsei runs your AI agents on hosted infrastructure with
        observability, guardrails, and a dispatch chain wired in. This page
        walks you from a fresh account to your first deployed bot in about
        five minutes. Skip steps you&apos;ve already done.
      </p>

      <Step n={1} title="Grab your API key">
        <p>
          Your workspace API key authenticates the SDK + CLI. It looks like{" "}
          <code className="font-mono text-xs bg-gray-100 px-1 py-0.5 rounded">
            bk_…
          </code>
          . Keys are shown once at creation; PyPI tokens are a different
          thing — don&apos;t mix them up.
        </p>
        <p>
          <Link
            href="/account"
            className="inline-block px-4 py-1.5 bg-accent-600 text-white rounded-md text-sm font-medium hover:bg-accent-700 no-underline"
          >
            Open /account →
          </Link>{" "}
          <span className="text-xs text-gray-500 ml-2">
            Scroll to &quot;API keys&quot; → create one → copy the value.
          </span>
        </p>
      </Step>

      <Step n={2} title="Pick how to deploy">
        <p>
          A &quot;bot&quot; is just a Python program with{" "}
          <code className="font-mono text-xs bg-gray-100 px-1 py-0.5 rounded">
            bot.py
          </code>{" "}
          and{" "}
          <code className="font-mono text-xs bg-gray-100 px-1 py-0.5 rounded">
            requirements.txt
          </code>
          . The worker installs the deps in a fresh venv and runs{" "}
          <code className="font-mono text-xs bg-gray-100 px-1 py-0.5 rounded">
            python bot.py
          </code>
          . Pick whichever path fits how you build:
        </p>
        <ul className="list-disc ml-5 space-y-2 text-sm">
          <li>
            <strong>Browser (no terminal needed).</strong> Zip the bot
            directory locally, drop it on{" "}
            <Link
              href="/agents/new"
              className="text-accent-600 hover:underline"
            >
              /agents/new
            </Link>
            . Easiest for non-engineers and one-offs.
          </li>
          <li>
            <strong>CLI.</strong>{" "}
            <code className="font-mono text-xs bg-gray-100 px-1 py-0.5 rounded">
              pip install lightsei
            </code>
            , then{" "}
            <code className="font-mono text-xs bg-gray-100 px-1 py-0.5 rounded">
              lightsei deploy ./my-bot
            </code>
            . Best for iterating quickly while you build.
          </li>
          <li>
            <strong>GitHub push-to-deploy.</strong> Connect a repo on{" "}
            <Link href="/github" className="text-accent-600 hover:underline">
              /github
            </Link>
            , register an agent path, then{" "}
            <code className="font-mono text-xs bg-gray-100 px-1 py-0.5 rounded">
              git push
            </code>{" "}
            redeploys automatically. Best for production.
          </li>
        </ul>
      </Step>

      <Step n={3} title="A minimal bot.py to start from">
        <p>
          Save this as{" "}
          <code className="font-mono text-xs bg-gray-100 px-1 py-0.5 rounded">
            my-first-bot/bot.py
          </code>
          :
        </p>
        <CodeBlock code={SAMPLE_BOT_PY} />
        <p>
          And alongside it,{" "}
          <code className="font-mono text-xs bg-gray-100 px-1 py-0.5 rounded">
            my-first-bot/requirements.txt
          </code>
          :
        </p>
        <CodeBlock code={SAMPLE_REQUIREMENTS} />
        <p className="text-xs text-gray-500">
          Add{" "}
          <code className="font-mono text-xs bg-gray-100 px-1 py-0.5 rounded">
            anthropic
          </code>
          ,{" "}
          <code className="font-mono text-xs bg-gray-100 px-1 py-0.5 rounded">
            openai
          </code>
          , or{" "}
          <code className="font-mono text-xs bg-gray-100 px-1 py-0.5 rounded">
            google-generativeai
          </code>{" "}
          to requirements.txt to make LLM calls — the SDK auto-instruments
          all three (no code changes needed).
        </p>
      </Step>

      <Step n={4} title="Watch it run">
        <p>
          Once your bot is deployed, you have several places to look:
        </p>
        <ul className="list-disc ml-5 space-y-1.5 text-sm">
          <li>
            <Link
              href="/"
              className="text-accent-600 hover:underline font-mono"
            >
              /
            </Link>{" "}
            — home / constellation map. Your bots show up as stars; Polaris
            (the orchestrator) is the bright center.
          </li>
          <li>
            <Link
              href="/runs"
              className="text-accent-600 hover:underline font-mono"
            >
              /runs
            </Link>{" "}
            — every LLM call your bots have made, newest first. Tokens,
            latency, model, cost.
          </li>
          <li>
            <Link
              href="/deployments"
              className="text-accent-600 hover:underline font-mono"
            >
              /deployments
            </Link>{" "}
            — what the worker is actually running. Click a row to see live
            stdout/stderr from the bot.
          </li>
          <li>
            <Link
              href="/dispatch"
              className="text-accent-600 hover:underline font-mono"
            >
              /dispatch
            </Link>{" "}
            — when bots dispatch commands to each other, they form chains.
            Each row is one chain; click to expand the timeline.
          </li>
        </ul>
      </Step>

      <Step n={5} title="Optional: connect Slack + GitHub">
        <p>
          These aren&apos;t required for a working bot, but most people want
          them eventually:
        </p>
        <ul className="list-disc ml-5 space-y-1.5 text-sm">
          <li>
            <Link
              href="/notifications"
              className="text-accent-600 hover:underline font-mono"
            >
              /notifications
            </Link>{" "}
            — wire up a Slack channel (incoming webhook URL) so Hermes can
            post agent results. Discord, Teams, Mattermost, generic webhook
            also supported.
          </li>
          <li>
            <Link
              href="/github"
              className="text-accent-600 hover:underline font-mono"
            >
              /github
            </Link>{" "}
            — register a repo so pushes to specific paths auto-redeploy
            agents. Polaris also reads MEMORY.md + TASKS.md from a
            registered repo if you set the corresponding workspace
            secrets.
          </li>
        </ul>
      </Step>

      <Step n={6} title="What to read once that&apos;s working">
        <p>Concepts that are useful to understand once you have one bot live:</p>
        <ul className="list-disc ml-5 space-y-1.5 text-sm">
          <li>
            <strong>Agents.</strong> A logical name for a bot. Multiple
            deployments can share an agent name; only one runs at a time —
            the latest deploy retires the previous one automatically. Agents
            can also have a pinned LLM provider + model (set on the agent
            detail page) so swapping from Claude to Gemini is one DB write.
          </li>
          <li>
            <strong>Commands + dispatch chains.</strong> Bots can send
            commands to other bots (e.g. Polaris dispatches{" "}
            <code className="font-mono text-xs bg-gray-100 px-1 py-0.5 rounded">
              atlas.run_tests
            </code>
            ). Each dispatch fans out into a chain rooted at whoever
            triggered the first command (a webhook push, a scheduled tick,
            a UI click). The dispatch view renders chains as nested
            timelines.
          </li>
          <li>
            <strong>Approval gates.</strong> Agent-to-agent dispatches start
            in <code className="font-mono text-xs bg-gray-100 px-1 py-0.5 rounded">pending</code>{" "}
            by default — a human clicks approve before the receiving bot
            runs. Auto-approval rules let you skip the click for trusted{" "}
            (source, target, kind) tuples.
          </li>
          <li>
            <strong>Validators.</strong> Per-event-kind schema or content
            checks. Set in the dashboard; failed validations either block
            the event (in <code className="font-mono text-xs bg-gray-100 px-1 py-0.5 rounded">strict</code>{" "}
            mode) or just record an audit row (
            <code className="font-mono text-xs bg-gray-100 px-1 py-0.5 rounded">advisory</code>
            ).
          </li>
        </ul>
      </Step>

      <div className="mt-12 pt-8 border-t border-gray-200 text-sm text-gray-500">
        <p>
          Stuck on something? The deployment detail page has the bot&apos;s
          live stdout/stderr — most setup issues surface there as a Python
          traceback or a pip install error.
        </p>
      </div>
    </main>
  );
}
