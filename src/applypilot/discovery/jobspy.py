"""JobSpy-based job discovery: searches Indeed, LinkedIn, Glassdoor, ZipRecruiter.

Uses python-jobspy to scrape multiple job boards, deduplicates results,
parses salary ranges, and stores everything in the ApplyPilot database.

Search queries, locations, and filtering rules are loaded from the user's
search configuration YAML (searches.yaml) rather than being hardcoded.
"""

import logging
import sqlite3
import time
from datetime import datetime, timezone

from jobspy import scrape_jobs

from applypilot import config
from applypilot.database import get_connection, init_db, store_jobs

log = logging.getLogger(__name__)


# -- Proxy parsing -----------------------------------------------------------

def parse_proxy(proxy_str: str) -> dict:
    """Parse host:port:user:pass into components."""
    parts = proxy_str.split(":")
    if len(parts) == 4:
        host, port, user, passwd = parts
        return {
            "host": host,
            "port": port,
            "user": user,
            "pass": passwd,
            "jobspy": f"{user}:{passwd}@{host}:{port}",
            "playwright": {
                "server": f"http://{host}:{port}",
                "username": user,
                "password": passwd,
            },
        }
    elif len(parts) == 2:
        host, port = parts
        return {
            "host": host,
            "port": port,
            "user": None,
            "pass": None,
            "jobspy": f"{host}:{port}",
            "playwright": {"server": f"http://{host}:{port}"},
        }
    else:
        raise ValueError(
            f"Proxy format not recognized: {proxy_str}. "
            f"Expected: host:port:user:pass or host:port"
        )


# -- Retry wrapper -----------------------------------------------------------

def _scrape_with_retry(kwargs: dict, max_retries: int = 2, backoff: float = 5.0):
    """Call scrape_jobs with retry on transient failures."""
    for attempt in range(max_retries + 1):
        try:
            return scrape_jobs(**kwargs)
        except Exception as e:
            err = str(e).lower()
            transient = any(k in err for k in ("timeout", "429", "proxy", "connection", "reset", "refused"))
            if transient and attempt < max_retries:
                wait = backoff * (attempt + 1)
                log.warning("Retry %d/%d in %.0fs: %s", attempt + 1, max_retries, wait, e)
                time.sleep(wait)
            else:
                raise


# -- Location filtering ------------------------------------------------------

def _load_location_config(search_cfg: dict) -> tuple[list[str], list[str]]:
    """Extract accept/reject location lists from search config.

    Falls back to sensible defaults if not defined in the YAML.
    """
    accept = search_cfg.get("location_accept", [])
    reject = search_cfg.get("location_reject_non_remote", [])
    return accept, reject


def _location_ok(location: str | None, accept: list[str], reject: list[str]) -> bool:
    """Check if a job location passes the user's location filter.

    Remote jobs are always accepted. Non-remote jobs must match an accept
    pattern and not match a reject pattern.
    """
    if not location:
        return True  # unknown location -- keep it, let scorer decide

    loc = location.lower()

    # Remote jobs always OK
    if any(r in loc for r in ("remote", "anywhere", "work from home", "wfh", "distributed")):
        return True

    # Reject non-remote matches
    for r in reject:
        if r.lower() in loc:
            return False

    # An empty accept list means "no location restriction", not "reject
    # everything". Treating it as an allowlist silently discarded every
    # on-site job for users who never defined location_accept.
    if not accept:
        return True

    # Accept matches
    for a in accept:
        if a.lower() in loc:
            return True

    # Allowlist configured and nothing matched
    return False


def _clean_str(value) -> str | None:
    """Normalise a scraped cell to a real string or None.

    pandas/jobspy hand back NaN, None, or the strings "nan"/"None"
    interchangeably. str() on any of them yields a TRUTHY string, which
    silently defeats every `x or fallback` downstream.
    """
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in ("", "nan", "none", "null", "<na>"):
        return None
    return text


# -- Title / function filtering ----------------------------------------------

