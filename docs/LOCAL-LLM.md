# Running ApplyPilot on a Local Model (LM Studio)

Scoring, tailoring, and cover letters can run entirely on your laptop. No API
key, no per-token cost, no rate limits, and no job description or resume text
leaving the machine.

**Read this first:** a local model covers the `run` pipeline only. `applypilot
apply` drives the browser through the Claude Code CLI, which always talks to
Anthropic's API. Setting `LLM_URL` does not change that. If you want *nothing*
to leave your laptop, use `applypilot run` and submit the applications
yourself — see [Keeping everything local](#keeping-everything-local).

## 1. Start the LM Studio server

In the LM Studio app:

1. Load a model (**Chat** tab → pick a model → wait for it to load).
2. Go to the **Developer** tab (older builds: **Local Server**).
3. Click **Start Server**.

It listens on `http://localhost:1234` by default and exposes an
OpenAI-compatible API at `/v1`.

Or from the terminal, if you've installed the LM Studio CLI:

```bash
lms server start
```

List what's loaded and get the exact model identifier:

```bash
lms ps
```

## 2. Point ApplyPilot at it

Run the wizard and choose `local`:

```bash
applypilot init
```

- **Local LLM endpoint URL** → `http://localhost:1234/v1`
- **Model name** → the identifier from `lms ps`, e.g.
  `qwen2.5-14b-instruct`

Or edit `~/.applypilot/.env` directly:

```bash
LLM_URL=http://localhost:1234/v1
LLM_MODEL=qwen2.5-14b-instruct
LLM_TIMEOUT=600
```

`LLM_URL` takes priority over `GEMINI_API_KEY` and `OPENAI_API_KEY`, so you can
leave a cloud key in the file and switch between them by commenting `LLM_URL`
in or out. `LLM_URL` is not a secret and is never expired by the
[key rotation](KEY-ROTATION.md) system.

## 3. Verify

```bash
applypilot doctor
```

Look for `LLM API key  OK  Local: http://localhost:1234/v1` and
`Current tier: Tier 2`.

End-to-end check on a single stage:

```bash
applypilot run score
```

## Choosing a model

The pipeline sends a full job description plus your resume in one prompt and
expects structured JSON back. That means context length matters more than raw
parameter count.

| | Recommendation |
|---|---|
| **Context** | 16k minimum, 32k comfortable. Long JDs plus a resume routinely exceed 8k. |
| **Size** | 14B instruct-tuned is the sweet spot on Apple Silicon. 7–8B works for scoring, gets loose on tailoring. |
| **Type** | Instruct/chat tuned, not base. Base models won't follow the JSON format. |
| **Quant** | Q4_K_M or better. Below Q4 the JSON gets malformed often enough to be annoying. |

Set the context window in LM Studio when you load the model — the loader
default is often lower than the model supports.

Qwen models get a small optimization for free: ApplyPilot prepends `/no_think`
to skip chain-of-thought on extraction tasks, which saves tokens and time.

## Tuning

**Requests timing out.** The default is 120s, which a laptop model can exceed
on a long tailoring prompt. Raise it:

```bash
LLM_TIMEOUT=600
```

**Validation failures on tailor/cover.** Smaller models trip the strict output
checks. Loosen them:

```bash
applypilot run --validation lenient
```

`lenient` ignores banned-word checks and skips the LLM judge pass, which also
cuts the number of round trips roughly in half.

**Too slow overall.** Run discovery and enrichment in parallel — those are
network-bound and don't touch the model — then do the model work serially:

```bash
applypilot run discover enrich -w 4
applypilot run score tailor cover
```

Don't raise `-w` for the model stages. Concurrent requests to one LM Studio
instance queue up and each one gets slower; you gain nothing and risk timeouts.

**Quality noticeably worse than Gemini.** Expected below ~14B, most visibly in
tailoring. A practical split: score locally (cheap, high volume, tolerant of a
smaller model), then switch to Gemini just for the handful of jobs you actually
want to apply to.

```bash
# triage locally
applypilot run discover enrich score -w 4

# comment out LLM_URL in ~/.applypilot/.env, then:
applypilot run tailor cover --min-score 8
```

## Keeping everything local

With `LLM_URL` set, the `run` pipeline makes no LLM calls off your machine. Job
discovery still hits job boards over the network — unavoidable, that's where the
listings are — but your resume and profile stay put.

To keep it that way, stop before `apply`:

```bash
applypilot run              # local model end to end
applypilot dashboard        # review results, submit by hand
```

Tailored resumes land in `~/.applypilot/tailored_resumes/` and cover letters in
`~/.applypilot/cover_letters/`.

If you do run `applypilot apply`, be aware it sends the job details, your
tailored resume, and your applicant profile to Anthropic as part of the agent
prompt — and if you filled in the profile's `password` field, that too. Leave
that field blank.

## Troubleshooting

**`Connection refused`** — server isn't running, or it's on another port. Check
the Developer tab, and confirm the port matches your `LLM_URL`.

**`404 Not Found`** — `/v1` is missing from the URL. It must be
`http://localhost:1234/v1`, not `http://localhost:1234`.

**`model not found`** — `LLM_MODEL` doesn't match what's loaded. Compare against
`lms ps`. Some LM Studio versions want the full path-style identifier
(`publisher/model-name`).

**Empty or truncated responses** — output token limit too low in LM Studio, or
the context window is too small to fit prompt plus response. Raise both in the
model loader.

**Falls back to Gemini unexpectedly** — `LLM_URL` is unset or commented out.
`applypilot doctor` shows which provider is actually selected.

## Other local runtimes

Anything with an OpenAI-compatible `/v1` endpoint works the same way — only the
port changes.

| Runtime | `LLM_URL` |
|---|---|
| LM Studio | `http://localhost:1234/v1` |
| Ollama | `http://localhost:11434/v1` |
| llama.cpp (`llama-server`) | `http://localhost:8080/v1` |

If your endpoint requires a token, set `LLM_API_KEY`. That one *is* treated as
a managed secret and expires on the normal 30-day schedule.
