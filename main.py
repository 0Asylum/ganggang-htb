import asyncio
import os
import argparse
from pathlib import Path
from dotenv import load_dotenv
import logging

from api_client import ApiClient
import discord
import commands
from commands import COMMANDS
import database
import config
import poller

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

logger = logging.getLogger(__name__)
htb_api = None
poll_tasks = {}
user_last_command = {}

POLLERS = []


class TokenError(Exception):
    """Raised when a required token is missing from the environment."""
    pass


def init_logger():
    """Configures root logging: DEBUG for this app's own loggers, INFO for
    the noisier discord.py/asyncio loggers, all to stdout.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    logging.getLogger("discord").setLevel(logging.INFO)
    logging.getLogger("asyncio").setLevel(logging.INFO)


def load_tokens():
    """Loads the Discord and HTB tokens from .env. Raises TokenError if
    either is missing.
    """
    logger.info("Loading tokens...")
    load_dotenv(dotenv_path=Path(".") / ".env")

    discord_token = os.getenv("DISCORD_TOKEN")
    if not discord_token:
        raise TokenError("Discord token not found. Is .env okay?")

    htb_token = os.getenv("HTB_TOKEN")
    if not htb_token:
        raise TokenError("HTB token not found. Is .env okay?")

    return discord_token, htb_token


@client.event
async def on_ready():
    """Starts each registered poller's loop once, when the Discord client
    first connects (or reconnects, guarded against starting duplicate tasks).
    """
    logger.info(f"Logged in as {client.user}")
    for p in POLLERS:
        key = type(p).__name__
        if key not in poll_tasks or poll_tasks[key].done():
            poll_tasks[key] = asyncio.create_task(p.start(client, htb_api))


@client.event
async def on_message(message):
    """Dispatches a "!command" message to its handler, after checking the
    guild restriction, per-user cooldown, and role requirement.
    """
    if message.author == client.user:
        return

    cfg = config.get()
    if cfg.guild_id and (message.guild is None or message.guild.id != cfg.guild_id):
        return

    if not message.content.startswith("!"):
        return

    command, _, data = message.content[1:].partition(" ")
    entry = COMMANDS.get(command)
    if not entry:
        return

    handler, min_role = entry

    now = asyncio.get_event_loop().time()
    user_id = message.author.id
    elapsed = now - user_last_command.get(user_id, 0)

    if commands.get_role(message) < commands.ROLE_OWNER and elapsed < cfg.command_cooldown:
        remaining = cfg.command_cooldown - elapsed
        logger.info(
            f"Command '{command}' from {message.author} ignored: {remaining:.1f}s cooldown remaining"
        )
        return

    if commands.get_role(message) < min_role:
        return

    user_last_command[user_id] = now
    await handler(message, data, htb_api)


def main():
    """Entry point: loads config/tokens/DB, fetches the team name, wires up
    the pollers, and starts the Discord client (blocks until it disconnects).
    """
    global htb_api
    init_logger()

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--database", default="database.db")
    args = parser.parse_args()

    try:
        config.init(args.config)
        discord_token, htb_token = load_tokens()
        database.init(args.database)
    except (config.ConfigError, TokenError, database.DatabaseError) as e:
        logger.critical(f"Failed to initialize: {e}")
        return

    cfg = config.get()
    database.get().set_discord_role(cfg.owner_id, commands.ROLE_OWNER)
    htb_api = ApiClient(
        base_url=cfg.htb_base_url, token=htb_token, user_agent=cfg.user_agent
    )

    team_info = htb_api.get(f"team/info/{cfg.team_id}", api_version="v4")
    if team_info and team_info.get("name"):
        config.set_team_name(team_info["name"])
        logger.info(f"Team name: {config.get_team_name()}")
    else:
        logger.warning("Failed to fetch team name; using fallback")

    sync_worker = poller.ProfileSyncWorker()
    poller.set_sync_worker(sync_worker)
    activity_poller = poller.TeamActivityPoller()
    poller.set_activity_poller(activity_poller)
    POLLERS.extend([
        activity_poller,
        poller.EasterEggPoller(),
        sync_worker,
    ])

    logger.info("Bot initialization complete.")
    client.run(discord_token, log_handler=None)


if __name__ == "__main__":
    main()
