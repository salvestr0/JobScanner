"""
Email notification module — sends job matches as HTML digest via Resend.
"""
import html as _html
import os
from datetime import datetime, timezone, timedelta

import requests


def _reason_list(reasons) -> list:
    """Normalise match_reasons to a list.

    Scan results carry a list, but Job.to_dict() returns the DB's
    "|"-joined string — slicing that as-is would render one pill per
    character.
    """
    if not reasons:
        return []
    if isinstance(reasons, str):
        return [r.strip() for r in reasons.split("|") if r.strip()]
    return reasons


def send_email_digest(jobs: list, settings: dict, subject_override: str = "") -> bool:
    """
    Send job matches as an HTML email digest via Resend.
    Requires RESEND_API_KEY and RESEND_FROM env vars.
    """
    api_key   = os.getenv("RESEND_API_KEY", "").strip()
    from_addr = os.getenv("RESEND_FROM", "CareerJobScan <noreply@jobscanner.app>").strip()
    to_email  = settings.get("email_to", "").strip()

    if not api_key:
        print("  [Email] RESEND_API_KEY not set — add it to your .env file")
        return False
    if not to_email:
        print("  [Email] No recipient email configured — set one in Settings")
        return False

    subject = subject_override or (
        f"CareerJobScan: {len(jobs)} new match{'es' if len(jobs) != 1 else ''} today"
        if jobs else "CareerJobScan: No new matches today"
    )

    if not jobs:
        body_html = "<p style='color:#64748b'>No new matching jobs were found in today's scan.</p>"
    else:
        rows = ""
        for job in jobs[:20]:
            score   = job.get("score", 0)
            title   = _html.escape(job.get("title", ""))
            company = _html.escape(job.get("company", ""))
            location = _html.escape(job.get("location", ""))
            source  = _html.escape(job.get("source", ""))
            _raw_url = job.get("url", "") or ""
            url     = _html.escape(_raw_url) if _raw_url.startswith(("http://", "https://")) else "#"

            if score >= 70:
                badge_bg, badge_fg = "#D1FAE5", "#065F46"
            elif score >= 50:
                badge_bg, badge_fg = "#FEF3C7", "#92400E"
            else:
                badge_bg, badge_fg = "#FEE2E2", "#7F1D1D"

            sal = ""
            if job.get("salary_min") and job.get("salary_max"):
                sal = f"${job['salary_min']:,}–${job['salary_max']:,}/mo"

            loc_sal = ""
            if location:
                loc_sal += f" · {location}"
            if sal:
                loc_sal += f" · {sal}"

            reasons = _reason_list(job.get("match_reasons"))
            reason_pills = "".join(
                f"<span style='display:inline-block;background:#EEF2FF;color:#4338CA;"
                f"font-size:11px;padding:2px 7px;border-radius:10px;margin:3px 3px 0 0'>"
                f"{_html.escape(r)}</span>"
                for r in reasons[:3]
            )
            reasons_html = f"<div style='margin-top:5px'>{reason_pills}</div>" if reason_pills else ""

            rows += f"""
            <tr>
              <td style="padding:10px 12px;border-bottom:1px solid #f1f5f9;vertical-align:top">
                <span style="display:inline-block;background:{badge_bg};color:{badge_fg};
                  font-size:12px;font-weight:700;padding:2px 8px;border-radius:20px">{score}</span>
              </td>
              <td style="padding:10px 12px;border-bottom:1px solid #f1f5f9;vertical-align:top">
                <a href="{url}" style="color:#4F46E5;font-weight:600;text-decoration:none;
                  font-size:14px">{title}</a><br>
                <span style="color:#64748b;font-size:13px">{company}{loc_sal}</span>
                {reasons_html}
              </td>
              <td style="padding:10px 12px;border-bottom:1px solid #f1f5f9;vertical-align:top;
                font-size:12px;color:#94a3b8;white-space:nowrap">{source}</td>
            </tr>"""

        body_html = f"""
        <h2 style="margin:0 0 16px;font-size:18px;color:#1e293b">
          {len(jobs)} New Job Match{'es' if len(jobs) != 1 else ''}
        </h2>
        <table style="width:100%;border-collapse:collapse;font-family:sans-serif;font-size:14px">
          <thead>
            <tr style="background:#f8fafc">
              <th style="padding:10px 12px;text-align:left;color:#94a3b8;font-size:11px;
                text-transform:uppercase;font-weight:600;letter-spacing:.05em">Score</th>
              <th style="padding:10px 12px;text-align:left;color:#94a3b8;font-size:11px;
                text-transform:uppercase;font-weight:600;letter-spacing:.05em">Job</th>
              <th style="padding:10px 12px;text-align:left;color:#94a3b8;font-size:11px;
                text-transform:uppercase;font-weight:600;letter-spacing:.05em">Source</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
        <p style="margin:16px 0 0;color:#94a3b8;font-size:12px">
          Log in to CareerJobScan to view all matches and track your applications.
        </p>"""

    html_body = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f8fafc;font-family:ui-sans-serif,system-ui,sans-serif">
  <div style="max-width:600px;margin:32px auto;background:white;border-radius:12px;
    padding:28px;box-shadow:0 1px 3px rgba(0,0,0,.08)">
    <div style="margin-bottom:20px">
      <span style="display:inline-block;background:#EEF2FF;color:#4F46E5;font-size:12px;
        font-weight:700;padding:3px 10px;border-radius:20px;letter-spacing:.05em">JOB SCANNER</span>
    </div>
    {body_html}
  </div>
