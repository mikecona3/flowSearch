# FlowSearch Desktop

A desktop job search app that pulls listings from 10 job boards at once,
then lets you search, filter by location, read job details, and save the
jobs you're interested in, all from one window. It's the desktop companion
to the FlowSearch iOS app and uses the same sources and search rules.

Built with Python and Tkinter ([ttkbootstrap](https://github.com/israel-dryer/ttkbootstrap) theme).
It runs on macOS, Windows and Linux.

## Features

- **10 job sources in one search.** All selected sources are fetched in
  parallel, and results appear as each source finishes, so you can start
  browsing in about a second while slower sources keep loading. Duplicate
  postings are removed.
- **Search with AND / OR / "phrases"** across title, company and location.
- **Location filter** for jobs within 25 miles of a place, or type `remote`
  for remote jobs only.
- **Job details panel.** Select a job to see its employment type, salary,
  and key requirements (or a short summary), with a button to open the
  full posting.
- **Saved jobs.** Save postings to a list that's kept on your computer
  between sessions.

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

### Search syntax

| You type | Matches jobs containing |
|---|---|
| `python developer` | both words, anywhere (AND is implied) |
| `python AND remote` | both terms |
| `python OR java` | either term |
| `"data engineer"` | that exact phrase |
| `python OR "data engineer"` | `python`, or the exact phrase |

Search is case-insensitive and looks at each job's title, company and location.

### Location

- Enter a place (e.g. `Buffalo, NY`) to see jobs within **25 miles**. Jobs
  without a recognizable location, like "Remote" or "Worldwide", are left out.
- Enter `remote` to see only remote jobs.

Places are looked up with OpenStreetMap's free
[Nominatim](https://nominatim.org) service, which allows about one lookup
per second. The first location filter after a big search can take a few
minutes while every job's location is looked up. Lookups are remembered
until the app closes.

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

## Limitations

- **VibeCode Careers is slow.** It has no API and allows only about 30
  page requests a minute, so reading all ~150 pages takes around 6
  minutes. Its jobs arrive after the other sources and include only title,
  company and location.
- **Some sources return only part of their catalog** (Himalayas, The
  Muse: one page each), since their full catalogs run into the tens or hundreds of
  thousands.
- **Job details depend on the source.** Not every posting has a salary or
  a recognizable requirements section.
- **Saved jobs stay on this computer.** Syncing them with the FlowSearch
  iOS app through an account isn't supported yet.
- **Sites can change.** Sources are third-party sites; if one changes its
  feed or page layout, that source may stop returning jobs until the
  scraper is updated.

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

## Project structure

| File | What it does |
|---|---|
| `job_scraper_gui.py` | The app window: search bar, results table, details panel, saved jobs view |
| `scraper.py` | One fetcher per job source, plus the shared `JobPosting` model |
| `job_description.py` | Pulls requirements or a summary out of a posting's description |
| `saved_jobs.py` | Reads and writes the saved jobs file |
| `geocoding.py` | Location lookups (Nominatim) and distance calculation |