# Titles that are a different job family from product management. Job boards
# return these for product queries because the descriptions mention the same
# tools ("AI", "LLM", "roadmap"), but they are engineering/IC-technical roles.
# Scoring them correctly returns 1-3 every time, so filtering here avoids
# paying an LLM call per job to reach a foregone conclusion.
#
# Matched as whole words against the lowercased title. Override in
# searches.yaml with `title_exclude:` / `title_exclude_extra:`.
DEFAULT_TITLE_EXCLUDE = [
    # hands-on engineering
    "software engineer", "custom software engineer", "backend engineer",
    "frontend engineer", "full stack engineer", "fullstack engineer",
    "devops", "sre", "site reliability", "platform engineer",
    "data platform engineer", "data engineer", "ml engineer",
    "machine learning engineer", "ai engineer", "artificial intelligence engineer",
    "solutions engineer", "systems engineer", "qa engineer", "test engineer",
    "developer", "programmer", "sdet",
    # architects
    "architect",
    # data science / research ICs
    "data scientist", "research scientist", "applied scientist",
    "research engineer",
    # other non-product functions that surface on AI queries
    "developer relations", "systems builder",
    "recruiter", "talent acquisition", "sales executive",
    "account executive", "customer support",
]

# Roles that LOOK excluded by the words above but are genuinely product /
# strategy work. Checked first, so these always survive.
DEFAULT_TITLE_KEEP = [
    "product manager", "product owner", "product management",
    "product marketing", "product lead", "product director",
    "product strategy", "head of product", "group product manager",
    "principal product", "product consultant",
    "strategy", "consultant", "advisory", "transformation",
    "enablement", "go-to-market", "gtm",
]


def _load_title_filters(search_cfg: dict) -> tuple[list[str], list[str]]:
    """Return (exclude, keep) title patterns, config overriding defaults."""
    exclude = search_cfg.get("title_exclude")
    if exclude is None:
        exclude = list(DEFAULT_TITLE_EXCLUDE)
    exclude = list(exclude) + list(search_cfg.get("title_exclude_extra", []))

    keep = search_cfg.get("title_keep")
    if keep is None:
        keep = list(DEFAULT_TITLE_KEEP)
    keep = list(keep) + list(search_cfg.get("title_keep_extra", []))
    return [e.lower() for e in exclude], [k.lower() for k in keep]


def _title_ok(title: str | None, exclude: list[str], keep: list[str]) -> bool:
    """False when the title is a different job family from the target.

    `keep` wins over `exclude`, so "Technical Product Manager" survives the
    "technical" style patterns and "Product Architect" survives "architect".
    """
    if not title:
        return True  # unknown title -- keep it, let the scorer decide
    t = title.lower()
    if any(k in t for k in keep):
        return True
    return not any(x in t for x in exclude)


# -- DB storage (JobSpy DataFrame -> SQLite) ---------------------------------

