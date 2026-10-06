"""Saved jobs, kept in a JSON file on this computer.

The iOS app syncs saved jobs through a signed-in Firebase account; this is
the local-only equivalent for now. Each saved job is stored whole (details
included), so the Saved Jobs view still works without re-fetching anything.
"""
import json
import os
import sys
from dataclasses import asdict, fields
from pathlib import Path

from scraper import JobPosting

_FILE_NAME = "saved_jobs.json"
_JOB_FIELDS = {f.name for f in fields(JobPosting)}


def _data_dir() -> Path:
    """The per-user app data folder for this OS."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "FlowSearch"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home()) / "FlowSearch"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "flowsearch"


class SavedJobsStore:
    """Saved jobs, newest first, keyed by URL (unique per posting)."""

    def __init__(self, path: "Path | None" = None):
        self.path = path or _data_dir() / _FILE_NAME
        self.jobs: list[JobPosting] = self._load()

    def is_saved(self, job: JobPosting) -> bool:
        return any(saved.url == job.url for saved in self.jobs)

    def toggle(self, job: JobPosting):
        if self.is_saved(job):
            self.unsave(job)
        else:
            self.save(job)

    def save(self, job: JobPosting):
        if not self.is_saved(job):
            self.jobs.insert(0, job)
            self._write()

    def unsave(self, job: JobPosting):
        remaining = [saved for saved in self.jobs if saved.url != job.url]
        if len(remaining) != len(self.jobs):
            self.jobs = remaining
            self._write()

    def _load(self) -> list[JobPosting]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            # Unreadable or corrupt: set it aside rather than silently
            # overwriting it on the next save, then start fresh.
            try:
                self.path.replace(self.path.with_suffix(".json.corrupt"))
            except OSError:
                pass
            return []
        jobs = []
        for entry in raw if isinstance(raw, list) else []:
            if not isinstance(entry, dict):
                continue
            try:
                # Ignore unknown keys, so a file written by a newer version
                # (with extra fields) still loads.
                jobs.append(JobPosting(**{k: v for k, v in entry.items() if k in _JOB_FIELDS}))
            except TypeError:
                continue  # missing required fields
        return jobs

    def _write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file and swap it in, so a crash mid-write can't
        # leave a half-written (and so unreadable) saved jobs file.
        tmp_path = self.path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps([asdict(job) for job in self.jobs], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, self.path)
