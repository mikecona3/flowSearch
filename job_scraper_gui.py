import re
import threading
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed

import ttkbootstrap as ttk
from ttkbootstrap.dialogs import Messagebox

from geocoding import geocode, haversine_miles
from saved_jobs import SavedJobsStore
from scraper import fetch_jobs, JobPosting

THEME = "flatly"

RESULTS_PER_PAGE = 50
LOCATION_RADIUS_MILES = 25
LOCATION_DEBOUNCE_MS = 600
JOB_SITES = {
    "VibeCode Careers": "https://vibecodecareers.com/jobs/",
    "We Work Remotely": "https://weworkremotely.com/remote-jobs.rss",
    "RemoteOK": "https://remoteok.com/api",
    "Himalayas": "https://himalayas.app/jobs/api",
    "Remotive": "https://remotive.com/api/remote-jobs",
    "Jobicy": "https://jobicy.com/api/v2/remote-jobs",
    "Working Nomads": "https://www.workingnomads.com/api/exposed_jobs/",
    "Arbeitnow": "https://www.arbeitnow.com/api/job-board-api",
    # Page is required -- an omitted `page` param returns a 400, not page 0.
    "The Muse": "https://www.themuse.com/api/public/jobs?page=0",
    "Jobspresso": "https://jobspresso.co/?feed=job_feed",
}

# Matches a quoted phrase (kept whole, quotes included) or a single bare word.
_SEARCH_TOKEN_RE = re.compile(r'"[^"]*"|\'[^\']*\'|\S+')


def _strip_quotes(term: str) -> str:
    term = term.strip()
    if len(term) >= 2 and term[0] == term[-1] and term[0] in "\"'":
        term = term[1:-1].strip()
    return term


def _parse_search_query(raw: str) -> list[list[str]]:
    """Parse search text into OR-groups of AND-terms: every term in a group
    must be found (AND) for at least one group to match (OR). Bare words with
    no operator are implicitly AND'd together (each must appear, not
    necessarily adjacent) -- e.g. "vibe code" requires both "vibe" and
    "code" somewhere in the text. A quoted phrase is kept whole as a single
    literal term, even if it contains the words AND/OR.
    """
    tokens = _SEARCH_TOKEN_RE.findall(raw.strip())
    or_groups: list[list[str]] = [[]]
    for token in tokens:
        if token.upper() == "OR":
            or_groups.append([])
        else:
            or_groups[-1].append(token)

    parsed_groups = []
    for group_tokens in or_groups:
        terms = [
            _strip_quotes(token).lower()
            for token in group_tokens
            if token.upper() != "AND"
        ]
        if terms:
            parsed_groups.append(terms)
    return parsed_groups


