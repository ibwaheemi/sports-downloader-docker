#!/bin/bash

# entrypoint.sh - Docker entrypoint for Sports Downloader

set -e

echo "Sports Downloader Container Starting..."
echo "=================================="
echo "WEBSITE_URL: $WEBSITE_URL"
echo "DOWNLOAD_PATH: $DOWNLOAD_PATH"
echo "CHECK_INTERVAL: $CHECK_INTERVAL minutes"
echo "RETENTION_DAYS: $RETENTION_DAYS days"
echo "LOG_FILE: $LOG_FILE"
echo "=================================="

# Create necessary directories
mkdir -p "$DOWNLOAD_PATH"
mkdir -p "$(dirname "$DATA_FILE")"
mkdir -p "$(dirname "$LOG_FILE")"

# Set permissions
chmod 755 "$DOWNLOAD_PATH"
chmod 755 "$(dirname "$DATA_FILE")"
chmod 755 "$(dirname "$LOG_FILE")"

# Update cron job with the CHECK_INTERVAL from environment
CRON_INTERVAL="*/${CHECK_INTERVAL}"
if [ "$CHECK_INTERVAL" = "300" ]; then
    CRON_INTERVAL="*/5"
elif [ "$CHECK_INTERVAL" = "600" ]; then
    CRON_INTERVAL="*/10"
elif [ "$CHECK_INTERVAL" = "900" ]; then
    CRON_INTERVAL="*/15"
elif [ "$CHECK_INTERVAL" = "1800" ]; then
    CRON_INTERVAL="*/30"
elif [ "$CHECK_INTERVAL" = "3600" ]; then
    CRON_INTERVAL="0 *"
else
    # For custom intervals, calculate minutes
    MINUTES=$((CHECK_INTERVAL / 60))
    if [ $MINUTES -lt 60 ]; then
        CRON_INTERVAL="*/$MINUTES"
    else
        echo "Warning: CHECK_INTERVAL too large, defaulting to 30 minutes"
        CRON_INTERVAL="*/30"
    fi
fi

echo "$CRON_INTERVAL * * * * cd /app && python3 sports_downloader.py >> /var/log/sports_cron.log 2>&1" > /etc/cron.d/sports-downloader
chmod 0644 /etc/cron.d/sports-downloader
crontab /etc/cron.d/sports-downloader

echo "Cron job scheduled: $CRON_INTERVAL * * * *"

# Run initial validation
echo "Running initial validation..."
python3 /app/sports_downloader.py --validate

# Start cron daemon
echo "Starting cron daemon..."
cron

# Keep container running and show logs
echo "Container started successfully. Monitoring logs..."
echo "Press Ctrl+C to stop the container."

# Tail the log files
touch "$LOG_FILE" /var/log/sports_cron.log
tail -f "$LOG_FILE" /var/log/sports_cron.log