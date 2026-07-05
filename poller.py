import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone

import discord
import database
from database import SHERLOCK_CATEGORIES
from api_client import RateLimitError
import config
import cache
import image_gen

logger = logging.getLogger(__name__)

_htb_limiter = None
_sync_worker = None
_activity_poller = None

# Entries older than this when first seen (e.g. the bot was offline for a
# while and comes back to a backlog, or a purge triggers a history re-sync)
# are recorded silently instead of announced, so a long gap doesn't spam a
# wall of "new" pwns that actually happened days ago.
ANNOUNCE_MAX_AGE = timedelta(hours=1)


def _is_stale(entry, now):
    """True if `entry`'s date is older than ANNOUNCE_MAX_AGE relative to `now`."""
    try:
        entry_date = datetime.fromisoformat(entry["date"].replace("Z", "+00:00"))
    except (ValueError, KeyError):
        return False
    return (now - entry_date) > ANNOUNCE_MAX_AGE


class _RateLimiter:
    """Simple async rate limiter: acquire() blocks just long enough to keep
    calls spaced at least 60/calls_per_minute seconds apart.
    """

    def __init__(self, calls_per_minute):
        """Sets the target call rate."""
        self._interval = 60.0 / calls_per_minute
        self._last_call = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self):
        """Waits (if needed) until enough time has passed since the last call."""
        async with self._lock:
            now = asyncio.get_event_loop().time()
            wait = self._interval - (now - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = asyncio.get_event_loop().time()


def _get_htb_limiter():
    """Returns the shared HTB rate limiter, creating it on first use."""
    global _htb_limiter
    if _htb_limiter is None:
        _htb_limiter = _RateLimiter(config.get().htb_rate_limit)
    return _htb_limiter


def get_sync_worker():
    """Returns the registered ProfileSyncWorker instance, or None if not set."""
    return _sync_worker


def set_sync_worker(worker):
    """Registers the running ProfileSyncWorker instance for other modules to
    look up (e.g. commands.py's !syncqueue/!syncstatus).
    """
    global _sync_worker
    _sync_worker = worker


def get_activity_poller():
    """Returns the registered TeamActivityPoller instance, or None if not set."""
    return _activity_poller


def set_activity_poller(poller_instance):
    """Registers the running TeamActivityPoller instance for other modules to
    look up (e.g. commands.py's !syncqueue).
    """
    global _activity_poller
    _activity_poller = poller_instance


_SEASON_CACHE_TTL = 900  # 15 minutes -- max age before get_current_season_machines() re-fetches
_season_cache = {"season_id": None, "season_name": None, "machine_ids": None, "fetched_at": None}


async def _refresh_season_cache(htb_api):
    """Unconditionally re-fetches the active season and its machine list from HTB
    and updates the module-level cache. Called from get_current_season_machines()
    below, either by TeamActivityPoller at the top of each poll cycle or, more
    rarely, by a !leaderboard season command landing when the cache is stale.
    """
    now = asyncio.get_event_loop().time()

    await _get_htb_limiter().acquire()
    seasons = await asyncio.to_thread(htb_api.get, "season/list", api_version="v4")
    if not seasons:
        logger.warning("Failed to fetch season/list")
        return

    active = next((s for s in seasons.get("data", []) if s.get("active")), None)
    if active is None:
        logger.warning("No active season found in season/list")
        return

    season_id = active["id"]
    season_name = active["name"]

    await _get_htb_limiter().acquire()
    result = await asyncio.to_thread(htb_api.get, f"season/machines/{season_id}", api_version="v4")
    if not result:
        logger.warning(f"Failed to fetch season/machines/{season_id}")
        return

    machine_ids = {m["id"] for m in result.get("data", []) if "id" in m}

    _season_cache.update({
        "season_id": season_id,
        "season_name": season_name,
        "machine_ids": machine_ids,
        "fetched_at": now,
    })
    logger.debug(f"Season cache refreshed: {season_name} ({len(machine_ids)} machines)")


async def get_current_season_machines(htb_api):
    """Returns (season_id, season_name, machine_ids) for the currently active HTB
    season. Refreshes the cache if it's missing or older than _SEASON_CACHE_TTL,
    otherwise returns the cached value with no live HTB call.

    Called at the top of TeamActivityPoller.poll(), before that cycle's activity
    fetch, so a season transition is always reflected before we tag that same
    cycle's new solves as season/non-season -- see the comment there for why
    ordering matters (a machine solved minutes after a season launch must not be
    tagged against the outgoing season).
    """
    now = asyncio.get_event_loop().time()
    fetched_at = _season_cache["fetched_at"]
    if fetched_at is None or now - fetched_at >= _SEASON_CACHE_TTL:
        await _refresh_season_cache(htb_api)
    return _season_cache["season_id"], _season_cache["season_name"], _season_cache["machine_ids"]


class BasePoller:
    """Base class for a poller that repeatedly calls poll() on a fixed
    interval, starting immediately once the Discord client is ready.
    """

    interval = 600

    def __init__(self):
        """Sets the poll interval from config."""
        self.interval = config.get().poll_interval

    async def htb_get(self, htb_api, *args, **kwargs):
        """Rate-limited HTB API GET, run off the event loop thread."""
        await _get_htb_limiter().acquire()
        return await asyncio.to_thread(htb_api.get, *args, **kwargs)

    async def poll(self, client, htb_api):
        """Subclasses implement one poll cycle's work here."""
        raise NotImplementedError

    async def start(self, client, htb_api):
        """Runs poll() immediately, then repeatedly every `interval` seconds,
        until the client disconnects. A poll() error is logged and skipped
        rather than stopping the loop.
        """
        await client.wait_until_ready()
        while not client.is_closed():
            try:
                await self.poll(client, htb_api)
            except Exception as e:
                logger.error(f"{self.__class__.__name__} error: {e}")
            await asyncio.sleep(self.interval)


async def _fetch_guild_member(guild, discord_id):
    """get_member() only checks the local member cache, which is incomplete
    without the privileged Members intent -- fetch_member() is a live API call
    that works regardless of cache/chunked state. Returns None if the ID isn't
    actually a member of this guild (left, kicked, etc) or on any API error.
    """
    if guild is None:
        return None
    member = guild.get_member(discord_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(discord_id)
    except (discord.NotFound, discord.HTTPException):
        return None


async def resolve_avatar_path(htb_user_id, htb_avatar_url, guild):
    """Returns the local cached avatar file path to show for this HTB user,
    respecting their Discord avatar preference (discord_users.avatar_pref) if
    they've claimed a profile and set one. Falls back to their HTB avatar if
    unclaimed, no preference set, preference is 'htb', or the Discord avatar
    can't be resolved for any reason (member left the guild, API error, etc).
    """
    if guild is not None:
        db = database.get()
        discord_id = db.get_discord_id_for_htb_user(htb_user_id)
        if discord_id is not None:
            row = db.get_discord_user(discord_id)
            if row and row["avatar_pref"] == "discord":
                member = await _fetch_guild_member(guild, discord_id)
                if member is not None:
                    discord_path = await cache.get_discord_avatar(str(member.display_avatar.url))
                    if discord_path:
                        return discord_path
    return await cache.get_avatar(htb_avatar_url)


async def resolve_discord_display(htb_user_id, guild):
    """Returns (display_name, tag) for this HTB user's linked Discord account, if
    claimed -- (None, None) if unclaimed or the member can't be live-resolved.
    Shown on !stats regardless of avatar_pref, which only controls which avatar
    image is used, not whether the Discord identity itself is shown.
    """
    db = database.get()
    discord_id = db.get_discord_id_for_htb_user(htb_user_id)
    if discord_id is None:
        return None, None

    row = db.get_discord_user(discord_id)
    tag = row["tag"] if row else None

    member = await _fetch_guild_member(guild, discord_id)
    display_name = member.display_name if member is not None else None
    return display_name, tag


async def build_pwn_image(entry, user_avatar_url, guild=None):
    """Resolves everything a pwn-alert card needs (user avatar, box avatar,
    linked Discord identity) and renders it via image_gen, returning the PNG
    as a BytesIO.
    """
    user_avatar_path = await resolve_avatar_path(entry["user_id"], user_avatar_url, guild)
    discord_display_name, tag = await resolve_discord_display(entry["user_id"], guild)

    # Prefer the avatar URL stored during profile sync over the one from the activity feed,
    # as the activity feed URL may point to a generic placeholder on HTB's CDN.
    db = database.get()
    obj_type = entry.get("object_type")
    obj_id = entry.get("object_id")
    db_obj = None
    if obj_type == "machine":
        db_obj = db.get_machine(obj_id)
    elif obj_type == "challenge":
        db_obj = db.get_challenge(obj_id)
    elif obj_type == "sherlock":
        db_obj = db.get_sherlock(obj_id)

    # Both sources can be a generic HTB placeholder (see database.clean_avatar_url) --
    # the DB value already goes through that guard on write, but the activity-feed
    # fallback doesn't until now, so it's cleaned here too before ever being used.
    machine_avatar_url = (db_obj["avatar_url"] if db_obj else None) or database.clean_avatar_url(entry.get("object_avatar_url"))
    machine_avatar_path = await cache.get_avatar(machine_avatar_url)

    return image_gen.generate_solve_image(entry, user_avatar_path, machine_avatar_path, discord_display_name, tag)


async def _resolve_challenge_avatar(htb_api, db, category):
    """team/activity never reports avatar_url for challenge-type entries,
    across every category -- fall back to our own record of
    challenge/categories/list. Only hits HTB when this category isn't already
    on file, so a fully-seen category set costs zero extra calls.
    """
    if not category:
        return None

    icon = db.get_challenge_category_icon(category)
    if icon is not None:
        return icon

    await _get_htb_limiter().acquire()
    result = await asyncio.to_thread(htb_api.get, "challenge/categories/list", api_version="v4")
    if not result:
        logger.warning("Failed to fetch challenge/categories/list")
        return None

    categories = {c["name"]: c["icon"] for c in result.get("info", []) if c.get("icon")}
    db.upsert_challenge_categories(categories)
    db.commit()
    logger.info(f"Learned {len(categories)} challenge categories (new one seen: {category!r})")
    return categories.get(category)


async def _resolve_machine_avatar(htb_api, machine_id):
    """team/activity sometimes reports only a generic default-thumb placeholder
    for a machine's avatar (seen on old/retired/Starting-Point boxes, which
    don't always show up in a user's own profile/activity sync either) even
    though HTB has real box art on machine/profile/{id}. Only called when the
    caller has already confirmed there's no real avatar on file yet -- the
    result then gets stored via upsert_machine's COALESCE, so this is a
    one-time call per machine, not recurring.
    """
    await _get_htb_limiter().acquire()
    result = await asyncio.to_thread(htb_api.get, f"machine/profile/{machine_id}", api_version="v4")
    if not result:
        logger.warning(f"Failed to fetch machine/profile/{machine_id}")
        return None
    return result.get("info", {}).get("avatar")


class TeamActivityPoller(BasePoller):
    """Polls HTB's team activity feed, records new solves, resolves any
    missing avatars, and posts pwn-alert cards for anything new.
    """

    def __init__(self):
        """Initializes with an idle status."""
        super().__init__()
        self._status = "idle"

    def _set_status(self, status):
        """Updates the live status string returned by status()."""
        self._status = status
        logger.debug(f"TeamActivityPoller: {status}")

    def status(self):
        """Live one-line description of what this cycle's poll() call is doing
        right now -- distinct from ProfileSyncWorker.status(), since the avatar-
        resolution phase below can run for minutes on a from-scratch rebuild
        before anything gets enqueued to the sync worker at all. Without this,
        !syncqueue showing 0 during that phase reads as "nothing to do" when
        there's actually a lot happening that just hasn't reached the queue yet.
        """
        return self._status

    def _parse_entry(self, raw):
        """Normalizes one raw team/activity API entry into the dict shape used
        throughout this module.
        """
        user = raw["user"]
        obj_type = raw.get("object_type", "machine")
        object_avatar_url = (
            raw.get("machine_avatar") if obj_type == "machine" else raw.get("avatar_url")
        )
        return {
            "user_id": user["id"],
            "user_name": user["name"],
            "user_avatar_url": user.get("avatar_thumb"),
            "object_id": raw["id"],
            "object_name": raw["name"],
            "object_type": obj_type,
            "type": raw["type"],
            "points": raw["points"],
            "first_blood": raw.get("first_blood", False),
            "category": raw.get("challenge_category"),
            "object_avatar_url": object_avatar_url,
            "date": raw["date"],
        }

    def _resolve_user_avatar(self, entry):
        """Returns the HTB avatar URL to use for this entry's user.

        Future: if discord_users links this htb_user_id to a claimed Discord
        user, return their Discord avatar URL instead.
        """
        return entry["user_avatar_url"]

    async def _cache_avatars(self, entry):
        """Ensures this entry's user and object avatars are locally cached."""
        if entry["user_avatar_url"]:
            await cache.get_avatar(entry["user_avatar_url"])
        if entry["object_avatar_url"]:
            await cache.get_avatar(entry["object_avatar_url"])

    def _record_solve(self, db, entry):
        """Writes every solve the poller sees into the authoritative holder table
        (user_machine_solves / user_challenge_solves / user_sherlock_solves) as it
        happens, not just at a user's one-time full profile sync. Without this,
        anything solved after a user's initial sync would only ever exist in
        team_activity (the rolling 90-day feed) and silently drift out of sync with
        the "full history" tables that team-blood status, !stats, and the bloods/
        season leaderboards all actually read from.
        """
        obj_id   = entry["object_id"]
        obj_name = entry["object_name"]
        avatar   = entry.get("object_avatar_url")
        date     = entry["date"]
        blood    = entry.get("first_blood", False)
        points   = entry.get("points", 0)

        if entry["object_type"] == "machine":
            db.upsert_machine(obj_id, obj_name, avatar_url=avatar)
            db.upsert_machine_solve(entry["user_id"], obj_id, entry["type"], date, blood, points)
        elif entry.get("category") in SHERLOCK_CATEGORIES:
            db.upsert_sherlock(obj_id, obj_name, avatar_url=avatar, category=entry.get("category"))
            db.upsert_sherlock_solve(entry["user_id"], obj_id, date, blood, points)
        else:
            db.upsert_challenge(obj_id, obj_name, avatar_url=avatar, category=entry.get("category"))
            db.upsert_challenge_solve(entry["user_id"], obj_id, date, blood, points)

    async def _announce(self, client, channel_id, entries, msg_interval):
        """Posts one pwn-alert card per entry to the given channel, spaced
        `msg_interval` seconds apart to stay under Discord's rate limit.
        """
        channel = client.get_channel(channel_id)
        if not channel:
            logger.warning(f"Channel {channel_id} not found")
            return
        for i, entry in enumerate(entries):
            buf = await build_pwn_image(entry, self._resolve_user_avatar(entry), channel.guild)
            await channel.send(file=discord.File(buf, "solve.png"))
            if i < len(entries) - 1:
                await asyncio.sleep(msg_interval)

    async def poll(self, client, htb_api):
        """One poll cycle: refresh the season cache, fetch team activity,
        resolve any missing avatars, record new solves to the DB, enqueue
        profile syncs for unsynced users, and announce anything new (unless
        this is the initial seed of an empty database, which records silently).
        """
        cfg = config.get()
        db = database.get()
        worker = get_sync_worker()

        try:
            # Resolve the season *before* fetching this cycle's activity, so a season
            # transition is always picked up ahead of any solves reported in the same
            # cycle (e.g. someone blooding a brand-new season machine minutes after
            # launch) instead of tagging them against the outgoing season.
            self._set_status("checking season cache")
            _, _, season_machine_ids = await get_current_season_machines(htb_api)
            season_machine_ids = season_machine_ids or set()

            self._set_status("fetching team activity")
            result = await self.htb_get(
                htb_api,
                f"team/activity/{cfg.team_id}",
                api_version="v4",
                params={"n_past_days": 90},
            )
            if not result:
                logger.warning("Team activity poll returned no data")
                return

            entries = [self._parse_entry(r) for r in result]

            # Per-cycle cache so a machine referenced by many entries in this same
            # batch (e.g. a from-scratch rebuild pulling the full 90-day window, where
            # a popular machine can appear a dozen+ times before any of those entries
            # have been written to the DB yet) only ever triggers one HTB call here,
            # not one per entry. db.get_machine() alone isn't enough for that, since
            # nothing gets upserted until after this whole loop finishes.
            resolved_machine_avatars = {}

            for i, entry in enumerate(entries):
                if entry["object_type"] == "challenge" and not entry["object_avatar_url"]:
                    self._set_status(f"resolving challenge avatar ({i + 1}/{len(entries)})")
                    entry["object_avatar_url"] = await _resolve_challenge_avatar(
                        htb_api, db, entry.get("category")
                    )
                elif entry["object_type"] == "machine" and not database.clean_avatar_url(entry["object_avatar_url"]):
                    obj_id = entry["object_id"]
                    if obj_id in resolved_machine_avatars:
                        entry["object_avatar_url"] = resolved_machine_avatars[obj_id]
                        continue
                    db_machine = db.get_machine(obj_id)
                    if db_machine and db_machine["avatar_url"]:
                        resolved_machine_avatars[obj_id] = db_machine["avatar_url"]
                    else:
                        self._set_status(
                            f"resolving avatar for machine {obj_id} ({i + 1}/{len(entries)})"
                        )
                        resolved_machine_avatars[obj_id] = await _resolve_machine_avatar(htb_api, obj_id)
                    entry["object_avatar_url"] = resolved_machine_avatars[obj_id]

            if db.is_empty():
                self._set_status(f"seeding {len(entries)} activity entries")
                logger.info(f"Seeding {len(entries)} activity entries, skipping announcements")
                for entry in entries:
                    db.insert_activity(entry)
                    self._record_solve(db, entry)
                    await self._cache_avatars(entry)
                db.commit()
                if worker:
                    self._set_status("enqueueing profile syncs")
                    seen = set()
                    for entry in entries:
                        uid = entry["user_id"]
                        if uid not in seen:
                            seen.add(uid)
                            worker.enqueue(uid)
                return

            self._set_status("recording new solves")
            new_entries = [entry for entry in entries if db.insert_activity(entry)]

            for entry in new_entries:
                self._record_solve(db, entry)
            if new_entries:
                db.commit()

            if worker:
                seen = set()
                for entry in entries:
                    uid = entry["user_id"]
                    if uid not in seen:
                        seen.add(uid)
                        if not db.user_synced(uid):
                            worker.enqueue(uid)

            if not new_entries:
                logger.debug("Poll complete: no new activity")
                return

            new_entries.sort(key=lambda e: e["date"])
            logger.info(f"Poll complete: {len(new_entries)} new entries")

            self._set_status(f"caching avatars for {len(new_entries)} new entries")
            for entry in new_entries:
                await self._cache_avatars(entry)

            now = datetime.now(timezone.utc)
            announce_entries = [e for e in new_entries if not _is_stale(e, now)]
            stale_count = len(new_entries) - len(announce_entries)
            if stale_count:
                logger.info(
                    f"Suppressing {stale_count} stale entr{'y' if stale_count == 1 else 'ies'} "
                    "from announcement (recorded silently, e.g. re-synced history)"
                )

            if not announce_entries:
                return

            for entry in announce_entries:
                entry["team_blood"] = db.is_team_blood(
                    entry["object_id"], entry["object_type"], entry["type"],
                    entry["user_id"], entry["date"], entry.get("category"),
                )
                entry["is_season_machine"] = (
                    entry["object_type"] == "machine" and entry["object_id"] in season_machine_ids
                )

            self._set_status(f"announcing {len(announce_entries)} entries")
            msg_interval = 60 / cfg.discord_rate_limit
            await self._announce(client, cfg.channel_id, announce_entries, msg_interval)
        finally:
            self._set_status("idle")


class EasterEggPoller:
    """Inside joke carried over from the bot this one replaced: its DB tracking was
    broken in a way that made it repeatedly announce Kamigold first-blooding root on
    Monteverde, over and over, forever. Recreating that on purpose here -- fires at
    a random ~3-day interval, purely cosmetic (no DB writes, doesn't touch real
    solves/points/leaderboards.
    """
    USER_NAME    = "Kamigold"
    MACHINE_NAME = "Monteverde"
    MIN_SECONDS  = 2 * 86400  # 2 days
    MAX_SECONDS  = 4 * 86400  # 4 days (~3 days average)

    def _next_delay(self):
        """Returns a random delay (seconds) until the next fire, uniformly
        between MIN_SECONDS and MAX_SECONDS.
        """
        return random.uniform(self.MIN_SECONDS, self.MAX_SECONDS)

    async def start(self, client, htb_api):
        """Fires _fire() on a random ~2-4 day interval, for as long as
        `funnymode` is enabled in config. Does nothing at all if disabled.
        """
        await client.wait_until_ready()
        if not config.get().funnymode:
            logger.info("EasterEggPoller: funnymode disabled in config, not starting")
            return
        while not client.is_closed():
            try:
                await asyncio.sleep(self._next_delay())
                await self._fire(client)
            except Exception as e:
                logger.error(f"EasterEggPoller error: {e}")

    async def _fire(self, client):
        """Posts one fake root-blood pwn-alert card for Kamigold/Monteverde."""
        db = database.get()
        cfg = config.get()

        user = db.get_user_by_name(self.USER_NAME)
        machine = db.get_machine_by_name(self.MACHINE_NAME)
        if user is None or machine is None:
            logger.warning(
                f"EasterEggPoller: '{self.USER_NAME}' or '{self.MACHINE_NAME}' "
                "not found in DB, skipping this cycle"
            )
            return

        entry = {
            "user_id":           user["id"],
            "user_name":         user["name"],
            "user_avatar_url":   user["avatar_url"],
            "object_id":         machine["id"],
            "object_name":       machine["name"],
            "object_type":       "machine",
            "type":              "root",
            "points":            20,
            "first_blood":       True,
            "team_blood":        True,
            "is_season_machine": False,
            "category":          None,
            "object_avatar_url": machine["avatar_url"],
        }

        channel = client.get_channel(cfg.channel_id)
        if not channel:
            logger.warning(f"Channel {cfg.channel_id} not found")
            return

        buf = await build_pwn_image(entry, user["avatar_url"], channel.guild)
        await channel.send(file=discord.File(buf, "solve.png"))
        logger.info("EasterEggPoller: fired Kamigold/Monteverde root blood")


class ProfileSyncWorker:
    """Background queue that pulls each user's full HTB profile (basic info,
    pro labs, fortresses, and complete lifetime activity history) into the DB
    once, the first time they're seen.
    """

    def __init__(self):
        """Initializes an empty sync queue."""
        self._queue = asyncio.Queue()
        self._queued = set()
        self._current_user = None
        self._current_step = None

    def enqueue(self, user_id):
        """Queues a user for sync, unless they're already queued."""
        if user_id not in self._queued:
            self._queued.add(user_id)
            self._queue.put_nowait(user_id)
            logger.info(f"Queued profile sync for user {user_id}")

    def force_enqueue(self, user_id):
        """Queues a user for sync even if they're already queued (used to
        retry after a rate-limit backoff).
        """
        self._queued.discard(user_id)
        self.enqueue(user_id)

    def queue_size(self):
        """Number of users currently waiting to be synced."""
        return self._queue.qsize()

    def status(self):
        """Live one-line status: idle-with-queue-size, or which user/step is
        currently syncing plus how many remain queued.
        """
        if self._current_user is None:
            return f"Idle | Queue: {self.queue_size()} pending"
        return f"Syncing user {self._current_user} -- {self._current_step} | Queue: {self.queue_size()} remaining"

    def _set_step(self, step):
        """Updates the current sync step shown by status()."""
        self._current_step = step
        logger.debug(f"Sync step [{self._current_user}]: {step}")

    async def _htb_get(self, htb_api, *args, **kwargs):
        """Rate-limited HTB API GET, run off the event loop thread."""
        await _get_htb_limiter().acquire()
        return await asyncio.to_thread(htb_api.get, *args, **kwargs)

    async def sync_user(self, user_id, htb_api):
        """Pulls one user's basic profile, pro labs, fortresses, and full
        paginated activity history from HTB, upserting everything into the
        DB and caching every avatar encountered along the way. Marks the user
        as synced on success.
        """
        db = database.get()
        self._current_user = user_id
        logger.info(f"Starting profile sync for user {user_id}")

        self._set_step("basic profile")
        result = await self._htb_get(
            htb_api, f"user/profile/basic/{user_id}", api_version="v4"
        )
        if not result:
            logger.warning(f"Could not fetch basic profile for user {user_id}")
            return False
        profile = result.get("profile", {})
        db.upsert_user(profile)
        await cache.get_avatar(profile.get("avatar"))

        self._set_step("pro labs")
        result = await self._htb_get(
            htb_api, f"user/profile/progress/prolab/{user_id}", api_version="v4"
        )
        if result:
            for lab in result.get("profile", {}).get("prolabs", []):
                db.upsert_prolab(
                    lab["id"], lab["name"],
                    avatar_url=lab.get("avatar"),
                    identifier=lab.get("identifier"),
                    total_flags=lab.get("total_flags", 0),
                    total_machines=lab.get("total_machines", 0),
                )
                db.upsert_prolab_progress(
                    user_id, lab["id"],
                    owned_flags=lab.get("owned_flags", 0),
                    completion_percentage=lab.get("completion_percentage", 0),
                )
                await cache.get_avatar(lab.get("avatar"))
            db.commit()

        self._set_step("fortresses")
        result = await self._htb_get(
            htb_api, f"user/profile/progress/fortress/{user_id}", api_version="v4"
        )
        if result:
            for fort in result.get("profile", {}).get("fortresses", []):
                db.upsert_fortress(
                    fort["id"], fort["name"],
                    avatar_url=fort.get("avatar"),
                    total_flags=fort.get("total_flags", 0),
                )
                db.upsert_fortress_progress(
                    user_id, fort["id"],
                    owned_flags=fort.get("owned_flags", 0),
                    completion_percentage=fort.get("completion_percentage", 0),
                )
                await cache.get_avatar(fort.get("avatar"))
            db.commit()

        page = 1
        while True:
            self._set_step(f"activity page {page}/?")
            result = await self._htb_get(
                htb_api,
                f"user/profile/activity/{user_id}",
                api_version="v5",
                params={"per_page": 100, "page": page},
            )
            if not result:
                break

            last_page = result.get("meta", {}).get("lastPage", 1)
            self._set_step(f"activity page {page}/{last_page}")
            for item in result.get("data", []):
                item_type = item.get("type")
                item_id = item.get("id")
                name = item.get("name")
                avatar = item.get("avatar")
                own_date = item.get("ownDate")
                blood = item.get("blood", False)
                points = item.get("points", 0)
                category = item.get("categoryName")

                if item_type in ("user", "root"):
                    db.upsert_machine(item_id, name, avatar_url=avatar)
                    db.upsert_machine_solve(
                        user_id, item_id, item_type, own_date, blood, points
                    )
                elif item_type == "challenge":
                    if category in SHERLOCK_CATEGORIES:
                        db.upsert_sherlock(item_id, name, avatar_url=avatar, category=category)
                        db.upsert_sherlock_solve(user_id, item_id, own_date, blood, points)
                    else:
                        db.upsert_challenge(item_id, name, avatar_url=avatar, category=category)
                        db.upsert_challenge_solve(user_id, item_id, own_date, blood, points)
                await cache.get_avatar(avatar)

            db.commit()
            logger.debug(f"User {user_id}: synced activity page {page}/{last_page}")

            if page >= last_page:
                break
            page += 1

        db.mark_user_synced(user_id, datetime.now(timezone.utc).isoformat())
        logger.info(f"Profile sync complete for user {user_id}")
        return True

    async def _notify(self, client, msg):
        """Posts a plain text message to the configured pwn-alert channel."""
        channel = client.get_channel(config.get().channel_id)
        if channel:
            await channel.send(msg, allowed_mentions=discord.AllowedMentions.none())

    async def start(self, client, htb_api):
        """Continuously pulls user IDs off the queue and syncs them one at a
        time. On a rate-limit hit, warns the channel, backs off 5 minutes, and
        re-queues that user rather than losing the sync.
        """
        await client.wait_until_ready()
        while not client.is_closed():
            try:
                user_id = await asyncio.wait_for(self._queue.get(), timeout=5.0)
                rate_limited = False
                try:
                    await self.sync_user(user_id, htb_api)
                except RateLimitError:
                    rate_limited = True
                    logger.warning(f"Rate limited while syncing user {user_id} -- backing off 5 min")
                except Exception as e:
                    logger.error(f"Profile sync failed for user {user_id}: {e}")
                finally:
                    self._current_user = None
                    self._current_step = None
                    self._queued.discard(user_id)
                    self._queue.task_done()

                if rate_limited:
                    self._current_step = "rate limited -- backing off 5 min"
                    await self._notify(client, f"WARNING: Possible HTB rate limit hit while syncing user {user_id}. Backing off for 5 minutes.")
                    await asyncio.sleep(300)
                    self._current_step = None
                    self.enqueue(user_id)

            except asyncio.TimeoutError:
                pass
