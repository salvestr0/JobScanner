"""
Job Scoring Engine
Scores each job listing against Jayden's profile.
Returns a score from 0-100 indicating how well the job matches.
"""

import re
from config import SEARCH_CONFIG


def score_job(job: dict, cfg: dict = None) -> dict:
    """
    Score a job listing against the user profile.

    Args:
        job: dict with keys like 'title', 'company', 'description',
             'salary_min', 'salary_max', 'location', 'url', 'source'
        cfg: optional config snapshot; falls back to global SEARCH_CONFIG

    Returns:
        dict with original job data + 'score', 'score_breakdown', 'match_reasons'
    """
    c = cfg if cfg is not None else SEARCH_CONFIG

    title = job.get("title", "").lower()
    description = job.get("description", "").lower()
    location = job.get("location", "").lower()
    search_query = job.get("search_query", "").lower()
    full_text = f"{title} {description} {location} {search_query}"

    score = 0
    breakdown = {}
    reasons = []

    # 1. TITLE MATCH (0-30 points)
    title_score = 0

    # First check for SENIOR/LEAD in title — instant penalty
    senior_title_words = ["senior", "lead ", "lead,", "principal", "head of", "manager", "director", "vp "]
    is_senior_title = any(word in title for word in senior_title_words)

    if is_senior_title:
        title_score = -15
        reasons.append("🚫 Senior/Lead role (not entry-level)")
    else:
        for target in c["target_titles"]:
            if target.lower() in title:
                title_score = 30
                reasons.append(f"Title match: {target}")
                break
        # Partial match — any significant word from the current target titles appears in job title
        _skip_words = {"and", "or", "of", "the", "a", "an", "in", "for", "at", "to"}
        if title_score == 0:
            for target in c["target_titles"]:
                for word in target.lower().split():
                    if word not in _skip_words and len(word) > 3 and word in title:
                        title_score = 15
                        reasons.append(f"Partial title match: {word}")
                        break
                if title_score != 0:
                    break
        # Job was returned by a targeted search (helps sources with sparse descriptions)
        if title_score == 0 and search_query:
            for target in c["target_titles"]:
                if target.lower() in search_query:
                    title_score = 15
                    reasons.append(f"Found via '{target}' search")
                    break
        # Adjacent roles — coordinator/associate/officer/support roles that match mode keywords
        if title_score == 0:
            adjacent = ["associate", "coordinator", "executive", "support", "officer", "assistant"]
            mode_keywords = c.get("preferred_keywords", [])[:10]
            for adj in adjacent:
                if adj in title and any(k in full_text for k in mode_keywords):
                    title_score = 10
                    reasons.append("Adjacent role matching search criteria")
                    break
    breakdown["title"] = title_score
    score += title_score

    # 2. SKILLS MATCH (0-30 points)
    skills_score = 0
    matched_skills = []
    for keyword in c["preferred_keywords"]:
        if keyword in full_text:
            skills_score += 3
            matched_skills.append(keyword)
    skills_score = min(skills_score, 30)  # Cap at 30
    if matched_skills:
        reasons.append(f"Skills match: {', '.join(matched_skills[:5])}")
    breakdown["skills"] = skills_score
    score += skills_score

    # 3. EXPERIENCE LEVEL (0-15 points)
    exp_score = 0
    junior_indicators = [
        "junior", "entry level", "entry-level", "fresh grad",
        "0-1 year", "0-2 year", "1-2 year", "no experience",
        "willing to train", "training provided", "internship",
        "fresh graduate", "recent graduate",
    ]
    for indicator in junior_indicators:
        if indicator in full_text:
            exp_score = 15
            reasons.append(f"Entry-level friendly: '{indicator}'")
            break
    # If no explicit mention but also no high experience requirement
    if exp_score == 0:
        high_exp = re.findall(r'(\d+)\+?\s*(?:years?|yrs?)', full_text)
        if high_exp:
            max_exp = max(int(x) for x in high_exp)
            if max_exp <= 2:
                exp_score = 12
                reasons.append(f"Requires ≤{max_exp} years experience")
            elif max_exp <= 3:
                exp_score = 5
                reasons.append(f"Requires {max_exp} years (stretch)")
            else:
                exp_score = -10
                reasons.append(f"Requires {max_exp}+ years (too senior)")
        else:
            exp_score = 8  # No mention = probably okay
    breakdown["experience"] = exp_score
    score += exp_score

    # 4. SALARY MATCH (0-10 points)
    salary_score = 0
    sal_min = job.get("salary_min")
    sal_max = job.get("salary_max")
    if sal_min is not None and sal_max is not None:
        if sal_min <= c["max_salary"] and sal_max >= c["min_salary"]:
            salary_score = 10
            reasons.append(f"Salary in range: ${sal_min}-${sal_max}")
        elif sal_max < c["min_salary"]:
            salary_score = -5
            reasons.append(f"Salary below range: ${sal_max}")
        elif sal_min > c["max_salary"] + 1000:
            salary_score = 3  # Higher salary = probably more senior, but worth a shot
            reasons.append(f"Salary above range (may be senior): ${sal_min}")
    else:
        salary_score = 5  # Unknown salary = neutral
    breakdown["salary"] = salary_score
    score += salary_score

    # 5. LOCATION MATCH (0-10 points)
    location_score = 0
    for loc_kw in c["location_keywords"]:
        if loc_kw in full_text:
            if loc_kw in ["remote", "work from home", "wfh", "hybrid"]:
                location_score = 10
                reasons.append("Remote/hybrid available")
            elif loc_kw in ["sengkang", "punggol", "hougang", "serangoon"]:
                location_score = 10
                reasons.append(f"Near home: {loc_kw}")
            else:
                location_score = max(location_score, 6)
                reasons.append(f"Reasonable commute: {loc_kw}")
            break
    breakdown["location"] = location_score
    score += location_score

    # 6. NEGATIVE KEYWORD PENALTY (-20 to 0)
    neg_score = 0
    neg_matches = []
    for neg in c["negative_keywords"]:
        if neg in full_text:
            neg_score -= 5
            neg_matches.append(neg)
    neg_score = max(neg_score, -20)  # Cap penalty
    if neg_matches:
        reasons.append(f"⚠️ Red flags: {', '.join(neg_matches[:3])}")
    breakdown["negative"] = neg_score
    score += neg_score

    # 7. EDUCATION MATCH (critical filter for diploma holders)
    edu_score = 0

    # Absolute hard requirements — almost never hire without a degree
    strict_degree_phrases = [
        "must have a degree", "must possess a degree",
        "requires a degree", "require a degree",
        "strictly bachelor", "minimum bachelor",
        "minimum degree", "degree is mandatory",
    ]
    # Standard degree mentions — preferred but often flexible in practice
    soft_degree_phrases = [
        "bachelor's degree required", "bachelor degree required",
        "degree required", "degree in computer science",
        "degree in information", "degree in business",
        "degree in statistics", "degree in mathematics",
        "degree in engineering", "degree in data science",
        "university degree", "recognised degree", "recognized degree",
    ]

    # "ite" needs word boundaries — bare substring matching would hit
    # "onsite", "website", "suite", etc.
    accepts_diploma = any(kw in full_text for kw in [
        "diploma", "polytechnic", "nitec",
        "diploma or degree", "degree or diploma",
        "degree/diploma", "diploma/degree",
    ]) or bool(re.search(r"\bite\b", full_text))
    has_strict_degree = any(p in full_text for p in strict_degree_phrases)
    has_soft_degree = any(p in full_text for p in soft_degree_phrases)
    mentions_degree = "degree" in full_text

    if accepts_diploma:
        edu_score = 5
        reasons.append("✅ Accepts diploma holders")
    elif has_strict_degree:
        edu_score = -40
        reasons.append("🚫 Strictly requires degree")
    elif has_soft_degree:
        edu_score = -15
        reasons.append("⚠️ Degree preferred (diploma may still qualify)")
    elif mentions_degree:
        edu_score = -8
        reasons.append("⚠️ Mentions degree (often flexible)")
    else:
        edu_score = 3
        reasons.append("No specific education requirement listed")
    
    breakdown["education"] = edu_score
    score += edu_score

    # Normalize to 0-100
    score = max(0, min(100, score))

    job["score"] = score
    job["score_breakdown"] = breakdown
    job["match_reasons"] = reasons

    return job


def rank_jobs(jobs: list, cfg: dict = None) -> list:
    """Score and rank a list of jobs, highest score first."""
    scored = [score_job(job, cfg) for job in jobs]
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


def filter_jobs(jobs: list, min_score: int = None) -> list:
    """Filter jobs by minimum score threshold."""
    if min_score is None:
        min_score = SEARCH_CONFIG["min_score_threshold"]
    return [j for j in jobs if j["score"] >= min_score]
