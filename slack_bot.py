#!/usr/bin/env python3
"""
HighTrade Slack Bot — Two-Way Command Interface
Listens for messages in your Slack channel and routes them as slash commands
to the running orchestrator via the shared command file system.

Runs alongside the orchestrator as a separate process.

Usage:
  python3 slack_bot.py                  # Start the bot
  python3 slack_bot.py --setup          # Interactive token setup

Requires Socket Mode enabled on your Slack app.
See setup instructions: python3 slack_bot.py --setup
"""

import json
import sys
import os
import time
import logging
import threading
from pathlib import Path
from datetime import datetime

from hightrade_cmd import ALIAS_MAP as COMMAND_ALIAS_MAP, COMMANDS

# ─── Paths ────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent.resolve()
CONFIG_PATH = SCRIPT_DIR / 'trading_data' / 'alert_config.json'
CMD_DIR = SCRIPT_DIR / 'trading_data' / 'commands'
CMD_FILE = CMD_DIR / 'pending_command.json'
RESPONSE_FILE = CMD_DIR / 'command_response.json'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('slack_bot')

# ─── Config helpers ───────────────────────────────────────────

def load_config():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def save_config(config):
    with open(CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=2)


def get_slack_tokens(config):
    """Return (bot_token, app_token) from config or env vars."""
    slack_cfg = config.get('channels', {}).get('slack', {})
    bot_token = os.environ.get('SLACK_BOT_TOKEN') or slack_cfg.get('bot_token', '')
    app_token = os.environ.get('SLACK_APP_TOKEN') or slack_cfg.get('app_token', '')
    return bot_token, app_token


# ─── Command routing ─────────────────────────────────────────

KNOWN_COMMANDS = set(COMMAND_ALIAS_MAP.keys())
ALIAS_MAP = COMMAND_ALIAS_MAP


def _build_help_text() -> str:
    category_titles = {
        'decisions': 'Decisions',
        'control': 'Control',
        'info': 'Info',
        'config': 'Config',
    }

    lines = ['*HighTrade Commands*']
    for category in ('decisions', 'control', 'info', 'config'):
        category_commands = [
            (cmd, meta) for cmd, meta in COMMANDS.items()
            if meta.get('category') == category
        ]
        if not category_commands:
            continue

        lines.append('')
        lines.append(f"*{category_titles[category]}*")
        for cmd, meta in category_commands:
            lines.append(f"`{cmd}` — {meta['description']}")

    lines.append('')
    lines.append('You can type commands with or without the leading `/` in Slack.')
    return '\n'.join(lines)


HELP_TEXT = _build_help_text()


def send_command_to_orchestrator(raw_text: str) -> dict:
    """Write command to shared file and wait for response."""
    CMD_DIR.mkdir(parents=True, exist_ok=True)

    parts = raw_text.strip().split(None, 1)
    cmd_name = parts[0].lower() if parts else ''
    cmd_args = parts[1] if len(parts) > 1 else ''

    # Normalize: add / if missing
    if not cmd_name.startswith('/'):
        cmd_name = '/' + cmd_name

    # Resolve alias
    canonical = ALIAS_MAP.get(cmd_name)
    if not canonical:
        return {'ok': False, 'message': f"Unknown command: `{cmd_name}`\nType `/help` for available commands."}

    # /help handled locally
    if canonical == '/help':
        return {'ok': True, 'message': HELP_TEXT, 'local': True}

    payload = {
        'command': canonical,
        'args': cmd_args,
        'timestamp': datetime.now().isoformat(),
        'raw': raw_text.strip(),
        'source': 'slack',
    }

    # Atomic write
    tmp = CMD_FILE.with_suffix('.tmp')
    with open(tmp, 'w') as f:
        json.dump(payload, f, indent=2)
    tmp.rename(CMD_FILE)

    logger.info(f"Sent command to orchestrator: {canonical} {cmd_args}")

    # Wait for response
    response = _wait_for_response(timeout=15)
    if response:
        return response
    else:
        return {'ok': True, 'message': f"Command `{canonical}` sent. Bot will process it shortly."}


def _wait_for_response(timeout: int):
    start = time.time()
    while time.time() - start < timeout:
        if RESPONSE_FILE.exists():
            try:
                with open(RESPONSE_FILE, 'r') as f:
                    resp = json.load(f)
                RESPONSE_FILE.unlink(missing_ok=True)
                return resp
            except (json.JSONDecodeError, IOError):
                pass
        time.sleep(0.3)
    return None


