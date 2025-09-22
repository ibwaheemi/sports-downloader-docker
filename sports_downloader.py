#!/usr/bin/env python3
"""
Sports Replay Downloader - Docker Version
Monitors sports replay websites for new videos and downloads them from various sources
Configured via environment variables for Docker deployment
"""

import requests
import os
import subprocess
import time
import json
import logging
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
import re
import random
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3

# Configuration from environment variables
WEBSITE_URL = os.getenv('WEBSITE_URL', 'https://basketballreplays.net')
DOWNLOAD_PATH = os.getenv('DOWNLOAD_PATH', '/downloads')
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '300'))  # seconds
RETENTION_DAYS = int(os.getenv('RETENTION_DAYS', '7'))
DATA_FILE = os.getenv('DATA_FILE', '/var/lib/sports-downloader/sports_downloads.json')
KNOWN_LINKS_FILE = os.getenv('KNOWN_LINKS_FILE', '/var/lib/sports-downloader/sports_known_links.json')
LOG_FILE = os.getenv('LOG_FILE', '/var/log/sports_downloader.log')
MAX_DOWNLOAD_TIME = int(os.getenv('MAX_DOWNLOAD_TIME', '14400'))  # 4 hours default
MAX_FILE_SIZE = int(os.getenv('MAX_FILE_SIZE', '16106127360'))  # 15GB default

# Handle START_DATE from environment
START_DATE_ENV = os.getenv('START_DATE', '')
if START_DATE_ENV:
    try:
        START_DATE = datetime.fromisoformat(START_DATE_ENV)
    except ValueError:
        START_DATE = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
else:
    START_DATE = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

# User agents for rotation
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0'
]

# Setup logging with rotation
from logging.handlers import RotatingFileHandler

# Ensure log directory exists
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=5),  # 10MB max, 5 backups
        logging.StreamHandler()
    ]
)

