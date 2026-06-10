"""
Unit tests for the job scoring engine (scorer.py).

Every component of the score is asserted via job["score_breakdown"] so each
test pins down one scoring rule without depending on the others:
  - Title matching (exact / partial / search-query / adjacent / senior penalty)
  - Skills keyword scoring and its 30-point cap
  - Experience level detection (junior indicators, years regex, no mention)
  - Salary range overlap and boundary cases
  - Location scoring (remote / near home / commutable)
  - Negative keyword penalty and its -20 cap
  - Education filter (diploma / strict degree / soft degree / mention / none)
  - Score clamping to 0-100, rank_jobs ordering, filter_jobs threshold

All tests pass an explicit cfg so they are independent of the global
SEARCH_CONFIG defaults.
"""
from scorer import filter_jobs, rank_jobs, score_job


# ── Helpers ─────────────────────────────────────────────────────────────────────

def make_cfg(**overrides) -> dict:
    cfg = {
        "target_titles": ["data analyst", "it support engineer"],
        "preferred_keywords": [
            "python", "sql", "excel", "tableau", "pandas", "reporting",
            "dashboards", "statistics", "etl", "automation", "analytics",
        ],
        "negative_keywords": [
            "commission only", "door-to-door", "mlm", "cold calling", "insurance sales",
        ],
        "min_salary": 2200,
        "max_salary": 4000,
        "location_keywords": ["remote", "sengkang", "tampines"],
        "min_score_threshold": 30,
    }
    cfg.update(overrides)
    return cfg


def make_job(**overrides) -> dict:
    job = {
        "title": "",
        "company": "TestCo",
        "description": "",
        "location": "",
        "url": "https://example.com/job/1",
        "source": "mcf",
    }
    job.update(overrides)
    return job


def score(cfg=None, **job_fields) -> dict:
    return score_job(make_job(**job_fields), cfg or make_cfg())


# ── Title matching ──────────────────────────────────────────────────────────────

def test_exact_title_match_scores_30():
    result = score(title="Data Analyst")
    assert result["score_breakdown"]["title"] == 30
    assert any("Title match" in r for r in result["match_reasons"])


def test_senior_title_penalty_overrides_exact_match():
    result = score(title="Senior Data Analyst")
    assert result["score_breakdown"]["title"] == -15
    assert any("Senior/Lead" in r for r in result["match_reasons"])


def test_manager_title_is_penalised():
    result = score(title="Data Analytics Manager")
    assert result["score_breakdown"]["title"] == -15


def test_partial_title_match_scores_15():
    # "analyst" appears in the title but no full target title does
    result = score(title="Business Analyst")
    assert result["score_breakdown"]["title"] == 15
    assert any("Partial title match" in r for r in result["match_reasons"])


def test_search_query_match_scores_15():
    # Title has no overlap with targets, but the job came from a targeted search
    result = score(title="Specialist", search_query="data analyst jobs singapore")
    assert result["score_breakdown"]["title"] == 15
    assert any("Found via" in r for r in result["match_reasons"])


def test_adjacent_role_with_mode_keyword_scores_10():
    result = score(title="Marketing Coordinator", description="uses python daily")
    assert result["score_breakdown"]["title"] == 10
    assert any("Adjacent role" in r for r in result["match_reasons"])


def test_unrelated_title_scores_0():
    result = score(title="Warehouse Picker")
    assert result["score_breakdown"]["title"] == 0


# ── Skills matching ─────────────────────────────────────────────────────────────

def test_skills_score_3_points_per_keyword():
    result = score(title="Warehouse Picker", description="uses python and sql daily")
    assert result["score_breakdown"]["skills"] == 6
    assert any("Skills match" in r for r in result["match_reasons"])


def test_skills_score_capped_at_30():
    # All 11 configured keywords present: 33 raw points, capped at 30
    all_skills = ", ".join(make_cfg()["preferred_keywords"])
    result = score(title="Warehouse Picker", description=f"needs {all_skills}")
    assert result["score_breakdown"]["skills"] == 30


