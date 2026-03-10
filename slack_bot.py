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
from pathlib import Path
from datetime import datetime

INFO_COMMANDS = {'/status', '/portfolio', '/defcon', '/trades', '/broker', '/help'}

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


def _normalize_command_token(token: str) -> str:
    token = (token or '').strip().lower()
    if not token:
        return ''
    if token.startswith('/'):  # already slash style
        return token
    return '/' + token


def _extract_command_text(text: str, bot_user_id: str | None = None) -> str:
    """Extract a command from plain text, slash-like text, or Slack bot mentions."""
    text = (text or '').strip()
    if not text:
        return ''

    # Support: <@U123> status, <@U123>: status, status
    mention_prefixes = []
    if bot_user_id:
        mention_prefixes.extend([
            f'<@{bot_user_id}>',
            f'<@{bot_user_id}>:',
        ])

    lowered = text.lower()
    for prefix in mention_prefixes:
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break

    if not text:
        return ''

    parts = text.split(None, 1)
    cmd_name = _normalize_command_token(parts[0])
    if cmd_name not in ALIAS_MAP:
        return ''

    return f"{cmd_name} {parts[1]}".strip() if len(parts) > 1 else cmd_name


def _discover_conversations(client, preferred_channel_id: str = '') -> list[dict]:
    """Find channel/DM conversations the bot can listen to."""
    conversations: dict[str, dict] = {}
    requested_type_sets = [
        'public_channel,private_channel,im,mpim',
        'public_channel',
    ]

    last_error = None
    for types in requested_type_sets:
        cursor = None
        try:
            while True:
                response = client.conversations_list(types=types, limit=500, cursor=cursor)
                for convo in response.get('channels', []):
                    convo_id = convo.get('id')
                    if not convo_id:
                        continue

                    is_dm = convo.get('is_im') or convo.get('is_mpim')
                    is_member = convo.get('is_member', False)
                    if convo_id == preferred_channel_id or is_dm or is_member:
                        conversations[convo_id] = convo

                cursor = response.get('response_metadata', {}).get('next_cursor')
                if not cursor:
                    break

            if types != requested_type_sets[0]:
                logger.warning(
                    'Slack app is missing some scopes; falling back to public-channel polling only. '
                    'Add groups/im/mpim scopes and reinstall the app for private channels and DMs.'
                )
            break
        except Exception as exc:
            last_error = exc
            if 'missing_scope' in str(exc) and types != requested_type_sets[-1]:
                continue
            raise

    if not conversations and last_error:
        logger.error(f"Conversation discovery failed: {last_error}")

    ordered = []
    if preferred_channel_id and preferred_channel_id in conversations:
        ordered.append(conversations.pop(preferred_channel_id))
    ordered.extend(sorted(conversations.values(), key=lambda c: c.get('name') or c.get('user') or c.get('id', '')))
    return ordered


def _conversation_label(conversation: dict) -> str:
    if conversation.get('is_im'):
        user = conversation.get('user') or conversation.get('id', '?')
        return f'DM:{user}'
    if conversation.get('is_mpim'):
        return conversation.get('name') or f"group-dm:{conversation.get('id', '?')}"
    return f"#{conversation.get('name', conversation.get('id', '?'))}"


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
    lines.append('Use them in any channel the bot has joined.')
    lines.append('If DM/private scopes are installed, the bot can also listen in private channels, group DMs, and direct messages.')
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

    RESPONSE_FILE.unlink(missing_ok=True)

    timeout = 20 if canonical in INFO_COMMANDS else 30

    # Atomic write
    tmp = CMD_FILE.with_suffix('.tmp')
    with open(tmp, 'w') as f:
        json.dump(payload, f, indent=2)
    tmp.rename(CMD_FILE)

    logger.info(f"Sent command to orchestrator: {canonical} {cmd_args}")

    # Wait for response
    response = _wait_for_response(timeout=timeout, expected_command=canonical)
    if response:
        return response
    else:
        return {'ok': True, 'message': f"Command `{canonical}` sent. Bot will process it shortly."}


