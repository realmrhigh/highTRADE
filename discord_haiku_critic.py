#!/usr/bin/env python3
"""
Discord Haiku Critic Bot
Watches a channel for messages from Haiku Bot, deletes them, and posts
a Claude-generated spicy roast in their place.

Usage:
  python3 discord_haiku_critic.py           # Run the bot
  python3 discord_haiku_critic.py --setup   # Interactive token setup

Required Discord bot permissions:
  - Read Messages / View Channels
  - Send Messages
  - Manage Messages  (to delete Haiku Bot posts)

Required Discord Gateway Intents (Developer Portal → Bot):
  - Message Content Intent  (privileged — must be toggled ON)

Config is stored in trading_data/alert_config.json under channels.discord.
All values can be overridden with environment variables (see get_discord_cfg).
"""

import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path

import anthropic
import discord

# ─── Paths ────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent.resolve()
CONFIG_PATH = SCRIPT_DIR / "trading_data" / "alert_config.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("haiku_critic")


# ─── Config ───────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open() as fh:
            return json.load(fh)
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_PATH.open("w") as fh:
        json.dump(cfg, fh, indent=2)


def get_discord_cfg(cfg: dict) -> dict:
    """Merge file config with environment-variable overrides."""
    stored = cfg.get("channels", {}).get("discord", {})
    return {
        "bot_token":           os.environ.get("DISCORD_BOT_TOKEN")   or stored.get("bot_token", ""),
        "anthropic_api_key":   os.environ.get("ANTHROPIC_API_KEY")   or stored.get("anthropic_api_key", ""),
        "haiku_bot_name":      os.environ.get("HAIKU_BOT_NAME")      or stored.get("haiku_bot_name", "Haiku Bot"),
        "haiku_bot_id":        os.environ.get("HAIKU_BOT_ID")        or stored.get("haiku_bot_id", ""),
        "monitored_channel_ids": [
            int(x) for x in stored.get("monitored_channel_ids", [])
        ],
    }


# ─── Haiku parser ─────────────────────────────────────────────────────────────

def parse_haiku(raw: str) -> dict:
    """
    Strip Discord markdown and split into lines.

    Returns:
      lines      — up to 3 non-empty lines
      full_text  — lines joined with newlines
    """
    clean = re.sub(r"[*_`~|>]", "", raw).strip()
    lines = [ln.strip() for ln in re.split(r"\n+|/|\|", clean) if ln.strip()]
    return {
        "lines": lines[:3],
        "full_text": "\n".join(lines[:3]) or clean,
    }


# ─── Critique generator ───────────────────────────────────────────────────────

_SYSTEM = (
    "You are a razor-tongued poetry critic — equal parts Gordon Ramsay and "
    "Basho's disappointed ghost. Your job: demolish mediocre haiku with "
    "devastating wit, a surprise metaphor, and a flicker of genuine insight. "
    "2–4 sentences. Never cruel without being clever. Finish with a mic-drop line."
)

async def generate_critique(haiku: dict, api_key: str) -> str:
    """Call Claude Haiku asynchronously and return a roast string."""
    client = anthropic.AsyncAnthropic(api_key=api_key)

    user_msg = (
        "Roast this haiku with surgical precision — structural, thematic, or "
        "spiritual flaws welcome. Make a Tokyo tea master weep.\n\n"
        f"```\n{haiku['full_text']}\n```"
    )

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    return resp.content[0].text.strip()


# ─── Bot ──────────────────────────────────────────────────────────────────────

