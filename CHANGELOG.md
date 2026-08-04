# Changelog

All notable changes to ApplyPilot will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

Silent failures — each of these reported success while producing nothing
usable, so the pipeline looked healthy while dropping data.

- **Discovery stored no non-remote jobs.** `_location_ok()` treated an empty
  `location_accept` list as an allowlist rather than "no restriction". A crawl
  scraped 235 jobs and stored 0 with no error raised.
- **Apply agent navigated to a URL called "None".** `str(None)` yields the
  truthy string `"None"`, which passed a `!= "nan"` check and was stored in
  `application_url`; downstream `application_url or url` fallbacks then
  resolved to that string. Added `_clean_str()` and applied it to every
  scraped field.
- **Scorer was told every employer was the job board.** `company` was read
  from the scrape then dropped before `INSERT`, so the prompt received
  `COMPANY: linkedin` for every job. Added a `company` column with
  auto-migration.
- **Scoring measured skills overlap, not fit.** The prompt ranked a junior
  graduate scheme 8-9/10 for a candidate with six years' experience.
  Rewritten around capability AND level, scored as the lower of the two, with
  hard rules for entry-level roles, wrong job families, and non-job postings.
  Candidate title, years, target role and job family are now passed
  explicitly instead of inferred from resume prose.
- **Parse failures were recorded as spam.** An unparseable response defaulted
  to `score=0`, which the prompt defines as "not a real job posting" — ten
  genuine Product Manager roles were permanently written off. Unparseable
  responses now return `None` and leave `fit_score` NULL for retry. Added a
  JSON fallback parser, since models answer with JSON despite the requested
  format. LLM errors likewise no longer persist as `0`.
- **Correct resumes were rejected by the validator.** The preserved-companies
  check inspected only the `header` field; models legitimately put the role in
  `header` and the employer in `subtitle`. Approvals went from 2/9 to 15/16.
  Genuine omissions still fail.
- **Two schools could never both validate.** `preserved_school` was a single
  string checked as one literal, so every tailored resume hard-failed. Now
  accepts a list and splits comma strings.
- **Reasoning models returned empty output.** Models that emit
  chain-of-thought count it against `max_tokens`; the tailor stage requests
  2048, which they spend entirely on reasoning. Added
  `LLM_MIN_OUTPUT_TOKENS`, a named error for the condition, and detection of
  empty `content` with a populated `reasoning_content`.
- **Multi-hour runs lost everything on interruption.** Scoring and tailoring
  batched all database writes until the loop ended. Both now commit per job.
- **Salary was dollar-prefixed regardless of currency** (`$3200000 INR`) with
  a `$110K` senior floor meaningless outside USD. Replaced with per-region
  bands selected from the job's location, a percentage-based senior uplift,
  monthly-vs-annual handling for markets that quote monthly, and an absolute
  rule against entering any figure below the floor.
- **Overseas roles were always rejected.** The location check and screening
  section hardcoded "cannot relocate". Now driven by `profile.relocation`.
- **Sponsorship could not be answered honestly.** A single boolean cannot
  express that a citizen of one country needs no sponsorship there and does
  need one elsewhere. Now states the per-country rule explicitly.
- **Applications could be submitted with the wrong contact email.** Sites
  pre-fill the account address, often a work address. The profile email is
  now authoritative and the agent stops rather than submitting with another.
- **`--location` filtering never worked.** It matched a `label` key that
  discovery never wrote.

### Added

- **Time-limited API keys.** Keys are scrubbed from `.env` after a
  configurable TTL (default 30 days); rotation is detected by fingerprint and
  restarts the clock. The key value is never written to the metadata file.
  New `applypilot key status|set|expire` command.
- **Job-family filtering at discovery.** Job boards return engineering roles
  for product queries because the descriptions mention the same tools;
  scoring them costs one LLM call each to reach a foregone conclusion.
  `title_exclude` / `title_keep` in `searches.yaml` drop them before they
  reach the database, with `keep` checked first so "Technical Product
  Manager" survives. Skipped counts are logged, not silently dropped.
- **Duplicate-application guard.** Employers bulk-post one requisition across
  many URLs (20 for a single req in observed data); the `url` PRIMARY KEY
  cannot see these. A posting whose company+title matches an existing
  `applied` record is marked `duplicate` and skipped. Company is required for
  a match, since titles like "Product Manager" are too generic alone.
- **Bidirectional LLM failover.** `LLM_FALLBACK_URL` allows cloud-to-local as
  well as local-to-cloud, and quota-exhaustion 429s switch provider
  immediately rather than exhausting retries on a limit that will not reset.