def _wait_for_response(timeout: int, expected_command: str | None = None):
    start = time.time()
    while time.time() - start < timeout:
        if RESPONSE_FILE.exists():
            try:
                with open(RESPONSE_FILE, 'r') as f:
                    resp = json.load(f)

                response_command = resp.get('command')
                if expected_command and response_command and response_command != expected_command:
                    logger.warning(
                        f"Ignoring response for {response_command}; waiting for {expected_command}"
                    )
                    time.sleep(0.3)
                    continue

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

    # Find conversations to monitor
    slack_cfg = config.get('channels', {}).get('slack', {})
    preferred_channel_id = slack_cfg.get('channel_id', '')

    try:
        conversations = _discover_conversations(client, preferred_channel_id)
    except Exception as e:
        logger.error(f"Could not discover conversations: {e}")
        conversations = []

    if not conversations:
        print("❌ No Slack conversations found. Invite the bot to channels or DM it, then restart.")
        sys.exit(1)

    monitored_conversations = []
    for convo in conversations:
        convo_id = convo.get('id')
        label = _conversation_label(convo)
        try:
            if not (convo.get('is_im') or convo.get('is_mpim')):
                info = client.conversations_info(channel=convo_id)
                convo = info.get('channel', convo)
                if not convo.get('is_member', False):
                    logger.warning(f"Bot is not a member of {label}. Attempting to join...")
                    try:
                        client.conversations_join(channel=convo_id)
                        logger.info(f"Joined {label}")
                        refreshed = client.conversations_info(channel=convo_id)
                        convo = refreshed.get('channel', convo)
                    except Exception:
                        logger.warning(f"Could not auto-join {label}. Invite the bot there manually.")
            monitored_conversations.append(convo)
        except Exception as e:
            logger.warning(f"Could not verify conversation {label}: {e}")
            monitored_conversations.append(convo)

    conversation_names = ', '.join(_conversation_label(c) for c in monitored_conversations)

    print("\n" + "=" * 60)
    print("  HighTrade Slack Bot — ONLINE")
    print("=" * 60)
    print(f"  Mode:    Channel Polling (every 2s)")
    print(f"  Listening in: {conversation_names}")
    print(f"  Bot:     @{bot_name} ({bot_user_id})")
    print(f"  Type a command in any joined channel (e.g. 'status', 'hold', 'yes')")
    print("=" * 60 + "\n")

    logger.info(f"Polling Slack conversations for commands: {conversation_names}")

    # Send startup message
    startup_channel_id = monitored_conversations[0].get('id')
    try:
        client.chat_postMessage(
            channel=startup_channel_id,
            text=":robot_face: *HighTrade Bot is online and listening!*\n"
                 "Use commands in any joined channel the bot has joined.\n"
                 "Try: `status`, `portfolio`, `defcon`, `hold`, `yes`, `no`, `estop`\n"
                 "Type `help` for the full list."
        )
    except Exception as e:
        logger.warning(f"Could not send startup message: {e}")

    # ── Polling loop ──
    # Start reading from "now" so we don't replay old messages
    last_seen_ts = {convo.get('id'): str(time.time()) for convo in monitored_conversations}
    poll_interval = 2  # seconds
    logger.info("Starting multi-conversation poll loop")

    try:
        while True:
            try:
                for convo in monitored_conversations:
                    convo_id = convo.get('id')
                    result = client.conversations_history(
                        channel=convo_id,
                        oldest=last_seen_ts.get(convo_id, str(time.time())),
                        limit=20,
                    )

                    messages = result.get('messages', [])
                    if messages:
                        logger.info(f"Poll returned {len(messages)} message(s) from {_conversation_label(convo)}")

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

                        command_text = _extract_command_text(text, bot_user_id=bot_user_id)
                        if not command_text:
                            continue

                        user = msg.get('user', '?')
                        logger.info(f"Command from {user} in {_conversation_label(convo)}: {command_text}")

                        response = send_command_to_orchestrator(command_text)
                        reply = format_response_for_slack(response)

                        try:
                            post_args = {
                                'channel': convo_id,
                                'text': reply,
                            }
                            if not convo.get('is_im'):
                                post_args['thread_ts'] = ts
                            client.chat_postMessage(**post_args)
                            logger.info(
                                f"Posted reply in {_conversation_label(convo)}"
                                + (" thread" if post_args.get('thread_ts') else "")
                            )
                        except Exception as e:
                            logger.error(f"Failed to reply in {_conversation_label(convo)}: {e}")
                            if post_args.get('thread_ts'):
                                try:
                                    fallback_args = {
                                        'channel': convo_id,
                                        'text': reply,
                                    }
                                    client.chat_postMessage(**fallback_args)
                                    logger.info(f"Posted fallback non-thread reply in {_conversation_label(convo)}")
                                except Exception as fallback_error:
                                    logger.error(
                                        f"Fallback reply also failed in {_conversation_label(convo)}: {fallback_error}"
                                    )

                    for msg in messages:
                        ts = msg.get('ts', '')
                        if ts > last_seen_ts.get(convo_id, '0'):
                            last_seen_ts[convo_id] = ts

            except Exception as e:
                logger.error(f"Poll error: {e}")
                time.sleep(5)  # Back off on errors

            time.sleep(poll_interval)

    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
        try:
            client.chat_postMessage(
                channel=startup_channel_id,
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
      groups:history      (read private channels)
      groups:read         (list private channels)
      im:history          (read direct messages)
      im:read             (list direct messages)
      mpim:history        (read group DMs)
      mpim:read           (list group DMs)
      chat:write          (send messages)
      app_mentions:read   (respond to @mentions)

  STEP 3 — Enable Events
  ───────────────────────
  • Left sidebar → 'Event Subscriptions'
  • Toggle ON 'Enable Events'
  • Under 'Subscribe to bot events', add:
      message.channels
      message.groups
      message.im
      message.mpim
      app_mention
  • Click 'Save Changes'

  STEP 4 — Reinstall App
  ───────────────────────
  • Left sidebar → 'Install App'
  • Click 'Reinstall to Workspace'
  • Copy the Bot User OAuth Token (starts with xoxb-)

    STEP 5 — Invite bot where you want commands
  ────────────────────────────────────
    • In Slack, go to each channel you want to control the bot from
  • Type: /invite @HighTrade Broker
    • You can also DM the bot directly after installation
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
        print("  It will listen in every joined channel, group DM, and direct message.")
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
