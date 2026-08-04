"""Regression tests for silent-failure bugs.

Every case here comes from a bug that reported success while producing
nothing usable — the pipeline looked healthy while dropping data. They are
grouped by the module that owned the defect.
"""

from datetime import timedelta
from typing import ClassVar

import pytest

from applypilot.apply.launcher import _already_applied_elsewhere
from applypilot.apply.prompt import _relocation_line, _sponsorship_line, resolve_region
from applypilot.discovery.jobspy import (
    DEFAULT_TITLE_EXCLUDE,
    DEFAULT_TITLE_KEEP,
    _clean_str,
    _filter_locations,
    _location_ok,
    _title_ok,
)
from applypilot.scoring.scorer import _parse_score_response
from applypilot.scoring.validator import _as_school_list, validate_json_fields

# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

class TestLocationFilter:
    """An empty accept list meant "reject everything", discarding every
    non-remote job: a crawl scraped hundreds of jobs and stored none."""

    def test_empty_accept_list_accepts_everything(self):
        assert _location_ok("Berlin, Germany", [], []) is True
        assert _location_ok("Austin, TX", [], []) is True

    def test_remote_always_accepted(self):
        assert _location_ok("Remote", [], []) is True

    def test_configured_allowlist_still_filters(self):
        assert _location_ok("Berlin, Germany", ["Berlin"], []) is True
        assert _location_ok("Paris, France", ["Berlin"], []) is False

    def test_rejectlist_wins(self):
        assert _location_ok("Berlin, Germany", [], ["Berlin"]) is False

    def test_unknown_location_kept_for_scorer(self):
        assert _location_ok(None, ["Berlin"], []) is True


class TestCleanStr:
    """str(None) produces the truthy string "None", which passed a
    != "nan" check and was stored in application_url. Downstream
    `application_url or url` then resolved to "None" and the apply agent was
    told to navigate to a URL literally called "None"."""

    @pytest.mark.parametrize("value", [None, "nan", "None", "null", "", "   ", "<NA>"])
    def test_nullish_values_become_none(self, value):
        assert _clean_str(value) is None

    def test_real_value_survives(self):
        assert _clean_str("https://example.com/job/1") == "https://example.com/job/1"

    def test_fallback_chain_works(self):
        """The actual downstream expression that broke."""
        job = {"application_url": _clean_str(None), "url": "https://real.example/job"}
        assert (job.get("application_url") or job["url"]) == "https://real.example/job"


class TestTitleFilter:
    """Job boards return engineering roles for product queries because the
    descriptions mention the same tools. Scoring them costs an LLM call to
    reach a foregone conclusion."""

    EXCLUDE: ClassVar[list[str]] = [t.lower() for t in DEFAULT_TITLE_EXCLUDE]
    KEEP: ClassVar[list[str]] = [t.lower() for t in DEFAULT_TITLE_KEEP]

    @pytest.mark.parametrize("title", [
        "Data Platform Engineer",
        "AI Infrastructure Architect",
        "Large Language Model Architect",
        "Custom Software Engineer",
        "Business Architect",
        "Senior Developer Relations Manager",
    ])
    def test_engineering_titles_dropped(self, title):
        assert _title_ok(title, self.EXCLUDE, self.KEEP) is False

    @pytest.mark.parametrize("title", [
        "AI Product Manager",
        "Senior Technical Product Manager",
        "Group Product Manager",
        "Product Owner",
        "Product Marketing Manager",
        "AI Consultant",
        "AI Transformation Lead",
        "Head of Product",
        "Product Strategy Manager",
    ])
    def test_product_and_strategy_titles_kept(self, title):
        assert _title_ok(title, self.EXCLUDE, self.KEEP) is True

    def test_keep_beats_exclude(self):
        """"Product Architect" must survive the "architect" exclusion."""
        assert _title_ok("Product Architect", ["architect"], ["product"]) is True

    def test_unknown_title_kept(self):
        assert _title_ok(None, self.EXCLUDE, self.KEEP) is True