</body></html>"""

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"from": from_addr, "to": [to_email], "subject": subject, "html": html_body},
            timeout=15,
        )
        if resp.status_code in (200, 201):
            print(f"  [Email] Digest sent to {to_email}")
            return True
        else:
            print(f"  [Email] Resend error {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as e:
        print(f"  [Email] Failed: {e}")
        return False


def send_weekly_digest(to_email: str, jobs: list, base_url: str = "", week_total: int = 0) -> bool:
    """
    Send a weekly top-matches summary email.
    Called by /api/cron/weekly-digest every Monday morning.
    """
    api_key   = os.getenv("RESEND_API_KEY", "").strip()
    from_addr = os.getenv("RESEND_FROM", "CareerJobScan <noreply@jobscanner.app>").strip()

    if not api_key:
        print("  [Weekly] RESEND_API_KEY not set")
        return False
    if not to_email:
        return False

    now           = datetime.now(timezone.utc)
    week_start_dt = now - timedelta(days=7)
    week_start    = f"{week_start_dt.day} {week_start_dt.strftime('%b')}"
    week_end      = f"{now.day} {now.strftime('%b %Y')}"
    subject = "Your top job matches this week · CareerJobScan"
    dashboard_url = _html.escape(f"{base_url}/app") if base_url else "#"

    stat_parts = []
    if week_total:
        stat_parts.append(f"{week_total} job{'s' if week_total != 1 else ''} scanned")
    stat_parts.append(f"{len(jobs)} top match{'es' if len(jobs) != 1 else ''}")
    stats_line = " &nbsp;·&nbsp; ".join(stat_parts)

    rows = ""
    for job in jobs:
        score   = job.get("score", 0)
        title   = _html.escape(job.get("title", ""))
        company = _html.escape(job.get("company", ""))
        source  = _html.escape(job.get("source", ""))
        raw_url = job.get("url", "") or ""
        url     = _html.escape(raw_url) if raw_url.startswith(("http://", "https://")) else "#"

        sal = ""
        if job.get("salary_min") and job.get("salary_max"):
            sal = f" &nbsp;·&nbsp; S${job['salary_min']:,}–${job['salary_max']:,}/mo"

        if score >= 70:
            badge_bg, badge_fg = "#D1FAE5", "#065F46"
        elif score >= 50:
            badge_bg, badge_fg = "#FEF3C7", "#92400E"
        else:
            badge_bg, badge_fg = "#FEE2E2", "#7F1D1D"

        reasons = _reason_list(job.get("match_reasons"))
        reason_pills = "".join(
            f"<span style='display:inline-block;background:#EEF2FF;color:#4338CA;"
            f"font-size:11px;padding:2px 7px;border-radius:10px;margin:3px 3px 0 0'>"
            f"{_html.escape(r)}</span>"
            for r in reasons[:3]
        )
        reasons_html = f"<div style='margin-top:5px'>{reason_pills}</div>" if reason_pills else ""

        rows += f"""
            <tr>
              <td style="padding:12px 14px;border-bottom:1px solid #f1f5f9;vertical-align:top;width:52px">
                <span style="display:inline-block;background:{badge_bg};color:{badge_fg};
                  font-size:12px;font-weight:700;padding:3px 9px;border-radius:20px;
                  white-space:nowrap">{score}</span>
              </td>
              <td style="padding:12px 14px;border-bottom:1px solid #f1f5f9;vertical-align:top">
                <a href="{url}" style="color:#4F46E5;font-weight:600;text-decoration:none;
                  font-size:14px;line-height:1.4">{title}</a><br>
                <span style="color:#64748b;font-size:13px">{company}{sal}</span>
                {reasons_html}
              </td>
              <td style="padding:12px 14px;border-bottom:1px solid #f1f5f9;vertical-align:top;
                font-size:11px;color:#94a3b8;white-space:nowrap">{source}</td>
            </tr>"""

    html_body = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f8fafc;font-family:ui-sans-serif,system-ui,sans-serif">
  <div style="max-width:600px;margin:32px auto;background:white;border-radius:16px;
    overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.08)">

    <!-- Header -->
    <div style="background:#4F46E5;padding:28px 32px">
      <div style="margin-bottom:14px">
        <span style="display:inline-block;background:rgba(255,255,255,.15);color:white;
          font-size:11px;font-weight:700;padding:3px 10px;border-radius:20px;
          letter-spacing:.07em">JOB SCANNER</span>
      </div>
      <h1 style="margin:0 0 6px;color:white;font-size:22px;font-weight:700;line-height:1.3">
        Your week in review
      </h1>
      <p style="margin:0;color:rgba(255,255,255,.7);font-size:13px">
        {week_start} – {week_end}
      </p>
    </div>

    <!-- Stats bar -->
    <div style="background:#EEF2FF;padding:12px 32px;border-bottom:1px solid #e0e7ff">
      <span style="color:#4338CA;font-size:13px;font-weight:600">{stats_line}</span>
    </div>

    <!-- Body -->
    <div style="padding:24px 32px">
      <p style="margin:0 0 20px;color:#475569;font-size:14px;line-height:1.6">
        Here are your best matches from the past 7 days — sorted by match score.
        Apply directly or open the dashboard to track your pipeline.
      </p>

      <table style="width:100%;border-collapse:collapse;font-family:ui-sans-serif,sans-serif">
        <thead>
          <tr style="background:#f8fafc">
            <th style="padding:10px 14px;text-align:left;color:#94a3b8;font-size:10px;
              text-transform:uppercase;font-weight:700;letter-spacing:.06em">Score</th>
            <th style="padding:10px 14px;text-align:left;color:#94a3b8;font-size:10px;
              text-transform:uppercase;font-weight:700;letter-spacing:.06em">Job</th>
            <th style="padding:10px 14px;text-align:left;color:#94a3b8;font-size:10px;
              text-transform:uppercase;font-weight:700;letter-spacing:.06em">Source</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>

      <!-- CTA -->
      <div style="margin-top:24px;text-align:center">
        <a href="{dashboard_url}"
          style="display:inline-block;background:#4F46E5;color:white;font-size:14px;
            font-weight:600;text-decoration:none;padding:12px 28px;border-radius:10px">
          Open Dashboard →
        </a>
      </div>
    </div>

    <!-- Footer -->
    <div style="background:#f8fafc;border-top:1px solid #f1f5f9;padding:16px 32px;text-align:center">
      <p style="margin:0;color:#94a3b8;font-size:12px;line-height:1.6">
        You're receiving this because weekly digests are enabled on your account.<br>
        <a href="{dashboard_url}" style="color:#6366F1;text-decoration:none">Manage preferences</a>
        in your account settings.
      </p>
    </div>

  </div>
</body></html>"""

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"from": from_addr, "to": [to_email], "subject": subject, "html": html_body},
            timeout=15,
        )
        if resp.status_code in (200, 201):
            print(f"  [Weekly] Digest sent to {to_email}")
            return True
        else:
            print(f"  [Weekly] Resend error {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as e:
        print(f"  [Weekly] Failed: {e}")
        return False