def store_jobspy_results(conn: sqlite3.Connection, df, source_label: str,
                         title_exclude: list[str] | None = None,
                         title_keep: list[str] | None = None) -> tuple[int, int]:
    """Store JobSpy DataFrame results into the DB. Returns (new, existing).

    Rows whose title belongs to a different job family are dropped before
    insert, so no downstream stage pays to process them.
    """
    now = datetime.now(timezone.utc).isoformat()
    new = 0
    existing = 0
    skipped_title = 0
    title_exclude = title_exclude if title_exclude is not None else [t.lower() for t in DEFAULT_TITLE_EXCLUDE]
    title_keep = title_keep if title_keep is not None else [t.lower() for t in DEFAULT_TITLE_KEEP]

    for _, row in df.iterrows():
        url = str(row.get("job_url", ""))
        if not url or url == "nan":
            continue

        title = _clean_str(row.get("title"))
        if not _title_ok(title, title_exclude, title_keep):
            skipped_title += 1
            continue
        company = _clean_str(row.get("company"))
        location_str = _clean_str(row.get("location"))

        # Build salary string from min/max
        salary = None
        min_amt = row.get("min_amount")
        max_amt = row.get("max_amount")
        interval = _clean_str(row.get("interval")) or ""
        currency = _clean_str(row.get("currency")) or ""
        if min_amt and str(min_amt) != "nan":
            if max_amt and str(max_amt) != "nan":
                salary = f"{currency}{int(float(min_amt)):,}-{currency}{int(float(max_amt)):,}"
            else:
                salary = f"{currency}{int(float(min_amt)):,}"
            if interval:
                salary += f"/{interval}"

        description = _clean_str(row.get("description"))
        site_name = str(row.get("site", source_label))
        is_remote = row.get("is_remote", False)

        site_label = f"{site_name}"
        if is_remote:
            location_str = f"{location_str} (Remote)" if location_str else "Remote"

        strategy = "jobspy"

        # If JobSpy gave us a full description, promote it directly
        full_description = None
        detail_scraped_at = None
        if description and len(description) > 200:
            full_description = description
            detail_scraped_at = now

        # Extract apply URL if JobSpy provided it.
        # str(None) is the four-character string "None", which passes a
        # != "nan" check and is TRUTHY — so `application_url or url` fallbacks
        # downstream resolved to the literal string "None" and the apply agent
        # was told to navigate to a URL called "None". Normalise properly.
        apply_url = _clean_str(row.get("job_url_direct"))

        try:
            conn.execute(
                "INSERT INTO jobs (url, title, company, salary, description, location, site, strategy, discovered_at, "
                "full_description, application_url, detail_scraped_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (url, title, company, salary, description, location_str, site_label, strategy, now,
                 full_description, apply_url, detail_scraped_at),
            )
            new += 1
        except sqlite3.IntegrityError:
            existing += 1

    conn.commit()
    if skipped_title:
        log.info("Skipped %d listing(s) as wrong job family (title filter)", skipped_title)
    return new, existing


# -- Single search execution -------------------------------------------------

def _run_one_search(
    search: dict,
    sites: list[str],
    results_per_site: int,
    hours_old: int,
    proxy_config: dict | None,
    defaults: dict,
    max_retries: int,
    accept_locs: list[str],
    reject_locs: list[str],
    glassdoor_map: dict,
    title_exclude: list[str] | None = None,
    title_keep: list[str] | None = None,
) -> dict:
    """Run a single search query and store results in DB."""
    s = search
    label = f"\"{s['query']}\" in {s['location']} {'(remote)' if s.get('remote') else ''}"
    if "tier" in s:
        label += f" [tier {s['tier']}]"

    # Split sites: Glassdoor needs simplified location, others use original
    gd_location = glassdoor_map.get(s["location"], s["location"].split(",")[0])
    has_glassdoor = "glassdoor" in sites
    other_sites = [si for si in sites if si != "glassdoor"]

    all_dfs = []

    # Run non-Glassdoor sites with original location
    if other_sites:
        kwargs = {
            "site_name": other_sites,
            "search_term": s["query"],
            "location": s["location"],
            "results_wanted": results_per_site,
            "hours_old": hours_old,
            "description_format": "markdown",
            "country_indeed": defaults.get("country_indeed", "usa"),
            "verbose": 0,
        }
        if s.get("remote"):
            kwargs["is_remote"] = True
        if proxy_config:
            kwargs["proxies"] = [proxy_config["jobspy"]]
        if "linkedin" in other_sites:
            kwargs["linkedin_fetch_description"] = True
        try:
            df = _scrape_with_retry(kwargs, max_retries=max_retries)
            all_dfs.append(df)
        except Exception as e:
            log.error("[%s] (non-gd): %s", label, e)

    # Run Glassdoor separately with simplified location
    if has_glassdoor:
        gd_kwargs = {
            "site_name": ["glassdoor"],
            "search_term": s["query"],
            "location": gd_location,
            "results_wanted": results_per_site,
            "hours_old": hours_old,
            "description_format": "markdown",
            "verbose": 0,
        }
        if s.get("remote"):
            gd_kwargs["is_remote"] = True
        if proxy_config:
            gd_kwargs["proxies"] = [proxy_config["jobspy"]]
        try:
            gd_df = _scrape_with_retry(gd_kwargs, max_retries=max_retries)
            all_dfs.append(gd_df)
        except Exception as e:
            log.error("[%s] (glassdoor): %s", label, e)

    if not all_dfs:
        log.error("[%s]: all sites failed", label)
        return {"new": 0, "existing": 0, "errors": 1, "filtered": 0, "total": 0, "label": label}

    import pandas as pd
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        df = pd.concat(all_dfs, ignore_index=True) if len(all_dfs) > 1 else all_dfs[0]

    if len(df) == 0:
        log.info("[%s] 0 results", label)
        return {"new": 0, "existing": 0, "errors": 0, "filtered": 0, "total": 0, "label": label}

    # Filter by location before storing
    before = len(df)
    df = df[df.apply(lambda row: _location_ok(
        str(row.get("location", "")) if str(row.get("location", "")) != "nan" else None,
        accept_locs, reject_locs,
    ), axis=1)]
    filtered = before - len(df)

    conn = get_connection()
    new, existing = store_jobspy_results(conn, df, s["query"],
                                         title_exclude, title_keep)

    msg = f"[{label}] {before} results -> {new} new, {existing} dupes"
    if filtered:
        msg += f", {filtered} filtered (location)"
    log.info(msg)

    return {"new": new, "existing": existing, "errors": 0, "filtered": filtered, "total": before, "label": label}


