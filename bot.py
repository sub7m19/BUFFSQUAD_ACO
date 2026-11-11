"""BUFFSQUAD checkout bot.

Watches the checkout channel(s) for success webhooks from the AIO bots, logs each
checkout against the member whose profile it ran on (so it shows up on their
dashboard and the admin panel), and DMs that member.

It reposts each checkout to the success feed channel, if one is set, with just the
item, store, profile name and a tag of the member: none of the order number,
proxies or other details the AIO bots put in their webhooks.

It also sends DMs the admin panel queues (an unmatched checkout assigned to a
member, or "Resend DM"), posts drop recaps sent from the admin panel, DMs each
member a summary of their hits from a drop ("DM everyone their hits"), DMs fee
invoices and their reminders, and catches up on messages posted while it was offline.

The channels to watch are set on the admin panel's Channels tab. The bot re-reads
that list every few seconds, so adding or removing one needs no restart.

Run: python bot.py   (needs DISCORD_BOT_TOKEN in .env)
"""
import asyncio
import io
import logging

import discord

import config
import db
import recap_card
from checkout_parser import parse_message

log = logging.getLogger("buffsquad.bot")

intents = discord.Intents.default()
# Privileged: turn on "Message Content Intent" for the bot in the Discord developer portal,
# or webhook embeds arrive empty.
intents.message_content = True
client = discord.Client(intents=intents)

CATCH_UP_LIMIT = 200
POLL_SECONDS = 10

# Channel ids being watched right now, refreshed from the database by heartbeat().
watched: set[int] = set()


def message_dict(message: discord.Message) -> dict:
    return {
        "content": message.content or "",
        "author": message.author.name if message.author else "",
        "embeds": [{
            "title": e.title or "",
            "description": e.description or "",
            "author": e.author.name if e.author and e.author.name else "",
            "footer": e.footer.text if e.footer and e.footer.text else "",
            "fields": [{"name": f.name or "", "value": f.value or ""} for f in e.fields],
            # The product photo, for the drop recap card.
            "thumbnail": (e.thumbnail.url if e.thumbnail else None) or "",
            "image": (e.image.url if e.image else None) or "",
        } for e in message.embeds],
    }


def handle(message: discord.Message) -> int:
    """Logs every checkout in the message. Returns how many were new."""
    if message.channel.id not in watched or message.author == client.user:
        return 0
    new = 0
    with db.transaction() as conn:
        for i, parsed in enumerate(parse_message(message_dict(message))):
            checkout_id = db.record_checkout(conn, parsed, message_key=f"{message.id}:{i}", channel_id=str(message.channel.id))
            if checkout_id:
                new += 1
                who = "unmatched" if not parsed["profile"] else parsed["profile"]
                log.info("%s %s · %s · profile %s", parsed["status"], parsed["product"] or "?", parsed["retailer"] or "?", who)
        db.kv_set(conn, f"last_message:{message.channel.id}", str(message.id))
    return new


def dm_embed(c) -> discord.Embed:
    embed = discord.Embed(title="You got a checkout! 🎉", description=c["product"] or "New order", color=0x23CED9)
    embed.add_field(name="Retailer", value=c["retailer"] or "—")
    embed.add_field(name="Qty", value=str(c["qty"]))
    if c["price"]:
        embed.add_field(name="Price", value=f"${c['price']:,.2f}")
    if c["order_no"]:
        embed.add_field(name="Order", value=f"#{c['order_no']}")
    if c["fee"]:
        embed.add_field(name="ACO fee", value=f"${c['fee']:,.2f}")
    url = f"{config.PUBLIC_URL}/dashboard#checkouts"
    if url.startswith("https://"):
        embed.url = url
    embed.set_footer(text="BUFFSQUAD ACO · Full details on your dashboard")
    return embed


