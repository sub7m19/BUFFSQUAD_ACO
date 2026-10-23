"""BUFFSQUAD checkout bot.

Watches the checkout channel(s) for success webhooks from the AIO bots, logs each
checkout against the member whose profile it ran on (so it shows up on their
dashboard and the admin panel), and DMs that member.

It also sends DMs the admin panel queues (an unmatched checkout assigned to a
member, or "Resend DM"), and catches up on messages posted while it was offline.

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


async def heartbeat() -> None:
    """Lets the admin panel show the bot as online, follows channel changes, and sends queued DMs."""
    while not client.is_closed():
        try:
            await refresh_channels()
            with db.transaction() as conn:
                db.kv_set(conn, "bot_status", {"at": db.now() * 1000, "name": str(client.user), "channels": len(watched)})
                conn.execute("DELETE FROM kv WHERE key = 'bot_error'")  # signed in fine, so any old sign-in error is gone
            await send_pending_dms()
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