class TestLocationSelection:
    """--location matched a `label` key that discovery never wrote."""

    LOCS: ClassVar[list[dict]] = [
        {"label": "Remote", "location": "Remote"},
        {"label": "Germany", "location": "Germany"},
        {"label": "Berlin, Germany", "location": "Berlin, Germany"},
        {"label": "Austin, United States", "location": "Austin, United States"},
    ]

    def test_partial_match_case_insensitive(self):
        got = [c["label"] for c in _filter_locations(self.LOCS, ["austin"])]
        assert got == ["Austin, United States"]

    def test_broad_term_matches_all_children(self):
        got = [c["label"] for c in _filter_locations(self.LOCS, ["Germany"])]
        assert got == ["Germany", "Berlin, Germany"]

    def test_no_match_returns_empty(self):
        assert _filter_locations(self.LOCS, ["Atlantis"]) == []

    def test_empty_request_returns_all(self):
        assert len(_filter_locations(self.LOCS, [])) == len(self.LOCS)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

class TestScoreParsing:
    """A parse failure defaulted to score=0, which the prompt defines as
    "not a real job posting" — 10 genuine Product Manager roles were
    permanently written off as spam."""

    def test_wellformed_response(self):
        r = _parse_score_response("SCORE: 8\nKEYWORDS: a, b\nREASONING: good fit")
        assert r["score"] == 8
        assert r["reasoning"] == "good fit"

    def test_unparseable_returns_none_not_zero(self):
        r = _parse_score_response("I think this is a reasonable match.")
        assert r["score"] is None, "must be None so the job is retried, not marked spam"

    def test_empty_returns_none(self):
        assert _parse_score_response("")["score"] is None

    def test_json_fallback(self):
        """Models answer with JSON despite the requested format."""
        r = _parse_score_response('```json\n{"score": 7, "reasoning": "solid"}\n```')
        assert r["score"] == 7
        assert r["reasoning"] == "solid"

    def test_json_alternate_keys(self):
        r = _parse_score_response('{"fit_rating": 9, "rationale": "strong"}')
        assert r["score"] == 9

    def test_out_of_range_clamped(self):
        assert _parse_score_response("SCORE: 99\nREASONING: x")["score"] == 10

    def test_zero_is_preserved(self):
        """0 is meaningful: "not a real job posting"."""
        assert _parse_score_response("SCORE: 0\nREASONING: advert")["score"] == 0


# ---------------------------------------------------------------------------
# Tailoring / validation
# ---------------------------------------------------------------------------

class TestPreservedSchool:
    """preserved_school was a single string checked as one literal, so two
    schools could never both match and every tailored resume hard-failed."""

    def test_comma_string_is_split(self):
        assert _as_school_list("State University, Tech Institute") == [
            "State University", "Tech Institute"]

    def test_list_passes_through(self):
        assert _as_school_list(["A", "B"]) == ["A", "B"]

    def test_empty_is_empty(self):
        assert _as_school_list("") == []
        assert _as_school_list(None) == []


class TestPreservedCompanies:
    """The validator checked only the `header` field. Models legitimately put
    the role in `header` and the employer in `subtitle`, so correct resumes
    were rejected — approvals went from 2/9 to 15/16 once fixed."""

    PROFILE: ClassVar[dict] = {
        "resume_facts": {
            "preserved_companies": ["Acme Corp", "Globex"],
            "preserved_school": ["State University"],
            "preserved_projects": [],
        },
        "personal": {"full_name": "Test User", "email": "t@example.com", "phone": "+1"},
        "skills_boundary": {},
    }
    BASE: ClassVar[dict] = {
        "title": "PM",
        "summary": "s",
        "skills": {"Languages": "Python"},
        "education": "State University, MBA",
        "projects": [{"header": "Proj", "bullets": ["did it"]}],
    }

    def _company_errors(self, experience):
        data = dict(self.BASE, experience=experience)
        result = validate_json_fields(data, self.PROFILE, mode="lenient")
        return [e for e in result["errors"] if "Company" in e]

    def test_company_in_subtitle_accepted(self):
        assert self._company_errors([
            {"header": "Product Manager", "subtitle": "Acme Corp | NY", "bullets": ["a"]},
            {"header": "Founder", "subtitle": "Globex | SF", "bullets": ["a"]},
        ]) == []

    def test_company_in_header_accepted(self):
        assert self._company_errors([
            {"header": "Acme Corp — PM", "bullets": ["a"]},
            {"header": "Globex — Founder", "bullets": ["a"]},
        ]) == []

    def test_genuinely_dropped_employer_still_fails(self):
        errors = self._company_errors([
            {"header": "PM", "subtitle": "Acme Corp", "bullets": ["a"]},
        ])
        assert len(errors) == 1
        assert "Globex" in errors[0]


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

