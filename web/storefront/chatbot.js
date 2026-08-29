/* Storefront — the shopper's side of the conversation.
 *
 * Everything rendered here comes from a TYPED SSE event. Nothing is scraped out of
 * the token stream: prose is for reading, structure arrives as `products`,
 * `comparison`, `probe`, `preview`, `cart`, `notice`, `receipt` and `error`. A
 * frontend that parses structure out of prose breaks on the next prompt edit.
 *
 * LAYOUT: a 60/40 landscape split. The conversation (prose, probes, comparison
 * tables) lives on the left; PRODUCTS AND THE CART NEVER DO. Product cards render
 * into the right-hand panel, which is always on screen, so a shopper never scrolls
 * back up a transcript to find what was recommended three turns ago. The chat gets
 * a one-line pointer instead.
 *
 * THE RULE THAT MATTERS MOST: no category-specific field name may appear in this
 * file. Product cards render from `attributes[]`, each carrying the column, the
 * merchant-approved layman `label`, and a preformatted `display` string. A
 * storefront that can only draw one kind of product breaks the core claim as surely
 * as a backend that hardcodes one.
 *
 * Confirmation is its own POST to /chat/confirm — never inferred from chat text.
 * That mirrors the backend, where the charging tool is absent from the model's
 * schema until that request mints a token.
 */

