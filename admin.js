// BUFFSQUAD admin panel. Everything comes from /api/admin/*, which only answers
// for Discord accounts listed in ADMIN_DISCORD_IDS.

const STAGES = {
  confirmed: { label: "Confirmed", icon: "check", tone: "cyan" },
  shipped: { label: "Shipped", icon: "truck", tone: "violet" },
  delivered: { label: "Delivered", icon: "box", tone: "green" },
  canceled: { label: "Canceled", icon: "x", tone: "red" },
};

const DM = {
  pending: ["DM sending", "paused"],
  sent: ["DM sent", "paid"],
  failed: ["DM failed", "due"],
  off: ["DMs off", "paused"],
};

const ROUTES = ["overview", "checkouts", "channels", "members", "export", "settings", "activity"];

const data = { members: [], checkouts: [], settings: null };
const filters = { view: "", search: "", memberSearch: "" };
// Checkouts page selection mode: ids ticked for deleting.
const selection = { on: false, ids: new Set() };

const byId = (id) => document.getElementById(id);
const icon = (name) => `<svg class="icon"><use href="#i-${name}"/></svg>`;
const escape = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (n) => "$" + Number(n || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const initials = (name) => (String(name || "").match(/\b[a-z0-9]/gi) || ["?"]).join("").slice(0, 2).toUpperCase();
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const when = (ms) => new Date(ms).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

function ago(ms) {
  const s = Math.round((Date.now() - ms) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

async function api(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) {
    location.href = "/auth/discord/login?next=/admin";
    throw new Error("Signed out");
  }
  const json = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(json.message || `Error ${res.status}`);
  return json;
}

const toast = byId("toast");
let toastTimer;
function notify(message) {
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 2600);
}

const avatar = (m) => m && m.avatar
  ? `<span class="avatar round"><img src="${escape(m.avatar)}" alt=""></span>`
  : `<span class="avatar round">${escape(initials(m ? m.name : "?"))}</span>`;

const stagePill = (c) => `<span class="stage ${STAGES[c.stage].tone}">${icon(STAGES[c.stage].icon)}${STAGES[c.stage].label}</span>`;

const feeStatus = (c) => c.result === "miss"
  ? `<span class="status due">Missed</span>`
  : c.stage === "canceled"
    ? `<span class="status paused">No fee</span>`
    : `<span class="status ${c.paid ? "paid" : "due"}">${c.paid ? "Paid" : "Due"}</span>`;

function checkoutRow(c, selectable = false) {
  const who = c.member
    ? escape(c.member)
    : `<b class="unmatched">Unmatched</b>${c.profileName ? ` · profile “${escape(c.profileName)}”` : " · no profile in webhook"}`;
  const dm = DM[c.dmStatus];
  return `
    <li class="admin-row ${selectable && selection.ids.has(c.id) ? "selected" : ""}" data-checkout="${c.id}" tabindex="0" role="button"
        aria-label="${selectable ? "Select" : "Edit"} ${escape(c.product)}"${selectable ? ` aria-pressed="${selection.ids.has(c.id)}"` : ""}>
      ${selectable ? `<span class="row-check" aria-hidden="true">${icon("check")}</span>` : ""}
      <span class="avatar">${escape(initials(c.retailer))}</span>
      <div class="checkout-info">
        <strong>${escape(c.product)}</strong>
        <span>${who}</span>
        <span>${escape(c.retailer)}${c.order ? ` · #${escape(c.order)}` : ""} · ${escape(c.date)}${c.source ? ` · ${escape(c.source)}` : ""}${dm ? ` · <i class="dm ${dm[1]}" title="${escape(c.dmError)}">${dm[0]}</i>` : ""}</span>
      </div>
      ${c.result === "miss" ? "" : stagePill(c)}
      <div class="checkout-fee">
        <strong>${c.result === "miss" || c.stage === "canceled" ? "—" : money(c.fee)}</strong>
        ${feeStatus(c)}
      </div>
    </li>`;
}

// Overview

async function loadOverview() {
  const o = await api("GET", "/api/admin/overview");
  const tiles = [
    ["user", "cyan", "Members", o.members, "Signed in with Discord"],
    ["bag", "cyan", "Checkouts", o.hits, `${o.today} in the last 24h`],
    ["bolt", "peach", "Unmatched", o.unmatched, o.unmatched ? "Need a member" : "All matched"],
    ["card", "peach", "Fees due", money(o.due), `${money(o.paid)} paid`],
    ["chart", "violet", "Retail value", money(o.value), `${plural(o.misses, "miss")}`],
    ["chat", o.dm_failed ? "peach" : "green", "DMs failed", o.dm_failed, o.dm_failed ? "See Checkouts > DM failed" : "All delivered"],
  ];
  byId("overview-tiles").innerHTML = tiles.map(([ic, tone, label, value, note]) => `
    <div class="tile">
      <span class="tile-icon ${tone}">${icon(ic)}</span>
      <div><small>${label}</small><strong>${escape(value)}</strong><span class="tile-note">${escape(note)}</span></div>
    </div>`).join("");

  const online = o.bot && Date.now() - o.bot.at < 60 * 1000;
  byId("bot-line").innerHTML = o.botError
    ? `<span class="status due">Bot can't sign in</span> ${escape(o.botError.message)}, then run C:\buffsquad\bin\restart.cmd on the server.`
    : o.bot
    ? `<span class="status ${online ? "paid" : "due"}">${online ? "Bot online" : "Bot offline"}</span> ${escape(o.bot.name)} · watching ${plural(o.bot.channels, "channel")} · last seen ${ago(o.bot.at)}`
    : `<span class="status due">Bot not running</span> Start it with <code>python bot.py</code>`;

  lastRecent = o.recent;
  byId("recent-list").innerHTML = o.recent.map((c) => checkoutRow(c)).join("");
  byId("recent-empty").hidden = o.recent.length > 0;
  setUnmatched(o.unmatched);
}

function setUnmatched(n) {
  byId("nav-unmatched").textContent = n || "";
  byId("nav-unmatched").hidden = !n;
  byId("count-unmatched").textContent = n || "";
}

// Checkouts

let searchTimer;
async function loadCheckouts() {
  const params = new URLSearchParams({ view: filters.view, q: filters.search });
  data.checkouts = await api("GET", `/api/admin/checkouts?${params}`);
  // Keep only ticks for checkouts still in this list.
  const shown = new Set(data.checkouts.map((c) => c.id));
  selection.ids.forEach((id) => shown.has(id) || selection.ids.delete(id));
  byId("checkout-list").innerHTML = data.checkouts.map((c) => checkoutRow(c, selection.on)).join("");
  updateBulkBar();
  byId("checkouts-empty").hidden = data.checkouts.length > 0;
  byId("checkouts-empty-text").textContent = filters.search ? "Nothing matches that search." : {
    unmatched: "Every checkout is matched to a member.",
    due: "No fees outstanding.",
    misses: "No misses logged.",
    dm_failed: "Every DM went through.",
  }[filters.view] || "No checkouts yet.";
}

// Selecting and deleting checkouts

function updateBulkBar() {
  const n = selection.ids.size;
  byId("bulk-bar").hidden = !selection.on;
  byId("bulk-count").textContent = `${n} selected`;
  byId("delete-selected").disabled = n === 0;
  byId("delete-selected").lastChild.textContent = n ? `Delete ${n} selected` : "Delete selected";
  byId("delete-view").hidden = data.checkouts.length === 0;
  const all = byId("select-all");
  all.checked = n > 0 && n === data.checkouts.length;
  all.indeterminate = n > 0 && n < data.checkouts.length;
  byId("select-mode").setAttribute("aria-pressed", selection.on);
  byId("select-mode").lastChild.textContent = selection.on ? "Done selecting" : "Select";
}

function toggleRow(row) {
  const id = Number(row.dataset.checkout);
  if (selection.ids.has(id)) selection.ids.delete(id);
  else selection.ids.add(id);
  row.classList.toggle("selected", selection.ids.has(id));
  row.setAttribute("aria-pressed", selection.ids.has(id));
  updateBulkBar();
}

byId("select-mode").addEventListener("click", () => {
  selection.on = !selection.on;
  selection.ids.clear();
  byId("checkout-list").innerHTML = data.checkouts.map((c) => checkoutRow(c, selection.on)).join("");
  updateBulkBar();
});

byId("select-all").addEventListener("change", (e) => {
  selection.ids = new Set(e.target.checked ? data.checkouts.map((c) => c.id) : []);
  byId("checkout-list").innerHTML = data.checkouts.map((c) => checkoutRow(c, true)).join("");
  updateBulkBar();
});

const VIEW_NAMES = { "": "Hits", unmatched: "Unmatched", due: "Fee due", misses: "Misses", dm_failed: "DM failed" };
const confirmModal = byId("confirm-modal");
let pendingDelete = null;

function askToDelete(request, count, typed) {
  pendingDelete = request;
  byId("confirm-title").textContent = `Delete ${plural(count, "checkout")}?`;
  byId("confirm-text").textContent = (request.all
    ? `Everything in ${VIEW_NAMES[request.view]}${request.q ? ` matching “${request.q}”` : ""}, including any not shown in the list. `
    : "") + "They disappear from the admin panel and from members' dashboards, along with any fees owed on them. The nightly backup on the server is the only way back.";
  byId("confirm-type-row").hidden = !typed;
  byId("confirm-type").value = "";
  byId("confirm-error").hidden = true;
  byId("confirm-go").textContent = `Delete ${plural(count, "checkout")}`;
  confirmModal.showModal();
  (typed ? byId("confirm-type") : byId("confirm-go")).focus();
}

byId("delete-selected").addEventListener("click", () => {
  if (selection.ids.size) askToDelete({ ids: [...selection.ids] }, selection.ids.size, false);
});

byId("delete-view").addEventListener("click", async () => {
  try {
    const request = { all: true, view: filters.view, q: filters.search };
    const { count } = await api("POST", "/api/admin/checkouts/delete", { ...request, dryRun: true });
    if (!count) return notify("Nothing to delete in this view.");
    askToDelete(request, count, true);
  } catch (err) {
    notify(err.message);
  }
});

byId("confirm-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!byId("confirm-type-row").hidden && byId("confirm-type").value.trim() !== "DELETE") {
    byId("confirm-error").textContent = "Type DELETE in capitals to confirm.";
    byId("confirm-error").hidden = false;
    return;
  }
  try {
    const { deleted } = await api("POST", "/api/admin/checkouts/delete", pendingDelete);
    confirmModal.close();
    selection.ids.clear();
    notify(`Deleted ${plural(deleted, "checkout")}`);
    refresh();
  } catch (err) {
    byId("confirm-error").textContent = err.message;
    byId("confirm-error").hidden = false;
  }
});