class TestRegionalSalary:
    """Salary was hardcoded with a "$" prefix regardless of currency and a
    "$110K" senior floor meaningless outside USD. Rates do not convert
    across markets."""

    COMP: ClassVar[dict] = {
        "salary_currency": "INR",
        "salary_expectation": "1000000",
        "by_region": {
            "Home": {"currency": "INR", "expectation": "1000000", "min": "1000000",
                      "max": "1200000", "match": ["germany", "berlin"]},
            "Foreign": {"currency": "AED", "expectation": "240000", "min": "240000",
                    "max": "288000", "quote_monthly": True,
                    "monthly_min": "20000", "monthly_max": "24000",
                    "match": ["austin", "united states"]},
            "Remote (global)": {"currency": "USD", "expectation": "120000",
                                "min": "100000", "max": "150000", "match": ["remote"]},
        },
        "default_region": "Remote (global)",
    }

    def test_each_market_uses_its_own_band(self):
        assert resolve_region(self.COMP, "Austin, United States")[1]["currency"] == "AED"
        assert resolve_region(self.COMP, "Berlin, Germany")[1]["currency"] == "INR"

    def test_no_cross_currency_conversion(self):
        """A foreign market must not be the home figure at spot FX."""
        _, uae = resolve_region(self.COMP, "Austin")
        assert uae["expectation"] == "240000"

    def test_more_specific_market_wins_over_remote(self):
        """"Remote - Bengaluru, India" is an India role, not global-remote."""
        name, _ = resolve_region(self.COMP, "Remote - Berlin, Germany")
        assert name == "Home"

    def test_unmatched_falls_back_to_default_region(self):
        name, band = resolve_region(self.COMP, "Reykjavik, Iceland")
        assert name == "Remote (global)"
        assert band["currency"] == "USD"

    def test_profile_without_by_region_still_works(self):
        name, band = resolve_region({"salary_currency": "USD",
                                     "salary_expectation": "100000"}, "anywhere")
        assert name == "default"
        assert band["currency"] == "USD"


class TestRelocationAndSponsorship:
    """The prompt hardcoded "cannot relocate", blocking every overseas role.
    Sponsorship was a single boolean, which cannot be honest across
    countries: a citizen needs none at home and does need one abroad."""

    def test_willing_to_relocate_is_stated(self):
        line = _relocation_line({"relocation": {
            "willing_to_relocate": True,
            "target_locations": ["Berlin", "Austin"],
        }}, "Home City")
        assert "WILLING TO RELOCATE" in line
        assert "Berlin" in line

    def test_not_willing_is_respected(self):
        line = _relocation_line({"relocation": {"willing_to_relocate": False}}, "Home City")
        assert "cannot relocate" in line

    def test_missing_relocation_block_defaults_to_cannot(self):
        assert "cannot relocate" in _relocation_line({}, "Home City")

    def test_sponsorship_is_country_dependent(self):
        line = _sponsorship_line({"work_authorization": {
            "no_sponsorship_needed_in": ["Homeland"],
            "work_permit_type": "Citizen",
        }})
        assert "Homeland" in line
        assert "YES" in line, "must answer YES truthfully outside authorized countries"


