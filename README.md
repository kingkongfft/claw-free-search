# Kimi AI Search Scripts

Python scripts for authenticating and searching with Kimi AI.

## Installation

```bash
pip install -r requirements.txt
playwright install chromium
```

## Usage

### Method 1: Simple Script

```bash
# First time login
python kimi_search.py --login

# Search
python kimi_search.py "your search query here"
```

### Method 2: Interactive Script

```bash
python kimi_auth.py
```

This will show a menu with options:
1. Test search
2. Re-login
3. Exit

## How It Works

1. **Login**: Opens a browser window where you can manually login to Kimi
2. **Save**: Captures and saves authentication cookies to `kimi_cookies.json`
3. **Search**: Uses saved cookies to authenticate and perform searches

## Files

- `kimi_search.py` - Simple script for quick searches
- `kimi_auth.py` - Interactive script with menu
- `kimi_cookies.json` - Saved authentication cookies (created after login)
- `kimi_auth.json` - Auth data (created after login)
- `requirements.txt` - Python dependencies

## Notes

- You need to manually login the first time (no password storage for security)
- Cookies are saved locally and reused for subsequent searches
- If authentication expires, run `--login` again