(function () {
  const { AGENT_BASE, MERCHANT_ID } = window.TOES;
  const $ = (s) => document.querySelector(s);
  const thread = $("#thread");
  const conversation = $("#conversation");

  /* Four is the cap for both recommendations and probe options. More than that in a
   * 40%-wide panel stops being a choice and starts being a list to wade through. */
  const MAX_OPTIONS = 4;

  let sessionId = null;
  let activePreview = null;
  let busy = false;
  /** Last rendered quantity per product id — what a stepper press steps from. */
  let cartQuantities = new Map();

  const esc = (v) =>
    String(v ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
    );

  const bottom = () => (thread.scrollTop = thread.scrollHeight);

  /* Money is formatted from the server's currency, never a hardcoded symbol. The
   * total the shopper is shown is the total the server computed — it is never
   * recomputed here, because a client-side sum that disagrees with the ledger is
   * the worst possible bug in a checkout. */
  function money(amount, currency) {
    if (amount == null) return "";
    try {
      return new Intl.NumberFormat(undefined, {
        style: "currency",
        currency: currency || "USD",
      }).format(amount);
    } catch {
      return `${amount} ${currency || ""}`.trim();
    }
  }

  // --- transport -------------------------------------------------------------

  /* POST + ReadableStream, not EventSource: EventSource cannot send a request body,
   * and both /chat and /chat/confirm need one. */
  async function stream(path, body, handlers) {
    const res = await fetch(`${AGENT_BASE}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    if (!res.ok) {
      let detail;
      try {
        detail = (await res.json()).detail;
      } catch {
        detail = null;
      }
      const d = typeof detail === "object" && detail ? detail : {};
      handlers.error?.({
        code: d.code || `http_${res.status}`,
        message:
          d.message ||
          (typeof detail === "string" ? detail : `Request failed (${res.status}).`),
      });
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // Frames are separated by a blank line. Keep the trailing partial in the buffer.
      const frames = buffer.split("\n\n");
      buffer = frames.pop();

      for (const frame of frames) {
        let type = null;
        const dataLines = [];
        for (const line of frame.split("\n")) {
          if (line.startsWith("event:")) type = line.slice(6).trim();
          else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
          // ": keepalive" comments are ignored, which is what they are for.
        }
        if (!type || !dataLines.length) continue;
        let payload;
        try {
          payload = JSON.parse(dataLines.join("\n"));
        } catch {
          continue;
        }
        handlers[type]?.(payload);
      }
    }
  }

  // --- chat-side rendering ---------------------------------------------------

  function userBubble(text) {
    conversation.insertAdjacentHTML(
      "beforeend",
      `<div class="message user"><div class="bubble">${esc(text)}</div></div>`,
    );
    bottom();
  }

  /* One agent bubble per turn, filled as `token` events arrive. */
  function newAgentBubble() {
    const wrap = document.createElement("div");
    wrap.className = "message agent";
    wrap.innerHTML = '<div class="bubble"></div>';
    conversation.appendChild(wrap);
    const bubble = wrap.querySelector(".bubble");
    let text = "";
    return {
      append(chunk) {
        text += chunk;
        bubble.textContent = text;
        bottom();
      },
      finish() {
        if (!text.trim()) wrap.remove();
      },
    };
  }

  function toolChip(summary) {
    const el = document.createElement("div");
    el.className = "tool-chip";
    el.innerHTML = `<span class="spinner"></span>${esc(summary)}`;
    conversation.appendChild(el);
    bottom();
    return el;
  }

  // --- the product panel (right, always on screen) ---------------------------

  function productCard(p, rank) {
    // Up to four attributes, rendered from whatever the merchant's catalog actually
    // has. `label` is the merchant-approved name for the column; `display` is already
    // formatted (units, lists, booleans) by the backend. The label is set in small
    // caps above the value, and neither is larger than the product name.
    const attrs = (p.attributes || [])
      .slice(0, MAX_OPTIONS)
      .map(
        (a) =>
          `<div class="attr"><span>${esc(a.label || a.column)}</span><b title="${esc(
            a.display || a.value,
          )}">${esc(a.display || a.value)}</b></div>`,
      )
      .join("");

    const low = p.in_stock && p.stock != null && p.stock <= 5;
    const badge = !p.in_stock
      ? '<span class="stock-badge out">Out of stock</span>'
      : low
        ? '<span class="stock-badge low">Low stock</span>'
        : '<span class="stock-badge">In stock</span>';

    return `<article class="product-card" data-id="${esc(p.id)}">
      <div class="product-image${p.image ? "" : " placeholder"}"${
        p.image ? ` style="background-image:url('${esc(p.image)}')"` : ""
      }>
        <span class="rank">Pick ${rank}</span>${badge}
      </div>
      <div class="product-copy">
        <div class="name">${esc(p.title)}</div>
        ${p.description ? `<div class="desc">${esc(p.description)}</div>` : ""}
        ${attrs ? `<div class="attrs">${attrs}</div>` : ""}
        <div class="price-row"><span class="price">${esc(
          money(p.price, p.currency),
        )}</span></div>
      </div>
      <button class="add" data-id="${esc(p.id)}" ${p.in_stock ? "" : "disabled"}>
        ${p.in_stock ? "Add to basket" : "Unavailable"}
      </button>
    </article>`;
  }

  function renderProducts(d) {
    if (!d.items || !d.items.length) return;
    const items = d.items.slice(0, MAX_OPTIONS);

    // Mid-checkout, fill the panel but do not yank the shopper out of it.
    if (!activePreview) showPane("products");
    $("#product-empty").classList.add("hidden");
    $("#product-list").innerHTML = items
      .map((p, i) => productCard(p, i + 1))
      .join("");

    const count = $("#products-count");
    count.textContent = items.length;
    count.classList.remove("hidden");
    $("#products-note").textContent =
      d.total_candidates > items.length
        ? `Top ${items.length} of ${d.total_candidates} matches`
        : `${items.length} option${items.length === 1 ? "" : "s"} for you`;

    // An honest "I widened the search" is the difference between a relaxed result and
    // an apparent wrong answer. It belongs beside the results, not in the transcript.
    const caveat = $("#product-caveat");
    if (d.filters_relaxed && d.filters_relaxed.length) {
      const what = d.filters_relaxed
        .map((f) => f.description || f.column || JSON.stringify(f))
        .join(", ");
      caveat.innerHTML = `<div class="caveat">Nothing matched exactly, so I widened the search: ${esc(
        what,
      )}</div>`;
    } else {
      caveat.innerHTML = "";
    }

    $("#product-list")
      .querySelectorAll(".add")
      .forEach((b) => {
        b.onclick = () => send(`Add ${b.dataset.id} to my basket`);
      });
    $("#product-scroll").scrollTop = 0;

    // The conversation gets a pointer, not a second copy of the cards.
    conversation.insertAdjacentHTML(
      "beforeend",
      `<div class="panel-pointer">▦ ${items.length} option${
        items.length === 1 ? "" : "s"
      } in the panel on the right →</div>`,
    );
    bottom();
  }

  // --- comparison (stays in the chat) ---------------------------------------

  function renderComparison(d) {
    if (!d.rows || !d.rows.length) return;
    const axes = d.axes || [];
    const head = axes
      .map((a) => `<th>${esc(a.label || a.column || a)}</th>`)
      .join("");
    const body = d.rows
      .map(
        (r) =>
          `<tr><th>${esc(r.title || r.id)}</th>${axes
            .map((a) => {
              const key = a.column || a.label || a;
              const cell = (r.values || r)[key];
              const v =
                cell && typeof cell === "object" ? cell.display ?? cell.value : cell;
              return `<td>${esc(v ?? "—")}</td>`;
            })
            .join("")}</tr>`,
      )
      .join("");
    conversation.insertAdjacentHTML(
      "beforeend",
      `<div class="comparison"><table><thead><tr><th></th>${head}</tr></thead>
       <tbody>${body}</tbody></table></div>`,
    );
    bottom();
  }

  function renderProbe(d) {
    const options = (d.options || [])
      .slice(0, MAX_OPTIONS)
      .map(
        (o, i) =>
          `<button class="chip-option" data-v="${esc(o)}"><i>${i + 1}</i>${esc(
            o,
          )}</button>`,
      )
      .join("");
    const el = document.createElement("div");
    el.className = "probe";
    el.innerHTML = `
      <div class="probe-head">
        <div class="probe-icon">?</div>
        <div>
          <span class="probe-title">${esc(d.question)}</span>
          ${d.why_it_matters ? `<span class="probe-why">${esc(d.why_it_matters)}</span>` : ""}
        </div>
      </div>
      ${d.how_to_find_out ? `<span class="probe-how">${esc(d.how_to_find_out)}</span>` : ""}
      ${options ? `<div class="probe-options">${options}</div>` : ""}`;
    conversation.appendChild(el);
    el.querySelectorAll(".chip-option").forEach((b) => {
      b.onclick = () => send(b.dataset.v);
    });
    bottom();
  }

  function renderNotice(d) {
    // Merchant-approved cross-field warnings. Without this the rules a merchant
    // signed off in the console would have no way to reach the shopper.
    conversation.insertAdjacentHTML(
      "beforeend",
      `<div class="notice ${esc(d.level || "info")}">${esc(d.message)}</div>`,
    );
    bottom();
  }

  function renderError(d) {
    conversation.insertAdjacentHTML(
      "beforeend",
      `<div class="notice error">${esc(d.message)}</div>`,
    );
    bottom();
  }

  // --- the cart dock (bottom right) ------------------------------------------

  /* `build_cart` caps a line at 99 (MAX_QUANTITY in the tool). Mirrored here only so
   * `+` greys out at the ceiling instead of sending a turn the tool will clamp. */
  const MAX_QUANTITY = 99;

  /* One row, three fixed columns after the title, so quantity and money line up
   * down the list instead of drifting with the length of each name. `editable`
   * adds the stepper and the remove button — the basket takes them, the preview
   * and the receipt do not. A preview is a quoted price and a receipt is a record
   * of a charge; neither is a thing you edit. */
  const lineRow = (i, currency, editable = false) => {
    const qty = i.quantity ?? 1;
    const total = esc(money((i.unit_price ?? 0) * qty, currency));
    if (!editable) {
      return `<div class="order-line">
         <span class="line-title">${esc(i.title)}</span>
         <span class="qty">× ${esc(qty)}</span>
         <b>${total}</b>
       </div>`;
    }
    const id = esc(i.id);
    return `<div class="order-line editable" data-id="${id}">
       <span class="line-title" title="${esc(i.title)}">${esc(i.title)}</span>
       <span class="qty-stepper">
         <button type="button" data-cart-action="dec" data-id="${id}"
                 ${qty <= 1 ? 'disabled data-at-limit="1"' : ""}
                 aria-label="One fewer ${esc(i.title)}">−</button>
         <span class="qty" aria-label="quantity">${esc(qty)}</span>
         <button type="button" data-cart-action="inc" data-id="${id}"
                 ${qty >= MAX_QUANTITY ? 'disabled data-at-limit="1"' : ""}
                 aria-label="One more ${esc(i.title)}">+</button>
       </span>
       <b>${total}</b>
       <button type="button" class="line-remove" data-cart-action="remove" data-id="${id}"
               aria-label="Remove ${esc(i.title)} from the basket"
               title="Remove from basket">×</button>
     </div>`;
  };

  function renderCart(d) {
    const order = $("#order");
    if (!d.items || !d.items.length) {
      order.classList.add("hidden");
      return;
    }
    const count = d.items.reduce((n, i) => n + (i.quantity || 1), 0);
    order.classList.remove("hidden");
    $("#item-count").textContent = `${count} item${count === 1 ? "" : "s"}`;
    // Quantities live on the server. Keep the last rendered ones so a stepper press
    // knows what it is stepping from without reading the number back out of the DOM.
    cartQuantities = new Map(d.items.map((i) => [String(i.id), i.quantity ?? 1]));
    $("#order-lines").innerHTML = d.items
      .map((i) => lineRow(i, d.currency, true))
      .join("");
    $("#order-total").textContent = money(d.subtotal, d.currency);
    // This render happens mid-turn, while the stream that produced it is still open,
    // so the fresh buttons have to be born disabled or they would swallow a click.
    lockCart(busy);
  }

  /* The basket is the agent's, not the browser's: `session.cart` lives in the agent
   * process and `build_cart` is the only thing that may touch it, because the trust
   * gate depends on nothing else being able to. There is no REST route to it by
   * design, so a stepper press is an ordinary chat turn naming the product id. The
   * agent answers in the transcript, which keeps the conversation honest about what
   * the buttons did. */
  function cartAction(action, id) {
    if (busy || !id) return;
    const qty = cartQuantities.get(String(id)) ?? 1;
    if (action === "remove") return send(`Remove ${id} from my basket.`);
    const next = action === "inc" ? qty + 1 : qty - 1;
    if (next < 1 || next > MAX_QUANTITY) return;
    send(`Set the quantity of ${id} to ${next} in my basket.`);
  }

  /* Disabled for the duration of a turn, because `send()` drops anything sent while
   * one is open — a stepper that silently ate the click would be worse than a greyed
   * one. `data-at-limit` buttons stay disabled when the lock lifts. */
  function lockCart(locked) {
    document
      .querySelectorAll("#order-lines [data-cart-action]")
      .forEach((b) => (b.disabled = locked || b.dataset.atLimit === "1"));
  }

  function setBusy(v) {
    busy = v;
    lockCart(v);
  }

  // --- checkout --------------------------------------------------------------

  function showPane(which) {
    $("#pane-products").classList.toggle("hidden", which !== "products");
    $("#pane-checkout").classList.toggle("hidden", which !== "checkout");
  }

  /* Checkout is three stages driven by ONE button. The button sits outside the
   * scrolling area, so it is on screen whatever the window size, and its label
   * always says what pressing it will do. Stage 4 is the receipt — the same
   * button, now the way back to shopping. */
  const STAGES = [
    { id: "#customer-details", label: "Name and address", action: "Continue to card details" },
    { id: "#card-step", label: "Card details", action: "Continue to authorization" },
    { id: "#authorize-step", label: "Review & authorize", action: "Authorize and pay" },
  ];

  let stage = 1;

  function showStage(n) {
    stage = n;
    STAGES.forEach((st, i) => $(st.id).classList.toggle("hidden", i + 1 !== n));
    $("#receipt").classList.add("hidden");
    $("#checkout-step").textContent = `Step ${n} of 3 · ${STAGES[n - 1].label}`;
    markStepper(n);

    const btn = $("#co-action");
    btn.textContent = STAGES[n - 1].action;
    btn.className = n === 3 ? "confirm" : "checkout";
    $("#foot-total").classList.toggle("hidden", n !== 3);

    // Only the last stage is gated — the earlier ones validate on press, so a
    // shopper is told what is wrong rather than left with a dead button.
    if (n === 3) refreshConfirmGate();
    else btn.disabled = false;

    $("#checkout-scroll").scrollTop = 0;
  }

  function markStepper(n) {
    document.querySelectorAll("#stepper li").forEach((li) => {
      const i = Number(li.dataset.step);
      li.classList.toggle("current", i === n);
      li.classList.toggle("done", i < n);
    });
  }

  function renderPreview(d) {
    activePreview = d;
    showPane("checkout");

    const total = money(d.total, d.currency);
    $("#pay-amount").textContent = total;
    $("#consent-amount").textContent = total;
    $("#foot-amount").textContent = total;
    $("#preview-lines").innerHTML = d.items
      .map((i) => lineRow(i, d.currency))
      .join("");

    // The server's deadline, not a locally invented one — payments is the side that
    // refuses the charge, so its clock is the only one worth showing.
    const expiry = $("#preview-expiry");
    if (d.expires_at) {
      const when = new Date(d.expires_at);
      expiry.textContent = isNaN(when)
        ? ""
        : `This total is held until ${when.toLocaleTimeString([], {
            hour: "2-digit",
            minute: "2-digit",
          })}.`;
    } else {
      expiry.textContent = "";
    }

    $("#consent").checked = false;
    $("#payment-status").classList.add("hidden");
    $("#details-problem").textContent = "";
    $("#card-problem").textContent = "";
    showStage(1);

    conversation.insertAdjacentHTML(
      "beforeend",
      `<div class="panel-pointer">🔒 Checkout is open in the panel on the right →</div>`,
    );
    bottom();
  }

  function renderReceipt(d) {
    showPane("checkout");
    stage = 4;
    STAGES.forEach((st) => $(st.id).classList.add("hidden"));
    $("#order").classList.add("hidden");
    $("#foot-total").classList.add("hidden");
    $("#checkout-step").textContent = "Step 3 of 3 · Paid";
    markStepper(4);

    const btn = $("#co-action");
    btn.className = "checkout";
    btn.disabled = false;
    btn.textContent = "Back to shopping";

    const r = $("#receipt");
    r.classList.remove("hidden");
    $("#receipt-title").textContent = `Order confirmed · ${d.transaction_id.slice(0, 8)}`;
    $("#receipt-lines").innerHTML = (d.items || [])
      .map((i) => lineRow(i, d.currency))
      .join("");
    const when = d.timestamp ? new Date(d.timestamp) : null;
    $("#receipt-summary").textContent = `${money(d.total, d.currency)} charged${
      when && !isNaN(when) ? ` on ${when.toLocaleString()}` : ""
    }.`;
    activePreview = null;
  }

  // --- card details (demo only; nothing here leaves the browser) -------------

  /* The card panel is a shopper-facing prop: the charge is authorized by the agent's
   * session token, not by these digits. It is still validated, because a checkout
   * that silently accepts "1234" teaches the wrong thing about the flow. */

  const digits = (v) => v.replace(/\D/g, "");

  function paintCard() {
    const num = digits($("#card-number").value);
    const name = $("#card-name").value.trim();
    const exp = $("#card-expiry").value.trim();
    const groups = (num.padEnd(16, "•").slice(0, 16).match(/.{1,4}/g) || []).map((g) =>
      g.replace(/\d/g, "•"),
    );
    const shown = num.length >= 4 ? num.slice(-4) : "";
    $("#visa-number").textContent =
      groups.slice(0, 3).join(" ") + " " + (shown || "••••");
    $("#visa-cardholder").textContent = name ? name.toUpperCase() : "CARDHOLDER NAME";
    $("#visa-expiry").textContent = exp || "MM/YY";
    $("#pay-method").textContent = `Visa •••• ${shown || "••••"}`;
    $("#consent-last4").textContent = shown || "••••";
  }

  function cardProblem() {
    const num = digits($("#card-number").value);
    const exp = $("#card-expiry").value.trim();
    const cvc = digits($("#card-cvc").value);
    if (num.length < 15) return "Enter a full card number.";
    if (!$("#card-name").value.trim()) return "Enter the name on the card.";
    if (!/^(0[1-9]|1[0-2])\/\d{2}$/.test(exp)) return "Expiry must look like MM/YY.";
    if (cvc.length < 3) return "Enter the 3-digit CVC.";
    return "";
  }

  function refreshConfirmGate() {
    if (stage !== 3) return;
    $("#co-action").disabled = !$("#consent").checked || !!cardProblem();
  }

  // --- turns -----------------------------------------------------------------

  function handlers(bubble) {
    let chip = null;
    const clearChip = () => {
      if (chip) {
        chip.remove();
        chip = null;
      }
    };
    return {
      session: (d) => (sessionId = d.session_id),
      token: (d) => {
        clearChip();
        bubble.append(d.text);
      },
      tool_start: (d) => {
        clearChip();
        chip = toolChip(d.summary || d.tool);
      },
      products: (d) => {
        clearChip();
        renderProducts(d);
      },
      comparison: (d) => {
        clearChip();
        renderComparison(d);
      },
      probe: (d) => {
        clearChip();
        renderProbe(d);
      },
      cart: (d) => {
        clearChip();
        renderCart(d);
      },
      notice: (d) => {
        clearChip();
        renderNotice(d);
      },
      preview: (d) => {
        clearChip();
        renderPreview(d);
      },
      receipt: (d) => {
        clearChip();
        renderReceipt(d);
      },
      error: (d) => {
        clearChip();
        renderError(d);
      },
      done: clearChip,
    };
  }

  async function send(message) {
    if (busy) return;
    setBusy(true);
    userBubble(message);
    const bubble = newAgentBubble();
    try {
      await stream(
        "/chat",
        { message, merchant_id: MERCHANT_ID, session_id: sessionId },
        handlers(bubble),
      );
    } catch (err) {
      renderError({ message: `Lost connection to the assistant: ${err.message}` });
    } finally {
      bubble.finish();
      setBusy(false);
      bottom();
    }
  }

  /* The confirm button. A separate POST carrying the preview_id — the only path to a
   * charge. The agent mints a single-use token here and only then can its charging
   * tool appear in the model's schema at all. */
  async function confirmPurchase() {
    if (!activePreview || busy) return;
    setBusy(true);
    const btn = $("#co-action");
    btn.disabled = true;
    btn.textContent = "Authorizing…";
    const status = $("#payment-status");
    status.classList.remove("hidden");
    status.className = "payment-status";
    status.innerHTML = '<span class="spinner"></span> Authorising with Visa Secure…';

    const bubble = newAgentBubble();
    try {
      await stream(
        "/chat/confirm",
        { session_id: sessionId, preview_id: activePreview.preview_id },
        {
          ...handlers(bubble),
          receipt: (d) => {
            status.innerHTML = "✓ Authorised · charge captured";
            status.classList.add("approved");
            renderReceipt(d);
          },
          error: (d) => {
            // A declined card leaves the basket intact so another way to pay can be
            // offered. The agent says so in the stream; this just re-opens the door.
            status.innerHTML = `✗ ${esc(d.message)}`;
            status.classList.add("declined");
            btn.disabled = true; // re-ticking consent is what re-arms it
            btn.textContent = "Authorize and pay";
            $("#consent").checked = false;
            renderError(d);
          },
        },
      );
    } catch (err) {
      status.innerHTML = `✗ ${esc(err.message)}`;
      status.classList.add("declined");
      btn.disabled = false;
      btn.textContent = "Authorize and pay";
    } finally {
      bubble.finish();
      setBusy(false);
      bottom();
    }
  }

  // --- wiring ----------------------------------------------------------------

  $("#order-lines").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-cart-action]");
    if (!btn || btn.disabled) return;
    cartAction(btn.dataset.cartAction, btn.dataset.id);
  });

  $("#composer").onsubmit = (e) => {
    e.preventDefault();
    const input = $("#chat-input");
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    send(text);
  };

  document.querySelectorAll(".chips button").forEach((b) => {
    b.onclick = () => send(b.textContent.trim());
  });

  // Checkout asks the agent for a total; the agent produces the preview itself when
  // it decides the shopper has decided, so this never invents one.
  $("#checkout").onclick = () => send("I'm ready to check out. Show me the total.");

  $("#back-to-products").onclick = () => showPane("products");

  // Back links inside the stages. Going back never discards what was typed.
  document.querySelectorAll(".co-back").forEach((b) => {
    b.onclick = () => showStage(Number(b.dataset.back));
  });

  function toCardStage() {
    const name = $("#customer-name").value.trim();
    const email = $("#customer-email").value.trim();
    const address = $("#customer-address").value.trim();
    const problem = $("#details-problem");
    if (!name || !email || !address) {
      problem.textContent = "Please add your name, email and delivery address.";
      return;
    }
    problem.textContent = "";
    if (!$("#card-name").value.trim()) $("#card-name").value = name;
    showStage(2);
    paintCard();
  }

  function toAuthorizeStage() {
    const problem = cardProblem();
    $("#card-problem").textContent = problem;
    if (problem) return;
    paintCard();
    showStage(3);
  }

  // One button, four meanings — see showStage().
  $("#co-action").onclick = () => {
    if (stage === 1) return toCardStage();
    if (stage === 2) return toAuthorizeStage();
    if (stage === 3) return confirmPurchase();
    showPane("products");
  };

  // Card number and expiry format as they are typed — a checkout that fights the
  // keyboard reads as broken even when it works.
  $("#card-number").oninput = (e) => {
    const d = digits(e.target.value).slice(0, 19);
    e.target.value = (d.match(/.{1,4}/g) || []).join(" ");
    paintCard();
  };
  $("#card-expiry").oninput = (e) => {
    const d = digits(e.target.value).slice(0, 4);
    e.target.value = d.length > 2 ? `${d.slice(0, 2)}/${d.slice(2)}` : d;
    paintCard();
  };
  $("#card-cvc").oninput = (e) => {
    e.target.value = digits(e.target.value).slice(0, 4);
  };
  $("#card-name").oninput = paintCard;

  // Typing clears a stale complaint about the field you are fixing.
  ["#card-number", "#card-expiry", "#card-cvc", "#card-name"].forEach((sel) => {
    $(sel).addEventListener("input", () => {
      $("#card-problem").textContent = "";
    });
  });

  $("#consent").onchange = refreshConfirmGate;

  bottom();
})();
