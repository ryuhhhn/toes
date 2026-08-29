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

  let allProducts = [];
  let page = 0;
  const perPage = 8;

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
  }

  $$(".nav-item").forEach((b) => (b.onclick = () => showView(b.dataset.view)));

  // --- the catalog table -----------------------------------------------------

  /* The console's own normalized view. Deliberately tolerant about field names:
   * this table is a merchant convenience, and it must not become a second place
   * that decides what a product "is". */
  async function loadCatalog() {
    const body = $("#product-rows");
    body.innerHTML = `<tr><td colspan="5" class="table-note">Loading your catalog…</td></tr>`;
    try {
      const res = await fetch(
        `${MERCHANT_BASE}/catalog?merchant_id=${encodeURIComponent(MERCHANT_ID)}`,
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      allProducts = Array.isArray(data) ? data : data.products || data.rows || [];
    } catch (err) {
      allProducts = [];
      body.innerHTML = `<tr><td colspan="5" class="table-note error">
        Could not reach the merchant service at ${escapeHtml(MERCHANT_BASE)} — ${escapeHtml(
          err.message,
        )}</td></tr>`;
      $("#total-products").textContent = "—";
      $("#product-count").textContent = "catalog unavailable";
      return;
    }
    page = 0;
    renderCatalog();
    loadProfileSummary();
  }

  function renderCatalog() {
    const q = $("#search").value.trim().toLowerCase();
    const list = q
      ? allProducts.filter((p) => JSON.stringify(p).toLowerCase().includes(q))
      : allProducts;

    const shown = list.slice(page * perPage, page * perPage + perPage);
    const body = $("#product-rows");

    if (!shown.length) {
      body.innerHTML = `<tr><td colspan="5" class="table-note">${
        allProducts.length ? "Nothing matches that search." : "No products yet — upload a CSV to begin."
      }</td></tr>`;
    } else {
      body.innerHTML = shown
        .map(
          (p) => `<tr>
            <td><strong>${escapeHtml(p.name ?? p.title ?? "—")}</strong></td>
            <td>${escapeHtml(p.sku ?? p.product_id ?? p.id ?? "—")}</td>
            <td>${p.category ? `<span class="tag">${escapeHtml(p.category)}</span>` : "—"}</td>
            <td>${p.price != null ? escapeHtml(p.price) : "—"}</td>
            <td>${p.stock != null ? escapeHtml(p.stock) : "—"}</td>
          </tr>`,
        )
        .join("");
    }

    $("#total-products").textContent = allProducts.length;
    $("#product-count").textContent = `${list.length} product${
      list.length === 1 ? "" : "s"
    } in your catalog`;
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

  const reload = document.querySelector("#reload-review");
  if (reload) reload.onclick = () => window.ToesReview.load();

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
    if (!/\.csv$/i.test(file.name)) {
      say("error", "That is not a CSV. Choose a .csv file to continue.");
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
    loadCatalog();
  };
  $("#done-review").onclick = () => {
    closeModal();
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
      // still usable by the agent, so a 422 here is a warning, not a dead end.
      if (!res.ok && res.status !== 422) {
        throw new Error(report.detail || `HTTP ${res.status}`);
      }
    } catch (err) {
      say("error", `Upload failed: ${escapeHtml(err.message)}`);
      btn.disabled = false;
      btn.innerHTML = "Try again";
      $("#cancel-upload").disabled = false;
      return;
    }

    const stored = report.rows_stored ?? report.received ?? "?";
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
})();
