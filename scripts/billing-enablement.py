#!/usr/bin/env python3
"""
Make sure this workshop has a Google Cloud project with billing on it.

Adapted from gca-americas/way-back-home, scripts/billing-enablement.py. Two
things changed for this repo:

  * It can CREATE the project, not just bill an existing one.
  * The account heuristic looks for CDP workshop credits, which arrive named
    "[2026-08-27]CDP Credit: 01ABCD-234567-89EFGH". Newest date wins.

Two kinds of room run this workshop:

  Google Skills   hands you a project and a billing account before you type
                  anything, and exports GOOGLE_CLOUD_PROJECT and
                  GOOGLE_CLOUD_REGION. Nothing here needs to happen — we
                  confirm billing is on and get out of the way.

  Anywhere else   your laptop, your own account. We create a project called
                  long-running-<10 random chars> and attach the newest CDP
                  credit we can find.

Everything goes through `gcloud` rather than the google-cloud-billing library,
which is what the original used. gcloud is already a hard requirement of
setup.sh, and it carries its own credentials and quota project — which sidesteps
a chicken-and-egg the library version has here: a freshly created project cannot
be its own quota project until its APIs are on, and its APIs cannot come on
until it has billing.

Kept from the original, because workshops are where these actually bite:
propagation backoff after enabling an API, a waiting loop for credits that were
claimed thirty seconds ago, and verifying the link rather than trusting it.

  stdout   exactly one machine-readable line, for setup.sh:   PROJECT=<id>
  stderr   everything a human reads

Usage:
    python3 scripts/billing-enablement.py --project my-existing-project
    python3 scripts/billing-enablement.py --create
"""

import argparse
import json
import random
import re
import string
import subprocess
import sys
import time
from datetime import date

# "[2026-08-27]CDP Credit: 01ABCD-234567-89EFGH" — the date is the only part we
# can order on, and on a multi-day workshop the newest one is the live credit.
CDP_PATTERN = re.compile(r"^\[(\d{4})-(\d{2})-(\d{2})\]\s*CDP\s*Credit", re.I)

PROJECT_PREFIX = "long-running-"
# Project ids are 6-30 chars, lowercase letters/digits/hyphens, must start with a
# letter. 13 + 10 = 23, comfortably inside that.
ID_ALPHABET = string.ascii_lowercase + string.digits


def say(*args):
    """Humans read stderr. stdout is reserved for the PROJECT= line."""
    print(*args, file=sys.stderr, flush=True)


