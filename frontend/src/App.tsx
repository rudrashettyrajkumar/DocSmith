import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";

/* ---------------------------------------------------------------- types */

type ModelInfo = { id: string; name: string; free: boolean };
type Catalog = Record<string, ModelInfo[]>;
type Todo = { content: string; status: "pending" | "in_progress" | "completed" };
type JobEvent = { at: number; text: string };
type JobResult = {
  filename: string;
  title: string;
  document_type: string;
  assumptions: string[];
  summary: string;
  download_url: string;
  partial?: boolean;
};
type Job = {
  id: string;
  provider: string;
  model: string;
  status: "queued" | "running" | "completed" | "failed";
  todos: Todo[];
  events: JobEvent[];
  stream_text: string;
  result: JobResult | null;
  error: string | null;
  error_type: "rate_limit" | "auth" | "unknown" | null;
  elapsed_seconds: number;
};

const PROVIDERS = [
  { id: "groq", label: "Groq", hint: "Free tier · fastest inference", keyUrl: "console.groq.com/keys" },
  { id: "openrouter", label: "OpenRouter", hint: "Free + paid · 300+ models", keyUrl: "openrouter.ai/keys" },
  { id: "openai", label: "OpenAI", hint: "Paid · GPT-4o family", keyUrl: "platform.openai.com/api-keys" },
  { id: "anthropic", label: "Anthropic", hint: "Paid · Claude family", keyUrl: "console.anthropic.com" },
] as const;

const SAMPLES = [
  {
    tag: "Standard",
    text: "Create a business proposal for offering an AI-powered customer support chatbot to a mid-sized online retail company.",
  },
  {
    tag: "Complex",
    text: "We have a leadership meeting next week about our mobile app project which is in trouble - costs are exploding, the client keeps demanding new features, and two developers just quit. Prepare something we can present. Keep it positive but also honest about the risks.",
  },
];

/* ------------------------------------------------------------- helpers */

// Set VITE_API_URL when the frontend (Vercel) and backend (Render) are
// deployed separately. Left blank, calls stay relative — used for local dev
// (Vite proxy) and the fallback where FastAPI serves the built frontend itself.
const API_BASE = import.meta.env.VITE_API_URL ?? "";

const spring = { type: "spring", stiffness: 320, damping: 28 } as const;

/** The raw stream includes the JSON envelope of the save_word_document tool
 * call (that's where the model writes the document) — strip it and undo the
 * common string escapes so the live pane reads like a document being typed. */
