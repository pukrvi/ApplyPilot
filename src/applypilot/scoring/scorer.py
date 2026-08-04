"""Job fit scoring: LLM-powered evaluation of candidate-job match quality.

Scores jobs on a 1-10 scale by comparing the user's resume against each
job description. All personal data is loaded at runtime from the user's
profile and resume file.
"""

import json
import logging
import re
import time
from datetime import datetime, timezone

from applypilot.config import RESUME_PATH, load_profile
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.llm import get_client

log = logging.getLogger(__name__)


# ── Scoring Prompt ────────────────────────────────────────────────────────

SCORE_PROMPT = """You are a job fit evaluator advising a candidate on which roles are worth applying to.

Score is NOT "does the candidate have these skills". A junior role always
matches a senior candidate's skills — that does not make it worth applying to.
Score how good this opportunity is FOR THIS CANDIDATE, given their level and
target role.

Two tests, and the score is the LOWER of the two:
  A. CAPABILITY — can they do the job?
  B. LEVEL — is it the right seniority and function? Not a step down, not an
     unreachable stretch, and in the direction they want to go.

SCORING CRITERIA:
- 9-10: Right function, right level or one step up. Should definitely apply.
- 7-8:  Right function, level within one step. Worth applying.
- 5-6:  Adjacent function or a stretch they might land. Apply if volume allows.
- 3-4:  Wrong function or clearly the wrong level in either direction.
- 1-2:  Different field entirely, or a junior/entry role for a senior candidate.

HARD RULES — these override any skills overlap:
- Graduate scheme, internship, trainee, "junior", "assistant", "associate"
  (when it denotes entry level), or 0-2 years required, and the candidate has
  5+ years? Score 1-2. It does not matter how well the skills line up.
- Requires 5+ more years than the candidate has, or is 2+ levels above their
  current title (e.g. IC/Manager -> C-suite, VP, Partner)? Score 2-3.
- Different function that merely mentions the candidate's tools (e.g. a Sales
  or Engineering role that says "AI"), where the candidate would not be doing
  their target job? Score 3-4.
- Not a real job posting — a product advert, a service listing, a course, a
  recruiter's generic talent pool? Score 0.
- A hands-on engineering or research role rather than a product role — the
  day job is writing code, designing systems, building data pipelines, or
  training models (Software/Data/ML/AI Engineer, Solution or Data Architect,
  Data Scientist, DevOps/SRE, Tech Lead, Engineering Manager)? Score 1-2,
  even when the description is full of matching tools. Owning a product that
  USES those technologies is a different job from BUILDING them. Product
  Manager, Product Owner, Product Marketing, and strategy/consulting roles
  are the target family; engineering-adjacent titles are not.

Seniority ladder for reference, lowest to highest:
  Intern/Graduate -> Analyst/Associate -> Manager/Senior -> Lead/Principal/
  Director -> VP/Head -> C-level

Judge the LEVEL from the requirements and responsibilities, not the title
alone: titles inflate and deflate across companies and countries.

RESPOND IN EXACTLY THIS FORMAT (no other text):
SCORE: [0-10]
KEYWORDS: [comma-separated ATS keywords from the job description that match or could match the candidate]
REASONING: [2-3 sentences. State the role's level, the candidate's level, and whether the direction is right.]"""


