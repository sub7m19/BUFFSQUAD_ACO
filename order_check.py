"""Order check (test): did a member's checkouts actually stick?

For each of a member's checkouts with an order number and account email, two AYCD
Inbox mail tasks are run on the member's own AYCD key: one for the order
confirmation email, one for a follow-up (canceled / shipped / delivered). An email
only counts when it names the checkout's order number, so another order's email
on the same account is never mistaken for this one. Nothing on the checkout is
changed; the results are just shown to the admin.

Ported from the AYCD scraper in proxy_webhook. AYCD runs the tasks in the
member's AYCD Inbox app, so that app has to be open while the check runs.
"""
import json
import re
import secrets
import threading
import time
from collections import deque
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import db

BASE_URL = "https://inbox-api.aycd.io"
# A search that finds its email comes back in seconds; one with nothing to find runs until its timeout and
# holds its slot the whole time. AYCD Inbox has to open a connection to each mail account it searches, and with
# many accounts at once that can take a while, so a search can time out before it reaches an email that's there.
# So the check runs twice: a quick first pass over everything, then a slow, patient second pass over only the
# checkouts the first pass found nothing for.
FIRST_PASS = {"timeout": 60, "active": 20}
SECOND_PASS = {"timeout": 180, "active": 8}     # 180s is what proxy_webhook settled on
DEADLINE_GRACE = 60        # extra wait for AYCD to report a task done before we give up on it
POLL_SECONDS = 1.0         # AYCD's docs allow up to 60 completed-task polls a minute
MAX_RUNNING_JOBS = 3       # checks running at once across all members
# How far before the checkout to look for its emails. The checkout time is when the webhook was posted, which
# can be after the email came (and a checkout the bot caught up on later, or added by hand, is later still).
# With the order number in the subject (Target) nothing else can match, so look back a whole day.
LOOKBACK_BY_ORDER_NUMBER = 86400
LOOKBACK_LOOSE = 1800

ORDER_NUMBER_RE = re.compile(r"\d{8,}")
FOLLOWUP_WORDS = ("cancel", "ship", "arriv", "deliver")
FOLLOWUP_STATUS = (
    (re.compile(r"cancel", re.I), "canceled"),
    (re.compile(r"arrived|delivered", re.I), "delivered"),
    (re.compile(r"ship|on the way|on its way|out for delivery|arriv|tracking", re.I), "shipped"),
)
TOTAL_RE = re.compile(r"\$\s?([\d,]+\.\d{2})")

ORDER_NUMBER_ELEMENT = {"name": "orderNumber", "target": "body",
                        "regex": r"(?i)\border\s*(?:number|#|id)?\s*[:#]?\s*\d[\d-]{5,39}"}
ORDER_ELEMENTS = [
    {"name": "subject", "target": "subject"},
    {"name": "from", "target": "from"},
    ORDER_NUMBER_ELEMENT,
    {"name": "orderTotal", "target": "body",
     "regex": r"(?i)\b(?:order\s+total|total|grand\s+total|amount\s+paid)\b[^\d$]{0,80}(\$?\s?\d[\d,]*(?:\.\d{2})?)"},
]
FOLLOWUP_ELEMENTS = [
    {"name": "subject", "target": "subject"},
    {"name": "from", "target": "from"},
    ORDER_NUMBER_ELEMENT,
    {"name": "trackingNumber", "target": "body",
     "regex": r"(?i)\btracking\s*(?:number|#|no\.?)?\s*[:#]?\s*[A-Z0-9]*\d[A-Z0-9]{7,39}|\b1Z[0-9A-Z]{16}\b"},
]

# What each outcome means, and whether the order stuck.
OUTCOMES = {
    "confirmed": "Confirmed", "shipped": "Shipped", "delivered": "Delivered", "canceled": "Canceled",
    "not_found": "No email found", "mismatch": "Other order's email", "error": "Error",
}
STUCK = {"confirmed", "shipped", "delivered"}


class AycdError(Exception):
    pass


