# Sports Downloader Docker Container

A containerized video downloader that monitors sports replay websites for new games and automatically downloads them from various video sources (OK.ru, YouTube, etc.).

## Quick Start

1. Create the project directory:
```bash
mkdir sports-downloader-docker
cd sports-downloader-docker
```
2. Download files cp/scp to sports-downloader-docker

3. Make entrypoint executable:
```bash
chmod +x entrypoint.sh
```

4. Start the container:
```bash
docker-compose up -d
```

## Configuration

All settings are configurable via environment variables in `docker-compose.yml`:

### Core Settings
- `WEBSITE_URL`: The sports replay website to monitor (default: "https://basketballreplays.net")
- `DOWNLOAD_PATH`: Where to store downloaded videos inside container (default: "/downloads")
- `CHECK_INTERVAL`: How often to check for new videos in seconds (default: "300" = 5 minutes)
- `RETENTION_DAYS`: How many days to keep downloaded files (default: "7")

### Download Settings  
- `MAX_DOWNLOAD_TIME`: Maximum time per download in seconds (default: "14400" = 4 hours)
- `MAX_FILE_SIZE`: Maximum file size in bytes (default: "16106127360" = 15GB)

### Advanced Settings
- `START_DATE`: Only download videos discovered after this date (format: "YYYY-MM-DD")
- `DATA_FILE`: Path to downloads tracking file
- `KNOWN_LINKS_FILE`: Path to known links tracking file  
- `LOG_FILE`: Path to log file

## Volume Mounts

The Docker compose file creates these directories on your host:

- `./downloads` - Downloaded video files
- `./data` - Persistent data (tracking files)
- `./logs` - Log files

## Common Configurations

### Different Sports Sites
```yaml
environment:
  WEBSITE_URL: "https://nflreplays.com"  # NFL
  # or
  WEBSITE_URL: "https://soccerstreams.net"  # Soccer
  # or  
  WEBSITE_URL: "https://hockeyreplays.tv"  # Hockey
```

### Every 10 minutes instead of 5:
```yaml
environment:
  CHECK_INTERVAL: "600"
```

### Different download location:
```yaml
volumes:
  - /path/to/your/videos:/downloads
```

### Only download new season games:
```yaml
environment:
  START_DATE: "2025-10-01"  # Start of new season
```

### Longer retention (30 days):
```yaml
environment:
  RETENTION_DAYS: "30"
```

## Commands

### Start container:
```bash
docker compose up -d
```

### View logs:
```bash
docker-compose logs -f
# or
docker logs -f sports-downloader
```

### Validate setup:
```bash
docker exec sports-downloader python3 /app/sports_downloader.py --validate
```

### Test download capability:
```bash
docker exec sports-downloader python3 /app/sports_downloader.py --test-download
```

### Stop container:
```bash
docker-compose down
```

### Rebuild after changes:
```bash
docker-compose down
docker-compose build --no-cache
docker-compose up -d
```

## Monitoring

### Check container health:
```bash
docker ps
# Look for "healthy" status
```

### View current downloads:
```bash
ls -lah ./downloads/
```

### Check what's running:
```bash
docker exec sports-downloader ps aux
```

## Video Source Support

The downloader supports multiple video hosting platforms:
- **OK.ru (Odnoklassniki)** - Optimized format selection
- **YouTube** - Quality-limited downloads
- **Vimeo** - Standard downloads
- **Dailymotion** - Standard downloads
- **Streamable** - Standard downloads

## Troubleshooting

### Container won't start:
- Check logs: `docker-compose logs`
- Verify file permissions: `chmod +x entrypoint.sh`
- Check available disk space

### No downloads happening:
- Run validation: `docker exec sports-downloader python3 /app/sports_downloader.py --validate`
- Check if it's the sports season for your configured sport
- View logs for errors: `docker-compose logs -f`

### SSL/Connection errors:
- Container includes SSL fixes for problematic sites
- Check network connectivity: `docker exec sports-downloader ping 8.8.8.8`

### Downloads failing:
- Check yt-dlp version: `docker exec sports-downloader yt-dlp --version`
- Test specific URL: `docker exec sports-downloader python3 /app/sports_downloader.py --test-download "https://ok.ru/videoembed/12345"`

### Out of disk space:
- Adjust `RETENTION_DAYS` to keep files for fewer days
- Manually clean up old files in `./downloads/`

## Migration from Systemd

To migrate from an existing systemd setup:

1. Copy existing data files:
```bash
sudo cp /var/lib/sports-downloader/* ./data/
sudo cp /var/log/sports_downloader.log ./logs/
sudo chown -R $(id -u):$(id -g) ./data ./logs
```

2. Stop old systemd service:
```bash
sudo systemctl stop sports-downloader.timer
sudo systemctl disable sports-downloader.timer
```

3. Start Docker container:
```bash
docker-compose up -d
```

## File Structure

```
sports-downloader-docker/
├── Dockerfile
├── sports_downloader.py
├── entrypoint.sh
├── docker-compose.yml
├── README.md
├── downloads/          # Video files (created automatically)
├── data/              # Tracking data (created automatically)  
└── logs/              # Log files (created automatically)
```

## Customization for Different Sports

The downloader can be easily customized for different sports by modifying the keywords in the `get_video_links()` method. Current keywords include:

- Basketball: `nba`, `basketball`
- Football: `football`, `nfl`  
- Soccer: `soccer`, `football`
- Hockey: `hockey`, `nhl`
- Baseball: `baseball`, `mlb`
- General: `vs`, `game`, `replay`, `highlights`, `final`, `match`, `championship`

## Updates

To update the container with new features:

1. Update the Python script in `sports_downloader.py`
2. Rebuild: `docker-compose build --no-cache`  
3. Restart: `docker-compose up -d`

## Features

The container includes:
- Automatic resume for interrupted downloads
- Multi-platform video source support (OK.ru, YouTube, etc.)
- SSL handling for problematic connections  
- Configurable retry logic
- Health checks and monitoring
- Log rotation
- Automatic cleanup of old files
- Generic sports content detection