def format_response_for_slack(response: dict) -> str:
    """Convert command response dict into a nice Slack message."""
    icon = ":white_check_mark:" if response.get('ok') else ":x:"
    msg = response.get('message', 'Done')

    lines = [f"{icon}  *{msg}*"]

    data = response.get('data')
    if data:
        if isinstance(data, dict):
            for k, v in data.items():
                lines.append(f"  *{k}:*  {v}")
        elif isinstance(data, list):
            for item in data:
                lines.append(f"  {item}")
        elif isinstance(data, str):
            lines.append(data)

    if response.get('warning'):
        lines.append(f":warning:  {response['warning']}")

    return '\n'.join(lines)


# ─── Slack Bot (Polling Mode) ─────────────────────────────────

def start_bot():
    """Start the Slack bot using channel polling.

    Polls the channel every 2 seconds for new messages.
    Only requires bot_token with channels:history + chat:write scopes.
    No Event Subscriptions or Socket Mode config needed.
    """
    from slack_sdk import WebClient

    config = load_config()
    bot_token, _ = get_slack_tokens(config)

    if not bot_token:
        print("\n" + "=" * 60)
        print("  Slack Bot token not configured!")
        print("=" * 60)
        print("\nRun:  python3 slack_bot.py --setup")
        sys.exit(1)

    client = WebClient(token=bot_token)

    # Identify ourselves
    auth = client.auth_test()
    bot_user_id = auth['user_id']
    bot_name = auth['user']
    logger.info(f"Authenticated as {bot_name} ({bot_user_id})")

    # Find the channel to monitor
    slack_cfg = config.get('channels', {}).get('slack', {})
    channel_id = slack_cfg.get('channel_id', '')

    if not channel_id:
        # Auto-detect: find channels the bot is a member of
        try:
            resp = client.conversations_list(types='public_channel')
            for ch in resp['channels']:
                if ch.get('is_member'):
                    channel_id = ch['id']
                    channel_name = ch['name']
                    break
            if not channel_id:
                # Try all channels and pick one we know about
                for ch in resp['channels']:
                    channel_id = ch['id']
                    channel_name = ch['name']
                    break
        except Exception as e:
            logger.error(f"Could not list channels: {e}")

    if not channel_id:
        print("❌ No channel found. Set 'channel_id' in alert_config.json under slack.")
        sys.exit(1)

    # Verify membership
    try:
        info = client.conversations_info(channel=channel_id)
        channel_name = info['channel']['name']
        is_member = info['channel'].get('is_member', False)
        if not is_member:
            logger.warning(f"Bot is NOT a member of #{channel_name}. Attempting to join...")
            try:
                client.conversations_join(channel=channel_id)
                logger.info(f"Joined #{channel_name}")
            except Exception:
                logger.warning(f"Could not auto-join. Invite the bot: /invite @{bot_name}")
    except Exception as e:
        channel_name = channel_id
        logger.warning(f"Could not verify channel: {e}")

    print("\n" + "=" * 60)
    print("  HighTrade Slack Bot — ONLINE")
    print("=" * 60)
    print(f"  Mode:    Channel Polling (every 2s)")
    print(f"  Channel: #{channel_name} ({channel_id})")
    print(f"  Bot:     @{bot_name} ({bot_user_id})")
    print(f"  Type a command in Slack (e.g. 'status', 'hold', 'yes')")
    print("=" * 60 + "\n")

    logger.info(f"Polling #{channel_name} ({channel_id}) for commands")

    # Send startup message
    try:
        client.chat_postMessage(
            channel=channel_id,
            text=":robot_face: *HighTrade Bot is online and listening!*\n"
                 "Type a command: `status`, `portfolio`, `defcon`, `hold`, `yes`, `no`, `estop`\n"
                 "Type `help` for the full list."
        )
    except Exception as e:
        logger.warning(f"Could not send startup message: {e}")

    # ── Polling loop ──
    # Start reading from "now" so we don't replay old messages
    last_ts = str(time.time())
    poll_interval = 2  # seconds
    logger.info(f"Starting poll loop with last_ts={last_ts}")

    try:
        while True:
            try:
                result = client.conversations_history(
                    channel=channel_id,
                    oldest=last_ts,
                    limit=10,
                )

                messages = result.get('messages', [])
                if messages:
                    logger.info(f"Poll returned {len(messages)} message(s)")

                # Process oldest first, then advance cursor
                for msg in reversed(messages):
                    ts = msg.get('ts', '')

                    # Skip bot messages / subtypes
                    if msg.get('bot_id') or msg.get('subtype'):
                        continue
                    if msg.get('user') == bot_user_id:
                        continue

                    text = (msg.get('text') or '').strip()
                    if not text:
                        continue

                    # Check if it's a command
                    first_word = text.split()[0].lower()
                    if not first_word.startswith('/'):
                        first_word = '/' + first_word

                    if first_word not in KNOWN_COMMANDS and first_word not in ALIAS_MAP:
                        continue

                    user = msg.get('user', '?')
                    logger.info(f"Command from {user}: {text}")

                    # Route command
                    response = send_command_to_orchestrator(text)
                    reply = format_response_for_slack(response)

                    # Reply in thread
                    try:
                        client.chat_postMessage(
                            channel=channel_id,
                            text=reply,
                            thread_ts=ts,
                        )
                    except Exception as e:
                        logger.error(f"Failed to reply: {e}")

                # Advance cursor past all messages we just read
                for msg in messages:
                    ts = msg.get('ts', '')
                    if ts > last_ts:
                        last_ts = ts

            except Exception as e:
                logger.error(f"Poll error: {e}")
                time.sleep(5)  # Back off on errors

            time.sleep(poll_interval)

    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
        try:
            client.chat_postMessage(
                channel=channel_id,
                text=":octagonal_sign: *HighTrade Bot going offline.*"
            )
        except Exception:
            pass


