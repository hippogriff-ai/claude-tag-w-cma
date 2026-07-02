#!/usr/bin/env python3
"""
setup_check.py — is weekend-window ready to run on Slack?

Prints a ✓/✗ checklist and NEVER reveals a secret (only presence, prefix, and — if
the bot token is set — the result of Slack's auth.test, which shows the workspace
and bot name but not the token). Exit 0 when Slack is ready.

    python setup_check.py
"""
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ENV = os.path.join(HERE, ".env.local")


def load_env(path):
    if not os.path.exists(path):
        return None
    vals = {}
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip('"').strip("'")
        if v.startswith("#"):
            v = ""
        vals[k.strip()] = v
    return vals


def mark(ok):
    return "✓" if ok else "✗"


def check_prefix(vals, key, prefix, required=True):
    v = vals.get(key, "")
    good = bool(v) and v.startswith(prefix)
    if not v:
        detail = "missing" + ("" if required else "  (optional for the Slack demo)")
    elif good:
        detail = "set ✓"
    else:
        detail = f"set but doesn't start with {prefix!r}"
    print(f"  {mark(good or (not required and not v))} {key:18s} {detail}")
    return good


def slack_auth_test(token):
    req = urllib.request.Request(
        "https://slack.com/api/auth.test", data=b"",
        headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def main():
    print("weekend-window setup check\n" + "-" * 40)
    vals = load_env(ENV)
    if vals is None:
        print("  ✗ .env.local not found")
        print("    → create it (the /setup skill will) with SLACK_BOT_TOKEN / SLACK_APP_TOKEN")
        return 1
    print("  ✓ .env.local found")

    bot = check_prefix(vals, "SLACK_BOT_TOKEN", "xoxb-")
    app = check_prefix(vals, "SLACK_APP_TOKEN", "xapp-")
    check_prefix(vals, "ANTHROPIC_API_KEY", "sk-ant-", required=False)

    try:
        import slack_bolt  # noqa: F401
        print("  ✓ slack_bolt        installed")
    except ImportError:
        print("  ✗ slack_bolt        not installed  → pip install slack_bolt aiohttp")

    slack_ready = bot and app
    if bot:
        try:
            r = slack_auth_test(vals["SLACK_BOT_TOKEN"])
            if r.get("ok"):
                print(f"  ✓ Slack auth.test   workspace {r.get('team')!r}, bot {r.get('user')!r}")
            else:
                print(f"  ✗ Slack auth.test   error: {r.get('error')}  (re-copy the bot token after Install)")
                slack_ready = False
        except Exception as e:
            print(f"  ⚠ Slack auth.test   couldn't reach Slack ({e})")

    print("-" * 40)
    if slack_ready:
        print("  → Slack is ready.  Run:  python slack_app.py")
        return 0
    print("  → Not ready yet — fill the ✗ items above. (The /setup skill walks you through each.)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