byId("checkout-search").addEventListener("input", (e) => {
  filters.search = e.target.value.trim();
  clearTimeout(searchTimer);
  searchTimer = setTimeout(loadCheckouts, 250);
});

document.querySelector('[data-filter="view"]').addEventListener("click", (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  filters.view = btn.dataset.value;
  btn.parentElement.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", b === btn));
  loadCheckouts();
});

// Channels

async function loadChannels() {
  const { channels, status, bot } = await api("GET", "/api/admin/channels");
  const online = bot && Date.now() - bot.at < 60 * 1000;
  let problems = 0;
  byId("channel-list").innerHTML = channels.map((c) => {
    const st = status[c.id];
    // Until the bot has looked at a new channel (or while it's offline) there's no status yet.
    const state = !online || !st ? ["paused", online ? "Checking…" : "Waiting for bot"] : st.ok ? ["paid", "Watching"] : ["due", "Bot can't see it"];
    if (online && st && !st.ok) problems++;
    const where = st && st.name ? `#${escape(st.name)}${st.guild ? ` in ${escape(st.guild)}` : ""}` : "";
    return `
      <li class="channel-row">
        <span class="avatar">#</span>
        <div class="checkout-info">
          <strong>${escape(c.label || (st && st.name ? "#" + st.name : "Checkout channel"))}</strong>
          <span><span class="mono">${escape(c.id)}</span>${where ? ` · ${where}` : ""}</span>
          <span>${online && st && !st.ok ? `<b class="unmatched">${escape(st.problem)}</b>` : `${plural(c.checkouts, "checkout")} logged · added by ${escape(c.addedBy)} ${ago(c.addedAt)}`}</span>
        </div>
        <span class="status ${state[0]}">${state[1]}</span>
        <button class="icon-button danger" type="button" data-remove-channel="${escape(c.id)}" aria-label="Stop watching ${escape(c.label || c.id)}" title="Stop watching">${icon("trash")}</button>
      </li>`;
  }).join("");
  byId("channels-empty").hidden = channels.length > 0;
  byId("channel-count").textContent = channels.length ? plural(channels.length, "channel") : "";
  // Badge: a count of channels the bot can't read, or "!" when there are none at all.
  byId("nav-channels").textContent = problems || (channels.length ? "" : "!");
  byId("nav-channels").hidden = !(problems || !channels.length);
}