async def send_pending_dms() -> None:
    with db.transaction() as conn:
        pending = conn.execute(
            """SELECT checkouts.*, users.discord_id FROM checkouts JOIN users ON users.id = checkouts.user_id
               WHERE checkouts.dm_status = 'pending' ORDER BY checkouts.id LIMIT 20""").fetchall()
    for c in pending:
        status, error = "sent", ""
        try:
            if not c["discord_id"].isdigit():
                raise ValueError("not a Discord account (dev sign-in)")
            user = client.get_user(int(c["discord_id"])) or await client.fetch_user(int(c["discord_id"]))
            await user.send(embed=dm_embed(c))
        except discord.Forbidden:
            status, error = "failed", "Member has DMs closed or doesn't share a server with the bot"
        except (discord.HTTPException, ValueError) as e:
            status, error = "failed", str(e)[:200]
        with db.transaction() as conn:
            conn.execute("UPDATE checkouts SET dm_status = ?, dm_error = ? WHERE id = ?", (status, error, c["id"]))
        log.info("DM %s for checkout #%s%s", status, c["id"], f": {error}" if error else "")


def feed_embed(c) -> discord.Embed:
    # Only what's safe for everyone in the server to see. No order number, price, proxy, email or address.
    embed = discord.Embed(title="Successful checkout", description=c["product"] or "New order", color=0x23CED9)
    embed.add_field(name="Store", value=c["retailer"] or "—")
    if c["qty"] > 1:
        embed.add_field(name="Qty", value=str(c["qty"]))
    embed.add_field(name="Profile", value=c["profile_name"] or "—")
    embed.set_footer(text="BUFFSQUAD ACO")
    return embed


async def send_pending_feed() -> None:
    with db.transaction() as conn:
        feed = db.feed_channel(conn)
        if not feed:
            # The feed was turned off: drop what was waiting instead of posting it if it's turned on again.
            conn.execute("UPDATE checkouts SET feed_status = 'none' WHERE feed_status = 'pending'")
            return
        pending = conn.execute(
            """SELECT checkouts.*, users.discord_id FROM checkouts LEFT JOIN users ON users.id = checkouts.user_id
               WHERE checkouts.feed_status = 'pending' ORDER BY checkouts.id LIMIT 20""").fetchall()
    channel = client.get_channel(int(feed["id"]))
    if not pending or channel is None:
        return
    for c in pending:
        member = c["discord_id"] if c["discord_id"] and c["discord_id"].isdigit() else None
        status = "sent"
        try:
            await channel.send(content=f"<@{member}> checked out!" if member else "Checkout! (profile not linked to a member yet)",
                               embed=feed_embed(c), allowed_mentions=discord.AllowedMentions(users=True, everyone=False, roles=False))
        except discord.HTTPException as e:
            status = "failed"
            log.warning("Feed post failed for checkout #%s: %s", c["id"], e)
        with db.transaction() as conn:
            conn.execute("UPDATE checkouts SET feed_status = ? WHERE id = ?", (status, c["id"]))


EMBED_DESCRIPTION = 4096  # Discord's limit on an embed's description


def fit_lines(head: str, lines: list[str], more: str, limit: int = EMBED_DESCRIPTION) -> str:
    """head, then as many whole lines as fit under Discord's limit. If some don't fit,
    ends with `more` (formatted with how many were left off) instead of cutting a line in half."""
    text = head
    for i, line in enumerate(lines):
        tail = "" if i == len(lines) - 1 else "\n" + more.format(n=len(lines) - i - 1)
        if len(text) + 1 + len(line) + len(tail) > limit:
            return text + "\n" + more.format(n=len(lines) - i)
        text += "\n" + line
    return text