def test_no_skills_scores_0():
    result = score(title="Warehouse Picker", description="general labour work")
    assert result["score_breakdown"]["skills"] == 0


# ── Experience level ────────────────────────────────────────────────────────────

def test_junior_indicator_scores_15():
    result = score(title="Warehouse Picker", description="fresh graduate welcome")
    assert result["score_breakdown"]["experience"] == 15


def test_two_years_required_scores_12():
    result = score(title="Warehouse Picker", description="needs 2 years of experience")
    assert result["score_breakdown"]["experience"] == 12


def test_three_years_required_is_a_stretch():
    result = score(title="Warehouse Picker", description="needs 3 years of experience")
    assert result["score_breakdown"]["experience"] == 5


def test_five_years_required_penalised():
    result = score(title="Warehouse Picker", description="minimum 5 years of experience")
    assert result["score_breakdown"]["experience"] == -10


def test_multiple_year_mentions_uses_maximum():
    result = score(
        title="Warehouse Picker",
        description="2 years in support roles plus 6 years overall experience",
    )
    assert result["score_breakdown"]["experience"] == -10


def test_no_experience_mention_scores_8():
    result = score(title="Warehouse Picker", description="general labour work")
    assert result["score_breakdown"]["experience"] == 8


# ── Salary ──────────────────────────────────────────────────────────────────────

def test_salary_overlapping_range_scores_10():
    result = score(title="Warehouse Picker", salary_min=3000, salary_max=5000)
    assert result["score_breakdown"]["salary"] == 10


def test_salary_below_range_penalised():
    result = score(title="Warehouse Picker", salary_min=1000, salary_max=2000)
    assert result["score_breakdown"]["salary"] == -5


def test_salary_far_above_range_scores_3():
    result = score(title="Warehouse Picker", salary_min=5500, salary_max=7000)
    assert result["score_breakdown"]["salary"] == 3


def test_salary_in_dead_zone_above_range_scores_0():
    # min above max_salary but within the +1000 buffer: no rule fires
    result = score(title="Warehouse Picker", salary_min=4200, salary_max=4800)
    assert result["score_breakdown"]["salary"] == 0


def test_unknown_salary_is_neutral_5():
    result = score(title="Warehouse Picker")
    assert result["score_breakdown"]["salary"] == 5


def test_single_salary_bound_treated_as_unknown():
    result = score(title="Warehouse Picker", salary_min=3000, salary_max=None)
    assert result["score_breakdown"]["salary"] == 5


# ── Location ────────────────────────────────────────────────────────────────────

def test_remote_location_scores_10():
    result = score(title="Warehouse Picker", description="fully remote work")
    assert result["score_breakdown"]["location"] == 10
    assert any("Remote/hybrid" in r for r in result["match_reasons"])


def test_near_home_location_scores_10():
    result = score(title="Warehouse Picker", location="Sengkang")
    assert result["score_breakdown"]["location"] == 10
    assert any("Near home" in r for r in result["match_reasons"])


def test_commutable_location_scores_6():
    result = score(title="Warehouse Picker", location="Tampines")
    assert result["score_breakdown"]["location"] == 6
    assert any("Reasonable commute" in r for r in result["match_reasons"])


def test_no_location_match_scores_0():
    result = score(title="Warehouse Picker", location="Tuas")
    assert result["score_breakdown"]["location"] == 0


# ── Negative keywords ───────────────────────────────────────────────────────────

def test_one_negative_keyword_costs_5():
    result = score(title="Warehouse Picker", description="commission only pay")
    assert result["score_breakdown"]["negative"] == -5
    assert any("Red flags" in r for r in result["match_reasons"])


