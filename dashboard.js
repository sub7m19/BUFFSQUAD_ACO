// Sample data, shown when the page runs without the BUFFSQUAD server (static
// hosting). With the server, boot() swaps in the member's own data.
const user = { name: "Member" };

const RETAILERS = ["Target", "Pokemon Center"];

const minute = 60 * 1000;
const hour = 60 * minute;
const now = Date.now();

// stage: confirmed -> shipped -> delivered, or canceled (never charged).
const checkouts = [
  { product: "Prismatic Evolutions ETB", retailer: "Target", order: "TGT-48213", date: "Oct 17", qty: 2, price: 54.99, fee: 15, paid: false, stage: "confirmed" },
  { product: "Pokemon 151 Booster Bundle", retailer: "Pokemon Center", order: "PC-99104", date: "Oct 14", qty: 1, price: 29.99, fee: 8, paid: false, stage: "shipped" },
  { product: "Surging Sparks Booster Box", retailer: "Target", order: "TGT-20577", date: "Oct 9", qty: 1, price: 159.99, fee: 25, paid: true, stage: "delivered" },
  { product: "Paldean Fates ETB", retailer: "Pokemon Center", order: "PC-77310", date: "Oct 2", qty: 2, price: 49.99, fee: 12, paid: true, stage: "delivered" },
  { product: "Destined Rivals Booster Bundle", retailer: "Target", order: "TGT-11902", date: "Sep 28", qty: 1, price: 26.94, fee: 8, paid: false, stage: "canceled" },
];

// Attempts that didn't go through, for the hit rate.
let declined = 2;

const feed = [
  { kind: "hit", text: "Checked out Prismatic Evolutions ETB", meta: "Target · Qty 2", at: now - 6 * minute },
  { kind: "miss", text: "Missed Pokemon 151 Booster Bundle", meta: "Pokemon Center · Sold out in 40s", at: now - 52 * minute },
  { kind: "ship", text: "Pokemon 151 Booster Bundle shipped", meta: "Pokemon Center · #PC-99104", at: now - 5 * hour },
  { kind: "new", text: "Surging Sparks Booster Box added", meta: "Target · Drop opens soon", at: now - 9 * hour },
];

const STAGES = {
  confirmed: { label: "Confirmed", icon: "check", tone: "cyan" },
  shipped: { label: "Shipped", icon: "truck", tone: "violet" },
  delivered: { label: "Delivered", icon: "box", tone: "green" },
  canceled: { label: "Canceled", icon: "x", tone: "red" },
};

// Member settings, saved in this browser until there's an API to save them to.
const STORE_KEY = "buffsquad.dashboard.v1";

const defaults = {
  // Retailer accounts we check out on, several per retailer. Only emails live
  // here; passwords go through saveAccountPassword() (see Retailer accounts).
  accounts: { Target: [{ id: "a1", email: "member@example.com" }], "Pokemon Center": [] },
  // Checkout profiles: who and where we ship to, plus which card is on file.
  // Only the card brand, last 4 and expiry live here, never the full number.
  profiles: [
    {
      id: "p1", label: "Main", sites: ["Target"], email: "member@example.com",
      name: "Member Name", line1: "123 Main St", line2: "", city: "Denver", state: "CO", zip: "80202", phone: "",
      cardName: "Member Name", brand: "Visa", last4: "4242", exp: "08/28", active: true,
    },
  ],
  discord: false,
  notify: { checkout: true, reminders: true, receipts: false },
  // How we read the member's inbox: "aycd" or "gmail", plus the Gmail address.
  // The AYCD key or Gmail app password goes through saveInboxSecret() (see Inbox).
  inbox: { method: null, email: "" },
};

// A signed-in member with nothing saved yet starts empty, not with the samples.
const blank = {
  accounts: Object.fromEntries(RETAILERS.map((r) => [r, []])),
  profiles: [],
  discord: true,
  notify: { ...defaults.notify },
  inbox: { ...defaults.inbox },
};

function load() {
  try {
    return normalize(JSON.parse(localStorage.getItem(STORE_KEY)));
  } catch {
    return structuredClone(defaults);
  }
}

// Fills in anything an older save is missing.
function normalize(saved, base = defaults) {
  try {
    if (!saved) return structuredClone(base);
    // Older saves kept a single shipping address; turn it into a profile.
    if (!saved.profiles) {
      saved.profiles = saved.shipping
        ? [{ id: "p1", label: "Main", sites: [...RETAILERS], email: "", line2: "", phone: "", brand: "", last4: "", exp: "", active: true, ...saved.shipping }]
        : [];
    }
    delete saved.shipping;
    // Older saves kept one email (or null) per retailer.
    const accounts = Object.fromEntries(RETAILERS.map((r) => {
      const v = saved.accounts && saved.accounts[r];
      return [r, Array.isArray(v) ? v : v ? [{ id: "a" + r.length, email: v }] : []];
    }));
    return { ...base, ...saved, accounts, notify: { ...base.notify, ...saved.notify }, inbox: { ...base.inbox, ...saved.inbox } };
  } catch {
    return structuredClone(base);
  }
}

const state = load();

// Server mode

// True once /api/me answered: settings save to the server instead of this browser.
let remote = false;
// Which secrets the server already holds for this member (ids only, never values).
const saved = { cards: new Set(), accounts: new Set(), inbox: false };

// Writes go out one at a time and in order, so a card saved right after a
// settings change never lands before it.
let writes = Promise.resolve();

function send(method, url, body) {
  const run = () => fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then((res) => {
    if (res.status === 401) location.href = "/auth/discord/login?next=/dashboard";
    if (!res.ok) throw new Error(res.status);
    return res.json();
  });
  const done = writes.then(run);
  writes = done.catch(() => {});
  return done;
}

function applySaved(s) {
  saved.cards = new Set(s.cards);
  saved.accounts = new Set(s.accounts);
  saved.inbox = s.inbox;
}

// Card numbers, passwords and inbox keys go to the server one at a time, where
// they're stored encrypted. They're never part of the settings document.
function storeSecret(kind, ref, value) {
  if (!remote) return;
  send("POST", "/api/me/secret", { kind, ref, value })
    .then(() => {
      const set = kind === "card" ? saved.cards : saved.accounts;
      if (kind === "inbox") saved.inbox = value !== null;
      else if (value === null) set.delete(ref);
      else set.add(ref);
    })
    .catch(() => notify("Couldn't save that to the server. Try again."));
}

const hasPassword = (id) => accountVault.has(id) || saved.accounts.has(id);

let saveTimer;

function save() {
  if (remote) {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      send("PUT", "/api/me/settings", state)
        .then((res) => applySaved(res.saved))
        .catch(() => notify("Couldn't save your changes. Check your connection and try again."));
    }, 400);
    return;
  }
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(state));
  } catch {
    // Storage blocked; changes still apply for this visit.
  }
}

const filters = { checkouts: "all", stage: "all", search: "", profileSite: "all", profileSearch: "", profileTab: "profiles", inboxMethod: state.inbox.method || "aycd" };

const money = (n) => "$" + n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const escape = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const initials = (name) => (name.match(/\b[a-z0-9]/gi) || ["?"]).join("").slice(0, 2).toUpperCase();

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

const icon = (name) => `<svg class="icon"><use href="#i-${name}"/></svg>`;

const byId = (id) => document.getElementById(id);

const accountsFor = (r) => state.accounts[r] || [];
const hasAccount = (r) => accountsFor(r).length > 0;

const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const charged = (c) => c.stage !== "canceled";