def build_bot(dcfg: dict) -> discord.Client:
    haiku_name: str        = dcfg["haiku_bot_name"].lower()
    haiku_id: int | None   = int(dcfg["haiku_bot_id"]) if dcfg["haiku_bot_id"] else None
    watch_channels: set    = set(dcfg["monitored_channel_ids"])
    api_key: str           = dcfg["anthropic_api_key"]

    intents = discord.Intents.default()
    intents.message_content = True   # privileged intent — enable in Developer Portal
    client = discord.Client(intents=intents)

    def _is_haiku_bot(msg: discord.Message) -> bool:
        if haiku_id and msg.author.id == haiku_id:
            return True
        name = getattr(msg.author, "display_name", "") or getattr(msg.author, "name", "")
        return name.lower() == haiku_name

    def _in_scope(msg: discord.Message) -> bool:
        return not watch_channels or msg.channel.id in watch_channels

    @client.event
    async def on_ready():
        scope = f"channels {sorted(watch_channels)}" if watch_channels else "all channels"
        log.info(f"Online as {client.user}  |  target='{haiku_name}' id={haiku_id or 'any'}  |  scope={scope}")
        print(f"\n{'='*60}\n  Haiku Critic ONLINE — {client.user}\n{'='*60}\n")

    @client.event
    async def on_message(msg: discord.Message):
        if msg.author == client.user:
            return
        if not _is_haiku_bot(msg) or not _in_scope(msg):
            return

        raw = (msg.content or "").strip()
        if not raw:
            return

        log.info(f"Haiku detected in #{msg.channel} (id={msg.id}): {raw!r}")

        haiku = parse_haiku(raw)

        # Generate critique — native async, no executor needed
        try:
            critique = await generate_critique(haiku, api_key)
        except anthropic.APIError as exc:
            log.error(f"Anthropic API error: {exc}")
            critique = (
                "A haiku so forgettable the algorithm rating it has already moved on. Next."
            )
        except Exception as exc:
            log.error(f"Critique generation failed: {exc}", exc_info=True)
            critique = "Five-seven-five syllables of profound nothing. Impressive in its own way."

        # Delete the original
        try:
            await msg.delete()
            log.info(f"Deleted message {msg.id}")
        except discord.Forbidden:
            log.warning("No 'Manage Messages' permission — skipping delete.")
        except discord.NotFound:
            pass  # already gone
        except discord.HTTPException as exc:
            log.error(f"Delete failed: {exc}")

        # Build reply: quoted haiku + roast
        quote = "\n".join(f"> {ln}" for ln in haiku["lines"]) if haiku["lines"] else f"> {raw}"
        reply = f"**A haiku arrived. I dispatched it.**\n{quote}\n\n{critique}"

        try:
            await msg.channel.send(reply)
            log.info(f"Critique posted in #{msg.channel}")
        except discord.Forbidden:
            log.error("No 'Send Messages' permission in this channel.")
        except discord.HTTPException as exc:
            log.error(f"Send failed: {exc}")

    return client


# ─── Setup wizard ─────────────────────────────────────────────────────────────

def setup() -> None:
    print("""
=================================================================
  Discord Haiku Critic — Setup
=================================================================

You need four things:

  1. Discord Bot Token
     • discord.com/developers/applications → New Application → Bot
     • Reset Token and copy it
     • Enable MESSAGE CONTENT INTENT under Privileged Gateway Intents
     • OAuth2 → URL Generator: scope=bot, permissions:
         Read Messages, Send Messages, Manage Messages
     • Invite the bot with that URL

  2. Haiku Bot's user ID  (most reliable; name matching also works)
     • Enable Developer Mode in Discord (User Settings → Advanced)
     • Right-click the Haiku Bot → Copy User ID

  3. Channel IDs to restrict monitoring  (optional — blank = all)
     • Right-click a channel → Copy Channel ID

  4. Anthropic API Key  (console.anthropic.com/settings/keys)
""")

    bot_token = input("Discord Bot Token: ").strip()
    if not bot_token:
        print("No token — aborting.")
        sys.exit(1)

    haiku_bot_name = input("Haiku Bot display name [Haiku Bot]: ").strip() or "Haiku Bot"
    haiku_bot_id   = input("Haiku Bot user ID (optional): ").strip()
    raw_channels   = input("Channel IDs, comma-separated (blank = all): ").strip()
    channel_ids    = [c.strip() for c in raw_channels.split(",") if c.strip()]
    anthropic_key  = input("Anthropic API Key: ").strip()

    cfg = load_config()
    cfg.setdefault("channels", {})
    cfg["channels"]["discord"] = {
        "bot_token":             bot_token,
        "haiku_bot_name":        haiku_bot_name,
        "haiku_bot_id":          haiku_bot_id,
        "monitored_channel_ids": channel_ids,
        "anthropic_api_key":     anthropic_key,
    }
    save_config(cfg)
    print(f"\nSaved to {CONFIG_PATH}\nRun the bot:  python3 discord_haiku_critic.py\n")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    if "--setup" in sys.argv:
        setup()
        return

    cfg    = load_config()
    dcfg   = get_discord_cfg(cfg)

    if not dcfg["bot_token"]:
        print("No Discord bot token. Run:  python3 discord_haiku_critic.py --setup")
        sys.exit(1)
    if not dcfg["anthropic_api_key"]:
        print("No Anthropic API key. Run:  python3 discord_haiku_critic.py --setup")
        sys.exit(1)

    bot = build_bot(dcfg)

    try:
        bot.run(dcfg["bot_token"], log_handler=None)
    except discord.LoginFailure:
        log.error("Invalid Discord bot token.")
        sys.exit(1)
    except KeyboardInterrupt:
        log.info("Stopped.")


if __name__ == "__main__":
    main()
