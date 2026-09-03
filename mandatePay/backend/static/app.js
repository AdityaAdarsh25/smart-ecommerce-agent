/* MandatePay demo front-end.
 *
 * Vanilla JS, no build step, no framework. The organising rule of this
 * file is that it OWNS NO FINANCIAL FACT. Every price, verdict, status
 * and total rendered below is read straight out of a server response and
 * re-read after every call; nothing here computes an amount, decides a
 * policy outcome, or writes a payment state.
 *
 * In particular:
 *   - create-order is called with a quote_id and nothing else.
 *   - "PAID" is rendered only when POST /payment/verify came back 200
 *     with verified === true AND transaction.status === "paid".
 *   - Razorpay's own success callback is treated as an unverified claim.
 */

(function () {
  "use strict";

  var API = "/app/v1";

  /* ------------------------------------------------------------------ state
   * Deliberately flat, and deliberately small: identifiers plus the last
   * server response for each stage. No derived financial values live here.
   */
  var state = {
    buyers: [],
    buyer: null,
    config: null,
    agent: null,
    quoteId: null,
    transactionId: null,
    transaction: null,
    approval: null,
    busy: false
  };

  /* ------------------------------------------------------------------- dom */

  function $(id) { return document.getElementById(id); }

  function show(el, visible) { el.hidden = !visible; }

  function clear(el) { while (el.firstChild) { el.removeChild(el.firstChild); } }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
  }

  function cell(parent, label, value, className) {
    var box = el("div", className || null);
    box.appendChild(el("span", null, label));
    box.appendChild(el("b", null, value));
    parent.appendChild(box);
    return box;
  }

  /* --------------------------------------------------------------- format */

  function money(value) {
    if (value === null || value === undefined || value === "") { return "—"; }
    var n = Number(value);
    if (isNaN(n)) { return String(value); }
    return "₹" + n.toLocaleString("en-IN", {
      minimumFractionDigits: 2, maximumFractionDigits: 2
    });
  }

  function paise(value) {
    return money(Number(value) / 100);
  }

  function when(iso) {
    if (!iso) { return "—"; }
    // Server timestamps are naive UTC; label them so they parse as UTC.
    var text = /(Z|[+-]\d{2}:?\d{2})$/.test(iso) ? iso : iso + "Z";
    var d = new Date(text);
    if (isNaN(d.getTime())) { return iso; }
    return d.toLocaleString();
  }

  function clock(iso) {
    if (!iso) { return ""; }
    var text = /(Z|[+-]\d{2}:?\d{2})$/.test(iso) ? iso : iso + "Z";
    var d = new Date(text);
    if (isNaN(d.getTime())) { return iso; }
    return d.toLocaleTimeString();
  }

  function shortId(value) {
    return value ? String(value).slice(0, 8) : "—";
  }

  /* ------------------------------------------------------------------ http */

  function apiCall(path, options) {
    var opts = options || {};
    var init = { method: opts.method || "GET", headers: { "Accept": "application/json" } };
    if (opts.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    return fetch(path, init).then(function (response) {
      return response.text().then(function (text) {
        var body = null;
        if (text) {
          try { body = JSON.parse(text); } catch (e) { body = { message: text }; }
        }
        if (!response.ok) {
          var err = new Error(messageFor(body, response.status));
          err.status = response.status;
          err.body = body;
          throw err;
        }
        return body;
      });
    });
  }

  /** Pull a readable sentence out of a FastAPI error body. */
  function messageFor(body, status) {
    if (!body) { return "Request failed (HTTP " + status + ")."; }
    var detail = body.detail !== undefined ? body.detail : body;
    if (typeof detail === "string") { return detail; }
    if (detail && typeof detail === "object") {
      if (detail.message) {
        return detail.reason ? detail.message + " [" + detail.reason + "]" : detail.message;
      }
      if (Array.isArray(detail) && detail.length && detail[0].msg) {
        return detail.map(function (d) { return d.msg; }).join("; ");
      }
    }
    return "Request failed (HTTP " + status + ").";
  }

  /* --------------------------------------------------------------- presets
   * Every one of these is written against the seeded catalogue in
   * seed_data.py and the seeded mandate (autonomous 3000, absolute 6000,
   * monthly cap 20000, merchants FreshMart + TechBazaar, categories
   * groceries + electronics). They stay editable after being clicked.
   *
   * The first four deliberately state a ceiling as well as an item. The
   * agent treats a request carrying NO budget, brand, category or stated
   * requirement as materially vague and answers it with a question rather
   * than spending against it -- see `is_materially_vague`. A preset meant
   * to demonstrate a policy verdict therefore has to be a request the
   * agent will actually price, and each ceiling below is set ABOVE the
   * seeded price so the product clears the buyer's own budget filter and
   * the verdict on show is the MANDATE's, not the buyer's.
   */
  var PRESETS = [
    { tag: "Allow",
      text: "Order the weekly grocery hamper from FreshMart, under 3000 rupees." },
    { tag: "Approval",
      text: "I need boAt wireless headphones for calls, under 5000 rupees." },
    { tag: "Block – limit",
      text: "Buy an ASUS gaming laptop for work, up to 8000 rupees." },
    { tag: "Block – category",
      text: "Get me a portable table for the balcony, under 1000 rupees." },
    { tag: "Clarify", text: "I need something nice for the house." },
    { tag: "Unsupported", text: "Buy a mixed fruit basket and a bluetooth speaker." }
  ];

  function renderPresets() {
    var host = $("presets");
    clear(host);
    PRESETS.forEach(function (preset) {
      var chip = el("button", "chip");
      chip.type = "button";
      chip.appendChild(el("b", null, preset.tag));
      chip.appendChild(document.createTextNode(preset.text));
      chip.addEventListener("click", function () {
        $("request-input").value = preset.text;
        $("request-input").focus();
      });
      host.appendChild(chip);
    });
  }

  /* ---------------------------------------------------------------- buyers */

  function loadBuyers() {
    return apiCall(API + "/buyers").then(function (buyers) {
      state.buyers = buyers || [];
      var select = $("buyer-select");
      clear(select);

      if (!state.buyers.length) {
        show($("buyer-empty"), true);
        select.disabled = true;
        $("submit-btn").disabled = true;
        return;
      }

      state.buyers.forEach(function (buyer, index) {
        var option = el("option", null,
          buyer.name + " — " + money(buyer.balance) + " available");
        option.value = String(index);
        select.appendChild(option);
      });
      select.addEventListener("change", function () {
        selectBuyer(state.buyers[Number(select.value)]);
      });
      selectBuyer(state.buyers[0]);
    }).catch(function (err) {
      show($("buyer-empty"), true);
      $("buyer-empty").textContent = "Could not load demo buyers: " + err.message;
      $("submit-btn").disabled = true;
    });
  }

  function selectBuyer(buyer) {
    state.buyer = buyer;
    resetFlow();
    var mandate = buyer && buyer.mandate;
    show($("buyer-detail"), !!mandate);
    if (!mandate) { return; }

    $("m-balance").textContent = money(buyer.balance);
    $("m-autonomous").textContent = money(mandate.autonomous_limit);
    $("m-absolute").textContent = money(mandate.absolute_transaction_limit);
    $("m-cap").textContent = money(mandate.monthly_cap);
    $("m-spent").textContent = money(mandate.monthly_spent);
    $("m-categories").textContent = mandate.allowed_categories.length
      ? mandate.allowed_categories.join(", ")
      : "unrestricted";
  }

  function refreshBuyers() {
    // Balance and monthly spend move only when a payment is verified, so
    // the panel is re-read from the server rather than adjusted here.
    if (!state.buyer) { return Promise.resolve(); }
    var id = state.buyer.buyer_id;
    return apiCall(API + "/buyers").then(function (buyers) {
      state.buyers = buyers || [];
      for (var i = 0; i < state.buyers.length; i++) {
        if (state.buyers[i].buyer_id === id) {
          var current = state.buyers[i];
          state.buyer = current;
          var mandate = current.mandate;
          $("m-balance").textContent = money(current.balance);
          if (mandate) { $("m-spent").textContent = money(mandate.monthly_spent); }
          var select = $("buyer-select");
          if (select.options[i]) {
            select.options[i].textContent =
              current.name + " — " + money(current.balance) + " available";
          }
          return;
        }
      }
    }).catch(function () { /* a stale balance is not worth an error banner */ });
  }

  /* ---------------------------------------------------------------- config */

  function loadConfig() {
    return apiCall(API + "/config").then(function (config) {
      state.config = config;
      if (!config.agent_configured) {
        showWarning(
          "The AI buyer agent is not configured on the server. Requests will be " +
          "refused with an explicit configuration error rather than pretending an " +
          "AI ran; the error names the missing setting."
        );
      }
    }).catch(function (err) {
      state.config = null;
      showWarning("Could not read public configuration: " + err.message);
    });
  }

  function showWarning(text) {
    var node = $("agent-warning");
    node.textContent = text;
    show(node, true);
  }

  /* ------------------------------------------------------------- evidence */

  function loadEvidence() {
    var host = $("evidence-body");
    return apiCall(API + "/evaluation/summary").then(function (summary) {
      clear(host);

      var top = el("div", "ev-top");
      var scenarios = el("div", "ev-stat");
      scenarios.appendChild(el("span", null, "Scenarios passed"));
      scenarios.appendChild(el("b", null, summary.passed + " / " + summary.scenarios));
      top.appendChild(scenarios);

      var bypass = el("div", "ev-stat");
      bypass.appendChild(el("span", null, "Unsafe financial bypass"));
      bypass.appendChild(el("b", null, summary.unsafe_financial_bypass_rate.toFixed(1) + "%"));
      top.appendChild(bypass);
      host.appendChild(top);

      var groups = el("div", "ev-groups");
      summary.groups.forEach(function (group) {
        groups.appendChild(el("span", "ev-group",
          group.name.replace(/_/g, " ") + " " + group.passed + "/" + group.total));
      });
      host.appendChild(groups);

      var foot = el("p", "ev-foot",
        summary.pass_rate.toFixed(1) + "% pass rate · " +
        summary.live_provider_calls + " live provider calls · recorded " +
        when(summary.generated_at));
      host.appendChild(foot);
    }).catch(function (err) {
      clear(host);
      host.appendChild(el("p", "empty",
        "Evaluation evidence unavailable: " + err.message));
    });
  }

  /* ----------------------------------------------------------- the request */

  function resetFlow() {
    state.agent = null;
    state.quoteId = null;
    state.transactionId = null;
    state.transaction = null;
    state.approval = null;

    ["ai-card", "clarify-card", "quote-card", "policy-card", "action-card",
     "audit-card"].forEach(function (id) { show($(id), false); });
    show($("pipeline-empty"), true);
    show($("request-error"), false);
    show($("action-error"), false);
    show($("payment-status"), false);
    show($("txn-detail"), false);
    clear($("action-buttons"));
    $("action-footnote").textContent = "";
  }

  function setBusy(busy) {
    state.busy = busy;
    $("submit-btn").disabled = busy || !state.buyers.length;
    $("submit-btn").textContent = busy ? "Evaluating…" : "Find & Evaluate";
  }

  function submitRequest() {
    if (state.busy) { return; }          // no double submission
    if (!state.buyer) { return; }

    var message = $("request-input").value.trim();
    if (!message) {
      $("request-error").textContent = "Describe what the buyer wants.";
      show($("request-error"), true);
      return;
    }

    resetFlow();
    setBusy(true);

    apiCall(API + "/agent/purchase", {
      method: "POST",
      body: { buyer_id: state.buyer.buyer_id, message: message }
    }).then(function (response) {
      state.agent = response;
      state.quoteId = response.quote ? response.quote.quote_id : null;
      renderAgent(response);
    }).catch(function (err) {
      $("request-error").textContent = err.message;
      show($("request-error"), true);
    }).then(function () {
      setBusy(false);
    });
  }

  /* -------------------------------------------------------- AI + selection */

  function renderAgent(response) {
    show($("pipeline-empty"), false);
    show($("ai-card"), true);

    var grid = $("intent-grid");
    clear(grid);
    var intent = response.intent;
    if (intent) {
      cell(grid, "Interpreted item", intent.item || "—");
      cell(grid, "Quantity", intent.quantity);
      cell(grid, "Stated budget",
        intent.max_budget ? money(intent.max_budget) : "none given");
      if (intent.required_brand) { cell(grid, "Required brand", intent.required_brand); }
      if (intent.required_category) {
        cell(grid, "Required category", intent.required_category);
      }
      if (intent.hard_constraints.length) {
        cell(grid, "Hard constraints", intent.hard_constraints.join(", "));
      }
      if (intent.preferred_brand) {
        cell(grid, "Preferred brand", intent.preferred_brand);
      }
      if (intent.soft_preferences.length) {
        cell(grid, "Preferences", intent.soft_preferences.join(", "));
      }
      if (intent.is_multi_item && intent.requested_items.length) {
        cell(grid, "Items requested", intent.requested_items.join(", "));
      }
    }

    var selection = response.selection;
    show($("selection-block"), !!selection);
    if (selection) {
      $("sel-name").textContent = selection.product.product_name;
      $("sel-meta").textContent = [
        selection.product.brand,
        selection.product.category || "uncategorised"
      ].join(" · ");
      $("sel-price").textContent = money(selection.unit_price) + " each";
      $("sel-rationale").textContent = selection.rationale || "";
      $("sel-eligible").textContent =
        selection.eligible_candidates +
        " catalogue row(s) passed the hard-constraint filter and were eligible " +
        "to be chosen from.";
    }

    var notes = $("agent-notes");
    clear(notes);
    show(notes, response.notes.length > 0);
    response.notes.forEach(function (note) { notes.appendChild(el("li", null, note)); });

    renderNonProposal(response);
    renderQuote(response.quote);
    renderPolicy(response.policy);
    renderNextAction(response);
  }

  /** CLARIFY, NO_MATCH and UNSUPPORTED all end the flow with no quote. */
  function renderNonProposal(response) {
    var action = response.selection_action;
    if (action === "PROPOSE") { show($("clarify-card"), false); return; }

    var titles = {
      CLARIFY: "Clarification required",
      NO_MATCH: "No matching product",
      UNSUPPORTED: "Request not supported"
    };
    $("clarify-title").textContent = titles[action] || "Nothing was quoted";
    $("clarify-badge").textContent = action;
    $("clarify-message").textContent =
      response.clarification_question || response.message;
    show($("clarify-card"), true);
  }

  /* ---------------------------------------------------------------- quote */

  function renderQuote(quote) {
    show($("quote-card"), !!quote);
    if (!quote) { return; }
    $("q-product").textContent = quote.product.product_name;
    $("q-quantity").textContent = quote.quantity;
    $("q-unit").textContent = money(quote.quoted_unit_price);
    $("q-total").textContent = money(quote.quoted_total);
    $("q-status").textContent = quote.quote_status;
    $("q-expiry").textContent = when(quote.expires_at);
    $("q-id").textContent = "quote " + quote.quote_id;
  }

  /* --------------------------------------------------------------- policy */

  // Keys are the wire values of PolicyDecision.
  var VERDICT_CLASS = {
    allow: "v-allow",
    require_approval: "v-amber",
    block: "v-block"
  };

  var VERDICT_LABEL = {
    allow: "ALLOW",
    require_approval: "REQUIRE APPROVAL",
    block: "BLOCK"
  };

  var VERDICT_SUMMARY = {
    allow: "Within the mandate. The agent may act alone.",
    require_approval: "Above the autonomous limit. A human must decide before " +
                      "any provider order can exist.",
    block: "A hard mandate rule failed. No payment path exists for this request."
  };

  function renderPolicy(policy) {
    show($("policy-card"), !!policy);
    if (!policy) { return; }

    var decision = policy.decision;
    var badge = $("verdict-badge");
    badge.className = "verdict-badge " + (VERDICT_CLASS[decision] || "");
    badge.textContent = VERDICT_LABEL[decision] || String(decision).toUpperCase();
    $("verdict-summary").textContent = VERDICT_SUMMARY[decision] || "";

    var findings = $("policy-findings");
    clear(findings);
    var list = el("ul", "findings");

    policy.hard_violations.forEach(function (text, index) {
      var item = el("li", "block");
      item.appendChild(el("code", null, policy.violation_codes[index] || "BLOCK"));
      item.appendChild(el("span", null, text));
      list.appendChild(item);
    });
    policy.approval_reasons.forEach(function (text, index) {
      var item = el("li", "approval");
      item.appendChild(el("code", null, policy.approval_codes[index] || "APPROVAL"));
      item.appendChild(el("span", null, text));
      list.appendChild(item);
    });
    if (list.childNodes.length) { findings.appendChild(list); }

    var rules = $("policy-rules");
    clear(rules);
    policy.evaluated_rules.forEach(function (rule) {
      rules.appendChild(el("li", null, rule));
    });
    show($("policy-rules-wrap"), policy.evaluated_rules.length > 0);

    renderPolicyContext(policy);
  }

  /** The mandate numbers the verdict was measured against. Display only --
   *  the verdict itself already arrived from the server. */
  function renderPolicyContext(policy) {
    var host = $("policy-context");
    clear(host);
    var buyer = state.buyer;
    var mandate = buyer && buyer.mandate;
    if (!mandate) { return; }

    var quote = state.agent && state.agent.quote;
    var total = quote ? Number(quote.quoted_total) : null;
    var codes = (policy.violation_codes || []).concat(policy.approval_codes || []);

    function flag(fragment) {
      return codes.some(function (code) {
        return String(code).toUpperCase().indexOf(fragment) !== -1;
      });
    }

    cell(host, "Transaction total", total === null ? "—" : money(total));
    cell(host, "Autonomous limit", money(mandate.autonomous_limit),
      flag("AUTONOMOUS") ? "near" : null);
    cell(host, "Absolute maximum", money(mandate.absolute_transaction_limit),
      flag("ABSOLUTE") ? "hit" : null);
    cell(host, "Monthly cap",
      money(mandate.monthly_spent) + " / " + money(mandate.monthly_cap),
      flag("MONTHLY") ? "hit" : null);
    cell(host, "Balance", money(buyer.balance), flag("BALANCE") ? "hit" : null);
    cell(host, "Merchant permitted", flag("MERCHANT") ? "no" : "yes",
      flag("MERCHANT") ? "hit" : null);
    cell(host, "Category permitted", flag("CATEGORY") ? "no" : "yes",
      flag("CATEGORY") ? "hit" : null);
  }

  /* -------------------------------------------------- next action / payment */

  function setActionBadge(text, className) {
    var badge = $("action-badge");
    badge.className = "badge " + (className || "");
    badge.textContent = text;
  }

  function actionButton(label, className, handler) {
    var button = el("button", "btn " + className, label);
    button.type = "button";
    button.addEventListener("click", function () {
      if (button.disabled) { return; }
      // Every action button disables itself on click: a purchase must not
      // be attempted twice because someone double-clicked.
      Array.prototype.forEach.call(
        $("action-buttons").children,
        function (sibling) { sibling.disabled = true; }
      );
      handler(button);
    });
    $("action-buttons").appendChild(button);
    return button;
  }

  function renderNextAction(response) {
    var next = response.next_action;
    show($("action-card"), next === "create_order" || next === "await_approval");
    clear($("action-buttons"));
    show($("action-error"), false);

    if (next === "create_order") {
      setActionBadge("POLICY: ALLOW", "badge-green");
      $("action-lead").textContent =
        "The mandate permits this purchase without a human. A Razorpay Test Mode " +
        "order can now be created from the quote.";
      $("action-footnote").textContent =
        "The browser sends only the quote id. The order amount is derived " +
        "server-side from the persisted quote.";
      actionButton("Proceed to Payment", "btn-primary", function (button) {
        createOrder(button);
      });
      return;
    }

    if (next === "await_approval") {
      setActionBadge("POLICY: APPROVAL REQUIRED", "badge-amber");
      $("action-lead").textContent =
        "Human approval required. No Razorpay order exists and none will be " +
        "created until a human approves and the mandate is re-validated.";
      $("action-footnote").textContent = "";
      actionButton("Open approval request", "btn-primary", function (button) {
        createOrder(button);
      });
      return;
    }

    if (next === "stop" && response.policy && response.policy.decision === "block") {
      // The BLOCK is already displayed, in red, by the policy panel. There
      // is deliberately no button of any kind here.
      show($("action-card"), true);
      setActionBadge("POLICY: BLOCK", "badge-red");
      $("action-lead").textContent =
        "Blocked by the deterministic policy engine. The AI selected this " +
        "product, but no order was created, Razorpay was never contacted, and " +
        "there is no action available that could pay for it.";
      $("action-footnote").textContent = "";
    }
  }

  /* --------------------------------------------------- order and approval */

  function actionError(message) {
    $("action-error").textContent = message;
    show($("action-error"), true);
  }

  /** The authoritative outcome the SERVER reported alongside a refusal.
   *
   *  A refusal carrying `transaction_status` describes a transaction the
   *  server has already written to -- the action WAS carried out, and it
   *  ended somewhere definite. Continuing to render the state this page
   *  was holding before the call would then contradict the server.
   *
   *  Null means the refusal reported no transaction state at all (a
   *  validation error, an unreachable server, a refusal that changed
   *  nothing), and the caller leaves the panel exactly as it was.
   */
  function refusalOutcome(err) {
    var detail = err && err.body ? err.body.detail : null;
    if (!detail || typeof detail !== "object") { return null; }
    if (!detail.transaction_status) { return null; }
    return {
      status: detail.transaction_status,
      transactionId: detail.transaction_id || null,
      message: detail.message || null
    };
  }

  /** Render the terminal state the server reported after an action that
   *  was carried out and then failed.
   *
   *  The pre-action controls go, because the state that offered them no
   *  longer exists -- leaving Approve and Reject on screen beside a
   *  provider error was the exact contradiction this replaces.
   *
   *  `humanDecision` is what the human had already decided when the
   *  failure happened, or null if no human was involved. It is reported
   *  separately from the payment state on purpose: a granted approval and
   *  a failed payment are two different facts, and collapsing them would
   *  misreport one of them.
   */
  function renderRefusedOutcome(outcome, message, humanDecision) {
    clear($("action-buttons"));
    applyTransactionStatus(outcome.status, outcome.transactionId);

    var payment = String(outcome.status).toUpperCase();
    setActionBadge(
      (humanDecision ? "HUMAN " + humanDecision + " · " : "") +
        "PAYMENT " + payment,
      "badge-red"
    );
    $("action-lead").textContent =
      (humanDecision
        ? "The human decision was recorded and stands. Execution afterwards "
        : "Execution ") +
      "did not complete, and the server reports this transaction as " +
      payment + ". There is no action available here.";
    $("action-footnote").textContent =
      (humanDecision ? "Human approval: " + humanDecision + " · " : "") +
      "Payment state: " + payment;
    // The sentence shown is the server's own, not one composed here.
    setPaymentStatus("NOT PAID", "s-fail", outcome.message || message);
    actionError(message);
  }

  function createOrder(button) {
    if (!state.quoteId) { return; }
    var original = button.textContent;
    button.textContent = "Working…";

    // quote_id and nothing else. There is no field on this request for an
    // amount, and the server would ignore one if there were.
    apiCall(API + "/create-order", {
      method: "POST",
      body: { quote_id: state.quoteId }
    }).then(function (order) {
      applyTransaction(order.transaction);
      state.approval = order.approval || null;
      renderPolicy(order.policy);

      if (order.transaction.status === "awaiting_approval") {
        renderApprovalControls(order);
      } else if (order.transaction.status === "blocked") {
        renderBlockedAfterOrder(order);
      } else if (order.razorpay) {
        renderOrderCreated(order);
      } else {
        actionError("The order was not created and no payment is possible.");
      }
      return refreshAudit();
    }).catch(function (err) {
      var outcome = refusalOutcome(err);
      if (outcome === null) {
        // Nothing was attempted, or nothing came of it. The offer stands.
        button.textContent = original;
        button.disabled = false;
        actionError(err.message);
        return refreshAudit();
      }
      renderRefusedOutcome(outcome, err.message, null);
      return refreshAudit();
    });
  }

  function renderBlockedAfterOrder(order) {
    setActionBadge("POLICY: BLOCK", "badge-red");
    clear($("action-buttons"));
    $("action-lead").textContent =
      "Re-evaluated at order time and blocked. Razorpay was never contacted.";
    setPaymentStatus("NOT PAID", "s-fail", order.policy.summary);
  }

  function renderApprovalControls(order) {
    clear($("action-buttons"));
    setActionBadge("AWAITING HUMAN APPROVAL", "badge-amber");
    $("action-lead").textContent =
      "Approval request opened. The transaction is AWAITING_APPROVAL, no " +
      "Razorpay order exists, and nothing has been charged.";
    $("action-footnote").textContent =
      "Approving answers only the spending-threshold question. Every hard " +
      "mandate rule is re-evaluated afterwards and can still stop the purchase.";
    setPaymentStatus("NOT PAID", "s-warn", "Waiting on a human decision.");

    actionButton("Approve", "btn-approve", function (button) {
      resolveApproval("approve", button);
    });
    actionButton("Reject", "btn-reject", function (button) {
      resolveApproval("reject", button);
    });
  }

  function resolveApproval(action, button) {
    var original = button.textContent;
    button.textContent = "Working…";

    apiCall(API + "/transactions/" + state.transactionId + "/" + action, {
      method: "POST",
      body: { reviewer: "demo-approver" }
    }).then(function (resolution) {
      applyTransaction(resolution.transaction);
      state.approval = resolution.approval;
      if (resolution.policy) { renderPolicy(resolution.policy); }
      clear($("action-buttons"));

      if (action === "reject") {
        setActionBadge("REJECTED BY HUMAN", "badge-red");
        $("action-lead").textContent =
          "Rejected. The transaction is cancelled, the quote is cancelled, and " +
          "the provider was never contacted.";
        setPaymentStatus("NOT PAID", "s-fail", "Rejected by the approver.");
      } else if (resolution.order_created && resolution.razorpay) {
        setActionBadge("APPROVED · REVALIDATED", "badge-green");
        $("action-lead").textContent =
          "Approved, and every hard mandate rule was re-evaluated and passed. " +
          "A Razorpay Test Mode order now exists.";
        renderOrderCreated(resolution);
      } else {
        setActionBadge("APPROVED · REVALIDATION FAILED", "badge-red");
        $("action-lead").textContent =
          "A human approved the spend, and deterministic re-validation refused " +
          "it anyway. No Razorpay order was created.";
        var reason = resolution.revalidation
          ? messageFor({ detail: resolution.revalidation }, 409)
          : (resolution.policy ? resolution.policy.summary : "Re-validation failed.");
        setPaymentStatus("NOT PAID", "s-fail", reason);
      }
      return refreshAudit();
    }).catch(function (err) {
      var outcome = refusalOutcome(err);
      if (outcome === null) {
        // The refusal changed nothing -- the decision is still the
        // human's to make, so the controls come back.
        button.textContent = original;
        Array.prototype.forEach.call(
          $("action-buttons").children,
          function (sibling) { sibling.disabled = false; }
        );
        actionError(err.message);
        return refreshAudit();
      }
      // The decision WAS recorded and execution failed after it. Say both.
      renderRefusedOutcome(
        outcome, err.message, action === "approve" ? "APPROVED" : "REJECTED"
      );
      return refreshAudit();
    });
  }

  /* -------------------------------------------------------------- checkout */

  function renderOrderCreated(order) {
    clear($("action-buttons"));
    setPaymentStatus("ORDER CREATED · NOT PAID", "s-pending",
      "A Razorpay order is an intent to collect, not a collection.");
    $("action-footnote").textContent =
      "Checkout's own success callback is not proof of payment. The result is " +
      "sent to the server, and only the server's verification can report PAID.";

    if (!state.config || !state.config.razorpay_key_id) {
      actionError(
        "Razorpay is not configured in this environment (RAZORPAY_KEY_ID is " +
        "not set), so Checkout cannot be opened. The order exists server-side " +
        "and the transaction is correctly NOT PAID."
      );
      return;
    }
    if (typeof window.Razorpay === "undefined") {
      actionError(
        "The Razorpay Checkout script could not be loaded (no network?). The " +
        "transaction remains NOT PAID."
      );
      return;
    }

    actionButton("Pay with Razorpay (Test Mode)", "btn-primary", function () {
      openCheckout(order);
    });
  }

  function openCheckout(order) {
    var rzp;
    var transactionId = state.transactionId;

    try {
      rzp = new window.Razorpay({
        key: state.config.razorpay_key_id,
        // Amount, currency and order id are all server-created. They are
        // echoed to Checkout, never chosen here.
        order_id: order.razorpay.razorpay_order_id,
        amount: order.razorpay.amount_in_paise,
        currency: order.razorpay.currency,
        name: "MandatePay",
        description: state.agent && state.agent.quote
          ? state.agent.quote.product.product_name
          : "Mandated purchase",
        handler: function (result) {
          verifyPayment(transactionId, result);
        },
        modal: {
          ondismiss: function () {
            // Dismissal is not a failure of ours, and it is certainly not
            // a payment. Report exactly what happened.
            setPaymentStatus("NOT PAID", "s-warn",
              "Checkout was dismissed before a payment completed.");
            clear($("action-buttons"));
            actionButton("Reopen Checkout", "btn-ghost", function () {
              openCheckout(order);
            });
            refreshAudit();
          }
        }
      });

      rzp.on("payment.failed", function (event) {
        var description = event && event.error && event.error.description
          ? event.error.description : "The provider reported a failed payment.";
        setPaymentStatus("NOT PAID", "s-fail", description);
        refreshAudit();
      });

      rzp.open();
      setPaymentStatus("AWAITING PAYMENT", "s-pending",
        "Checkout is open. Nothing is paid yet.");
    } catch (e) {
      actionError("Razorpay Checkout could not be opened: " + e.message);
    }
  }

  /** The only route to a PAID display in this file. */
  function verifyPayment(transactionId, result) {
    setPaymentStatus("VERIFYING", "s-pending",
      "Sending the provider's response to the server for signature verification.");

    apiCall(API + "/payment/verify", {
      method: "POST",
      body: {
        transaction_id: transactionId,
        razorpay_order_id: result.razorpay_order_id,
        razorpay_payment_id: result.razorpay_payment_id,
        razorpay_signature: result.razorpay_signature
      }
    }).then(function (verification) {
      applyTransaction(verification.transaction);

      // Both conditions, deliberately. A 200 alone is not the claim being
      // made here -- the transaction's own status is.
      if (verification.verified && verification.transaction.status === "paid") {
        setPaymentStatus("PAID", "s-paid",
          verification.already_verified
            ? "Already settled by this payment; the replay moved nothing."
            : "Signature verified server-side. Balance and stock moved in the " +
              "same database transaction as the status change.");
        clear($("action-buttons"));
      } else {
        setPaymentStatus("NOT PAID", "s-fail",
          "The server did not confirm this payment.");
      }
      return Promise.all([refreshAudit(), refreshBuyers()]);
    }).catch(function (err) {
      // A refused verification carries the transaction's real status, read
      // by the server after the refusal was applied. Show that rather than
      // the state the transaction was in before it failed -- and take it
      // only from the response, never inferring one here.
      var detail = err.body && err.body.detail;
      var status = detail && detail.transaction_status;
      applyTransactionStatus(status);
      setPaymentStatus(
        status === "failed" ? "PAYMENT FAILED · NOT PAID" : "NOT PAID",
        "s-fail",
        err.message
      );
      clear($("action-buttons"));
      return refreshAudit();
    });
  }

  function setPaymentStatus(label, className, detail) {
    var badge = $("payment-badge");
    badge.className = "status-badge " + className;
    badge.textContent = label;
    $("payment-detail").textContent = detail || "";
    show($("payment-status"), true);
  }

  /** Update the transaction panel from a status the SERVER reported.
   *  A falsy status leaves the panel alone: showing nothing is better
   *  than showing a status this file made up. */
  function applyTransactionStatus(status, transactionId) {
    if (transactionId) { state.transactionId = transactionId; }
    if (!status) { return; }
    if (state.transaction) {
      state.transaction.status = status;
    } else {
      // No transaction body ever reached this page for that id -- the
      // server reported only where the row ended up. The other cells stay
      // empty rather than invented, and the two provider ids read "none"
      // because the response carried none.
      $("t-amount").textContent = "—";
      $("t-order").textContent = "none";
      $("t-payment").textContent = "none";
    }
    $("t-status").textContent = String(status).toUpperCase();
    show($("txn-detail"), true);
  }

  function applyTransaction(transaction) {
    state.transaction = transaction;
    state.transactionId = transaction.id;
    show($("txn-detail"), true);
    $("t-status").textContent = String(transaction.status).toUpperCase();
    $("t-amount").textContent = money(transaction.amount);
    $("t-order").textContent = transaction.razorpay_order_id || "none";
    $("t-payment").textContent = transaction.razorpay_payment_id || "none";
  }

  /* ----------------------------------------------------------------- audit */

  var EVENT_LABEL = {
    AGENT_REQUEST_INTERPRETED: "AI interpreted the request",
    AGENT_CLARIFICATION_REQUIRED: "AI asked for clarification",
    AGENT_NO_MATCH: "No catalogue match",
    AGENT_REQUEST_UNSUPPORTED: "Request refused as unsupported",
    AGENT_PRODUCT_SELECTED: "AI selected a product",
    AGENT_REQUEST_FAILED: "AI request failed",
    QUOTE_CREATED: "Quote created",
    QUOTE_EXPIRED: "Quote expired",
    QUOTE_REVALIDATION_FAILED: "Quote re-validation failed",
    PRICE_DRIFT_DETECTED: "Price drift detected",
    POLICY_ALLOWED: "Policy verdict: ALLOW",
    POLICY_APPROVAL_REQUIRED: "Policy verdict: REQUIRE APPROVAL",
    POLICY_BLOCKED: "Policy verdict: BLOCK",
    APPROVAL_CREATED: "Human approval requested",
    APPROVAL_APPROVED: "Human approved",
    APPROVAL_REJECTED: "Human rejected",
    APPROVAL_REVALIDATION_FAILED: "Re-validation failed after approval",
    ORDER_CREATION_ATTEMPTED: "Razorpay order attempted",
    ORDER_CREATED: "Razorpay order created",
    ORDER_CREATION_FAILED: "Razorpay order failed",
    PAYMENT_VERIFICATION_ATTEMPTED: "Payment verification attempted",
    PAYMENT_VERIFIED: "Payment verified server-side",
    PAYMENT_VERIFICATION_FAILED: "Payment verification failed",
    TRANSACTION_PAID: "Transaction PAID",
    TRANSACTION_BLOCKED: "Transaction blocked",
    TRANSACTION_CANCELLED: "Transaction cancelled",
    TRANSACTION_FAILED: "Transaction failed"
  };

  var EVENT_TONE = {
    AGENT_REQUEST_INTERPRETED: "ai",
    AGENT_PRODUCT_SELECTED: "ai",
    AGENT_CLARIFICATION_REQUIRED: "warn",
    AGENT_NO_MATCH: "warn",
    AGENT_REQUEST_UNSUPPORTED: "warn",
    AGENT_REQUEST_FAILED: "bad",
    POLICY_ALLOWED: "ok",
    POLICY_APPROVAL_REQUIRED: "warn",
    POLICY_BLOCKED: "bad",
    APPROVAL_CREATED: "warn",
    APPROVAL_APPROVED: "ok",
    APPROVAL_REJECTED: "bad",
    APPROVAL_REVALIDATION_FAILED: "bad",
    ORDER_CREATED: "ok",
    ORDER_CREATION_FAILED: "bad",
    PAYMENT_VERIFIED: "ok",
    PAYMENT_VERIFICATION_FAILED: "bad",
    TRANSACTION_PAID: "ok",
    TRANSACTION_BLOCKED: "bad",
    TRANSACTION_CANCELLED: "bad",
    TRANSACTION_FAILED: "bad",
    QUOTE_EXPIRED: "bad",
    QUOTE_REVALIDATION_FAILED: "bad",
    PRICE_DRIFT_DETECTED: "warn"
  };

  /* A short human line per event, built from the recorded details. Kept
     to the fields a reviewer actually reads; the rest stays behind the
     expandable "details" block. */
  function eventSummary(event) {
    var d = event.details || {};
    var bits = [];
    if (d.item) { bits.push("item: " + d.item); }
    if (d.product_name) { bits.push(d.product_name); }
    if (d.quantity !== undefined && d.quantity !== null) { bits.push("qty " + d.quantity); }
    if (d.amount !== undefined && d.amount !== null) { bits.push(money(d.amount)); }
    if (d.quoted_total !== undefined && d.quoted_total !== null) {
      bits.push("total " + money(d.quoted_total));
    }
    if (d.amount_in_paise !== undefined && d.amount_in_paise !== null) {
      bits.push(paise(d.amount_in_paise));
    }
    if (d.decision) { bits.push("decision: " + String(d.decision).toUpperCase()); }
    if (Array.isArray(d.violation_codes) && d.violation_codes.length) {
      bits.push(d.violation_codes.join(", "));
    }
    if (Array.isArray(d.approval_codes) && d.approval_codes.length) {
      bits.push(d.approval_codes.join(", "));
    }
    if (d.summary && typeof d.summary === "string") { bits.push(d.summary); }
    if (d.reason && typeof d.reason === "string") { bits.push(d.reason); }
    if (d.reviewer) { bits.push("reviewer: " + d.reviewer); }
    if (d.status_to) { bits.push("→ " + d.status_to); }
    if (d.provider_called === false) { bits.push("provider not contacted"); }
    if (d.balance_or_stock_mutated === false) { bits.push("nothing mutated"); }
    return bits.join(" · ");
  }

  function refreshAudit() {
    if (!state.transactionId) { return Promise.resolve(); }
    return apiCall(API + "/transactions/" + state.transactionId + "/audit")
      .then(function (audit) {
        show($("audit-card"), true);
        $("audit-count").textContent =
          audit.event_count + " events · transaction " + shortId(audit.transaction_id);

        var timeline = $("timeline");
        clear(timeline);
        audit.events.forEach(function (event) {
          var item = el("li", EVENT_TONE[event.event_type] || "");

          var head = el("div", "ev-head");
          head.appendChild(el("span", "ev-label",
            EVENT_LABEL[event.event_type] || event.event_type));
          head.appendChild(el("span", "ev-time", clock(event.timestamp)));
          head.appendChild(el("span", "ev-type", "#" + event.sequence));
          item.appendChild(head);

          var summary = eventSummary(event);
          if (summary) { item.appendChild(el("p", "ev-detail", summary)); }

          if (event.details && Object.keys(event.details).length) {
            var details = el("details");
            details.appendChild(el("summary", null, event.event_type));
            details.appendChild(el("pre", "ev-json",
              JSON.stringify(event.details, null, 2)));
            item.appendChild(details);
          }
          timeline.appendChild(item);
        });
      })
      .catch(function () { /* the audit read is observational; never block on it */ });
  }

  /* ------------------------------------------------------------------ boot */

  function boot() {
    renderPresets();
    $("submit-btn").addEventListener("click", submitRequest);
    $("reset-btn").addEventListener("click", function () {
      $("request-input").value = "";
      resetFlow();
    });
    $("request-input").addEventListener("keydown", function (event) {
      if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
        submitRequest();
      }
    });

    loadBuyers();
    loadConfig();
    loadEvidence();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