def invoice_embed(conn, inv, reminder: bool = False) -> discord.Embed:
    number = db.invoice_number(inv["id"])
    rows = db.invoice_checkouts(conn, inv["id"])
    amount = round(sum(r["fee"] for r in rows), 2)
    explain = db.fee_explainer(conn)
    # The same item at the same price and fee reads as one entry with its equation
    # ("$63.98 × 5% = $3.20 × 14 checkouts = $44.80"), so a big drop's invoice fits in one message.
    groups: dict[tuple, list] = {}
    for r in rows:
        groups.setdefault((r["product"] or "Item", r["qty"], r["price"], r["fee"]), []).append(r)
    lines = []
    for (product, qty, _, fee), same in groups.items():
        math = explain(same[0])
        # The equation in code style so it reads as math; a fee with no equation (set by hand, or
        # one today's settings no longer give) shows just its amount.
        eq = f"`{math['math']}`" if math["terms"] and math["adds"] else f"**${fee:,.2f}** fee"
        if len(same) > 1:
            eq += f" × {len(same)} checkouts = **${fee * len(same):,.2f}**"
        lines.append(f"**{product}**{f' · Quantity {qty}' if qty > 1 else ''}\n↳ {eq}")
    # What the fees were charged by, from the ACO fees settings, with each one's subtotal.
    bases = db.fee_bases(rows, explain, member=True)
    how = "\n".join(f"• {b['basis']} · {b['checkouts']} checkout{'s' if b['checkouts'] != 1 else ''} · ${b['total']:,.2f}" for b in bases)
    head = (f"You owe **${amount:,.2f}** for {len(rows)} checkout{'s' if len(rows) != 1 else ''}.\n\n"
            f"**How the fee is charged**\n{how}\n\n**Checkouts**")
    embed = discord.Embed(
        title=f"Reminder: ACO fee invoice {number} is still open" if reminder else f"ACO fee invoice {number}",
        description=fit_lines(head, lines, "…and {n} more on your dashboard's Billing page"), color=0xFCA47C)
    # The subtotals above add up to what they owe.
    total = (" + ".join(f"${b['total']:,.2f}" for b in bases) + f" = **${amount:,.2f}**") if len(bases) > 1 else f"**${amount:,.2f}**"
    embed.add_field(name="Total", value=total[:1024], inline=False)
    pay = db.pay_settings(conn)
    how = [f"**{name}:** {pay[k]}" for k, name in db.PAY_METHODS.items() if pay[k]]
    if how:
        embed.add_field(name="How to pay", value="\n".join(how) + f"\nPut **{number}** in the payment note."
                        + (f"\n{pay['note']}" if pay["note"] else ""), inline=False)
    url = f"{config.PUBLIC_URL}/dashboard#billing"
    if url.startswith("https://"):
        embed.url = url
    embed.set_footer(text="BUFFSQUAD ACO · Your invoices are on your dashboard's Billing page")
    return embed


async def send_pending_invoices() -> None:
    """Sends invoices admins create on the Fees tab, and reminders for ones still open."""
    with db.transaction() as conn:
        days = db.pay_settings(conn)["remindDays"]
        due = conn.execute(
            """SELECT invoices.*, users.discord_id, 0 AS reminder FROM invoices JOIN users ON users.id = invoices.user_id
               WHERE invoices.dm_status = 'pending' AND invoices.status != 'void'
               UNION ALL
               SELECT invoices.*, users.discord_id, 1 AS reminder FROM invoices JOIN users ON users.id = invoices.user_id
               WHERE ? > 0 AND invoices.status = 'open' AND invoices.dm_status = 'sent'
                 AND COALESCE(invoices.reminded_at, invoices.created_at) < ?
               LIMIT 20""", (days, db.now() - days * 86400)).fetchall()
        embeds = {(inv["id"], inv["reminder"]): invoice_embed(conn, inv, bool(inv["reminder"])) for inv in due}
    for inv in due:
        status, error = "sent", ""
        try:
            if not inv["discord_id"].isdigit():
                raise ValueError("not a Discord account (dev sign-in)")
            user = client.get_user(int(inv["discord_id"])) or await client.fetch_user(int(inv["discord_id"]))
            await user.send(embed=embeds[(inv["id"], inv["reminder"])])
        except discord.Forbidden:
            status, error = "failed", "Member has DMs closed or doesn't share a server with the bot"
        except (discord.HTTPException, ValueError) as e:
            status, error = "failed", str(e)[:200]
        with db.transaction() as conn:
            if inv["reminder"]:
                # Tried either way, so a member with DMs closed isn't retried every 10 seconds.
                conn.execute("UPDATE invoices SET reminded_at = ? WHERE id = ?", (db.now(), inv["id"]))
            else:
                conn.execute("UPDATE invoices SET dm_status = ?, dm_error = ? WHERE id = ?", (status, error, inv["id"]))
        log.info("Invoice %s %s %s%s", db.invoice_number(inv["id"]), "reminder" if inv["reminder"] else "DM", status,
                 f": {error}" if error else "")


