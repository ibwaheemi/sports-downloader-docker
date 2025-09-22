FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    wget \
    ffmpeg \
    cron \
    && rm -rf /var/lib/apt/lists/*

# Install yt-dlp
RUN pip install --no-cache-dir yt-dlp requests beautifulsoup4 urllib3

# Create app directory
WORKDIR /app

# Create necessary directories
RUN mkdir -p /var/lib/sports-downloader /var/log /downloads

# Copy the application script
COPY sports_downloader.py /app/
COPY entrypoint.sh /app/
RUN chmod +x /app/entrypoint.sh

# Environment variables with defaults
ENV WEBSITE_URL="https://basketballreplays.net"
ENV DOWNLOAD_PATH="/downloads"
ENV CHECK_INTERVAL="300"
ENV RETENTION_DAYS="7"
ENV DATA_FILE="/var/lib/sports-downloader/sports_downloads.json"
ENV KNOWN_LINKS_FILE="/var/lib/sports-downloader/sports_known_links.json"
ENV START_DATE=""
ENV LOG_FILE="/var/log/sports_downloader.log"
ENV MAX_DOWNLOAD_TIME="14400"
ENV MAX_FILE_SIZE="16106127360"

# Expose volume for downloads
VOLUME ["/downloads", "/var/lib/sports-downloader", "/var/log"]

# Set up cron job
RUN echo "*/5 * * * * cd /app && python3 sports_downloader.py >> /var/log/sports_cron.log 2>&1" > /etc/cron.d/sports-downloader
RUN chmod 0644 /etc/cron.d/sports-downloader
RUN crontab /etc/cron.d/sports-downloader

# Health check
HEALTHCHECK --interval=10m --timeout=30s --start-period=5m --retries=3 \
    CMD python3 /app/sports_downloader.py --validate || exit 1

# Run cron in foreground
CMD ["/app/entrypoint.sh"]