def test_negative_penalty_capped_at_minus_20():
    # All 5 configured negatives present: -25 raw, capped at -20
    all_negs = ", ".join(make_cfg()["negative_keywords"])
    result = score(title="Warehouse Picker", description=all_negs)
    assert result["score_breakdown"]["negative"] == -20


# ── Education filter ────────────────────────────────────────────────────────────

def test_accepts_diploma_scores_plus_5():
    result = score(title="Warehouse Picker", description="diploma holders welcome")
    assert result["score_breakdown"]["education"] == 5


def test_diploma_wins_over_degree_mention():
    result = score(title="Warehouse Picker", description="degree or diploma accepted")
    assert result["score_breakdown"]["education"] == 5


def test_strict_degree_requirement_scores_minus_40():
    result = score(title="Warehouse Picker", description="candidates must have a degree")
    assert result["score_breakdown"]["education"] == -40


def test_soft_degree_requirement_scores_minus_15():
    result = score(title="Warehouse Picker", description="degree required for this role")
    assert result["score_breakdown"]["education"] == -15


def test_bare_degree_mention_scores_minus_8():
    result = score(title="Warehouse Picker", description="a degree would be a plus")
    assert result["score_breakdown"]["education"] == -8


def test_ite_as_word_counts_as_accepting_diploma():
    result = score(title="Warehouse Picker", description="open to ite graduates")
    assert result["score_breakdown"]["education"] == 5


def test_onsite_and_website_are_not_ite_matches():
    # Regression: "ite" was matched as a bare substring, so "onsite",
    # "website", "suite" etc. wrongly earned the diploma bonus
    result = score(title="Warehouse Picker", description="onsite role, apply via our website")
    assert result["score_breakdown"]["education"] == 3


def test_no_education_mention_scores_plus_3():
    result = score(title="Warehouse Picker", description="general labour work")
    assert result["score_breakdown"]["education"] == 3


# ── Total score behaviour ───────────────────────────────────────────────────────

def test_score_clamped_at_zero():
    result = score(
        title="Senior Data Analyst Manager",
        description=(
            "minimum 8 years of experience, must have a degree. "
            "commission only, door-to-door, mlm, cold calling, insurance sales"
        ),
    )
    # Raw total is -80; clamped to the floor
    assert result["score"] == 0


def test_perfect_job_scores_100():
    result = score(
        title="Data Analyst",
        description=(
            "Fresh graduate welcome. Skills: python, sql, excel, tableau, pandas, "
            "reporting, dashboards, statistics, etl, automation. "
            "Remote work. Diploma holders welcome."
        ),
        salary_min=2500,
        salary_max=3500,
    )
    assert result["score"] == 100


def test_neutral_job_gets_baseline_score():
    # No signals at all: 8 (experience) + 5 (salary) + 3 (education) = 16
    result = score(title="Underwater Basket Weaver")
    assert result["score"] == 16


def test_result_contains_breakdown_and_reasons():
    result = score(title="Data Analyst", description="python")
    assert set(result["score_breakdown"]) == {
        "title", "skills", "experience", "salary", "location", "negative", "education",
    }
    assert result["match_reasons"]
    assert 0 <= result["score"] <= 100


# ── rank_jobs / filter_jobs ─────────────────────────────────────────────────────

def test_rank_jobs_orders_by_score_descending():
    cfg = make_cfg()
    jobs = [
        make_job(title="Warehouse Picker"),
        make_job(title="Data Analyst", description="python, sql, diploma welcome"),
        make_job(title="Business Analyst"),
    ]
    ranked = rank_jobs(jobs, cfg)
    scores = [j["score"] for j in ranked]
    assert scores == sorted(scores, reverse=True)
    assert ranked[0]["title"] == "Data Analyst"


def test_filter_jobs_threshold_is_inclusive():
    jobs = [{"score": 30}, {"score": 29}, {"score": 31}]
    kept = filter_jobs(jobs, min_score=30)
    assert [j["score"] for j in kept] == [30, 31]