function cleanDraft(raw: string): string {
  return raw
    .replace(/\{\s*"document_json"\s*:\s*"/g, "")
    .replace(/"\s*\}\s*(\n\n|$)/g, "$1")
    .replace(/\\n/g, "\n")
    .replace(/\\t/g, "  ")
    .replace(/\\"/g, '"')
    .replace(/\\\\/g, "\\");
}

const downloadHref = (url: string) => `${API_BASE}${url}`;

function useLocalStorage(key: string, initial: string) {
  const [value, setValue] = useState(() => localStorage.getItem(key) ?? initial);
  useEffect(() => setValue(localStorage.getItem(key) ?? initial), [key]); // reload when key changes
  useEffect(() => localStorage.setItem(key, value), [key, value]);
  return [value, setValue] as const;
}

/* ------------------------------------------------------ small widgets */

function StatusIcon({ status }: { status: Todo["status"] }) {
  if (status === "completed")
    return (
      <motion.span initial={{ scale: 0 }} animate={{ scale: 1 }} transition={spring}
        className="flex h-5 w-5 items-center justify-center rounded-full bg-sea-400/20 text-sea-400">
        <svg viewBox="0 0 16 16" className="h-3 w-3" fill="none" stroke="currentColor" strokeWidth="2.4">
          <path d="M3 8.5l3.2 3L13 4.5" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </motion.span>
    );
  if (status === "in_progress")
    return (
      <span className="relative flex h-5 w-5 items-center justify-center">
        <motion.span
          className="absolute h-5 w-5 rounded-full border-2 border-brass-400 border-t-transparent"
          animate={{ rotate: 360 }}
          transition={{ repeat: Infinity, duration: 0.9, ease: "linear" }}
        />
        <span className="h-1.5 w-1.5 rounded-full bg-brass-400" />
      </span>
    );
  return <span className="flex h-5 w-5 items-center justify-center"><span className="h-4 w-4 rounded-full border border-white/25" /></span>;
}

function TodoList({ todos }: { todos: Todo[] }) {
  return (
    <ul className="space-y-2.5">
      <AnimatePresence initial={false}>
        {todos.map((t, i) => (
          <motion.li
            key={t.content}
            layout
            initial={{ opacity: 0, x: -14 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ ...spring, delay: i * 0.04 }}
            className="flex items-start gap-3"
          >
            <StatusIcon status={t.status} />
            <span className={`text-sm leading-5 ${t.status === "completed" ? "text-white/40 line-through decoration-white/20" : t.status === "in_progress" ? "text-brass-300" : "text-white/70"}`}>
              {t.content}
            </span>
          </motion.li>
        ))}
      </AnimatePresence>
    </ul>
  );
}

/* ------------------------------------------------------------- the app */

export default function App() {
  const [catalog, setCatalog] = useState<Catalog>({});
  const [provider, setProvider] = useState<(typeof PROVIDERS)[number]["id"]>("groq");
  const [model, setModel] = useState("");
  const [freeOnly, setFreeOnly] = useState(true);
  const [search, setSearch] = useState("");
  const [apiKey, setApiKey] = useLocalStorage(`agent-key-${provider}`, "");
  const [request, setRequest] = useState("");
  const [job, setJob] = useState<Job | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState("");
  const pollRef = useRef<number | null>(null);
  const keyInputRef = useRef<HTMLInputElement | null>(null);
  const draftRef = useRef<HTMLDivElement | null>(null);

  // keep the live draft scrolled to the newest tokens
  useEffect(() => {
    const el = draftRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [job?.stream_text]);

  const focusApiKey = () => {
    keyInputRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
    keyInputRef.current?.focus();
  };

  useEffect(() => {
    fetch(`${API_BASE}/api/models`)
      .then((r) => r.json())
      .then((d) => setCatalog(d.providers))
      .catch(() => setFormError("Could not load model catalog — is the backend running?"));
  }, []);

  const models = useMemo(() => {
    let list = catalog[provider] ?? [];
    if (provider === "openrouter") {
      if (freeOnly) list = list.filter((m) => m.free);
      if (search) list = list.filter((m) => m.id.toLowerCase().includes(search.toLowerCase()));
    }
    return list;
  }, [catalog, provider, freeOnly, search]);

  useEffect(() => {
    if (models.length && !models.some((m) => m.id === model)) setModel(models[0].id);
  }, [models, model]);

  const running = job !== null && (job.status === "queued" || job.status === "running");

  const stopPolling = useCallback(() => {
    if (pollRef.current !== null) { window.clearInterval(pollRef.current); pollRef.current = null; }
  }, []);

  const submit = async () => {
    setFormError("");
    if (request.trim().length < 10) return setFormError("Describe the document you need (at least 10 characters).");
    if (apiKey.trim().length < 8) return setFormError("Paste your API key for the selected provider.");
    setSubmitting(true);
    setJob(null);
    stopPolling();
    try {
      const res = await fetch(`${API_BASE}/agent`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request: request.trim(), provider, model, api_key: apiKey.trim() }),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => null);
        throw new Error(typeof detail?.detail === "string" ? detail.detail : `Request failed (${res.status})`);
      }
      const { job_id } = await res.json();
      pollRef.current = window.setInterval(async () => {
        const j: Job = await (await fetch(`${API_BASE}/api/jobs/${job_id}`)).json();
        setJob(j);
        if (j.status === "completed" || j.status === "failed") stopPolling();
      }, 800);
    } catch (e) {
      setFormError(e instanceof Error ? e.message : "Something went wrong.");
    } finally {
      setSubmitting(false);
    }
  };

  useEffect(() => stopPolling, [stopPolling]);

  const providerMeta = PROVIDERS.find((p) => p.id === provider)!;

  return (
    <div className="mx-auto max-w-6xl px-5 pb-16 pt-10 font-sans">
      {/* header */}
      <motion.header initial={{ opacity: 0, y: -12 }} animate={{ opacity: 1, y: 0 }} transition={spring}
        className="mb-10 flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="mb-1 font-mono text-[11px] uppercase tracking-[0.25em] text-brass-400">Deep Agents · LangChain</p>
          <h1 className="font-display text-4xl font-semibold tracking-tight">
            Docsmith <span className="text-white/40">—</span> <span className="italic text-brass-300">autonomous</span> document agent
          </h1>
          <p className="mt-2 max-w-xl text-sm text-white/55">
            Describe what you need. The agent plans its own tasks, executes them with the model you choose, and delivers a polished Word document.
          </p>
        </div>
        <div className="card px-4 py-2.5 text-xs text-white/50">
          Bring your own key · keys stay in your browser &amp; one request
        </div>
      </motion.header>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,5fr)_minmax(0,6fr)]">
        {/* ------------------------------------------------ config panel */}
        <motion.section initial={{ opacity: 0, y: 14 }} animate={{ opacity: 1, y: 0 }} transition={{ ...spring, delay: 0.05 }}
          className="card p-6">
          <h2 className="mb-4 font-display text-lg font-semibold">1 · Choose your model</h2>

          <div className="mb-4 grid grid-cols-2 gap-2">
            {PROVIDERS.map((p) => (
              <button key={p.id} onClick={() => setProvider(p.id)}
                className={`rounded-xl border px-3 py-2.5 text-left transition-all ${
                  provider === p.id
                    ? "border-brass-400/70 bg-brass-400/10 shadow-[0_0_20px_rgba(201,162,75,0.12)]"
                    : "border-white/10 bg-white/[0.02] hover:border-white/25"}`}>
                <span className={`block text-sm font-semibold ${provider === p.id ? "text-brass-300" : "text-white/85"}`}>{p.label}</span>
                <span className="block text-[11px] text-white/45">{p.hint}</span>
              </button>
            ))}
          </div>

          {provider === "openrouter" && (
            <div className="mb-3 flex items-center gap-3">
              <label className="flex cursor-pointer items-center gap-2 text-xs text-white/70">
                <button role="switch" aria-checked={freeOnly} onClick={() => setFreeOnly(!freeOnly)}
                  className={`h-5 w-9 rounded-full p-0.5 transition-colors ${freeOnly ? "bg-sea-400/70" : "bg-white/15"}`}>
                  <motion.span layout transition={spring}
                    className={`block h-4 w-4 rounded-full bg-white ${freeOnly ? "ml-4" : "ml-0"}`} />
                </button>
                Free models only
              </label>
              <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search e.g. nemotron…"
                className="field flex-1 px-3 py-1.5 text-xs text-white placeholder-white/30" />
            </div>
          )}

          <label className="mb-1 block text-xs font-medium text-white/60">Model</label>
          <select value={model} onChange={(e) => setModel(e.target.value)}
            className="field mb-4 w-full px-3 py-2.5 text-sm">
            {models.map((m) => (
              <option key={m.id} value={m.id}>{m.free ? "🟢 " : ""}{m.name}{m.name !== m.id ? ` — ${m.id}` : ""}</option>
            ))}
            {models.length === 0 && <option>Loading models…</option>}
          </select>

          <label className="mb-1 block text-xs font-medium text-white/60">
            {providerMeta.label} API key <span className="text-white/35">· get one at {providerMeta.keyUrl}</span>
          </label>
          <input ref={keyInputRef} type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="sk-…"
            className="field mb-6 w-full px-3 py-2.5 font-mono text-sm text-white placeholder-white/30" />

          <h2 className="mb-3 font-display text-lg font-semibold">2 · Describe the document</h2>
          <textarea value={request} onChange={(e) => setRequest(e.target.value)} rows={5}
            placeholder="e.g. Draft meeting minutes for yesterday's product sync covering roadmap slips and hiring…"
            className="field w-full resize-none px-3 py-2.5 text-sm leading-6 text-white placeholder-white/30" />

          <div className="mt-3 flex flex-wrap gap-2">
            {SAMPLES.map((s) => (
              <button key={s.tag} onClick={() => setRequest(s.text)}
                className="rounded-full border border-white/12 bg-white/[0.03] px-3 py-1 text-[11px] text-white/60 transition hover:border-brass-400/60 hover:text-brass-300">
                {s.tag} sample
              </button>
            ))}
          </div>

          <AnimatePresence>
            {formError && (
              <motion.p initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }} exit={{ opacity: 0, height: 0 }}
                className="mt-3 text-xs text-red-400">{formError}</motion.p>
            )}
          </AnimatePresence>

          <motion.button whileTap={{ scale: 0.98 }} whileHover={{ y: -1 }} onClick={submit}
            disabled={submitting || running}
            className="mt-5 w-full rounded-xl bg-gradient-to-r from-brass-500 to-brass-400 px-4 py-3 text-sm font-semibold text-ink-950 shadow-[0_8px_30px_rgba(201,162,75,0.25)] transition disabled:cursor-not-allowed disabled:opacity-50">
            {running ? "Agent is working…" : submitting ? "Starting…" : "Run the agent →"}
          </motion.button>
        </motion.section>

        {/* ------------------------------------------------ activity panel */}
        <motion.section initial={{ opacity: 0, y: 14 }} animate={{ opacity: 1, y: 0 }} transition={{ ...spring, delay: 0.12 }}
          className="card flex min-h-[560px] flex-col p-6">
          <div className="mb-4 flex items-center justify-between">
            <h2 className="font-display text-lg font-semibold">Agent activity</h2>
            {job && (
              <span className={`rounded-full px-3 py-1 font-mono text-[11px] uppercase tracking-wider ${
                job.status === "completed" ? "bg-sea-400/15 text-sea-400"
                : job.status === "failed" ? "bg-red-500/15 text-red-400"
                : "bg-brass-400/15 text-brass-300"}`}>
                {job.status} · {job.elapsed_seconds}s
              </span>
            )}
          </div>

          {!job && (
            <div className="flex flex-1 flex-col items-center justify-center text-center">
              <motion.div animate={{ y: [0, -8, 0] }} transition={{ repeat: Infinity, duration: 3, ease: "easeInOut" }}
                className="mb-4 text-5xl">📄</motion.div>
              <p className="max-w-xs text-sm text-white/45">
                The agent's self-generated task list, tool calls and the finished document will appear here.
              </p>
            </div>
          )}

          {job && (
            <div className="flex flex-1 flex-col gap-5 overflow-hidden">
              <div>
                <p className="mb-2.5 font-mono text-[11px] uppercase tracking-[0.2em] text-white/40">Task plan — written by the agent</p>
                {job.todos.length > 0
                  ? <TodoList todos={job.todos} />
                  : <p className="text-sm text-white/40">Waiting for the agent to write its plan…</p>}
              </div>

              {job.stream_text && (
                <div className="flex min-h-0 flex-1 flex-col">
                  <p className="mb-2 flex items-center gap-2 font-mono text-[11px] uppercase tracking-[0.2em] text-white/40">
                    Live draft
                    {running && (
                      <motion.span animate={{ opacity: [1, 0.25, 1] }} transition={{ repeat: Infinity, duration: 1.4 }}
                        className="h-1.5 w-1.5 rounded-full bg-brass-400" />
                    )}
                  </p>
                  <div ref={draftRef}
                    className="scroll-slim min-h-[140px] flex-1 overflow-y-auto rounded-xl border border-white/10 bg-white/[0.04] p-4">
                    <pre className="whitespace-pre-wrap font-sans text-[13px] leading-6 text-white/75">
                      {cleanDraft(job.stream_text)}
                      {running && (
                        <motion.span animate={{ opacity: [1, 0, 1] }} transition={{ repeat: Infinity, duration: 0.9 }}
                          className="ml-0.5 inline-block h-3.5 w-[2px] translate-y-0.5 bg-brass-300" />
                      )}
                    </pre>
                  </div>
                </div>
              )}

              <div className={job.stream_text ? "" : "min-h-0 flex-1"}>
                <p className="mb-2 font-mono text-[11px] uppercase tracking-[0.2em] text-white/40">Log</p>
                <div className="scroll-slim max-h-28 space-y-1.5 overflow-y-auto pr-1">
                  {job.events.map((e, i) => (
                    <motion.p key={i} initial={{ opacity: 0 }} animate={{ opacity: 1 }}
                      className="font-mono text-[11.5px] text-white/55">
                      <span className="text-white/30">{e.at.toFixed(1)}s</span> {e.text}
                    </motion.p>
                  ))}
                </div>
              </div>

              <AnimatePresence>
                {job.status === "failed" && job.error_type === "rate_limit" && (
                  <motion.div initial={{ opacity: 0, y: 16, scale: 0.98 }} animate={{ opacity: 1, y: 0, scale: 1 }} transition={spring}
                    className="rounded-xl border border-brass-400/40 bg-gradient-to-br from-brass-400/15 via-brass-500/[0.07] to-transparent p-5">
                    <div className="flex items-start gap-3">
                      <motion.span animate={{ rotate: [0, -12, 12, 0] }} transition={{ repeat: Infinity, duration: 2.4, ease: "easeInOut" }}
                        className="text-2xl">⏳</motion.span>
                      <div className="min-w-0 flex-1">
                        <p className="font-display text-lg font-semibold text-brass-300">Your model's API limit has been reached</p>
                        <p className="mt-1 text-sm leading-6 text-white/65">
                          The free tier for <span className="font-mono text-xs text-brass-200">{job.model}</span> ran
                          out of tokens mid-run. Paste a premium API key — or switch to another provider or model — and run it again.
                        </p>
                        {job.result && (
                          <p className="mt-2 flex items-center gap-1.5 text-xs text-sea-400">
                            <svg viewBox="0 0 16 16" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="2">
                              <path d="M3 8.5l3.2 3L13 4.5" strokeLinecap="round" strokeLinejoin="round" />
                            </svg>
                            Everything generated so far was recovered into a Word document.
                          </p>
                        )}
                        <div className="mt-4 flex flex-wrap gap-2">
                          {job.result && (
                            <a href={downloadHref(job.result.download_url)}
                              className="inline-flex items-center gap-2 rounded-lg bg-brass-400 px-4 py-2.5 text-sm font-semibold text-ink-950 transition hover:brightness-110">
                              <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.8">
                                <path d="M8 2v8m0 0l-3-3m3 3l3-3M3 13h10" strokeLinecap="round" strokeLinejoin="round" />
                              </svg>
                              Download partial draft (.docx)
                            </a>
                          )}
                          <button onClick={focusApiKey}
                            className="inline-flex items-center gap-2 rounded-lg border border-brass-400/50 px-4 py-2.5 text-sm font-semibold text-brass-300 transition hover:bg-brass-400/10">
                            Use a premium key →
                          </button>
                        </div>
                        <p className="mt-3 truncate font-mono text-[10.5px] text-white/30" title={job.error ?? undefined}>{job.error}</p>
                      </div>
                    </div>
                  </motion.div>
                )}

                {job.status === "failed" && job.error_type !== "rate_limit" && (
                  <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}
                    className="rounded-xl border border-red-500/30 bg-red-500/10 p-4">
                    <p className="mb-1 text-sm font-semibold text-red-300">
                      {job.error_type === "auth" ? "API key rejected" : "The run failed"}
                    </p>
                    <p className="font-mono text-xs leading-5 text-red-200/70">{job.error}</p>
                    <p className="mt-2 text-xs text-white/50">
                      {job.error_type === "auth"
                        ? `The ${job.provider} key you pasted was not accepted — double-check it and try again. Keys are never stored.`
                        : "Check your API key and model choice, then try again — nothing was stored."}
                    </p>
                    <div className="mt-3 flex flex-wrap gap-2">
                      {job.result && (
                        <a href={downloadHref(job.result.download_url)}
                          className="inline-flex items-center gap-2 rounded-lg bg-white/10 px-3 py-2 text-xs font-semibold text-white/80 transition hover:bg-white/15">
                          Download partial draft (.docx)
                        </a>
                      )}
                      {job.error_type === "auth" && (
                        <button onClick={focusApiKey}
                          className="rounded-lg border border-white/20 px-3 py-2 text-xs font-semibold text-white/70 transition hover:bg-white/10">
                          Fix API key →
                        </button>
                      )}
                    </div>
                  </motion.div>
                )}

                {job.status === "completed" && job.result && (
                  <motion.div initial={{ opacity: 0, y: 16, scale: 0.98 }} animate={{ opacity: 1, y: 0, scale: 1 }} transition={spring}
                    className="rounded-xl border border-sea-400/25 bg-sea-400/[0.06] p-5">
                    <p className="mb-1 font-mono text-[11px] uppercase tracking-[0.2em] text-sea-400">{job.result.document_type}</p>
                    <h3 className="font-display text-xl font-semibold">{job.result.title}</h3>
                    <p className="mt-2 text-sm leading-6 text-white/65">{job.result.summary}</p>
                    {job.result.assumptions.length > 0 && (
                      <div className="mt-3">
                        <p className="mb-1 text-xs font-semibold text-white/50">Assumptions the agent made</p>
                        <ul className="list-disc space-y-0.5 pl-5 text-xs text-white/55">
                          {job.result.assumptions.map((a, i) => <li key={i}>{a}</li>)}
                        </ul>
                      </div>
                    )}
                    <a href={downloadHref(job.result.download_url)}
                      className="mt-4 inline-flex items-center gap-2 rounded-lg bg-sea-400 px-4 py-2.5 text-sm font-semibold text-ink-950 transition hover:brightness-110">
                      <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.8">
                        <path d="M8 2v8m0 0l-3-3m3 3l3-3M3 13h10" strokeLinecap="round" strokeLinejoin="round" />
                      </svg>
                      Download .docx
                    </a>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>
          )}
        </motion.section>
      </div>

      <footer className="mt-10 text-center font-mono text-[11px] text-white/30">
        FastAPI · LangChain Deep Agents · python-docx — your API key is sent only with your request and never persisted server-side
      </footer>
    </div>
  );
}
