# Key Rotation

ApplyPilot deletes API keys after a fixed lifetime instead of leaving them on
disk indefinitely. Default TTL is **30 days**.

## What happens

Every ApplyPilot command loads `~/.applypilot/.env` and then checks each key's
age. When a key reaches its TTL:

1. The key's line is removed from `~/.applypilot/.env`, replaced with a dated
   comment and an empty `VAR=` placeholder.
2. The key is dropped from the running process environment.
3. Its entry in `~/.applypilot/.keymeta.json` is deleted.
4. You get a message telling you to configure a new key.

Everything else in your `.env` — model names, `LLM_URL`, comments — is left
untouched.

Managed keys: `GEMINI_API_KEY`, `OPENAI_API_KEY`, `CAPSOLVER_API_KEY`,
`LLM_API_KEY`. `LLM_URL` is **not** managed, so a local model never stops
working (see [LOCAL-LLM.md](LOCAL-LLM.md)).

## Setting a key

```bash
applypilot key set
```

Prompts with hidden input, writes the key with `0600` permissions, and starts
the 30-day clock. For a different variable:

```bash
applypilot key set --var OPENAI_API_KEY
```

`applypilot init` does the same thing as part of first-time setup.

## Checking status

```bash
applypilot key status
```

```
API key status  (TTL: 30 days)

  GEMINI_API_KEY       23d left (on 2026-09-01)
```

Turns yellow at 5 days left, red once overdue. `applypilot doctor` shows the
same countdown inline.

## Rotating

Get a fresh key from [aistudio.google.com/apikey](https://aistudio.google.com/apikey),
**revoke the old one there**, then:

```bash
applypilot key set
```

Rotation is also detected automatically. ApplyPilot fingerprints the key value
(truncated SHA-256), so if you edit `.env` by hand the changed value is noticed
on the next run and the clock restarts from that day. You never need to reset
anything manually.

## Deleting early

```bash
applypilot key expire            # GEMINI_API_KEY, with confirmation
applypilot key expire --var CAPSOLVER_API_KEY --yes
```

Use this if a key is exposed — in a screenshot, a chat window, a shared
terminal. Deleting it locally is only half the job: **revoke it at the provider
too**, since anyone who saw the value can still use it.

## Changing the TTL

Set `APPLYPILOT_KEY_TTL_DAYS` in your `.env` or shell:

```bash
APPLYPILOT_KEY_TTL_DAYS=14
```

Applies to all managed keys. Invalid or non-positive values fall back to 30.

## Writing a key without a prompt

If you need to script it, keep the value out of your shell history by reading
it into a variable rather than typing it as an argument:

```bash
read -s -p "Gemini key: " k && printf 'GEMINI_API_KEY=%s\n' "$k" >> ~/.applypilot/.env && unset k
```

`read -s` suppresses the echo. The clock starts on the next ApplyPilot run —
`applypilot key status` confirms it. Avoid `export GEMINI_API_KEY=...` on the
command line: that lands in `~/.zsh_history` in plaintext.

## Where things live

| Path | Contents | Mode |
|---|---|---|
| `~/.applypilot/.env` | The keys themselves | `0600` |
| `~/.applypilot/.keymeta.json` | Set-date + truncated hash per key | `0600` |

`.keymeta.json` never contains a key value — only a 16-char SHA-256 prefix used
for change detection. Deleting it is harmless; every key is simply treated as
first-seen and its clock restarts.

## Notes

- Expiry is checked when an ApplyPilot command runs, not by a background timer.
  A key that came due while you weren't using the tool is deleted on the next
  invocation.
- A key present in `.env` with no metadata entry is adopted, not expired. That
  keeps hand-edited files working.
- Expiry only removes the local copy. It does not revoke the key at Google or
  OpenAI — do that in the provider console.