byId("channel-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const error = byId("channel-error");
  error.hidden = true;
  try {
    await api("POST", "/api/admin/channels", { id: byId("channel-id").value, label: byId("channel-label").value });
    e.target.reset();
    notify("Channel added. The bot starts watching it within 10 seconds.");
    loadChannels();
  } catch (err) {
    error.textContent = err.message;
    error.hidden = false;
  }
});

byId("channel-form").addEventListener("input", () => (byId("channel-error").hidden = true));

// Two clicks to remove: the first arms the button, the second removes.
byId("channel-list").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-remove-channel]");
  if (!btn) return;
  if (!btn.classList.contains("confirm")) {
    btn.classList.add("confirm");
    btn.title = "Click again to stop watching";
    setTimeout(() => btn.isConnected && btn.classList.remove("confirm"), 4000);
    return;
  }
  try {
    await api("DELETE", `/api/admin/channels/${btn.dataset.removeChannel}`);
    notify("Channel removed. The bot stops watching it within 10 seconds.");
    loadChannels();
  } catch (err) {
    notify(err.message);
  }
});

// Members

async function loadMembers() {
  data.members = await api("GET", "/api/admin/members");
  renderMembers();
}

function renderMembers() {
  const q = filters.memberSearch.toLowerCase();
  const list = data.members.filter((m) => !q || `${m.name} ${m.username} ${m.discordId}`.toLowerCase().includes(q));
  byId("member-list").innerHTML = list.map((m) => `
    <li class="admin-row" data-member="${m.id}" tabindex="0" role="button" aria-label="Open ${escape(m.name)}">
      ${avatar(m)}
      <div class="checkout-info">
        <strong>${escape(m.name)} ${m.isAdmin ? `<span class="status paid">Admin</span>` : ""}${m.disabled ? `<span class="status due">Off</span>` : ""}</strong>
        <span>@${escape(m.username)} · ${plural(m.profiles, "profile")} · ${plural(m.accounts, "account")} · ${m.inbox ? (m.inbox === "aycd" ? "AYCD Inbox" : "Gmail") : "No inbox"}</span>
        <span>Last seen ${m.lastLogin ? ago(m.lastLogin) : "never"}</span>
      </div>
      <span class="stage cyan">${icon("bag")}${plural(m.hits, "hit")}</span>
      <div class="checkout-fee">
        <strong>${money(m.due)}</strong>
        <span class="status ${m.due ? "due" : "paid"}">${m.due ? "Due" : "Paid up"}</span>
      </div>
    </li>`).join("");
  byId("members-empty").hidden = list.length > 0;
  if (!list.length && data.members.length) byId("members-empty").querySelector("strong").textContent = "Nothing matches that search.";
}

byId("member-search").addEventListener("input", (e) => {
  filters.memberSearch = e.target.value.trim();
  renderMembers();
});

const memberModal = byId("member-modal");
let openMember = null;

const revealButton = (kind, ref, label = "Reveal") =>
  `<button class="button ghost small" type="button" data-reveal="${kind}" data-ref="${escape(ref)}">${icon("eye")}${label}</button>`;

