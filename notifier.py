"""
Email notification module — sends job matches as HTML digest via Resend.
"""
import html as _html
import os

import requests


def send_email_digest(jobs: list, settings: dict, subject_override: str = "") -> bool:
    """
    Send job matches as an HTML email digest via Resend.
    Requires RESEND_API_KEY and RESEND_FROM env vars.
    """
    api_key   = os.getenv("RESEND_API_KEY", "").strip()
    from_addr = os.getenv("RESEND_FROM", "Job Scanner <noreply@jobscanner.app>").strip()
    to_email  = settings.get("email_to", "").strip()

    if not api_key:
        print("  [Email] RESEND_API_KEY not set — add it to your .env file")
        return False
    if not to_email:
        print("  [Email] No recipient email configured — set one in Settings")
        return False

    subject = subject_override or (
        f"Job Scanner: {len(jobs)} new match{'es' if len(jobs) != 1 else ''} today"
        if jobs else "Job Scanner: No new matches today"
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
            url     = _html.escape(job.get("url", "#"))

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
          Log in to Job Scanner to view all matches and track your applications.
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
