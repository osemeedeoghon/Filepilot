"use strict";

(() => {
  const PAGE_SIZE = 300;

  const $ = (id) => document.getElementById(id);
  const els = {
    form: $("scan-form"),
    path: $("path-input"),
    choose: $("choose-btn"),
    scan: $("scan-btn"),
    hidden: $("hidden-toggle"),
    status: $("status"),
    statusTitle: $("status-title"),
    statusDetail: $("status-detail"),
    results: $("results"),
    mFiles: $("m-files"),
    mFolders: $("m-folders"),
    mSize: $("m-size"),
    mSkipped: $("m-skipped"),
    notices: $("notices"),
    search: $("search-input"),
    typeFilter: $("type-filter"),
    matchCount: $("match-count"),
    rows: $("file-rows"),
    emptyFilter: $("empty-filter"),
    more: $("more-btn"),
    skippedPanel: $("skipped-panel"),
    skippedList: $("skipped-list"),
    headers: document.querySelectorAll("th[data-sort]"),
  };

  const state = {
    files: [],
    filtered: [],
    shown: PAGE_SIZE,
    sortKey: "relative_path",
    sortDir: 1,
    busy: false,
  };

  const numberFmt = new Intl.NumberFormat();
  const dateFmt = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

  function formatBytes(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    const units = ["KB", "MB", "GB", "TB"];
    let value = bytes;
    let i = -1;
    do { value /= 1024; i++; } while (value >= 1024 && i < units.length - 1);
    return `${value.toFixed(value >= 100 ? 0 : 1)} ${units[i]}`;
  }

  function setStatus(kind, title, detail) {
    els.status.className = `status status-${kind}`;
    els.statusTitle.textContent = title;
    els.statusDetail.textContent = detail || "";
  }

  function setBusy(busy) {
    state.busy = busy;
    els.scan.disabled = busy;
    els.choose.disabled = busy;
    els.path.disabled = busy;
    els.scan.textContent = busy ? "Scanning…" : "Scan";
  }

  async function postJSON(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    let data = null;
    try { data = await res.json(); } catch { /* non-JSON body */ }
    if (!res.ok) {
      let message = data?.error?.message;
      if (!message && Array.isArray(data?.detail)) message = "The request was not valid. Check the folder path.";
      throw new Error(message || `Request failed (${res.status}).`);
    }
    return data;
  }

  // Client-side checks mirror the server's, for faster feedback only.
  function validatePath(value) {
    const v = value.trim();
    if (!v) return "Enter or choose a folder to scan.";
    if (v.length > 1024) return "That path is too long.";
    if (!(v.startsWith("/") || v === "~" || v.startsWith("~/"))) return "Use a full path that starts with /.";
    if (v.split("/").includes("..")) return "Folder paths may not contain '..'.";
    return null;
  }

  async function runScan() {
    if (state.busy) return;
    const problem = validatePath(els.path.value);
    if (problem) {
      els.path.setAttribute("aria-invalid", "true");
      setStatus("error", "Can’t scan", problem);
      els.path.focus();
      return;
    }
    els.path.removeAttribute("aria-invalid");
    setBusy(true);
    setStatus("running", "Scanning…", els.path.value.trim());
    try {
      const data = await postJSON("/api/scan", {
        path: els.path.value.trim(),
        include_hidden: els.hidden.checked,
      });
      showResults(data);
    } catch (err) {
      els.results.hidden = true;
      els.path.setAttribute("aria-invalid", "true");
      setStatus("error", "Scan failed", err.message || "Unexpected error.");
    } finally {
      setBusy(false);
    }
  }

  function showResults(data) {
    state.files = data.files;
    state.root = data.root;
    state.shown = PAGE_SIZE;
    els.path.value = data.root;

    els.mFiles.textContent = numberFmt.format(data.total_files);
    els.mFolders.textContent = numberFmt.format(data.total_folders);
    els.mSize.textContent = formatBytes(data.total_size);
    els.mSkipped.textContent = numberFmt.format(data.skipped_count);

    // Notices for partial scans.
    els.notices.replaceChildren();
    const notes = [];
    if (data.truncated) notes.push(`${data.truncated_reason} Results are partial; choose a smaller folder for a full listing.`);
    if (data.depth_limited) notes.push("Some deeply nested folders were not opened because of the folder-depth limit.");
    if (data.symlinks) notes.push(`${numberFmt.format(data.symlinks)} symbolic link(s) were listed but not followed.`);
    if (data.skipped_count) notes.push(`${numberFmt.format(data.skipped_count)} item(s) could not be read, usually because of macOS permissions.`);
    for (const text of notes) {
      const div = document.createElement("div");
      div.className = "notice";
      div.textContent = text;
      els.notices.append(div);
    }

    // Skipped items.
    els.skippedList.replaceChildren();
    for (const item of data.skipped) {
      const li = document.createElement("li");
      const code = document.createElement("code");
      code.textContent = item.relative_path;
      li.append(code, ` — ${item.reason}`);
      els.skippedList.append(li);
    }
    if (data.skipped_count > data.skipped.length) {
      const li = document.createElement("li");
      li.textContent = `…and ${numberFmt.format(data.skipped_count - data.skipped.length)} more.`;
      els.skippedList.append(li);
    }
    els.skippedPanel.hidden = data.skipped.length === 0;

    // Type filter options.
    const counts = new Map();
    for (const f of data.files) counts.set(f.category, (counts.get(f.category) || 0) + 1);
    const previous = els.typeFilter.value;
    els.typeFilter.replaceChildren(new Option("All types", ""));
    for (const [cat, n] of [...counts].sort((a, b) => a[0].localeCompare(b[0]))) {
      els.typeFilter.append(new Option(`${cat} (${numberFmt.format(n)})`, cat));
    }
    els.typeFilter.value = counts.has(previous) ? previous : "";

    els.results.hidden = false;
    const secs = (data.duration_ms / 1000).toFixed(1);
    if (data.files.length === 0) {
      setStatus("empty", "Scan complete: no files found",
        data.total_folders ? `Found ${numberFmt.format(data.total_folders)} folder(s) but no files.` : "This folder is empty.");
    } else {
      setStatus("complete", data.truncated ? "Scan stopped early" : "Scan complete",
        `${numberFmt.format(data.total_files)} files in ${secs}s · ${data.root}`);
    }
    applyFilters();
    onNewScan(data.root);
  }

  function applyFilters() {
    const q = els.search.value.trim().toLowerCase();
    const type = els.typeFilter.value;
    const key = state.sortKey;
    const dir = state.sortDir;
    state.filtered = state.files.filter((f) =>
      (!type || f.category === type) &&
      (!q || f.relative_path.toLowerCase().includes(q))
    );
    state.filtered.sort((a, b) => {
      const av = a[key], bv = b[key];
      const cmp = typeof av === "number" ? av - bv : String(av).localeCompare(String(bv), undefined, { numeric: true });
      return cmp * dir;
    });
    state.shown = PAGE_SIZE;
    renderRows();
  }

  function cell(cls, text, label) {
    const td = document.createElement("td");
    if (cls) td.className = cls;
    if (label) td.dataset.label = label;
    td.textContent = text;
    return td;
  }

  function renderRows() {
    const frag = document.createDocumentFragment();
    for (const f of state.filtered.slice(0, state.shown)) {
      const tr = document.createElement("tr");
      tr.append(cell("name", f.name));

      const typeTd = document.createElement("td");
      const badge = document.createElement("span");
      badge.className = f.kind === "symlink" ? "badge badge-link" : "badge";
      badge.textContent = f.category;
      typeTd.append(badge);
      if (f.extension) {
        const ext = document.createElement("span");
        ext.className = "ext";
        ext.textContent = `.${f.extension}`;
        typeTd.append(ext);
      }
      tr.append(typeTd);

      tr.append(cell("num", f.kind === "symlink" ? "—" : formatBytes(f.size), "Size"));
      tr.append(cell("date", dateFmt.format(new Date(f.modified)), "Modified"));
      tr.append(cell("path", f.relative_path));
      const act = document.createElement("td");
      act.className = "actions";
      if (isInspectable(f)) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn btn-secondary btn-small";
        btn.dataset.inspect = f.relative_path;
        btn.textContent = "Inspect";
        btn.setAttribute("aria-label", `Inspect ${f.name}`);
        act.append(btn);
      }
      tr.append(act);
      frag.append(tr);
    }
    els.rows.replaceChildren(frag);

    const total = state.filtered.length;
    els.emptyFilter.hidden = total > 0 || state.files.length === 0;
    els.more.hidden = total <= state.shown;
    els.more.textContent = `Show more (${numberFmt.format(Math.min(total, state.shown))} of ${numberFmt.format(total)} shown)`;
    els.matchCount.textContent = total === state.files.length
      ? `${numberFmt.format(total)} items`
      : `${numberFmt.format(total)} of ${numberFmt.format(state.files.length)} items`;

    for (const th of els.headers) {
      th.setAttribute("aria-sort", th.dataset.sort === state.sortKey ? (state.sortDir > 0 ? "ascending" : "descending") : "none");
    }
  }

  async function chooseFolder() {
    if (state.busy) return;
    setBusy(true);
    setStatus("running", "Waiting for folder selection…", "A macOS folder dialog has opened. It may appear behind this window.");
    try {
      const data = await postJSON("/api/pick-folder");
      if (data.cancelled) {
        setStatus("idle", "No folder chosen", data.message || "");
        return;
      }
      els.path.value = data.path;
      setBusy(false);
      await runScan();
    } catch (err) {
      setStatus("error", "Folder picker unavailable", `${err.message} Type the folder path instead.`);
    } finally {
      setBusy(false);
    }
  }

  // --- Organization preview (read-only) ----------------------------------------
  const STATUS_LABELS = { move: "Ready to move", conflict: "Name conflict", review: "Needs review", keep: "Left in place" };
  const FALLBACK_CATEGORIES = ["Documents", "Images", "Videos", "Audio", "Spreadsheets", "Presentations", "Archives", "Code", "Other"];

  const o = {
    tabFiles: $("tab-files"),
    tabOrganize: $("tab-organize"),
    panelFiles: $("panel-files"),
    panelOrganize: $("panel-organize"),
    chips: $("cat-chips"),
    catAll: $("cat-all"),
    catNone: $("cat-none"),
    status: $("org-status"),
    statusTitle: $("org-status-title"),
    statusDetail: $("org-status-detail"),
    body: $("org-body"),
    counts: { move: $("o-move"), conflict: $("o-conflict"), review: $("o-review"), keep: $("o-keep") },
    metricBtns: document.querySelectorAll(".metric-btn"),
    notices: $("org-notices"),
    search: $("org-search"),
    statusFilter: $("org-status-filter"),
    catFilter: $("org-cat-filter"),
    count: $("org-count"),
    rows: $("org-rows"),
    empty: $("org-empty"),
    more: $("org-more"),
    protectedPanel: $("protected-panel"),
    protectedList: $("protected-list"),
    scopeRadios: document.querySelectorAll('input[name="scope"]'),
  };

  const org = {
    root: null,
    includeHidden: false,
    categories: FALLBACK_CATEGORIES,
    selected: new Set(FALLBACK_CATEGORIES),
    plan: null,
    filtered: [],
    shown: PAGE_SIZE,
    requestId: 0,
    stale: true,
    timer: null,
  };

  function setOrgStatus(kind, title, detail) {
    o.status.className = `status status-${kind}`;
    o.statusTitle.textContent = title;
    o.statusDetail.textContent = detail || "";
  }

  const TABS = [
    { name: "files", tab: o.tabFiles, panel: o.panelFiles },
    { name: "organize", tab: o.tabOrganize, panel: o.panelOrganize },
    { name: "duplicates", tab: $("tab-duplicates"), panel: $("panel-duplicates") },
  ];

  function selectTab(which) {
    for (const t of TABS) {
      const active = t.name === which;
      t.tab.setAttribute("aria-selected", String(active));
      t.tab.tabIndex = active ? 0 : -1;
      t.panel.hidden = !active;
    }
    if (which === "organize" && org.stale) requestPreview();
  }

  function renderChips() {
    const counts = org.plan?.category_counts;
    o.chips.replaceChildren();
    for (const cat of org.categories) {
      const label = document.createElement("label");
      label.className = "chip";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = cat;
      input.checked = org.selected.has(cat);
      input.addEventListener("change", () => {
        if (input.checked) org.selected.add(cat); else org.selected.delete(cat);
        schedulePreview();
      });
      const name = document.createElement("span");
      name.textContent = cat;
      label.append(input, name);
      if (counts) {
        const n = document.createElement("span");
        n.className = "count";
        n.textContent = numberFmt.format(counts[cat]?.files ?? 0);
        label.append(n);
      }
      o.chips.append(label);
    }
  }

  function currentScope() {
    return document.querySelector('input[name="scope"]:checked').value;
  }

  function schedulePreview() {
    org.stale = true;
    clearTimeout(org.timer);
    org.timer = setTimeout(requestPreview, 250);
  }

  async function requestPreview() {
    if (!org.root) return;
    clearTimeout(org.timer);
    const id = ++org.requestId;
    org.stale = false;
    setOrgStatus("running", "Building preview…", "Reading file details only. Nothing is changed.");
    try {
      const plan = await postJSON("/api/organize/preview", {
        path: org.root,
        categories: org.categories.filter((c) => org.selected.has(c)),
        scope: currentScope(),
        include_hidden: org.includeHidden,
      });
      if (id === org.requestId) showPlan(plan);
    } catch (err) {
      if (id !== org.requestId) return;
      org.stale = true;
      o.body.hidden = true;
      setOrgStatus("error", "Preview failed", err.message || "Unexpected error.");
    }
  }

  function showPlan(plan) {
    org.plan = plan;
    for (const s of Object.keys(o.counts)) o.counts[s].textContent = numberFmt.format(plan.status_counts[s] || 0);
    renderChips();

    o.notices.replaceChildren();
    const notes = [...plan.warnings];
    if (plan.hidden_skipped) notes.push(`${numberFmt.format(plan.hidden_skipped)} hidden item(s) are not shown and won't be reorganized.`);
    if (plan.protected_folders.length) notes.push(`${numberFmt.format(plan.protected_folders.length)} protected folder(s), such as app packages and software projects, are left untouched.`);
    for (const text of notes) {
      const div = document.createElement("div");
      div.className = "notice";
      div.textContent = text;
      o.notices.append(div);
    }

    o.protectedList.replaceChildren();
    for (const p of plan.protected_folders) {
      const li = document.createElement("li");
      const code = document.createElement("code");
      code.textContent = p.relative_path;
      li.append(code, ` — ${p.reason}`);
      o.protectedList.append(li);
    }
    o.protectedPanel.hidden = plan.protected_folders.length === 0;

    const previous = o.catFilter.value;
    o.catFilter.replaceChildren(new Option("All categories", ""));
    for (const cat of org.categories) {
      const n = plan.category_counts[cat]?.files || 0;
      if (n) o.catFilter.append(new Option(`${cat} (${numberFmt.format(n)})`, cat));
    }
    o.catFilter.value = [...o.catFilter.options].some((opt) => opt.value === previous) ? previous : "";

    o.body.hidden = false;
    const c = plan.status_counts;
    const proposed = c.move + c.conflict + c.review;
    if (!plan.selected_categories.length) {
      setOrgStatus("empty", "No categories selected", "Select at least one category to see proposed moves.");
    } else if (proposed === 0) {
      setOrgStatus("empty", "Nothing to organize",
        plan.items.length ? "Every file is already organized or is left in place for safety." : "No files found in this folder.");
    } else {
      const parts = [`${numberFmt.format(proposed)} proposed move(s), ${formatBytes(plan.size_to_move)}`];
      if (c.conflict) parts.push(`${numberFmt.format(c.conflict)} conflict(s)`);
      if (c.review) parts.push(`${numberFmt.format(c.review)} need review`);
      setOrgStatus(plan.complete ? "complete" : "empty", plan.complete ? "Preview ready" : "Partial preview", parts.join(" · "));
    }
    applyOrgFilters();
  }

  function applyOrgFilters() {
    if (!org.plan) return;
    const q = o.search.value.trim().toLowerCase();
    const status = o.statusFilter.value;
    const cat = o.catFilter.value;
    org.filtered = org.plan.items.filter((it) =>
      (!status || it.status === status) &&
      (!cat || it.suggested_category === cat) &&
      (!q || it.relative_path.toLowerCase().includes(q) || (it.destination || "").toLowerCase().includes(q))
    );
    for (const btn of o.metricBtns) btn.setAttribute("aria-pressed", String(btn.dataset.status === status));
    org.shown = PAGE_SIZE;
    renderOrgRows();
  }

  function renderOrgRows() {
    const frag = document.createDocumentFragment();
    for (const it of org.filtered.slice(0, org.shown)) {
      const tr = document.createElement("tr");
      tr.className = `row-${it.status}`;
      tr.append(cell("path", it.relative_path));

      const catTd = document.createElement("td");
      if (it.suggested_category) {
        const badge = document.createElement("span");
        badge.className = "badge";
        badge.textContent = it.suggested_category;
        catTd.append(badge);
      } else {
        catTd.textContent = "—";
        catTd.className = "muted";
      }
      tr.append(catTd);

      tr.append(it.destination ? cell("dest", it.destination) : cell("dest none", "Stays where it is"));

      const stTd = document.createElement("td");
      const st = document.createElement("span");
      st.className = `st st-${it.status}`;
      st.textContent = STATUS_LABELS[it.status] || it.status;
      stTd.append(st);
      tr.append(stTd);

      const reasonTd = document.createElement("td");
      reasonTd.className = "reason";
      reasonTd.append(it.reason);
      if (it.notes.length) {
        const ul = document.createElement("ul");
        for (const note of it.notes) {
          const li = document.createElement("li");
          li.textContent = note;
          ul.append(li);
        }
        reasonTd.append(ul);
      }
      tr.append(reasonTd);
      frag.append(tr);
    }
    o.rows.replaceChildren(frag);

    const total = org.filtered.length;
    const all = org.plan.items.length;
    o.empty.hidden = total > 0;
    o.empty.textContent = all === 0 ? "No files to show." : "No items match your search or filters.";
    o.more.hidden = total <= org.shown;
    o.more.textContent = `Show more (${numberFmt.format(Math.min(total, org.shown))} of ${numberFmt.format(total)} shown)`;
    o.count.textContent = total === all ? `${numberFmt.format(total)} items` : `${numberFmt.format(total)} of ${numberFmt.format(all)} items`;
  }

  function onNewScan(root) {
    org.root = root;
    org.includeHidden = els.hidden.checked;
    org.plan = null;
    org.stale = true;
    o.body.hidden = true;
    renderChips();
    setOrgStatus("idle", "No preview yet", "Building a preview reads file details only.");
    if (!o.panelOrganize.hidden) requestPreview();
    dupOnNewScan(root);
  }

  TABS.forEach((t, i) => {
    t.tab.addEventListener("click", () => selectTab(t.name));
    t.tab.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      const next = TABS[(i + (e.key === "ArrowRight" ? 1 : TABS.length - 1)) % TABS.length];
      selectTab(next.name);
      next.tab.focus();
    });
  });
  o.catAll.addEventListener("click", () => { org.selected = new Set(org.categories); renderChips(); schedulePreview(); });
  o.catNone.addEventListener("click", () => { org.selected.clear(); renderChips(); schedulePreview(); });
  for (const r of o.scopeRadios) r.addEventListener("change", schedulePreview);
  let orgSearchTimer;
  o.search.addEventListener("input", () => { clearTimeout(orgSearchTimer); orgSearchTimer = setTimeout(applyOrgFilters, 120); });
  o.statusFilter.addEventListener("change", applyOrgFilters);
  o.catFilter.addEventListener("change", applyOrgFilters);
  o.more.addEventListener("click", () => { org.shown += PAGE_SIZE; renderOrgRows(); });
  for (const btn of o.metricBtns) {
    btn.addEventListener("click", () => {
      o.statusFilter.value = o.statusFilter.value === btn.dataset.status ? "" : btn.dataset.status;
      applyOrgFilters();
    });
  }

  // --- Duplicate detection (read-only) ------------------------------------------
  const GROUP_PAGE = 50;
  const d = {
    start: $("dup-start"),
    cancel: $("dup-cancel"),
    status: $("dup-status"),
    statusTitle: $("dup-status-title"),
    statusDetail: $("dup-status-detail"),
    progress: $("dup-progress"),
    bar: $("dup-progress-bar"),
    progressText: $("dup-progress-text"),
    body: $("dup-body"),
    groups: $("d-groups"),
    extra: $("d-extra"),
    recoverable: $("d-recoverable"),
    hashed: $("d-hashed"),
    notices: $("dup-notices"),
    search: $("dup-search"),
    sort: $("dup-sort"),
    count: $("dup-count"),
    list: $("dup-groups"),
    empty: $("dup-empty"),
    more: $("dup-more"),
    skippedPanel: $("dup-skipped-panel"),
    skippedList: $("dup-skipped-list"),
  };
  const dup = { root: null, includeHidden: false, jobId: null, timer: null, report: null, filtered: [], shown: GROUP_PAGE };

  function setDupStatus(kind, title, detail) {
    d.status.className = `status status-${kind}`;
    d.statusTitle.textContent = title;
    d.statusDetail.textContent = detail || "";
  }

  function setDupRunning(running) {
    d.start.disabled = running || !dup.root;
    d.cancel.hidden = !running;
    d.cancel.disabled = false;
    d.progress.hidden = !running;
  }

  async function startDuplicates() {
    if (!dup.root || dup.jobId) return;
    d.body.hidden = true;
    setDupRunning(true);
    renderProgress({ phase: "starting", files_total: 0, files_done: 0, bytes_read: 0, elapsed_seconds: 0 });
    try {
      const job = await postJSON("/api/duplicates/start", { path: dup.root, include_hidden: dup.includeHidden });
      dup.jobId = job.id;
      pollDuplicates();
    } catch (err) {
      setDupRunning(false);
      setDupStatus("error", "Couldn't start the duplicate check", err.message);
    }
  }

  async function pollDuplicates() {
    const id = dup.jobId;
    if (!id) return;
    let job;
    try {
      const res = await fetch(`/api/duplicates/${encodeURIComponent(id)}`);
      job = await res.json();
      if (!res.ok) throw new Error(job?.error?.message || `Request failed (${res.status}).`);
    } catch (err) {
      if (dup.jobId !== id) return;
      dup.jobId = null;
      setDupRunning(false);
      setDupStatus("error", "Lost track of the duplicate check", err.message);
      return;
    }
    if (dup.jobId !== id) return; // a newer scan replaced this one
    if (job.status === "running") {
      renderProgress(job.progress);
      dup.timer = setTimeout(pollDuplicates, 400);
      return;
    }
    dup.jobId = null;
    setDupRunning(false);
    if (job.status === "failed") {
      setDupStatus("error", "Duplicate check failed", job.error?.message || "Unexpected error.");
      return;
    }
    showDuplicates(job.result, job.status === "cancelled");
  }

  function renderProgress(p) {
    const phases = { starting: "Starting…", listing: "Listing files…", hashing: "Comparing file contents…", done: "Finishing…" };
    setDupStatus("running", phases[p.phase] || "Working…", p.current ? p.current : "Reading files only. Nothing is changed.");
    // Bytes track progress inside large files; file counts are the fallback.
    const ratio = p.bytes_total ? p.bytes_read / p.bytes_total : (p.files_total ? p.files_done / p.files_total : 0);
    const pct = Math.min(100, Math.round(ratio * 100));
    d.bar.style.width = `${p.phase === "hashing" ? pct : 2}%`;
    d.progressText.textContent = p.phase === "hashing"
      ? `${numberFmt.format(p.files_done)} of ${numberFmt.format(p.files_total)} files · ${formatBytes(p.bytes_read)} read · ${p.elapsed_seconds}s`
      : `${p.elapsed_seconds}s`;
  }

  function showDuplicates(r, cancelled) {
    dup.report = r;
    d.groups.textContent = numberFmt.format(r.group_count);
    d.extra.textContent = numberFmt.format(r.duplicate_files);
    d.recoverable.textContent = formatBytes(r.recoverable_bytes);
    d.hashed.textContent = numberFmt.format(r.files_hashed);

    d.notices.replaceChildren();
    const notes = [];
    if (r.truncated) notes.push(`${r.truncated_reason} ${numberFmt.format(r.unchecked_files)} file(s) were not checked.`);
    if (r.scan_truncated) notes.push("The file listing stopped at its limit, so some files were not included. Choose a smaller folder for a complete check.");
    if (r.empty_files) notes.push(`${numberFmt.format(r.empty_files)} empty file(s) were skipped. They take no space, so there is nothing to recover.`);
    if (r.symlinks_ignored) notes.push(`${numberFmt.format(r.symlinks_ignored)} symbolic link(s) were ignored and not followed.`);
    if (r.protected_folders.length) notes.push(`${numberFmt.format(r.protected_folders.length)} app package or support folder(s) were not opened (for example ${r.protected_folders[0]}).`);
    if (r.skipped_count) notes.push(`${numberFmt.format(r.skipped_count)} file(s) could not be checked. See the list below.`);
    for (const text of notes) {
      const div = document.createElement("div");
      div.className = "notice";
      div.textContent = text;
      d.notices.append(div);
    }

    d.skippedList.replaceChildren();
    for (const s of r.skipped) {
      const li = document.createElement("li");
      const code = document.createElement("code");
      code.textContent = s.relative_path;
      li.append(code, ` — ${s.reason}`);
      d.skippedList.append(li);
    }
    if (r.skipped_count > r.skipped.length) {
      const li = document.createElement("li");
      li.textContent = `…and ${numberFmt.format(r.skipped_count - r.skipped.length)} more.`;
      d.skippedList.append(li);
    }
    d.skippedPanel.hidden = r.skipped.length === 0;

    d.body.hidden = false;
    const secs = (r.duration_ms / 1000).toFixed(1);
    if (cancelled) {
      setDupStatus("empty", "Duplicate check cancelled", `Partial results: ${numberFmt.format(r.group_count)} group(s) found before stopping.`);
    } else if (r.group_count === 0) {
      setDupStatus(r.truncated ? "empty" : "complete", "No exact duplicates found",
        `Compared ${numberFmt.format(r.files_considered)} file(s) in ${secs}s.`);
    } else {
      setDupStatus(r.truncated ? "empty" : "complete", r.truncated ? "Partial results" : "Duplicate check complete",
        `${numberFmt.format(r.group_count)} group(s) · about ${formatBytes(r.recoverable_bytes)} recoverable · ${secs}s`);
    }
    applyDupFilters();
  }

  function applyDupFilters() {
    if (!dup.report) return;
    const q = d.search.value.trim().toLowerCase();
    const key = d.sort.value;
    dup.filtered = dup.report.groups.filter((g) => !q || g.files.some((f) => f.relative_path.toLowerCase().includes(q)));
    const cmp = {
      recoverable: (a, b) => b.recoverable_bytes - a.recoverable_bytes || b.size - a.size,
      size: (a, b) => b.size - a.size,
      count: (a, b) => b.count - a.count || b.size - a.size,
      path: (a, b) => a.files[0].relative_path.localeCompare(b.files[0].relative_path, undefined, { numeric: true }),
    }[key];
    dup.filtered.sort(cmp);
    dup.shown = GROUP_PAGE;
    renderGroups();
  }

  function span(cls, text) {
    const el = document.createElement("span");
    el.className = cls;
    el.textContent = text;
    return el;
  }

  function renderGroups() {
    const frag = document.createDocumentFragment();
    for (const g of dup.filtered.slice(0, dup.shown)) {
      const box = document.createElement("div");
      box.className = "dup-group";
      const head = document.createElement("div");
      head.className = "dup-group-head";
      head.append(
        span("copies", `${g.count} copies`),
        span("muted", `${formatBytes(g.size)} each`),
        span("save", g.recoverable_bytes ? `≈ ${formatBytes(g.recoverable_bytes)} recoverable` : "No space recoverable"),
      );
      const hash = span("hash", `SHA-256 ${g.sha256.slice(0, 12)}…`);
      hash.title = g.sha256;
      head.append(hash);
      box.append(head);

      const ul = document.createElement("ul");
      ul.className = "dup-files";
      for (const f of g.files) {
        const li = document.createElement("li");
        li.append(span("p", f.relative_path), span("d", dateFmt.format(new Date(f.modified))));
        ul.append(li);
      }
      box.append(ul);
      if (g.hard_linked) {
        const note = document.createElement("div");
        note.className = "dup-note";
        note.textContent = "Some of these paths are hard links to the same data, so they already share storage.";
        box.append(note);
      }
      frag.append(box);
    }
    d.list.replaceChildren(frag);

    const total = dup.filtered.length;
    const all = dup.report.groups.length;
    d.empty.hidden = total > 0;
    d.empty.textContent = all === 0 ? "No duplicate files were found in this folder." : "No groups match your search.";
    d.more.hidden = total <= dup.shown;
    d.more.textContent = `Show more (${numberFmt.format(Math.min(total, dup.shown))} of ${numberFmt.format(total)} groups shown)`;
    d.count.textContent = total === all ? `${numberFmt.format(total)} groups` : `${numberFmt.format(total)} of ${numberFmt.format(all)} groups`;
  }

  function dupOnNewScan(root) {
    if (dup.jobId) {
      postJSON(`/api/duplicates/${encodeURIComponent(dup.jobId)}/cancel`).catch(() => {});
    }
    clearTimeout(dup.timer);
    dup.root = root;
    dup.includeHidden = els.hidden.checked;
    dup.jobId = null;
    dup.report = null;
    d.body.hidden = true;
    setDupRunning(false);
    setDupStatus("idle", "Not started", "Checking large folders can take a while. You can cancel at any time.");
  }

  d.start.addEventListener("click", startDuplicates);
  d.cancel.addEventListener("click", async () => {
    if (!dup.jobId) return;
    d.cancel.disabled = true;
    try { await postJSON(`/api/duplicates/${encodeURIComponent(dup.jobId)}/cancel`); } catch { /* polling reports the outcome */ }
  });
  let dupSearchTimer;
  d.search.addEventListener("input", () => { clearTimeout(dupSearchTimer); dupSearchTimer = setTimeout(applyDupFilters, 120); });
  d.sort.addEventListener("change", applyDupFilters);
  d.more.addEventListener("click", () => { dup.shown += GROUP_PAGE; renderGroups(); });

  // --- Document inspector (read-only) ---------------------------------------------
  const DOC_EXTENSIONS = new Set(["pdf", "txt", "md", "markdown", "docx", "csv"]);
  const DOC_STATUS = {
    ok: ["complete", "Text extracted"],
    partial: ["empty", "Partly extracted"],
    empty: ["empty", "No text found"],
    needs_ocr: ["empty", "Needs OCR"],
    encrypted: ["error", "Encrypted document"],
    unsupported: ["error", "Unsupported file type"],
    too_large: ["error", "File too large"],
    timeout: ["error", "Took too long"],
    error: ["error", "Couldn't read this document"],
  };
  const doc = {
    dialog: $("doc-dialog"),
    close: $("doc-close"),
    type: $("doc-type"),
    title: $("doc-title"),
    path: $("doc-path"),
    status: $("doc-status"),
    statusTitle: $("doc-status-title"),
    statusDetail: $("doc-status-detail"),
    warnings: $("doc-warnings"),
    body: $("doc-body"),
    facts: $("doc-facts"),
    analysis: $("doc-analysis"),
    ai: $("doc-ai"),
    previewNote: $("doc-preview-note"),
    preview: $("doc-preview"),
  };
  const docState = { requestId: 0, current: null, report: null, aiStatus: null };

  function isInspectable(f) {
    return f.kind === "file" && DOC_EXTENSIONS.has(f.extension);
  }

  function setDocStatus(kind, title, detail) {
    doc.status.className = `status status-${kind}`;
    doc.statusTitle.textContent = title;
    doc.statusDetail.textContent = detail || "";
  }

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function factValue(f) {
    if (f.label === "Size" && typeof f.value === "number") return formatBytes(f.value);
    if (f.label === "Modified") return dateFmt.format(new Date(f.value));
    return typeof f.value === "number" ? numberFmt.format(f.value) : String(f.value);
  }

  async function openDocument(relativePath) {
    if (!state.root) return;
    const id = ++docState.requestId;
    docState.current = relativePath;
    docState.report = null;
    const name = relativePath.split("/").pop();
    const ext = name.includes(".") ? name.split(".").pop().toUpperCase() : "";
    doc.type.textContent = ext || "File";
    doc.title.textContent = name;
    doc.path.textContent = relativePath;
    doc.body.hidden = true;
    doc.warnings.hidden = true;
    setDocStatus("running", "Reading document…", "Extracting text on this Mac. Nothing is changed.");
    if (!doc.dialog.open) doc.dialog.showModal();
    try {
      const report = await postJSON("/api/documents/inspect", { path: state.root, relative_path: relativePath, use_ai: false });
      if (id !== docState.requestId) return;
      showDocument(report);
    } catch (err) {
      if (id !== docState.requestId) return;
      setDocStatus("error", "Couldn't open this document", err.message);
    }
  }

  function showDocument(r) {
    docState.report = r;
    doc.type.textContent = r.type_label;
    if (r.title) doc.title.textContent = r.title;
    const [kind, title] = DOC_STATUS[r.extraction.status] || ["error", "Couldn't read this document"];
    setDocStatus(kind, title, r.extraction.message);

    doc.warnings.replaceChildren(...r.extraction.warnings.map((w) => el("li", null, w)));
    doc.warnings.hidden = r.extraction.warnings.length === 0;

    doc.facts.replaceChildren();
    for (const f of r.facts) {
      const dd = el("dd", null, factValue(f));
      dd.append(el("small", null, f.source));
      doc.facts.append(el("dt", null, f.label), dd);
    }

    doc.analysis.replaceChildren();
    if (r.analysis) {
      renderAnalysis(doc.analysis, r.analysis);
    } else {
      doc.analysis.append(el("p", "analysis-note", "No summary or category, because no text could be extracted."));
    }

    const ex = r.extraction;
    doc.preview.textContent = ex.preview;
    if (!ex.char_count) {
      doc.previewNote.textContent = "Nothing was extracted.";
    } else if (ex.preview_truncated || ex.read_truncated) {
      doc.previewNote.textContent = `Showing the first ${numberFmt.format(ex.preview.length)} of ${numberFmt.format(ex.char_count)} extracted characters${ex.read_truncated ? " (the file itself was only partly read)" : ""}.`;
    } else {
      doc.previewNote.textContent = `${numberFmt.format(ex.char_count)} characters, ${numberFmt.format(ex.word_count)} words.`;
    }
    doc.body.hidden = false;
    renderAI();
  }

  function renderAnalysis(container, a) {
    if (a.summary) {
      container.append(el(a.generated_by === "rules" ? "blockquote" : "p", a.generated_by === "rules" ? "summary-quote" : "summary-ai", a.summary));
    }
    container.append(el("p", "analysis-note", a.summary_note));
    const ul = el("ul", "cat-list");
    for (const c of a.categories) {
      const li = el("li");
      li.append(el("span", "cat-name", c.name));
      if (c.confidence !== "model") li.append(el("span", `conf conf-${c.confidence}`, `${c.confidence} confidence`));
      if (c.evidence.length) li.append(el("span", "cat-evidence", `${a.generated_by === "rules" ? "Evidence" : "Reason"}: ${c.evidence.join(", ")}`));
      ul.append(li);
    }
    container.append(ul, el("p", "analysis-note", a.category_note));
    for (const n of a.notes || []) container.append(el("p", "analysis-note", n));
  }

  async function loadAIStatus(force) {
    if (docState.aiStatus && !force) return docState.aiStatus;
    try {
      const res = await fetch("/api/ai/status");
      docState.aiStatus = await res.json();
    } catch {
      docState.aiStatus = { available: false, reason: "Could not check for a local AI model.", setup_steps: [] };
    }
    return docState.aiStatus;
  }

  async function renderAI(force = false) {
    const r = docState.report;
    doc.ai.replaceChildren(el("p", "muted small", "Checking for a local model…"));
    const s = await loadAIStatus(force);
    if (docState.report !== r) return;
    const box = el("div", "ai-block");
    if (!r.analysis) {
      box.append(el("p", "muted", "Local AI needs extracted text, and this document has none."));
    } else if (s.available) {
      box.append(el("p", null, `Ollama is running with “${s.model}”. The extracted text is sent only to Ollama on this Mac (127.0.0.1), never to the internet.`));
      const btn = el("button", "btn btn-primary btn-small", "Summarize with local AI");
      btn.type = "button";
      btn.addEventListener("click", () => runAI(btn));
      box.append(btn);
    } else {
      box.append(el("p", null, `Local AI is off: ${s.reason || "not available."} The rule-based results above work without it.`));
      if (s.setup_steps?.length) {
        const ol = el("ol");
        for (const step of s.setup_steps) {
          const li = el("li");
          // Show `code` spans as code; everything is text, never HTML.
          step.split("`").forEach((part, i) => li.append(i % 2 ? el("code", null, part) : document.createTextNode(part)));
          ol.append(li);
        }
        box.append(el("p", "muted small", "To turn it on (optional):"), ol);
      }
      const again = el("button", "btn btn-secondary btn-small", "Check again");
      again.type = "button";
      again.addEventListener("click", () => renderAI(true));
      box.append(again);
    }
    doc.ai.replaceChildren(box);
  }

  async function runAI(btn) {
    const r = docState.report;
    btn.disabled = true;
    btn.textContent = "Running the local model… (this can take a minute)";
    const out = el("div", "ai-result");
    try {
      const res = await postJSON("/api/documents/inspect", { path: state.root, relative_path: r.relative_path, use_ai: true });
      if (docState.report !== r) return;
      if (res.ai.result) {
        const tag = el("p", null);
        tag.append(el("span", "tag tag-ai", res.ai.result.label));
        out.append(tag);
        renderAnalysis(out, res.ai.result);
      } else {
        out.append(el("p", "ai-error", res.ai.error || "The local model did not return a result."));
      }
    } catch (err) {
      out.append(el("p", "ai-error", err.message));
    }
    doc.ai.querySelector(".ai-result")?.remove();
    doc.ai.append(out);
    btn.disabled = false;
    btn.textContent = "Run again";
  }

  doc.close.addEventListener("click", () => doc.dialog.close());
  doc.dialog.addEventListener("click", (e) => { if (e.target === doc.dialog) doc.dialog.close(); });
  doc.dialog.addEventListener("close", () => {
    // The close event is queued; ignore it if another document was opened meanwhile.
    if (doc.dialog.open) return;
    docState.requestId++;
    docState.report = null;
  });
  els.rows.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-inspect]");
    if (btn) openDocument(btn.dataset.inspect);
  });

  // Events
  els.form.addEventListener("submit", (e) => { e.preventDefault(); runScan(); });
  els.choose.addEventListener("click", chooseFolder);
  els.path.addEventListener("input", () => els.path.removeAttribute("aria-invalid"));
  let searchTimer;
  els.search.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(applyFilters, 120); });
  els.typeFilter.addEventListener("change", applyFilters);
  els.more.addEventListener("click", () => { state.shown += PAGE_SIZE; renderRows(); });
  for (const th of els.headers) {
    th.querySelector("button").addEventListener("click", () => {
      const key = th.dataset.sort;
      state.sortDir = state.sortKey === key ? -state.sortDir : (key === "size" || key === "modified" ? -1 : 1);
      state.sortKey = key;
      applyFilters();
    });
  }

  // Show the native picker button only when the server supports it.
  fetch("/api/health")
    .then((r) => r.json())
    .then((h) => {
      if (h.picker_available) els.choose.hidden = false;
      if (Array.isArray(h.organize_categories)) {
        org.categories = h.organize_categories;
        org.selected = new Set(org.categories);
        renderChips();
      }
      els.statusDetail.textContent = h.picker_available
        ? "Click “Choose folder…” or type a folder path."
        : "Type the full path of a folder to scan.";
    })
    .catch(() => setStatus("error", "FilePilot server not reachable", "Start the server and reload this page."));
})();