async function openMemberModal(id) {
  const m = await api("GET", `/api/admin/members/${id}`);
  openMember = m;
  const s = m.settings || {};
  const profiles = s.profiles || [];
  const accounts = s.accounts || {};
  const saved = { cards: new Set(m.saved.cards), accounts: new Set(m.saved.accounts) };

  const retailers = data.settings ? data.settings.retailers : ["Target", "Pokemon Center"];

  // Header
  byId("member-avatar").outerHTML = avatar(m).replace('class="avatar round"', 'class="avatar round" id="member-avatar"');
  byId("member-modal-title").textContent = m.name;
  byId("member-tags").innerHTML = [
    m.isAdmin ? `<span class="status paid">Admin</span>` : "",
    m.disabled ? `<span class="status due">Turned off</span>` : `<span class="status running">Active</span>`,
  ].join("");
  byId("member-sub").innerHTML = `
    <span>@${escape(m.username)}</span>
    <span class="member-discord">Discord ID <code>${escape(m.discordId)}</code>
      <button class="icon-button tiny" type="button" data-copy="${escape(m.discordId)}" aria-label="Copy Discord ID" title="Copy Discord ID">${icon("copy")}</button></span>
    <span>Joined ${escape(new Date(m.createdAt).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }))}</span>
    <span>Last seen ${m.lastLogin ? ago(m.lastLogin) : "never"}</span>`;
  byId("member-toggle").innerHTML = m.disabled ? `${icon("check")}Turn account back on` : `${icon("x")}Turn account off`;
  byId("member-toggle").hidden = m.isAdmin;

  const section = (title, count, body, tools = "") => `
    <section class="m-section">
      <div class="m-head">
        <h3 class="label">${title}${count === null ? "" : ` <b>${count}</b>`}</h3>
        ${tools}
      </div>
      ${body}
    </section>`;
  const empty = (ic, text) => `<div class="m-empty"><span class="empty-icon">${icon(ic)}</span><span>${text}</span></div>`;

  // At a glance, in the dashboard's tile style
  const stats = `
    <div class="m-stats">
      <div class="tile"><span class="tile-icon cyan">${icon("bag")}</span><div><small>Checkouts</small><strong>${m.hits}</strong></div></div>
      <div class="tile ${m.due ? "due" : ""}"><span class="tile-icon peach">${icon("card")}</span><div><small>Fees due</small><strong>${money(m.due)}</strong></div></div>
      <div class="tile"><span class="tile-icon green">${icon("check")}</span><div><small>Fees paid</small><strong>${money(m.paid)}</strong></div></div>
      <div class="tile"><span class="tile-icon violet">${icon("user")}</span><div><small>Set up</small><strong>${plural(m.profiles, "profile")}</strong><span class="tile-note">${plural(m.accounts, "account")}</span></div></div>
    </div>`;

  // Profiles, with this member's AYCD download in the section header
  const active = profiles.filter((p) => p.active !== false);
  const downloads = active.length ? `
    <div class="m-tools">
      ${[...retailers, ""].map((r) => {
        const n = r ? active.filter((p) => (p.sites || []).includes(r)).length : active.length;
        return `<button class="button ghost small" type="button" data-member-export="${escape(r)}" ${n ? "" : "disabled"}
          title="Download ${r ? escape(r) + " " : ""}profiles as AYCD JSON">${icon("download")}${r ? escape(r) : "All"} <b class="count">${n}</b></button>`;
      }).join("")}
      <label class="check-row m-cards"><input type="checkbox" id="member-export-cards" checked><span>With cards</span></label>
    </div>` : "";
  const profileHtml = profiles.length ? `<div class="m-grid">${profiles.map((p) => `
    <article class="m-card ${p.active === false ? "off" : ""}">
      <div class="m-card-head">
        <strong>${escape(p.label)}</strong>
        <code title="Name in the checkout bots">${escape(p.botName || "—")}</code>
        ${p.active === false ? `<span class="status paused">Paused</span>` : ""}
      </div>
      <div class="m-sites">${(p.sites || []).map((site) => `<span class="retailer"><i></i>${escape(site)}</span>`).join("")}</div>
      <dl>
        <dt>Ship to</dt><dd>${escape(p.name)}<br>${escape(p.line1)}${p.line2 ? ", " + escape(p.line2) : ""}<br>${escape(p.city)}, ${escape(p.state)} ${escape(p.zip)}</dd>
        <dt>Email</dt><dd>${escape(p.email || "—")}</dd>
        <dt>Phone</dt><dd>${escape(p.phone || "—")}</dd>
        <dt>Card</dt><dd class="secret" data-secret="card:${escape(p.id)}">${p.last4 ? `<span>${escape(p.brand)} •••• ${escape(p.last4)} · ${escape(p.exp)}</span>` : "<span>No card</span>"}
          ${saved.cards.has(String(p.id)) ? revealButton("card", p.id, "Full card") : p.last4 ? `<small>Number not saved on server</small>` : ""}</dd>
      </dl>
    </article>`).join("")}</div>
    ${active.length ? `<p class="field-hint m-hint">Downloads are AYCD JSON named by bot name. Paused profiles are left out, and each download is logged under Activity.</p>` : ""}`
    : empty("user", "No profiles yet.");

  // Retailer accounts
  const accountHtml = `<div class="m-grid">${retailers.map((retailer) => {
    const list = accounts[retailer] || [];
    return `
      <article class="m-card">
        <div class="m-card-head">
          <span class="avatar">${escape(initials(retailer))}</span>
          <strong>${escape(retailer)}</strong>
          <span class="status ${list.length ? "paid" : "paused"}">${plural(list.length, "account")}</span>
        </div>
        ${list.length ? `<ul class="plain-list">${list.map((a) => `
          <li class="secret" data-secret="account:${escape(a.id)}"><span>${escape(a.email)}</span>
            ${saved.accounts.has(String(a.id)) ? revealButton("account", a.id, "Password") : `<small>No password saved</small>`}</li>`).join("")}</ul>`
          : `<p class="field-hint">No ${escape(retailer)} accounts yet.</p>`}
      </article>`;
  }).join("")}</div>`;

  // Inbox and notifications, side by side
  const inbox = s.inbox || {};
  const notify = { checkout: "Checkout alerts", reminders: "Drop reminders", receipts: "Email receipts" };
  const connections = `
    <div class="m-grid">
      <article class="m-card">
        <div class="m-card-head">
          <span class="tile-icon cyan small-icon">${icon("inbox")}</span>
          <strong>Inbox</strong>
          <span class="status ${inbox.method ? "paid" : "due"}">${inbox.method ? "Connected" : "Not connected"}</span>
        </div>
        ${inbox.method
          ? `<div class="secret" data-secret="inbox:inbox"><span>${inbox.method === "aycd" ? "AYCD Inbox" : `Gmail · ${escape(inbox.email)}`}</span>
              ${m.saved.inbox ? revealButton("inbox", "inbox", inbox.method === "aycd" ? "Key" : "App password") : `<small>Not saved on server</small>`}</div>`
          : `<p class="field-hint">No inbox connected for verification codes.</p>`}
      </article>
      <article class="m-card">
        <div class="m-card-head">
          <span class="tile-icon violet small-icon">${icon("bell")}</span>
          <strong>Notifications</strong>
        </div>
        <ul class="m-toggles">${Object.entries(notify).map(([key, label]) => {
          const on = (s.notify || {})[key] ?? key !== "receipts";
          return `<li><span>${label}</span><span class="status ${on ? "paid" : "paused"}">${on ? "On" : "Off"}</span></li>`;
        }).join("")}</ul>
      </article>
    </div>`;

  byId("member-body").innerHTML = [
    stats,
    section("Profiles", profiles.length, profileHtml, downloads),
    section("Retailer accounts", Object.values(accounts).reduce((n, list) => n + (list ? list.length : 0), 0), accountHtml),
    section("Inbox & notifications", null, connections),
    section("Checkouts", m.checkouts.length, m.checkouts.length
      ? `<ul class="checkout-list">${m.checkouts.map((c) => checkoutRow(c)).join("")}</ul>`
      : empty("bag", "No checkouts yet.")),
  ].join("");
  byId("member-body").scrollTop = 0;
  if (!memberModal.open) memberModal.showModal();
}

