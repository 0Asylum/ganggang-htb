import re
import os
import fnmatch
import logging
from datetime import datetime, timedelta, timezone
import discord
import config
import database
import cache
import image_gen
import poller

logger = logging.getLogger(__name__)

ROLE_EVERYONE = 0
ROLE_ADMIN = 1
ROLE_OWNER = 2

# (name, usage, description, min_role)
COMMAND_HELP = [
    ("ganggang",     "!ganggang",                       "gang gang",                                   ROLE_EVERYONE),
    ("ping",         "!ping",                           "health check",                                ROLE_EVERYONE),
    ("version",      "!version",                        "show the bot's current version",             ROLE_EVERYONE),
    ("stats",        "!stats [profileID|name|@mention]", "show a member's HTB stats card (default: yourself, if claimed)", ROLE_EVERYONE),
    ("leaderboard",  "!leaderboard [points|bloods|season]", "top 10 leaderboard (default: points)",     ROLE_EVERYONE),
    ("help",         "!help",                           "show this message",                           ROLE_EVERYONE),
    ("syncqueue",    "!syncqueue",                      "show pending profile sync queue size",        ROLE_ADMIN),
    ("syncstatus",   "!syncstatus",                     "show profile sync worker status",             ROLE_ADMIN),
    ("dbstats",      "!dbstats",                        "show DB row counts",                          ROLE_ADMIN),
    ("setchannel",   "!setchannel <channelID|name>",    "set the channel pwn alerts are posted to (persisted to config.json)", ROLE_ADMIN),
    ("showstale",    "!showstale <duration>",           "list users with no activity in the given time (e.g. 30d, 6mo)", ROLE_ADMIN),
    ("claim",        "!claim <profileID|name>",         "request to link your Discord account to one HTB profile (1:1, permanent until an admin changes it)", ROLE_EVERYONE),
    ("preference",   "!preference <htb|discord>",       "choose which avatar shows for you (requires an approved claim)", ROLE_EVERYONE),
    ("tag",          "!tag <text>",                     f"set a short tag shown on your !stats card (requires an approved claim, max {image_gen.MAX_TAG_LENGTH} chars)", ROLE_EVERYONE),
    ("claimqueue",   "!claimqueue",                     "list pending claim requests",                 ROLE_ADMIN),
    ("approve",      "!approve <claimID>",              "approve a pending claim",                     ROLE_ADMIN),
    ("deny",         "!deny <claimID> [reason]",        "deny a pending claim",                        ROLE_ADMIN),
    ("addadmin",     "!addadmin <@mention|discordID>",      "grant admin role",                        ROLE_OWNER),
    ("removeadmin",  "!removeadmin <@mention|discordID>",   "revoke admin role",                       ROLE_OWNER),
    ("showadmins",   "!showadmins",                         "list current admins",                     ROLE_OWNER),
    ("testpwn",      "!testpwn <profileID|name> <machineID|name> <user|root> <no|global|team|season>",
                                                             "post a fake pwn card (testing)",          ROLE_OWNER),
    ("andor",        "!andor <user1> <user2> <machine|challenge|sherlock> <ID|name> [user|root]",
                                                             "compare two users' full-history solve dates (testing)", ROLE_OWNER),
    ("trimcache",    "!trimcache",                          "remove orphaned cached avatar files",     ROLE_OWNER),
    ("purgeuser",    "!purgeuser <profileID|name>",         "permanently delete a user and all their history",  ROLE_OWNER),
]

ROLE_LABELS = {
    ROLE_EVERYONE: None,
    ROLE_ADMIN: "Admin",
    ROLE_OWNER: "Owner",
}


def get_role(message):
    """Returns this message author's permission role. The configured owner_id
    always resolves to ROLE_OWNER regardless of their DB row.
    """
    if message.author.id == config.get().owner_id:
        return ROLE_OWNER
    return database.get().get_discord_role(message.author.id)


def _validate_profile_id(raw):
    """Parses the first whitespace-separated token of `raw` as an HTB profile
    ID (a short numeric string). Returns None if it doesn't look like one.
    """
    pid = raw.split()[0] if raw.split() else ""
    if not (0 < len(pid) < 15 and pid.isdigit()):
        return None
    return int(pid)


def _parse_mention(raw):
    """Parses a Discord user mention (`<@123>` or `<@!123>`) into its ID, or
    returns None if `raw` isn't a mention.
    """
    m = re.match(r"<@!?(\d+)>$", raw.strip())
    return int(m.group(1)) if m else None


def _resolve_claimed_user(discord_id, db):
    """Returns the HTB user row linked to this Discord account, or None if
    unclaimed.
    """
    row = db.get_discord_user(discord_id)
    if row is None or row["htb_user_id"] is None:
        return None
    return db.get_user(row["htb_user_id"])


