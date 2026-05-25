"use client";

import JSZip from "jszip";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  Agent,
  AgentGenerateOutput,
  UnauthorizedError,
  fetchAgents,
  generateAgent,
  handleAuthError,
  patchAgent,
  uploadDeploymentBundle,
} from "../../api";


export default function GenerateAgentPage() {
  const router = useRouter();

  // Form state for the LLM call.
  const [description, setDescription] = useState("");
  const [nameHint, setNameHint] = useState("");
  const [targets, setTargets] = useState<Set<string>>(new Set());

  // Existing agents in the workspace, fetched once on mount, used for
  // the "coordinate with these agents" multi-select.
  const [agents, setAgents] = useState<Agent[]>([]);

  // Generation state.
  const [generating, setGenerating] = useState(false);
  const [output, setOutput] = useState<AgentGenerateOutput | null>(null);
  // Editable copies — mirror output but the user can tweak before deploy.
  const [editName, setEditName] = useState("");
  const [editBotPy, setEditBotPy] = useState("");
  const [editRequirements, setEditRequirements] = useState("");

  // Iteration loop (12B.3): textarea for refinement requests below the
  // preview. Wraps the same generate endpoint with tweak_request +
  // previous_* set so Claude refines instead of starting over.
  const [tweakRequest, setTweakRequest] = useState("");

  const [deploying, setDeploying] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    fetchAgents()
      .then((a) => {
        if (alive) setAgents(a);
      })
      .catch((e) => {
        handleAuthError(e, router);
      });
    return () => {
      alive = false;
    };
  }, [router]);

  // When a generation lands, populate the editable fields.
  useEffect(() => {
    if (output) {
      setEditName(output.agent_name_suggestion);
      setEditBotPy(output.bot_py);
      setEditRequirements(output.requirements_txt);
    }
  }, [output]);

  const onGenerate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!description.trim()) return;
    setGenerating(true);
    setError(null);
    setOutput(null);
    try {
      const result = await generateAgent({
        description: description.trim(),
        target_agents: targets.size > 0 ? Array.from(targets) : undefined,
        name_hint: nameHint.trim() || undefined,
      });
      setOutput(result);
    } catch (e) {
      if (handleAuthError(e, router)) return;
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setGenerating(false);
    }
  };

  const onTweak = async () => {
    if (!tweakRequest.trim() || !editBotPy.trim()) return;
    setGenerating(true);
    setError(null);
    try {
      const result = await generateAgent({
        description: description.trim(),
        target_agents: targets.size > 0 ? Array.from(targets) : undefined,
        name_hint: nameHint.trim() || undefined,
        tweak_request: tweakRequest.trim(),
        previous_bot_py: editBotPy,
        previous_requirements_txt: editRequirements,
      });
      setOutput(result);
      setTweakRequest("");
    } catch (e) {
      if (handleAuthError(e, router)) return;
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setGenerating(false);
    }
  };

  const onDeploy = async () => {
    if (!editName.trim() || !editBotPy.trim()) return;
    setDeploying(true);
    setError(null);
    try {
      // Build the .zip in-browser. Two files at the root: bot.py and
      // requirements.txt — exactly the bundle shape the worker expects.
      const zip = new JSZip();
      zip.file("bot.py", editBotPy);
      zip.file("requirements.txt", editRequirements || "lightsei>=0.1.3\n");
      const blob = await zip.generateAsync({ type: "blob" });
      const file = new File([blob], `${editName.trim()}.zip`, {
        type: "application/zip",
      });
      const dep = await uploadDeploymentBundle(editName.trim(), file);
      // Carry the LLM's rationale forward as the agent's description so
      // it shows up on the /agents roster. Best-effort — don't block the
      // navigation if it fails (the bot deployed; description is polish).
      if (output?.rationale) {
        try {
          await patchAgent(editName.trim(), { description: output.rationale });
        } catch {
          /* ignore */
        }
      }
      router.push(`/deployments/${dep.id}`);
    } catch (e) {
      if (handleAuthError(e, router)) return;
      setError(String(e instanceof Error ? e.message : e));
      setDeploying(false);
    }
  };

  const toggleTarget = (name: string) => {
    setTargets((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  return (
    <main className="px-8 py-10 max-w-4xl mx-auto">
      <div className="mb-6">
        <Link
          href="/agents/new"
          className="text-sm text-gray-500 hover:text-gray-900"
        >
          ← back to deploy
        </Link>
      </div>

      <h1 className="text-2xl font-semibold tracking-tight mb-2">
        Generate a bot from a description
      </h1>
      <p className="text-sm text-gray-500 mb-8 leading-relaxed">
        Describe what you want the bot to do — Lightsei generates a working{" "}
        <code>bot.py</code> + <code>requirements.txt</code> against the SDK,
        coordinating with your existing agents where it makes sense. Review
        the code, edit if you want, then deploy.
      </p>

      {/* Form */}
      <form onSubmit={onGenerate} className="space-y-5 mb-8">
        <label className="block">
          <span className="text-xs font-medium text-gray-600 uppercase tracking-wider">
            Description
          </span>
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="e.g. Watch a public RSS feed every 30 minutes and post new items to Slack via hermes."
            rows={4}
            disabled={generating || deploying}
            className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-indigo-400 focus:ring-1 focus:ring-indigo-400 focus:outline-none disabled:opacity-50"
          />
          <span className="text-[11px] text-gray-500 mt-1 block">
            The clearer the description, the closer the first draft. Mention
            triggers (timer, push, command from another agent), what it should
            DO, and where the result should go.
          </span>
        </label>

        {agents.length > 0 && (
          <div>
            <span className="text-xs font-medium text-gray-600 uppercase tracking-wider">
              Coordinate with (optional)
            </span>
            <div className="mt-1 flex flex-wrap gap-2">
              {agents.map((a) => (
                <button
                  key={a.name}
                  type="button"
                  onClick={() => toggleTarget(a.name)}
                  disabled={generating || deploying}
                  className={
                    "px-2.5 py-1 text-xs rounded-full font-mono border transition-colors " +
                    (targets.has(a.name)
                      ? "bg-accent-600 text-white border-accent-600"
                      : "bg-white text-gray-700 border-gray-300 hover:border-gray-400")
                  }
                >
                  {a.name}
                </button>
              ))}
            </div>
            <span className="text-[11px] text-gray-500 mt-1 block">
              The LLM sees every agent regardless; this just nudges it to
              dispatch to specific ones (e.g. select <code>hermes</code> if
              the bot should produce notifications).
            </span>
          </div>
        )}

        <label className="block">
          <span className="text-xs font-medium text-gray-600 uppercase tracking-wider">
            Name hint (optional)
          </span>
          <input
            type="text"
            value={nameHint}
            onChange={(e) => setNameHint(e.target.value)}
            placeholder="e.g. argus, vega, sirius"
            disabled={generating || deploying}
            className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-1.5 text-sm font-mono focus:border-indigo-400 focus:ring-1 focus:ring-indigo-400 focus:outline-none disabled:opacity-50"
          />
          <span className="text-[11px] text-gray-500 mt-1 block">
            Names come from a curated star-naming dictionary. The generator
            picks one matching the bot&apos;s role — this is a hint if you
            already have a preference.
          </span>
        </label>

        <button
          type="submit"
          disabled={generating || deploying || !description.trim()}
          className="px-5 py-2 bg-accent-600 text-white rounded-md text-sm font-medium hover:bg-accent-700 disabled:opacity-50 transition-colors"
        >
          {generating ? "generating… (~10-30s)" : "generate"}
        </button>
      </form>

      {error && (
        <div className="mb-6 p-3 border border-red-200 bg-red-50 text-red-700 text-sm rounded-md">
          {error}
        </div>
      )}

      {/* Output / preview */}
      {output && (
        <div className="space-y-5 border-t border-gray-200 pt-8">
          <div>
            <h2 className="text-sm font-semibold text-gray-800 mb-1">
              Generated bot
            </h2>
            <p className="text-xs text-gray-500 mb-3">
              {output.rationale}
            </p>
            <p className="text-[11px] text-gray-400 font-mono">
              {output.model_used} · {output.tokens_in ?? "?"} →{" "}
              {output.tokens_out ?? "?"} tokens
            </p>
          </div>

          <label className="block">
            <span className="text-xs font-medium text-gray-600 uppercase tracking-wider">
              Agent name
            </span>
            <input
              type="text"
              value={editName}
              onChange={(e) => setEditName(e.target.value)}
              disabled={deploying}
              className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-1.5 text-sm font-mono focus:border-indigo-400 focus:ring-1 focus:ring-indigo-400 focus:outline-none disabled:opacity-50"
            />
          </label>

          <label className="block">
            <span className="text-xs font-medium text-gray-600 uppercase tracking-wider">
              bot.py
            </span>
            <textarea
              value={editBotPy}
              onChange={(e) => setEditBotPy(e.target.value)}
              disabled={deploying}
              rows={Math.min(28, Math.max(10, editBotPy.split("\n").length + 1))}
              className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-[12px] font-mono focus:border-indigo-400 focus:ring-1 focus:ring-indigo-400 focus:outline-none disabled:opacity-50"
              spellCheck={false}
            />
          </label>

          <label className="block">
            <span className="text-xs font-medium text-gray-600 uppercase tracking-wider">
              requirements.txt
            </span>
            <textarea
              value={editRequirements}
              onChange={(e) => setEditRequirements(e.target.value)}
              disabled={deploying}
              rows={Math.min(8, Math.max(3, editRequirements.split("\n").length + 1))}
              className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-[12px] font-mono focus:border-indigo-400 focus:ring-1 focus:ring-indigo-400 focus:outline-none disabled:opacity-50"
              spellCheck={false}
            />
          </label>

          {/* Iteration loop: ask the LLM to revise without starting over. */}
          <div className="rounded-lg border border-indigo-100 bg-indigo-50/40 p-4">
            <label className="block">
              <span className="text-xs font-medium text-indigo-900 uppercase tracking-wider">
                Regenerate with tweaks
              </span>
              <textarea
                value={tweakRequest}
                onChange={(e) => setTweakRequest(e.target.value)}
                placeholder="e.g. Use httpx instead of requests; tick every 30 minutes instead of 60; also dispatch to argus for security scans."
                rows={2}
                disabled={generating || deploying}
                className="mt-1 block w-full rounded-md border border-indigo-200 bg-white px-3 py-2 text-sm focus:border-indigo-400 focus:ring-1 focus:ring-indigo-400 focus:outline-none disabled:opacity-50"
              />
              <span className="text-[11px] text-indigo-700 mt-1 block">
                The LLM sees your current edits — describe what you want
                changed and it produces a refined version.
              </span>
            </label>
            <button
              type="button"
              onClick={onTweak}
              disabled={generating || deploying || !tweakRequest.trim()}
              className="mt-3 px-4 py-1.5 border border-indigo-300 bg-white text-indigo-700 rounded-md text-sm font-medium hover:bg-indigo-100 disabled:opacity-50 transition-colors"
            >
              {generating ? "regenerating…" : "regenerate"}
            </button>
          </div>

          <div className="flex items-center justify-between pt-2">
            <button
              type="button"
              onClick={() => {
                setOutput(null);
                setError(null);
                setTweakRequest("");
              }}
              disabled={deploying || generating}
              className="text-sm text-gray-500 hover:text-gray-900 disabled:opacity-50"
            >
              start over
            </button>
            <button
              type="button"
              onClick={onDeploy}
              disabled={
                deploying || generating || !editName.trim() || !editBotPy.trim()
              }
              className="px-5 py-2 bg-accent-600 text-white rounded-md text-sm font-medium hover:bg-accent-700 disabled:opacity-50 transition-colors"
            >
              {deploying ? "deploying…" : "deploy"}
            </button>
          </div>
        </div>
      )}
    </main>
  );
}