MEDALS = ["🥇", "🥈", "🥉"]


def recap_embed(post) -> discord.Embed:
    # Counts and members only: no order numbers or profiles. Spend and products are on the share card under it.
    s = post["stats"]
    start, end = s["from"] // 1000, s["to"] // 1000
    embed = discord.Embed(title=f"🏆 {post['title']}", color=0x23CED9,
                          description=f"<t:{start}:f> – <t:{end}:t>")
    embed.add_field(name="Successful checkouts", value=f"**{s['checkouts']:,}**")
    embed.add_field(name="Items", value=f"**{s['items']:,}**")
    embed.add_field(name="Members", value=f"**{s['members']:,}**")
    lines = []
    for i, m in enumerate(s["top"]):
        place = MEDALS[i] if i < len(MEDALS) else f"`{i + 1}.`"
        who = f"<@{m['discordId']}>" if m["discordId"] else m["name"]
        lines.append(f"{place} {who} · **{m['items']}** item{'s' if m['items'] != 1 else ''}"
                     f" ({m['checkouts']} checkout{'s' if m['checkouts'] != 1 else ''})")
    if lines:
        embed.add_field(name="Top checkouts", value="\n".join(lines)[:1024], inline=False)
    embed.set_footer(text="BUFFSQUAD ACO")
    return embed


def set_recap(post_id: str, **changes) -> None:
    with db.transaction() as conn:
        recaps = db.kv_get(conn, "recaps", [])
        for p in recaps:
            if p["id"] == post_id:
                p.update(changes)
        db.kv_set(conn, "recaps", recaps)


# Discord turns away bot uploads over 10 MB; a GIF that big goes up as the still instead.
UPLOAD_LIMIT = 10 * 1024 * 1024 - 64 * 1024