async def _find_member_by_display_name(guild, name):
    """Finds a guild member by display name or username (case-insensitive),
    checking the local member cache first and falling back to a live query.
    Returns None if no match is found.
    """
    if guild is None:
        return None
    name_lower = name.lower()

    # Cheap path first: whatever's already cached (populated by gateway events the
    # bot has seen so far -- e.g. members who've sent a message recently).
    for member in guild.members:
        if member.display_name.lower() == name_lower or member.name.lower() == name_lower:
            return member

    # Fall back to a live gateway query. This does NOT require the privileged
    # Members intent (that's only needed for an unfiltered/empty query or for
    # member add/update/remove events) -- a prefix `query` search is always allowed.
    try:
        results = await guild.query_members(query=name, limit=5)
    except discord.HTTPException:
        return None

    for member in results:
        if member.display_name.lower() == name_lower or member.name.lower() == name_lower:
            return member
    return None


async def _resolve_discord_identity(discord_id, db, guild):
    """Resolves a known Discord ID to an HTB user. Prefers an approved !claim
    link; if unclaimed, falls back to matching that Discord member's live
    display name/username directly against the HTB username table -- handles
    teammates who use the same handle on both platforms without having
    formally run !claim yet.
    """
    claimed = _resolve_claimed_user(discord_id, db)
    if claimed is not None:
        return claimed

    if guild is not None:
        # get_member() only checks the local member cache, which is incomplete
        # without the privileged Members intent -- fetch_member() is a live API
        # call that works regardless of cache/chunked state.
        member = guild.get_member(discord_id)
        if member is None:
            try:
                member = await guild.fetch_member(discord_id)
            except discord.NotFound:
                member = None
            except discord.HTTPException:
                member = None

        if member is not None:
            for candidate in (member.display_name, member.name):
                htb_user = db.get_user_by_name(candidate)
                if htb_user is not None:
                    return htb_user

    return None


async def _resolve_user(raw, db, guild=None):
    """Resolves a command argument to an HTB user row. Accepts, in order: a
    "<@discord_id>" mention, a bare Discord snowflake ID, an HTB profile ID, an
    HTB username, or (if `guild` is given) a Discord display name/username.
    A mention/ID/display-name match resolves via an approved !claim link if one
    exists, otherwise falls back to matching that Discord member's live name
    directly against the HTB username table.
    """
    raw = raw.strip()

    mentioned_id = _parse_mention(raw)
    if mentioned_id is not None:
        return await _resolve_discord_identity(mentioned_id, db, guild)

    if raw.isdigit():
        # Discord snowflake IDs are always >=15 digits; HTB profile IDs are much
        # shorter (see _validate_profile_id), so length alone disambiguates a bare
        # numeric ID between the two without needing "<@...>" mention syntax.
        if len(raw) >= 15:
            return await _resolve_discord_identity(int(raw), db, guild)
        return db.get_user(int(raw))

    # tolerate a manually-typed "@name" that Discord didn't convert to a real
    # "<@id>" mention (only happens if picked from the autocomplete popup)
    name = raw[1:] if raw.startswith("@") else raw

    htb_user = db.get_user_by_name(name)
    if htb_user is not None:
        return htb_user

    member = await _find_member_by_display_name(guild, name)
    if member is not None:
        return await _resolve_discord_identity(member.id, db, guild)

    return None


def _resolve_machine(raw, db):
    """Resolves a command argument to a machine row, by ID or name."""
    raw = raw.strip()
    if raw.isdigit():
        return db.get_machine(int(raw))
    return db.get_machine_by_name(raw)


def _resolve_challenge(raw, db):
    """Resolves a command argument to a challenge row, by ID or name."""
    raw = raw.strip()
    if raw.isdigit():
        return db.get_challenge(int(raw))
    return db.get_challenge_by_name(raw)


def _resolve_sherlock(raw, db):
    """Resolves a command argument to a sherlock row, by ID or name."""
    raw = raw.strip()
    if raw.isdigit():
        return db.get_sherlock(int(raw))
    return db.get_sherlock_by_name(raw)


def _parse_discord_id(text):
    """Parses a Discord user mention or bare snowflake ID from `text`.
    Returns None if neither form matches.
    """
    text = text.strip()
    m = re.match(r"<@!?(\d+)>", text)
    if m:
        return int(m.group(1))
    if text.isdigit():
        return int(text)
    return None


async def cmd_ganggang(message, data, api):
    """!ganggang -- replies with "ganggang"."""
    await message.channel.send("ganggang")


async def cmd_version(message, data, api):
    """!version -- shows the bot's current version, read from version.txt."""
    try:
        with open("version.txt") as f:
            version = f.read().strip()
    except OSError:
        version = "unknown"
    await message.channel.send(f"ggasylum v{version}")


async def cmd_ping(message, data, api):
    """!ping -- health check."""
    await message.channel.send("!pong")