- **`--location` / `-l`** on `run` and `apply`, matching on label or location
  text.
- **`--strict-mcp-config`** on the apply agent, which previously inherited
  every MCP server configured on the machine while running in
  `bypassPermissions` mode.
- **`LLM_TIMEOUT`** for slow local models.
- **Regression test suite** (`tests/test_regressions.py`, 65 tests) covering
  every fix above.
- **Docs**: `docs/KEY-ROTATION.md`, `docs/LOCAL-LLM.md`.

### Changed

- `init` no longer echoes API keys to the terminal and writes `.env` with
  `0600` permissions.
- The wizard asks for multiple locations and generates one search entry per
  place; a single comma-joined string matched nothing on the job boards.
- `.gitignore` covers CV and resume files, which were otherwise committable.
- `profile.example.json` documents the new `relocation`, `by_region`,
  `job_family` and per-country sponsorship fields, and defaults
  `personal.password` to empty.

## [0.2.0] - 2026-02-17

### Added
- **Parallel workers for discovery/enrichment** - `applypilot run --workers N` enables
  ThreadPoolExecutor-based parallelism for Workday scraping, smart extract, and detail
  enrichment. Default is sequential (1); power users can scale up.
- **Apply utility modes** - `--gen` (generate prompt for manual debugging), `--mark-applied`,
  `--mark-failed`, `--reset-failed` flags on `applypilot apply`
- **Dry-run mode** - `applypilot apply --dry-run` fills forms without clicking Submit
- **5 new tracking columns** - `agent_id`, `last_attempted_at`, `apply_duration_ms`,
  `apply_task_id`, `verification_confidence` for better apply-stage observability
- **Manual ATS detection** - `manual_ats` list in `config/sites.yaml` skips sites with
  unsolvable CAPTCHAs (e.g. TCS iBegin)
- **Qwen3 `/no_think` optimization** - automatically saves tokens when using Qwen models
- **`config.DEFAULTS`** - centralized dict for magic numbers (`min_score`, `max_apply_attempts`,
  `poll_interval`, `apply_timeout`, `viewport`)

### Fixed
- **Config YAML not found after install** - moved `config/` into the package at
  `src/applypilot/config/` so YAML files (employers, sites, searches) ship with `pip install`
- **Search config format mismatch** - wizard wrote `searches:` key but discovery code
  expected `queries:` with tier support. Aligned wizard output and example config
- **JobSpy install isolation** - removed python-jobspy from package dependencies due to
  broken numpy==1.26.3 exact pin in jobspy metadata. Installed separately with `--no-deps`
- **Scoring batch limit** - default limit of 50 silently left jobs unscored across runs.
  Changed to no limit (scores all pending jobs in one pass)
- **Missing logging output** - added `logging.basicConfig(INFO)` so per-job progress for
  scoring, tailoring, and cover letters is visible during pipeline runs

### Changed
- **Blocked sites externalized** - moved from hardcoded sets in launcher.py to
  `config/sites.yaml` under `blocked:` key
- **Site base URLs externalized** - moved from hardcoded dict in detail.py to
  `config/sites.yaml` under `base_urls:` key
- **SSO domains externalized** - moved from hardcoded list in prompt.py to
  `config/sites.yaml` under `blocked_sso:` key
- **Prompt improvements** - screening context uses `target_role` from profile,
  salary section includes `currency_conversion_note` and dynamic hourly rate examples
- **`acquire_job()` fixed** - writes `agent_id` and `last_attempted_at` to proper columns
  instead of misusing `apply_error`
- **`profile.example.json`** - added `currency_conversion_note` and `target_role` fields

## [0.1.0] - 2026-02-17

### Added
- 6-stage pipeline: discover, enrich, score, tailor, cover letter, apply
- Multi-source job discovery: Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs
- Workday employer portal support (46 preconfigured employers)
- Direct career site scraping (28 preconfigured sites)
- 3-tier job description extraction cascade (JSON-LD, CSS selectors, AI fallback)
- AI-powered job scoring (1-10 fit scale with rationale)
- Resume tailoring with factual preservation (no fabrication)
- Cover letter generation per job
- Autonomous browser-based application submission via Playwright
- Interactive setup wizard (`applypilot init`)
- Cross-platform Chrome/Chromium detection (Windows, macOS, Linux)
- Multi-provider LLM support (Gemini, OpenAI, local models via OpenAI-compatible endpoints)
- Pipeline stats and HTML results dashboard
- YAML-based configuration for employers, career sites, and search queries
- Job deduplication across sources
- Configurable score threshold filtering
- Safety limits for maximum applications per run
- Detailed application results logging
