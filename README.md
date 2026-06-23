# ListenBrainz Artist Listen Deleter

A Python script to find and delete ListenBrainz listens that match one or more artist MBIDs and/or artist names.

The script reads your ListenBrainz username and token from environment variables, scans your listen history, lists all matches it finds, saves them to a JSON file, and then asks for confirmation before deleting anything. ListenBrainz uses user tokens for authenticated API access and applies rate limiting to clients, so long scans and large deletions can take time.

## Features

- Matches listens by **artist MBID**.
- Matches listens by **artist name** as a fallback when MBIDs are missing from stored listen metadata.
- Reads credentials from environment variables instead of prompting for them.
- Saves matched listens to a JSON file before deletion.
- Shows a preview of matched listens before asking for confirmation.
- Retries on API rate limits (`429`) and transient network failures.
- Supports resuming long scans with `--resume-from-ts`.
- Prints deleted-listen counts by artist MBID and artist name.

## Requirements

- Python 3.8+
- `requests`

Install dependencies:

```bash
pip install requests
```

## Environment variables

Set these before running the script:

- `LB_USER` — your ListenBrainz username
- `LB_TOKEN` — your ListenBrainz user token

Example:

```bash
export LB_USER="your_username"
export LB_TOKEN="your_token"
```

The ListenBrainz API expects the token in the `Authorization: Token ...` header, and the script validates the token against the expected username before scanning.

## Usage

Run the script:

```bash
python3 app.py
```

Skip the confirmation prompt after listing matches:

```bash
python3 app.py --yes
```

Resume from a saved timestamp checkpoint:

```bash
python3 app.py --resume-from-ts 1648210357
```

Save matches to a custom JSON file:

```bash
python3 app.py --output my_matches.json
```

## How to enter targets

When prompted for `Artist targets`, you can enter:

- one or more MBIDs,
- one or more artist names with the `artist:` prefix,
- or a mix of both separated by `|`.

Examples:

```text
cc197bad-dc9c-440d-a5b5-d52ba2e14234
```

```text
artist:Nine Inch Nails
```

```text
mbid:cc197bad-dc9c-440d-a5b5-d52ba2e14234 | artist:Nine Inch Nails | artist:Röyksopp
```

Artist-name matching is useful because ListenBrainz listens generally include `track_metadata.artist_name`, while MBID fields in additional metadata may be absent or empty on some listens.

## Workflow

The script works like this:

1. Reads `LB_USER` and `LB_TOKEN` from the environment.
2. Validates the token with ListenBrainz before doing any history scan.
3. Fetches your listen history page by page using `max_ts` pagination.
4. Matches listens against the target artist MBIDs and/or normalized artist names.
5. Saves the matched listens to a JSON file.
6. Prints counts and a preview of the first matches.
7. Asks for confirmation.
8. Deletes matching listens one by one.

ListenBrainz deletion works per listen rather than as a bulk “delete by artist” operation, which is why large deletions can take time.

## Output files

The default output file is:

```text
listenbrainz_matches.json
```

It contains:

- `username`
- `artist_mbids`
- `artist_names`
- `match_count`
- `resume_from_ts`
- `matches`

The script also writes a checkpoint file during long scans so you can resume from the last known timestamp if needed.

## Confirmation and deletion

By default, the script lists matches first and only deletes after you type:

```text
YES
```

Use `--yes` only if you want to skip that confirmation step.

The ListenBrainz API endpoint deletes a particular listen from a user's listen history using listen identifiers, and deleted listens may not disappear immediately from the UI.

## Rate limits and reliability

ListenBrainz rate-limits API clients and can return HTTP `429 Too Many Requests` when the request rate is too high. The script handles this by waiting and retrying, and it also retries transient connection failures so long history scans are less likely to fail halfway through.

## Notes

- Matching by artist name is case-insensitive and whitespace-normalized.
- If a listen matches both an MBID target and a name target, both matches are recorded.
- Some imported or older listens may not contain artist MBIDs, which is why artist-name matching was added.
- Listen deletions may only become visible around the next hourly update rather than instantly.

## Related tool

If you want a more established ListenBrainz cleanup CLI, **Elbisaur** supports exporting, previewing, and deleting listens from prepared files.
