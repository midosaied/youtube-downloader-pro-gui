# YouTube Downloader Pro

A modern desktop GUI (built with CustomTkinter) for downloading audio and video from YouTube, powered by [yt-dlp](https://github.com/yt-dlp/yt-dlp). Also works as a headless command-line tool.

## Features

- Audio and video downloads with selectable quality (e.g. `320k`, `1080p`, `best`)
- Playlist support and multiple concurrent downloads
- Resumable downloads and history (skips files you already downloaded)
- Automatic retries with friendly error messages
- Embedded metadata and thumbnails
- Optional subtitles and SponsorBlock segment removal
- Cookies from browser (Chrome / Firefox / Edge / Brave) for restricted videos
- Auto-detects and installs missing Python packages and ffmpeg
- Automatic yt-dlp update check (once every 24 hours)

## Requirements

- Python 3.9+
- Tkinter (bundled with Python on Windows/macOS; on Linux: `sudo apt install python3-tk`)
- ffmpeg (installed automatically if missing, or install it manually)

  
## Download (Windows)

Grab the latest `YouTubeDownloaderPro.exe` from the [Releases page](https://github.com/midosaied/youtube-downloader-pro-gui/releases/latest). No Python needed.
## Installation

```bash
git clone https://github.com/midosaied/youtube-downloader-pro-gui.git
cd youtube-downloader-pro-gui
pip install -r requirements.txt
```

> The app also installs missing packages by itself on first launch.

## Usage

### GUI

```bash
python youtube_downloader_pro.py
```

### Command line (headless)

```bash
python youtube_downloader_pro.py -u "https://www.youtube.com/watch?v=VIDEO_ID" -f audio -q 320k
python youtube_downloader_pro.py -u URL1 URL2 -f video -q 1080p -o ~/Downloads --workers 3
```

| Option | Description |
|---|---|
| `-u, --urls` | One or more YouTube links (enables headless mode) |
| `-f, --format` | `audio` or `video` |
| `-q, --quality` | Quality, e.g. `320k`, `1080p`, or `best` |
| `-o, --output` | Output folder |
| `--no-skip` | Re-download files already in history |
| `--workers` | Number of concurrent downloads |

Default folders: `~/YouTube/Audio` and `~/YouTube/Video`.

## Troubleshooting

- **"Requested format is not available"**: yt-dlp is outdated. Run `pip install -U yt-dlp`.
- **ffmpeg not found**: install it manually
  - Windows: `winget install -e --id Gyan.FFmpeg`
  - macOS: `brew install ffmpeg`
  - Linux: `sudo apt install ffmpeg`
- **Age-restricted / login-required videos**: enable "cookies from browser" in settings.

## Disclaimer

This tool is for personal use only. Downloading content may violate YouTube's Terms of Service or copyright law in your country. You are responsible for how you use it. Only download content you own or have permission to download.

## License

[MIT](LICENSE)
