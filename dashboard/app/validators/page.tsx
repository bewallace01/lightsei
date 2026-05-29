"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  UnauthorizedError,
  ValidatorConfig,
  ValidatorMode,
  deleteValidator,
  fetchValidators,
  handleAuthError,
  putValidator,
} from "../api";


function fmtRelative(iso: string): string {
  try {
    const ts = new Date(iso).getTime();
    const diff = Math.max(0, Date.now() - ts);
    const m = Math.round(diff / 60000);
    if (m < 1) return "just now";
    if (m < 60) return `${m}m ago`;
    const h = Math.round(m / 60);
    if (h < 24) return `${h}h ago`;
    const d = Math.round(h / 24);
    return `${d}d ago`;
  } catch {
    return "—";
  }
}


function rowKey(v: ValidatorConfig): string {
  return `${v.event_kind}|${v.validator_name}`;
}


export default function ValidatorsPage() {
  const router = useRouter();
  const [rows, setRows] = useState<ValidatorConfig[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // Per-row local edit state, keyed by `${event_kind}|${validator_name}`.
  // Stays in scope while the user is mid-edit; gets dropped once save lands.
  const [drafts, setDrafts] = useState<Record<string, { config: string; mode: ValidatorMode }>>({});
  const [saving, setSaving] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<Record<string, number>>({});

  const refresh = async () => {
    try {
      const data = await fetchValidators();
      setRows(data);
      setError(null);
    } catch (e) {
      if (handleAuthError(e, router)) return;
      setError(String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const startEdit = (v: ValidatorConfig) => {
    setDrafts((cur) => ({
      ...cur,
      [rowKey(v)]: {
        config: JSON.stringify(v.config, null, 2),
        mode: v.mode,
      },
    }));
  };

  const cancelEdit = (key: string) => {
    setDrafts((cur) => {
      const next = { ...cur };
      delete next[key];
      return next;
    });
  };

  const onSave = async (v: ValidatorConfig) => {
    const key = rowKey(v);
    const draft = drafts[key];
    if (!draft) return;
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(draft.config);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        throw new Error("config must be a JSON object");
      }
    } catch (e) {
      setError(`invalid JSON config: ${(e as Error).message}`);
      return;
    }
    setSaving(key);
    setError(null);
    try {
      await putValidator(v.event_kind, v.validator_name, parsed, draft.mode);
      cancelEdit(key);
      setSavedAt((cur) => ({ ...cur, [key]: Date.now() }));
      await refresh();
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setSaving(null);
    }
  };

  const onDelete = async (v: ValidatorConfig) => {
    if (
      !confirm(
        `Delete the ${v.validator_name} validator on ${v.event_kind}?\n\n` +
        "Polaris won't auto-recreate it on next deploy unless you re-run " +
        "polaris/setup_validators.py. Events of this kind will pass without " +
        "this check until then.",
      )
    ) {
      return;
    }
    setError(null);
    try {
      await deleteValidator(v.event_kind, v.validator_name);
      await refresh();
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    }
  };

  return (
    <main className="px-4 py-6 sm:px-8 sm:py-10 max-w-5xl mx-auto">
      <div className="mb-8">
        <h1 className="text-2xl font-semibold tracking-tight">Validators</h1>
        <p className="text-sm text-gray-500 mt-1 max-w-2xl">
          Per-event-kind checks that run when an agent emits an event. A
          validator can be{" "}
          <span className="font-mono text-xs">advisory</span> (records a
          violation but lets the event through) or{" "}
          <span className="font-mono text-xs">blocking</span> (rejects the
          event with HTTP 422 if it fails). Tune the rules below if a check
          is firing on innocuous content; the new config takes effect on the
          next event ingest.
        </p>
      </div>

      {error && (
        <div className="mb-6 p-3 border border-red-200 bg-red-50 text-red-700 text-sm rounded-md">
          {error}
        </div>
      )}

      {loading ? (
        <div className="text-gray-400 text-sm">loading…</div>
      ) : rows.length === 0 ? (
        <div className="border border-dashed border-gray-200 rounded-lg p-10 text-center">
          <div className="text-gray-700 font-medium mb-2">
            No validators registered yet
          </div>
          <p className="text-sm text-gray-500">
            Polaris registers a default rule pack against{" "}
            <code className="font-mono">polaris.plan</code> events when you
            run <code className="font-mono">polaris/setup_validators.py</code>{" "}
            against your workspace. Until then, no validation runs.
          </p>
        </div>
      ) : (
        <ul className="space-y-3">
          {rows.map((v) => {
            const key = rowKey(v);
            const draft = drafts[key];
            const isSaving = saving === key;
            const justSaved = savedAt[key] && Date.now() - savedAt[key] < 4000;
            return (
              <li
                key={key}
                className="rounded-lg border border-gray-200 bg-white"
              >
                <div className="flex items-baseline justify-between px-4 py-3 border-b border-gray-100">
                  <div className="flex items-baseline gap-3 min-w-0">
                    <span className="font-mono text-sm text-gray-900 truncate">
                      {v.event_kind}
                    </span>
                    <span className="text-xs text-gray-400">·</span>
                    <span className="font-mono text-sm text-gray-700 truncate">
                      {v.validator_name}
                    </span>
                    <span
                      className={
                        "ml-2 inline-block px-2 py-0.5 rounded-full text-[10px] font-medium uppercase tracking-wider " +
                        (v.mode === "blocking"
                          ? "bg-red-100 text-red-800"
                          : "bg-gray-100 text-gray-700")
                      }
                    >
                      {v.mode}
                    </span>
                  </div>
                  <div className="flex items-baseline gap-3 text-xs text-gray-400 shrink-0">
                    <span>updated {fmtRelative(v.updated_at)}</span>
                    {!draft && (
                      <button
                        type="button"
                        onClick={() => startEdit(v)}
                        className="text-accent-600 hover:text-accent-700"
                      >
                        edit
                      </button>
                    )}
                    <button
                      type="button"
                      onClick={() => onDelete(v)}
                      className="text-gray-400 hover:text-red-600"
                    >
                      delete
                    </button>
                  </div>
                </div>

                {draft ? (
                  <div className="px-4 py-3 space-y-3">
                    <label className="block">
                      <span className="text-xs font-medium text-gray-600 uppercase tracking-wider">
                        Mode
                      </span>
                      <select
                        value={draft.mode}
                        onChange={(e) =>
                          setDrafts((cur) => ({
                            ...cur,
                            [key]: {
                              ...cur[key]!,
                              mode: e.target.value as ValidatorMode,
                            },
                          }))
                        }
                        disabled={isSaving}
                        className="mt-1 block w-48 rounded-md border border-gray-300 px-3 py-1.5 text-sm focus:border-indigo-400 focus:ring-1 focus:ring-indigo-400 focus:outline-none"
                      >
                        <option value="advisory">advisory</option>
                        <option value="blocking">blocking</option>
                      </select>
                      <span className="text-[11px] text-gray-500 mt-1 block">
                        {draft.mode === "blocking"
                          ? "Failing this rule rejects the event (HTTP 422). Use sparingly — incoming bot events that hit this validator and fail are dropped on the floor."
                          : "Failing this rule records a violation but lets the event through. Safe default for most rules."}
                      </span>
                    </label>
                    <label className="block">
                      <span className="text-xs font-medium text-gray-600 uppercase tracking-wider">
                        Config (JSON)
                      </span>
                      <textarea
                        value={draft.config}
                        onChange={(e) =>
                          setDrafts((cur) => ({
                            ...cur,
                            [key]: { ...cur[key]!, config: e.target.value },
                          }))
                        }
                        disabled={isSaving}
                        rows={Math.min(20, Math.max(6, draft.config.split("\n").length + 1))}
                        spellCheck={false}
                        className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-[12px] font-mono focus:border-indigo-400 focus:ring-1 focus:ring-indigo-400 focus:outline-none"
                      />
                      <span className="text-[11px] text-gray-500 mt-1 block">
                        Validator-specific schema. For{" "}
                        <code className="font-mono">content_rules</code>:
                        <code className="font-mono">{` {"rules": [{"name", "pattern", "fields", "mode", "severity"}, ...]}`}</code>
                        . For{" "}
                        <code className="font-mono">schema_strict</code>:
                        <code className="font-mono">{` {"schema": <jsonschema>}`}</code>
                        .
                      </span>
                    </label>
                    <div className="flex items-center justify-end gap-3">
                      <button
                        type="button"
                        onClick={() => cancelEdit(key)}
                        disabled={isSaving}
                        className="text-sm text-gray-500 hover:text-gray-900 disabled:opacity-50"
                      >
                        cancel
                      </button>
                      <button
                        type="button"
                        onClick={() => onSave(v)}
                        disabled={isSaving}
                        className="px-4 py-1.5 bg-accent-600 text-white rounded-md text-sm font-medium hover:bg-accent-700 disabled:opacity-50"
                      >
                        {isSaving ? "saving…" : "save"}
                      </button>
                    </div>
                  </div>
                ) : (
                  <div className="px-4 py-3">
                    <pre className="font-mono text-[11px] bg-gray-50 border border-gray-100 rounded p-3 overflow-x-auto text-gray-700 whitespace-pre-wrap">
                      {JSON.stringify(v.config, null, 2)}
                    </pre>
                    {justSaved && (
                      <div className="text-xs text-green-700 mt-2">saved.</div>
                    )}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}

      <div className="mt-12 pt-8 border-t border-gray-200 text-sm text-gray-500 leading-relaxed">
        <p>
          Adding a brand-new validator? The validator function code lives
          in{" "}
          <code className="font-mono">backend/validators/</code> and is
          referenced by name (e.g. <code className="font-mono">content_rules</code>
          ). Once the function exists, register it for an event kind by
          PUTing to <code className="font-mono">/workspaces/me/validators/{"{event_kind}"}/{"{validator_name}"}</code>{" "}
          with a config + mode body — the dashboard then surfaces it here for
          editing.
        </p>
        <p className="mt-3">
          Past validation results show on{" "}
          <Link href="/runs" className="text-accent-600 hover:underline">
            /runs
          </Link>
          : click any run that landed with a fail chip to see which rule
          tripped and on what input.
        </p>
      </div>
    </main>
  );
}
