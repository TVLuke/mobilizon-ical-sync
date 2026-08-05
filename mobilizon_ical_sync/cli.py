"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import webbrowser
from pathlib import Path

from . import __version__
from .config import Config, OrgConfig, load_config
from .ical import dedupe, load_ical, load_ical_file
from .mobilizon import MobilizonAuth, MobilizonClient
from .recurring import generate_recurring_events
from .state import State
from .sync import sync_org


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not verbose:
        # httpx logs every request at INFO which is noisy during device polling.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mobilizon-event-sync",
        description="Sync events from various sources into Mobilizon groups as events.",
    )
    p.add_argument("--config", "-c", type=Path, default=Path("config.json"))
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Register the OAuth application on the instance.")

    login = sub.add_parser(
        "login",
        help="Obtain OAuth token for the instance via device flow.",
    )
    login.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not try to open the verification URL in a browser.",
    )

    sync = sub.add_parser("sync", help="Run a one-shot sync pass.")
    sync.add_argument(
        "--org",
        help="Only sync this organization id (defaults to all).",
    )
    sync.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse events and print what would happen, but do not call the API.",
    )

    sub.add_parser("status", help="Print stored app/token state.")

    return p


def _select_orgs(cfg: Config, org_id: str | None) -> list[OrgConfig]:
    if not org_id:
        return list(cfg.organizations)
    matches = [o for o in cfg.organizations if o.id == org_id]
    if not matches:
        raise SystemExit(f"no organization with id {org_id!r} in config")
    return matches


def _cmd_init(cfg: Config, state: State) -> int:
    auth = MobilizonAuth(cfg.instance_url, state, timeout=cfg.http_timeout_seconds)
    try:
        creds = auth.ensure_app(cfg.app.name, cfg.app.website)
    finally:
        auth.close()
    print(f"App registered on {cfg.instance_base}")
    print(f"  client_id: {creds.client_id}")
    print(f"  scope:     {creds.scope}")
    return 0


def _cmd_login(cfg: Config, state: State, open_browser: bool) -> int:
    import os
    auth = MobilizonAuth(cfg.instance_url, state, timeout=cfg.http_timeout_seconds)
    try:
        # Use password-based auth if email and password are provided via environment variables
        email = os.environ.get("MOBILIZON_EMAIL")
        password = os.environ.get("MOBILIZON_PASSWORD")
        if email and password:
            print(f"\n=== Login for instance {cfg.instance_base} (password-based) ===")
            token = auth.login_password(email, password)
            state.save_token(cfg.instance_url, token)
            print(f"Token stored for instance {cfg.instance_base}.")
            return 0
        
        # Otherwise use OAuth device flow
        creds = auth.ensure_app(cfg.app.name, cfg.app.website)
        print(f"\n=== Login for instance {cfg.instance_base} (OAuth device flow) ===")
        dc = auth.request_device_code(creds)
        print(f"Your one-time code: {dc.user_code}")
        print(f"Verification URL:   {dc.verification_uri}")
        print(f"(code expires in {dc.expires_in} seconds)")
        if open_browser:
            opened = webbrowser.open(dc.verification_uri, new=2)
            if opened:
                print("Opened the URL in your default browser. Enter the code and approve.")
            else:
                print("Could not open a browser automatically; open the URL above manually.")
        else:
            print("Open the URL above in a browser, enter the code, and approve.")
        print("Waiting for approval", end="", flush=True)

        def _tick(_status: str) -> None:
            print(".", end="", flush=True)

        try:
            token = auth.poll_device_token(creds, dc, on_poll=_tick)
        finally:
            print()  # newline after dots
        print("Approved.")
        state.save_token(cfg.instance_url, token)
        print(f"Token stored for instance {cfg.instance_base}.")
    finally:
        auth.close()
    return 0


def _cmd_sync(cfg: Config, state: State, org_id: str | None, dry_run: bool) -> int:
    import time
    auth = MobilizonAuth(cfg.instance_url, state, timeout=cfg.http_timeout_seconds)
    total_errors = 0
    try:
        client = MobilizonClient(
            cfg.instance_url, state, auth, timeout=cfg.http_timeout_seconds
        )
        try:
            for org in _select_orgs(cfg, org_id):
                if not org.active:
                    print(f"\n=== Skipping '{org.id}' (inactive) ===")
                    continue
                
                print(f"\n=== Syncing '{org.id}' ({org.group}) ===")
                
                # Determine event source and load events
                if org.recurring:
                    print(f"Source: Recurring event pattern")
                    events = generate_recurring_events(
                        org.recurring,
                        org.default_timezone,
                        org.max_future_events or 12,
                    )
                elif org.ical_file:
                    print(f"Source: Local file {org.ical_file}")
                    events = dedupe(load_ical_file(org.ical_file, org.default_timezone))
                elif org.ical_url:
                    print(f"Source: URL {org.ical_url}")
                    events = dedupe(load_ical(org.ical_url, cfg.http_timeout_seconds, org.default_timezone))
                else:
                    print(f"Error: No event source configured for org '{org.id}'")
                    total_errors += 1
                    continue
                
                events.sort(key=lambda e: e.begins_on)
                capped = (
                    events[: org.max_future_events]
                    if org.max_future_events and org.max_future_events > 0
                    else events
                )
                print(
                    f"Events: {len(events)} future events after dedupe"
                    + (f" (syncing next {len(capped)})" if len(capped) != len(events) else "")
                )

                if dry_run:
                    for ev in capped:
                        print(f"  - {ev.begins_on.isoformat()}  {ev.title}  (uid={ev.uid})")
                    continue

                config_dir = cfg.state_file.parent
                stats = sync_org(org, events, client, state, config_dir)
                print("Result:", json.dumps(stats.as_dict()))
                total_errors += stats.errors
                
                # Add delay between organizations to avoid rate limiting
                if not dry_run:
                    time.sleep(2)
        finally:
            client.close()
    finally:
        auth.close()
    return 1 if total_errors else 0


def _cmd_status(cfg: Config, state: State) -> int:
    creds = state.get_app(cfg.instance_url)
    print(f"instance: {cfg.instance_base}")
    print(f"app registered: {'yes' if creds else 'no'}")
    if creds:
        print(f"  client_id: {creds.client_id}")
        print(f"  scope:     {creds.scope}")
    tok = state.get_token(cfg.instance_url)
    print(f"token: {'logged in' if tok else 'not logged in'}")
    if tok:
        print(f"  access_expires_at={tok.access_expires_at}")
    print("organizations:")
    for org in cfg.organizations:
        mappings = state.get_mappings(org.id)
        print(f"  - {org.id} (group={org.group}, active={org.active})")
        print(f"      tracked events: {len(mappings)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    _setup_logging(args.verbose)

    cfg_path = args.config
    if not cfg_path.exists():
        print(f"config file not found: {cfg_path}", file=sys.stderr)
        return 2
    cfg = load_config(cfg_path)

    with State(cfg.state_file) as state:
        if args.cmd == "init":
            return _cmd_init(cfg, state)
        if args.cmd == "login":
            return _cmd_login(cfg, state, open_browser=not args.no_browser)
        if args.cmd == "sync":
            return _cmd_sync(cfg, state, args.org, args.dry_run)
        if args.cmd == "status":
            return _cmd_status(cfg, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
