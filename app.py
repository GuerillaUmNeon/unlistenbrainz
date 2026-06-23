#!/usr/bin/env python3

import argparse
import collections
import json
import os
import random
import re
import sys
import time
from pathlib import Path

import requests
from requests.exceptions import ConnectionError, HTTPError, Timeout

API_ROOT = "https://api.listenbrainz.org"
MAX_PAGE_SIZE = 1000
MAX_RETRIES = 10
REQUEST_DELAY_SECONDS = 1.5
PROGRESS_SAVE_EVERY = 5000

session = requests.Session()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Delete ListenBrainz listens matching artist MBIDs and/or artist names."
    )
    parser.add_argument(
        "--output",
        default="listenbrainz_matches.json",
        help="Path to save matched listens as JSON.",
    )
    parser.add_argument(
        "--resume-from-ts",
        type=int,
        default=None,
        help="Resume scan from this max_ts value.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Delete without asking for confirmation after listing matches.",
    )
    return parser.parse_args()


def normalize_artist_name(name):
    if not isinstance(name, str):
        return ""
    name = name.strip().casefold()
    name = re.sub(r"\s+", " ", name)
    return name


def parse_targets(raw_input):
    mbid_targets = set()
    artist_targets = set()

    mbid_pattern = re.compile(
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    )

    parts = [part.strip() for part in raw_input.split("|") if part.strip()]

    for part in parts:
        lowered = part.lower()

        if lowered.startswith("mbid:"):
            value = part[5:].strip().lower()
            if value:
                mbid_targets.add(value)
            continue

        if lowered.startswith("artist:"):
            value = normalize_artist_name(part[7:])
            if value:
                artist_targets.add(value)
            continue

        for token in part.split():
            token = token.strip()
            if not token:
                continue
            if mbid_pattern.fullmatch(token):
                mbid_targets.add(token.lower())
            else:
                artist_targets.add(normalize_artist_name(token))

    return mbid_targets, artist_targets


def get_credentials_from_env():
    username = os.getenv("LB_USER", "").strip()
    token = os.getenv("LB_TOKEN", "").strip()

    if not username:
        print("Error: LB_USER environment variable is not set.")
        sys.exit(1)

    if not token:
        print("Error: LB_TOKEN environment variable is not set.")
        sys.exit(1)

    return username, token


def prompt_targets():
    print(
        "Targets:\n"
        "- MBIDs separated by spaces, or\n"
        "- artist names with artist:, or\n"
        "- mix both using | as separator.\n"
        "Example:\n"
        "  mbid:cc197bad-dc9c-440d-a5b5-d52ba2e14234 | artist:Nine Inch Nails | artist:Röyksopp"
    )
    targets_raw = input("Artist targets: ").strip()

    target_artist_mbids, target_artist_names = parse_targets(targets_raw)
    if not target_artist_mbids and not target_artist_names:
        print("Error: at least one artist MBID or artist name is required.")
        sys.exit(1)

    return target_artist_mbids, target_artist_names


def auth_headers(token):
    return {
        "Authorization": f"Token {token}",
        "Content-Type": "application/json",
        "User-Agent": "unlistenbrainz/1.4 (local cleanup script)",
        "Accept": "application/json",
        "Connection": "keep-alive",
    }


def get_retry_wait_seconds_from_response(response, attempt):
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return max(float(retry_after), REQUEST_DELAY_SECONDS)
        except ValueError:
            pass

    reset_in = response.headers.get("X-RateLimit-Reset-In")
    if reset_in:
        try:
            return max(float(reset_in), REQUEST_DELAY_SECONDS)
        except ValueError:
            pass

    return min(90, (2 ** attempt)) + random.uniform(0, 1)


def get_retry_wait_seconds_for_exception(attempt):
    return min(60, (2 ** attempt)) + random.uniform(0.5, 1.5)


def request_with_retry(method, url, *, headers=None, params=None, json_body=None, timeout=60):
    last_error = None

    for attempt in range(MAX_RETRIES):
        try:
            response = session.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=timeout,
            )

            if response.status_code == 429:
                wait_time = get_retry_wait_seconds_from_response(response, attempt)
                print(f"Rate limited (429). Waiting {wait_time:.1f}s before retrying...")
                time.sleep(wait_time)
                continue

            response.raise_for_status()
            return response

        except (ConnectionError, Timeout) as e:
            last_error = e
            wait_time = get_retry_wait_seconds_for_exception(attempt)
            print(f"Network error: {e}. Waiting {wait_time:.1f}s before retrying...")
            time.sleep(wait_time)
            continue

        except HTTPError:
            raise

    if last_error:
        raise last_error

    raise HTTPError(f"Request failed after {MAX_RETRIES} retries: {url}")