class SportsDownloader:
    def __init__(self):
        # Log configuration on startup
        logging.info("=== SPORTS DOWNLOADER DOCKER CONTAINER ===")
        logging.info(f"Website URL: {WEBSITE_URL}")
        logging.info(f"Download Path: {DOWNLOAD_PATH}")
        logging.info(f"Check Interval: {CHECK_INTERVAL} seconds")
        logging.info(f"Retention Days: {RETENTION_DAYS}")
        logging.info(f"Max Download Time: {MAX_DOWNLOAD_TIME} seconds ({MAX_DOWNLOAD_TIME/3600:.1f} hours)")
        logging.info(f"Max File Size: {MAX_FILE_SIZE} bytes ({MAX_FILE_SIZE/1024/1024/1024:.1f} GB)")
        logging.info("=" * 50)
        
        self.session = self.create_session()
        
        # Create data directory if it doesn't exist
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        
        self.downloaded_videos = self.load_downloaded_list()
        self.known_links = self.load_known_links()
        
        # Create download directory if it doesn't exist
        os.makedirs(DOWNLOAD_PATH, exist_ok=True)
        
        # Only clean up old partial downloads (keep recent ones for resume)
        self.cleanup_old_partial_downloads()
        
        logging.info(f"Starting from: {START_DATE.strftime('%Y-%m-%d %H:%M:%S')}")
        logging.info("Will only download videos discovered after this time")
    
    def create_session(self):
        """Create a robust session with retries and SSL handling"""
        session = requests.Session()
        
        # Set up retry strategy
        retry_strategy = Retry(
            total=5,
            backoff_factor=2,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        
        # Set headers with random user agent
        session.headers.update({
            'User-Agent': random.choice(USER_AGENTS),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        })
        
        # Handle SSL issues - be more lenient
        session.verify = False  # Disable SSL verification for problematic sites
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        
        return session
    
    def get_resumable_downloads(self):
        """Find partial downloads that can be resumed"""
        resumable = []
        try:
            for filename in os.listdir(DOWNLOAD_PATH):
                if filename.endswith(('.part', '.f4v.part', '.webm.part')):
                    filepath = os.path.join(DOWNLOAD_PATH, filename)
                    file_size = os.path.getsize(filepath)
                    file_time = datetime.fromtimestamp(os.path.getmtime(filepath))
                    
                    # Only consider files larger than 1MB and newer than 7 days
                    if file_size > 1024 * 1024 and file_time > datetime.now() - timedelta(days=7):
                        # Try to determine the original title from filename
                        original_name = filename.replace('.part', '').replace('.f4v', '').replace('.webm', '')
                        if not original_name.endswith('.mp4'):
                            original_name += '.mp4'
                        
                        resumable.append({
                            'partial_file': filename,
                            'original_name': original_name,
                            'size_mb': file_size / (1024 * 1024),
                            'modified': file_time
                        })
                        
            if resumable:
                # Sort by modification time (most recent first)
                resumable.sort(key=lambda x: x['modified'], reverse=True)
                logging.info(f"Found {len(resumable)} resumable downloads")
                
            return resumable
        except Exception as e:
            logging.error(f"Error checking resumable downloads: {e}")
            return []
    
    def cleanup_old_partial_downloads(self):
        """Clean up old partial downloads (older than 24 hours) but keep recent ones for resume"""
        try:
            cutoff_time = datetime.now() - timedelta(hours=24)
            
            for filename in os.listdir(DOWNLOAD_PATH):
                if filename.endswith(('.part', '.ytdl', '.temp')):
                    filepath = os.path.join(DOWNLOAD_PATH, filename)
                    try:
                        file_time = datetime.fromtimestamp(os.path.getmtime(filepath))
                        if file_time < cutoff_time:
                            os.remove(filepath)
                            logging.info(f"Cleaned up old partial download: {filename}")
                    except OSError:
                        pass
        except Exception as e:
            logging.error(f"Error cleaning up old partial downloads: {e}")
    
    def load_known_links(self):
        """Load the list of all known links"""
        if os.path.exists(KNOWN_LINKS_FILE):
            try:
                with open(KNOWN_LINKS_FILE, 'r') as f:
                    data = json.load(f)
                    # Clean up old entries (older than 30 days)
                    cutoff = datetime.now() - timedelta(days=30)
                    cleaned_data = {}
                    for url, info in data.items():
                        try:
                            discovered_date = datetime.fromisoformat(info['discovered_date'])
                            if discovered_date >= cutoff:
                                cleaned_data[url] = info
                        except:
                            pass
                    return cleaned_data
            except:
                return {}
        return {}
    
    def save_known_links(self):
        """Save the list of known links"""
        try:
            with open(KNOWN_LINKS_FILE, 'w') as f:
                json.dump(self.known_links, f, indent=2)
        except Exception as e:
            logging.error(f"Error saving known links: {e}")
    
    def load_downloaded_list(self):
        """Load the list of already downloaded videos"""
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, 'r') as f:
                    return json.load(f)
            except:
                return {}
        return {}
    
    def save_downloaded_list(self):
        """Save the list of downloaded videos"""
        try:
            with open(DATA_FILE, 'w') as f:
                json.dump(self.downloaded_videos, f, indent=2)
        except Exception as e:
            logging.error(f"Error saving downloaded list: {e}")
    
    def is_new_video(self, video_url, title):
        """Check if this is a new video"""
        # Skip obviously non-video links
        if any(skip in title.lower() for skip in ['home', 'about', 'contact', 'category', 'tag']):
            return False
            
        if video_url not in self.known_links:
            self.known_links[video_url] = {
                'title': title,
                'discovered_date': datetime.now().isoformat(),
                'processed': False
            }
            self.save_known_links()
            return datetime.now() >= START_DATE
        
        link_info = self.known_links[video_url]
        discovered_date = datetime.fromisoformat(link_info['discovered_date'])
        
        return (discovered_date >= START_DATE and not link_info.get('processed', False))
    
    def mark_video_processed(self, video_url):
        """Mark a video as processed"""
        if video_url in self.known_links:
            self.known_links[video_url]['processed'] = True
            self.save_known_links()
    
    def get_video_links(self):
        """Scrape the main website for video links"""
        try:
            # Add delay and rotate user agent
            time.sleep(random.uniform(1, 3))
            self.session.headers['User-Agent'] = random.choice(USER_AGENTS)
            
            response = self.session.get(WEBSITE_URL, timeout=30)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            video_links = []
            
            # Look for post containers first
            posts = soup.find_all(['article', 'div'], class_=lambda x: x and any(
                keyword in x.lower() for keyword in ['post', 'entry', 'article', 'content']
            ))
            
            if not posts:
                # Fallback to all links in main content area
                main_content = soup.find(['main', 'div'], {'id': ['main', 'content']}) or soup
                posts = [main_content]
            
            for post in posts:
                for link in post.find_all('a', href=True):
                    href = link.get('href')
                    text = link.get_text(strip=True)
                    
                    # Skip empty or very short links
                    if not text or not href or len(text) < 10:
                        continue
                    
                    # Skip navigation and non-content links
                    if any(skip in text.lower() for skip in [
                        'read more', 'continue reading', 'home', 'about', 'contact',
                        'privacy', 'terms', 'subscribe', 'follow', 'share'
                    ]):
                        continue
                    
                    full_url = urljoin(WEBSITE_URL, href)
                    
                    # Only include links that look like sports game replays
                    # Customize these keywords for different sports
                    if (any(keyword in text.lower() for keyword in 
                           ['vs', 'v.', 'game', 'replay', 'nba', 'basketball', 'football', 'soccer', 'hockey', 
                            'baseball', 'highlights', 'final', 'match', 'championship']) and
                        WEBSITE_URL.replace('https://', '').replace('http://', '') in full_url and
                        len(text) > 15):  # Longer titles are more likely to be games
                        
                        video_links.append({
                            'title': text,
                            'url': full_url
                        })
            
            # Remove duplicates
            seen = set()
            unique_links = []
            for link in video_links:
                if link['url'] not in seen:
                    seen.add(link['url'])
                    unique_links.append(link)
            
            logging.info(f"Found {len(unique_links)} potential video links")
            return unique_links
            
        except requests.exceptions.SSLError as e:
            logging.error(f"SSL Error scraping main website: {e}")
            return []
        except Exception as e:
            logging.error(f"Error scraping main website: {e}")
            return []
    
    def find_video_source_link(self, video_page_url):
        """Find video source link (ok.ru, youtube, etc.) on a video page"""
        try:
            # Add delay and rotate user agent
            time.sleep(random.uniform(2, 4))
            self.session.headers['User-Agent'] = random.choice(USER_AGENTS)
            
            response = self.session.get(video_page_url, timeout=30)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Look for video source links in various places
            video_links = []
            
            # Common video hosting sites
            video_hosts = ['ok.ru', 'youtube.com', 'youtu.be', 'vimeo.com', 'dailymotion.com', 'streamable.com']
            
            # Direct links
            for link in soup.find_all('a', href=True):
                href = link.get('href')
                if any(host in href for host in video_hosts):
                    video_links.append(href)
            
            # Embedded iframes
            for iframe in soup.find_all('iframe', src=True):
                src = iframe.get('src')
                if any(host in src for host in video_hosts):
                    video_links.append(src)
            
            # Look in script tags for embedded links
            for script in soup.find_all('script'):
                if script.string:
                    for host in video_hosts:
                        pattern = r'https?://[^"\']*' + re.escape(host) + r'[^"\']*'
                        matches = re.findall(pattern, script.string)
                        video_links.extend(matches)
            
            if video_links:
                # Prefer certain sources (ok.ru for basketball, youtube for others)
                for link in video_links:
                    if 'ok.ru' in link and '/video/' in link:
                        return link
                for link in video_links:
                    if 'youtube.com' in link or 'youtu.be' in link:
                        return link
                # Return first available link
                return video_links[0]
            
            return None
            
        except requests.exceptions.SSLError as e:
            logging.error(f"SSL Error finding video link on {video_page_url}: {e}")
            return None
        except Exception as e:
            logging.error(f"Error finding video link on {video_page_url}: {e}")
            return None
    
    def sanitize_filename(self, filename):
        """Sanitize filename for filesystem"""
        # Remove invalid characters
        filename = re.sub(r'[<>:"/\\|?*]', '', filename)
        # Replace multiple spaces with single space
        filename = re.sub(r'\s+', ' ', filename)
        # Remove extra whitespace and dots
        filename = filename.strip(' .')
        
        # Limit length
        if len(filename) > 200:
            filename = filename[:200]
        
        if not filename.endswith('.mp4'):
            filename += '.mp4'
        return filename
    
    def download_video(self, video_url, title):
        """Download video using yt-dlp with resume capability"""
        try:
            filename = self.sanitize_filename(title)
            filepath = os.path.join(DOWNLOAD_PATH, filename)
            
            # Check if file already exists and is complete
            if os.path.exists(filepath):
                file_size = os.path.getsize(filepath)
                if file_size > 10 * 1024 * 1024:  # At least 10MB for complete videos
                    logging.info(f"File already exists: {filename} ({file_size / (1024*1024):.1f} MB)")
                    return True
                else:
                    # Remove small incomplete file
                    logging.info(f"Removing incomplete file: {filename} ({file_size} bytes)")
                    os.remove(filepath)
            
            # Check for existing partial downloads that can be resumed
            partial_files = []
            for existing in os.listdir(DOWNLOAD_PATH):
                if (existing.startswith(filename.replace('.mp4', '')) and 
                    existing.endswith(('.part', '.f4v.part', '.webm.part'))):
                    partial_size = os.path.getsize(os.path.join(DOWNLOAD_PATH, existing))
                    if partial_size > 1024 * 1024:  # Only consider sizeable partial files
                        partial_files.append((existing, partial_size))
            
            if partial_files:
                largest_partial = max(partial_files, key=lambda x: x[1])
                logging.info(f"Found resumable partial download: {largest_partial[0]} ({largest_partial[1] / (1024*1024):.1f} MB)")
                logging.info("yt-dlp will automatically resume from this point")
            
            # Determine format based on video source
            format_selector = 'best'
            if 'ok.ru' in video_url:
                format_selector = 'hd/sd/low/lowest'  # OK.ru specific formats
            elif 'youtube.com' in video_url or 'youtu.be' in video_url:
                format_selector = 'best[height<=1080]'  # YouTube with quality limit
            
            # Download using yt-dlp
            cmd = [
                'yt-dlp',
                '--no-playlist',
                '--format', format_selector,
                '--output', filepath,
                '--continue',  # Continue partial downloads
                '--no-part',   # Don't use .part files for better resume support
                '--retries', '10',
                '--fragment-retries', '10',
                '--retry-sleep', '5',
                '--socket-timeout', '30',
                '--no-check-certificates',  # Help with SSL issues
                '--concurrent-fragments', '1',  # Reduce for stability
                '--hls-use-mpegts',  # Better for live/long streams
                '--no-warnings',
                '--progress',  # Show progress for monitoring
                '--verbose',   # Add verbose logging to debug issues
                video_url
            ]
            
            logging.info(f"Downloading: {title}")
            logging.info(f"Source: {video_url}")
            logging.info(f"Command: {' '.join(cmd)}")
            
            result = subprocess.run(
                cmd, 
                capture_output=True, 
                text=True, 
                timeout=MAX_DOWNLOAD_TIME
            )
            
            if result.returncode == 0:
                if os.path.exists(filepath):
                    file_size = os.path.getsize(filepath)
                    if file_size > 10 * 1024 * 1024:  # At least 10MB for complete videos
                        subprocess.run(['chmod', '644', filepath])
                        logging.info(f"Successfully downloaded: {filename} ({file_size / (1024*1024):.1f} MB)")
                        
                        # Clean up any remaining partial files for this video
                        for existing in os.listdir(DOWNLOAD_PATH):
                            if (existing.startswith(filename.replace('.mp4', '')) and 
                                existing != filename and
                                existing.endswith(('.part', '.ytdl', '.temp'))):
                                try:
                                    os.remove(os.path.join(DOWNLOAD_PATH, existing))
                                    logging.info(f"Cleaned up partial file: {existing}")
                                except:
                                    pass
                        
                        return True
                    else:
                        logging.error(f"Downloaded file too small: {filename} ({file_size} bytes)")
                        return False
                else:
                    logging.error(f"Download completed but file missing: {filename}")
                    return False
            else:
                logging.error(f"Download failed for {title}")
                logging.error(f"yt-dlp stderr: {result.stderr}")
                
                # Check if we have a partial download that can be resumed later
                for existing in os.listdir(DOWNLOAD_PATH):
                    if (existing.startswith(filename.replace('.mp4', '')) and 
                        existing.endswith(('.part', '.f4v.part', '.webm.part'))):
                        partial_size = os.path.getsize(os.path.join(DOWNLOAD_PATH, existing))
                        if partial_size > 1024 * 1024:  # At least 1MB
                            logging.info(f"Partial download available for resume: {existing} ({partial_size / (1024*1024):.1f} MB)")
                            break
                
                return False
                
        except subprocess.TimeoutExpired:
            logging.error(f"Download timeout ({MAX_DOWNLOAD_TIME/3600:.1f}h) for {title}")
            
            # Don't kill the process immediately - let it finish current fragment
            logging.info("Download timed out, but partial download will be available for resume")
            
            # Check what we have so far
            for existing in os.listdir(DOWNLOAD_PATH):
                if (existing.startswith(filename.replace('.mp4', '')) and 
                    existing.endswith(('.part', '.f4v.part', '.webm.part'))):
                    partial_size = os.path.getsize(os.path.join(DOWNLOAD_PATH, existing))
                    if partial_size > 1024 * 1024:
                        logging.info(f"Partial download saved: {existing} ({partial_size / (1024*1024):.1f} MB)")
            
            return False
        except Exception as e:
            logging.error(f"Error downloading {title}: {e}")
            return False
    
    def cleanup_old_files(self):
        """Remove files older than retention period"""
        try:
            cutoff_date = datetime.now() - timedelta(days=RETENTION_DAYS)
            removed_count = 0
            
            for filename in os.listdir(DOWNLOAD_PATH):
                filepath = os.path.join(DOWNLOAD_PATH, filename)
                
                if os.path.isfile(filepath) and filename.endswith('.mp4'):
                    file_time = datetime.fromtimestamp(os.path.getctime(filepath))
                    
                    if file_time < cutoff_date:
                        try:
                            os.remove(filepath)
                            logging.info(f"Deleted old file: {filename}")
                            removed_count += 1
                            
                            # Remove from downloaded list
                            if filename in self.downloaded_videos:
                                del self.downloaded_videos[filename]
                        except OSError as e:
                            logging.error(f"Failed to delete {filename}: {e}")
            
            if removed_count > 0:
                logging.info(f"Cleanup complete: removed {removed_count} old files")
                self.save_downloaded_list()
                        
        except Exception as e:
            logging.error(f"Error during cleanup: {e}")
    
    def test_download_capability(self, test_url=None):
        """Test if we can download from video sources - useful for testing setup"""
        if not test_url:
            # Use a test URL based on the website
            test_url = "https://ok.ru/videoembed/10084927212118"
        
        logging.info(f"Testing download capability with: {test_url}")
        
        try:
            # Test format detection
            cmd = ['yt-dlp', '--list