# Outbound SIP setup (Twilio + LiveKit)

This guide covers **outbound-only** telephony setup for Clauver using `scripts/provision_sip.py`. It does **not** configure inbound trunks, origination URLs, dispatch rules, or MCP integration.

## What the helper does

1. Confirms you already own a Twilio phone number (strict ASCII E.164).
2. Creates or reuses a **Twilio Elastic SIP trunk** and termination credentials (`SIP_AUTH_PASSWORD` or hidden prompt — never CLI args or journal fields).
3. Creates or reuses a **LiveKit outbound trunk** with your number as the **only** outbound caller ID on that trunk.
4. Prints `SIP_OUTBOUND_TRUNK_ID` and non-secret **setup metadata** (phone, account, domain bindings). It **never** writes `.env` files and **does not** place test calls or probe SIP authentication.

## Twilio number handling

This helper **GETs** incoming phone numbers only. It never POSTs to `/Trunks/{Sid}/PhoneNumbers`. Caller ID is configured on the LiveKit outbound trunk.

## Preview (safe default)

Host Python (no network, no `livekit` import):

```bash
python scripts/provision_sip.py
python scripts/provision_sip.py preview --phone-number +61412345678
python scripts/provision_sip.py --help
```

Preview works on Windows. **`apply` is refused on native Windows** because durable journal writes need POSIX directory fsync and `0700`/`0600` semantics — use Linux or Docker for apply (below).

### Docker preview (credential-free, no compose `.env` required)

Build an image locally (operator-run; agents do not run this for you):

```bash
docker build -t clauver:local .
docker run --rm --network none clauver:local python scripts/provision_sip.py preview
```

`--network none` ensures preview cannot reach the network even if misconfigured.

## Apply (operator-run on Linux or in Docker)

The script reads **`os.environ` only** — use an operator-managed env file outside the repo.

| Variable | Purpose |
|----------|---------|
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` | Twilio API |
| `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | LiveKit project |
| `SIP_AUTH_PASSWORD` | Optional; otherwise hidden prompt |
| `CLAUVER_PROVISION_STATE` | Journal path outside repo / app tree (default `~/.local/state/clauver/outbound-provision-state.json`) |

Host apply (Linux/macOS only):

```bash
python scripts/provision_sip.py apply \
  --phone-number +61412345678 \
  --sip-username your_term_user
```

On LiveKit Python SDK releases that expose a `failover` constructor flag (for example 1.2.x in the agent image), the helper passes `failover=False` so Twirp does not replay mutating POSTs after transport or 5xx errors.

When reusing Twilio termination credentials but **creating** a LiveKit trunk, you must type `ACK-SIP-PASSWORD` after the plan — Twilio never returns passwords over the API.

### Docker apply (persistent journal across `--rm`)

Create a **named volume per environment** (example name; use your own for staging/production):

```bash
docker volume create clauver-provision-state

docker build -t clauver:local .

docker run --rm -it \
  --env-file /path/to/operator.env \
  --mount type=volume,source=clauver-provision-state,target=/var/lib/clauver/state \
  -e CLAUVER_PROVISION_STATE=/var/lib/clauver/state/private/outbound-provision-state.json \
  clauver:local \
  python scripts/provision_sip.py apply \
    --phone-number +61412345678 \
    --sip-username your_term_user
```

Journal data survives container removal because it lives in the volume, not the container filesystem. Mount the volume at `/var/lib/clauver/state` but set `CLAUVER_PROVISION_STATE` to a path under a **`private/` subdirectory** (for example `/var/lib/clauver/state/private/outbound-provision-state.json`) so the helper can create mode `0700` parents even when the volume root is `0755`. Do **not** use `docker compose run` for provisioning: compose may require a repo `.env` even for unrelated commands, and `docker compose run` does not accept `--env-file` like `docker run`.

After success, copy `SIP_OUTBOUND_TRUNK_ID` into the env source you use for the running agent (`docker compose up` with your operator env).

## Progress journal

Default: `~/.local/state/clauver/outbound-provision-state.json` (not `~/.clauver`, which other tools may use). Must not sit inside a Git checkout or the application tree (including `/app` in Docker without `.git`).

Stores `format_version`, binding fields, status, resource IDs, **`setup_metadata`** (phone/account/domain/username bindings), and **`resources_created_by_helper`** vs **`resources_reused`**. No passwords.

**Pending** / **uncertain** statuses block further apply until reconciled in provider consoles — do not delete the journal to bypass uncertainty. Known IDs from successful POST responses are retained even when later verification fails.

### Manual recovery

1. Read the journal and `setup_metadata`.
2. Compare status and IDs with Twilio/LiveKit consoles.
3. Fix pending/uncertain rows in the cloud, then resume only from resumable statuses when live config matches the journal.

### Undo

Delete **only** resources listed under `resources_created_by_helper` after owner verification. Do **not** delete reused trunks or credential lists unless you independently own that decision.

## Voice / AI keys

Unchanged: `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`, `CARTESIA_API_KEY`, optional `BOSS_NAME`.

## Costs

See [Twilio pricing](https://www.twilio.com/pricing) and [LiveKit pricing](https://livekit.io/pricing). No quotes in this repo.

## Runtime variable

| Variable | Purpose |
|----------|---------|
| `SIP_OUTBOUND_TRUNK_ID` | LiveKit outbound trunk ID for `agent.py` / MCP |

## API references

- [Twilio SIP Trunking](https://www.twilio.com/docs/sip-trunking)
- [LiveKit outbound trunk](https://docs.livekit.io/telephony/making-calls/outbound-trunk/)

## Live tests

Live apply and outbound calls are operator-owned. This document does not describe CI or automated cloud runs.