// Animate a number from its current value to the new one.
function countTo(el, value, format) {
  const from = Number(el.dataset.value || 0);
  el.dataset.value = value;
  if (reduceMotion || from === value) {
    el.textContent = format(value);
    return;
  }
  const start = performance.now();
  const step = (t0) => {
    const t = Math.min((t0 - start) / 700, 1);
    el.textContent = format(from + (value - from) * (1 - Math.pow(1 - t, 3)));
    if (t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function totals() {
  const billable = checkouts.filter(charged);
  return {
    hits: billable.length,
    due: billable.filter((c) => !c.paid).reduce((sum, c) => sum + c.fee, 0),
    dueCount: billable.filter((c) => !c.paid).length,
    paid: billable.filter((c) => c.paid).reduce((sum, c) => sum + c.fee, 0),
    value: billable.reduce((sum, c) => sum + c.price, 0),
  };
}

// Home

function renderHome() {
  const t = totals();
  const whole = (n) => Math.round(n).toString();

  byId("user-name").textContent = user.name;
  const onTheWay = checkouts.filter((c) => c.stage === "confirmed" || c.stage === "shipped").length;
  byId("page-sub").textContent = onTheWay || t.dueCount
    ? `${plural(onTheWay, "order")} on the way · ${money(t.due)} due`
    : "You're all caught up";

  // Hit rate: one block per checkout attempt, hits lit up, misses dimmed.
  const attempts = t.hits + declined;
  const rate = attempts ? Math.round((t.hits / attempts) * 100) : 0;
  countTo(byId("hit-rate"), rate, (n) => Math.round(n) + "%");
  byId("hit-tile").title = `${t.hits} checked out, ${declined} missed`;
  byId("hit-ok").textContent = t.hits;
  byId("hit-bad").textContent = declined;
  // Past 20 attempts, each block stands for a share of them instead.
  const blocks = Math.min(attempts, 20);
  const lit = attempts ? Math.round((t.hits / attempts) * blocks) : 0;
  byId("hit-segs").innerHTML = Array.from({ length: blocks }, (_, i) =>
    `<i class="${i < lit ? "hit" : "miss"}" style="--i:${i}"></i>`).join("");

  countTo(byId("stat-checkouts"), t.hits, whole);
  countTo(byId("stat-value"), t.value, money);
  countTo(byId("stat-due"), t.due, money);

  // Notes under the stats and the Pay button (only on pages that have them).
  const note = (id, text) => { if (byId(id)) byId(id).textContent = text; };
  note("note-checkouts", !t.hits ? "None yet" : onTheWay ? `${onTheWay} on the way` : "All delivered");
  note("note-value", `Across ${plural(t.hits, "order")}`);
  note("note-due", t.dueCount ? `${plural(t.dueCount, "fee")} unpaid` : "All paid up");
  if (byId("pay-now")) byId("pay-now").hidden = !t.due;

  // Recent orders: newest first, with where each one is and its fee.
  // The prototype page shows the retailer above the item, plus a quantity column.
  const proto = document.body.dataset.layout === "proto";
  byId("recent-orders").innerHTML = checkouts.slice(0, 8).map((c) => `
    <li>
      ${proto ? `
      <div class="checkout-info">
        <span class="via-row"><b class="via" data-retailer="${escape(c.retailer)}">${escape(c.retailer)}</b>${escape(c.date)}</span>
        <strong>${escape(c.product)}</strong>
      </div>
      <span class="qty"><small>Qty</small>${c.qty}</span>` : `
      <span class="avatar">${escape(initials(c.retailer))}</span>
      <div class="checkout-info">
        <strong>${escape(c.product)}</strong>
        <span>${escape(c.retailer)} · ${escape(c.date)}</span>
      </div>`}
      <span class="stage ${STAGES[c.stage].tone}">${icon(STAGES[c.stage].icon)}${STAGES[c.stage].label}</span>
      <div class="checkout-fee">
        <strong>${charged(c) ? money(c.fee) : "—"}</strong>
        ${feeStatus(c)}
      </div>
    </li>`).join("");
}

// Checkouts

const feeStatus = (c) => !charged(c)
  ? `<span class="status paused">No fee</span>`
  : `<span class="status ${c.paid ? "paid" : "due"}">${c.paid ? "Paid" : "Due"}</span>`;

function renderCheckouts() {
  byId("stage-chips").innerHTML = [["all", { label: "All orders", icon: "bag", tone: "cyan" }], ...Object.entries(STAGES)]
    .map(([key, s]) => {
      const n = key === "all" ? checkouts.length : checkouts.filter((c) => c.stage === key).length;
      return `
        <button class="chip ${s.tone}" type="button" data-stage="${key}" aria-pressed="${filters.stage === key}">
          <span class="tile-icon ${s.tone}">${icon(s.icon)}</span>
          <strong>${n}</strong>
          <small>${s.label}</small>
        </button>`;
    }).join("");

  const q = filters.search.trim().toLowerCase();
  const list = checkouts.filter((c) =>
    (filters.stage === "all" || c.stage === filters.stage) &&
    (filters.checkouts === "all" || (charged(c) && (filters.checkouts === "paid") === c.paid)) &&
    (!q || `${c.product} ${c.order} ${c.retailer}`.toLowerCase().includes(q)));

  byId("checkouts-list").innerHTML = list.map((c) => `
    <li>
      <span class="avatar">${escape(initials(c.retailer))}</span>
      <div class="checkout-info">
        <strong>${escape(c.product)}</strong>
        <span>${escape(c.retailer)}${c.order ? ` · #${escape(c.order)}` : ""} · ${escape(c.date)}</span>
      </div>
      <span class="stage ${STAGES[c.stage].tone}">${icon(STAGES[c.stage].icon)}${STAGES[c.stage].label}</span>
      <div class="checkout-fee">
        <strong>${charged(c) ? money(c.fee) : "—"}</strong>
        ${feeStatus(c)}
      </div>
    </li>`).join("");
  byId("checkouts-empty").hidden = list.length > 0;
}

// Billing

function renderBilling() {
  const t = totals();
  countTo(byId("billing-due"), t.due, money);
  const total = t.paid + t.due;
  const pct = total ? Math.round((t.paid / total) * 100) : 100;
  byId("billing-meter").style.width = pct + "%";
  byId("billing-meter-wrap").setAttribute("aria-label", `${pct}% of fees paid`);
  byId("billing-paid").textContent = money(t.paid);
  byId("billing-due-small").textContent = money(t.due);

  const billable = checkouts.filter(charged).sort((a, b) => a.paid - b.paid);
  byId("invoice-count").textContent = t.dueCount ? `${t.dueCount} unpaid` : "All settled";
  byId("invoice-list").innerHTML = billable.map((c) => `
    <li>
      <span class="avatar">${escape(initials(c.retailer))}</span>
      <div class="checkout-info">
        <strong>${escape(c.product)}</strong>
        <span>${c.order ? `#${escape(c.order)} · ` : ""}${escape(c.date)} · ${money(c.price)} retail</span>
      </div>
      <div class="checkout-fee">
        <strong>${money(c.fee)}</strong>
        ${feeStatus(c)}
      </div>
    </li>`).join("");
}

// Profiles

const NOTIFY_LABELS = {
  checkout: ["Checkout alerts", "Discord DM the moment we hit"],
  reminders: ["Drop reminders", "Heads up 15 minutes before a drop"],
  receipts: ["Email receipts", "A copy of each fee invoice"],
};

const CARD_SHORT = { Visa: "Visa", Mastercard: "MC", Amex: "Amex", Discover: "Disc" };

const hasProfileFor = (retailer) => state.profiles.some((p) => p.active && p.sites.includes(retailer));

function profileRow(p) {
  return `
    <li class="profile-row ${p.active ? "" : "off"}">
      <span class="avatar round">${escape(initials(p.label || p.name || "?"))}</span>
      <div class="profile-main">
        <strong>${escape(p.label)}</strong>
        <span>${p.email ? escape(p.email) : "No account email"}</span>
      </div>
      <div class="profile-sites">
        ${p.sites.map((s) => `<span class="retailer" title="${escape(s)}"><i></i>${escape(s)}</span>`).join("")}
      </div>
      <div class="profile-addr">
        <strong>${escape(p.line1)}${p.line2 ? ", " + escape(p.line2) : ""}</strong>
        <span>${p.name ? escape(p.name) + " · " : ""}${escape(p.city)}, ${escape(p.state)} ${escape(p.zip)}</span>
      </div>
      <div class="profile-card">
        ${p.last4
          ? `<span class="card-brand" title="${escape(p.brand)}">${escape(CARD_SHORT[p.brand] || "Card")}</span><b>•••• ${escape(p.last4)}</b><small>${escape(p.exp)}</small>`
          : `<span class="status due">No card</span>`}
      </div>
      <button class="switch" type="button" role="switch" aria-checked="${p.active}" aria-label="${escape(p.label)} active" data-profile-active="${p.id}">
        <span class="switch-track"><span class="switch-thumb"></span></span>
      </button>
      <div class="row-actions">
        <button class="icon-button" type="button" data-edit-profile="${p.id}" aria-label="Edit ${escape(p.label)}" title="Edit">${icon("edit")}</button>
        <button class="icon-button danger" type="button" data-delete-profile="${p.id}" aria-label="Delete ${escape(p.label)}" title="Delete">${icon("trash")}</button>
      </div>
    </li>`;
}

// The profiles the table shows right now: site filter plus search.
function visibleProfiles() {
  const q = filters.profileSearch.trim().toLowerCase();
  return state.profiles.filter((p) =>
    (filters.profileSite === "all" || p.sites.includes(filters.profileSite)) &&
    (!q || [p.label, p.email, p.name, p.line1, p.city, p.zip].join(" ").toLowerCase().includes(q)));
}

function groupLabel(n) {
  if (filters.profileSearch.trim()) return `Delete ${n} shown`;
  return filters.profileSite === "all" ? "Delete all" : `Delete ${filters.profileSite}`;
}

function renderProfiles() {
  // The prototype moves Notifications to its own page, so it has no alerts tab.
  ["profiles", "accounts", "alerts"].forEach((tab) => {
    if (byId("pane-" + tab)) byId("pane-" + tab).hidden = filters.profileTab !== tab;
  });
  byId("tab-count-profiles").textContent = state.profiles.length;
  byId("tab-count-accounts").textContent = RETAILERS.reduce((n, r) => n + accountsFor(r).length, 0);

  // Site pills with counts, like the retailer filter on Drops.
  byId("profile-sites").innerHTML = ["all", ...RETAILERS].map((s) => {
    const n = s === "all" ? state.profiles.length : state.profiles.filter((p) => p.sites.includes(s)).length;
    return `<button type="button" aria-pressed="${filters.profileSite === s}" data-value="${escape(s)}">${s === "all" ? "All" : escape(s)} <b>${n}</b></button>`;
  }).join("");

  const list = visibleProfiles();

  byId("profile-list").innerHTML = list.map(profileRow).join("");
  const groupBtn = byId("delete-group");
  groupBtn.hidden = list.length === 0;
  groupBtn.classList.remove("confirm");
  byId("delete-group-label").textContent = groupLabel(list.length);
  byId("profile-table").hidden = list.length === 0;
  byId("profiles-empty").hidden = list.length > 0;
  byId("profiles-empty-text").textContent = state.profiles.length
    ? "Nothing matches that search."
    : "Add a profile so we know where to ship and which card to use.";

  // Retailer accounts
  byId("accounts-grid").innerHTML = RETAILERS.map((r) => {
    const list = accountsFor(r);
    const profiles = state.profiles.filter((p) => p.active && p.sites.includes(r)).length;
    return `
      <article class="account ${list.length ? "linked" : ""}">
        <div class="account-head">
          <span class="avatar" data-retailer="${r}">${initials(r)}</span>
          <div>
            <strong>${r}</strong>
            <span class="status ${list.length ? "paid" : "due"}">${list.length ? plural(list.length, "account") : "No accounts"}</span>
          </div>
          <button class="icon-button" type="button" data-link="${r}" aria-label="Add ${r} account" title="Add account">${icon("plus")}</button>
        </div>
        ${list.length
          ? `<ul class="account-list">${list.map((a) => `
              <li>
                <div>
                  <strong>${escape(a.email)}</strong>
                  <small>${hasPassword(a.id) ? "Password ••••••••" : remote ? "No password saved" : "No password this visit"}</small>
                </div>
                <button class="icon-button" type="button" data-edit-account="${a.id}" aria-label="Edit ${escape(a.email)}" title="Edit">${icon("edit")}</button>
                <button class="icon-button danger" type="button" data-unlink="${a.id}" aria-label="Remove ${escape(a.email)}" title="Remove">${icon("trash")}</button>
              </li>`).join("")}</ul>`
          : `<p class="account-detail">Add one so we can check out on ${r}.</p>`}
        <span class="account-sub">${plural(profiles, "active profile")}</span>
      </article>`;
  }).join("");

  byId("notify-list").innerHTML = Object.entries(NOTIFY_LABELS).map(([key, [label, hint]]) => `
    <button class="switch row" type="button" role="switch" aria-checked="${state.notify[key]}" data-notify="${key}">
      <span><strong>${label}</strong><small>${hint}</small></span>
      <span class="switch-track"><span class="switch-thumb"></span></span>
    </button>`).join("");
}

// Shell: sidebar badge and user chip. Only Billing gets a badge (unpaid fees);
// unfinished setup isn't flagged, so new members don't see "unread" bubbles.

function renderShell() {
  const t = totals();
  byId("nav-billing").textContent = t.dueCount || "";
  byId("nav-billing").hidden = !t.dueCount;

  byId("user-chip-name").textContent = user.name;
  byId("user-avatar").textContent = initials(user.name);
}

function render() {
  renderShell();
  renderHome();
  renderCheckouts();
  renderBilling();
  renderProfiles();
  renderInbox();
  requestAnimationFrame(updateScrollFades);
}

// Lists that scroll inside their panel have no visible scrollbar, so their
// bottom edge fades out while there's more below.
const SCROLLERS = ".home-grid .panel > ul, .home-stack, .drop-grid, .checkout-list, #profile-table, .account-grid";

function updateScrollFades() {
  document.querySelectorAll(SCROLLERS).forEach((el) => {
    el.classList.toggle("has-more", el.scrollHeight - el.scrollTop - el.clientHeight > 2);
  });
}

document.addEventListener("scroll", (e) => {
  if (e.target instanceof Element && e.target.matches(SCROLLERS)) updateScrollFades();
}, true);
window.addEventListener("resize", updateScrollFades);

const toast = byId("toast");
let toastTimer;
let undoAction = null;

// Pass `undo` to show an Undo button that runs it.
function notify(message, undo) {
  undoAction = undo || null;
  toast.innerHTML = escape(message) + (undo ? ` <button class="toast-undo" type="button">Undo</button>` : "");
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), undo ? 5000 : 2600);
}

toast.addEventListener("click", (e) => {
  if (!e.target.closest(".toast-undo") || !undoAction) return;
  undoAction();
  undoAction = null;
  toast.classList.remove("show");
});

// Adds what the member just did to the top of the live feed.
function log(text, meta) {
  feed.unshift({ kind: "you", text, meta, at: Date.now() });
  feed.length = Math.min(feed.length, 8);
}

function update(message, undo) {
  save();
  render();
  if (message) notify(message, undo);
}

// Routing: each sidebar item is its own page.

const ROUTES = ["home", "checkouts", "billing", "profiles", "inbox", "notifications"];

function route() {
  // Old #accounts links land on the Retailer accounts tab of Profiles.
  if (location.hash === "#accounts") {
    filters.profileTab = "accounts";
    syncSegment("profileTab");
    renderProfiles();
    history.replaceState(null, "", "#profiles");
  }
  const hash = location.hash.slice(1);
  // Pages without a view for this route (the OG dashboard has no Inbox) fall back to Home.
  const name = ROUTES.includes(hash) && document.querySelector(`.view[data-view="${hash}"]`) ? hash : "home";
  document.querySelectorAll(".view").forEach((v) => {
    const show = v.dataset.view === name;
    v.hidden = !show;
    if (show) {
      v.classList.remove("enter");
      void v.offsetWidth;
      v.classList.add("enter");
    }
  });
  document.querySelectorAll(".nav a").forEach((a) => {
    const on = a.dataset.route === name;
    a.classList.toggle("active", on);
    if (on) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  window.scrollTo(0, 0);
  requestAnimationFrame(updateScrollFades);
}

window.addEventListener("hashchange", route);

// Modals

const accountModal = byId("account-modal");
const accountForm = byId("account-form");
const profileModal = byId("profile-modal");
const profileForm = byId("profile-form");
let editingAccount = null;
let editingProfile = null;

const findAccount = (id) => {
  for (const r of RETAILERS) {
    const a = accountsFor(r).find((x) => x.id === id);
    if (a) return { retailer: r, account: a };
  }
  return null;
};

// Retailer radio tiles, shared by the add-account and import/export dialogs.
const retailerPicks = (checked, disabled) => RETAILERS.map((r) => `
  <label class="site-pick">
    <input type="radio" name="retailer" value="${r}" ${r === checked ? "checked" : ""} ${disabled ? "disabled" : ""} required>
    <span class="avatar">${initials(r)}</span>
    <span><strong>${r}</strong><small>${plural(accountsFor(r).length, "account")}</small></span>
    <span class="site-check">${icon("check")}</span>
  </label>`).join("");

// Opens the account form for a new account on `retailer`, or to edit one.
function openAccountModal(retailer, id) {
  const found = id ? findAccount(id) : null;
  editingAccount = found ? found.account.id : null;
  accountForm.reset();
  byId("account-modal-title").textContent = found ? "Edit account" : "Add account";
  byId("account-submit").textContent = found ? "Save changes" : "Add account";
  byId("account-picks").innerHTML = retailerPicks(found ? found.retailer : retailer || RETAILERS[0], Boolean(found));
  accountForm.elements.email.value = found ? found.account.email : "";
  const keep = Boolean(found && hasPassword(found.account.id));
  accountForm.elements.password.required = !keep;
  accountForm.elements.password.placeholder = keep ? "Leave blank to keep it" : "";
  accountModal.showModal();
}

// Opens the profile form, either for an existing profile or a new one
// with `site` already ticked.
function openProfileModal(id, site) {
  const p = state.profiles.find((x) => x.id === id);
  editingProfile = p ? p.id : null;
  profileForm.reset();
  byId("profile-modal-title").textContent = p ? `Edit ${p.label}` : "New profile";
  byId("profile-submit").textContent = p ? "Save changes" : "Add profile";
  byId("profile-sites-pick").innerHTML = RETAILERS.map((r) => `
    <label class="site-pick">
      <input type="checkbox" name="sites" value="${r}" ${(p ? p.sites.includes(r) : r === site) ? "checked" : ""}>
      <span class="avatar">${initials(r)}</span>
      <span><strong>${r}</strong><small>${hasAccount(r) ? plural(accountsFor(r).length, "account") : "Add an account after"}</small></span>
      <span class="site-check">${icon("check")}</span>
    </label>`).join("");
  if (p) {
    Object.entries(p).forEach(([k, v]) => {
      const field = profileForm.elements[k];
      if (field && typeof v === "string") field.value = v;
    });
  } else {
    profileForm.elements.label.value = `Profile ${state.profiles.length + 1}`;
    profileForm.elements.email.value = hasAccount(site) ? accountsFor(site)[0].email : "";
  }
  // Editing a profile with a card: the number can stay blank to keep it.
  const keep = Boolean(p && p.last4);
  byId("card-keep").hidden = !keep;
  byId("card-keep").textContent = keep ? `Leave blank to keep ${p.brand || "card"} •••• ${p.last4}.` : "";
  profileForm.elements.cardNumber.required = !keep;
  profileForm.elements.cvv.required = !keep;
  profileForm.elements.cvv.maxLength = 4;
  profileForm.elements.cardNumber.setCustomValidity("");
  profileForm.elements.exp.setCustomValidity("");
  showCardBrand("");
  byId("sites-error").hidden = true;
  profileModal.showModal();
}

document.querySelectorAll("[data-close-modal]").forEach((btn) => btn.addEventListener("click", () => btn.closest("dialog").close()));

document.querySelectorAll(".modal").forEach((modal) => modal.addEventListener("click", (e) => {
  if (e.target === modal) modal.close();
}));

accountForm.addEventListener("submit", (e) => {
  const email = accountForm.elements.email.value.trim();
  const password = accountForm.elements.password.value;
  if (editingAccount) {
    findAccount(editingAccount).account.email = email;
    if (password) saveAccountPassword(editingAccount, password);
    update(`${email} saved`);
    return;
  }
  const retailer = accountForm.elements.retailer.value;
  if (accountsFor(retailer).some((a) => a.email.toLowerCase() === email.toLowerCase())) {
    e.preventDefault();
    accountForm.elements.email.setCustomValidity(`That email is already on ${retailer}.`);
    accountForm.elements.email.reportValidity();
    return;
  }
  const id = "a" + Date.now();
  state.accounts[retailer] = [...accountsFor(retailer), { id, email }];
  saveAccountPassword(id, password);
  log(`Added a ${retailer} account`, email);
  update(`${retailer} account added`);
});

accountForm.elements.email.addEventListener("input", (e) => e.target.setCustomValidity(""));

// Retailer accounts
//
// Like card numbers, passwords never go into localStorage. saveAccountPassword()
// is the one place they leave the page: point it at the backend (over HTTPS)
// once the database exists. Until then they're kept in memory for this visit.
const accountVault = new Map();

function saveAccountPassword(id, password) {
  accountVault.set(id, password);
  storeSecret("account", id, password);
}

// Inbox
//
// Members connect one inbox, by AYCD Inbox key (preferred), Gmail or iCloud IMAP, so we
// can read verification codes and order emails. Same rule as passwords: the key
// or app password stays in memory, and saveInboxSecret() is where it would go to
// the backend. Gmail is imap.gmail.com and iCloud imap.mail.me.com, both on port 993.
//
// All are checked by main.py before they're saved: Gmail and iCloud by signing in over IMAP,
// AYCD with its verify-key call (which also fails while the Inbox app is closed).
let inboxSecret = null;

async function checkInbox(kind, body) {
  try {
    const res = await fetch(`/api/inbox/${kind}/check`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return await res.json();
  } catch {
    // Static hosting has no server to run the check.
    return { ok: false, message: "Checking your inbox needs the BUFFSQUAD server, which isn't running on this site." };
  }
}

function saveInboxSecret(secret) {
  inboxSecret = secret;
  storeSecret("inbox", "inbox", secret);
}

const INBOX_NAMES = { aycd: "AYCD Inbox", gmail: "Gmail", icloud: "iCloud Mail" };

function renderInbox() {
  if (!byId("inbox-status")) return;
  const { method } = state.inbox;
  const ready = Boolean(method && (inboxSecret || saved.inbox));
  const name = INBOX_NAMES[method] || "Inbox";
  const status = byId("inbox-status");
  status.className = "status " + (ready ? "paid" : method ? "paused" : "due");
  status.textContent = !method ? "Not connected" : ready ? `${name} connected` : `Re-enter your ${name} details this visit`;

  syncSegment("inboxMethod");
  byId("inbox-aycd-form").hidden = filters.inboxMethod !== "aycd";
  byId("inbox-gmail-form").hidden = filters.inboxMethod !== "gmail";
  byId("inbox-icloud-form").hidden = filters.inboxMethod !== "icloud";
  // Show the saved address in the form for the connected method.
  const form = byId(`inbox-${method}-form`);
  if (state.inbox.email && form && form.elements.email && !form.elements.email.value) form.elements.email.value = state.inbox.email;
  document.querySelectorAll("[data-inbox-clear]").forEach((b) => (b.hidden = !method));
}

if (byId("inbox-status")) {
  const aycdForm = byId("inbox-aycd-form");
  const gmailForm = byId("inbox-gmail-form");
  const icloudForm = byId("inbox-icloud-form");
  const allForms = [aycdForm, gmailForm, icloudForm];

  // Runs the server check with the button showing "Checking…", and shows the
  // error under the form if it fails. Returns whether it passed.
  async function runCheck(form, kind, body) {
    const error = form.querySelector(".field-error");
    const submit = form.querySelector('[type="submit"]');
    const label = submit.textContent;
    error.hidden = true;
    submit.disabled = true;
    submit.textContent = "Checking…";
    form.setAttribute("aria-busy", "true");
    const result = await checkInbox(kind, body);
    submit.disabled = false;
    submit.textContent = label;
    form.removeAttribute("aria-busy");
    if (!result.ok) {
      error.textContent = result.message;
      error.hidden = false;
    }
    return result.ok;
  }

  allForms.forEach((form) => form.addEventListener("input", () => (form.querySelector(".field-error").hidden = true)));

  aycdForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const key = aycdForm.elements.key.value.trim();
    if (!(await runCheck(aycdForm, "aycd", { key }))) return;
    saveInboxSecret(key);
    state.inbox = { method: "aycd", email: "" };
    allForms.forEach((form) => form.reset());
    update("AYCD Inbox connected");
  });

  gmailForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = gmailForm.elements;
    const email = f.email.value.trim();
    // Google shows app passwords in groups of four; the spaces aren't part of it.
    const password = f.password.value.replace(/\s/g, "");
    if (!(await runCheck(gmailForm, "gmail", { email, password }))) return;
    saveInboxSecret(password);
    state.inbox = { method: "gmail", email };
    f.password.value = "";
    aycdForm.reset();
    icloudForm.reset();
    update("Gmail connected");
  });

  icloudForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = icloudForm.elements;
    const email = f.email.value.trim();
    // Apple shows app-specific passwords as xxxx-xxxx-xxxx-xxxx; the dashes are part of it.
    const password = f.password.value.trim();
    if (!(await runCheck(icloudForm, "icloud", { email, password }))) return;
    saveInboxSecret(password);
    state.inbox = { method: "icloud", email };
    f.password.value = "";
    aycdForm.reset();
    gmailForm.reset();
    update("iCloud Mail connected");
  });

  document.querySelectorAll("[data-inbox-clear]").forEach((btn) => btn.addEventListener("click", () => {
    saveInboxSecret(null);
    state.inbox = structuredClone(defaults.inbox);
    allForms.forEach((form) => form.reset());
    document.querySelectorAll(".inbox-form .field-error").forEach((el) => (el.hidden = true));
    update("Inbox removed");
  }));
}

// Reads a .txt of `email:password` lines. Only the first colon splits, so
// passwords may contain colons. Returns the good rows and the bad line numbers.
function parseAccountsTxt(text) {
  const rows = [], bad = [];
  text.replace(/^﻿/, "").split(/\r?\n/).forEach((line, i) => {
    const raw = line.trim();
    if (!raw) return;
    const at = raw.indexOf(":");
    const email = at > 0 ? raw.slice(0, at).trim() : "";
    const password = at > 0 ? raw.slice(at + 1) : "";
    if (/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) && password) rows.push({ email, password });
    else bad.push(i + 1);
  });
  return { rows, bad };
}

const accountsIoModal = byId("accounts-io-modal");
const accountsIoForm = byId("accounts-io-form");
const accountsFile = byId("accounts-file");
let accountsIoMode = "import";

function openAccountsIo(mode) {
  accountsIoMode = mode;
  accountsIoForm.reset();
  const importing = mode === "import";
  byId("accounts-io-title").textContent = importing ? "Import accounts" : "Export accounts";
  byId("accounts-io-submit").innerHTML = importing ? `${icon("upload")}Choose .txt file` : `${icon("download")}Download .txt`;
  byId("accounts-io-picks").innerHTML = retailerPicks(RETAILERS[0]);
  byId("accounts-io-format").hidden = !importing;
  updateAccountsIoNote();
  accountsIoModal.showModal();
}

function updateAccountsIoNote() {
  const note = byId("accounts-io-note");
  if (accountsIoMode === "import") {
    note.textContent = "Accounts already on this retailer get their password updated instead of being added twice.";
    return;
  }
  const r = accountsIoForm.elements.retailer.value;
  const ready = accountsFor(r).filter((a) => accountVault.has(a.id)).length;
  note.textContent = `${ready} of ${plural(accountsFor(r).length, "account")} ${ready === 1 ? "has" : "have"} a password this visit and will be exported. The file holds passwords in plain text, so delete it once you're done.`;
}

accountsIoForm.addEventListener("change", updateAccountsIoNote);

accountsIoForm.addEventListener("submit", (e) => {
  const r = accountsIoForm.elements.retailer.value;
  if (accountsIoMode === "import") {
    // Keep the dialog open until a file is picked.
    e.preventDefault();
    accountsFile.dataset.retailer = r;
    accountsFile.click();
    return;
  }
  const lines = accountsFor(r).filter((a) => accountVault.has(a.id)).map((a) => `${a.email}:${accountVault.get(a.id)}`);
  if (!lines.length) {
    e.preventDefault();
    byId("accounts-io-note").textContent = `No ${r} passwords to export this visit. Add or import the accounts first.`;
    return;
  }
  download(`buffsquad-${r.toLowerCase().replace(/\s+/g, "-")}-accounts.txt`, lines.join("\r\n") + "\r\n", "text/plain");
  notify(`Exported ${plural(lines.length, r + " account")}`);
});

accountsFile.addEventListener("change", async () => {
  const file = accountsFile.files[0];
  const r = accountsFile.dataset.retailer;
  accountsFile.value = "";
  if (!file) return;
  accountsIoModal.close();
  const { rows, bad } = parseAccountsTxt(await file.text());
  if (!rows.length) {
    notify(`No accounts found. Each line must be email:password${bad.length ? ` (check line ${bad[0]})` : ""}.`);
    return;
  }
  const before = accountsFor(r).map((a) => ({ ...a }));
  const oldPasswords = new Map(before.map((a) => [a.id, accountVault.get(a.id)]));
  const list = [...before];
  let added = 0, updated = 0;
  rows.forEach(({ email, password }, i) => {
    const existing = list.find((a) => a.email.toLowerCase() === email.toLowerCase());
    if (existing) {
      updated++;
      saveAccountPassword(existing.id, password);
    } else {
      const id = `a${Date.now()}${i}`;
      list.push({ id, email });
      saveAccountPassword(id, password);
      added++;
    }
  });
  state.accounts[r] = list;
  log(`Imported ${plural(added, r + " account")}`, file.name);
  const parts = [`Added ${added}`, updated && `updated ${updated}`, bad.length && `skipped ${bad.length} bad line${bad.length === 1 ? "" : "s"}`].filter(Boolean);
  update(`${r}: ${parts.join(", ")}`, () => {
    state.accounts[r] = before;
    oldPasswords.forEach((pw, id) => (pw === undefined ? accountVault.delete(id) : accountVault.set(id, pw)));
    update("Import undone");
  });
});

byId("accounts-import").addEventListener("click", () => openAccountsIo("import"));
byId("accounts-export").addEventListener("click", () => openAccountsIo("export"));

profileForm.addEventListener("submit", (e) => {
  const form = new FormData(profileForm);
  const sites = form.getAll("sites");
  if (!sites.length) {
    e.preventDefault();
    byId("sites-error").hidden = false;
    return;
  }
  const number = form.get("cardNumber").replace(/\D/g, "");
  const cvv = form.get("cvv").trim();
  const data = Object.fromEntries([...form]
    .filter(([k]) => !["sites", "cardNumber", "cvv"].includes(k))
    .map(([k, v]) => [k, v.trim()]));
  data.state = data.state.toUpperCase();
  data.sites = sites;
  if (number) {
    data.brand = cardBrand(number) || "Card";
    data.last4 = number.slice(-4);
  }
  const id = editingProfile || "p" + Date.now();
  if (number) saveCard(id, { number, cvv, exp: data.exp, name: data.cardName });
  if (editingProfile) {
    Object.assign(state.profiles.find((p) => p.id === editingProfile), data);
    update(`${data.label} saved`);
  } else {
    state.profiles.push({ id, active: true, ...data });
    log(`Added profile ${data.label}`, sites.join(" · "));
    update(`${data.label} added`);
  }
});

// Card details
//
// The full number and CVV never go into localStorage. saveCard() is the one
// place they leave the form: swap its body for a POST to the card database
// (over HTTPS) once the backend exists. Until then they live in memory for
// this visit only.
const cardVault = new Map();

function saveCard(profileId, card) {
  cardVault.set(profileId, card);
  storeSecret("card", profileId, card);
}

const CARD_PATTERNS = [
  ["Amex", /^3[47]/],
  ["Visa", /^4/],
  ["Mastercard", /^(5[1-5]|2[2-7])/],
  ["Discover", /^(6011|65|64[4-9])/],
];

const cardBrand = (digits) => (CARD_PATTERNS.find(([, re]) => re.test(digits)) || [""])[0];

// Luhn checksum, catches most typos in a card number.
function luhnOk(digits) {
  let sum = 0;
  [...digits].reverse().forEach((ch, i) => {
    let n = Number(ch);
    if (i % 2) n = n * 2 > 9 ? n * 2 - 9 : n * 2;
    sum += n;
  });
  return sum % 10 === 0;
}

function showCardBrand(digits) {
  const brand = cardBrand(digits);
  const badge = byId("card-brand-live");
  badge.hidden = !brand;
  badge.textContent = CARD_SHORT[brand] || "";
}

function expired(exp) {
  const [mm, yy] = exp.split("/").map(Number);
  const now = new Date();
  return 2000 + yy < now.getFullYear() || (2000 + yy === now.getFullYear() && mm < now.getMonth() + 1);
}

// Card fields: group the number (4-6-5 for Amex, 4-4-4-4 otherwise), keep
// CVV to digits and auto-insert the slash in MM/YY.
profileForm.elements.cardNumber.addEventListener("input", (e) => {
  const brand = cardBrand(e.target.value.replace(/\D/g, ""));
  const digits = e.target.value.replace(/\D/g, "").slice(0, brand === "Amex" ? 15 : 19);
  e.target.value = brand === "Amex"
    ? [digits.slice(0, 4), digits.slice(4, 10), digits.slice(10)].filter(Boolean).join(" ")
    : digits.replace(/(\d{4})(?=\d)/g, "$1 ");
  showCardBrand(digits);
  const ok = !digits || (digits.length >= (brand === "Amex" ? 15 : 13) && luhnOk(digits));
  e.target.setCustomValidity(ok ? "" : "Check the card number.");
  profileForm.elements.cvv.maxLength = brand === "Amex" ? 4 : 3;
  profileForm.elements.cvv.required = Boolean(digits) || e.target.required;
});

profileForm.elements.cvv.addEventListener("input", (e) => {
  e.target.value = e.target.value.replace(/\D/g, "").slice(0, e.target.maxLength);
});

profileForm.elements.exp.addEventListener("input", (e) => {
  const d = e.target.value.replace(/\D/g, "").slice(0, 4);
  e.target.value = d.length > 2 ? `${d.slice(0, 2)}/${d.slice(2)}` : d;
  e.target.setCustomValidity(d.length === 4 && expired(e.target.value) ? "This card has expired." : "");
});

// Import / export in the AYCD profile format
//
// JSON follows AYCD's spec exactly (an array of profiles). AYCD doesn't
// define CSV columns, so the CSV uses the JSON paths as headers, one profile
// per row. Our own `sites` field rides along as an extra key, which the spec
// allows. Retailers here are US-only, so US states are long form in files
// ("California") and two letters in the dashboard ("CA").

const US_STATES = {
  AL: "Alabama", AK: "Alaska", AS: "American Samoa", AZ: "Arizona", AR: "Arkansas", CA: "California",
  CO: "Colorado", CT: "Connecticut", DE: "Delaware", DC: "District of Columbia", FL: "Florida",
  GA: "Georgia", GU: "Guam", HI: "Hawaii", ID: "Idaho", IL: "Illinois", IN: "Indiana", IA: "Iowa",
  KS: "Kansas", KY: "Kentucky", LA: "Louisiana", ME: "Maine", MD: "Maryland", MA: "Massachusetts",
  MI: "Michigan", MN: "Minnesota", MS: "Mississippi", MO: "Missouri", MT: "Montana", NE: "Nebraska",
  NV: "Nevada", NH: "New Hampshire", NJ: "New Jersey", NM: "New Mexico", NY: "New York",
  NC: "North Carolina", ND: "North Dakota", MP: "Northern Mariana Islands", OH: "Ohio", OK: "Oklahoma",
  OR: "Oregon", PA: "Pennsylvania", PR: "Puerto Rico", RI: "Rhode Island", SC: "South Carolina",
  SD: "South Dakota", TN: "Tennessee", TX: "Texas", UM: "U.S. Minor Outlying Islands", UT: "Utah",
  VT: "Vermont", VI: "U.S. Virgin Islands", VA: "Virginia", WA: "Washington", WV: "West Virginia",
  WI: "Wisconsin", WY: "Wyoming",
};

const stateCode = (s) => {
  const v = String(s || "").trim();
  if (US_STATES[v.toUpperCase()]) return v.toUpperCase();
  return Object.keys(US_STATES).find((k) => US_STATES[k].toLowerCase() === v.toLowerCase()) || "";
};

const AYCD_CARD_TYPES = { Visa: "Visa", Mastercard: "MasterCard", Amex: "AmericanExpress", Discover: "Discover" };
const fromAycdCard = (t) => {
  const v = String(t || "").toLowerCase().replace(/[^a-z]/g, "");
  if (v.startsWith("amex") || v === "americanexpress") return "Amex";
  return Object.keys(AYCD_CARD_TYPES).find((k) => k.toLowerCase() === v) || "";
};

const CSV_COLUMNS = [
  "name", "size", "profileGroup",
  ...["shippingAddress", "billingAddress"].flatMap((a) =>
    ["name", "email", "phone", "line1", "line2", "line3", "postCode", "city", "country", "state"].map((k) => `${a}.${k}`)),
  "paymentDetails.nameOnCard", "paymentDetails.cardType", "paymentDetails.cardNumber",
  "paymentDetails.cardExpMonth", "paymentDetails.cardExpYear", "paymentDetails.cardCvv",
  "sameBillingAndShippingAddress", "onlyCheckoutOnce", "matchNameOnCardAndAddress", "sites",
];

// Friendlier CSV headers some sheets use, mapped to the JSON paths.
const CSV_ALIASES = {
  profilename: "name", email: "shippingAddress.email", phone: "shippingAddress.phone",
  address1: "shippingAddress.line1", address2: "shippingAddress.line2", zip: "shippingAddress.postCode",
  zipcode: "shippingAddress.postCode", city: "shippingAddress.city", state: "shippingAddress.state",
  country: "shippingAddress.country", nameoncard: "paymentDetails.nameOnCard", cardholder: "paymentDetails.nameOnCard",
  cardtype: "paymentDetails.cardType", cardnumber: "paymentDetails.cardNumber", expmonth: "paymentDetails.cardExpMonth",
  expyear: "paymentDetails.cardExpYear", cvv: "paymentDetails.cardCvv", cvc: "paymentDetails.cardCvv",
};
const headerKey = (h) => h.toLowerCase().replace(/[^a-z0-9]/g, "");
const HEADER_MAP = Object.fromEntries([
  ...CSV_COLUMNS.map((c) => [headerKey(c), c]),
  ...Object.entries(CSV_ALIASES),
]);

function toAycd(p, withCards) {
  const card = withCards ? cardVault.get(p.id) : null;
  const extra = p.aycd || {};
  const shipping = {
    name: p.name, email: p.email, phone: (p.phone || "").replace(/\D/g, ""),
    line1: p.line1, line2: p.line2 || "", line3: extra.line3 || "",
    postCode: p.zip, city: p.city, country: "United States", state: US_STATES[p.state] || p.state,
  };
  const [mm = "", yy = ""] = (p.exp || "").split("/");
  return {
    name: p.label,
    size: extra.size || "",
    profileGroup: extra.profileGroup || "",
    billingAddress: extra.billing || { ...shipping },
    shippingAddress: shipping,
    paymentDetails: {
      nameOnCard: p.cardName || p.name,
      cardType: AYCD_CARD_TYPES[p.brand] || p.brand || "",
      cardNumber: card ? card.number : "",
      cardExpMonth: mm,
      cardExpYear: yy ? "20" + yy : "",
      cardCvv: card ? card.cvv : "",
    },
    sameBillingAndShippingAddress: !extra.billing,
    onlyCheckoutOnce: Boolean(extra.onlyCheckoutOnce),
    matchNameOnCardAndAddress: (p.cardName || p.name) === p.name,
    sites: p.sites,
  };
}

// Turns one AYCD profile into ours. Returns { profile, card } or { error }.
function fromAycd(a, takenLabels) {
  const ship = a.shippingAddress || a.billingAddress || {};
  const bill = a.billingAddress || ship;
  const pay = a.paymentDetails || {};
  const country = String(ship.country || "United States").trim();
  if (!/^(united states|us|usa)$/i.test(country)) return { error: `${a.name || "A profile"} ships outside the US` };
  const state = stateCode(ship.state);
  if (!ship.line1 || !ship.city || !state || !ship.postCode) return { error: `${a.name || "A profile"} is missing part of its address` };

  let label = String(a.name || ship.name || "Imported").trim();
  for (let n = 2; takenLabels.has(label.toLowerCase()); n++) label = `${String(a.name || "Imported").trim()} (${n})`;
  takenLabels.add(label.toLowerCase());

  const number = String(pay.cardNumber || "").replace(/\D/g, "");
  const month = String(pay.cardExpMonth || "").replace(/\D/g, "").padStart(2, "0");
  const year = String(pay.cardExpYear || "").replace(/\D/g, "").slice(-2);
  const sites = (Array.isArray(a.sites) ? a.sites : String(a.sites || "").split(/[|;,]/))
    .map((s) => s.trim()).filter((s) => RETAILERS.includes(s));
  const same = a.sameBillingAndShippingAddress !== false && String(a.sameBillingAndShippingAddress).toLowerCase() !== "false";

  const profile = {
    id: "p" + Date.now() + Math.random().toString(36).slice(2, 6),
    label,
    sites: sites.length ? sites : filters.profileSite !== "all" ? [filters.profileSite] : [...RETAILERS],
    email: String(ship.email || bill.email || "").trim(),
    name: String(ship.name || "").trim(),
    phone: String(ship.phone || "").replace(/\D/g, ""),
    line1: String(ship.line1).trim(),
    line2: [ship.line2, ship.line3].filter(Boolean).join(" ").trim(),
    city: String(ship.city).trim(),
    state,
    zip: String(ship.postCode).trim().slice(0, 5),
    cardName: String(pay.nameOnCard || ship.name || "").trim(),
    brand: fromAycdCard(pay.cardType) || (number ? cardBrand(number) : "") || "",
    last4: number.slice(-4),
    exp: month !== "00" && year ? `${month}/${year}` : "",
    active: true,
    aycd: {
      size: a.size || "",
      profileGroup: a.profileGroup || "",
      onlyCheckoutOnce: a.onlyCheckoutOnce === true || String(a.onlyCheckoutOnce).toLowerCase() === "true",
      line3: ship.line3 || "",
      billing: same ? null : bill,
    },
  };
  const card = number && luhnOk(number) ? { number, cvv: String(pay.cardCvv || "").replace(/\D/g, ""), exp: profile.exp, name: profile.cardName } : null;
  if (number && !card) profile.last4 = "";
  return { profile, card };
}

// Small CSV reader: handles quoted fields, escaped quotes and CRLF.
function parseCsv(text) {
  const rows = [];
  let row = [], field = "", quoted = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"' && text[i + 1] === '"') { field += '"'; i++; }
      else if (ch === '"') quoted = false;
      else field += ch;
    } else if (ch === '"') quoted = true;
    else if (ch === ",") { row.push(field); field = ""; }
    else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && text[i + 1] === "\n") i++;
      row.push(field); rows.push(row); row = []; field = "";
    } else field += ch;
  }
  if (field || row.length) { row.push(field); rows.push(row); }
  return rows.filter((r) => r.some((c) => c.trim()));
}