byId("member-sub").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-copy]");
  if (!btn) return;
  navigator.clipboard.writeText(btn.dataset.copy).then(() => notify("Discord ID copied"), () => notify("Couldn't copy. Select the ID and copy it instead."));
});

byId("member-body").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-member-export]");
  if (!btn || !openMember) return;
  const params = new URLSearchParams({
    userId: openMember.id,
    retailer: btn.dataset.memberExport,
    cards: byId("member-export-cards").checked ? "1" : "0",
  });
  location.href = `/api/admin/export/profiles?${params}`;
  notify(`Downloading ${openMember.name}'s ${btn.dataset.memberExport || "profiles"}${btn.dataset.memberExport ? " profiles" : ""}`);
});

byId("member-body").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-reveal]");
  if (!btn || !openMember) return;
  try {
    const { value } = await api("POST", "/api/admin/reveal", { userId: openMember.id, kind: btn.dataset.reveal, ref: btn.dataset.ref });
    const text = typeof value === "object"
      ? `${value.number.replace(/(\d{4})(?=\d)/g, "$1 ")} · CVV ${value.cvv} · ${value.exp}`
      : value;
    const shown = document.createElement("code");
    shown.className = "revealed";
    shown.textContent = text;
    btn.replaceWith(shown);
    // Hide it again after 30 seconds.
    setTimeout(() => shown.isConnected && shown.replaceWith(Object.assign(document.createElement("small"), { textContent: "Hidden again" })), 30000);
  } catch (err) {
    notify(err.message);
  }
});

byId("member-toggle").addEventListener("click", async () => {
  if (!openMember) return;
  try {
    await api("PATCH", `/api/admin/members/${openMember.id}`, { disabled: !openMember.disabled });
    notify(openMember.disabled ? `${openMember.name} is back on` : `${openMember.name} is turned off and signed out`);
    await openMemberModal(openMember.id);
    loadMembers();
  } catch (err) {
    notify(err.message);
  }
});

// Add / edit checkout

const checkoutModal = byId("checkout-modal");
const checkoutForm = byId("checkout-form");
let editing = null;

async function openCheckoutModal(id) {
  if (!data.members.length) await loadMembers();
  if (!data.settings) await loadSettings();
  const c = id ? findCheckout(id) : null;
  editing = c;
  checkoutForm.reset();
  const f = checkoutForm.elements;
  f.userId.innerHTML = `<option value="">${c ? "Unmatched (no member)" : "Pick a member"}</option>` +
    data.members.map((m) => `<option value="${m.id}">${escape(m.name)} (@${escape(m.username)})</option>`).join("");
  const retailers = [...data.settings.retailers, ...(c && c.retailer && !data.settings.retailers.includes(c.retailer) ? [c.retailer] : []), "Other"];
  f.retailer.innerHTML = retailers.map((r) => `<option>${escape(r)}</option>`).join("");

  byId("checkout-modal-title").textContent = c ? c.product : "Add checkout";
  byId("checkout-modal-eyebrow").textContent = c ? (c.result === "miss" ? "Missed checkout" : "Checkout") : "Manual checkout";
  byId("checkout-source").textContent = c
    ? `${c.source === "manual" ? "Added by hand" : `From ${c.source || "the checkout channel"}`} · ${when(c.at)}${c.profileName ? ` · profile “${c.profileName}”` : ""}${c.dmError ? ` · DM error: ${c.dmError}` : ""}`
    : "For orders the bot didn't catch. The member's dashboard shows it straight away.";
  byId("fee-source").hidden = !c || c.result === "miss";
  if (c) byId("fee-source").textContent = feeSourceText(c);
  byId("checkout-delete").hidden = !c;
  byId("checkout-resend").hidden = !(c && c.userId && c.result === "hit");
  byId("checkout-submit").textContent = c ? "Save changes" : "Add checkout";
  byId("checkout-error").hidden = true;

  if (c) {
    f.userId.value = c.userId || "";
    f.product.value = c.product;
    f.retailer.value = c.retailer;
    f.order.value = c.order;
    f.sku.value = c.sku || "";
    f.qty.value = c.qty;
    f.price.value = c.price || "";
    f.fee.value = c.fee;
    f.stage.value = c.stage;
    f.paid.checked = c.paid;
    // Only offer a DM when this edit gives the checkout a member.
    f.notify.checked = !c.userId;
  }
  updateNotifyRow();
  checkoutModal.showModal();
}