def aycd(key: str, method: str, path: str, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = Request(BASE_URL + path, data=data, method=method, headers={
        "Authorization": f"Token {key}", "Content-Type": "application/json", "User-Agent": "BUFFSQUAD-ACO"})
    try:
        with urlopen(req, timeout=30) as res:
            raw = res.read()
    except HTTPError as e:
        if e.code == 401:
            raise AycdError("AYCD didn't accept the member's key, or their AYCD Inbox app isn't open.") from None
        if e.code == 429:
            raise AycdError("AYCD is rate limiting this key. Wait a minute and try again.") from None
        body = e.read()[:200].decode(errors="replace")
        raise AycdError(f"AYCD returned an error ({e.code}){': ' + body if body else ''}") from None
    except (URLError, OSError):
        raise AycdError("Couldn't reach AYCD. Try again in a moment.") from None
    return json.loads(raw) if raw else {}


# Matching an email to a checkout

def numbers(text) -> set[str]:
    return set(ORDER_NUMBER_RE.findall(str(text or "")))


def site_word(retailer: str) -> str:
    """The word that's in the retailer's sender address."""
    r = (retailer or "").lower().strip()
    if r in ("", "unknown"):
        # The bot couldn't tell the store. No sender to look for (a "from includes unknown" filter never matches).
        return ""
    if "target" in r:
        return "target"
    if "pokemon" in r or "pokémon" in r:
        return "pokemon"
    return re.sub(r"[^a-z0-9.-]+", "", r)[:80]


def mail_filters(c: dict, kind: str) -> list[dict]:
    digits = next(iter(sorted(numbers(c["order"]), key=len, reverse=True)), "")
    if order_in_subject(c):
        # Target puts the order number in every subject, so require it: AYCD can't hand back another order.
        # Only "includes" filters: with "excludes" ones added, AYCD never found Target confirmations that were
        # in the mailbox. So the order search takes any email with the number, and outcome() reads its subject.
        filters = [{"target": "subject", "comparator": "includes", "value": digits}]
        if site_word(c["retailer"]):
            filters.append({"target": "from", "comparator": "includes", "value": site_word(c["retailer"])})
        if kind == "followup":
            first, *rest = FOLLOWUP_WORDS
            filters.append({"target": "subject", "comparator": "includes", "value": first,
                            "orValues": [*rest, "on the way", "tracking"]})
        return filters
    # Elsewhere: a loose subject match from the retailer; the order number in the body is checked afterwards.
    if kind == "followup":
        first, words = "cancel", ["shipped", "shipment", "tracking", "delivered", "delivery", "on the way", "arriving"]
    else:
        first, words = "order", ["confirmation", "confirmed", "received", "placed", "thank you", "purchase"]
    if c["order"]:
        words = [c["order"][:80], *words]
    filters = [{"target": "subject", "comparator": "includes", "value": first, "orValues": words[:10], "optional": True}]
    if site_word(c["retailer"]):
        filters.append({"target": "from", "comparator": "includes", "value": site_word(c["retailer"])})
    return filters


def order_in_subject(c: dict) -> bool:
    """Search by the order number in the subject: Target, and any checkout whose store the bot couldn't tell
    (the order number alone is specific enough, and a loose search with no sender would match anything)."""
    return bool(numbers(c["order"])) and site_word(c["retailer"]) in ("target", "")


def build_task(c: dict, kind: str, group: str, template_id: str, timeout: int) -> dict:
    lookback = LOOKBACK_BY_ORDER_NUMBER if order_in_subject(c) else LOOKBACK_LOOSE
    task = {
        "group": group, "timeout": timeout, "email": c["email"],
        "receivedAt": max(0, c.get("placedAt", c["createdAt"]) - lookback),
        "mailFilters": mail_filters(c, kind),
        "mailElements": FOLLOWUP_ELEMENTS if kind == "followup" else ORDER_ELEMENTS,
    }
    if template_id:
        task["templateId"] = template_id
    return task


def results_of(resp) -> dict:
    r = resp.get("results") if isinstance(resp, dict) else None
    return r if isinstance(r, dict) else {}


def succeeded(resp) -> bool:
    return isinstance(resp, dict) and str(resp.get("status") or "").lower() in ("success", "ok")


def email_names_order(resp, order: str):
    """True if the email names this order, False if it names a different one, None if it names none."""
    r = results_of(resp)
    text = f"{r.get('subject') or ''}\n{r.get('orderNumber') or ''}"
    found, own = numbers(text), numbers(order)
    if own:
        return bool(found & own) if found else None
    if order and order.lower() in text.lower():
        return True
    return False if found else None


def first_line(value) -> str:
    return next((line.strip() for line in str(value or "").splitlines() if line.strip()), "")


def error_text(resp) -> str:
    for source in (results_of(resp), resp if isinstance(resp, dict) else {}):
        for k in ("errorMessage", "error", "message"):
            if source.get(k):
                return str(source[k])[:200]
    return ""


def timed_out(resp) -> bool:
    """AYCD ran the search and nothing matched before the timeout: no email, not an error."""
    status = str((resp or {}).get("status") or "").lower()
    return status == "timeout" or (status == "error" and "timed out" in error_text(resp).lower())


def outcome(c: dict, order_resp, follow_resp, create_errors: list[str]) -> dict:
    """What the emails say about one checkout."""
    order_ok = succeeded(order_resp) and email_names_order(order_resp, c["order"]) is True
    follow_ok = succeeded(follow_resp) and email_names_order(follow_resp, c["order"]) is True
    res = {"outcome": "not_found", "subject": "", "emailTotal": None, "tracking": "", "error": ""}
    if order_ok:
        r = results_of(order_resp)
        res.update(outcome="confirmed", subject=first_line(r.get("subject")))
        total = TOTAL_RE.search(first_line(r.get("orderTotal")))
        res["emailTotal"] = float(total.group(1).replace(",", "")) if total else None
    # The order search can hand back a follow-up (shipped, canceled) too; read it as one. The strongest
    # news wins: canceled over delivered over shipped.
    rank = {"confirmed": 0, "shipped": 1, "delivered": 2, "canceled": 3}
    for resp in (order_resp, follow_resp):
        if not (succeeded(resp) and email_names_order(resp, c["order"]) is True):
            continue
        r = results_of(resp)
        subject = first_line(r.get("subject"))
        status = next((s for pattern, s in FOLLOWUP_STATUS if pattern.search(subject)), None)
        if status and rank[status] > rank.get(res["outcome"], -1):
            res.update(outcome=status, subject=subject)
            tracking = first_line(r.get("trackingNumber"))
            m = re.search(r"\b1Z[0-9A-Z]{16}\b|[A-Z0-9]*\d[A-Z0-9]{7,39}\s*$", tracking, re.I)
            if m and m.group(0).strip() not in numbers(c["order"]):
                res["tracking"] = m.group(0).strip().upper()
    if res["outcome"] != "not_found":
        return res
    if any(succeeded(x) and email_names_order(x, c["order"]) is False for x in (order_resp, follow_resp)):
        other = next(x for x in (order_resp, follow_resp) if succeeded(x) and email_names_order(x, c["order"]) is False)
        return {**res, "outcome": "mismatch", "subject": first_line(results_of(other).get("subject"))}
    real = [x for x in (order_resp, follow_resp) if not timed_out(x)]
    errors = create_errors + [e for e in map(error_text, real) if e]
    statuses = {str((x or {}).get("status") or "").lower() for x in real}
    if create_errors or "error" in statuses:
        return {**res, "outcome": "error", "error": "; ".join(dict.fromkeys(errors)) or "AYCD couldn't run the search."}
    return res


# Running a check

JOBS: dict[int, dict] = {}   # member id -> the check running (or last run since the server started)
LOCK = threading.Lock()


def kv_key(user_id: int) -> str:
    return f"order_check:{user_id}"


def running(user_id: int) -> bool:
    job = JOBS.get(user_id)
    return bool(job and job["status"] == "running")


def summary(job: dict) -> dict:
    counts = {k: 0 for k in OUTCOMES}
    for r in job["results"]:
        if r.get("outcome"):
            counts[r["outcome"]] += 1
    done = sum(counts.values())
    stuck = sum(counts[k] for k in STUCK)
    return {"counts": counts, "done": done, "stuck": stuck,
            "rate": round(stuck / done * 100) if done else None}


def public(job: dict | None) -> dict | None:
    if not job:
        return None
    # serverNow lets the page run its timer off the server's clock, not the viewer's.
    return {k: v for k, v in job.items() if not k.startswith("_")} | {"summary": summary(job), "serverNow": int(time.time() * 1000)}


def get(conn, user_id: int) -> dict | None:
    """The check running for a member, or the last one that finished."""
    job = JOBS.get(user_id)
    if job:
        return public(job)
    return public(db.kv_get(conn, kv_key(user_id)))


def kinds(options: dict) -> tuple[str, ...]:
    """The searches per checkout: the confirmation, and unless it's a quick check, any cancel or shipping email."""
    return ("order",) if options.get("quick") else ("order", "followup")


def start(key: str, user_id: int, checkouts: list[dict], options: dict, by: str) -> dict:
    with LOCK:
        if running(user_id):
            raise AycdError("A check is already running for this member.")
        if sum(1 for j in JOBS.values() if j["status"] == "running") >= MAX_RUNNING_JOBS:
            raise AycdError("Too many checks are running. Wait for one to finish.")
        job = {
            "id": secrets.token_hex(4), "userId": user_id, "status": "running", "message": "Connecting to AYCD…",
            "options": options, "by": by, "startedAt": int(time.time() * 1000), "finishedAt": None,
            "tasks": {"total": len(checkouts) * len(kinds(options)), "settled": 0, "active": 0},
            "results": [{**c, "outcome": None} for c in checkouts],
            "_stop": threading.Event(),
        }
        JOBS[user_id] = job
    threading.Thread(target=_run, args=(job, key), daemon=True, name=f"order-check-{user_id}").start()
    return public(job)


def stop(user_id: int) -> bool:
    job = JOBS.get(user_id)
    if not job or job["status"] != "running":
        return False
    job["_stop"].set()
    job["message"] = "Stopping…"
    return True


def _run(job: dict, key: str) -> None:
    try:
        aycd(key, "GET", "/api/v1/user/verify")
        everything = list(range(len(job["results"])))
        _scrape(job, key, everything, FIRST_PASS, "Searching inbox")
        retry = [i for i in everything if job["results"][i].get("outcome") == "not_found"]
        if retry and not job["_stop"].is_set():
            job["secondLook"] = len(retry)
            job["tasks"]["total"] += len(retry) * len(kinds(job["options"]))
            _scrape(job, key, retry, SECOND_PASS, f"Second, slower look at {len(retry)} with no email")
        status, message = ("stopped", "Stopped.") if job["_stop"].is_set() else ("done", "Done.")
    except AycdError as e:
        status, message = "failed", str(e)
    except Exception as e:  # noqa: BLE001 - shown to the admin rather than lost in a thread
        status, message = "failed", f"The check stopped on an error: {e}"
    # Save the finished run first and flip the status last, so anyone who sees it finished
    # also sees its finish time and finds it saved.
    job["finishedAt"], job["message"] = int(time.time() * 1000), message
    try:
        with db.transaction() as conn:
            db.kv_set(conn, kv_key(job["userId"]), public(job) | {"status": status})
    except Exception:  # noqa: BLE001
        pass
    job["status"] = status


def _scrape(job: dict, key: str, indices: list[int], settings: dict, label: str) -> None:
    group = quote(f"buffsquad-check-{job['id']}", safe="")
    template_id = job["options"].get("templateId") or ""
    timeout, max_active = settings["timeout"], settings["active"]
    wanted = kinds(job["options"])
    queue = deque((i, kind) for i in indices for kind in wanted)
    began: dict[int, float] = {}          # result index -> when its first search in this pass started
    responses: dict[int, dict] = {}       # result index -> {"order": resp, "followup": resp}
    create_errors: dict[int, list] = {}
    pending: dict[str, tuple[int, str, float]] = {}   # task id -> (index, kind, deadline)
    tasks = job["tasks"]

    def settle(i: int, kind: str, resp) -> None:
        responses.setdefault(i, {})[kind] = resp
        tasks["settled"] += 1
        got = responses[i]
        if all(k in got for k in wanted):
            c = job["results"][i]
            c.update(outcome(c, got["order"], got.get("followup"), create_errors.get(i, [])))
            c["secs"] = c.get("secs", 0) + round(time.monotonic() - began.get(i, time.monotonic()))

    while queue or pending:
        if job["_stop"].is_set():
            return
        while queue and len(pending) < max_active:
            i, kind = queue.popleft()
            began.setdefault(i, time.monotonic())
            try:
                task = build_task(job["results"][i], kind, group, template_id, timeout)
                resp = aycd(key, "POST", "/api/v1/tasks/mail/create", task)
                task_id = resp.get("id") if isinstance(resp, dict) else None
                if not task_id:
                    raise AycdError(f"AYCD didn't start the search: {str(resp)[:150]}")
                pending[str(task_id)] = (i, kind, time.monotonic() + timeout + DEADLINE_GRACE)
            except AycdError as e:
                if "didn't accept" in str(e) or "rate limiting" in str(e):
                    raise
                create_errors.setdefault(i, []).append(str(e))
                settle(i, kind, {"status": "error"})
        tasks["active"] = len(pending)
        job["message"] = f"{label}: {tasks['settled']} of {tasks['total']} searches done"
        if not pending:
            continue
        time.sleep(POLL_SECONDS)
        try:
            done = aycd(key, "GET", f"/api/v1/tasks/completed?group={group}")
        except AycdError as e:
            job["message"] = f"{e} Retrying..."
            continue
        done = done.get("tasks", []) if isinstance(done, dict) else done if isinstance(done, list) else []
        for task in done:
            task_id = str(task.get("id")) if isinstance(task, dict) else ""
            if task_id in pending:
                i, kind, _ = pending.pop(task_id)
                settle(i, kind, task)
        now = time.monotonic()
        for task_id in [t for t, (_, _, deadline) in pending.items() if deadline <= now]:
            i, kind, _ = pending.pop(task_id)
            settle(i, kind, {"status": "timeout"})
    tasks["active"] = 0
