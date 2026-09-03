/* Runs backend/static/app.js against a stub DOM and a scripted server,
 * then prints what the page would be showing.
 *
 * This exists because the bug it guards is a RENDERING bug: the server
 * said FAILED and the page went on showing AWAITING HUMAN APPROVAL with
 * live Approve / Reject buttons. No assertion about a response body can
 * catch that -- only running the real app.js can.
 *
 * Nothing here fakes a verdict. Every response replayed below was
 * produced by the real FastAPI app in the calling test, so the page is
 * rendering server output, not a hand-written fixture.
 *
 * Usage:  node tests/frontend_dom_harness.mjs  < input.json
 *
 * Input (stdin):  { app_js, index_html, request: "...",
 *                   responses: {"POST /path": {status, body}},
 *                   steps: ["submit", "click:Approve"] }
 * Output (stdout): one JSON snapshot of the rendered page.
 */

import fs from "node:fs";
import vm from "node:vm";

const input = JSON.parse(fs.readFileSync(0, "utf-8"));

/* ------------------------------------------------------------------- dom */

/** Ids the real page starts with `hidden` on, so "is this card showing?"
 *  means the same here as in the browser. */
function initiallyHidden(html) {
  const ids = new Set();
  for (const tag of html.match(/<[^>]+>/g) || []) {
    const id = /\bid="([^"]+)"/.exec(tag);
    if (id && /\shidden[\s/>]/.test(tag)) { ids.add(id[1]); }
  }
  return ids;
}

const hiddenIds = initiallyHidden(fs.readFileSync(input.index_html, "utf-8"));

function makeNode(tag) {
  const node = {
    tagName: tag,
    className: "",
    textContent: "",
    hidden: false,
    disabled: false,
    type: "",
    value: "",
    options: [],
    childNodes: [],
    listeners: {},
    appendChild(child) { node.childNodes.push(child); return child; },
    removeChild(child) {
      node.childNodes = node.childNodes.filter((c) => c !== child);
      return child;
    },
    addEventListener(type, fn) {
      (node.listeners[type] = node.listeners[type] || []).push(fn);
    },
    focus() {},
    click() { (node.listeners.click || []).forEach((fn) => fn({})); }
  };
  Object.defineProperty(node, "children", { get: () => node.childNodes });
  Object.defineProperty(node, "firstChild", {
    get: () => node.childNodes[0] || null
  });
  return node;
}

const byId = new Map();

function $(id) {
  if (!byId.has(id)) {
    const node = makeNode("div");
    node.hidden = hiddenIds.has(id);
    byId.set(id, node);
  }
  return byId.get(id);
}

const document = {
  readyState: "complete",
  getElementById: $,
  createElement: makeNode,
  createTextNode(text) {
    const node = makeNode("#text");
    node.textContent = text;
    return node;
  },
  addEventListener() {}
};

/* ---------------------------------------------------------------- server */

const calls = [];

function reply(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: () => Promise.resolve(JSON.stringify(body))
  };
}

function fetchStub(path, init) {
  const key = ((init && init.method) || "GET") + " " + path;
  calls.push(key);
  const canned = input.responses[key];
  if (!canned) {
    return Promise.resolve(
      reply(404, { detail: { reason: "NOT_SCRIPTED", message: key } })
    );
  }
  return Promise.resolve(reply(canned.status, canned.body));
}

/* ------------------------------------------------------------------- run */

globalThis.document = document;
// Checkout is never loaded here. The page must cope with that and say so
// rather than pretending a payment is possible.
globalThis.window = {};
globalThis.fetch = fetchStub;

vm.runInThisContext(fs.readFileSync(input.app_js, "utf-8"), {
  filename: "app.js"
});

/** Let every promise the page started run to completion. */
async function settle() {
  for (let i = 0; i < 50; i++) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

function clickInActions(label) {
  const button = $("action-buttons").childNodes.find(
    (child) => child.textContent.indexOf(label) === 0
  );
  if (!button) {
    const shown = $("action-buttons").childNodes.map((c) => c.textContent);
    throw new Error(
      "no action button labelled " + JSON.stringify(label) + "; showing " +
      JSON.stringify(shown)
    );
  }
  button.click();
}

function snapshot() {
  return {
    calls,
    action_card_hidden: $("action-card").hidden,
    action_badge: {
      text: $("action-badge").textContent,
      class: $("action-badge").className
    },
    action_lead: $("action-lead").textContent,
    action_footnote: $("action-footnote").textContent,
    action_error: {
      text: $("action-error").textContent,
      hidden: $("action-error").hidden
    },
    buttons: $("action-buttons").childNodes.map((button) => ({
      label: button.textContent,
      disabled: button.disabled,
      class: button.className
    })),
    payment: {
      hidden: $("payment-status").hidden,
      badge: $("payment-badge").textContent,
      class: $("payment-badge").className,
      detail: $("payment-detail").textContent
    },
    transaction: {
      hidden: $("txn-detail").hidden,
      status: $("t-status").textContent,
      amount: $("t-amount").textContent,
      order: $("t-order").textContent,
      payment: $("t-payment").textContent
    },
    audit: {
      hidden: $("audit-card").hidden,
      count: $("audit-count").textContent,
      // The ev-label span of each timeline entry.
      events: $("timeline").childNodes.map((item) =>
        item.childNodes[0].childNodes[0].textContent
      )
    },
    verdict: $("verdict-badge").textContent
  };
}

async function main() {
  await settle();

  for (const step of input.steps) {
    if (step === "submit") {
      $("request-input").value = input.request;
      $("submit-btn").click();
    } else if (step.indexOf("click:") === 0) {
      clickInActions(step.slice("click:".length));
    } else {
      throw new Error("unknown step: " + step);
    }
    await settle();
  }

  process.stdout.write(JSON.stringify(snapshot(), null, 2));
}

main().catch((err) => {
  process.stderr.write(String(err && err.stack ? err.stack : err));
  process.exit(1);
});