function updateNotifyRow() {
  const f = checkoutForm.elements;
  const assigning = f.userId.value && (!editing || String(editing.userId || "") !== f.userId.value);
  f.notify.closest(".check-row").hidden = !assigning || (editing && editing.result === "miss");
}

checkoutForm.elements.userId.addEventListener("change", updateNotifyRow);

const findCheckout = (id) => data.checkouts.find((c) => c.id === id) || lastRecent.find((c) => c.id === id) || memberCheckouts().find((c) => c.id === id);
const memberCheckouts = () => (openMember ? openMember.checkouts : []);
let lastRecent = [];

checkoutForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = checkoutForm.elements;
  const body = {
    userId: f.userId.value ? Number(f.userId.value) : null,
    product: f.product.value.trim(),
    retailer: f.retailer.value,
    order: f.order.value.trim(),
    sku: f.sku.value.trim(),
    qty: Number(f.qty.value || 1),
    price: Number(f.price.value || 0),
    stage: f.stage.value,
    paid: f.paid.checked,
    notify: f.notify.checked && !f.notify.closest(".check-row").hidden,
  };
  if (f.fee.value !== "") body.fee = Number(f.fee.value);
  if (!editing && !body.userId) {
    byId("checkout-error").textContent = "Pick the member this checkout is for.";
    byId("checkout-error").hidden = false;
    return;
  }
  try {
    if (editing) {
      await api("PATCH", `/api/admin/checkouts/${editing.id}`, body);
    } else {
      const { id } = await api("POST", "/api/admin/checkouts", body);
      // Stage and paid aren't part of adding; set them if they differ from the defaults.
      if (body.stage !== "confirmed" || body.paid) await api("PATCH", `/api/admin/checkouts/${id}`, { stage: body.stage, paid: body.paid });
    }
    checkoutModal.close();
    notify(editing ? "Checkout saved" : "Checkout added");
    refresh();
  } catch (err) {
    byId("checkout-error").textContent = err.message;
    byId("checkout-error").hidden = false;
  }
});

byId("checkout-delete").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  if (!btn.classList.contains("confirm")) {
    btn.classList.add("confirm");
    btn.lastChild.textContent = "Click again to delete";
    setTimeout(() => { btn.classList.remove("confirm"); btn.lastChild.textContent = "Delete"; }, 4000);
    return;
  }
  try {
    await api("DELETE", `/api/admin/checkouts/${editing.id}`);
    checkoutModal.close();
    notify("Checkout deleted");
    refresh();
  } catch (err) {
    notify(err.message);
  }
});

byId("checkout-resend").addEventListener("click", async () => {
  try {
    await api("PATCH", `/api/admin/checkouts/${editing.id}`, { resendDm: true });
    checkoutModal.close();
    notify("DM queued. The bot sends it within a few seconds.");
    refresh();
  } catch (err) {
    notify(err.message);
  }
});

byId("add-checkout").addEventListener("click", () => openCheckoutModal(null));

// Where a checkout's fee came from, for the checkout editor.
function feeSourceText(c) {
  if (c.feeSource === "manual") return "Fee set by hand. Applying fee rules won't change it.";
  if (c.feeSource === "default") {
    const key = data.settings && data.settings.fees[c.retailer] !== undefined ? c.retailer : "Other";
    const pct = data.settings && (data.settings.feeTypes || {})[key] === "percent";
    return `Fee from the ${key === "Other" ? "other retailers" : key} default in Settings${pct ? ` (${data.settings.fees[key]}% of price)` : ""}.`;
  }
  if (c.feeSource && c.feeSource.startsWith("rule:")) {
    const rule = ((data.settings && data.settings.rules) || []).find((r) => `rule:${r.id}` === c.feeSource);
    return rule ? `Fee from the rule ${ruleSummary(rule)}.` : "Fee from a rule that has since been deleted.";
  }
  return "Fee from the retailer default.";
}

// Export

function renderExport() {
  if (!data.settings) return;
  const sel = byId("export-retailer");
  sel.innerHTML = `<option value="">All retailers</option>` + data.settings.retailers.map((r) => `<option>${escape(r)}</option>`).join("");
  byId("export-accounts").innerHTML = data.settings.retailers.map((r) =>
    `<a class="button ghost" href="/api/admin/export/accounts?retailer=${encodeURIComponent(r)}" download>${icon("download")}${escape(r)} accounts</a>`).join("");
}

byId("export-profiles").addEventListener("click", () => {
  const params = new URLSearchParams({ retailer: byId("export-retailer").value, cards: byId("export-cards").checked ? "1" : "0" });
  location.href = `/api/admin/export/profiles?${params}`;
  notify("Downloading profiles");
});

// Settings

