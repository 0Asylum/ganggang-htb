from dataclasses import dataclass, asdict
import json
import os
import logging

logger = logging.getLogger(__name__)

_instance = None
_config_path = None


class ConfigError(Exception):
    """Raised for any config load/save/init failure."""
    pass


@dataclass
class Config:
    # Discord user ID of the bot owner (highest permission tier)
    owner_id: int = 218957491671793664
    # HTB team ID this bot tracks
    team_id: int = 8709
    # Discord channel pwn alerts are posted to (changeable at runtime via !setchannel)
    channel_id: int = 1518407815008358570
    # Discord server (guild) this bot is restricted to
    guild_id: int = 1500843126502195370
    # Base URL for HTB's API
    htb_base_url: str = "https://labs.hackthebox.com"
    user_agent: str = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
    # Per-user cooldown between commands, in seconds
    command_cooldown: int = 10
    # HTB API calls per minute
    htb_rate_limit: int = 10
    # Discord messages per minute (paranoid)
    discord_rate_limit: int = 5
    # Rolling window (days) for team-blood leaderboards (bloods/season). Points is exempt -- HTB computes that total itself.
    leaderboard_window_days: int = 90
    # How often (seconds) TeamActivityPoller re-fetches team/activity from HTB. It also
    # refreshes the season cache (season/list + season/machines) at the top of every poll
    # whenever that cache is stale (see poller._SEASON_CACHE_TTL, currently 900s), so a
    # single poll can cost up to 3 HTB calls, not just 1.
    #
    # Recommended: keep this at least ~3x (60 / htb_rate_limit) -- e.g. with the default
    # htb_rate_limit of 10/min (6s between calls), that floor is ~18s -- so one poll cycle
    # always has room to finish its calls through the shared rate limiter before the next
    # one starts. Going much lower risks this poller monopolizing the limiter and starving
    # ProfileSyncWorker (which shares it). Going higher just delays how fast new pwns get
    # announced -- no data is lost either way, HTB's activity feed is a 90-day window and
    # everything gets picked up on the next poll regardless of interval.
    poll_interval: int = 600
    # 1 = enable EasterEggPoller (Kamigold randomly first-bloods root on Monteverde
    # every ~2-4 days, purely cosmetic, no DB writes). 0 = disable it entirely.
    funnymode: int = 1


def config_exists(filename):
    """Returns True if a config file already exists at the given path."""
    return os.path.exists(filename)


def load_config(filename):
    """Reads and parses a Config from a JSON file, raising ConfigError on any
    read or parse failure.
    """
    try:
        with open(filename, "r") as f:
            data = json.load(f)
        return Config(**data)
    except (OSError, json.JSONDecodeError, TypeError) as e:
        raise ConfigError(f"Failed to load config '{filename}': {e}")


def write_config(filename, cfg):
    """Serializes a Config to JSON and writes it to the given path."""
    try:
        with open(filename, "w") as f:
            json.dump(asdict(cfg), f, indent=2)
    except OSError as e:
        raise ConfigError(f"Failed to write config '{filename}': {e}")


def create_config(filename):
    """Writes a fresh, default Config to the given path and returns it."""
    cfg = Config()
    write_config(filename, cfg)
    return cfg


_team_name = "GANGGANG"  # fallback used if the startup API fetch fails


def get_team_name():
    """Returns the team's display name, as last set by set_team_name()."""
    return _team_name


def set_team_name(name):
    """Updates the cached team display name (fetched from HTB at startup)."""
    global _team_name
    _team_name = name


def get():
    """Returns the active Config instance. Raises ConfigError if init() hasn't
    been called yet.
    """
    if _instance is None:
        raise ConfigError("Config has not been initialized. Call init() first.")
    return _instance


def init(filename):
    """Loads the config from the given path, or creates one with defaults if
    it doesn't exist yet. Must be called once before get() or save() are used.
    """
    global _instance, _config_path
    _config_path = filename
    try:
        if config_exists(filename):
            _instance = load_config(filename)
            logger.info(f"Loaded config from '{filename}'.")
        else:
            logger.info(f"Config '{filename}' not found, creating with defaults.")
            _instance = create_config(filename)
        return _instance
    except ConfigError:
        raise


def save():
    """Persists the current in-memory config back to the file it was loaded
    from (e.g. after a runtime change like !setchannel), so it survives a
    restart instead of reverting to whatever's on disk.
    """
    if _config_path is None:
        raise ConfigError("Config has not been initialized. Call init() first.")
    write_config(_config_path, get())
