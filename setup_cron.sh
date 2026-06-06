#!/bin/bash
# setup_cron.sh - Set up daily job scanning at 9 AM
# Run: chmod +x setup_cron.sh && ./setup_cron.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_PATH="$(which python3 || which python)"

if [ -z "$PYTHON_PATH" ]; then
    echo "❌ Python not found. Install Python 3 first."
    exit 1
fi

echo "📁 Scanner directory: $SCRIPT_DIR"
echo "🐍 Python path: $PYTHON_PATH"

# Create the cron job line
CRON_LINE="0 9 * * * cd $SCRIPT_DIR && $PYTHON_PATH main.py >> data/scan.log 2>&1"

# Check if cron job already exists
if crontab -l 2>/dev/null | grep -q "job_scanner"; then
    echo "⚠️  A job_scanner cron job already exists:"
    crontab -l | grep "job_scanner"
    read -p "Replace it? (y/n): " confirm
    if [ "$confirm" != "y" ]; then
        echo "Cancelled."
        exit 0
    fi
    # Remove existing job_scanner entries
    crontab -l | grep -v "job_scanner" | crontab -
fi

# Add new cron job
(crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -

echo "✅ Cron job added! The scanner will run daily at 9:00 AM."
echo ""
echo "To verify: crontab -l"
echo "To remove: crontab -e (delete the job_scanner line)"
echo ""
echo "📌 Logs will be saved to: $SCRIPT_DIR/data/scan.log"