def validate_token(token, expected_username):
    url = f"{API_ROOT}/1/validate-token"
    response = request_with_retry("GET", url, headers=auth_headers(token), timeout=30)
    data = response.json()

    if not data.get("valid"):
        print("Error: token is invalid.")
        sys.exit(1)

    token_user = data.get("user_name")
    if token_user and token_user != expected_username:
        print(f"Error: token belongs to '{token_user}', not '{expected_username}'.")
        sys.exit(1)


def fetch_listens(username, token, start_max_ts=None):
    url = f"{API_ROOT}/1/user/{username}/listens"
    max_ts = start_max_ts

    while True:
        params = {"count": MAX_PAGE_SIZE}
        if max_ts is not None:
            params["max_ts"] = max_ts

        response = request_with_retry(
            "GET",
            url,
            params=params,
            headers=auth_headers(token),
            timeout=60,
        )
        data = response.json()

        listens = data.get("payload", {}).get("listens", [])
        if not listens:
            break

        yield listens

        if len(listens) < MAX_PAGE_SIZE:
            break

        oldest_ts = listens[-1].get("listened_at")
        if oldest_ts is None:
            break

        max_ts = oldest_ts - 1
        time.sleep(REQUEST_DELAY_SECONDS)


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


def extract_found_artist_names(listen):
    track_metadata = listen.get("track_metadata", {})
    additional_info = track_metadata.get("additional_info", {})
    found_names = set()

    artist_name = track_metadata.get("artist_name")
    if isinstance(artist_name, str) and artist_name.strip():
        found_names.add(normalize_artist_name(artist_name))

    additional_artist_name = additional_info.get("artist_name")
    if isinstance(additional_artist_name, str) and additional_artist_name.strip():
        found_names.add(normalize_artist_name(additional_artist_name))

    artist_names = additional_info.get("artist_names")
    if isinstance(artist_names, list):
        found_names.update(
            normalize_artist_name(name)
            for name in artist_names
            if isinstance(name, str) and name.strip()
        )

    master_album_artist = additional_info.get("master_metadata_album_artist_name")
    if isinstance(master_album_artist, str) and master_album_artist.strip():
        found_names.add(normalize_artist_name(master_album_artist))

    return {name for name in found_names if name}


def match_listen(listen, target_artist_mbids, target_artist_names):
    found_mbids = extract_found_artist_mbids(listen)
    found_names = extract_found_artist_names(listen)

    matched_mbids = sorted(found_mbids.intersection(target_artist_mbids))
    matched_names = sorted(found_names.intersection(target_artist_names))

    return matched_mbids, matched_names


def delete_listen(token, listened_at, recording_msid):
    url = f"{API_ROOT}/1/delete-listen"
    payload = {
        "listened_at": listened_at,
        "recording_msid": recording_msid,
    }

    response = request_with_retry(
        "POST",
        url,
        json_body=payload,
        headers=auth_headers(token),
        timeout=30,
    )
    return response.json()


