"""BUFFSQUAD checkout bot.

Watches the checkout channel(s) for success webhooks from the AIO bots, logs each
checkout against the member whose profile it ran on (so it shows up on their
dashboard and the admin panel), and DMs that member.

It reposts each checkout to the success feed channel, if one is set, with just the
item, store, profile name and a tag of the member: none of the order number,
proxies or other details the AIO bots put in their webhooks.

It also sends DMs the admin panel queues (an unmatched checkout assigned to a
member, or "Resend DM"), posts drop recaps sent from the admin panel, DMs fee
invoices and their reminders, and catches up on messages posted while it was offline.

The channels to watch are set on the admin panel's Channels tab. The bot re-reads
that list every few seconds, so adding or removing one needs no restart.

Run: python bot.py   (needs DISCORD_BOT_TOKEN in .env)
"""
import asyncio
import logging

import discord

import config
import db
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


def invoice_embed(conn, inv, reminder: bool = False) -> discord.Embed:
    number = db.invoice_number(inv["id"])
    rows = db.invoice_checkouts(conn, inv["id"])
    amount = sum(r["fee"] for r in rows)
    embed = discord.Embed(
        title=f"Reminder: ACO fee invoice {number} is still open" if reminder else f"ACO fee invoice {number}",
        description=f"You owe **${amount:,.2f}** for {len(rows)} checkout{'s' if len(rows) != 1 else ''}.", color=0xFCA47C)
    lines = []
    for r in rows:
        qty = f" ×{r['qty']}" if r["qty"] > 1 else ""
        lines.append(f"{r['product'] or 'Item'}{qty} · ${r['fee']:,.2f}")
    shown = "\n".join(lines[:15]) + (f"\n…and {len(lines) - 15} more" if len(lines) > 15 else "")
    embed.add_field(name="Checkouts", value=shown[:1024] or "—", inline=False)
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
    # Counts and members only: no products' prices, order numbers or profiles.
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


async def send_pending_recaps() -> None:
    """Posts the drop recaps admins send from the admin panel."""
    with db.transaction() as conn:
        pending = [p for p in db.kv_get(conn, "recaps", []) if p["status"] == "pending"]
    for post in pending:
        st = feed_status(int(post["channel"]))
        if not st["ok"]:
            set_recap(post["id"], status="failed", error=st["problem"])
            continue
        try:
            # The tags in the embed show names without pinging anyone.
            await client.get_channel(int(post["channel"])).send(embed=recap_embed(post), allowed_mentions=discord.AllowedMentions.none())
            set_recap(post["id"], status="sent", channelName=st["name"])
            log.info("Posted drop recap %s to #%s", post["title"], st["name"])
        except discord.HTTPException as e:
            set_recap(post["id"], status="failed", error=str(e)[:200])


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