class JobScraperApp(ttk.Window):
    def __init__(self):
        super().__init__(
            title="Job Listing Scraper",
            themename=THEME,
            size=(1200, 640),
            minsize=(820, 420),
        )

        self.all_jobs: list[JobPosting] = []
        self.filtered_jobs: list[JobPosting] = []
        # Jobs on the current page, keyed by URL (the table rows' ids).
        self._page_jobs: dict[str, JobPosting] = {}
        self.selected_job: JobPosting | None = None
        self.saved_store = SavedJobsStore()
        # Which list the table shows: "results" (search) or "saved".
        self.view_var = tk.StringVar(value="results")
        self._seen_job_urls: set = set()
        self._fetch_errors: list[str] = []
        self._sources_total = 0
        self._sources_pending = 0
        self.current_page = 0
        self.search_var = tk.StringVar()
        self.location_var = tk.StringVar()
        self._location_debounce_id: str | None = None
        self._location_filter_token = 0
        self.all_sites_var = tk.BooleanVar(value=True)
        self.site_vars: dict[str, tk.BooleanVar] = {
            name: tk.BooleanVar(value=True) for name in JOB_SITES
        }
        self.site_popup: tk.Toplevel | None = None
        self._open_popups: list[tuple[tk.Toplevel, tk.Widget, object]] = []

        self._build_top_bar()
        self._build_results_table()
        self._build_pagination_bar()

        self.search_var.trace_add("write", lambda *_: self._apply_filters())
        self.location_var.trace_add("write", lambda *_: self._on_location_changed())
        self.bind_all("<Button-1>", self._on_global_click, add="+")

    # ---------- UI construction ----------

    def _build_top_bar(self):
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")
        # A shared `uniform` group makes these two columns split the width
        # strictly 3:1 regardless of content size, keeping the controls to
        # 75% of the window (plain column weights only share out leftover
        # space beyond each column's natural size).
        top.columnconfigure(0, weight=3, uniform="top_split")
        top.columnconfigure(1, weight=1, uniform="top_split")

        # Labels share column 0 so the search, location and sources
        # controls all line up on the same left edge.
        form = ttk.Frame(top)
        form.grid(row=0, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="Search:").grid(row=0, column=0, sticky="w")
        search_entry = ttk.Entry(form, textvariable=self.search_var)
        search_entry.grid(row=0, column=1, sticky="ew", padx=8)

        ttk.Label(form, text="Location:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        location_entry = ttk.Entry(form, textvariable=self.location_var)
        location_entry.grid(row=1, column=1, sticky="ew", padx=8, pady=(8, 0))

        ttk.Label(form, text="Sources:").grid(row=2, column=0, sticky="w", pady=(8, 0))
        # Split the field column in two equal halves: the sources dropdown
        # fills the left half (half the width of the boxes above), and the
        # Search Jobs button sits at the start of the right half.
        sources_row = ttk.Frame(form)
        sources_row.grid(row=2, column=1, sticky="ew", padx=8, pady=(8, 0))
        sources_row.columnconfigure(0, weight=1, uniform="sources_split")
        sources_row.columnconfigure(1, weight=1, uniform="sources_split")

        self.site_button = ttk.Button(
            sources_row,
            text=self._site_button_text(),
            command=self._toggle_site_popup,
            bootstyle="secondary-outline",
        )
        self.site_button.grid(row=0, column=0, sticky="ew")

        self.fetch_btn = ttk.Button(
            sources_row, text="Search Jobs", command=self._on_fetch, bootstyle="info"
        )
        self.fetch_btn.grid(row=0, column=1, sticky="w", padx=(8, 0))

    def _build_results_table(self):
        # Table on the left, details for the selected job on the right; the
        # divider between them can be dragged to resize either side.
        view_row = ttk.Frame(self, padding=(10, 0, 10, 6))
        view_row.pack(fill="x")
        ttk.Radiobutton(
            view_row, text="Search Results", variable=self.view_var, value="results",
            command=self._on_view_changed, bootstyle="info-outline-toolbutton",
        ).pack(side="left")
        self.saved_view_btn = ttk.Radiobutton(
            view_row, text=self._saved_view_label(), variable=self.view_var, value="saved",
            command=self._on_view_changed, bootstyle="info-outline-toolbutton",
        )
        self.saved_view_btn.pack(side="left", padx=(6, 0))

        panes = ttk.Panedwindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        self.panes = panes

        columns = ("title", "company", "location")
        self.tree = ttk.Treeview(panes, columns=columns, show="headings")
        for col, label, width in [
            ("title", "Title", 320),
            ("company", "Company", 180),
            ("location", "Location", 160),
        ]:
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, anchor="w")

        self.tree.tag_configure("oddrow", background=self.style.colors.light)
        self.tree.bind("<<TreeviewSelect>>", self._on_row_selected)
        self.tree.bind("<Double-1>", self._on_row_open)
        panes.add(self.tree, weight=3)
        # Built now but only added to the panes once a job is selected (see
        # _set_details_visible), so the table has the full width until then.
        self.details_frame = self._build_details_panel(panes)
        self._show_details(None)

        self.status_var = tk.StringVar(value="Click Search Jobs to get started.")
        ttk.Label(self, textvariable=self.status_var, padding=(10, 0)).pack(
            fill="x", anchor="w"
        )

    def _build_details_panel(self, parent) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=(12, 0, 0, 0))
        colors = self.style.colors
        base_font = tkfont.nametofont("TkDefaultFont").actual()
        family, size = base_font["family"], base_font["size"]

        # A read-only Text widget rather than stacked Labels: it re-wraps
        # long lines on its own as the panel is resized, and scrolls if a
        # listing's details run longer than the window.
        self.details_text = tk.Text(
            frame, wrap="word", relief="flat", borderwidth=0, highlightthickness=0,
            background=colors.bg, foreground=colors.fg, font=(family, size),
            padx=0, pady=4, cursor="arrow", width=34,
        )
        self.details_text.tag_configure(
            "title", font=(family, size + 4, "bold"), spacing3=4
        )
        self.details_text.tag_configure("company", font=(family, size + 1), spacing3=2)
        self.details_text.tag_configure("meta", foreground=colors.secondary, spacing3=2)
        self.details_text.tag_configure("salary", foreground=colors.success, spacing3=2)
        self.details_text.tag_configure(
            "heading", font=(family, size, "bold"), spacing1=12, spacing3=4
        )
        # Hanging indent, so a wrapped bullet lines up under its own text.
        self.details_text.tag_configure("bullet", lmargin1=4, lmargin2=18, spacing3=4)
        self.details_text.tag_configure("placeholder", foreground=colors.secondary)
        self.details_text.pack(fill="both", expand=True)

        buttons = ttk.Frame(frame)
        buttons.pack(anchor="w", pady=(8, 0))
        self.open_posting_btn = ttk.Button(
            buttons, text="Open Job Posting", command=self._open_selected_job,
            bootstyle="info", state="disabled",
        )
        self.open_posting_btn.pack(side="left")
        self.save_btn = ttk.Button(
            buttons, text="Save Job", command=self._toggle_save_selected,
            bootstyle="info-outline", state="disabled",
        )
        self.save_btn.pack(side="left", padx=(8, 0))

        return frame

    def _set_details_visible(self, visible: bool):
        shown = str(self.details_frame) in self.panes.panes()
        if visible and not shown:
            self.panes.add(self.details_frame, weight=1)
        elif not visible and shown:
            self.panes.forget(self.details_frame)

    def _show_details(self, job: "JobPosting | None"):
        self.selected_job = job
        text = self.details_text
        text.config(state="normal")
        text.delete("1.0", "end")
        if job is None:
            text.insert("end", "Select a job to see its details.", "placeholder")
            self.open_posting_btn.config(state="disabled")
            self.save_btn.config(state="disabled", text="Save Job")
        else:
            text.insert("end", job.title + "\n", "title")
            if job.company:
                text.insert("end", job.company + "\n", "company")
            meta = " • ".join(part for part in (job.employment_type, job.location) if part)
            if meta:
                text.insert("end", meta + "\n", "meta")
            if job.salary_range:
                text.insert("end", job.salary_range + "\n", "salary")

            if job.requirements:
                text.insert("end", "Requirements\n", "heading")
                for requirement in job.requirements:
                    text.insert("end", f"•  {requirement}\n", "bullet")
            elif job.summary:
                text.insert("end", "About the role\n", "heading")
                text.insert("end", job.summary + "\n")
            else:
                text.insert("end", "\nNo further details from this source.", "placeholder")
            self.open_posting_btn.config(state="normal" if job.url else "disabled")
            self.save_btn.config(
                state="normal",
                text="Unsave Job" if self.saved_store.is_saved(job) else "Save Job",
            )
        text.config(state="disabled")
        self._set_details_visible(job is not None)

    def _build_pagination_bar(self):
        frame = ttk.Frame(self, padding=(10, 0))
        frame.pack()

        self.prev_btn = ttk.Button(
            frame, text="< Prev", command=self._go_prev_page, bootstyle="secondary-outline"
        )
        self.prev_btn.pack(side="left")

        self.page_label_var = tk.StringVar(value="Page 1 of 1")
        ttk.Label(frame, textvariable=self.page_label_var).pack(side="left", padx=10)

        self.next_btn = ttk.Button(
            frame, text="Next >", command=self._go_next_page, bootstyle="secondary-outline"
        )
        self.next_btn.pack(side="left")

    # ---------- behavior ----------

    def _site_button_text(self) -> str:
        selected = [name for name, var in self.site_vars.items() if var.get()]
        if not selected:
            return "Select sites ▾"
        if len(selected) == len(JOB_SITES):
            return "All sites ▾"
        if len(selected) == 1:
            return f"{selected[0]} ▾"
        return f"{len(selected)} sites selected ▾"

    def _refresh_site_button(self):
        self.site_button.config(text=self._site_button_text())

    def _open_checklist_popup(self, anchor, title, all_var, option_vars, on_toggle, on_close):
        """Build a small popup below `anchor` with an "All" checkbox plus one
        checkbox per entry in `option_vars`. `on_toggle` runs after any
        checkbox changes (e.g. to refresh a button label or re-filter
        results); `on_close` runs when the popup is dismissed."""
        popup = ttk.Toplevel(title=title, transient=self, resizable=(False, False))

        content = ttk.Frame(popup, padding=10)
        content.pack(fill="both", expand=True)

        def on_all_toggle():
            value = all_var.get()
            for var in option_vars.values():
                var.set(value)
            on_toggle()

        def on_option_toggle():
            all_var.set(all(var.get() for var in option_vars.values()))
            on_toggle()

        ttk.Checkbutton(
            content,
            text="All",
            variable=all_var,
            command=on_all_toggle,
            bootstyle="success-round-toggle",
        ).pack(anchor="w", pady=(0, 8))
        ttk.Separator(content, orient="horizontal").pack(fill="x", pady=8)
        for name, var in option_vars.items():
            ttk.Checkbutton(
                content,
                text=name,
                variable=var,
                command=on_option_toggle,
                bootstyle="success-round-toggle",
            ).pack(anchor="w", pady=6)

        ttk.Button(content, text="Done", command=on_close, bootstyle="info").pack(
            pady=(10, 0)
        )

        # Match the popup's width to the button that opened it, so it reads
        # as that control's dropdown -- but never narrower than its contents.
        popup.update_idletasks()
        width = max(anchor.winfo_width(), popup.winfo_reqwidth())
        x = anchor.winfo_rootx()
        y = anchor.winfo_rooty() + anchor.winfo_height()
        popup.geometry(f"{width}x{popup.winfo_reqheight()}+{x}+{y}")

        popup.protocol("WM_DELETE_WINDOW", on_close)
        self._open_popups.append((popup, anchor, on_close))
        return popup

    def _on_global_click(self, event):
        """Close any open checklist popup when a click lands outside both the
        popup itself and the button that opened it (that button's own
        command already handles toggling the popup open/closed)."""
        self._open_popups = [p for p in self._open_popups if p[0].winfo_exists()]
        for popup, anchor, on_close in list(self._open_popups):
            if event.widget is anchor:
                continue
            px, py = popup.winfo_rootx(), popup.winfo_rooty()
            pw, ph = popup.winfo_width(), popup.winfo_height()
            if not (px <= event.x_root <= px + pw and py <= event.y_root <= py + ph):
                on_close()

    def _toggle_site_popup(self):
        if self.site_popup is not None and self.site_popup.winfo_exists():
            self._close_site_popup()
        else:
            self.site_popup = self._open_checklist_popup(
                self.site_button,
                "Select Job Sites",
                self.all_sites_var,
                self.site_vars,
                self._refresh_site_button,
                self._close_site_popup,
            )

    def _close_site_popup(self):
        if self.site_popup is not None:
            self.site_popup.destroy()
            self.site_popup = None

    def _on_fetch(self):
        selected_names = [name for name, var in self.site_vars.items() if var.get()]
        if not selected_names:
            Messagebox.show_warning(
                "Please select at least one job site.", "No site selected"
            )
            return
        self.fetch_btn.config(state="disabled")
        self.view_var.set("results")
        self.all_jobs = []
        self._seen_job_urls = set()
        self._fetch_errors = []
        self._sources_total = len(selected_names)
        self._sources_pending = len(selected_names)
        self._apply_filters()
        label = "all job sites" if len(selected_names) == len(JOB_SITES) else ", ".join(
            selected_names
        )
        self.status_var.set(f"Fetching all jobs from {label} ...")
        threading.Thread(
            target=self._fetch_worker, args=(selected_names,), daemon=True
        ).start()

    def _fetch_worker(self, site_names: list[str]):
        # Fetch every source at once, and hand each one's results to the UI
        # as soon as it finishes, so fast sources show up right away instead
        # of waiting on the slowest one.
        with ThreadPoolExecutor(max_workers=len(site_names)) as pool:
            futures = {
                pool.submit(fetch_jobs, JOB_SITES[name], max_pages=None): name
                for name in site_names
            }
            for future in as_completed(futures):
                try:
                    jobs, error = future.result(), None
                except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
                    jobs, error = [], str(exc)
                self.after(0, self._fetch_source_done, futures[future], jobs, error)

    def _fetch_source_done(self, site_name: str, jobs: list[JobPosting], error: str | None):
        self._sources_pending -= 1
        if error:
            self._fetch_errors.append(f"{site_name}: {error}")
        else:
            new_jobs = [job for job in jobs if job.url not in self._seen_job_urls]
            self._seen_job_urls.update(job.url for job in new_jobs)
            if new_jobs:
                self.all_jobs.extend(new_jobs)
                # Stay on the current page so incoming results don't yank
                # the user back to page 1 while they're browsing.
                self._apply_filters(keep_page=True)

        if self._sources_pending:
            done = self._sources_total - self._sources_pending
            self.status_var.set(
                f"Found {len(self.all_jobs)} postings so far "
                f"({done}/{self._sources_total} sources loaded) ..."
            )
            return

        self.fetch_btn.config(state="normal")
        if self.all_jobs:
            status = f"Found {len(self.all_jobs)} candidate postings."
        else:
            status = "No postings detected on that page (site may need JS rendering)."
        if self._fetch_errors:
            failed = len(self._fetch_errors)
            status += f" {failed} source(s) failed to load."
            Messagebox.show_error("\n".join(self._fetch_errors), "Some sources failed")
        self.status_var.set(status)

    def _on_location_changed(self):
        if self._location_debounce_id is not None:
            self.after_cancel(self._location_debounce_id)
        self._location_debounce_id = self.after(LOCATION_DEBOUNCE_MS, self._apply_filters)

    def _apply_filters(self, keep_page: bool = False):
        self._location_debounce_id = None
        if self.view_var.get() == "saved":
            # Like the iOS Saved Jobs screen, this lists every saved job;
            # the search and location filters apply to search results only.
            self._location_filter_token += 1  # supersede any geocoding in flight
            self.filtered_jobs = list(self.saved_store.jobs)
            if not keep_page:
                self.current_page = 0
            self._render_page()
            self.status_var.set(f"{len(self.filtered_jobs)} saved job(s).")
            return
        search_groups = _parse_search_query(self.search_var.get())

        filtered = []
        for job in self.all_jobs:
            haystack = f"{job.title} {job.company} {job.location}".lower()
            if search_groups and not any(
                all(term in haystack for term in and_terms) for and_terms in search_groups
            ):
                continue
            filtered.append(job)

        self.filtered_jobs = filtered
        if not keep_page:
            self.current_page = 0
        self._render_page()

        self._location_filter_token += 1
        token = self._location_filter_token
        location_query = self.location_var.get().strip()
        if not location_query:
            return
        # "Remote" isn't a place to geocode; treat it as "remote jobs only".
        if location_query.lower() == "remote":
            self.filtered_jobs = [job for job in filtered if job.work_type == "Remote"]
            if not keep_page:
                self.current_page = 0
            self._render_page()
            self.status_var.set(f"Showing {len(self.filtered_jobs)} remote job(s).")
            return

        self.status_var.set(f'Finding jobs within {LOCATION_RADIUS_MILES} mi of "{location_query}" ...')
        threading.Thread(
            target=self._location_filter_worker,
            args=(filtered, location_query, token),
            daemon=True,
        ).start()

    def _location_filter_worker(self, jobs: list[JobPosting], location_query: str, token: int):
        origin = geocode(location_query)
        if origin is None:
            self.after(0, self._location_filter_failed, location_query, token)
            return

        unique_locations = sorted({job.location for job in jobs if job.location})
        total = len(unique_locations)
        coords_by_location: dict[str, "tuple[float, float] | None"] = {}
        for i, loc in enumerate(unique_locations, start=1):
            if token != self._location_filter_token:
                return  # a newer location query superseded this one
            coords_by_location[loc] = geocode(loc)
            self.after(
                0,
                self.status_var.set,
                f'Geocoding locations ({i}/{total}) for "{location_query}" ...',
            )

        matched = [
            job
            for job in jobs
            if coords_by_location.get(job.location) is not None
            and haversine_miles(origin, coords_by_location[job.location])
            <= LOCATION_RADIUS_MILES
        ]

        if token != self._location_filter_token:
            return
        self.after(0, self._location_filter_done, matched, location_query, token)

    def _location_filter_done(self, matched: list[JobPosting], location_query: str, token: int):
        if token != self._location_filter_token:
            return
        self.filtered_jobs = matched
        self.current_page = 0
        self._render_page()
        self.status_var.set(
            f'Showing {len(matched)} job(s) within {LOCATION_RADIUS_MILES} mi of "{location_query}".'
        )

    def _location_filter_failed(self, location_query: str, token: int):
        if token != self._location_filter_token:
            return
        self.status_var.set(f'Could not find a location matching "{location_query}".')

    def _total_pages(self) -> int:
        if not self.filtered_jobs:
            return 1
        return (len(self.filtered_jobs) - 1) // RESULTS_PER_PAGE + 1

    def _render_page(self):
        total_pages = self._total_pages()
        self.current_page = max(0, min(self.current_page, total_pages - 1))

        start = self.current_page * RESULTS_PER_PAGE
        end = start + RESULTS_PER_PAGE
        page_items = self.filtered_jobs[start:end]

        # Rows are keyed by job URL (unique after deduplication), so a row
        # maps straight back to its exact posting.
        self._page_jobs = {job.url: job for job in page_items}
        self.tree.delete(*self.tree.get_children())
        for i, job in enumerate(page_items):
            tags = ("oddrow",) if i % 2 else ()
            self.tree.insert(
                "",
                "end",
                iid=job.url,
                values=(job.title, job.company, job.location),
                tags=tags,
            )

        # Keep the details panel on the selected job across re-renders (e.g.
        # results streaming in), re-highlighting its row if it's on this page,
        # and clear it once that job is no longer among the results at all.
        selected = self.selected_job
        if selected is not None:
            if selected.url in self._page_jobs:
                self.tree.selection_set(selected.url)
            elif selected not in self.filtered_jobs:
                self._show_details(None)

        self.page_label_var.set(f"Page {self.current_page + 1} of {total_pages}")
        self.prev_btn.config(state="normal" if self.current_page > 0 else "disabled")
        self.next_btn.config(
            state="normal" if self.current_page < total_pages - 1 else "disabled"
        )

    def _go_prev_page(self):
        if self.current_page > 0:
            self.current_page -= 1
            self._render_page()

    def _go_next_page(self):
        if self.current_page < self._total_pages() - 1:
            self.current_page += 1
            self._render_page()

    def _on_row_selected(self, _event):
        selected = self.tree.selection()
        # Selection also empties whenever the table is re-rendered; the
        # details panel keeps its job then (see _render_page), rather than
        # blanking out under someone mid-read.
        if selected and selected[0] in self._page_jobs:
            self._show_details(self._page_jobs[selected[0]])

    def _on_row_open(self, event):
        job = self._page_jobs.get(self.tree.identify_row(event.y))
        if job and job.url:
            webbrowser.open(job.url)

    def _saved_view_label(self) -> str:
        return f"Saved Jobs ({len(self.saved_store.jobs)})"

    def _on_view_changed(self):
        self._apply_filters()

    def _toggle_save_selected(self):
        job = self.selected_job
        if job is None:
            return
        self.saved_store.toggle(job)
        self.saved_view_btn.config(text=self._saved_view_label())
        if self.view_var.get() == "saved":
            # Unsaving here drops the job from the list (and clears the panel).
            self._apply_filters(keep_page=True)
        else:
            self._show_details(job)

    def _open_selected_job(self):
        if self.selected_job and self.selected_job.url:
            webbrowser.open(self.selected_job.url)


if __name__ == "__main__":
    app = JobScraperApp()
    app.mainloop()
