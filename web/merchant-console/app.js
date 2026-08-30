/* Merchant console — navigation, the real catalog table, and the real upload flow.
 *
 * This replaces six minified lines that held 342 generated products, a hardcoded
 * "Category detected: Home & living", and an import button wired to nothing.
 *
 * The upload path is now the real one:
 *   POST {merchant}/catalog/upload   store the raw rows
 *   POST {agent}/ingest/analyze      derive a profile from those rows
 *   -> Review                        the merchant approves what was derived
 *
 * Note which service does what. The merchant stores rows and serves them untouched;
 * the agent interprets them. Normalizing at upload would destroy the original column
 * names the agent's profiler reads, and a normalized row cannot be un-normalized —
 * so the console's own product table is the ONLY consumer of GET /catalog.
 */

(function () {
  const { MERCHANT_BASE, AGENT_BASE, MERCHANT_ID } = window.TOES;
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => document.querySelectorAll(s);

  const escapeHtml = (v) =>
    String(v ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
    );

  // --- navigation ------------------------------------------------------------

  const TITLES = {
    catalog: "Catalog",
    review: "Profile review",
    activity: "Agent activity",
    settings: "Settings",
  };

  function showView(name) {
    $$(".nav-item").forEach((b) =>
      b.classList.toggle("active", b.dataset.view === name),
    );
    $$("main .content").forEach((s) =>
      s.classList.toggle("hidden", s.id !== `${name}-view`),
    );
    $("#page-title").textContent = TITLES[name] || name;
    if (name === "review") window.ToesReview.load();
    if (name === "catalog") loadCatalog({ quiet: true });
    syncPolling();
  }

  $$(".nav-item").forEach((b) => (b.onclick = () => showView(b.dataset.view)));

  // --- the catalog table -----------------------------------------------------

  /* The table renders GET /catalog/raw: the merchant's own sheet, stored verbatim, and
   * the exact rows the agent sells from. Three things were wrong with reading the
   * normalized GET /catalog instead, and all three surfaced as "my product list is out
   * of date":
   *
   *  1. Normalization is all-or-nothing. A sheet whose id/title/price columns could not
   *     be mapped normalized to NOTHING, and the old backend then left the PREVIOUS
   *     upload's products in place — so this screen showed last week's catalog while the
   *     agent sold from this week's, and nothing anywhere said so.
   *  2. Rows failing a per-row check (an unparseable price) were dropped silently, so
   *     the count here could be lower than the catalog really is.
   *  3. It coerced everything into nine fixed fields, so a merchant could not see their
   *     own columns.
   *
   * Headers therefore come from the sheet. No column name is hardcoded here — the same
   * rule the storefront and the agent backend hold themselves to. */

  let columns = [];
  let allRows = [];
  let signature = null;
  let page = 0;
  const perPage = 8;

  async function fetchCatalog() {
    const res = await fetch(
      `${MERCHANT_BASE}/catalog/raw?merchant_id=${encodeURIComponent(MERCHANT_ID)}`,
    );
    // 404 is a merchant with nothing uploaded yet. That is a state, not a failure.
    if (res.status === 404) return { rows: [], columns: [], row_count: 0 };
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  }

  /* Cheap change detection, so a poll that found nothing new does not blow away the
   * merchant's search box, page or scroll position. */
  const signatureOf = (d) =>
    [
      d.row_count,
      d.uploaded_at || "",
      (d.columns || []).join(""),
      JSON.stringify(d.rows || []).length,
    ].join("|");

  async function loadCatalog({ quiet = false } = {}) {
    const body = $("#product-rows");
    if (!quiet && !allRows.length) {
      body.innerHTML = `<tr><td class="table-note">Loading your catalog…</td></tr>`;
    }

    let data;
    try {
      data = await fetchCatalog();
    } catch (err) {
      // A failed poll must not wipe a table that is still correct.
      if (quiet) return;
      allRows = [];
      columns = [];
      signature = null;
      body.innerHTML = `<tr><td class="table-note error">
        Could not reach the merchant service at ${escapeHtml(MERCHANT_BASE)} — ${escapeHtml(
          err.message,
        )}</td></tr>`;
      $("#total-products").textContent = "—";
      $("#product-count").textContent = "catalog unavailable";
      $("#synced-sub").textContent = "not reachable";
      return;
    }

    const next = signatureOf(data);
    if (next !== signature) {
      signature = next;
      allRows = data.rows || [];
      columns = (data.columns || []).length
        ? data.columns
        : [...new Set(allRows.flatMap((r) => Object.keys(r)))];
      if (page * perPage >= allRows.length) page = 0;
      renderCatalog();
      loadProfileSummary();
    }

    $("#source-sub").textContent = data.source_filename
      ? `from ${data.source_filename}`
      : "stored as raw rows, served untouched";
    $("#synced-sub").textContent = `checked ${new Date().toLocaleTimeString()}`;
  }

  function renderCatalog() {
    const q = $("#search").value.trim().toLowerCase();
    const list = q
      ? allRows.filter((r) =>
          Object.values(r).some((v) => String(v ?? "").toLowerCase().includes(q)),
        )
      : allRows;

    const shown = list.slice(page * perPage, page * perPage + perPage);
    const span = Math.max(columns.length, 1);
    const body = $("#product-rows");

    $("#product-head").innerHTML = columns
      .map((c) => `<th>${escapeHtml(c)}</th>`)
      .join("");

    if (!shown.length) {
      body.innerHTML = `<tr><td colspan="${span}" class="table-note">${
        allRows.length
          ? "Nothing matches that search."
          : "No products yet — upload a spreadsheet to begin."
      }</td></tr>`;
    } else {
      body.innerHTML = shown
        .map(
          (row) =>
            `<tr>${columns
              .map((c) => {
                const v = row[c];
                const blank = v === null || v === undefined || v === "";
                return `<td>${blank ? '<span class="muted">—</span>' : escapeHtml(v)}</td>`;
              })
              .join("")}</tr>`,
        )
        .join("");
    }

    $("#total-products").textContent = allRows.length;
    $("#product-count").textContent = `${list.length} row${
      list.length === 1 ? "" : "s"
    } · ${columns.length} column${columns.length === 1 ? "" : "s"}`;
    const from = list.length ? page * perPage + 1 : 0;
    $("#page-label").textContent = `Showing ${from}–${Math.min(
      (page + 1) * perPage,
      list.length,
    )} of ${list.length}`;
    $("#prev").disabled = page === 0;
    $("#next").disabled = (page + 1) * perPage >= list.length;
  }

  /* The "product category" stat used to read a hardcoded "Home & living" with a
   * hardcoded confirmation date. It now reports what the agent actually derived,
   * including whether a human has approved it — which is the honest thing for a
   * console to show, since an unapproved profile is a guess. */
  async function loadProfileSummary() {
    const stat = $("#category-stat");
    const sub = $("#category-sub");
    try {
      const res = await fetch(`${AGENT_BASE}/ingest/report/${MERCHANT_ID}`);
      if (!res.ok) throw new Error(String(res.status));
      const r = await res.json();
      stat.textContent = r.category || "—";
      sub.textContent =
        r.status === "approved"
          ? `Approved · version ${r.version}`
          : `Draft — needs your review`;
      sub.className = r.status === "approved" ? "positive" : "needs-action";
    } catch {
      stat.textContent = "Not analyzed";
      sub.textContent = "Upload a catalog to derive one";
      sub.className = "";
    }
  }

  /* Kept current without being asked. A sale decrements stock in the merchant's raw
   * rows, and a colleague may upload from another tab — neither of which this screen
   * would otherwise ever hear about.
   *
   * Polling is suspended while the tab is hidden or another view is open, so a console
   * left open overnight is not still hammering the service in the morning. A hidden tab
   * refreshes the moment it comes back rather than waiting out the interval. */
  const POLL_MS = 10000;
  let poller = null;

  const pollingShouldRun = () =>
    document.visibilityState === "visible" &&
    !$("#catalog-view").classList.contains("hidden");

  function syncPolling() {
    if (pollingShouldRun()) {
      if (poller === null) {
        poller = setInterval(() => loadCatalog({ quiet: true }), POLL_MS);
      }
    } else if (poller !== null) {
      clearInterval(poller);
      poller = null;
    }
  }

  document.addEventListener("visibilitychange", () => {
    syncPolling();
    if (pollingShouldRun()) loadCatalog({ quiet: true });
  });

  const reload = document.querySelector("#reload-review");
  if (reload) reload.onclick = () => window.ToesReview.load();

  $("#refresh-catalog").onclick = () => loadCatalog();

  $("#search").oninput = () => {
    page = 0;
    renderCatalog();
  };
  $("#prev").onclick = () => {
    if (page > 0) page--;
    renderCatalog();
  };
  $("#next").onclick = () => {
    page++;
    renderCatalog();
  };


  // --- upload ----------------------------------------------------------------

  /* Two-step by design: choosing a file only STAGES it. The merchant sees what
   * they picked, presses Ingest, watches both requests happen, and lands on an
   * unambiguous "uploaded" state. The previous version fired the upload from the
   * file picker's change event and auto-dismissed the modal 1.2s later, which
   * looked identical whether it had worked or not. */

  const modal = $("#upload-modal");
  let pickedFile = null;

  function resetModal() {
    pickedFile = null;
    $("#file-input").value = "";
    $("#file-picked").classList.add("hidden");
    $("#ingest-btn").disabled = true;
    $("#ingest-btn").innerHTML = "Ingest catalog";
    $("#upload-form").classList.remove("hidden");
    $("#upload-done").classList.add("hidden");
    $("#upload-feedback").className = "";
    $("#upload-feedback").innerHTML = "";
  }

  const openModal = () => {
    resetModal();
    modal.classList.remove("hidden");
  };
  const closeModal = () => modal.classList.add("hidden");

  $("#open-upload").onclick = openModal;
  $("#replace-upload").onclick = openModal;
  $("#close-modal").onclick = closeModal;
  $("#cancel-upload").onclick = closeModal;
  modal.onclick = (e) => {
    if (e.target === modal) closeModal();
  };

  const prettySize = (bytes) =>
    bytes < 1024
      ? `${bytes} B`
      : bytes < 1024 * 1024
        ? `${(bytes / 1024).toFixed(1)} KB`
        : `${(bytes / 1024 / 1024).toFixed(2)} MB`;

  function pickFile(file) {
    if (!file) return;
    // Merchants keep catalogs in spreadsheets. Insisting on CSV made "export to CSV
    // first" a precondition for using the product at all — and the backend reads all
    // of these, so refusing them here was the console inventing a limit of its own.
    if (!/\.(csv|tsv|txt|xlsx|xls)$/i.test(file.name)) {
      say(
        "error",
        "Choose an Excel file (.xlsx, .xls) or a delimited file (.csv, .tsv, .txt).",
      );
      return;
    }
    pickedFile = file;
    $("#file-name").textContent = file.name;
    $("#file-size").textContent = `${prettySize(file.size)} · ready to ingest`;
    $("#file-picked").classList.remove("hidden");
    $("#ingest-btn").disabled = false;
    $("#upload-feedback").className = "";
    $("#upload-feedback").innerHTML = "";
  }

  $("#clear-file").onclick = (e) => {
    e.preventDefault();
    resetModal();
  };

  const dropzone = $("#dropzone");
  ["dragover", "dragenter"].forEach((ev) =>
    dropzone.addEventListener(ev, (e) => {
      e.preventDefault();
      dropzone.classList.add("dragging");
    }),
  );
  ["dragleave", "drop"].forEach((ev) =>
    dropzone.addEventListener(ev, () => dropzone.classList.remove("dragging")),
  );
  dropzone.addEventListener("drop", (e) => {
    e.preventDefault();
    pickFile(e.dataTransfer.files[0]);
  });
  $("#file-input").onchange = (e) => pickFile(e.target.files[0]);

  function say(kind, html) {
    const el = $("#upload-feedback");
    el.className = `feedback ${kind}`;
    el.innerHTML = html;
  }

  /* A two-line progress list, so it is obvious which of the two calls is in
   * flight and which has already landed. */
  function progress(step, detail) {
    const done = (n) => (step > n ? "done" : step === n ? "busy" : "todo");
    const mark = (s) =>
      s === "done" ? "✓" : s === "busy" ? '<span class="spinner"></span>' : "○";
    const rows = [
      ["Storing your rows with the merchant service", done(1)],
      ["Asking the agent what these columns mean", done(2)],
    ];
    say(
      "busy",
      `<ol class="upload-steps">${rows
        .map(
          ([label, state]) =>
            `<li class="${state}"><i>${mark(state)}</i>${escapeHtml(label)}</li>`,
        )
        .join("")}</ol>${detail ? `<p class="step-detail">${detail}</p>` : ""}`,
    );
  }

  $("#ingest-btn").onclick = () => {
    if (pickedFile) ingest(pickedFile);
  };

  $("#done-close").onclick = () => {
    closeModal();
    signature = null;
    loadCatalog();
  };
  $("#done-review").onclick = () => {
    closeModal();
    signature = null;
    loadCatalog();
    showView("review");
  };

  async function ingest(file) {
    const btn = $("#ingest-btn");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Ingesting…';
    $("#cancel-upload").disabled = true;
    progress(1);

    // 1. The merchant stores the raw rows, verbatim.
    let report;
    try {
      const form = new FormData();
      form.append("file", file);
      form.append("merchant_id", MERCHANT_ID);
      const res = await fetch(`${MERCHANT_BASE}/catalog/upload`, {
        method: "POST",
        body: form,
      });
      report = await res.json();
      // A sheet the console's normalizer rejects is still stored as raw rows and is
      // still usable by the agent, so a 422 here is a warning, not a dead end. The
      // table below reads those raw rows, so it shows this upload either way.
      if (!res.ok && res.status !== 422) {
        const detail = report.detail;
        throw new Error(
          (typeof detail === "string" ? detail : detail?.message) || `HTTP ${res.status}`,
        );
      }
    } catch (err) {
      say("error", `Upload failed: ${escapeHtml(err.message)}`);
      btn.disabled = false;
      btn.innerHTML = "Try again";
      $("#cancel-upload").disabled = false;
      return;
    }

    // The row count lives on the report, or on `raw` when normalization rejected the
    // sheet. The old code read `rows_stored`/`received`, neither of which this endpoint
    // has ever returned, so this line said "Stored ? rows" on every single upload.
    const stored = report.raw?.row_count ?? report.report?.rows_in ?? "?";
    progress(2, `Stored <strong>${escapeHtml(stored)}</strong> rows.`);

    // 2. The agent derives a profile from those raw rows.
    try {
      const res = await fetch(`${AGENT_BASE}/ingest/analyze`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ merchant_id: MERCHANT_ID, use_llm: true }),
      });
      const body = await res.json();
      if (!res.ok) throw new Error(JSON.stringify(body.detail || body));

      const p = body.profile;
      $("#done-file").innerHTML = `<strong>${escapeHtml(
        file.name,
      )}</strong> is now your live catalog.`;
      $("#done-stats").innerHTML = [
        ["Rows stored", p.source.row_count],
        ["Columns read", p.source.column_count],
        ["Fields derived", p.fields.length],
        ["Category", p.category],
      ]
        .map(
          ([label, value]) =>
            `<div><small>${escapeHtml(label)}</small><b>${escapeHtml(value)}</b></div>`,
        )
        .join("");
      $("#upload-form").classList.add("hidden");
      $("#upload-done").classList.remove("hidden");
      signature = null;
      loadCatalog();
    } catch (err) {
      say(
        "error",
        `The catalog was stored, but analysis failed: ${escapeHtml(err.message)}`,
      );
      btn.disabled = false;
      btn.innerHTML = "Retry analysis";
    } finally {
      $("#cancel-upload").disabled = false;
    }
  }

  loadCatalog();
  syncPolling();
})();