# ─── Setup wizard ─────────────────────────────────────────────

def setup():
    """Interactive setup to configure Slack bot tokens."""
    print("\n" + "=" * 60)
    print("  HighTrade Slack Bot — Setup")
    print("=" * 60)

    print("""
Your existing Slack app (HighTrade Broker) needs two more things
to receive messages back from you:

  1. A Bot Token    (xoxb-...)
  2. An App Token   (xapp-...)

Follow these steps in https://api.slack.com/apps :

  STEP 1 — Enable Socket Mode
  ────────────────────────────
  • Open your 'HighTrade Broker' app
  • Left sidebar → 'Socket Mode'
  • Toggle ON 'Enable Socket Mode'
  • Give the token a name (e.g. 'hightrade-socket')
  • Copy the App-Level Token (starts with xapp-)

  STEP 2 — Add Bot Scopes
  ────────────────────────
  • Left sidebar → 'OAuth & Permissions'
  • Scroll to 'Bot Token Scopes'
  • Add these scopes:
      channels:history    (read messages)
      channels:read       (list channels)
      chat:write          (send messages)
      app_mentions:read   (respond to @mentions)

  STEP 3 — Enable Events
  ───────────────────────
  • Left sidebar → 'Event Subscriptions'
  • Toggle ON 'Enable Events'
  • Under 'Subscribe to bot events', add:
      message.channels
      app_mention
  • Click 'Save Changes'

  STEP 4 — Reinstall App
  ───────────────────────
  • Left sidebar → 'Install App'
  • Click 'Reinstall to Workspace'
  • Copy the Bot User OAuth Token (starts with xoxb-)

  STEP 5 — Invite bot to your channel
  ────────────────────────────────────
  • In Slack, go to your #trading channel
  • Type: /invite @HighTrade Broker
""")

    app_token = input("  Paste your App-Level Token (xapp-...): ").strip()
    if not app_token.startswith('xapp-'):
        print("  ❌ Invalid app token. Must start with xapp-")
        return False

    bot_token = input("  Paste your Bot Token (xoxb-...): ").strip()
    if not bot_token.startswith('xoxb-'):
        print("  ❌ Invalid bot token. Must start with xoxb-")
        return False

    # Save to config
    config = load_config()
    if 'channels' not in config:
        config['channels'] = {}
    if 'slack' not in config['channels']:
        config['channels']['slack'] = {}

    config['channels']['slack']['bot_token'] = bot_token
    config['channels']['slack']['app_token'] = app_token
    save_config(config)

    print("\n  ✅ Tokens saved to alert_config.json")

    # Quick test
    print("\n  🧪 Testing connection...")
    try:
        from slack_sdk import WebClient
        client = WebClient(token=bot_token)
        auth = client.auth_test()
        print(f"  ✅ Connected as: {auth['user']} in workspace {auth['team']}")
        print(f"\n  Start the bot:  python3 slack_bot.py")
        return True
    except Exception as e:
        print(f"  ⚠️  Connection test failed: {e}")
        print("  Tokens saved anyway — double-check scopes and reinstall.")
        return False


# ─── Main ─────────────────────────────────────────────────────

if __name__ == '__main__':
    if '--setup' in sys.argv:
        setup()
    else:
        start_bot()