function csvToAycd(text) {
  const [head, ...rows] = parseCsv(text.replace(/^﻿/, ""));
  if (!head) return [];
  const paths = head.map((h) => HEADER_MAP[headerKey(h)]);
  return rows.map((cells) => {
    const obj = {};
    paths.forEach((path, i) => {
      if (!path) return;
      const [top, key] = path.split(".");
      if (key) (obj[top] = obj[top] || {})[key] = cells[i] || "";
      else obj[top] = cells[i] || "";
    });
    return obj;
  });
}

const csvCell = (v) => {
  const s = Array.isArray(v) ? v.join("|") : String(v ?? "");
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
};

function aycdToCsv(list) {
  const lines = list.map((a) => CSV_COLUMNS.map((c) => {
    const [top, key] = c.split(".");
    return csvCell(key ? a[top][key] : a[top]);
  }).join(","));
  return [CSV_COLUMNS.join(","), ...lines].join("\r\n");
}

function download(filename, text, type) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = Object.assign(document.createElement("a"), { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

const importFile = byId("import-file");
byId("import-button").addEventListener("click", () => importFile.click());

importFile.addEventListener("change", async () => {
  const file = importFile.files[0];
  importFile.value = "";
  if (!file) return;
  let list;
  try {
    const text = await file.text();
    if (/\.csv$/i.test(file.name) || file.type === "text/csv") list = csvToAycd(text);
    else {
      const parsed = JSON.parse(text);
      list = Array.isArray(parsed) ? parsed : Array.isArray(parsed.profiles) ? parsed.profiles : [parsed];
    }
  } catch {
    notify("That file couldn't be read. Use an AYCD .json or .csv export.");
    return;
  }
  const taken = new Set(state.profiles.map((p) => p.label.toLowerCase()));
  const added = [], skipped = [];
  list.forEach((a) => {
    const result = a && typeof a === "object" ? fromAycd(a, taken) : { error: "Unknown entry" };
    if (result.error) return skipped.push(result.error);
    state.profiles.push(result.profile);
    if (result.card) saveCard(result.profile.id, result.card);
    added.push(result.profile.id);
  });
  if (!added.length) {
    notify(skipped.length ? `Nothing imported. ${skipped[0]}.` : "No profiles found in that file.");
    return;
  }
  log(`Imported ${plural(added.length, "profile")}`, file.name);
  const ids = new Set(added);
  update(`Imported ${plural(added.length, "profile")}${skipped.length ? `, skipped ${skipped.length}` : ""}`, () => {
    state.profiles = state.profiles.filter((p) => !ids.has(p.id));
    ids.forEach((id) => cardVault.delete(id));
    update("Import undone");
  });
  if (skipped.length) console.warn("Skipped profiles:", skipped);
});

const exportModal = byId("export-modal");
const exportForm = byId("export-form");

function updateExportNote() {
  const withCards = exportForm.elements.cards.checked;
  const full = state.profiles.filter((p) => cardVault.has(p.id)).length;
  byId("export-note").textContent = withCards
    ? `This file will hold card numbers in plain text. Upload it, then delete it. ${full} of ${plural(state.profiles.length, "profile")} ${full === 1 ? "has" : "have"} full card details right now; the rest export without a card number.`
    : "Card numbers and CVV are left blank. Addresses, names and expiry are included.";
}

byId("export-button").addEventListener("click", () => {
  if (!state.profiles.length) return notify("Add a profile first.");
  updateExportNote();
  exportModal.showModal();
});

exportForm.elements.cards.addEventListener("change", updateExportNote);

exportForm.addEventListener("submit", () => {
  const format = exportForm.elements.format.value;
  const list = state.profiles.map((p) => toAycd(p, exportForm.elements.cards.checked));
  const stamp = new Date().toISOString().slice(0, 10);
  if (format === "csv") download(`buffsquad-profiles-${stamp}.csv`, aycdToCsv(list), "text/csv");
  else download(`buffsquad-profiles-${stamp}.json`, JSON.stringify(list, null, 2), "application/json");
  notify(`Exported ${plural(list.length, "profile")} as ${format.toUpperCase()}`);
});

profileForm.addEventListener("change", (e) => {
  if (e.target.name === "sites") byId("sites-error").hidden = true;
});

byId("checkout-search").addEventListener("input", (e) => {
  filters.search = e.target.value;
  renderCheckouts();
});

byId("profile-search").addEventListener("input", (e) => {
  filters.profileSearch = e.target.value;
  renderProfiles();
});

// Deletes the group on screen. The first click arms the button, the second
// deletes. With a site filter on, profiles that also cover the other site
// just drop this site instead of being deleted.
let groupTimer;
byId("delete-group").addEventListener("click", (e) => {
  const btn = e.currentTarget;
  const list = visibleProfiles();
  if (!btn.classList.contains("confirm")) {
    btn.classList.add("confirm");
    byId("delete-group-label").textContent = `Confirm: ${plural(list.length, "profile")}`;
    clearTimeout(groupTimer);
    groupTimer = setTimeout(renderProfiles, 4000);
    return;
  }
  clearTimeout(groupTimer);
  const before = state.profiles.map((p) => ({ ...p, sites: [...p.sites] }));
  const site = filters.profileSite;
  const ids = new Set(list.map((p) => p.id));
  let trimmed = 0;
  state.profiles = state.profiles.filter((p) => {
    if (!ids.has(p.id)) return true;
    if (site !== "all" && p.sites.length > 1) {
      p.sites = p.sites.filter((s) => s !== site);
      trimmed++;
      return true;
    }
    return false;
  });
  const deleted = list.length - trimmed;
  log(`Deleted ${plural(deleted, "profile")}`, site === "all" ? "All sites" : site);
  update(`Deleted ${plural(deleted, "profile")}${trimmed ? `, removed ${site} from ${trimmed} more` : ""}`, () => {
    state.profiles = before;
    update("Profiles restored");
  });
});

// Keeps a segment's pressed button in step with its filter value.
function syncSegment(name) {
  document.querySelectorAll(`[data-filter="${name}"] button`).forEach((b) => b.setAttribute("aria-pressed", b.dataset.value === filters[name]));
}

// One click handler for everything rendered from data.
document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-link], [data-edit-account], [data-unlink], [data-add-profile], [data-edit-profile], [data-delete-profile], [data-profile-active], [data-notify], [data-discord], [data-stage], .segment button");
  if (!el) return;

  if (el.matches(".segment button")) {
    const group = el.parentElement;
    filters[group.dataset.filter] = el.dataset.value;
    group.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", b === el));
    render();
  } else if (el.dataset.stage) {
    filters.stage = el.dataset.stage;
    renderCheckouts();
  } else if (el.dataset.link) {
    openAccountModal(el.dataset.link);
  } else if (el.dataset.editAccount) {
    openAccountModal(null, el.dataset.editAccount);
  } else if (el.dataset.unlink) {
    const { retailer: r, account } = findAccount(el.dataset.unlink);
    const before = accountsFor(r);
    state.accounts[r] = before.filter((a) => a !== account);
    log(`Removed a ${r} account`, account.email);
    update(`${account.email} removed`, () => {
      state.accounts[r] = before;
      update(`${account.email} restored`);
    });
  } else if (el.hasAttribute("data-add-profile")) {
    openProfileModal(null, el.dataset.addProfile);
  } else if (el.dataset.editProfile) {
    openProfileModal(el.dataset.editProfile);
  } else if (el.dataset.deleteProfile) {
    const i = state.profiles.findIndex((p) => p.id === el.dataset.deleteProfile);
    const [removed] = state.profiles.splice(i, 1);
    update(`${removed.label} deleted`, () => {
      state.profiles.splice(i, 0, removed);
      update(`${removed.label} restored`);
    });
  } else if (el.dataset.profileActive) {
    const p = state.profiles.find((x) => x.id === el.dataset.profileActive);
    p.active = !p.active;
    update(`${p.label} ${p.active ? "on" : "paused"}`);
  } else if (el.dataset.notify) {
    const key = el.dataset.notify;
    state.notify[key] = !state.notify[key];
    update(`${NOTIFY_LABELS[key][0]} ${state.notify[key] ? "on" : "off"}`);
  } else if (el.hasAttribute("data-discord") && !state.discord) {
    state.discord = true;
    update();
  }
});