class TestDuplicateApplications:
    """Employers bulk-post one requisition across many city URLs — 20 URLs
    for a single req in observed data. The url PRIMARY KEY cannot see these."""

    class _FakeConn:
        def __init__(self, applied):
            self._applied = applied

        def execute(self, _sql, params):
            company, title, url = params
            match = next((a for a in self._applied
                          if a["company"] == company and a["title"] == title
                          and a["url"] != url), None)
            return _Result(match)

    def test_same_company_and_title_blocked(self):
        conn = self._FakeConn([
            {"company": "acme corp", "title": "product owner", "url": "u1"}])
        job = {"company": "Acme Corp", "title": "Product Owner", "url": "u2"}
        assert _already_applied_elsewhere(conn, job) == "u1"

    def test_same_title_different_company_allowed(self):
        """Different employers commonly post identically-titled roles."""
        conn = self._FakeConn([
            {"company": "alpha inc", "title": "product manager", "url": "u1"}])
        job = {"company": "Beta Ltd", "title": "Product Manager", "url": "u2"}
        assert _already_applied_elsewhere(conn, job) is None

    def test_missing_company_does_not_block(self):
        """Without a company, equivalence cannot be established."""
        conn = self._FakeConn([
            {"company": "acme", "title": "product manager", "url": "u1"}])
        assert _already_applied_elsewhere(
            conn, {"company": "", "title": "Product Manager", "url": "u2"}) is None


class _Result:
    """Minimal stand-in for a sqlite3 cursor result."""

    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


# ---------------------------------------------------------------------------
# Key expiry
# ---------------------------------------------------------------------------

class TestKeyExpiry:
    """Keys are deleted after a TTL; the key value must never be written to
    the metadata file."""

    @pytest.fixture(autouse=True)
    def _isolated_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
        import importlib

        from applypilot import config, keyexpiry
        importlib.reload(config)
        importlib.reload(keyexpiry)
        self.config = config
        self.keyexpiry = keyexpiry
        config.APP_DIR.mkdir(parents=True, exist_ok=True)

    def test_first_sighting_is_adopted_not_expired(self):
        self.config.ENV_PATH.write_text("GEMINI_API_KEY=abc123\n")
        self.config.load_env()
        assert self.keyexpiry.enforce() == []
        assert self.keyexpiry.days_remaining("GEMINI_API_KEY") == self.keyexpiry.ttl_days()

    def test_key_past_ttl_is_scrubbed(self, monkeypatch):
        self.config.ENV_PATH.write_text("GEMINI_API_KEY=abc123\nLLM_MODEL=keep-me\n")
        monkeypatch.setenv("GEMINI_API_KEY", "abc123")
        stale = self.keyexpiry._today() - timedelta(days=self.keyexpiry.ttl_days() + 1)
        self.keyexpiry.stamp("GEMINI_API_KEY", "abc123", when=stale)
        assert self.keyexpiry.enforce() == ["GEMINI_API_KEY"]
        text = self.config.ENV_PATH.read_text()
        assert "abc123" not in text
        assert "LLM_MODEL=keep-me" in text, "unrelated settings must survive"

    def test_rotation_restarts_the_clock(self, monkeypatch):
        old = self.keyexpiry._today() - timedelta(days=self.keyexpiry.ttl_days() - 1)
        self.keyexpiry.stamp("GEMINI_API_KEY", "old-value", when=old)
        monkeypatch.setenv("GEMINI_API_KEY", "brand-new-value")
        self.keyexpiry.enforce()
        assert self.keyexpiry.days_remaining("GEMINI_API_KEY") == self.keyexpiry.ttl_days()

    def test_metadata_never_contains_the_secret(self):
        self.keyexpiry.stamp("GEMINI_API_KEY", "super-secret-value")
        assert "super-secret-value" not in self.keyexpiry.META_PATH.read_text()

    def test_local_url_is_never_expired(self, monkeypatch):
        monkeypatch.setenv("LLM_URL", "http://127.0.0.1:1234/v1")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        assert self.keyexpiry.enforce() == []
