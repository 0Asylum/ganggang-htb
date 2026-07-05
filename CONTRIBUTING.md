# Contributing to ggasylum

This bot can't be developed against the production Discord server or database --
you won't have the production `.env`, and even if you did, testing against the
live server would spam real users with fake solve alerts. Every contributor
needs their own throwaway Discord bot, test server, and HTB team to develop
and test against. This doc walks through that setup, then the actual
contribution workflow.

## 1. Set up your own test environment

### Discord bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
   and create a new application, then add a Bot user to it.
2. Under the bot's settings, enable the **Message Content Intent** (under
   Privileged Gateway Intents) -- the bot reads `!command` text out of message
   content, and won't receive it without this enabled.
3. Copy the bot's token. You'll need it for `.env` in step 4.
4. Under OAuth2 > URL Generator, select the `bot` scope and at least
   `Send Messages` + `Attach Files` permissions, then use the generated URL to
   invite your bot to a **private test server** of your own (create one if you
   don't have one -- it costs nothing and takes a minute).

### HTB API token and team

1. Generate a personal API token from your HTB account settings.
2. You'll need an HTB team to poll activity from. Use one you're already in,
   or create a throwaway one -- either works for testing. Note its team ID
   (visible in the team's page URL).

### Local setup

```
git clone <your fork's URL>
cd ggasylum
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

You'll also need `rsvg-convert` installed system-wide (used to convert SVG
avatars to PNG) -- e.g. `librsvg2-bin` on Debian/Ubuntu, `librsvg` on Arch or
Homebrew.

Copy `.env.example` to `.env` and fill in the two tokens from above:

```
cp .env.example .env
```

### First run and config

Run the bot once to generate a fresh `config.json`:

```
python3 main.py
```

**Important:** `config.json` is gitignored (it's instance-specific) and gets
created with the production server's IDs as placeholder defaults. Stop the
bot, then edit `config.json` and replace these with your own test values,
or the bot will be configured for a server it isn't in and won't respond to
anything:

- `owner_id` -- your own Discord user ID
- `team_id` -- your test HTB team's ID
- `channel_id` -- a channel ID in your test server
- `guild_id` -- your test server's ID

Run it again after editing, and it should log in and start responding to
commands in your test server.

## 2. Making a change

1. Branch off `main`: `git checkout -b feature/short-description` (or
   `fix/short-description` for a bug fix).
2. Make your change. A few conventions this codebase follows:
   - Every function has a short docstring explaining what it does.
   - Comments explain *why*, not *what* -- if the code already makes the
     "what" obvious, it doesn't need a comment restating it.
   - Don't add abstractions, config options, or error handling for cases that
     can't actually happen -- keep changes scoped to what they're solving.
3. Test it against your own server before opening a PR. At minimum, exercise
   the actual command/flow you changed in Discord -- don't rely on "it
   compiles" alone.
4. Never commit `.env`, `config.json`, `database.db`, or anything under
   `cache/` -- all instance-specific, all already gitignored. If you're ever
   unsure whether something should be committed, ask first.

## 3. Opening a pull request

- Target `main`.
- Describe what changed and *why* in the PR description -- not just a
  restatement of the diff.
- Note what you tested and how (e.g. "ran `!leaderboard season` against my
  test team, confirmed the image renders").
- Keep PRs scoped to one change -- a bug fix and an unrelated refactor should
  be two PRs, not one.

Expect at least one review before merge. `main` is the branch the production
bot actually runs from, so anything landing there should be something you'd
be comfortable seeing show up live.