// Signed in through the BUFFSQUAD server: load this member's own data. Without
// a server (static hosting), /api/me isn't there and the sample data stays.
function applyRemote(data) {
  remote = true;
  user.name = data.user.name;
  checkouts.splice(0, checkouts.length, ...data.checkouts);
  declined = data.declined;
  feed.splice(0, feed.length, ...data.feed);
  const fresh = normalize(data.settings, blank);
  Object.keys(state).forEach((k) => delete state[k]);
  Object.assign(state, fresh);
  filters.inboxMethod = state.inbox.method || "aycd";
  applySaved(data.saved);
  document.querySelector(".logout").href = "/auth/logout";
  if (data.user.isAdmin) {
    byId("admin-link").hidden = false;
    checkVersion();
    setInterval(checkVersion, 60000);
  }
  const inboxNote = document.querySelector('[data-view="inbox"] .modal-note span');
  if (inboxNote) inboxNote.textContent = "Your key or app password is stored encrypted on the BUFFSQUAD server and is never saved in your browser.";
}

// Admins only: which update the site is on. When a newer one goes live, the label
// says so and reloads the page on click.
let loadedVersion = null;

async function checkVersion() {
  try {
    const res = await fetch("/api/version", { headers: { Accept: "application/json" } });
    if (!res.ok) return;
    const v = await res.json();
    if (!v.commit) return;
    loadedVersion ??= v.commit;
    const label = byId("app-version");
    const fresh = v.commit !== loadedVersion;
    const at = new Date(v.at).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
    label.hidden = false;
    label.classList.toggle("fresh", fresh);
    label.textContent = fresh ? `↻ v${v.number}` : `v${v.number}`;
    label.title = fresh ? `v${v.number} is live. Click to reload.` : `${v.commit} · updated ${at}: ${v.subject}`;
  } catch {
    // Offline for a moment: try again next time.
  }
}

byId("app-version").addEventListener("click", (e) => {
  if (e.currentTarget.classList.contains("fresh")) location.reload();
});

async function boot() {
  try {
    const res = await fetch("/api/me", { headers: { Accept: "application/json" } });
    if (res.status === 401) {
      location.href = "/auth/discord/login?next=" + encodeURIComponent("/dashboard" + location.hash);
      return;
    }
    if (res.ok && (res.headers.get("Content-Type") || "").includes("json")) applyRemote(await res.json());
  } catch {
    // No server: keep the sample data.
  }
  render();
  route();
}

boot();
