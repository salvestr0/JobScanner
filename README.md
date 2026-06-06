# 🔍 Job Scanner - Jayden's Automated Job Matching Tool

Scrapes MyCareersFuture, Indeed, and LinkedIn for analyst roles that match your profile, scores them, generates tailored cover notes, and sends the best matches straight to your Telegram.

---

## ⚡ Quick Start (5 minutes)

### 1. Install Python dependencies
```bash
cd job_scanner
pip install -r requirements.txt
```

### 2. Test your Telegram bot
```bash
python main.py --test
```
You should get a message in Telegram confirming the connection.

### 3. Run your first scan
```bash
python main.py
```

That's it! Check your Telegram for matched jobs and look in `data/cover_notes/` for generated cover notes.

---

## 📁 Project Structure

```
job_scanner/
├── main.py           # Run this - orchestrates everything
├── config.py         # YOUR settings - profile, search criteria, API keys
├── scrapers.py       # Scrapes MyCareersFuture, Indeed, LinkedIn
├── scorer.py         # Scores each job against your profile (0-100)
├── cover_notes.py    # Generates tailored cover notes per job
├── notifier.py       # Sends results to Telegram
├── requirements.txt  # Python dependencies
├── setup_cron.sh     # Auto-scheduling script (Mac/Linux)
└── data/             # Created on first run
    ├── matched_jobs.csv    # All matched jobs (grows over time)
    ├── seen_jobs.json      # Tracks which jobs you've already seen
    └── cover_notes/        # One .txt file per matched job
```

---

## 🔧 Commands

| Command | What it does |
|---------|-------------|
| `python main.py` | Full scan → score → cover notes → Telegram (analyst mode) |
| `python main.py --mode=it` | Scan for IT Support / Helpdesk roles |
| `python main.py --mode=admin` | Scan for Admin / Operations Executive roles |
| `python main.py --test` | Test Telegram bot connection |
| `python main.py --no-notify` | Scan and save results, skip Telegram |
| `python main.py --reset` | Clear history so all jobs appear as "new" |
| `python main.py --track` | Sync Telegram button taps → `data/application_status.json` |
| `python main.py --status` | View your application summary (Applied / Interview / Skipped) |

---

## ⏰ Auto-Schedule (Run Daily)

### Mac / Linux (cron)
```bash
# Run the setup script
chmod +x setup_cron.sh
./setup_cron.sh

# Or manually add to crontab:
crontab -e
# Add this line (runs daily at 9 AM):
0 9 * * * cd /full/path/to/job_scanner && /usr/bin/python3 main.py >> data/scan.log 2>&1
```

### Windows (Task Scheduler)
1. Open Task Scheduler
2. Create Basic Task → "Job Scanner"
3. Trigger: Daily at 9:00 AM
4. Action: Start a program
   - Program: `python` (or full path like `C:\Python312\python.exe`)
   - Arguments: `main.py`
   - Start in: `C:\path\to\job_scanner`

---

## 🤖 Enable AI Cover Notes (Optional but Recommended)

The tool generates decent template-based cover notes by default. To get **AI-powered personalized cover notes** using Gemini Flash (free):

1. Go to [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
2. Sign in with your Google account
3. Click **Create API key**
4. Open `config.py` and paste your key:
   ```python
   GEMINI_API_KEY = "AIza..."
   ```

Free tier gives you **1,500 requests/day** — far more than you'll ever need for daily scans. AI-generated notes are significantly better as they reference specific details from each job description.

---

## 🎛️ Customize Your Search

Edit `config.py` to change:

- **`target_titles`** — Add or remove job titles to search for
- **`preferred_keywords`** — Skills/terms that boost a job's score
- **`negative_keywords`** — Red flags that lower a job's score
- **`min_salary` / `max_salary`** — Your salary range (SGD monthly)
- **`location_keywords`** — Areas you prefer
- **`min_score_threshold`** — Minimum score to notify you (default: 40)

---

## 📊 How Scoring Works

Each job gets scored 0-100 based on:

| Factor | Points | What it checks |
|--------|--------|----------------|
| Title match | 0-30 | Does the job title match your target roles? |
| Skills match | 0-30 | How many of your skills appear in the JD? |
| Experience level | -10 to 15 | Is it junior/entry-level friendly? |
| Salary | -5 to 10 | Within your range? |
| Location | 0-10 | Near Sengkang or remote? |
| Education | -3 to 5 | Accepts diploma? |
| Red flags | -20 to 0 | Contains negative keywords? |

---

## 🛠️ Troubleshooting

**"No jobs found from any source"**
- Check your internet connection
- MyCareersFuture API may be temporarily down — try again later
- Indeed may block requests — the tool uses polite delays but this can happen

**"Telegram send failed"**
- Verify your bot token and chat ID in `config.py`
- Make sure you've sent at least one message TO your bot first
- Visit `https://api.telegram.org/bot<TOKEN>/getUpdates` to verify your chat ID

**"All jobs filtered out"**
- Lower `min_score_threshold` in config.py (try 30)
- Add more `target_titles`
- Check if `negative_keywords` is too aggressive

**Indeed returns no results**
- Indeed actively blocks scrapers — this is normal
- MyCareersFuture is the most reliable source for SG jobs
- LinkedIn public search has limited results but is stable

---

## 💡 Pro Tips

1. **Run it daily** — new jobs get posted every morning
2. **Check the CSV** — `data/matched_jobs.csv` is your running log, open it in Excel to track what you've applied to
3. **Edit cover notes** — The generated notes are a starting point, personalize them before submitting
4. **Add a column to the CSV** — Track application status (Applied / Interview / Rejected) manually
5. **Lower threshold gradually** — Start at 40, if too few results try 30. If too many, raise to 50
