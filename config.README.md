# config.json reference

`config.json` is loaded once at startup (`config.init()` in `main.py`). It's plain
JSON, so it can't hold comments — this file documents what each key means and how
to size it. Field definitions/defaults live in the `Config` dataclass in `config.py`;
if a key is missing from `config.json`, the default there is used.

Restart the bot after editing any of these — nothing is hot-reloaded.

## Identity / wiring

- **`owner_id`** (int) — Discord user ID with `ROLE_OWNER`. Full access, including
  destructive commands like `!purgeuser`. Currently asylum.
- **`team_id`** (int) — HTB team ID this bot tracks. GANGGANG is `8709`.
- **`channel_id`** (int) — Discord channel ID pwn/blood announcements get posted to.
- **`guild_id`** (int) — Discord server ID the bot is scoped to. If a message comes
  from any other guild (or a DM), `on_message` ignores it.
- **`htb_base_url`** (str) — HTB API base URL. `https://labs.hackthebox.com`. No
  reason to change this unless HTB restructures their API domain.
- **`user_agent`** (str) — sent on every HTB API request. Just needs to look like a
  normal browser UA; not security-sensitive.

## Rate limiting / cooldowns

- **`command_cooldown`** (int, seconds) — per-user cooldown between Discord command
  invocations. Doesn't apply to the owner. Exists to stop accidental spam, not a
  security control.
- **`htb_rate_limit`** (int, calls/minute) — global cap on outbound HTB API calls,
  shared by every poller and command that hits the API (`TeamActivityPoller`,
  `ProfileSyncWorker`, `!leaderboard season`'s season-cache refresh, etc). Enforced
  by a single shared token-bucket-style limiter (`poller._RateLimiter`) — one call
  every `60 / htb_rate_limit` seconds, no bursting. Raise this only if you have a
  concrete reason to believe HTB's actual limit is higher than 10/min; lowering it
  just slows everything down proportionally.
- **`discord_rate_limit`** (int, messages/minute) — throttles how fast queued pwn
  announcements get posted to Discord when several land in the same poll. Set
  conservatively (default 5) to stay well clear of Discord's own rate limits.

## Leaderboards

- **`leaderboard_window_days`** (int, days) — rolling lookback window for
  `!leaderboard bloods` and `!leaderboard season`. Both query "team bloods in the
  last N days" and both show `Last N days` in the card subtitle, so the two always
  stay in sync with whatever this is set to. `!leaderboard points` is **not**
  affected — that's HTB's own all-time point total, computed on their end, no local
  windowing involved.

## Polling

- **`poll_interval`** (int, seconds, default 600) — how often `TeamActivityPoller`
  re-fetches `team/activity` from HTB. At the top of every poll it also checks the
  season cache (`season/list` + `season/machines/{id}`, see `poller._SEASON_CACHE_TTL`,
  currently 900s) and refreshes it first if stale — that ordering is deliberate, so a
  season transition is always reflected before that same cycle's new solves get
  tagged season/non-season. This means a single poll can cost up to **3** HTB calls
  (season/list, season/machines, team/activity), not just 1.

  **Recommended floor:** at least `~3 * (60 / htb_rate_limit)` seconds — e.g. with
  the default `htb_rate_limit` of 10/min (6s between calls), that's ~18s — so one
  poll's calls always finish clearing the shared rate limiter before the next poll
  starts.

  **Why not go lower:** this poller and `ProfileSyncWorker` (which resyncs a user's
  full profile/history after a purge or a first-time-seen user) share the same rate
  limiter. If `TeamActivityPoller` polls too aggressively it monopolizes the
  limiter and profile syncs get starved out, queuing up indefinitely.

  **Why not worry about going higher:** no data is ever lost by polling less often
  — HTB's `team/activity` endpoint returns a 90-day window every time, so anything
  missed on one poll gets picked up on the next regardless of interval. A higher
  value only delays how quickly new pwns get announced in Discord.