# -- Single query search -----------------------------------------------------

def search_jobs(
    query: str,
    location: str,
    sites: list[str] | None = None,
    remote_only: bool = False,
    results_per_site: int = 50,
    hours_old: int = 72,
    proxy: str | None = None,
    country_indeed: str = "usa",
) -> dict:
    """Run a single job search via JobSpy and store results in DB."""
    if sites is None:
        sites = ["indeed", "linkedin", "zip_recruiter"]

    proxy_config = parse_proxy(proxy) if proxy else None

    log.info("Search: \"%s\" in %s | sites=%s | remote=%s", query, location, sites, remote_only)

    kwargs = {
        "site_name": sites,
        "search_term": query,
        "location": location,
        "results_wanted": results_per_site,
        "hours_old": hours_old,
        "description_format": "markdown",
        "country_indeed": country_indeed,
        "verbose": 2,
    }

    if remote_only:
        kwargs["is_remote"] = True

    if proxy_config:
        kwargs["proxies"] = [proxy_config["jobspy"]]

    if "linkedin" in sites:
        kwargs["linkedin_fetch_description"] = True

    try:
        df = scrape_jobs(**kwargs)
    except Exception as e:
        log.error("JobSpy search failed: %s", e)
        return {"error": str(e), "total": 0, "new": 0, "existing": 0}

    total = len(df)
    log.info("JobSpy returned %d results", total)

    if total == 0:
        return {"total": 0, "new": 0, "existing": 0}

    if "site" in df.columns:
        site_counts = df["site"].value_counts()
        for site, count in site_counts.items():
            log.info("  %s: %d", site, count)

    conn = init_db()
    new, existing = store_jobspy_results(conn, df, query)
    log.info("Stored: %d new, %d already in DB", new, existing)

    db_total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM jobs WHERE detail_scraped_at IS NULL").fetchone()[0]
    log.info("DB total: %d jobs, %d pending detail scrape", db_total, pending)

    return {"total": total, "new": new, "existing": existing}


def _filter_locations(locs: list[dict], wanted: list[str]) -> list[dict]:
    """Narrow configured locations to those the user asked for.

    Matches case-insensitively against a location's `label` or its
    `location` string, in either direction — so "Dubai" selects the entry
    "Dubai, United Arab Emirates", and "Dubai, United Arab Emirates"
    selects an entry labelled "dubai".
    """
    targets = [w.strip().lower() for w in wanted if w and w.strip()]
    if not targets:
        return locs

    selected = []
    for loc in locs:
        candidates = [str(loc.get("label", "")).lower(),
                      str(loc.get("location", "")).lower()]
        for target in targets:
            if any(c and (target in c or c in target) for c in candidates):
                selected.append(loc)
                break
    return selected


# -- Full crawl (all queries x all locations) --------------------------------