def _parse_score_response(response: str) -> dict:
    """Parse the LLM's score response into structured data.

    Args:
        response: Raw LLM response text.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    score = None
    keywords = ""
    reasoning = ""

    for line in response.split("\n"):
        line = line.strip()
        if line.startswith("SCORE:"):
            m = re.search(r"\d+", line)
            if m:
                score = max(0, min(10, int(m.group())))
        elif line.startswith("KEYWORDS:"):
            keywords = line.replace("KEYWORDS:", "").strip()
        elif line.startswith("REASONING:"):
            reasoning = line.replace("REASONING:", "").strip()

    # Fallback: models frequently answer with JSON despite the format
    # instruction. Recover the score rather than discarding the job.
    if score is None:
        m = re.search(r'"(?:score|fit_score|rating|fit_rating)"\s*:\s*(\d+)',
                      response, re.I)
        if m:
            score = max(0, min(10, int(m.group(1))))
            if not reasoning:
                rm = re.search(r'"(?:reasoning|rationale|reason)"\s*:\s*"([^"]{0,400})"',
                               response, re.I)
                reasoning = rm.group(1) if rm else response.strip()[:400]

    # Still nothing parseable: score=None so the caller leaves fit_score NULL
    # and retries later. Returning 0 here marked real jobs as "not a real
    # posting" and silently dropped them from the queue.
    if score is None:
        return {"score": None, "keywords": "",
                "reasoning": f"unparseable response: {response.strip()[:200]}"}

    return {"score": score, "keywords": keywords,
            "reasoning": reasoning or response.strip()[:400]}


def score_job(resume_text: str, job: dict) -> dict:
    """Score a single job against the resume.

    Args:
        resume_text: The candidate's full resume text.
        job: Job dict with keys: title, site, location, full_description.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    # `site` is the job board (e.g. "linkedin"), not the employer. Only pass a
    # company when we actually have one — telling the model the employer is
    # "linkedin" is worse than saying nothing.
    lines = [f"TITLE: {job['title']}"]
    company = (job.get("company") or "").strip()
    if company and company.lower() not in ("nan", "none"):
        lines.append(f"COMPANY: {company}")
    lines.append(f"LOCATION: {job.get('location', 'N/A')}")
    if job.get("salary"):
        lines.append(f"POSTED SALARY: {job['salary']}")
    job_text = "\n".join(lines) + f"\n\nDESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"

    # Give the model the candidate's level explicitly. Inferring seniority
    # from resume prose alone is exactly where the scoring went wrong.
    try:
        profile = load_profile()
        exp = profile.get("experience", {})
        candidate_ctx = (
            f"CANDIDATE CONTEXT (authoritative — use this for the LEVEL test):\n"
            f"- Current title: {exp.get('current_title', 'unknown')}\n"
            f"- Years of experience: {exp.get('years_of_experience_total', 'unknown')}\n"
            f"- Target role: {exp.get('target_role', 'unknown')}\n"
            f"- Education: {exp.get('education_level', 'unknown')}\n"
            + (f"- Job family: {exp['job_family']}\n" if exp.get("job_family") else "")
            + (f"- Acceptable families: {', '.join(exp['acceptable_job_families'])}\n"
               if exp.get("acceptable_job_families") else "")
            + (f"- NOT acceptable: {', '.join(exp['excluded_job_families'])}\n"
               if exp.get("excluded_job_families") else "")
            + (f"- Note: {exp['job_family_note']}\n" if exp.get("job_family_note") else "")
            + "\n"
        )
    except Exception:
        candidate_ctx = ""

    messages = [
        {"role": "system", "content": SCORE_PROMPT},
        {"role": "user",
         "content": f"{candidate_ctx}RESUME:\n{resume_text}\n\n---\n\nJOB POSTING:\n{job_text}"},
    ]

    try:
        client = get_client()
        response = client.chat(messages, max_tokens=512, temperature=0.2)
        return _parse_score_response(response)
    except Exception as e:
        log.error("LLM error scoring job '%s': %s", job.get("title", "?"), e)
        # score=None means "not scored", distinct from 0 = "not a real job".
        # Persisting 0 here would permanently mark the job as spam.
        return {"score": None, "keywords": "", "reasoning": f"LLM error: {e}"}


def run_scoring(limit: int = 0, rescore: bool = False) -> dict:
    """Score unscored jobs that have full descriptions.

    Args:
        limit: Maximum number of jobs to score in this run.
        rescore: If True, re-score all jobs (not just unscored ones).

    Returns:
        {"scored": int, "errors": int, "elapsed": float, "distribution": list}
    """
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()

    if rescore:
        query = "SELECT * FROM jobs WHERE full_description IS NOT NULL"
        if limit > 0:
            query += f" LIMIT {limit}"
        jobs = conn.execute(query).fetchall()
    else:
        jobs = get_jobs_by_stage(conn=conn, stage="pending_score", limit=limit)

    if not jobs:
        log.info("No unscored jobs with descriptions found.")
        return {"scored": 0, "errors": 0, "elapsed": 0.0, "distribution": []}

    # Convert sqlite3.Row to dicts if needed
    if jobs and not isinstance(jobs[0], dict):
        columns = jobs[0].keys()
        jobs = [dict(zip(columns, row)) for row in jobs]

    log.info("Scoring %d jobs sequentially...", len(jobs))
    t0 = time.time()
    completed = 0
    errors = 0
    results: list[dict] = []

    for job in jobs:
        result = score_job(resume_text, job)
        result["url"] = job["url"]
        completed += 1

        if result["score"] is None:
            errors += 1
            log.warning("[%d/%d] NOT SCORED (will retry next run)  %s",
                        completed, len(jobs), job.get("title", "?")[:60])
            continue  # leave fit_score NULL so the next run picks it up

        results.append(result)

        # Commit each score as it lands. On a slow local model this loop can
        # run for hours; batching every write until the end meant a crash,
        # a Ctrl+C, or an LM Studio restart threw away the entire run.
        conn.execute(
            "UPDATE jobs SET fit_score = ?, score_reasoning = ?, scored_at = ? WHERE url = ?",
            (result["score"], f"{result['keywords']}\n{result['reasoning']}",
             datetime.now(timezone.utc).isoformat(), result["url"]),
        )
        conn.commit()

        log.info(
            "[%d/%d] score=%d  %s",
            completed, len(jobs), result["score"], job.get("title", "?")[:60],
        )

    elapsed = time.time() - t0
    log.info("Done: %d scored in %.1fs (%.1f jobs/sec)", len(results), elapsed, len(results) / elapsed if elapsed > 0 else 0)

    # Score distribution
    dist = conn.execute("""
        SELECT fit_score, COUNT(*) FROM jobs
        WHERE fit_score IS NOT NULL
        GROUP BY fit_score ORDER BY fit_score DESC
    """).fetchall()
    distribution = [(row[0], row[1]) for row in dist]

    return {
        "scored": len(results),
        "errors": errors,
        "elapsed": elapsed,
        "distribution": distribution,
    }