async function loadSettings() {
  data.settings = await api("GET", "/api/admin/settings");
  const types = data.settings.feeTypes || {};
  byId("fee-fields").innerHTML = Object.entries(data.settings.fees).map(([r, fee]) => `
    <div class="fee-field">
      <label for="fee-${escape(r)}">${escape(r === "Other" ? "Any other retailer" : r)}</label>
      <div class="fee-input">
        <input id="fee-${escape(r)}" data-fee="${escape(r)}" type="number" min="0" step="0.01" value="${fee}">
        <select data-fee-type="${escape(r)}" aria-label="${escape(r === "Other" ? "Other retailers" : r)} fee type">
          <option value="flat" ${types[r] !== "percent" ? "selected" : ""}>$ per checkout</option>
          <option value="percent" ${types[r] === "percent" ? "selected" : ""}>% of price</option>
        </select>
      </div>
    </div>`).join("");
  // Don't wipe rule edits that haven't been saved yet.
  if (!rulesDirty) renderRules(data.settings.rules);
  renderExport();
}

// Fee rules

const KINDS = { flat: "$ per checkout", per_item: "$ per item", percent: "% of price" };

function ruleSummary(r) {
  const what = r.match === "sku" ? `SKU “${r.text}”` : `“${r.text}”`;
  const fee = r.kind === "percent" ? `${r.amount}% of price` : `${money(r.amount)}${r.kind === "per_item" ? " per item" : ""}`;
  return `${what}${r.retailer ? ` at ${r.retailer}` : ""} → ${fee}`;
}

function ruleRow(r) {
  const retailers = data.settings ? data.settings.retailers : ["Target", "Pokemon Center"];
  const opt = (value, label, current) => `<option value="${escape(value)}" ${value === current ? "selected" : ""}>${escape(label)}</option>`;
  return `
    <li class="rule" data-rule="${escape(r.id || "")}">
      <span class="rule-num" aria-hidden="true"></span>
      <label class="rule-match">
        <span class="sr-only">Match by</span>
        <select data-field="match">${opt("product", "Product contains", r.match)}${opt("sku", "SKU / TCIN is", r.match)}</select>
      </label>
      <label class="rule-text">
        <span class="sr-only">Text to match</span>
        <input data-field="text" value="${escape(r.text || "")}" maxlength="100" placeholder="${r.match === "sku" ? "94681785" : "Prismatic Evolutions"}">
      </label>
      <label class="rule-retailer">
        <span class="sr-only">Retailer</span>
        <select data-field="retailer">${opt("", "Any retailer", r.retailer || "")}${retailers.map((x) => opt(x, x, r.retailer)).join("")}</select>
      </label>
      <label class="rule-amount">
        <span class="sr-only">Fee</span>
        <input data-field="amount" type="number" min="0" step="0.01" value="${r.amount ?? ""}" placeholder="20">
      </label>
      <label class="rule-kind">
        <span class="sr-only">Fee type</span>
        <select data-field="kind">${Object.entries(KINDS).map(([k, label]) => opt(k, label, r.kind || "flat")).join("")}</select>
      </label>
      <div class="rule-tools">
        <button class="icon-button" type="button" data-rule-move="-1" aria-label="Move up" title="Move up">${icon("up")}</button>
        <button class="icon-button" type="button" data-rule-move="1" aria-label="Move down" title="Move down">${icon("down")}</button>
        <button class="icon-button danger" type="button" data-rule-delete aria-label="Delete rule" title="Delete rule">${icon("trash")}</button>
      </div>
    </li>`;
}

function renderRules(rules) {
  byId("rule-list").innerHTML = rules.map(ruleRow).join("");
  rulesChanged(false);
}

function rulesChanged(dirty = true) {
  const n = byId("rule-list").children.length;
  byId("rules-empty").hidden = n > 0;
  byId("rule-count").textContent = n ? plural(n, "rule") : "";
  byId("rules-dirty").hidden = !dirty;
  byId("rules-error").hidden = true;
  rulesDirty = dirty;
}

let rulesDirty = false;

function readRules() {
  return [...byId("rule-list").children].map((li) => {
    const get = (field) => li.querySelector(`[data-field="${field}"]`).value;
    return { id: li.dataset.rule, match: get("match"), text: get("text").trim(), retailer: get("retailer"), kind: get("kind"), amount: get("amount") };
  });
}

byId("add-rule").addEventListener("click", () => {
  byId("rule-list").insertAdjacentHTML("beforeend", ruleRow({ match: "product", kind: "flat" }));
  rulesChanged();
  byId("rule-list").lastElementChild.querySelector('[data-field="text"]').focus();
});

byId("rule-list").addEventListener("input", () => rulesChanged());
byId("rule-list").addEventListener("change", (e) => {
  // Keep the example in the text box in step with what's being matched.
  if (e.target.dataset.field === "match") {
    e.target.closest(".rule").querySelector('[data-field="text"]').placeholder = e.target.value === "sku" ? "94681785" : "Prismatic Evolutions";
  }
  rulesChanged();
});

byId("rule-list").addEventListener("click", (e) => {
  const li = e.target.closest(".rule");
  if (!li) return;
  const move = e.target.closest("[data-rule-move]");
  if (move) {
    const sibling = move.dataset.ruleMove === "-1" ? li.previousElementSibling : li.nextElementSibling;
    if (sibling) {
      if (move.dataset.ruleMove === "-1") sibling.before(li);
      else sibling.after(li);
      rulesChanged();
    }
  } else if (e.target.closest("[data-rule-delete]")) {
    li.remove();
    rulesChanged();
  }
});

