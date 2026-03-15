#!/usr/bin/env python3
"""
Sports Replay Downloader
Monitors replay websites for new videos and downloads them
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
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# Configuration
WEBSITE_URL = "https://basketballreplays.net/"
DOWNLOAD_PATH = "/mnt/storage"
CHECK_INTERVAL = 300  # 5 minutes in seconds (changed from 30min)
RETENTION_DAYS = 7
DATA_FILE = "/var/lib/basketball-downloader/basketball_downloads.json"
KNOWN_LINKS_FILE = "/var/lib/basketball-downloader/basketball_known_links.json"
START_DATE = (datetime.now() - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
LOG_FILE = "/var/log/basketball_downloader.log"
MAX_DOWNLOAD_TIME = 36000  # 5 hours timeout for downloads (5 * 60 * 60 = 18000)
MAX_FILE_SIZE = 10 * 1024 * 1024 * 1024  # 10GB max file size
MAX_CONCURRENT_DOWNLOADS = 5  # Maximum simultaneous downloads

#Apply team filter (keyword search)
TEAMS_TO_DOWNLOAD = ['Spurs', 'Mavericks', 'Nuggets', 'Pistons']  # Case-insensitive matching
#Download all teams:
#TEAMS_TO_DOWNLOAD = []
# or
# TEAMS_TO_DOWNLOAD = None

# User agents for rotation
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0'
]

# Setup logging with rotation
from logging.handlers import RotatingFileHandler
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
        self.session = self.create_session()
        
        # Create data directory if it doesn't exist
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        
        self.downloaded_videos = self.load_downloaded_list()
        self.known_links = self.load_known_links()
        
        # Thread lock for safe concurrent access to shared data
        self.data_lock = threading.Lock()
        
        # Create download directory if it doesn't exist
        os.makedirs(DOWNLOAD_PATH, exist_ok=True)
        
        # Only clean up old partial downloads (keep recent ones for resume)
        self.cleanup_old_partial_downloads()
        
        logging.info(f"Starting from: {START_DATE.strftime('%Y-%m-%d %H:%M:%S')}")
        logging.info("Will only download videos discovered after this time")
        logging.info(f"Max concurrent downloads: {MAX_CONCURRENT_DOWNLOADS}")
        if TEAMS_TO_DOWNLOAD:
            logging.info(f"Team filter enabled: {', '.join(TEAMS_TO_DOWNLOAD)}")
        else:
            logging.info("Team filter disabled: downloading all games")

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
        import urllib3
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
                if filename.endswith(('.part', '.ytdl', '.temp', '.downloading')):
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
        """Save the list of known links (should be called within a locked section)"""
        try:
            # Note: This should be called from within a locked section
            # Don't acquire lock here to avoid double-locking
            with open(KNOWN_LINKS_FILE, 'w') as f:
                json.dump(self.known_links, f, indent=2)
            logging.info(f"Known links saved successfully ({len(self.known_links)} entries)")
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
        """Save the list of downloaded videos (thread-safe)"""
        try:
            # Note: This should be called from within a locked section
            # Don't acquire lock here to avoid double-locking
            with open(DATA_FILE, 'w') as f:
                json.dump(self.downloaded_videos, f, indent=2)
            logging.info(f"Downloaded list saved successfully ({len(self.downloaded_videos)} entries)")
        except Exception as e:
            logging.error(f"Error saving downloaded list: {e}")
        
    def is_new_video(self, video_url, title, pub_date=None):
        """Check if this is a new video based on publication date"""
        
        # Skip obviously non-video links
        if any(skip in title.lower() for skip in ['home', 'about', 'contact', 'category', 'tag']):
            return False
        
        # Check team filter
        if not self.matches_team_filter(title):
            logging.info(f"Skipping video (team filter): {title}")
            return False
        
        # If we have a publication date, use it to filter old videos
        if pub_date:
            if pub_date < START_DATE:
                logging.info(f"Skipping old video from {pub_date.strftime('%Y-%m-%d')}: {title}")
                return False      
        if video_url not in self.known_links:
            self.known_links[video_url] = {
                'title': title,
                'discovered_date': datetime.now().isoformat(),
                'pub_date': pub_date.isoformat() if pub_date else None,
                'processed': False
            }
            self.save_known_links()
            
            # If we have pub_date, use it; otherwise fall back to discovery date
            if pub_date:
                return pub_date >= START_DATE
            else:
                return datetime.now() >= START_DATE
        
        link_info = self.known_links[video_url]
        
        # Check if already processed
        if link_info.get('processed', False):
            return False
        
        # Use pub_date if available in stored data
        if 'pub_date' in link_info and link_info['pub_date']:
            try:
                stored_pub_date = datetime.fromisoformat(link_info['pub_date'])
                return stored_pub_date >= START_DATE
            except:
                pass
        
        # Fall back to discovered_date
        discovered_date = datetime.fromisoformat(link_info['discovered_date'])
        return discovered_date >= START_DATE
    
    def mark_video_processed(self, video_url):
        """Mark a video as processed (thread-safe)"""
        with self.data_lock:
            if video_url in self.known_links:
                self.known_links[video_url]['processed'] = True
                self.save_known_links()
    
    def matches_team_filter(self, title):
        """Check if video title contains any of the teams we want to download"""
        if not TEAMS_TO_DOWNLOAD:
            return True  # No filter, download everything
        
        title_lower = title.lower()
        for team in TEAMS_TO_DOWNLOAD:
            if team.lower() in title_lower:
                return True
        
        return False

    def extract_date_from_title(self, title):
        """Extract date from video title (e.g., '13 June 2025' or 'June 13, 2025')"""
        try:
            # Common date patterns in titles
            patterns = [
                r'(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})',
                r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})',
                r'(\d{4})-(\d{2})-(\d{2})',  # YYYY-MM-DD
                r'(\d{2})/(\d{2})/(\d{4})',  # MM/DD/YYYY
            ]
            
            for pattern in patterns:
                match = re.search(pattern, title, re.IGNORECASE)
                if match:
                    groups = match.groups()
                    
                    # Pattern 1: "13 June 2025"
                    if len(groups) == 3 and groups[1].isalpha():
                        day = int(groups[0])
                        month_name = groups[1]
                        year = int(groups[2])
                        month = datetime.strptime(month_name, '%B').month
                        return datetime(year, month, day)
                    
                    # Pattern 2: "June 13, 2025"
                    elif len(groups) == 3 and groups[0].isalpha():
                        month_name = groups[0]
                        day = int(groups[1])
                        year = int(groups[2])
                        month = datetime.strptime(month_name, '%B').month
                        return datetime(year, month, day)
                    
                    # Pattern 3: "2025-06-13"
                    elif len(groups) == 3 and groups[0].isdigit() and len(groups[0]) == 4:
                        year = int(groups[0])
                        month = int(groups[1])
                        day = int(groups[2])
                        return datetime(year, month, day)
                    
                    # Pattern 4: "06/13/2025"
                    elif len(groups) == 3 and all(g.isdigit() for g in groups):
                        month = int(groups[0])
                        day = int(groups[1])
                        year = int(groups[2])
                        return datetime(year, month, day)
            
            return None
        except Exception as e:
            logging.debug(f"Could not extract date from title '{title}': {e}")
            return None
    
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
                # Try to extract publication date from the post
                pub_date = None
                
                # Look for time tags with datetime attribute
                time_tag = post.find('time', {'datetime': True})
                if time_tag:
                    try:
                        pub_date = datetime.fromisoformat(time_tag['datetime'].replace('Z', '+00:00'))
                    except:
                        pass
                
                # Look for common date classes if time tag not found
                if not pub_date:
                    date_elements = post.find_all(class_=lambda x: x and any(
                        keyword in x.lower() for keyword in ['date', 'published', 'time', 'posted']
                    ))
                    for elem in date_elements:
                        date_text = elem.get_text(strip=True)
                        # Try common date formats
                        for fmt in ['%B %d, %Y', '%b %d, %Y', '%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y']:
                            try:
                                pub_date = datetime.strptime(date_text, fmt)
                                break
                            except:
                                continue
                        if pub_date:
                            break
                
                # If still no date found, try to parse from URL or content
                if not pub_date:
                    # Look for dates in format YYYY/MM/DD in URLs
                    for link in post.find_all('a', href=True):
                        href = link.get('href')
                        date_match = re.search(r'/(\d{4})/(\d{2})/(\d{2})/', href)
                        if date_match:
                            try:
                                pub_date = datetime(int(date_match.group(1)), 
                                                   int(date_match.group(2)), 
                                                   int(date_match.group(3)))
                                break
                            except:
                                pass
                
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
                    if (any(keyword in text.lower() for keyword in 
                           ['vs', 'v.', 'game', 'replay', 'nba', 'basketball', 'highlights', 'final']) and
                        urlparse(WEBSITE_URL).netloc in full_url and
                        len(text) > 15):  # Longer titles are more likely to be games
                        
                        # Try to extract date from title first, fall back to pub_date from HTML
                        title_date = self.extract_date_from_title(text)
                        final_date = title_date if title_date else pub_date
                        
                        video_links.append({
                            'title': text,
                            'url': full_url,
                            'pub_date': final_date  # Prefer title date over HTML date
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

    def find_video_link(self, video_page_url):
            """Find video link on a video page - any platform or format"""
            
            VIDEO_DOMAINS = [
                'ok.ru', 'dailymotion.com', 'youtube.com', 'youtu.be',
                'vimeo.com', 'streamable.com', 'twitch.tv', 'rumble.com',
                'facebook.com', 'fb.watch'
            ]
            VIDEO_EXTENSIONS = ['.mp4', '.mkv', '.avi', '.mov', '.m3u8', '.ts', '.webm']

            def find_in_text(text):
                """Search raw text for any video link"""
                for domain in VIDEO_DOMAINS:
                    match = re.search(rf'https?://[^"\'<>\s]*{re.escape(domain)}[^"\'<>\s]*', text)
                    if match:
                        return match.group(0)
                for ext in VIDEO_EXTENSIONS:
                    match = re.search(rf'https?://[^"\'<>\s]*\{ext}[^"\'<>\s]*', text)
                    if match:
                        return match.group(0)
                return None

            try:
                time.sleep(random.uniform(2, 4))
                self.session.headers['User-Agent'] = random.choice(USER_AGENTS)

                response = self.session.get(video_page_url, timeout=30, allow_redirects=True)
                response.raise_for_status()

                # Search the game page directly first
                result = find_in_text(response.text)
                if result:
                    logging.info(f"Found video link directly on page: {result}")
                    return result

                # Nothing found directly - follow any external links on the page
                logging.info(f"No direct video link found, checking intermediate pages...")
                soup = BeautifulSoup(response.content, 'html.parser')
                for link in soup.find_all('a', href=True):
                    href = link.get('href')
                    if not href or urlparse(WEBSITE_URL).netloc in href:
                        continue
                    if href.startswith('http') and not any(href.endswith(ext) for ext in ['.jpg', '.png', '.css', '.js']):
                        try:
                            time.sleep(random.uniform(1, 2))
                            inter_response = self.session.get(href, timeout=30, allow_redirects=True)
                            result = find_in_text(inter_response.text)
                            if result:
                                logging.info(f"Found video link via intermediate page: {href}")
                                return result
                        except Exception as e:
                            logging.debug(f"Failed to fetch intermediate page {href}: {e}")
                            continue

                logging.warning(f"No video link found on page")
                return None

            except requests.Timeout:
                logging.error(f"Timeout finding video link on {video_page_url}")
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
    


    def download_video(self, video_link, title):
        """Download video using yt-dlp with resume capability"""
        try:
            filename = self.sanitize_filename(title)
            filepath = os.path.join(DOWNLOAD_PATH, filename)
            temp_filepath = filepath + '.downloading'
            
            # Check if file already exists and is complete
            if os.path.exists(filepath):
                file_size = os.path.getsize(filepath)
                if file_size > 10 * 1024 * 1024:  # At least 10MB for complete videos
                    # Verify the file is actually complete by checking with ffprobe
                    try:
                        verify_cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', filepath]
                        verify_result = subprocess.run(verify_cmd, capture_output=True, text=True, timeout=10)
                        if verify_result.returncode == 0 and verify_result.stdout.strip():
                            logging.info(f"File already exists and is valid: {filename} ({file_size / (1024*1024):.1f} MB)")
                            # Return special marker to indicate file already exists
                            return 'already_exists'
                        else:
                            logging.warning(f"File exists but appears incomplete, will re-download: {filename}")
                            os.remove(filepath)
                    except:
                        # If ffprobe fails, assume file is complete
                        logging.info(f"File already exists: {filename} ({file_size / (1024*1024):.1f} MB)")
                        # Return special marker to indicate file already exists
                        return 'already_exists'
                else:
                    # Remove small incomplete file
                    logging.info(f"Removing incomplete file: {filename} ({file_size} bytes)")
                    os.remove(filepath)
            
            # Check for existing .downloading marker
            if os.path.exists(temp_filepath):
                temp_size = os.path.getsize(temp_filepath)
                logging.info(f"Found incomplete download: {filename} ({temp_size / (1024*1024):.1f} MB) - will resume")
            
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
            
            # Download using yt-dlp with compatible format selection
            # Use temp file to track incomplete downloads
            cmd = [
                'yt-dlp',
                '--no-playlist',
                '--format', 'bestvideo+bestaudio/best',  # specific formats, best to worst
                '--output', temp_filepath,
                '--continue',  # Continue partial downloads
                '--retries', '15',  # Increased retries
                '--fragment-retries', '15',
                '--retry-sleep', '2',  # Faster retry
                '--socket-timeout', '60',  # Longer socket timeout
                '--no-check-certificates',  # Help with SSL issues
                '--concurrent-fragments', '16',  # Increased from 8 to 16 fragments
                '--hls-use-mpegts',  # Better for live/long streams
                '--no-warnings',
                '--progress',  # Show progress for monitoring
                '--newline',  # Better for log parsing
                '--buffer-size', '64K',  # Doubled buffer size
                '--http-chunk-size', '20M',  # Doubled chunk size
                '--throttled-rate', '50K',  # More aggressive throttle detection
                '--external-downloader', 'aria2c',  # Use aria2c for faster downloads
                '--external-downloader-args', 'aria2c:--max-connection-per-server=16 --split=16 --min-split-size=1M --max-concurrent-downloads=16',
                okru_url
            ]
            
            logging.info(f"Downloading: {title}")
            logging.info(f"Output: {temp_filepath}")
            
            result = subprocess.run(
                cmd, 
                capture_output=True, 
                text=True, 
                timeout=MAX_DOWNLOAD_TIME
            )
            
            if result.returncode == 0:
                if os.path.exists(temp_filepath):
                    file_size = os.path.getsize(temp_filepath)
                    if file_size > 10 * 1024 * 1024:  # At least 10MB for complete videos
                        # Rename from .downloading to final filename
                        os.rename(temp_filepath, filepath)
                        subprocess.run(['chmod', '644', filepath])
                        logging.info(f"Successfully downloaded: {filename} ({file_size / (1024*1024):.1f} MB)")
                        
                        # Clean up any remaining partial files for this video
                        for existing in os.listdir(DOWNLOAD_PATH):
                            if (existing.startswith(filename.replace('.mp4', '')) and 
                                existing != filename and
                                existing.endswith(('.part', '.ytdl', '.temp', '.downloading'))):
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
                if os.path.exists(temp_filepath):
                    partial_size = os.path.getsize(temp_filepath)
                    if partial_size > 1024 * 1024:  # At least 1MB
                        logging.info(f"Partial download available for resume: {temp_filepath} ({partial_size / (1024*1024):.1f} MB)")
                
                return False
                
        except subprocess.TimeoutExpired:
            logging.error(f"Download timeout ({MAX_DOWNLOAD_TIME/3600:.1f}h) for {title}")
            
            # Don't kill the process immediately - let it finish current fragment
            logging.info("Download timed out, but partial download will be available for resume")
            
            # Check what we have so far
            if os.path.exists(temp_filepath):
                partial_size = os.path.getsize(temp_filepath)
                if partial_size > 1024 * 1024:
                    logging.info(f"Partial download saved: {temp_filepath} ({partial_size / (1024*1024):.1f} MB)")
            
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






    def process_single_video(self, video):
        """Process a single video download (used for parallel execution)"""
        title = video['title']
        video_url = video['url']
        pub_date = video.get('pub_date')
        filename = self.sanitize_filename(title)
        
        try:
            # Log with publication date if available
            if pub_date:
                logging.info(f"Processing new video from {pub_date.strftime('%Y-%m-%d')}: {title}")
            else:
                logging.info(f"Processing new video: {title}")
            
            # Find video link
            video_link = self.find_video_link(video_url)
            
            if okru_url:
                logging.info(f"Starting download check for: {title}")
                download_result = self.download_video(okru_url, title)
                logging.info(f"Download result for {title}: {download_result}")
                
                # Handle both successful download and already-exists case
                if download_result == True or download_result == 'already_exists':
                    # File is ready (either just downloaded or already existed)
                    logging.info(f"Acquiring lock to save: {filename}")
                    with self.data_lock:
                        logging.info(f"Lock acquired, saving: {filename}")
                        self.downloaded_videos[filename] = {
                            'title': title,
                            'download_date': datetime.now().isoformat(),
                            'pub_date': pub_date.isoformat() if pub_date else None,
                            'source_url': video_url,
                            'video_url': video_link
                        }
                        self.save_downloaded_list()
                        logging.info(f"Saved to database: {filename}")
                    
                    # Mark as processed (outside the lock to avoid deadlock)
                    logging.info(f"Marking as processed: {video_url}")
                    self.mark_video_processed(video_url)
                    logging.info(f"Successfully marked as processed: {video_url}")
                    
                    if download_result == 'already_exists':
                        return {'status': 'already_exists', 'title': title}
                    else:
                        return {'status': 'success', 'title': title}
                else:
                    logging.error(f"Failed to download: {title}")
                    return {'status': 'failed', 'title': title}
            else:
                logging.warning(f"No video link found for: {title}")
                self.mark_video_processed(video_url)
                return {'status': 'no_link', 'title': title}
                
        except Exception as e:
            logging.error(f"Error processing video {title}: {e}", exc_info=True)
            return {'status': 'error', 'title': title, 'error': str(e)}

    def process_videos(self):
        """Main processing function"""
        # Prevent multiple instances from running simultaneously
        lock_file = os.path.join(os.path.dirname(DATA_FILE), "downloader.lock")
        import fcntl
        
        lock_fd = None
        try:
            lock_fd = open(lock_file, 'w')
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except IOError:
            logging.warning("Another instance is already running. Exiting.")
            return
        
        try:
            start_time = datetime.now()
            logging.info("Starting sports downloader run")
            logging.info("=" * 50)
            
            # First, check for resumable downloads
            resumable_downloads = self.get_resumable_downloads()
            if resumable_downloads:
                logging.info("=" * 30)
                logging.info("RESUMABLE DOWNLOADS FOUND:")
                for item in resumable_downloads:
                    logging.info(f"  - {item['original_name']} ({item['size_mb']:.1f} MB, {item['modified'].strftime('%Y-%m-%d %H:%M')})")
                logging.info("These will be resumed automatically when the same video is encountered")
                logging.info("=" * 30)
            
            video_links = self.get_video_links()
            new_downloads = 0
            processed_count = 0
            failed_count = 0
            
            # Filter to only new videos that need processing
            videos_to_process = []
            for video in video_links:
                filename = self.sanitize_filename(video['title'])
                pub_date = video.get('pub_date')

                # Skip if already in downloaded list
                if filename in self.downloaded_videos:
                    logging.debug(f"Skipping already downloaded: {video['title']}")
                    continue

                # Skip if doesn't pass team filter or date filter
                if not self.is_new_video(video['url'], video['title'], pub_date):
                    continue
                videos_to_process.append(video)

            if not videos_to_process:
                logging.info("No new videos found to download")
            else:
                logging.info(f"Found {len(videos_to_process)} new videos to download")
                logging.info(f"Processing videos with {MAX_CONCURRENT_DOWNLOADS} concurrent downloads...")
                
                # Process videos in parallel using ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_DOWNLOADS) as executor:
                    # Submit all videos for processing
                    future_to_video = {
                        executor.submit(self.process_single_video, video): video 
                        for video in videos_to_process
                    }
                    
                    # Process results as they complete
                    for future in as_completed(future_to_video):
                        video = future_to_video[future]
                        try:
                            result = future.result()
                            if result:
                                if result['status'] == 'success':
                                    new_downloads += 1
                                    processed_count += 1
                                    logging.info(f"✓ Completed: {result['title']}")
                                elif result['status'] == 'already_exists':
                                    processed_count += 1
                                    logging.info(f"✓ Already existed: {result['title']}")
                                elif result['status'] == 'failed':
                                    failed_count += 1
                                    processed_count += 1
                                    logging.warning(f"✗ Failed: {result['title']}")
                                elif result['status'] == 'no_link':
                                    processed_count += 1
                                    logging.info(f"⊘ No link: {result['title']}")
                        except Exception as e:
                            logging.error(f"Exception processing video {video['title']}: {e}", exc_info=True)
                            failed_count += 1
                            processed_count += 1

                logging.info("All downloads completed, executor closed")

            # Log summary
            if new_downloads > 0:
                logging.info(f"Successfully downloaded {new_downloads} new videos")
            if failed_count > 0:
                logging.warning(f"{failed_count} downloads failed")
            
            # Always run cleanup
            self.cleanup_old_files()
            
            # Log summary
            end_time = datetime.now()
            duration = end_time - start_time
            logging.info(f"Run completed in {duration}. Processed {processed_count} videos, downloaded {new_downloads}")
            
        except Exception as e:
            logging.error(f"Error in main process: {e}", exc_info=True)
        
        finally:
            # Always release the lock and clean up
            if lock_fd:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    lock_fd.close()
                except Exception as e:
                    logging.error(f"Error releasing lock: {e}")
            
            # Remove lock file
            try:
                if os.path.exists(lock_file):
                    os.remove(lock_file)
            except Exception as e:
                logging.error(f"Error removing lock file: {e}")
            
            logging.info("=" * 50)

if __name__ == "__main__":
    import sys
    
    try:
        downloader = SportsDownloader()
        downloader.process_videos()
            
    except KeyboardInterrupt:
        logging.info("Script interrupted by user")
    except Exception as e:
        logging.error(f"Fatal error: {e}", exc_info=True)