def save_matches(path, username, artist_mbids, artist_names, matches, resume_from_ts=None):
    output = {
        "username": username,
        "artist_mbids": sorted(artist_mbids),
        "artist_names": sorted(artist_names),
        "match_count": len(matches),
        "resume_from_ts": resume_from_ts,
        "matches": matches,
    }
    Path(path).write_text(
        json.dumps(output, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def save_checkpoint(path, username, artist_mbids, artist_names, matches, next_resume_ts, scanned):
    output = {
        "username": username,
        "artist_mbids": sorted(artist_mbids),
        "artist_names": sorted(artist_names),
        "match_count": len(matches),
        "scanned": scanned,
        "next_resume_ts": next_resume_ts,
        "matches": matches,
    }
    checkpoint_path = Path(path).with_suffix(".checkpoint.json")
    checkpoint_path.write_text(
        json.dumps(output, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def main():
    args = parse_args()
    username, token = get_credentials_from_env()
    target_artist_mbids, target_artist_names = prompt_targets()

    print(f"Using ListenBrainz user: {username}")
    print("Validating token...")
    validate_token(token, username)

    print("Fetching listens and filtering matches...")
    matches = []
    scanned = 0
    counts_by_mbid = collections.Counter()
    counts_by_name = collections.Counter()
    next_resume_ts = args.resume_from_ts

    for page in fetch_listens(username, token, start_max_ts=args.resume_from_ts):
        if page:
            last_ts = page[-1].get("listened_at")
            if last_ts is not None:
                next_resume_ts = last_ts - 1

        for listen in page:
            scanned += 1

            listened_at = listen.get("listened_at")
            recording_msid = listen.get("recording_msid")
            if not listened_at or not recording_msid:
                continue

            matched_mbids, matched_names = match_listen(
                listen,
                target_artist_mbids,
                target_artist_names,
            )

            if matched_mbids or matched_names:
                track_metadata = listen.get("track_metadata", {})
                matches.append({
                    "listened_at": listened_at,
                    "recording_msid": recording_msid,
                    "track_name": track_metadata.get("track_name", "<unknown track>"),
                    "artist_name": track_metadata.get("artist_name", "<unknown artist>"),
                    "release_name": track_metadata.get("release_name", ""),
                    "matched_artist_mbids": matched_mbids,
                    "matched_artist_names": matched_names,
                })

                for mbid in matched_mbids:
                    counts_by_mbid[mbid] += 1

                for name in matched_names:
                    counts_by_name[name] += 1

            if scanned % 1000 == 0:
                print(f"Scanned {scanned} listens... matched {len(matches)}")

            if scanned % PROGRESS_SAVE_EVERY == 0:
                save_checkpoint(
                    args.output,
                    username,
                    target_artist_mbids,
                    target_artist_names,
                    matches,
                    next_resume_ts,
                    scanned,
                )
                print(
                    f"Checkpoint saved at {scanned} listens. "
                    f"Resume with --resume-from-ts {next_resume_ts}"
                )

    print(f"\nFinished scanning {scanned} listens.")
    print(f"Found {len(matches)} matching listens.")

    save_matches(
        args.output,
        username,
        target_artist_mbids,
        target_artist_names,
        matches,
        resume_from_ts=next_resume_ts,
    )
    print(f"Saved matched listens to: {args.output}")

    if counts_by_mbid:
        print("\nMatched listens by artist MBID:")
        for mbid, count in sorted(counts_by_mbid.items()):
            print(f"- {mbid}: {count}")

    if counts_by_name:
        print("\nMatched listens by artist name:")
        for name, count in sorted(counts_by_name.items()):
            print(f"- {name}: {count}")

    if not matches:
        return

    preview_count = min(50, len(matches))
    print(f"\nPreviewing first {preview_count} matches:")
    for item in matches[:preview_count]:
        print(
            f"- {item['artist_name']} — {item['track_name']} "
            f"(ts={item['listened_at']}, msid={item['recording_msid']})"
        )

    if not args.yes:
        confirm = input(
            f"\nDelete all {len(matches)} matching listens? Type YES to confirm: "
        ).strip()
        if confirm != "YES":
            print("Cancelled.")
            return

    deleted = 0
    failed = 0
    deleted_by_mbid = collections.Counter()
    deleted_by_name = collections.Counter()

    for i, item in enumerate(matches, start=1):
        try:
            delete_listen(token, item["listened_at"], item["recording_msid"])
            deleted += 1

            for mbid in item["matched_artist_mbids"]:
                deleted_by_mbid[mbid] += 1

            for name in item["matched_artist_names"]:
                deleted_by_name[name] += 1

            time.sleep(REQUEST_DELAY_SECONDS)

        except Exception as e:
            failed += 1
            print(
                f"Failed to delete #{i}: {item['artist_name']} — {item['track_name']} "
                f"(ts={item['listened_at']}): {e}"
            )

        if i % 20 == 0:
            print(f"Processed {i}/{len(matches)}... deleted={deleted}, failed={failed}")

    print(f"\nDone. Delete requests sent successfully: {deleted}")
    print(f"Failed deletions: {failed}")

    if deleted_by_mbid:
        print("\nTracks deleted by artist MBID:")
        for mbid, count in sorted(deleted_by_mbid.items()):
            print(f"- {mbid}: {count}")

    if deleted_by_name:
        print("\nTracks deleted by artist name:")
        for name, count in sorted(deleted_by_name.items()):
            print(f"- {name}: {count}")

    print("Deleted listens may only disappear at the top of the next hour.")


if __name__ == "__main__":
    main()