async def cmd_stats(message, data, api):
    """!stats [profileID|name|@mention] -- posts a member's HTB stats card,
    defaulting to the caller's own claimed profile if no argument is given.
    """
    raw = data.strip()
    db = database.get()

    if not raw:
        user = _resolve_claimed_user(message.author.id, db)
        if user is None:
            await message.channel.send(
                "You haven't claimed an HTB profile yet -- use `!claim <profileID|name>`, "
                "or specify one directly: `!stats <profileID|name>`.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
    else:
        user = await _resolve_user(raw, db, message.guild)
        if user is None:
            await message.channel.send(
                f"User '{raw}' not found in DB.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

    pid = user["id"]

    if not db.user_synced(pid):
        worker = poller.get_sync_worker()
        if worker:
            worker.enqueue(pid)
        await message.channel.send(
            f"Profile {pid} hasn't been synced yet -- queued for sync. Try again in a moment.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    stats = db.get_user_stats(pid)
    avatar_path = await poller.resolve_avatar_path(pid, user["avatar_url"], message.guild)
    discord_display_name, tag = await poller.resolve_discord_display(pid, message.guild)
    buf = image_gen.generate_stats_image(dict(user), stats, avatar_path, discord_display_name, tag)
    await message.channel.send(file=discord.File(buf, "stats.png"))


async def _leaderboard_rows(db_rows, value_key, guild):
    """Converts DB rows into the {"name", "value", "avatar_path"} shape
    generate_leaderboard_image expects, resolving each user's avatar path
    along the way.
    """
    rows = []
    for r in db_rows:
        rows.append({
            "name": r["name"],
            "value": r[value_key],
            "avatar_path": await poller.resolve_avatar_path(r["id"], r["avatar_url"], guild),
        })
    return rows


async def cmd_leaderboard(message, data, api):
    """!leaderboard [points|bloods|season] -- posts a top-10 image for total
    points, team bloods in the configured rolling window, or team bloods on
    the current season's machines.
    """
    mode = data.strip().lower() or "points"
    db = database.get()

    if mode in ("points", "point", "pts"):
        db_rows = db.get_leaderboard_points(limit=10)
        rows = await _leaderboard_rows(db_rows, "points", message.guild)
        if not rows:
            await message.channel.send("No data yet.")
            return
        buf = image_gen.generate_leaderboard_image(
            "TOP 10 — POINTS", "HTB Points", rows, image_gen.HTB_GREEN, "pts",
            team_name=config.get_team_name(),
        )
        await message.channel.send(file=discord.File(buf, "leaderboard_points.png"))
    elif mode in ("bloods", "blood", "teamblood", "teambloods"):
        window_days = config.get().leaderboard_window_days
        since = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )
        db_rows = db.get_leaderboard_team_bloods(since, limit=10)
        rows = await _leaderboard_rows(db_rows, "blood_count", message.guild)
        if not rows:
            await message.channel.send(f"No team bloods recorded in the last {window_days} days.")
            return
        buf = image_gen.generate_leaderboard_image(
            "TOP 10 — TEAM BLOODS", f"Last {window_days} days", rows, image_gen.LB_PURPLE, "bloods",
            icon_path=str(image_gen.ASSETS / "blood_purple.png"),
            team_name=config.get_team_name(),
        )
        await message.channel.send(file=discord.File(buf, "leaderboard_bloods.png"))
    elif mode in ("season", "seasonal"):
        season_id, season_name, machine_ids = await poller.get_current_season_machines(api)
        if not machine_ids:
            await message.channel.send(
                "Couldn't determine the current season's machines right now. Try again shortly."
            )
            return
        db_rows = db.get_leaderboard_season_bloods(machine_ids, limit=10)
        rows = await _leaderboard_rows(db_rows, "blood_count", message.guild)
        if not rows:
            await message.channel.send(f"No team bloods recorded yet for {season_name}.")
            return
        buf = image_gen.generate_leaderboard_image(
            "TOP 10 — SEASON BLOODS", season_name, rows, image_gen.LB_GOLD, "bloods",
            icon_path=str(image_gen.ASSETS / "blood_gold.png"),
            team_name=config.get_team_name(),
        )
        await message.channel.send(file=discord.File(buf, "leaderboard_season.png"))
    else:
        await message.channel.send(
            "Usage: !leaderboard [points|bloods|season]",
            allowed_mentions=discord.AllowedMentions.none(),
        )


HELP_USAGE_COL   = 38  # usages longer than this wrap the description to its own line
HELP_INDENT      = "  "

DISCORD_MESSAGE_LIMIT = 2000
_CODE_BLOCK_OVERHEAD  = len("```\n\n```")  # the ``` wrapper itself counts against the limit


async def _send_code_block(channel, body):
    """Sends `body` wrapped in a code block, splitting into multiple messages
    on line boundaries if needed so it never hits (and silently fails against)
    Discord's 2000-char message limit.
    """
    max_chunk = DISCORD_MESSAGE_LIMIT - _CODE_BLOCK_OVERHEAD
    lines = body.split("\n")
    chunks = []
    current = []
    current_len = 0
    for line in lines:
        added_len = len(line) + (1 if current else 0)
        if current and current_len + added_len > max_chunk:
            chunks.append("\n".join(current))
            current = []
            added_len = len(line)
        current.append(line)
        current_len += added_len
    if current:
        chunks.append("\n".join(current))

    for chunk in chunks:
        await channel.send(
            f"```\n{chunk}\n```",
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def cmd_help(message, data, api):
    """!help -- lists every command available to the caller's role, grouped
    by role tier, packing short usage/description pairs onto one line each.
    """
    role = get_role(message)

    sections = {}
    for _, usage, desc, min_role in COMMAND_HELP:
        if role >= min_role:
            label = ROLE_LABELS[min_role]
            sections.setdefault(label, []).append((usage, desc))

    blocks = []
    for label in [None, "Admin", "Owner"]:
        if label not in sections:
            continue
        header = "COMMANDS" if label is None else label.upper()
        lines = [header]
        for usage, desc in sections[label]:
            if len(usage) > HELP_USAGE_COL:
                lines.append(f"{HELP_INDENT}{usage}")
                lines.append(f"{HELP_INDENT}{'':<{HELP_USAGE_COL}}{desc}")
            else:
                lines.append(f"{HELP_INDENT}{usage:<{HELP_USAGE_COL}}{desc}")
        blocks.append("\n".join(lines))

    body = "\n\n".join(blocks)
    await _send_code_block(message.channel, body)


async def cmd_help2(message, data, api):
    """!help2 -- same command listing as !help, but always puts each usage
    and description on its own line (more readable for longer descriptions,
    at the cost of a longer overall message).
    """
    role = get_role(message)

    sections = {}
    for _, usage, desc, min_role in COMMAND_HELP:
        if role >= min_role:
            label = ROLE_LABELS[min_role]
            sections.setdefault(label, []).append((usage, desc))

    blocks = []
    for label in [None, "Admin", "Owner"]:
        if label not in sections:
            continue
        header = "COMMANDS" if label is None else label.upper()
        lines = [header]
        for usage, desc in sections[label]:
            lines.append(f"{HELP_INDENT}{usage}")
            lines.append(f"{HELP_INDENT}{HELP_INDENT}{desc}")
        blocks.append("\n".join(lines))

    body = "\n\n".join(blocks)
    await _send_code_block(message.channel, body)


async def cmd_add_admin(message, data, api):
    """!addadmin <@mention|discordID> -- grants a Discord account the admin role."""
    discord_id = _parse_discord_id(data)
    if discord_id is None:
        await message.channel.send("Usage: !addadmin <@user|ID>")
        return
    database.get().set_discord_role(discord_id, ROLE_ADMIN)
    await message.channel.send(f"<@{discord_id}> is now an admin.", allowed_mentions=discord.AllowedMentions.none())


async def cmd_remove_admin(message, data, api):
    """!removeadmin <@mention|discordID> -- revokes a Discord account's admin role."""
    discord_id = _parse_discord_id(data)
    if discord_id is None:
        await message.channel.send("Usage: !removeadmin <@user|ID>")
        return
    database.get().set_discord_role(discord_id, ROLE_EVERYONE)
    await message.channel.send(f"<@{discord_id}> is no longer an admin.", allowed_mentions=discord.AllowedMentions.none())


async def cmd_show_admins(message, data, api):
    """!showadmins -- lists every Discord account with an elevated role."""
    role_names = {ROLE_ADMIN: "Admin", ROLE_OWNER: "Owner"}
    rows = database.get().get_admins()
    if not rows:
        await message.channel.send("No admins set.")
        return
    lines = [f"<@{row['discord_id']}> -- {role_names.get(row['role'], row['role'])}" for row in rows]
    await message.channel.send(
        "\n".join(lines),
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def cmd_testpwn(message, data, api):
    """!testpwn <profileID|name> <machineID|name> <user|root> <no|global|team|season>
    -- posts a fabricated pwn-alert card for testing the render pipeline,
    without writing anything to the DB.
    """
    parts = data.split()
    if len(parts) < 4:
        await message.channel.send("Usage: !testpwn <profileID|name> <machineID|name> <user|root> <no|global|team|season>")
        return

    own_type   = parts[2].lower()
    blood_type = parts[3].lower()

    if own_type not in ("user", "root"):
        await message.channel.send("Own type must be 'user' or 'root'.")
        return
    if blood_type not in ("no", "global", "team", "season"):
        await message.channel.send("Blood must be: no|global|team|season")
        return

    db = database.get()
    user    = await _resolve_user(parts[0], db, message.guild)
    machine = _resolve_machine(parts[1], db)

    if user is None:
        await message.channel.send(f"User '{parts[0]}' not found in DB.")
        return
    if machine is None:
        await message.channel.send(f"Machine '{parts[1]}' not found in DB.")
        return

    # a global blood is always also a team blood; season blood is a team blood on an
    # active seasonal machine (see image_gen.generate_solve_image for icon selection)
    entry = {
        "user_id":           user["id"],
        "user_name":         user["name"],
        "user_avatar_url":   user["avatar_url"],
        "object_id":         machine["id"],
        "object_name":       machine["name"],
        "object_type":       "machine",
        "type":              own_type,
        "points":            20,
        "first_blood":       blood_type == "global",
        "team_blood":        blood_type in ("global", "team", "season"),
        "is_season_machine": blood_type == "season",
        "category":          None,
        "object_avatar_url": machine["avatar_url"],
    }

    buf = await poller.build_pwn_image(entry, user["avatar_url"], message.guild)
    await message.channel.send(file=discord.File(buf, "solve.png"))


async def cmd_andor(message, data, api):
    """!andor <user1> <user2> <machine|challenge|sherlock> <ID|name> [user|root]
    -- compares two users' recorded solve dates for the same object, for
    debugging team-blood assignment.
    """
    parts = data.split()
    if len(parts) < 4:
        await message.channel.send(
            "Usage: !andor <user1> <user2> <machine|challenge|sherlock> <ID|name> [user|root]",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    db = database.get()
    obj_type = parts[2].lower()
    if obj_type not in ("machine", "challenge", "sherlock"):
        await message.channel.send("Object type must be: machine|challenge|sherlock")
        return

    solve_type = None
    if obj_type == "machine":
        if len(parts) < 5 or parts[4].lower() not in ("user", "root"):
            await message.channel.send(
                "Usage: !andor <user1> <user2> machine <ID|name> <user|root>",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        solve_type = parts[4].lower()

    user1 = await _resolve_user(parts[0], db, message.guild)
    user2 = await _resolve_user(parts[1], db, message.guild)
    if user1 is None:
        await message.channel.send(f"User '{parts[0]}' not found in DB.")
        return
    if user2 is None:
        await message.channel.send(f"User '{parts[1]}' not found in DB.")
        return

    resolver = {"machine": _resolve_machine, "challenge": _resolve_challenge, "sherlock": _resolve_sherlock}[obj_type]
    obj = resolver(parts[3], db)
    if obj is None:
        await message.channel.send(f"{obj_type.capitalize()} '{parts[3]}' not found in DB.")
        return

    rows = db.get_solve_pair(obj_type, obj["id"], user1["id"], user2["id"], solve_type)
    row1, row2 = rows[user1["id"]], rows[user2["id"]]

    label = obj["name"] + (f" ({solve_type})" if solve_type else "")
    lines = [f"**{label}** -- {user1['name']} vs {user2['name']}"]

    for u, row in ((user1, row1), (user2, row2)):
        if row is None:
            lines.append(f"  `{u['name']}`: no recorded solve")
        else:
            flags = []
            if row["blood"]:
                flags.append("global blood")
            if row["team_blood"]:
                flags.append("current team blood holder")
            flag_str = f" ({', '.join(flags)})" if flags else ""
            lines.append(f"  `{u['name']}`: {row['own_date']}{flag_str}")

    if row1 is not None and row2 is not None:
        if row1["own_date"] == row2["own_date"]:
            lines.append("Tied exactly -- shouldn't be possible, worth a closer look.")
        else:
            first = user1 if row1["own_date"] < row2["own_date"] else user2
            lines.append(f"**First: {first['name']}** (per full history, not the 90-day activity window)")
    elif row1 is None and row2 is None:
        lines.append("Neither user has a recorded solve for this.")

    await message.channel.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())


async def cmd_purge_user(message, data, api):
    """!purgeuser <profileID|name> -- permanently deletes a user and every
    trace of their solve/activity history, reassigning any team bloods they
    held to the next earliest solver.
    """
    raw = data.strip()
    if not raw:
        await message.channel.send(
            "Usage: !purgeuser <profileID|name>",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    db = database.get()
    user = await _resolve_user(raw, db, message.guild)
    if user is None:
        await message.channel.send(f"User '{raw}' not found in DB.")
        return

    result = db.purge_user(user["id"])
    counts = result["counts"]
    reassigned = result["reassigned"]

    lines = [f"Purged **{user['name']}** (ID {user['id']}) -- permanent, all history removed."]
    lines.append(
        f"Deleted: {counts['machine_solves']} machine solve(s), "
        f"{counts['challenge_solves']} challenge solve(s), "
        f"{counts['sherlock_solves']} sherlock solve(s), "
        f"{counts['prolab_rows']} prolab progress row(s), "
        f"{counts['fortress_rows']} fortress progress row(s), "
        f"{counts['activity_rows']} activity feed entry/entries."
    )
    if reassigned:
        lines.append("Team bloods reassigned:")
        lines.extend(f"  {r}" for r in reassigned)
    else:
        lines.append("No team bloods held -- nothing to reassign.")

    await message.channel.send(
        "\n".join(lines),
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def cmd_claim(message, data, api):
    """!claim <profileID|name> -- requests to link the caller's Discord
    account to one HTB profile. Needs an admin's !approve before it takes
    effect.
    """
    raw = data.strip()
    if not raw:
        await message.channel.send(
            "Usage: !claim <profileID|name>",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    db = database.get()
    discord_id = message.author.id

    existing = db.get_discord_user(discord_id)
    if existing is not None and existing["htb_user_id"] is not None:
        current = db.get_user(existing["htb_user_id"])
        current_name = current["name"] if current else str(existing["htb_user_id"])
        await message.channel.send(
            f"You've already claimed **{current_name}** -- an account can only claim one "
            "HTB profile. Ask an admin if it needs to change.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    htb_user = await _resolve_user(raw, db, message.guild)
    if htb_user is None:
        await message.channel.send(
            f"HTB profile '{raw}' not found -- it must already be tracked by the bot.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    claimant_id = db.get_discord_id_for_htb_user(htb_user["id"])
    if claimant_id is not None and claimant_id != discord_id:
        await message.channel.send(
            f"**{htb_user['name']}** is already claimed by another Discord user.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    pending = db.get_pending_claim_for_discord(discord_id)
    if pending is not None:
        await message.channel.send(
            f"You already have a pending claim (#{pending['id']}) for **{pending['htb_name']}** -- "
            "wait for it to be resolved before submitting another.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    requested_at = datetime.now(timezone.utc).isoformat()
    claim_id = db.create_claim(discord_id, htb_user["id"], requested_at)
    await message.channel.send(
        f"Claim #{claim_id} submitted -- linking your Discord account to HTB profile "
        f"**{htb_user['name']}**. An admin needs to run `!approve {claim_id}` to confirm it.",
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def cmd_claim_queue(message, data, api):
    """!claimqueue -- lists pending claim requests awaiting admin approval."""
    rows = database.get().get_pending_claims()
    if not rows:
        await message.channel.send("Claim queue is empty.", allowed_mentions=discord.AllowedMentions.none())
        return

    lines = ["**Pending claims:**"]
    for r in rows:
        lines.append(f"  #{r['id']} -- <@{r['discord_id']}> -> **{r['htb_name']}** (requested {r['requested_at']})")
    await message.channel.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())


async def cmd_claim_approve(message, data, api):
    """!approve <claimID> -- approves a pending claim, linking that Discord
    account to the requested HTB profile. Refuses self-approval below owner
    level, and re-checks for a race against another claim landing first.
    """
    raw = data.strip()
    if not raw.isdigit():
        await message.channel.send(
            "Usage: !approve <claimID>",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    claim_id = int(raw)
    db = database.get()
    claim = db.get_claim(claim_id)
    if claim is None or claim["status"] != "pending":
        await message.channel.send(
            f"Claim #{claim_id} not found or already resolved.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    if claim["discord_id"] == message.author.id and get_role(message) < ROLE_OWNER:
        await message.channel.send(
            "You can't approve your own claim request.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    # re-check for a race: someone else could have gotten linked to this HTB profile
    # since the claim was queued
    claimant_id = db.get_discord_id_for_htb_user(claim["htb_user_id"])
    if claimant_id is not None and claimant_id != claim["discord_id"]:
        await message.channel.send(
            f"Claim #{claim_id} can't be approved -- that HTB profile is already linked "
            f"to another Discord user. Consider `!deny {claim_id}`.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    if not db.link_htb_user(claim["discord_id"], claim["htb_user_id"]):
        await message.channel.send(
            f"Claim #{claim_id} can't be approved -- that HTB profile just got linked to "
            f"another Discord user. Consider `!deny {claim_id}`.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    resolved_at = datetime.now(timezone.utc).isoformat()
    db.resolve_claim(claim_id, "approved", message.author.id, resolved_at)

    htb_user = db.get_user(claim["htb_user_id"])
    htb_name = htb_user["name"] if htb_user else str(claim["htb_user_id"])
    await message.channel.send(
        f"Claim #{claim_id} approved -- <@{claim['discord_id']}> is now linked to **{htb_name}**.",
        allowed_mentions=discord.AllowedMentions(users=True),
    )


async def cmd_claim_deny(message, data, api):
    """!deny <claimID> [reason] -- denies a pending claim."""
    parts = data.strip().split(maxsplit=1)
    if not parts or not parts[0].isdigit():
        await message.channel.send(
            "Usage: !deny <claimID> [reason]",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    claim_id = int(parts[0])
    reason = parts[1] if len(parts) > 1 else None

    db = database.get()
    claim = db.get_claim(claim_id)
    if claim is None or claim["status"] != "pending":
        await message.channel.send(
            f"Claim #{claim_id} not found or already resolved.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    if claim["discord_id"] == message.author.id and get_role(message) < ROLE_OWNER:
        await message.channel.send(
            "You can't deny your own claim request -- ask another admin, or the owner, to review it.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    resolved_at = datetime.now(timezone.utc).isoformat()
    db.resolve_claim(claim_id, "denied", message.author.id, resolved_at)

    suffix = f" ({reason})" if reason else ""
    await message.channel.send(
        f"Claim #{claim_id} denied for <@{claim['discord_id']}>{suffix}.",
        allowed_mentions=discord.AllowedMentions(users=True),
    )


async def cmd_preference(message, data, api):
    """!preference <htb|discord> -- sets which avatar (HTB profile picture or
    Discord avatar) shows on the caller's stats/pwn-alert/leaderboard cards.
    Requires an approved claim.
    """
    pref = data.strip().lower()
    if pref not in ("htb", "discord"):
        await message.channel.send(
            "Usage: !preference <htb|discord>",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    db = database.get()
    existing = db.get_discord_user(message.author.id)
    if existing is None or existing["htb_user_id"] is None:
        await message.channel.send(
            "You need an approved HTB claim before setting an avatar preference -- use `!claim <profileID|name>` first.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    db.set_avatar_pref(message.author.id, pref)
    label = "your HTB profile picture" if pref == "htb" else "your Discord avatar"
    await message.channel.send(
        f"Preference set -- stats/pwn alerts/leaderboards will now show {label}.",
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def cmd_tag(message, data, api):
    """!tag <text> -- sets a short tag shown on the caller's !stats card.
    Requires an approved claim; rejects text over MAX_TAG_LENGTH.
    """
    tag = data.strip()
    if not tag:
        await message.channel.send(
            f"Usage: !tag <text> (max {image_gen.MAX_TAG_LENGTH} characters)",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    if len(tag) > image_gen.MAX_TAG_LENGTH:
        await message.channel.send(
            f"Tag too long -- max {image_gen.MAX_TAG_LENGTH} characters (yours was {len(tag)}).",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    db = database.get()
    existing = db.get_discord_user(message.author.id)
    if existing is None or existing["htb_user_id"] is None:
        await message.channel.send(
            "You need an approved HTB claim before setting a tag -- use `!claim <profileID|name>` first.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    db.set_tag(message.author.id, tag)
    await message.channel.send(
        f"Tag set -- **{tag}** will now show on your `!stats` card.",
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def cmd_trimcache(message, data, api):
    """!trimcache -- deletes any cached avatar file no longer referenced by
    an avatar_url stored in the DB.
    """
    known_urls = database.get().get_all_avatar_urls()
    known_paths = {cache._url_to_path(url) for url in known_urls}

    removed = 0
    freed = 0
    cache_dir = cache.CACHE_DIR
    try:
        for fname in os.listdir(cache_dir):
            fpath = os.path.join(cache_dir, fname)
            if fpath not in known_paths:
                freed += os.path.getsize(fpath)
                os.remove(fpath)
                removed += 1
    except FileNotFoundError:
        await message.channel.send("Cache directory not found.")
        return

    freed_kb = freed / 1024
    await message.channel.send(f"Removed {removed} orphaned file(s), freed {freed_kb:.1f} KB.")


async def cmd_syncqueue(message, data, api):
    """!syncqueue -- shows what the activity poller is doing right now and
    how many users are queued for a full profile sync.
    """
    activity_poller = poller.get_activity_poller()
    lines = []
    if activity_poller is not None:
        # Shown first because "0 pending" below reads as "nothing to do" even
        # while the poller is deep in a multi-minute avatar-resolution pass that
        # hasn't reached the point of enqueueing anything yet -- see
        # TeamActivityPoller.status().
        lines.append(f"Activity poller: {activity_poller.status()}")

    worker = poller.get_sync_worker()
    if worker is None:
        lines.append("Sync worker not running.")
    else:
        lines.append(f"Profile sync queue: {worker.queue_size()} pending.")

    await message.channel.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())


async def cmd_syncstatus(message, data, api):
    """!syncstatus -- shows which user (if any) the profile sync worker is
    currently syncing, and how many remain queued.
    """
    worker = poller.get_sync_worker()
    if worker is None:
        await message.channel.send("Sync worker not running.")
        return
    await message.channel.send(worker.status())


async def cmd_dbstats(message, data, api):
    """!dbstats -- shows row counts for every tracked table."""
    counts = database.get().get_table_counts()
    lines = [f"`{table:<30} {count}`" for table, count in counts.items()]
    await message.channel.send(
        "**DB row counts:**\n" + "\n".join(lines),
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def cmd_setchannel(message, data, api):
    """Changes where pwn alerts get posted (config.channel_id) -- unlike every
    other command, which just runs wherever it's typed, this one command has a
    persistent, global effect, so it's kept admin-only and always echoes back
    which channel it landed on.
    """
    query = data.strip()
    if not query:
        await message.channel.send(
            "Usage: !setchannel <channelID|channel name>",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    guild = message.guild
    if guild is None:
        await message.channel.send(
            "This command must be used in a server.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    if query.isdigit():
        channel = guild.get_channel(int(query))
        if channel is None:
            await message.channel.send(
                f"No channel with ID {query} found in this server.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        matches = [channel]
    else:
        pattern = query.lower()
        if "*" in pattern or "?" in pattern:
            matches = [c for c in guild.text_channels if fnmatch.fnmatch(c.name.lower(), pattern)]
        else:
            matches = [c for c in guild.text_channels if pattern in c.name.lower()]

        if not matches:
            await message.channel.send(
                f"No channels found matching '{query}'.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

    if len(matches) > 1:
        lines = [f"Multiple channels matched '{query}' -- pick one by ID:"]
        for c in matches:
            lines.append(f"  #{c.name} (ID: {c.id})")
        lines.append("")
        lines.append(f"Example: !setchannel {matches[0].id}")
        await message.channel.send(
            "\n".join(lines),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    channel = matches[0]
    cfg = config.get()
    cfg.channel_id = channel.id
    config.save()

    await message.channel.send(
        f"Pwn alert channel set to #{channel.name} ({channel.id}).",
        allowed_mentions=discord.AllowedMentions.none(),
    )


_DURATION_UNITS = {
    "s":  1,
    "m":  60,
    "h":  3600,
    "d":  86400,
    "w":  604800,
    "mo": 2592000,   # 30 days, approximate
    "y":  31536000,  # 365 days, approximate
}
_DURATION_RE = re.compile(r"(\d+)(mo|[smhdwy])")


def _parse_duration(raw):
    """Parses a duration string like "30d" or "6mo" into a timedelta. Returns
    None if it doesn't match `<number><unit>` (s|m|h|d|w|mo|y).
    """
    m = _DURATION_RE.fullmatch(raw.strip().lower())
    if not m:
        return None
    n, unit = m.groups()
    return timedelta(seconds=int(n) * _DURATION_UNITS[unit])


def _format_age(delta):
    """Formats a timedelta as a single rounded-down unit, e.g. "3d" or "6mo"."""
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    if days < 30:
        return f"{days}d"
    months = days // 30
    if months < 12:
        return f"{months}mo"
    return f"{days // 365}y"


async def cmd_show_stale(message, data, api):
    """!showstale <duration> -- lists users with no recorded activity since
    the given duration ago (e.g. 30d, 6mo).
    """
    raw = data.strip()
    if not raw:
        await message.channel.send(
            "Usage: !showstale <duration> (e.g. 45s, 20m, 6h, 30d, 2w, 3mo, 1y)",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    delta = _parse_duration(raw)
    if delta is None:
        await message.channel.send(
            "Couldn't parse duration. Use a number + unit: s|m|h|d|w|mo|y (e.g. 30d, 20m, 6mo).",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    now = datetime.now(timezone.utc)
    cutoff = (now - delta).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    rows = database.get().get_stale_users(cutoff)
    if not rows:
        await message.channel.send(
            f"No stale users -- everyone has activity within the last {raw}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    lines = [f"**Stale users (no activity in the last {raw}):**"]
    for row in rows:
        if row["last_active"] is None:
            lines.append(f"  `{row['name']}` -- no recorded activity")
        else:
            last = datetime.fromisoformat(row["last_active"].replace("Z", "+00:00"))
            lines.append(f"  `{row['name']}` -- last active {_format_age(now - last)} ago")

    await message.channel.send(
        "\n".join(lines),
        allowed_mentions=discord.AllowedMentions.none(),
    )


COMMANDS = {
    "ganggang":     (cmd_ganggang,     ROLE_EVERYONE),
    "ping":         (cmd_ping,         ROLE_EVERYONE),
    "version":      (cmd_version,      ROLE_EVERYONE),
    "stats":        (cmd_stats,        ROLE_EVERYONE),
    "leaderboard":  (cmd_leaderboard,  ROLE_EVERYONE),
    "help":         (cmd_help,         ROLE_EVERYONE),
    "help2":        (cmd_help2,        ROLE_EVERYONE),
    "syncqueue":    (cmd_syncqueue,    ROLE_ADMIN),
    "syncstatus":   (cmd_syncstatus,   ROLE_ADMIN),
    "dbstats":      (cmd_dbstats,      ROLE_ADMIN),
    "setchannel":   (cmd_setchannel,   ROLE_ADMIN),
    "showstale":    (cmd_show_stale,   ROLE_ADMIN),
    "claim":        (cmd_claim,        ROLE_EVERYONE),
    "preference":   (cmd_preference,   ROLE_EVERYONE),
    "tag":          (cmd_tag,          ROLE_EVERYONE),
    "claimqueue":   (cmd_claim_queue,  ROLE_ADMIN),
    "approve":      (cmd_claim_approve, ROLE_ADMIN),
    "deny":         (cmd_claim_deny,   ROLE_ADMIN),
    "testpwn":      (cmd_testpwn,      ROLE_OWNER),
    "andor":        (cmd_andor,        ROLE_OWNER),
    "trimcache":    (cmd_trimcache,    ROLE_OWNER),
    "purgeuser":    (cmd_purge_user,   ROLE_OWNER),
    "addadmin":     (cmd_add_admin,    ROLE_OWNER),
    "removeadmin":  (cmd_remove_admin, ROLE_OWNER),
    "showadmins":   (cmd_show_admins,  ROLE_OWNER),
}
