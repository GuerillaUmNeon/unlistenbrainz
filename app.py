#!/usr/bin/env python3

import argparse
import collections
import json
import sys
import time
from pathlib import Path
import getpass

import requests

API_ROOT = "https://api.listenbrainz.org"
MAX_PAGE_SIZE = 1000


def parse_args():
    parser = argparse.ArgumentParser(
        description="Delete ListenBrainz listens matching artist MBIDs."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview matching listens only; do not delete anything.",
    )
    parser.add_argument(
        "--output",
        default="listenbrainz_matches.json",
        help="Path to save matched listens as JSON.",
    )
    return parser.parse_args()


def prompt_user_input():
    username = input("ListenBrainz username: ").strip()
    token = getpass.getpass("ListenBrainz token: ").strip()
    mbids_raw = input("Artist MBIDs (separated by spaces): ").strip()

    if not username:
        print("Error: username is required.")
        sys.exit(1)

    if not token:
        print("Error: token is required.")
        sys.exit(1)

    artist_mbids = {mbid.strip().lower() for mbid in mbids_raw.split() if mbid.strip()}
    if not artist_mbids:
        print("Error: at least one artist MBID is required.")
        sys.exit(1)

    return username, token, artist_mbids


def auth_headers(token):
    return {
        "Authorization": f"Token {token}",
        "Content-Type": "application/json",
    }


def validate_token(token, expected_username):
    url = f"{API_ROOT}/1/validate-token"
    r = requests.get(url, headers=auth_headers(token), timeout=30)
    r.raise_for_status()
    data = r.json()

    if not data.get("valid"):
        print("Error: token is invalid.")
        sys.exit(1)

    token_user = data.get("user_name")
    if token_user and token_user != expected_username:
        print(f"Error: token belongs to '{token_user}', not '{expected_username}'.")
        sys.exit(1)


def fetch_listens(username, token):
    url = f"{API_ROOT}/1/user/{username}/listens"
    max_ts = None

    while True:
        params = {"count": MAX_PAGE_SIZE}
        if max_ts is not None:
            params["max_ts"] = max_ts

        r = requests.get(url, params=params, headers=auth_headers(token), timeout=60)
        r.raise_for_status()
        data = r.json()

        listens = data.get("payload", {}).get("listens", [])
        if not listens:
            break

        yield from listens

        if len(listens) < MAX_PAGE_SIZE:
            break

        oldest_ts = listens[-1].get("listened_at")
        if oldest_ts is None:
            break

        max_ts = oldest_ts


def extract_found_artist_mbids(listen):
    track_metadata = listen.get("track_metadata", {})
    additional_info = track_metadata.get("additional_info", {})
    found_mbids = set()

    artist_mbids_list = additional_info.get("artist_mbids")
    if isinstance(artist_mbids_list, list):
        found_mbids.update(
            mbid.strip().lower()
            for mbid in artist_mbids_list
            if isinstance(mbid, str) and mbid.strip()
        )

    artist_mbid = additional_info.get("artist_mbid")
    if isinstance(artist_mbid, str) and artist_mbid.strip():
        found_mbids.add(artist_mbid.strip().lower())

    return found_mbids


def delete_listen(token, listened_at, recording_msid):
    url = f"{API_ROOT}/1/delete-listen"
    payload = {
        "listened_at": listened_at,
        "recording_msid": recording_msid,
    }

    r = requests.post(url, json=payload, headers=auth_headers(token), timeout=30)
    r.raise_for_status()
    return r.json()


def save_matches(path, username, artist_mbids, matches, dry_run):
    output = {
        "username": username,
        "artist_mbids": sorted(artist_mbids),
        "mode": "dry-run" if dry_run else "delete",
        "match_count": len(matches),
        "matches": matches,
    }
    Path(path).write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")


def main():
    args = parse_args()
    username, token, target_artist_mbids = prompt_user_input()

    print("Validating token...")
    validate_token(token, username)

    print("Fetching listens and filtering matches...")
    matches = []
    scanned = 0
    counts_by_artist = collections.Counter()

    for listen in fetch_listens(username, token):
        scanned += 1

        listened_at = listen.get("listened_at")
        recording_msid = listen.get("recording_msid")
        if not listened_at or not recording_msid:
            continue

        found_artist_mbids = extract_found_artist_mbids(listen)
        matched_artist_mbids = sorted(found_artist_mbids.intersection(target_artist_mbids))

        if matched_artist_mbids:
            track_metadata = listen.get("track_metadata", {})
            matches.append({
                "listened_at": listened_at,
                "recording_msid": recording_msid,
                "track_name": track_metadata.get("track_name", "<unknown track>"),
                "artist_name": track_metadata.get("artist_name", "<unknown artist>"),
                "release_name": track_metadata.get("release_name", ""),
                "matched_artist_mbids": matched_artist_mbids,
            })

            for mbid in matched_artist_mbids:
                counts_by_artist[mbid] += 1

        if scanned % 1000 == 0:
            print(f"Scanned {scanned} listens... matched {len(matches)}")

    print(f"\nFinished scanning {scanned} listens.")
    print(f"Found {len(matches)} matching listens.")

    save_matches(args.output, username, target_artist_mbids, matches, args.dry_run)
    print(f"Saved matched listens to: {args.output}")

    if counts_by_artist:
        print("\nMatched listens by artist MBID:")
        for mbid, count in sorted(counts_by_artist.items()):
            print(f"- {mbid}: {count}")

    if not matches:
        return

    preview_count = min(20, len(matches))
    print(f"\nPreviewing first {preview_count} matches:")
    for item in matches[:preview_count]:
        print(
            f"- {item['artist_name']} — {item['track_name']} "
            f"(ts={item['listened_at']}, msid={item['recording_msid']})"
        )

    if args.dry_run:
        print("\nDry run enabled. No listens were deleted.")
        return

    confirm = input(f"\nDelete all {len(matches)} matching listens? Type YES to confirm: ").strip()
    if confirm != "YES":
        print("Cancelled.")
        return

    deleted = 0
    failed = 0
    deleted_by_artist = collections.Counter()

    for i, item in enumerate(matches, start=1):
        try:
            delete_listen(token, item["listened_at"], item["recording_msid"])
            deleted += 1
            for mbid in item["matched_artist_mbids"]:
                deleted_by_artist[mbid] += 1
        except Exception as e:
            failed += 1
            print(
                f"Failed to delete #{i}: {item['artist_name']} — {item['track_name']} "
                f"(ts={item['listened_at']}): {e}"
            )

        if i % 20 == 0:
            print(f"Processed {i}/{len(matches)}... deleted={deleted}, failed={failed}")
            time.sleep(1)

    print(f"\nDone. Delete requests sent successfully: {deleted}")
    print(f"Failed deletions: {failed}")

    if deleted_by_artist:
        print("\nTracks deleted by artist MBID:")
        for mbid, count in sorted(deleted_by_artist.items()):
            print(f"- {mbid}: {count}")

    print("Deleted listens may only disappear at the top of the next hour.")


if __name__ == "__main__":
    main()