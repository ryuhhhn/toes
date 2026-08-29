/* The profile approval screen.
 *
 * This is where a merchant sees what the agent inferred from their spreadsheet and
 * either agrees with it or corrects it. The agent side was already built for this
 * screen — `GET /ingest/report/{id}` returns exactly what it renders and
 * `PUT /ingest/profile/{id}` takes the edits back. Until now nothing called either.
 *
 * Two documents, deliberately:
 *   - the REPORT is the human view. It carries values the server derived for display
 *     (`read_as`, `aliases_collapsed`, role confidences) that would otherwise have to
 *     be recomputed here, badly.
 *   - the PROFILE is the machine object. It is what gets edited and PUT back.
 * They are joined on the column name. Rendering from the report and submitting the
 * profile means the screen never has to reconstruct a profile out of display strings.
 *
 * No column, value or attribute of any particular product category may be named in
 * this file. It renders whatever the merchant uploaded — the same rule that binds
 * the agent backend and the storefront.
 */

(function () {
  const { AGENT_BASE, MERCHANT_ID } = window.TOES;
  const $ = (s) => document.querySelector(s);

  /** The profile as loaded, mutated in place by the controls below. */
  let profile = null;
  /** Column names the merchant actually touched — merge.py preserves exactly these. */
  const editedFields = new Set();

  const escapeHtml = (v) =>
    String(v ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
    );

  const pct = (n) => `${Math.round((n ?? 0) * 100)}%`;

  /** Confidence is meaningless without a sense of scale, so it is banded, not just printed. */
  function confidenceClass(c) {
    if (c >= 0.85) return "high";
    if (c >= 0.6) return "medium";
    return "low";
  }

  async function loadReview() {
    const host = $("#review-body");
    host.innerHTML = `<div class="review-empty">Loading the derived profile…</div>`;

    let report, profileDoc;
    try {
      const [r1, r2] = await Promise.all([
        fetch(`${AGENT_BASE}/ingest/report/${MERCHANT_ID}`),
        fetch(`${AGENT_BASE}/ingest/profile/${MERCHANT_ID}`),
      ]);
      if (r1.status === 404 || r2.status === 404) {
        host.innerHTML = `<div class="review-empty">
          <strong>No profile yet for “${escapeHtml(MERCHANT_ID)}”.</strong>
          <p>Upload a catalog first — the agent derives a profile from it, and this
          screen is where you approve what it found.</p></div>`;
        return;
      }
      if (!r1.ok || !r2.ok) throw new Error(`HTTP ${r1.status} / ${r2.status}`);
      report = await r1.json();
      profileDoc = await r2.json();
    } catch (err) {
      host.innerHTML = `<div class="review-empty error">
        <strong>Could not reach the agent.</strong>
        <p>${escapeHtml(err.message)} — is it running on ${escapeHtml(AGENT_BASE)}?</p></div>`;
      return;
    }

    profile = profileDoc.profile;
    editedFields.clear();
    (profile.edited_fields || []).forEach((f) => editedFields.add(f));

    render(report);
  }

  function render(report) {
    const byColumn = new Map(profile.fields.map((f) => [f.column, f]));
    const roles = report.roles || {};
    const conf = roles.confidence || {};

    // Without id, title and price there is no checkout. This is the single most
    // important thing this screen can tell a merchant, so it is stated first and loudly.
    const missing = ["id", "title", "price"].filter((r) => !roles[r]);

    $("#review-body").innerHTML = `
      ${headerCard(report)}
      ${missing.length ? missingBanner(missing) : ""}
      ${rolesCard(roles, conf)}
      ${notesCard(report.notes || [])}
      ${fieldsCard(report.fields || [], byColumn)}
      ${rulesCard(report.proposed_rules || [])}
      ${submitCard(report)}
    `;
    wire(byColumn);
  }

  function headerCard(r) {
    const derived =
      r.derived_by === "llm"
        ? `<span class="chip">Derived by model</span>`
        : `<span class="chip neutral">Derived by rules only — no model was used</span>`;
    const status =
      r.status === "approved"
        ? `<span class="chip ok">Approved</span>`
        : `<span class="chip warn">Draft — not yet approved</span>`;
    const src = r.source || {};
    return `
      <div class="review-card review-header">
        <div>
          <span class="eyebrow">DERIVED PROFILE</span>
          <h3>${escapeHtml(r.category || "—")}</h3>
          <p>Read from <strong>${escapeHtml(src.filename || "your catalog")}</strong>${
            src.sheet ? ` · sheet ${escapeHtml(src.sheet)}` : ""
          } · ${src.row_count ?? "?"} rows × ${src.column_count ?? "?"} columns</p>
        </div>
        <div class="review-chips">
          ${status}${derived}
          <span class="chip neutral">Version ${r.version}</span>
          <span class="chip neutral">Category confidence ${pct(r.category_confidence)}</span>
        </div>
      </div>`;
  }

  function missingBanner(missing) {
    return `<div class="review-card danger">
      <strong>Checkout cannot work yet.</strong>
      <p>No column was identified as <strong>${missing.join("</strong>, <strong>")}</strong>.
      The agent can still talk about your products, but it cannot put them in a basket or
      price them until these are set.</p>
    </div>`;
  }

  function rolesCard(roles, conf) {
    const named = [
      ["id", "Product ID", "How each product is uniquely identified."],
      ["title", "Product name", "What the shopper is shown and what the agent calls it."],
      ["price", "Price", "What the shopper is charged. Re-checked before every payment."],
      ["stock", "Stock", "Whether it can be bought right now."],
      ["image", "Image", "Optional. Product cards render without one."],
    ];
    const cards = named
      .map(([key, label, why]) => {
        const column = roles[key];
        const c = (conf[key] || {}).confidence ?? 0;
        const reason = (conf[key] || {}).reason || "";
        return `<article class="role-card${column ? "" : " unmapped"}">
          <header>
            <h5>${label}</h5>
            <span class="conf ${confidenceClass(c)}">${pct(c)}</span>
          </header>
          <div class="role-column">${
            column
              ? `<code>${escapeHtml(column)}</code>`
              : `<span class="chip warn">no column found</span>`
          }</div>
          <p>${escapeHtml(why)}</p>
          ${reason ? `<small>${escapeHtml(reason)}</small>` : ""}
        </article>`;
      })
      .join("");
    return `<div class="review-card">
      <h4>Which column means what</h4>
      <p class="sub">The agent worked these out from your column names and the data in them.
      If any is wrong, the fix is in your spreadsheet's headings.</p>
      <div class="role-grid">${cards}</div></div>`;
  }

  function notesCard(notes) {
    if (!notes.length) return "";
    return `<div class="review-card">
      <h4>What the pipeline could not parse, or had to guess</h4>
      <ul class="notes">${notes
        .map((n) => `<li>${escapeHtml(n)}</li>`)
        .join("")}</ul></div>`;
  }

  function fieldsCard(fields, byColumn) {
    /* A table, not a card per column. A catalog with twenty columns became twenty
     * stacked cards and a page nobody scrolled to the end of; one row per column is
     * scannable, and comparing tiers down a column is the whole point of this screen.
     * Cells wrap rather than pushing the table wider, so the merchant's own decisions
     * — required, hidden — stay on screen instead of off the right edge. */
    const rows = fields
      .map((f, i) => {
        const p = byColumn.get(f.column) || {};
        const values = (f.values || []).slice(0, 4);
        const sample = values.length
          ? values.map((v) => `<span class="val">${escapeHtml(v)}</span>`).join("")
          : `<span class="val empty">no values read</span>`;
        const quality = [];
        if (f.empty_rate > 0) quality.push(`${pct(f.empty_rate)} empty`);
        if (f.unparseable_cells > 0) quality.push(`${f.unparseable_cells} unreadable`);
        if (f.aliases_collapsed > 0) quality.push(`${f.aliases_collapsed} spellings merged`);
        if (f.unit) quality.push(`unit ${escapeHtml(f.unit)}`);
        if (f.currency) quality.push(`currency ${escapeHtml(f.currency)}`);

        return `<tr class="field-row${p.hidden ? " is-hidden" : ""}" data-column="${escapeHtml(f.column)}">
          <td class="col-name">
            <strong>${escapeHtml(f.column)}</strong>
            <small>read as ${escapeHtml(f.read_as || f.kind)}</small>
            ${quality.length ? `<small class="quality">${quality.join(" · ")}</small>` : ""}
          </td>
          <td class="col-values"><div class="field-values">${sample}</div></td>
          <td class="col-tier">
            <select class="f-tier" data-i="${i}" aria-label="Tier for ${escapeHtml(f.column)}">
              ${[1, 2, 3]
                .map(
                  (t) =>
                    `<option value="${t}" ${p.tier === t ? "selected" : ""}>${t}</option>`,
                )
                .join("")}
            </select>
          </td>
          <td class="col-input">
            <input class="f-layman" data-i="${i}" value="${escapeHtml(p.layman_name || "")}"
                   placeholder="${escapeHtml(f.column)}"
                   aria-label="What to call ${escapeHtml(f.column)}" />
          </td>
          <td class="col-input">
            <input class="f-why" data-i="${i}" value="${escapeHtml(p.why_it_matters || "")}"
                   placeholder="why a shopper would care"
                   aria-label="Why ${escapeHtml(f.column)} matters" />
          </td>
          <td class="col-tick">
            <input type="checkbox" class="f-required" data-i="${i}"
                   ${p.required_before_purchase ? "checked" : ""}
                   aria-label="Require ${escapeHtml(f.column)} before purchase" />
            <small class="suggested">model: <b>${f.suggested_required ? "required" : "optional"}</b></small>
          </td>
          <td class="col-tick">
            <input type="checkbox" class="f-hidden" data-i="${i}"
                   ${p.hidden ? "checked" : ""}
                   aria-label="Hide ${escapeHtml(f.column)} from the agent" />
          </td>
        </tr>`;
      })
      .join("");

    return `<div class="review-card">
      <h4>Every column the agent read</h4>
      <p class="sub">Tier 1 columns are the ones the agent leans on when helping someone
      choose. <strong>Model</strong> is its opinion on whether a column must be settled;
      <strong>Required</strong> is your decision — the agent will refuse to check out until
      a required field is settled with the shopper.</p>
      <div class="field-table-wrap">
        <table class="field-table">
          <thead>
            <tr>
              <th>Column</th>
              <th>Sample values</th>
              <th class="col-tier">Tier</th>
              <th>Call it</th>
              <th>Why it matters to a shopper</th>
              <th class="col-tick">Required</th>
              <th class="col-tick">Hidden</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div></div>`;
  }

  function rulesCard(rules) {
    if (!rules.length) {
      return `<div class="review-card">
        <h4>Cross-field rules</h4>
        <p class="sub">None proposed for this catalog. Rules are the one part of the
        pipeline that cannot be checked against your data, so they only ever reach a
        shopper after you approve them here.</p></div>`;
    }
    const cards = rules
      .map(
        (r, i) => `<article class="rule-card" data-rule="${i}">
        <label class="tick approve">
          <input type="checkbox" class="r-approve" data-i="${i}" />
          <span>Approve this rule</span>
        </label>
        <div class="rule-logic">
          <code>${escapeHtml(r.if ?? r["if"] ?? "")}</code>
          <span class="then">then ${escapeHtml(r.then ?? "")}</span>
        </div>
        <label class="rule-message">
          <span>What the shopper is told</span>
          <input class="r-message" data-i="${i}" value="${escapeHtml(r.message || "")}" />
        </label>
        <footer>
          <span class="rule-columns">${(r.columns || [])
            .map((c) => `<code>${escapeHtml(c)}</code>`)
            .join(" ")}</span>
          <button class="link-danger r-delete" data-i="${i}">Delete</button>
        </footer>
      </article>`,
      )
      .join("");
    return `<div class="review-card">
      <h4>Cross-field rules</h4>
      <p class="sub"><strong>Unapproved by default.</strong> These are the only claims the
      agent cannot verify against your data, so each one needs your signature before it can
      be said to a shopper.</p>
      <div class="rule-list">${cards}</div></div>`;
  }

  function submitCard(r) {
    return `<div class="review-card submit-card">
      <div>
        <strong>Approve this profile</strong>
        <p class="sub">Approving makes it the version the agent uses and rebuilds the
        search index. Your edits are remembered: re-uploading this catalog will not
        overwrite the columns you changed here.</p>
        <p class="sub" id="edited-summary"></p>
      </div>
      <div class="submit-actions">
        <label class="reindex-toggle">
          <input type="checkbox" id="do-reindex" checked /> Rebuild the search index
        </label>
        <button class="primary" id="approve-profile">
          ${r.status === "approved" ? "Save changes" : "Approve and publish"}
        </button>
      </div>
      <div id="approve-feedback"></div>
    </div>`;
  }

  // --- editing ---------------------------------------------------------------

  function wire(byColumn) {
    const fieldByIndex = (i) => profile.fields[Number(i)];

    const mark = (field) => {
      editedFields.add(field.column);
      updateEditedSummary();
    };

    document.querySelectorAll(".f-tier").forEach((el) => {
      el.onchange = () => {
        const f = fieldByIndex(el.dataset.i);
        f.tier = Number(el.value);
        mark(f);
      };
    });
    document.querySelectorAll(".f-layman").forEach((el) => {
      el.onchange = () => {
        const f = fieldByIndex(el.dataset.i);
        f.layman_name = el.value.trim() || null;
        mark(f);
      };
    });
    document.querySelectorAll(".f-why").forEach((el) => {
      el.onchange = () => {
        const f = fieldByIndex(el.dataset.i);
        f.why_it_matters = el.value.trim() || null;
        mark(f);
      };
    });
    document.querySelectorAll(".f-required").forEach((el) => {
      el.onchange = () => {
        const f = fieldByIndex(el.dataset.i);
        f.required_before_purchase = el.checked;
        mark(f);
      };
    });
    document.querySelectorAll(".f-hidden").forEach((el) => {
      el.onchange = () => {
        const f = fieldByIndex(el.dataset.i);
        f.hidden = el.checked;
        el.closest("[data-column]").classList.toggle("is-hidden", el.checked);
        mark(f);
      };
    });

    // Rules are approved by being carried into the profile. An unticked rule is
    // simply not submitted, which is what "unapproved by default" has to mean.
    document.querySelectorAll(".r-delete").forEach((el) => {
      el.onclick = () => el.closest("[data-rule]").remove();
    });

    const btn = $("#approve-profile");
    if (btn) btn.onclick = submit;
    updateEditedSummary();
  }

  function updateEditedSummary() {
    const el = $("#edited-summary");
    if (!el) return;
    el.textContent = editedFields.size
      ? `You have edited: ${[...editedFields].join(", ")}`
      : "No edits yet — approving accepts everything above as-is.";
  }

  function collectApprovedRules() {
    const approved = [];
    document.querySelectorAll("[data-rule]").forEach((row) => {
      const check = row.querySelector(".r-approve");
      if (!check || !check.checked) return;
      const i = Number(check.dataset.i);
      const rule = (profile.cross_field_rules || [])[i];
      if (!rule) return;
      const msg = row.querySelector(".r-message");
      approved.push({ ...rule, message: msg ? msg.value : rule.message });
    });
    return approved;
  }

  async function submit() {
    const btn = $("#approve-profile");
    const feedback = $("#approve-feedback");
    btn.disabled = true;
    const original = btn.textContent;
    btn.textContent = "Approving…";
    feedback.className = "";
    feedback.textContent = "";

    // Only ticked rules survive. This is the approval, not a formality.
    profile.cross_field_rules = collectApprovedRules();

    try {
      const res = await fetch(`${AGENT_BASE}/ingest/profile/${MERCHANT_ID}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          profile,
          approved_by: "merchant-console",
          // merge.py preserves exactly these columns across a re-ingest. Getting the
          // list wrong silently loses the merchant's work on their next upload.
          edited_fields: [...editedFields],
          reindex: $("#do-reindex").checked,
        }),
      });
      const body = await res.json();
      if (!res.ok) throw new Error(JSON.stringify(body.detail || body));

      const reindex = body.reindex;
      const built =
        reindex && reindex.ok !== false
          ? ` Index rebuilt: ${reindex.rows ?? "?"} rows, ${
              reindex.embedding_model ?? "no embeddings"
            }.`
          : reindex
            ? ` Index rebuild failed: ${escapeHtml(reindex.error || "unknown")}.`
            : "";
      feedback.className = "feedback success";
      feedback.textContent = `Approved as version ${body.profile.version}.${built}`;
      btn.textContent = "Save changes";
      editedFields.clear();
      (body.profile.edited_fields || []).forEach((f) => editedFields.add(f));
      updateEditedSummary();
    } catch (err) {
      feedback.className = "feedback error";
      feedback.textContent = `Could not approve: ${err.message}`;
      btn.textContent = original;
    } finally {
      btn.disabled = false;
    }
  }

  window.ToesReview = { load: loadReview };
})();
