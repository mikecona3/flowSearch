# FlowSearch (for desktop)

A remote job search app that pulls listings from 10 job boards at once,
then lets you search, filter, read job details, and save the
jobs you're interested in, all from one window. It's the desktop companion
to the FlowSearch iOS app and uses the same sources and search rules.

Built with Python and Tkinter ([ttkbootstrap](https://github.com/israel-dryer/ttkbootstrap) theme).
It runs on macOS, Windows and Linux.

## Features

- **10 job sources in one search.**
- **Search with AND / OR / "phrases"**
- **Location filter**
- **Job details** 
- **Saved jobs.**


## Getting started

**Requirements:** Python 3.10 or newer, with Tkinter. The python.org
installers for macOS and Windows include Tkinter. On Linux you may need
to install it separately (e.g. `sudo apt install python3-tk`).

```bash
git clone <this-repo-url>
cd FlowSearch_py
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python3 job_scraper_gui.py
```

## Using the app

1. Pick your sources from the **Sources** dropdown (all are selected by
   default), then click **Search Jobs**.
2. Narrow the results with **Search** and **Location**. Both filter
   instantly; nothing is re-downloaded.
3. Click a job to open its details beside the table. **Open Job Posting**
   (or double-clicking the row) opens the listing in your browser.
4. Click **Save Job** to keep it. Switch to **Saved Jobs** above the table
   to see everything you've saved.


### Saved jobs

Saved jobs are stored with their full details in a JSON file in your user
data folder:

| OS | Location |
|---|---|
| macOS | `~/Library/Application Support/FlowSearch/saved_jobs.json` |
| Windows | `%APPDATA%\FlowSearch\saved_jobs.json` |
| Linux | `~/.local/share/flowsearch/saved_jobs.json` |

The Saved Jobs view lists everything you've saved, newest first. The
Search and Location filters only apply to search results.

## Building a standalone app

To build an app that runs without Python installed:

```bash
pip install pyinstaller
pyinstaller --windowed --onefile --name "FlowSearch" job_scraper_gui.py
```

The app is created under `dist/` for the OS you build on (a `.app` on
macOS, an `.exe` on Windows). PyInstaller doesn't cross-compile, so build
on each OS you want to support. On macOS the app is unsigned: open it the
first time with right-click → **Open**, or Gatekeeper will block it.
