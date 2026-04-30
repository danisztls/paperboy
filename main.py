#!/usr/bin/env python3
"""RSS to Discord webhook notifier"""

import json
import sys
import pathlib
import time
import logging
import argparse

from feed import get_new_entries
from discord import post_to_discord

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


def load_config(path: pathlib.Path) -> dict:
    text = path.read_text()
    if path.suffix in (".yaml", ".yml"):
        import yaml
        return yaml.safe_load(text)
    return json.loads(text)


def load_state(path: pathlib.Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_state(path: pathlib.Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def main():
    parser = argparse.ArgumentParser(description="RSS to Discord webhook notifier")
    parser.add_argument("config", help="Path to config file (YAML or JSON)")
    parser.add_argument(
        "--state",
        default=None,
        help="Path to state file (default: <config_dir>/state.json)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Post one entry (dry-run for state), verbose output",
    )
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        log.debug("Debug mode enabled — will post at most one entry and skip state save")

    config_path = pathlib.Path(args.config).expanduser().resolve()
    state_path = (
        pathlib.Path(args.state).expanduser().resolve()
        if args.state
        else config_path.parent / "state.json"
    )

    if not config_path.exists():
        log.error("Config file not found: %s", config_path)
        sys.exit(1)

    log.debug("Config: %s", config_path)
    log.debug("State:  %s", state_path)

    config = load_config(config_path)
    state = load_state(state_path)

    hooks = config.get("hooks", [])
    if not hooks:
        log.error("No hooks defined in config.")
        sys.exit(1)

    for hook_cfg in hooks:
        webhook = hook_cfg.get("webhook")
        if not webhook:
            log.warning("Skipping hook with no webhook URL")
            continue
        for feed_cfg in hook_cfg.get("feeds", []):
            url = feed_cfg.get("url")
            if not url:
                log.warning("Skipping feed with no URL: %s", feed_cfg)
                continue
            seen = set(state.get(url, []))
            current_ids, new_entries = get_new_entries(feed_cfg, seen)
            posted_any = False
            for entry in new_entries:
                try:
                    post_to_discord(webhook, entry, debug=args.debug)
                    log.info("[%s] Posted: %s", entry.feed_title, entry.title[:80])
                    posted_any = True
                    if args.debug:
                        break
                    time.sleep(1)
                except Exception:
                    log.error("Skipping entry %s due to post failure", entry.id)
            if not args.debug:
                state[url] = current_ids
            if args.debug and posted_any:
                log.debug("Debug mode: stopping after first posted entry")
                break
        else:
            continue
        break

    if args.debug:
        log.debug("Debug mode: state not saved")
    else:
        save_state(state_path, state)
        log.info("Done. State saved to %s", state_path)


if __name__ == "__main__":
    main()