def run(args, timeout=120):
    """Run a gcloud command. Returns (ok, stdout, stderr) and never raises."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, p.stdout.strip(), p.stderr.strip()
    except FileNotFoundError:
        return False, "", "gcloud not found"
    except subprocess.TimeoutExpired:
        return False, "", f"timed out after {timeout}s"


def run_json(args, timeout=120):
    ok, out, err = run(args + ["--format=json"], timeout=timeout)
    if not ok:
        return None, err
    try:
        return json.loads(out or "null"), ""
    except json.JSONDecodeError:
        return None, f"could not parse gcloud output: {out[:200]}"


# --- account -------------------------------------------------------------


def active_account():
    """Who gcloud will act as. Creating a project needs a logged-in human."""
    ok, out, _ = run(
        ["gcloud", "auth", "list", "--filter=status:ACTIVE",
         "--format=value(account)"],
        timeout=30,
    )
    return out.splitlines()[0].strip() if ok and out.strip() else ""


# --- project -------------------------------------------------------------


def new_project_id():
    tail = "".join(random.choices(ID_ALPHABET, k=10))
    return f"{PROJECT_PREFIX}{tail}"


def project_exists(project_id):
    ok, _, _ = run(
        ["gcloud", "projects", "describe", project_id, "--format=value(projectId)"],
        timeout=60,
    )
    return ok


def create_project():
    """Create long-running-<random>, retrying on the rare id collision."""
    for attempt in range(5):
        project_id = new_project_id()
        say(f"   Creating project {project_id} ...")
        ok, _, err = run(
            ["gcloud", "projects", "create", project_id,
             "--name=Long Running Agents Workshop", "--quiet"],
            timeout=180,
        )
        if ok:
            say(f"   ✓ Created {project_id}")
            return project_id

        low = err.lower()
        if "already in use" in low or "already exists" in low:
            # Project ids are globally unique, so somebody else may hold ours.
            say("   (id taken, trying another)")
            continue

        say(f"   ❌ Could not create a project: {err.splitlines()[0] if err else '?'}")
        if "permission" in low or "denied" in low:
            say("")
            say("   Your account may not be allowed to create projects — that is")
            say("   common on a corporate or school account. Two ways forward:")
            say("")
            say("     • use a project you already have:")
            say("           GOOGLE_CLOUD_PROJECT=your-project ./setup.sh")
            say("     • or create one by hand and pass it in:")
            say("           https://console.cloud.google.com/projectcreate")
            say("")
        elif "parent" in low or "organization" in low:
            say("")
            say("   Your organization requires new projects to name a parent")
            say("   folder or org. Create one in the console and pass it in:")
            say("")
            say("       GOOGLE_CLOUD_PROJECT=your-project ./setup.sh")
            say("")
        return None

    say("   ❌ Could not find a free project id after 5 tries.")
    return None


# --- billing -------------------------------------------------------------


def billing_is_enabled(project_id):
    """(enabled, account_name). Unknown answers count as not enabled."""
    data, _ = run_json(["gcloud", "billing", "projects", "describe", project_id],
                       timeout=60)
    if isinstance(data, dict):
        return bool(data.get("billingEnabled")), data.get("billingAccountName", "")
    return False, ""


def enable_billing_api(project_id):
    say("   Enabling Cloud Billing API...")
    ok, _, err = run(
        ["gcloud", "services", "enable", "cloudbilling.googleapis.com",
         "--project", project_id, "--quiet"],
        timeout=180,
    )
    if ok:
        say("   ✓ Cloud Billing API enabled")
    else:
        say(f"   ❌ Error enabling API: {err.splitlines()[0] if err else '?'}")
    return ok


def list_billing_accounts():
    """Open billing accounts this account can see.

    Returns (accounts, status) where status is "ok", "api", "denied" or "error".
    The "api" case is recoverable — the Cloud Billing API is on but has not
    propagated yet, which is normal seconds after enabling it.
    """
    data, err = run_json(["gcloud", "billing", "accounts", "list"], timeout=90)
    if data is None:
        low = err.lower()
        if "has not been used" in low or "is disabled" in low or "service_disabled" in low:
            return [], "api"
        if "permission" in low or "denied" in low or "forbidden" in low:
            say(f"   ❌ Permission denied: {err.splitlines()[0] if err else '?'}")
            return [], "denied"
        say(f"   ❌ Unexpected error: {err.splitlines()[0] if err else '?'}")
        return [], "error"
    return [a for a in data if a.get("open")], "ok"


def credit_date(display_name):
    """The date out of "[2026-08-27]CDP Credit: ...", or None if not a credit."""
    m = CDP_PATTERN.match(display_name or "")
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def linked_project_count(account_name):
    """How many projects already bill to this account. -1 if we cannot tell."""
    data, _ = run_json(
        ["gcloud", "billing", "projects", "list", "--billing-account", account_name],
        timeout=90,
    )
    return len(data) if isinstance(data, list) else -1


def pick_billing_account(accounts):
    """Choose which credit to spend.

    A multi-day workshop hands out a fresh credit each morning, and yesterday's
    is either drained or about to be. So:

      1. Newest CDP credit by the date in its name — what the room was just given
      2. Any account not yet linked to a project — fresh, if unlabelled
      3. First open account — better than stopping
    """
    dated = [(credit_date(a.get("displayName", "")), a) for a in accounts]
    dated = [(d, a) for d, a in dated if d is not None]
    if dated:
        dated.sort(key=lambda pair: pair[0], reverse=True)
        account = dated[0][1]
        say(f"   Newest CDP credit: {account['displayName']}")
        return account

    unlinked = [a for a in accounts if linked_project_count(a["name"]) == 0]
    if unlinked:
        say(f"   Unused billing account: {unlinked[0]['displayName']}")
        return unlinked[0]

    say(f"   No CDP credit found. Using: {accounts[0]['displayName']}")
    return accounts[0]


def link_billing(project_id, account):
    """Link, then check it actually took. The link can lag by a few seconds."""
    name, shown = account["name"], account.get("displayName", account["name"])
    say(f"   Linking '{shown}' to {project_id} ...")

    ok, _, err = run(
        ["gcloud", "billing", "projects", "link", project_id,
         "--billing-account", name, "--quiet"],
        timeout=180,
    )
    if not ok:
        low = err.lower()
        if "permission" in low or "denied" in low:
            say("   ❌ Permission denied. You may need the 'Billing Account User' role.")
        say(f"      {err.splitlines()[0] if err else '?'}")
        return False

    say("   Verifying billing link...")
    for i in range(6):
        enabled, linked_to = billing_is_enabled(project_id)
        if enabled and linked_to.endswith(name.split("/")[-1]):
            say("   ✓ Billing verified active")
            return True
        if i < 5:
            time.sleep(10)

    say("   ⚠️  Could not verify the link (it may still be propagating)")
    return True  # Optimistically continue — the next step will say if it is not.


def no_accounts_banner():
    say("")
    say("╔═══════════════════════════════════════════════════════════════╗")
    say("║              ⚠️  BILLING ACCOUNT REQUIRED                      ║")
    say("╠═══════════════════════════════════════════════════════════════╣")
    say("║                                                               ║")
    say("║  No billing accounts found after waiting.                     ║")
    say("║                                                               ║")
    say("║  If you're at a workshop:                                     ║")
    say("║  • Make sure you've CLAIMED YOUR CREDIT from the organiser    ║")
    say("║  • Wait a minute for it to apply, then run ./setup.sh again   ║")
    say("║                                                               ║")
    say("║  If you're self-learning:                                     ║")
    say("║  • Create a billing account (free tier available):            ║")
    say("║    https://console.cloud.google.com/billing/create            ║")
    say("║                                                               ║")
    say("╚═══════════════════════════════════════════════════════════════╝")


def ensure_billing(project_id):
    """True if project_id ends up with billing on it."""
    enabled, account = billing_is_enabled(project_id)
    if enabled:
        say(f"→ billing    already enabled on {project_id}")
        return True

    say("   Billing not enabled. Looking for a billing account...")
    accounts, status = list_billing_accounts()

    # The API is on but not yet serving. Enable and back off — this is the
    # normal state of a project created ninety seconds ago.
    if status == "api":
        if not enable_billing_api(project_id):
            return False
        wait = 15
        for i in range(5):
            say(f"   Waiting for the API to propagate ({i + 1}/5, {wait}s)...")
            time.sleep(wait)
            accounts, status = list_billing_accounts()
            if status != "api":
                break
            wait = int(wait * 1.5)

    if status == "denied":
        say("   ❌ You don't have permission to view billing accounts.")
        say("   Ask your organisation admin for the 'Billing Account User' role.")
        return False
    if status == "api":
        say("   ❌ The Cloud Billing API did not become active.")
        say("   Try again in a few minutes, or enable it by hand:")
        say("   https://console.cloud.google.com/apis/library/"
            f"cloudbilling.googleapis.com?project={project_id}")
        return False
    if status == "error":
        return False

    # A credit claimed moments ago takes a little while to show up.
    if not accounts:
        say("   No billing accounts yet. Waiting for credit propagation...")
        say("   (up to 2 minutes if you just claimed one)")
        for i in range(6):
            say(f"   Waiting... ({i + 1}/6)")
            time.sleep(20)
            accounts, status = list_billing_accounts()
            if accounts:
                say("   ✓ Found a billing account")
                break

    if not accounts:
        no_accounts_banner()
        return False

    if len(accounts) == 1:
        say(f"   Found: {accounts[0]['displayName']}")
        account = accounts[0]
    else:
        say(f"   Found {len(accounts)} open billing accounts")
        account = pick_billing_account(accounts)

    return link_billing(project_id, account)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--project", help="use this existing project")
    g.add_argument("--create", action="store_true",
                   help="create long-running-<random> and bill it")
    args = ap.parse_args()

    if not active_account():
        say("✗ gcloud has no active account. Run this, then ./setup.sh again:")
        say("")
        say("      gcloud auth login")
        return 1
    say(f"→ account    {active_account()}")

    if args.create:
        project_id = create_project()
        if not project_id:
            return 1
    else:
        project_id = args.project
        if not project_exists(project_id):
            say(f"✗ Cannot access project '{project_id}'.")
            say("  Either the id is wrong, or your account has no access to it.")
            ok, out, _ = run(
                ["gcloud", "projects", "list", "--format=value(projectId)"],
                timeout=60,
            )
            if ok and out:
                say("")
                say("  Projects you can see:")
                for line in out.splitlines()[:10]:
                    say(f"      {line}")
            say("")
            say("  Then re-run:   rm -f ~/project_id.txt && ./setup.sh")
            return 1

    if not ensure_billing(project_id):
        say("")
        say(f"  The project is {project_id}, but it has no billing account.")
        say("  Vertex AI and Cloud Run both need one, so setup stops here.")
        return 1

    # The one line setup.sh reads.
    print(f"PROJECT={project_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