def _full_crawl(
    search_cfg: dict,
    tiers: list[int] | None = None,
    locations: list[str] | None = None,
    sites: list[str] | None = None,
    results_per_site: int = 100,
    hours_old: int = 72,
    proxy: str | None = None,
    max_retries: int = 2,
) -> dict:
    """Run all search queries from search config across all locations."""
    if sites is None:
        sites = ["indeed", "linkedin", "zip_recruiter"]

    # Build search combinations from config
    queries = search_cfg.get("queries", [])
    locs = search_cfg.get("locations", [])
    defaults = search_cfg.get("defaults", {})
    glassdoor_map = search_cfg.get("glassdoor_location_map", {})
    accept_locs, reject_locs = _load_location_config(search_cfg)
    title_exclude, title_keep = _load_title_filters(search_cfg)

    if tiers:
        queries = [q for q in queries if q.get("tier") in tiers]
    if locations:
        locs = _filter_locations(locs, locations)
        if not locs:
            log.warning(
                "No configured location matched %s. Available: %s",
                locations,
                ", ".join(l.get("label") or l.get("location", "") for l in
                          search_cfg.get("locations", [])),
            )
            return {"new": 0, "existing": 0, "errors": 0, "db_total": 0, "queries": 0}

    searches = []
    for q in queries:
        for loc in locs:
            searches.append({
                "query": q["query"],
                "location": loc["location"],
                "remote": loc.get("remote", False),
                "tier": q.get("tier", 0),
            })

    proxy_config = parse_proxy(proxy) if proxy else None

    log.info("Full crawl: %d search combinations", len(searches))
    log.info("Sites: %s | Results/site: %d | Hours old: %d",
             ", ".join(sites), results_per_site, hours_old)

    # Ensure DB schema is ready
    init_db()

    total_new = 0
    total_existing = 0
    total_errors = 0
    completed = 0

    for s in searches:
        result = _run_one_search(
            s, sites, results_per_site, hours_old,
            proxy_config, defaults, max_retries,
            accept_locs, reject_locs, glassdoor_map,
            title_exclude, title_keep,
        )
        completed += 1
        total_new += result["new"]
        total_existing += result["existing"]
        total_errors += result["errors"]

        if completed % 5 == 0 or completed == len(searches):
            log.info("Progress: %d/%d queries done (%d new, %d dupes, %d errors)",
                     completed, len(searches), total_new, total_existing, total_errors)

    # Final stats
    conn = get_connection()
    db_total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    log.info("Full crawl complete: %d new | %d dupes | %d errors | %d total in DB",
             total_new, total_existing, total_errors, db_total)

    return {
        "new": total_new,
        "existing": total_existing,
        "errors": total_errors,
        "db_total": db_total,
        "queries": len(searches),
    }


# -- Public entry point ------------------------------------------------------

def run_discovery(cfg: dict | None = None,
                  locations: list[str] | None = None) -> dict:
    """Main entry point for JobSpy-based job discovery.

    Loads search queries and locations from the user's search config YAML,
    then runs a full crawl across all configured job boards.

    Args:
        cfg: Override the search configuration dict. If None, loads from
             the user's searches.yaml file.

    Returns:
        Dict with stats: new, existing, errors, db_total, queries.
    """
    if cfg is None:
        cfg = config.load_search_config()

    if not cfg:
        log.warning("No search configuration found. Run `applypilot init` to create one.")
        return {"new": 0, "existing": 0, "errors": 0, "db_total": 0, "queries": 0}

    proxy = cfg.get("proxy")
    sites = cfg.get("sites")
    results_per_site = cfg.get("defaults", {}).get("results_per_site", 100)
    hours_old = cfg.get("defaults", {}).get("hours_old", 72)
    tiers = cfg.get("tiers")
    if locations is None:
        locations = cfg.get("location_labels")

    return _full_crawl(
        search_cfg=cfg,
        tiers=tiers,
        locations=locations,
        sites=sites,
        results_per_site=results_per_site,
        hours_old=hours_old,
        proxy=proxy,
    )