def recap_card_file(post):
    """The drop's share card as a file to attach, or None for a recap posted without one.
    Rendered when it's posted (several seconds for a GIF), so it runs off the event loop."""
    kind = post.get("card", "none")
    if kind == "none":
        return None
    s = post["stats"]
    with db.transaction() as conn:
        stats = db.recap_card_stats(conn, s["from"] // 1000, s["to"] // 1000, post.get("tz", 0))
    title = post["title"] if post["title"] != "Drop recap" else None
    data = recap_card.render_gif(stats, theme=post.get("theme", "glare"), title=title).getvalue() if kind == "gif" else b""
    if not data or len(data) > UPLOAD_LIMIT:
        kind, data = "png", recap_card.render_png(stats, theme=post.get("theme", "glare"), title=title).getvalue()
    return discord.File(io.BytesIO(data), filename=f"drop-recap.{kind}")


async def send_pending_recaps() -> None:
    """Posts the drop recaps admins send from the admin panel."""
    with db.transaction() as conn:
        pending = [p for p in db.kv_get(conn, "recaps", []) if p["status"] == "pending"]
    for post in pending:
        st = feed_status(int(post["channel"]))
        if not st["ok"]:
            set_recap(post["id"], status="failed", error=st["problem"])
            continue
        embed = recap_embed(post)
        try:
            card = await asyncio.to_thread(recap_card_file, post)
        except Exception:
            # A card that won't draw doesn't hold up the recap: it posts without one.
            log.exception("Couldn't draw the share card for drop recap %s", post["title"])
            card = None
        if card:
            embed.set_image(url=f"attachment://{card.filename}")
        try:
            # The tags in the embed show names without pinging anyone.
            await client.get_channel(int(post["channel"])).send(embed=embed, file=card or discord.utils.MISSING,
                                                                allowed_mentions=discord.AllowedMentions.none())
            set_recap(post["id"], status="sent", channelName=st["name"])
            log.info("Posted drop recap %s to #%s", post["title"], st["name"])
        except discord.HTTPException as e:
            set_recap(post["id"], status="failed", error=str(e)[:200])


STORE_ICONS = {"Target": "🎯", "Pokemon Center": "⚡"}
STORE_NAMES = {"Pokemon Center": "Pokémon Center"}
FIELD_VALUE = 1024  # Discord's limit on an embed field's text
EMBED_TOTAL = 6000  # ...and on all of an embed's text together
HIT_GREEN = 0x7BE3A0  # the dashboard's green: a hit


def bot_avatar() -> str | None:
    """The bot's own profile picture, if one is set (not Discord's default), for the DM's header and footer."""
    user = client.user
    return user.avatar.url if user is not None and user.avatar else None


def hit_summary_embed(job, hits) -> discord.Embed:
    """One member's hits from a drop, laid out to read at a glance: totals side by side, then a
    section per store with each item, and the profile and email it hit on in small grey text.
    Only ever sent to that member, so their own email is fine here; it's behind a spoiler so a
    screenshot of the DM doesn't show it."""
    total = sum(c["qty"] for c in hits)
    stores: dict[str, list] = {}
    for c in hits:
        stores.setdefault(c["retailer"] or "Other", []).append(c)

    embed = discord.Embed(title=f"🎉 {job['title']}: you hit!", color=HIT_GREEN,
                          description=f"<t:{job['from']}:D> · <t:{job['from']}:t> – <t:{job['to']}:t>")
    embed.add_field(name="Items", value=f"**{total}**")
    embed.add_field(name="Checkouts", value=f"**{len(hits)}**")
    embed.add_field(name="Stores", value=f"**{len(stores)}**")

    # A section per store; each hit is its item line, then its profile and email as subtext,
    # with a blank line between hits. Kept within Discord's per-field and whole-embed limits.
    budget = EMBED_TOTAL - len(embed.title) - len(embed.description) - 200
    for i, (store, items) in enumerate(stores.items()):
        if i == 22:  # 3 total fields + 22 stores = Discord's 25-field limit
            break
        entries = []
        for c in items:
            details = " · ".join(x for x in (f"Quantity {c['qty']}" if c["qty"] > 1 else "", f"Size {c['size']}" if c["size"] else "") if x)
            who = " · ".join(x for x in (c["profile_name"], f"||{c['email']}||" if c["email"] else "") if x)
            entries.append(f"**{c['product'] or 'Item'}**" + (f"  ·  {details}" if details else "")
                           + (f"\n-# {who}" if who else "") + "\n")  # the trailing newline leaves a blank line between hits
        name = f"{STORE_ICONS.get(store, '🛒')} {STORE_NAMES.get(store, store)}"
        value = fit_lines("", entries, "-# …and {n} more on your dashboard", min(FIELD_VALUE, budget - len(name))).strip()
        budget -= len(name) + len(value)
        embed.add_field(name=name, value=value or "—", inline=False)
        if budget < 200:
            break

    url = f"{config.PUBLIC_URL}/dashboard#checkouts"
    if url.startswith("https://"):
        embed.url = url
    avatar = bot_avatar()
    embed.set_footer(text="BUFFSQUAD ACO · View it all on your dashboard", icon_url=avatar)
    if avatar:
        embed.set_thumbnail(url=avatar)
    return embed


def set_hit_dm(job_id: str, user_id: int, status: str, error: str) -> None:
    with db.transaction() as conn:
        jobs = db.kv_get(conn, "hit_dms", [])
        for job in jobs:
            if job["id"] == job_id:
                for m in job["members"]:
                    if m["userId"] == user_id:
                        m.update(status=status, error=error)
        db.kv_set(conn, "hit_dms", jobs)


async def send_pending_hit_dms(limit: int = 25) -> None:
    """DMs each member their hits from a drop, for the summaries admins send from the Drop recap window."""
    todo = []
    with db.transaction() as conn:
        for job in db.kv_get(conn, "hit_dms", []):
            for m in job["members"]:
                if m["status"] == "pending" and len(todo) < limit:
                    user = conn.execute("SELECT discord_id FROM users WHERE id = ?", (m["userId"],)).fetchone()
                    hits = db.member_hits(conn, m["userId"], job["from"], job["to"])
                    todo.append((job, m["userId"], user["discord_id"] if user else "", hits))
    for job, user_id, discord_id, hits in todo:
        status, error = "sent", ""
        try:
            if not hits:
                raise ValueError("No hits left in that time (canceled or deleted)")
            if not discord_id.isdigit():
                raise ValueError("not a Discord account (dev sign-in)")
            user = client.get_user(int(discord_id)) or await client.fetch_user(int(discord_id))
            await user.send(embed=hit_summary_embed(job, hits))
        except discord.Forbidden:
            status, error = "failed", "Member has DMs closed or doesn't share a server with the bot"
        except (discord.HTTPException, ValueError) as e:
            status, error = "failed", str(e)[:200]
        set_hit_dm(job["id"], user_id, status, error)
        log.info("Hit summary DM %s for member #%s (%s)%s", status, user_id, job["title"], f": {error}" if error else "")


def feed_status(channel_id: int) -> dict:
    """What the admin panel shows next to the feed channel: its name, and whether the bot can post there."""
    channel = client.get_channel(channel_id)
    if channel is None:
        return {"ok": False, "problem": "The bot isn't in this channel's server, or the ID is wrong"}
    perms = channel.permissions_for(channel.guild.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        return {"ok": False, "name": channel.name, "guild": channel.guild.name,
                "problem": "The bot needs View Channel, Send Messages and Embed Links here"}
    return {"ok": True, "name": channel.name, "guild": channel.guild.name}


def channel_status(channel_id: int) -> dict:
    """What the admin panel shows next to a channel: its name, and whether the bot can read it."""
    channel = client.get_channel(channel_id)
    if channel is None:
        return {"ok": False, "problem": "The bot isn't in this channel's server, or the ID is wrong"}
    perms = channel.permissions_for(channel.guild.me)
    if not (perms.view_channel and perms.read_message_history):
        return {"ok": False, "name": channel.name, "guild": channel.guild.name,
                "problem": "The bot needs View Channel and Read Message History here"}
    return {"ok": True, "name": channel.name, "guild": channel.guild.name}


async def catch_up(channel_ids) -> None:
    """Reads messages posted since the last one we saw in each channel. A channel
    seen for the first time starts from its newest message, so old checkouts
    aren't logged (and members aren't DMed about them)."""
    for channel_id in channel_ids:
        channel = client.get_channel(channel_id)
        if channel is None or not channel_status(channel_id)["ok"]:
            log.warning("Can't read channel %s. Is the bot in that server with Read Message History?", channel_id)
            continue
        with db.transaction() as conn:
            last = db.kv_get(conn, f"last_message:{channel_id}")
        if not last:
            newest = [m async for m in channel.history(limit=1)]
            with db.transaction() as conn:
                db.kv_set(conn, f"last_message:{channel_id}", str(newest[0].id) if newest else "0")
            log.info("Now watching #%s, starting from new messages", channel.name)
            continue
        new = 0
        async for message in channel.history(limit=CATCH_UP_LIMIT, after=discord.Object(id=int(last)), oldest_first=True):
            new += handle(message)
        if new:
            log.info("Caught up on %d checkout(s) in #%s", new, channel.name)


async def backfill_photos() -> None:
    """Fills in the product photo on checkouts logged before the bot saved photos, from
    their webhooks still in the channel, so the drop recap card shows them. Reads each
    channel's history once, 100 messages a request, across the span of those checkouts;
    once a pass finishes it doesn't run again."""
    with db.transaction() as conn:
        if db.kv_get(conn, "photo_backfill_v1"):
            return
        rows = conn.execute(
            """SELECT id, channel_id, message_key FROM checkouts
               WHERE image_url = '' AND result = 'hit' AND message_key LIKE '%:%' AND channel_id != ''""").fetchall()
    wanted: dict[int, dict[int, list[tuple[int, int]]]] = {}  # channel -> message id -> [(checkout id, index)]
    for r in rows:
        message_id, _, index = r["message_key"].partition(":")
        if message_id.isdigit() and index.isdigit() and str(r["channel_id"]).isdigit():
            wanted.setdefault(int(r["channel_id"]), {}).setdefault(int(message_id), []).append((r["id"], int(index)))
    filled = 0
    for channel_id, messages in wanted.items():
        channel = client.get_channel(channel_id)
        if channel is None or not channel_status(channel_id)["ok"]:
            log.warning("Can't read channel %s for old product photos; skipping it.", channel_id)
            continue
        try:
            async for message in channel.history(limit=None, after=discord.Object(id=min(messages) - 1),
                                                 before=discord.Object(id=max(messages) + 1), oldest_first=True):
                targets = messages.get(message.id)
                if not targets:
                    continue
                # The key's index counts the checkouts parsed from the message, the same as when it was logged.
                parsed = parse_message(message_dict(message))
                with db.transaction() as conn:
                    for checkout_id, index in targets:
                        image = parsed[index].get("image") if index < len(parsed) else ""
                        if image:
                            conn.execute("UPDATE checkouts SET image_url = ? WHERE id = ? AND image_url = ''", (image, checkout_id))
                            filled += 1
        except discord.HTTPException as e:
            log.warning("Stopped reading #%s for old product photos: %s", getattr(channel, "name", channel_id), e)
            return  # try the whole pass again next start
    with db.transaction() as conn:
        db.kv_set(conn, "photo_backfill_v1", db.now())
    log.info("Filled in product photos on %d older checkout(s)", filled)


async def refresh_channels() -> None:
    """Picks up channels added or removed on the admin panel, and reports each one's status."""
    with db.transaction() as conn:
        wanted = {int(c["id"]) for c in db.checkout_channels(conn)}
    added = wanted - watched
    for gone in watched - wanted:
        log.info("Stopped watching channel %s", gone)
    watched.clear()
    watched.update(wanted)
    if added:
        await catch_up(added)
    with db.transaction() as conn:
        db.kv_set(conn, "bot_channels", {str(c): channel_status(c) for c in wanted})
        feed = db.feed_channel(conn)
        db.kv_set(conn, "bot_feed_channel", {feed["id"]: feed_status(int(feed["id"]))} if feed else {})


async def heartbeat() -> None:
    """Lets the admin panel show the bot as online, follows channel changes, and sends queued DMs and feed posts."""
    while not client.is_closed():
        try:
            await refresh_channels()
            with db.transaction() as conn:
                db.kv_set(conn, "bot_status", {"at": db.now() * 1000, "name": str(client.user), "channels": len(watched)})
                conn.execute("DELETE FROM kv WHERE key = 'bot_error'")  # signed in fine, so any old sign-in error is gone
            await send_pending_dms()
            await send_pending_feed()
            await send_pending_recaps()
            await send_pending_hit_dms()
            await send_pending_invoices()
        except Exception:  # Keep the loop alive; the next tick retries.
            log.exception("Heartbeat failed")
        await asyncio.sleep(POLL_SECONDS)


@client.event
async def on_ready():
    log.info("Signed in as %s", client.user)
    if not getattr(client, "heartbeat_started", False):
        client.heartbeat_started = True
        client.loop.create_task(heartbeat())
        client.loop.create_task(run_backfill_photos())


async def run_backfill_photos() -> None:
    try:
        await backfill_photos()
    except Exception:  # A failed pass leaves the flag unset, so the next start tries again.
        log.exception("Filling in old product photos failed")


@client.event
async def on_message(message: discord.Message):
    if handle(message):
        await send_pending_dms()
        await send_pending_feed()


def main(own_logging: bool = True) -> None:
    """Runs until the bot stops. start.py sets up logging itself and passes own_logging=False."""
    if not config.DISCORD_BOT_TOKEN:
        raise SystemExit("Add DISCORD_BOT_TOKEN to .env (Discord developer portal > your app > Bot > Reset Token).")
    db.init()
    if own_logging:
        discord.utils.setup_logging(level=logging.INFO)
    client.run(config.DISCORD_BOT_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
