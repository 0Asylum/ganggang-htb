import asyncio
import hashlib
import os
import subprocess
import logging
import requests

logger = logging.getLogger(__name__)

CACHE_DIR = "./cache/htb"
CACHE_DIR_DISCORD = "./cache/discord"


def _url_to_path(url, cache_dir=CACHE_DIR):
    """Maps a remote URL to its local cache file path: an MD5 hash of the URL,
    keeping the original extension (SVGs map to .png, since they're converted
    on download; anything unrecognized falls back to .bin).
    """
    name = hashlib.md5(url.encode()).hexdigest()
    ext = url.rsplit(".", 1)[-1].split("?")[0].lower()
    if not ext.isalnum() or len(ext) > 5:
        ext = "bin"
    if ext == "svg":
        ext = "png"  # SVGs are converted to PNG on download
    return os.path.join(cache_dir, f"{name}.{ext}")


def _svg_to_png(svg_data, png_path):
    """Converts raw SVG bytes to a PNG file via the rsvg-convert CLI tool.
    Returns False (without raising) if the conversion fails or rsvg-convert
    isn't installed, so a missing avatar never crashes the caller.
    """
    try:
        result = subprocess.run(
            ["rsvg-convert", "-w", "256", "-h", "256", "-o", png_path],
            input=svg_data,
            capture_output=True,
        )
        return result.returncode == 0
    except FileNotFoundError:
        logger.warning("rsvg-convert not found; SVG avatar skipped")
        return False


async def get_avatar(url, cache_dir=CACHE_DIR):
    """Downloads and locally caches the image at `url`, converting SVGs to PNG,
    and returns the local file path (or None if the URL is empty or the
    download/conversion fails). Repeat calls for the same URL are a cache hit
    and return immediately.

    The network fetch and the rsvg-convert subprocess are both blocking calls,
    so they run off the event loop thread via asyncio.to_thread -- otherwise a
    slow download or conversion would stall Discord command handling (or
    anything else on the loop) for its duration.
    """
    if not url:
        return None

    path = _url_to_path(url, cache_dir)
    if os.path.exists(path):
        return path

    os.makedirs(cache_dir, exist_ok=True)
    try:
        response = await asyncio.to_thread(requests.get, url, timeout=10)
        response.raise_for_status()

        raw_ext = url.rsplit(".", 1)[-1].split("?")[0].lower()
        if raw_ext == "svg":
            if not await asyncio.to_thread(_svg_to_png, response.content, path):
                return None
        else:
            with open(path, "wb") as f:
                f.write(response.content)

        logger.debug(f"Cached {url} -> {path}")
        return path
    except requests.RequestException as e:
        logger.warning(f"Failed to download avatar {url}: {e}")
        return None


async def get_discord_avatar(url):
    """Same as get_avatar(), but for Discord avatar URLs, cached in a separate
    directory from HTB avatars.

    Discord CDN URLs already carry a content hash (it changes whenever a user
    updates their picture), so this needs no extra staleness handling -- a
    changed avatar is naturally a new URL, hence a new cache entry. Kept
    separate from cache/htb and deliberately left out of !trimcache's orphan
    sweep, which only knows about DB-stored HTB URLs (Discord avatar URLs are
    intentionally never stored in the DB).
    """
    return await get_avatar(url, cache_dir=CACHE_DIR_DISCORD)