byId("rules-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const { rules } = await api("PUT", "/api/admin/fee-rules", { rules: readRules() });
    data.settings.rules = rules;
    renderRules(rules);
    notify("Fee rules saved. New checkouts use them.");
  } catch (err) {
    byId("rules-error").textContent = err.message;
    byId("rules-error").hidden = false;
  }
});

// First click counts what would change, second click applies it.
let applyArmed = false;
byId("apply-rules").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  if (rulesDirty) {
    notify("Save your rules first, then apply them.");
    return;
  }
  try {
    if (!applyArmed) {
      const { changed, difference } = await api("POST", "/api/admin/fee-rules/apply", { dryRun: true });
      if (!changed) {
        notify("Every unpaid checkout already has the right fee.");
        return;
      }
      applyArmed = true;
      btn.classList.add("confirm");
      btn.textContent = `Update ${plural(changed, "checkout")} (${difference >= 0 ? "+" : "−"}${money(Math.abs(difference))})`;
      setTimeout(() => {
        applyArmed = false;
        btn.classList.remove("confirm");
        btn.textContent = "Apply to unpaid checkouts";
      }, 6000);
      return;
    }
    const { changed } = await api("POST", "/api/admin/fee-rules/apply", { dryRun: false });
    applyArmed = false;
    btn.classList.remove("confirm");
    btn.textContent = "Apply to unpaid checkouts";
    notify(`Updated the fee on ${plural(changed, "checkout")}`);
  } catch (err) {
    notify(err.message);
  }
});

byId("fees-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fees = Object.fromEntries([...e.target.querySelectorAll("[data-fee]")].map((el) => [el.dataset.fee, Number(el.value || 0)]));
  const feeTypes = Object.fromEntries([...e.target.querySelectorAll("[data-fee-type]")].map((el) => [el.dataset.feeType, el.value]));
  try {
    await api("PUT", "/api/admin/settings", { fees, feeTypes });
    notify("Fees saved. New checkouts use them.");
    loadSettings();
  } catch (err) {
    notify(err.message);
  }
});

// Activity

const ACTIONS = {
  login: "Signed in", "reveal.card": "Revealed a card", "reveal.account": "Revealed a password", "reveal.inbox": "Revealed an inbox key",
  "export.profiles": "Exported profiles", "export.accounts": "Exported accounts", "checkout.add": "Added a checkout",
  "checkout.edit": "Edited a checkout", "checkout.delete": "Deleted a checkout", "settings.fees": "Changed fees", "settings.rules": "Changed fee rules", "checkout.refee": "Applied fee rules",
  "member.disable": "Turned off a member", "member.enable": "Turned on a member",
  "channel.add": "Added a checkout channel", "channel.remove": "Removed a checkout channel",
  "checkout.bulk_delete": "Deleted checkouts",
};

async function loadAudit() {
  const rows = await api("GET", "/api/admin/audit");
  byId("audit-list").innerHTML = rows.map((r) => `
    <li>
      <span class="avatar round">${escape(initials(r.who || "?"))}</span>
      <div class="checkout-info">
        <strong>${escape(ACTIONS[r.action] || r.action)}</strong>
        <span>${escape(r.who || "Someone")} · ${escape(r.detail)}</span>
      </div>
      <span class="field-hint">${when(r.at)}</span>
    </li>`).join("") || `<li class="mini-empty">Nothing yet.</li>`;
}

// Routing and clicks

const loaders = {
  overview: loadOverview,
  checkouts: loadCheckouts,
  channels: loadChannels,
  members: loadMembers,
  export: loadSettings,
  settings: loadSettings,
  activity: loadAudit,
};

function current() {
  const hash = location.hash.slice(1);
  return ROUTES.includes(hash) ? hash : "overview";
}

function route() {
  const name = current();
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
  refresh();
}

function refresh() {
  loaders[current()]().catch((err) => notify(err.message));
  // Keep the Unmatched badge current on every page.
  if (current() !== "overview") api("GET", "/api/admin/overview").then((o) => setUnmatched(o.unmatched)).catch(() => {});
  if (openMember && memberModal.open) openMemberModal(openMember.id).catch(() => {});
}

window.addEventListener("hashchange", route);

function activate(e) {
  const row = e.target.closest("[data-checkout], [data-member]");
  if (!row || e.target.closest("button, a")) return;
  // In select mode, clicking a checkout on the Checkouts page ticks it instead of opening it.
  if (selection.on && row.dataset.checkout && row.closest("#checkout-list")) return toggleRow(row);
  if (row.dataset.checkout) openCheckoutModal(Number(row.dataset.checkout)).catch((err) => notify(err.message));
  else openMemberModal(Number(row.dataset.member)).catch((err) => notify(err.message));
}

document.addEventListener("click", activate);
document.addEventListener("keydown", (e) => {
  if ((e.key === "Enter" || e.key === " ") && e.target.matches("[data-checkout], [data-member]")) {
    e.preventDefault();
    activate(e);
  }
});

document.querySelectorAll("[data-close-modal]").forEach((btn) => btn.addEventListener("click", () => btn.closest("dialog").close()));
document.querySelectorAll(".modal").forEach((modal) => modal.addEventListener("click", (e) => {
  if (e.target === modal) modal.close();
}));
memberModal.addEventListener("close", () => (openMember = null));

// New checkouts appear without a reload.
setInterval(() => {
  if (!document.hidden && !checkoutModal.open && !confirmModal.open && ["overview", "checkouts", "channels"].includes(current())) refresh();
}, 15000);

(async function boot() {
  try {
    const me = await api("GET", "/api/me");
    byId("user-chip-name").textContent = me.user.name;
    byId("user-avatar").textContent = initials(me.user.name);
  } catch {
    // Signed out: api() already redirected.
  }
  route();
})();
