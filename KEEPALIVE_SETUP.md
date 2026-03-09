# HighTrade launchd Keep-Alive Setup

## Current Status
✅ **All three services running via launchd** with automatic restart on crash.

| Service | Label | Log |
|---|---|---|
| Dashboard | `com.hightrade2.dashboard` | `logs/dashboard_srv.log` |
| Orchestrator | `com.hightrade2.orchestrator` | `logs/orchestrator_srv.log` |
| Slack Bot | `com.hightrade.slackbot` | `logs/slack_bot.log` |

Dashboard: http://localhost:5055 · Network: http://192.168.0.233:5055

---

## Start / Stop

```bash
./start_with_launchd.sh   # Start all three
./stop_launchd.sh         # Stop all three
```

```bash
# Restart a single service
launchctl kickstart -k gui/$(id -u)/com.hightrade2.orchestrator
launchctl kickstart -k gui/$(id -u)/com.hightrade2.dashboard
launchctl kickstart -k gui/$(id -u)/com.hightrade.slackbot

# Check status
launchctl list | grep hightrade

# Watch logs
tail -f logs/orchestrator_srv.log
tail -f logs/dashboard_srv.log
tail -f logs/slack_bot.log
```

---

## Root Causes Fixed (March 2026)

Three bugs caused all three services to fail. Documented here to prevent recurrence.

### 1. Slackbot using wrong Python
**Symptom**: Immediate crash, exit 78
**Cause**: Plist used `/usr/bin/python3` (system Python, no packages installed)
**Fix**: Changed to `/opt/homebrew/bin/python3.11`

### 2. Orchestrator using bash wrapper
**Symptom**: Exit 126 — "Operation not permitted" on getcwd
**Cause**: `/bin/bash` is blocked from accessing `~/Documents/` by macOS TCC (privacy framework) when launched via launchd. Python has the grant but bash does not.
**Fix**: Plist now invokes Python directly (same as highcrypto suite):
```
/opt/homebrew/bin/python3.11 hightrade_orchestrator.py continuous 15
```

### 3. Label/log-file poisoning after repeated crashes
**Symptom**: Exit 78 (`EX_CONFIG`) on every restart attempt, even after `bootout`/`bootstrap`/`kickstart`. No output in log files. 102+ crash cycles accumulated.
**Cause**: macOS launchd permanently associates crash state with a label+log-file combo. After many rapid crashes, this state is cached and cannot be cleared without a reboot or entirely new label/log names.
**Fix**: Renamed labels to `com.hightrade2.*` and log files to `*_srv.log`. These had no crash history and started immediately.

---

## If Services Break Again

**Do NOT** attempt:
- `launchctl unload/load` (deprecated, doesn't clear poisoned state)
- `launchctl bootout/bootstrap` with the same label (still poisoned)
- `launchctl kickstart -k` (doesn't clear label state)

**Do:**
1. Check which service is broken: `launchctl list | grep hightrade`
2. Check the log for the actual error
3. Fix the underlying bug
4. In the plist, change `Label` from `com.hightrade2.X` → `com.hightrade3.X`
5. Change `StandardOutPath`/`StandardErrorPath` to `logs/X_v3.log`
6. `launchctl bootout` the old label, then `launchctl bootstrap` the updated plist

---

## Plist Files

All three live in `~/Library/LaunchAgents/`:

| Plist filename | Label inside |
|---|---|
| `com.hightrade.dashboard.plist` | `com.hightrade2.dashboard` |
| `com.hightrade.orchestrator.plist` | `com.hightrade2.orchestrator` |
| `com.hightrade.slackbot.plist` | `com.hightrade.slackbot` |

The plist *filenames* kept the original naming; only the *Label* key and log paths were versioned.

### Key plist rules
- Orchestrator must invoke Python directly — **no bash wrapper**
- All services must use `/opt/homebrew/bin/python3.11` — **not system python**
- `HOME=/Users/stantonhigh` must be in `EnvironmentVariables`
- Log file paths must be **new (non-existent)** when bootstrapping after a label change

---

**Last Updated**: 2026-03-09
