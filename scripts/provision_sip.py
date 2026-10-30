#!/usr/bin/env python3
"""
Standalone outbound SIP setup helper for Clauver (Twilio termination + LiveKit outbound trunk).

Default mode is preview/dry-run: no credentials file reads, no network I/O.
Use explicit ``apply`` only when you have exported credentials in your shell and
understand the cloud changes. This script never writes .env files.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import getpass
import inspect
import json
import os
import re
import secrets
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

# ASCII digits only — \d would accept Unicode numeric characters (e.g. Arabic-Indic).
E164_RE = re.compile(r"^\+[1-9][0-9]{1,14}$")
TWILIO_ACCOUNT_SID_RE = re.compile(r"^AC[0-9a-fA-F]{32}$")
TWILIO_TRUNK_SID_RE = re.compile(r"^TK[0-9a-fA-F]{32}$")
TWILIO_CL_SID_RE = re.compile(r"^CL[0-9a-fA-F]{32}$")
LIVEKIT_WSS_RE = re.compile(r"^wss://[a-zA-Z0-9._-]+\.[a-zA-Z0-9._-]+(?::\d+)?$")

TWILIO_API_HOST = "api.twilio.com"
TWILIO_TRUNKING_HOST = "trunking.twilio.com"
TWILIO_API = f"https://{TWILIO_API_HOST}"
TWILIO_TRUNKING = f"https://{TWILIO_TRUNKING_HOST}/v1"
TRUNK_FRIENDLY_NAME = "Clauver Outbound"
TRUNK_NAME_PREFIX = "clauver-outbound"
CRED_LIST_PREFIX = "clauver-outbound-term"
DEFAULT_TIMEOUT_SEC = 30
LIVEKIT_CALL_TIMEOUT_SEC = 45
MAX_RESPONSE_BYTES = 2_000_000
MAX_LIST_PAGES = 5
MAX_LIVEKIT_TRUNKS = 200
MAX_STATE_FILE_BYTES = 65_536
STATE_FORMAT_VERSION = "1"
STATE_LOCK_SUFFIX = ".lock"

BINDING_KEYS = (
    "phone_number",
    "twilio_account_sid",
    "livekit_url",
    "sip_username",
)

ALLOWED_STATE_KEYS = frozenset(
    {
        "format_version",
        *BINDING_KEYS,
        "status",
        "domain_name",
        "intended_domain",
        "twilio_trunk_sid",
        "twilio_credential_list_sid",
        "livekit_outbound_trunk_id",
        "phone_sid",
        "resources_created_by_helper",
        "resources_reused",
        "resources_planned_for_creation",
        "setup_metadata",
    }
)

RESUMABLE_STATUSES = frozenset(
    {
        "twilio_trunk_created",
        "twilio_termination_configured",
        "complete",
    }
)


class ProvisionError(Exception):
    """User-safe provisioning error (no secret payloads)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise ProvisionError("unexpected redirect from provider API")


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


@dataclass
class SettingsPreview:
    required_environment: list[str] = field(
        default_factory=lambda: [
            "TWILIO_ACCOUNT_SID",
            "TWILIO_AUTH_TOKEN",
            "LIVEKIT_URL",
            "LIVEKIT_API_KEY",
            "LIVEKIT_API_SECRET",
        ]
    )
    required_arguments: list[str] = field(
        default_factory=lambda: [
            "--phone-number (existing Twilio number, E.164 ASCII digits)",
            "--sip-username (termination username you choose, max 32 chars)",
        ]
    )
    optional_environment: list[str] = field(
        default_factory=lambda: [
            "SIP_AUTH_PASSWORD (termination password; otherwise secure prompt at apply)",
            "CLAUVER_PROVISION_STATE (path to non-secret progress file outside repo)",
        ]
    )


@dataclass
class ApplyInputs:
    phone_number: str
    sip_username: str
    sip_password: str
    twilio_account_sid: str
    twilio_auth_token: str
    livekit_url: str
    livekit_api_key: str
    livekit_api_secret: str


@dataclass
class LiveKitVerification:
    config_ok: bool
    password_verified: bool | None = None


@dataclass
class ResourcePlan:
    phone_number: str
    twilio_actions: list[str] = field(default_factory=list)
    livekit_actions: list[str] = field(default_factory=list)
    reuse: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    mutations_planned: bool = False
    resources_to_create: list[str] = field(default_factory=list)
    resources_to_reuse: list[str] = field(default_factory=list)
    setup_metadata: dict[str, str] = field(default_factory=dict)
    sip_auth_notes: list[str] = field(default_factory=list)


def _script_project_root() -> str:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(script_dir)


def find_git_worktree_root() -> str:
    start = _script_project_root()
    current = os.path.abspath(start)
    for _ in range(25):
        git_path = os.path.join(current, ".git")
        if os.path.isdir(git_path) or os.path.isfile(git_path):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return start


def validate_e164(phone_number: str) -> str:
    normalized = phone_number.strip()
    if not normalized.isascii():
        raise ProvisionError("phone_number must use ASCII characters only")
    if not E164_RE.fullmatch(normalized):
        raise ProvisionError(
            "phone_number must be strict E.164 with ASCII digits, for example +61412345678"
        )
    return normalized


def validate_twilio_account_sid(value: str) -> str:
    value = value.strip()
    if not TWILIO_ACCOUNT_SID_RE.fullmatch(value):
        raise ProvisionError("TWILIO_ACCOUNT_SID must match AC plus 32 hex chars")
    return value


def validate_twilio_trunk_sid(value: str) -> str:
    value = value.strip()
    if not TWILIO_TRUNK_SID_RE.fullmatch(value):
        raise ProvisionError("Twilio trunk SID must match TK plus 32 hex chars")
    return value


def validate_livekit_url(value: str) -> str:
    value = value.strip()
    if not LIVEKIT_WSS_RE.fullmatch(value):
        raise ProvisionError("LIVEKIT_URL must look like wss://your-project.livekit.cloud")
    return value


def validate_sip_username(value: str) -> str:
    value = value.strip()
    if not (4 <= len(value) <= 32):
        raise ProvisionError("sip username must be 4-32 characters")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", value):
        raise ProvisionError("sip username must use letters, digits, . _ - only")
    return value


def validate_sip_password(value: str) -> str:
    if not (12 <= len(value) <= 128):
        raise ProvisionError("sip password must be 12-128 characters")
    if not value.isascii():
        raise ProvisionError("sip password must be ASCII only")
    if not re.search(r"[a-z]", value):
        raise ProvisionError("sip password must include a lowercase letter")
    if not re.search(r"[A-Z]", value):
        raise ProvisionError("sip password must include an uppercase letter")
    if not re.search(r"[0-9]", value):
        raise ProvisionError("sip password must include a digit")
    return value


def validate_domain_name(domain: str) -> str:
    if not domain.startswith(f"{TRUNK_NAME_PREFIX}-") or not domain.endswith(
        ".pstn.twilio.com"
    ):
        raise ProvisionError("Twilio domain name has unexpected shape")
    return domain


def default_state_path() -> str:
    return os.path.join(
        os.path.expanduser("~"),
        ".local",
        "state",
        "clauver",
        "outbound-provision-state.json",
    )


def path_inside_git_checkout(path: str) -> bool:
    current = os.path.realpath(path)
    if os.path.isfile(current):
        current = os.path.dirname(current)
    for _ in range(30):
        git_path = os.path.join(current, ".git")
        if os.path.isdir(git_path) or os.path.isfile(git_path):
            return True
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return False


def _reject_symlink(path: str) -> None:
    if os.path.islink(path):
        raise ProvisionError("progress state path must not be a symlink")


def resolve_state_path() -> str:
    path = os.getenv("CLAUVER_PROVISION_STATE", default_state_path())
    path = os.path.abspath(os.path.expanduser(path))
    _reject_symlink(path)
    lower = path.lower()
    basename = os.path.basename(lower)
    if ".env" in basename or basename.endswith(".env"):
        raise ProvisionError("progress state path must not be a secret env file")

    repo_root = os.path.realpath(find_git_worktree_root())
    project_root = os.path.realpath(_script_project_root())
    real_path = os.path.realpath(path)
    if path_inside_git_checkout(real_path):
        raise ProvisionError(
            "progress state path must not lie inside a Git checkout"
        )
    if real_path == project_root or real_path.startswith(project_root + os.path.sep):
        raise ProvisionError(
            "progress state path must not lie inside the application checkout"
        )
    if real_path == repo_root or real_path.startswith(repo_root + os.path.sep):
        raise ProvisionError(
            "progress state path must live outside the repository checkout"
        )
    return path


def _sanitize_exception(exc: BaseException) -> ProvisionError:
    if isinstance(exc, ProvisionError):
        return exc
    return ProvisionError("provisioning failed; check inputs and provider consoles")


def require_posix_apply_platform() -> None:
    if sys.platform == "win32":
        raise ProvisionError(
            "apply is not supported on native Windows: durable journal writes require "
            "POSIX directory fsync and private file modes; run apply in Linux or Docker "
            "(see docs/setup-outbound.md). Preview remains available on Windows."
        )


def _posix_private_mode_ok(mode: int, expected: int) -> bool:
    return stat.S_IMODE(mode) == expected


def _validate_posix_path_permissions(path: str, *, expect_dir: bool) -> None:
    if os.name != "posix":
        return
    if not os.path.exists(path):
        return
    st = os.stat(path, follow_symlinks=False)
    if expect_dir and not stat.S_ISDIR(st.st_mode):
        raise ProvisionError("progress state path parent is not a directory")
    if not expect_dir and not stat.S_ISREG(st.st_mode):
        raise ProvisionError("progress state path is not a regular file")
    expected = stat.S_IRWXU if expect_dir else stat.S_IRUSR | stat.S_IWUSR
    if not _posix_private_mode_ok(st.st_mode, expected):
        raise ProvisionError(
            "progress state path has unsafe permissions; fix manually on POSIX hosts"
        )
    if st.st_uid != os.getuid():
        raise ProvisionError("progress state path is not owned by the current user")


def _prepare_private_state_directory(directory: str) -> None:
    if os.path.islink(directory):
        raise ProvisionError("progress state directory must not be a symlink")
    if os.path.exists(directory):
        _validate_posix_path_permissions(directory, expect_dir=True)
    else:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        if os.name == "posix":
            _validate_posix_path_permissions(directory, expect_dir=True)


def _fsync_parent_directory(file_path: str) -> None:
    if os.name != "posix":
        return
    directory = os.path.dirname(os.path.abspath(file_path)) or "."
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def build_setup_metadata(inputs: ApplyInputs, *, domain_name: str) -> str:
    payload = {
        "phone_number": inputs.phone_number,
        "twilio_account_sid": inputs.twilio_account_sid,
        "livekit_url": inputs.livekit_url,
        "sip_username": inputs.sip_username,
        "domain_name": domain_name,
    }
    return json.dumps(payload, sort_keys=True)


def _twilio_next_page_url(payload: dict[str, Any], api_family: str) -> str | None:
    if api_family == "v1":
        if "meta" not in payload:
            raise ProvisionError("provider list response missing meta pagination block")
        meta = payload["meta"]
        if not isinstance(meta, dict):
            raise ProvisionError("provider meta has unexpected shape")
        if "next_page_url" not in meta:
            raise ProvisionError("provider meta missing next_page_url pagination key")
        nxt = meta["next_page_url"]
        if nxt is None:
            return None
        if not isinstance(nxt, str) or nxt == "":
            raise ProvisionError("pagination link has unexpected shape")
        return nxt
    if api_family == "2010":
        if "next_page_uri" not in payload:
            raise ProvisionError(
                "provider list response missing next_page_uri pagination key"
            )
        nxt = payload["next_page_uri"]
        if nxt is None:
            return None
        if not isinstance(nxt, str) or nxt == "":
            raise ProvisionError("pagination link has unexpected shape")
        return nxt
    raise ProvisionError("internal error: unknown Twilio API family")


def _validate_twilio_url(url: str, account_sid: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ProvisionError("provider URL must use HTTPS")
    if parsed.netloc == TWILIO_API_HOST:
        if f"/Accounts/{account_sid}/" not in parsed.path:
            raise ProvisionError("Twilio API URL path does not match account")
        return
    if parsed.netloc == TWILIO_TRUNKING_HOST:
        if not parsed.path.startswith("/v1/"):
            raise ProvisionError("Twilio trunking URL path is not allowed")
        return
    raise ProvisionError("provider URL host is not allowed")


def _resolve_pagination_url(current_url: str, next_page: str, account_sid: str) -> str:
    current_host = urllib.parse.urlparse(current_url).netloc
    if next_page.startswith("/"):
        if next_page.startswith("/2010-04-01/"):
            if current_host != TWILIO_API_HOST:
                raise ProvisionError("pagination path inconsistent with list host")
            full = f"{TWILIO_API}{next_page}"
        elif next_page.startswith("/v1/"):
            if current_host != TWILIO_TRUNKING_HOST:
                raise ProvisionError("pagination path inconsistent with list host")
            full = f"https://{TWILIO_TRUNKING_HOST}{next_page}"
        else:
            raise ProvisionError("pagination URL path is not allowed")
        _validate_twilio_url(full, account_sid)
        return full
    _validate_twilio_url(next_page, account_sid)
    next_host = urllib.parse.urlparse(next_page).netloc
    if next_host != current_host:
        raise ProvisionError("pagination host changed unexpectedly")
    return next_page


def http_request(
    method: str,
    url: str,
    *,
    account_sid: str,
    auth: tuple[str, str] | None = None,
    data: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT_SEC,
) -> dict[str, Any]:
    _validate_twilio_url(url, account_sid)
    body = None
    headers = {"Accept": "application/json"}
    if data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    if auth:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode("ascii")
        req.add_header("Authorization", f"Basic {token}")
    try:
        with _NO_REDIRECT_OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ProvisionError("provider response exceeded size limit")
            if not raw:
                return {}
            try:
                payload = json.loads(raw.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise ProvisionError("provider returned invalid UTF-8") from exc
    except urllib.error.HTTPError as exc:
        raise ProvisionError(f"HTTP {exc.code} from provider API") from exc
    except urllib.error.URLError as exc:
        raise ProvisionError("network error reaching provider API") from exc
    except json.JSONDecodeError as exc:
        raise ProvisionError("provider returned non-JSON response") from exc
    if not isinstance(payload, dict):
        raise ProvisionError("provider returned unexpected JSON shape")
    return payload


def twilio_list_collection(
    inputs: ApplyInputs,
    start_url: str,
    collection_key: str,
    *,
    api_family: str,
) -> list[dict[str, Any]]:
    account_sid, token = twilio_auth(inputs)
    auth = (account_sid, token)
    url = start_url
    items: list[dict[str, Any]] = []
    for _ in range(MAX_LIST_PAGES):
        payload = http_request("GET", url, account_sid=account_sid, auth=auth)
        chunk = payload.get(collection_key)
        if chunk is None:
            raise ProvisionError("provider list response missing expected collection")
        if not isinstance(chunk, list):
            raise ProvisionError("provider list collection has unexpected shape")
        for entry in chunk:
            if not isinstance(entry, dict):
                raise ProvisionError("provider list item has unexpected shape")
            items.append(entry)
        next_page = _twilio_next_page_url(payload, api_family)
        if not next_page:
            return items
        url = _resolve_pagination_url(url, next_page, account_sid)
    raise ProvisionError(
        "provider list may be truncated; reconcile manually before apply"
    )


def twilio_auth(inputs: ApplyInputs) -> tuple[str, str]:
    return inputs.twilio_account_sid, inputs.twilio_auth_token


def twilio_list_incoming_numbers(inputs: ApplyInputs) -> list[dict[str, Any]]:
    account_sid, _ = twilio_auth(inputs)
    url = (
        f"{TWILIO_API}/2010-04-01/Accounts/{account_sid}/"
        f"IncomingPhoneNumbers.json?PageSize=50"
    )
    return twilio_list_collection(
        inputs, url, "incoming_phone_numbers", api_family="2010"
    )


def twilio_list_trunks(inputs: ApplyInputs) -> list[dict[str, Any]]:
    url = f"{TWILIO_TRUNKING}/Trunks?PageSize=50"
    return twilio_list_collection(inputs, url, "trunks", api_family="v1")


def twilio_trunk_credential_lists(
    inputs: ApplyInputs, trunk_sid: str
) -> list[dict[str, Any]]:
    validate_twilio_trunk_sid(trunk_sid)
    url = f"{TWILIO_TRUNKING}/Trunks/{trunk_sid}/CredentialLists?PageSize=20"
    return twilio_list_collection(inputs, url, "credential_lists", api_family="v1")


def twilio_list_credentials(
    inputs: ApplyInputs, credential_list_sid: str
) -> list[dict[str, Any]]:
    if not TWILIO_CL_SID_RE.fullmatch(credential_list_sid):
        raise ProvisionError("credential list SID has unexpected shape")
    account_sid, _ = twilio_auth(inputs)
    url = (
        f"{TWILIO_API}/2010-04-01/Accounts/{account_sid}/"
        f"SIP/CredentialLists/{credential_list_sid}/Credentials.json?PageSize=50"
    )
    return twilio_list_collection(inputs, url, "credentials", api_family="2010")


def select_clauver_trunks(trunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matches = []
    for trunk in trunks:
        if not isinstance(trunk.get("sid"), str):
            continue
        name = (trunk.get("friendly_name") or "").lower()
        domain = (trunk.get("domain_name") or "").lower()
        if name == TRUNK_FRIENDLY_NAME.lower() or name.startswith(TRUNK_NAME_PREFIX):
            matches.append(trunk)
            continue
        if domain.startswith(f"{TRUNK_NAME_PREFIX}-") and domain.endswith(
            ".pstn.twilio.com"
        ):
            matches.append(trunk)
    return matches


def generate_planned_trunk_domain() -> str:
    suffix = secrets.token_hex(3)
    return validate_domain_name(f"{TRUNK_NAME_PREFIX}-{suffix}.pstn.twilio.com")


def build_preview_plan(phone_number: str | None) -> ResourcePlan:
    return ResourcePlan(
        phone_number=phone_number or "<your existing Twilio E.164 number>",
        mutations_planned=False,
        twilio_actions=[
            "Verify Twilio credentials and number ownership (GET only)",
            "Create or reuse Clauver outbound Twilio trunk + termination credentials",
            "Never attach the number to the Twilio trunk (preserves inbound routing)",
        ],
        livekit_actions=[
            "List outbound trunks (read-only)",
            "Create or reuse LiveKit outbound trunk with your number as caller ID",
        ],
        warnings=[
            "No inbound trunks, origination URLs, dispatch rules, or calls.",
            "No .env writes; save SIP_OUTBOUND_TRUNK_ID yourself.",
            "Voice/AI keys stay user-configured (see docs/setup-outbound.md).",
        ],
    )


def load_sip_password() -> str:
    env_pass = os.getenv("SIP_AUTH_PASSWORD", "")
    if env_pass:
        return validate_sip_password(env_pass)
    prompted = getpass.getpass("SIP termination password: ")
    return validate_sip_password(prompted)


def load_apply_inputs(args: argparse.Namespace) -> ApplyInputs:
    phone = validate_e164(args.phone_number)
    sip_user = validate_sip_username(args.sip_username)
    sip_pass = load_sip_password()

    tw_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    tw_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    lk_url = os.getenv("LIVEKIT_URL", "").strip()
    lk_key = os.getenv("LIVEKIT_API_KEY", "").strip()
    lk_secret = os.getenv("LIVEKIT_API_SECRET", "").strip()

    missing = [
        name
        for name, val in [
            ("TWILIO_ACCOUNT_SID", tw_sid),
            ("TWILIO_AUTH_TOKEN", tw_token),
            ("LIVEKIT_URL", lk_url),
            ("LIVEKIT_API_KEY", lk_key),
            ("LIVEKIT_API_SECRET", lk_secret),
        ]
        if not val
    ]
    if missing:
        raise ProvisionError(
            "missing environment variables for apply: " + ", ".join(missing)
        )

    return ApplyInputs(
        phone_number=phone,
        sip_username=sip_user,
        sip_password=sip_pass,
        twilio_account_sid=validate_twilio_account_sid(tw_sid),
        twilio_auth_token=tw_token,
        livekit_url=validate_livekit_url(lk_url),
        livekit_api_key=lk_key,
        livekit_api_secret=lk_secret,
    )


def state_binding(inputs: ApplyInputs) -> dict[str, str]:
    return {
        "format_version": STATE_FORMAT_VERSION,
        "phone_number": inputs.phone_number,
        "twilio_account_sid": inputs.twilio_account_sid,
        "livekit_url": inputs.livekit_url,
        "sip_username": inputs.sip_username,
    }


def assert_state_binding(state: dict[str, str], inputs: ApplyInputs) -> None:
    binding = state_binding(inputs)
    for key, expected in binding.items():
        if state.get(key) != expected:
            raise ProvisionError(
                "progress file does not match current apply inputs; reconcile manually"
            )


def validate_state_schema(state: dict[str, str], inputs: ApplyInputs | None) -> dict[str, str]:
    if not state:
        return {}
    if state.get("format_version") != STATE_FORMAT_VERSION:
        raise ProvisionError("progress file format is not recognised; reconcile manually")
    for key in BINDING_KEYS:
        if key not in state:
            raise ProvisionError("progress file missing required binding fields")
    for key in state:
        if key not in ALLOWED_STATE_KEYS:
            raise ProvisionError("progress file contains unrecognised fields")
    status = state.get("status", "")
    if status:
        if status.startswith("pending_") or status.startswith("uncertain_"):
            pass
        elif status not in RESUMABLE_STATUSES:
            raise ProvisionError("progress file has unrecognised status value")
    if inputs is not None:
        assert_state_binding(state, inputs)
    return state


def enforce_journal_allows_apply(state: dict[str, str]) -> None:
    status = state.get("status", "")
    if not status:
        return
    if status.startswith("uncertain_") or status.startswith("pending_"):
        raise ProvisionError(
            "journal has pending or uncertain operation; manual reconciliation required "
            "before apply (do not discard the journal to bypass this)"
        )


def append_resource_tag(existing: str, tag: str) -> str:
    parts = [p for p in existing.split(",") if p]
    if tag not in parts:
        parts.append(tag)
    return ",".join(parts)


class StateSession:
    def __init__(self, path: str) -> None:
        self.path = path
        self.lock_path = path + STATE_LOCK_SUFFIX
        _reject_symlink(self.lock_path)

    def __enter__(self) -> StateSession:
        if os.path.exists(self.lock_path):
            raise ProvisionError(
                "another provisioning run may be in progress; remove lock only if stale"
            )
        directory = os.path.dirname(self.path) or "."
        _prepare_private_state_directory(directory)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("locked\n")
            handle.flush()
            os.fsync(handle.fileno())
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        if os.path.exists(self.lock_path):
            os.remove(self.lock_path)

    def load_raw(self) -> dict[str, str]:
        if not os.path.isfile(self.path):
            return {}
        _reject_symlink(self.path)
        _validate_posix_path_permissions(self.path, expect_dir=False)
        size = os.path.getsize(self.path)
        if size > MAX_STATE_FILE_BYTES:
            raise ProvisionError("progress state file exceeds size limit")
        with open(self.path, encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ProvisionError("progress state file is not a JSON object")
        return {str(k): str(v) for k, v in data.items()}

    def load(self, inputs: ApplyInputs) -> dict[str, str]:
        return validate_state_schema(self.load_raw(), inputs)

    def save(self, payload: dict[str, str]) -> None:
        validated = validate_state_schema(payload, None)
        encoded = json.dumps(validated, indent=2, sort_keys=True) + "\n"
        if len(encoded.encode("utf-8")) > MAX_STATE_FILE_BYTES:
            raise ProvisionError("progress state payload exceeds size limit")
        directory = os.path.dirname(self.path) or "."
        tmp_name = f".{os.path.basename(self.path)}.{secrets.token_hex(8)}.tmp"
        tmp_path = os.path.join(directory, tmp_name)
        fd = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self.path)
        if os.name == "posix":
            os.chmod(self.path, 0o600)
        _fsync_parent_directory(self.path)


def record_intent(session: StateSession, inputs: ApplyInputs, **fields: str) -> None:
    current = session.load_raw()
    if current:
        validate_state_schema(current, inputs)
    merged = {**current, **state_binding(inputs), **fields}
    session.save(merged)


def resolve_phone_record(inputs: ApplyInputs) -> str:
    numbers = twilio_list_incoming_numbers(inputs)
    selected = [n for n in numbers if n.get("phone_number") == inputs.phone_number]
    if not selected:
        raise ProvisionError("phone number was not found on the Twilio account")
    if len(selected) > 1:
        raise ProvisionError("ambiguous phone number matches on Twilio account")
    sid = selected[0].get("sid")
    if not isinstance(sid, str) or not sid.startswith("PN"):
        raise ProvisionError("Twilio phone record has unexpected shape")
    return sid


def fetch_twilio_trunk_by_sid(
    inputs: ApplyInputs, trunk_sid: str, *, expected_domain: str | None = None
) -> dict[str, Any] | None:
    validate_twilio_trunk_sid(trunk_sid)
    for trunk in twilio_list_trunks(inputs):
        if trunk.get("sid") == trunk_sid:
            domain = trunk.get("domain_name")
            if not isinstance(domain, str):
                raise ProvisionError("Twilio trunk domain has unexpected shape")
            validate_domain_name(domain)
            if expected_domain and domain != expected_domain:
                raise ProvisionError(
                    "Twilio trunk domain does not match journal or plan; reconcile manually"
                )
            return trunk
    return None


def verify_trunk_credential_list_attached(
    inputs: ApplyInputs, trunk_sid: str, credential_list_sid: str
) -> None:
    attached = twilio_trunk_credential_lists(inputs, trunk_sid)
    sids = [item.get("sid") for item in attached if isinstance(item.get("sid"), str)]
    if credential_list_sid not in sids:
        raise ProvisionError(
            "Twilio credential list attach could not be verified; reconcile manually"
        )


def validate_journal_credential_list_attached(
    inputs: ApplyInputs,
    progress: dict[str, str],
    trunk_sid: str,
) -> None:
    journal_cl = progress.get("twilio_credential_list_sid")
    if not journal_cl:
        return
    if not TWILIO_CL_SID_RE.fullmatch(journal_cl):
        raise ProvisionError("journal credential list SID has unexpected shape")
    attached = twilio_trunk_credential_lists(inputs, trunk_sid)
    attached_sids = [
        item.get("sid") for item in attached if isinstance(item.get("sid"), str)
    ]
    if journal_cl not in attached_sids:
        raise ProvisionError(
            "journal credential list is not attached to the Twilio trunk; reconcile manually"
        )


def inspect_twilio_trunk_credentials(
    inputs: ApplyInputs, trunk_sid: str
) -> tuple[str | None, bool]:
    """Return (credential_list_sid, credentials_ok)."""
    lists = twilio_trunk_credential_lists(inputs, trunk_sid)
    if len(lists) > 1:
        raise ProvisionError(
            "Twilio trunk has unexpected extra credential lists; resolve manually"
        )
    if len(lists) == 1:
        chosen = lists[0]
        if not (chosen.get("friendly_name") or "").lower().startswith(CRED_LIST_PREFIX):
            raise ProvisionError(
                "Twilio trunk has a non-Clauver credential list attached; resolve manually"
            )
    else:
        return None, False

    cred_sid = chosen.get("sid")
    if not isinstance(cred_sid, str) or not TWILIO_CL_SID_RE.fullmatch(cred_sid):
        raise ProvisionError("credential list record has unexpected shape")
    creds = twilio_list_credentials(inputs, cred_sid)
    if len(creds) == 0:
        raise ProvisionError(
            "Twilio trunk has an empty credential list attached; add credentials "
            "manually or remove the empty list before apply (this helper will not "
            "attach a second list)"
        )
    usernames = [c.get("username") for c in creds if c.get("username")]
    if len(usernames) > 1:
        raise ProvisionError(
            "credential list has unexpected extra usernames; resolve manually"
        )
    if len(creds) > 1:
        raise ProvisionError(
            "credential list has unexpected extra credentials; resolve manually"
        )
    if usernames and usernames[0] != inputs.sip_username:
        raise ProvisionError(
            "existing termination username on Twilio does not match --sip-username"
        )
    return cred_sid, bool(usernames)


def create_twilio_trunk(
    inputs: ApplyInputs, session: StateSession, *, domain_name: str
) -> dict[str, Any]:
    validate_domain_name(domain_name)
    record_intent(
        session,
        inputs,
        status="pending_twilio_trunk_create",
        intended_domain=domain_name,
        resources_planned_for_creation=append_resource_tag(
            session.load_raw().get("resources_planned_for_creation", ""),
            "twilio_trunk",
        ),
    )
    account_sid, token = twilio_auth(inputs)
    url = f"{TWILIO_TRUNKING}/Trunks"
    payload = http_request(
        "POST",
        url,
        account_sid=account_sid,
        auth=(account_sid, token),
        data={
            "FriendlyName": TRUNK_FRIENDLY_NAME,
            "DomainName": domain_name,
        },
    )
    sid = payload.get("sid")
    if not isinstance(sid, str):
        record_intent(session, inputs, status="uncertain_twilio_trunk_create")
        raise ProvisionError(
            "Twilio trunk create outcome uncertain; reconcile in console before retry"
        )
    validate_twilio_trunk_sid(sid)
    domain = payload.get("domain_name")
    if domain != domain_name:
        record_intent(
            session,
            inputs,
            status="uncertain_twilio_trunk_create",
            twilio_trunk_sid=sid,
            domain_name=str(domain or ""),
            setup_metadata=build_setup_metadata(inputs, domain_name=domain_name),
        )
        raise ProvisionError("Twilio trunk create returned unexpected domain")
    record_intent(
        session,
        inputs,
        status="twilio_trunk_created",
        twilio_trunk_sid=sid,
        domain_name=domain_name,
        resources_created_by_helper=append_resource_tag(
            session.load_raw().get("resources_created_by_helper", ""),
            "twilio_trunk",
        ),
        setup_metadata=build_setup_metadata(inputs, domain_name=domain_name),
    )
    return payload


def create_twilio_credential_list(
    inputs: ApplyInputs,
    session: StateSession,
    trunk_sid: str,
    suffix: str,
) -> str:
    record_intent(
        session,
        inputs,
        status="pending_twilio_credential_list_create",
        twilio_trunk_sid=trunk_sid,
    )
    account_sid, token = twilio_auth(inputs)
    url = f"{TWILIO_API}/2010-04-01/Accounts/{account_sid}/SIP/CredentialLists.json"
    payload = http_request(
        "POST",
        url,
        account_sid=account_sid,
        auth=(account_sid, token),
        data={"FriendlyName": f"{CRED_LIST_PREFIX}-{suffix}"},
    )
    list_sid = payload.get("sid")
    if not isinstance(list_sid, str) or not TWILIO_CL_SID_RE.fullmatch(list_sid):
        record_intent(session, inputs, status="uncertain_twilio_credential_list_create")
        raise ProvisionError(
            "Twilio credential list create outcome uncertain; reconcile before retry"
        )
    record_intent(
        session,
        inputs,
        status="pending_twilio_credential_create",
        twilio_credential_list_sid=list_sid,
        resources_created_by_helper=append_resource_tag(
            session.load_raw().get("resources_created_by_helper", ""),
            "twilio_credential_list",
        ),
        setup_metadata=build_setup_metadata(
            inputs,
            domain_name=session.load_raw().get("domain_name", ""),
        ),
    )
    cred_url = (
        f"{TWILIO_API}/2010-04-01/Accounts/{account_sid}/"
        f"SIP/CredentialLists/{list_sid}/Credentials.json"
    )
    http_request(
        "POST",
        cred_url,
        account_sid=account_sid,
        auth=(account_sid, token),
        data={"Username": inputs.sip_username, "Password": inputs.sip_password},
    )
    record_intent(
        session,
        inputs,
        status="pending_twilio_credential_attach",
        twilio_credential_list_sid=list_sid,
    )
    attach_url = f"{TWILIO_TRUNKING}/Trunks/{trunk_sid}/CredentialLists"
    attach_payload = http_request(
        "POST",
        attach_url,
        account_sid=account_sid,
        auth=(account_sid, token),
        data={"CredentialListSid": list_sid},
    )
    if attach_payload.get("sid") != list_sid and attach_payload.get(
        "credential_list_sid"
    ) not in (None, list_sid):
        record_intent(session, inputs, status="uncertain_twilio_credential_attach")
        raise ProvisionError(
            "Twilio credential attach response could not be validated; reconcile manually"
        )
    verify_trunk_credential_list_attached(inputs, trunk_sid, list_sid)
    _, credentials_ok = inspect_twilio_trunk_credentials(inputs, trunk_sid)
    if not credentials_ok:
        record_intent(session, inputs, status="uncertain_twilio_credential_attach")
        raise ProvisionError(
            "Twilio termination auth is not verified after attach; reconcile manually"
        )
    record_intent(
        session,
        inputs,
        status="twilio_termination_configured",
        twilio_credential_list_sid=list_sid,
        setup_metadata=build_setup_metadata(
            inputs,
            domain_name=session.load_raw().get("domain_name", ""),
        ),
    )
    return list_sid


def build_livekit_api_client(
    api_module: Any,
    *,
    url: str,
    api_key: str,
    api_secret: str,
) -> Any:
    """Construct LiveKitAPI; disable Twirp failover retries when SDK supports it."""
    init_params = inspect.signature(api_module.LiveKitAPI.__init__).parameters
    if "failover" in init_params:
        return api_module.LiveKitAPI(url, api_key, api_secret, failover=False)
    return api_module.LiveKitAPI(url, api_key, api_secret)


async def discover_livekit_outbound_trunks(inputs: ApplyInputs) -> list[Any]:
    from livekit import api
    from livekit.protocol.sip import ListSIPOutboundTrunkRequest

    async def _call() -> list[Any]:
        lkapi = build_livekit_api_client(
            api,
            url=inputs.livekit_url,
            api_key=inputs.livekit_api_key,
            api_secret=inputs.livekit_api_secret,
        )
        try:
            result = await lkapi.sip.list_outbound_trunk(ListSIPOutboundTrunkRequest())
            items = list(result.items)
            if len(items) > MAX_LIVEKIT_TRUNKS:
                raise ProvisionError(
                    "LiveKit outbound trunk list too large; reconcile manually"
                )
            return items
        finally:
            await lkapi.aclose()

    try:
        return await asyncio.wait_for(_call(), timeout=LIVEKIT_CALL_TIMEOUT_SEC)
    except TimeoutError as exc:
        raise ProvisionError("LiveKit API call timed out") from exc


def confirm_apply(plan: ResourcePlan) -> None:
    print()
    print("Apply plan (cloud mutations if you continue):")
    print(json.dumps(plan.__dict__, indent=2, sort_keys=True))
    print()
    answer = input("Type APPLY to continue: ").strip()
    if answer != "APPLY":
        raise ProvisionError("apply cancelled before any cloud mutation")


def confirm_reused_twilio_sip_password() -> None:
    print()
    print(
        "Reused Twilio termination credentials cannot be verified via Twilio API "
        "(passwords are never returned)."
    )
    print(
        "Before creating a LiveKit outbound trunk, confirm the SIP password already "
        "configured on Twilio matches what you supplied for this run."
    )
    answer = input("Type ACK-SIP-PASSWORD to continue: ").strip()
    if answer != "ACK-SIP-PASSWORD":
        raise ProvisionError(
            "apply cancelled: reconcile the existing Twilio SIP password manually first"
        )


def expected_livekit_outbound_transport() -> int:
    from livekit.protocol import sip

    return int(sip.SIP_TRANSPORT_AUTO)


def livekit_trunk_matches(
    item: Any,
    *,
    domain_name: str,
    phone_number: str,
    username: str,
    sip_password: str | None = None,
) -> LiveKitVerification:
    numbers = [str(n) for n in (getattr(item, "numbers", []) or [])]
    if sorted(numbers) != [phone_number]:
        return LiveKitVerification(False, None)
    address = getattr(item, "address", "") or ""
    auth_user = getattr(item, "auth_username", "") or ""
    if address != domain_name or auth_user != username:
        return LiveKitVerification(False, None)
    expected_transport = expected_livekit_outbound_transport()
    item_transport = getattr(item, "transport", None)
    if item_transport is None or int(item_transport) != expected_transport:
        return LiveKitVerification(False, None)
    auth_pass = getattr(item, "auth_password", "") or ""
    if auth_pass:
        if sip_password is None:
            return LiveKitVerification(True, False)
        return LiveKitVerification(True, auth_pass == sip_password)
    return LiveKitVerification(True, None)


def find_livekit_trunk_by_id(items: list[Any], trunk_id: str) -> Any | None:
    for item in items:
        if getattr(item, "sip_trunk_id", "") == trunk_id:
            return item
    return None


async def verify_livekit_trunk_config(
    inputs: ApplyInputs,
    trunk_id: str,
    *,
    domain_name: str,
    phone_number: str,
    username: str,
    sip_password: str | None = None,
) -> LiveKitVerification:
    items = await discover_livekit_outbound_trunks(inputs)
    item = find_livekit_trunk_by_id(items, trunk_id)
    if item is None:
        return LiveKitVerification(False, None)
    return livekit_trunk_matches(
        item,
        domain_name=domain_name,
        phone_number=phone_number,
        username=username,
        sip_password=sip_password,
    )


async def create_livekit_outbound_trunk(
    inputs: ApplyInputs,
    session: StateSession,
    *,
    domain_name: str,
) -> str:
    from livekit import api
    from livekit.protocol.sip import CreateSIPOutboundTrunkRequest, SIPOutboundTrunkInfo
    from livekit.protocol import sip as sip_protocol

    record_intent(
        session,
        inputs,
        status="pending_livekit_outbound_trunk_create",
        domain_name=domain_name,
    )

    async def _call() -> str:
        lkapi = build_livekit_api_client(
            api,
            url=inputs.livekit_url,
            api_key=inputs.livekit_api_key,
            api_secret=inputs.livekit_api_secret,
        )
        try:
            trunk_info = SIPOutboundTrunkInfo(
                name=TRUNK_FRIENDLY_NAME,
                address=domain_name,
                numbers=[inputs.phone_number],
                auth_username=inputs.sip_username,
                auth_password=inputs.sip_password,
                transport=sip_protocol.SIP_TRANSPORT_AUTO,
            )
            result = await lkapi.sip.create_outbound_trunk(
                CreateSIPOutboundTrunkRequest(trunk=trunk_info)
            )
            trunk_id = getattr(result, "sip_trunk_id", None)
            if not trunk_id:
                raise ProvisionError(
                    "LiveKit outbound trunk create returned no id; reconcile before retry"
                )
            trunk_id_str = str(trunk_id)
            record_intent(
                session,
                inputs,
                status="pending_livekit_outbound_trunk_verify",
                livekit_outbound_trunk_id=trunk_id_str,
                domain_name=domain_name,
                resources_created_by_helper=append_resource_tag(
                    session.load_raw().get("resources_created_by_helper", ""),
                    "livekit_outbound_trunk",
                ),
                setup_metadata=build_setup_metadata(inputs, domain_name=domain_name),
            )
            return trunk_id_str
        finally:
            await lkapi.aclose()

    try:
        trunk_id = await asyncio.wait_for(_call(), timeout=LIVEKIT_CALL_TIMEOUT_SEC)
    except TimeoutError as exc:
        record_intent(session, inputs, status="uncertain_livekit_outbound_trunk_create")
        raise ProvisionError("LiveKit outbound trunk create timed out") from exc
    except ProvisionError:
        record_intent(session, inputs, status="uncertain_livekit_outbound_trunk_create")
        raise
    except Exception as exc:
        record_intent(session, inputs, status="uncertain_livekit_outbound_trunk_create")
        raise _sanitize_exception(exc) from exc

    verification = await verify_livekit_trunk_config(
        inputs,
        trunk_id,
        domain_name=domain_name,
        phone_number=inputs.phone_number,
        username=inputs.sip_username,
        sip_password=inputs.sip_password,
    )
    if not verification.config_ok:
        record_intent(
            session,
            inputs,
            status="uncertain_livekit_outbound_trunk_create",
            livekit_outbound_trunk_id=trunk_id,
            domain_name=domain_name,
            setup_metadata=build_setup_metadata(inputs, domain_name=domain_name),
        )
        raise ProvisionError(
            "LiveKit trunk created but configuration could not be verified; reconcile manually"
        )
    if verification.password_verified is False:
        record_intent(
            session,
            inputs,
            status="uncertain_livekit_outbound_trunk_create",
            livekit_outbound_trunk_id=trunk_id,
            domain_name=domain_name,
            setup_metadata=build_setup_metadata(inputs, domain_name=domain_name),
        )
        raise ProvisionError(
            "LiveKit trunk auth_password does not match supplied password; reconcile manually"
        )

    record_intent(
        session,
        inputs,
        status="complete",
        livekit_outbound_trunk_id=trunk_id,
        domain_name=domain_name,
        setup_metadata=build_setup_metadata(inputs, domain_name=domain_name),
    )
    return trunk_id


def print_preview(phone_number: str | None) -> None:
    settings = SettingsPreview()
    plan = build_preview_plan(phone_number)
    print("Clauver outbound SIP setup (preview / dry-run)")
    print()
    print("Required environment variables (export in your shell before apply):")
    for name in settings.required_environment:
        print(f"  - {name}")
    print()
    print("Required apply arguments:")
    for item in settings.required_arguments:
        print(f"  - {item}")
    print()
    print("Optional environment:")
    for name in settings.optional_environment:
        print(f"  - {name}")
    print()
    print(f"Target phone number: {plan.phone_number}")
    print()
    print("Intended Twilio steps:")
    for step in plan.twilio_actions:
        print(f"  - {step}")
    print()
    print("Intended LiveKit steps:")
    for step in plan.livekit_actions:
        print(f"  - {step}")
    print()
    for warning in plan.warnings:
        print(f"Note: {warning}")
    print()
    print("No network calls were made in preview mode.")


def _journal_twilio_trunk_sid(progress: dict[str, str]) -> str | None:
    sid = progress.get("twilio_trunk_sid")
    if sid:
        validate_twilio_trunk_sid(sid)
    return sid


async def run_apply(args: argparse.Namespace) -> int:
    require_posix_apply_platform()
    inputs = load_apply_inputs(args)
    state_path = resolve_state_path()

    with StateSession(state_path) as session:
        progress = session.load(inputs)
        enforce_journal_allows_apply(progress)

        plan = ResourcePlan(phone_number=inputs.phone_number, mutations_planned=False)
        plan.warnings = [
            "Twilio phone numbers are never attached to the SIP trunk by this helper.",
            "Save SIP_OUTBOUND_TRUNK_ID locally after success; no .env write.",
            "This helper does not place test calls or probe SIP authentication.",
        ]

        _ = resolve_phone_record(inputs)

        journal_trunk_sid = _journal_twilio_trunk_sid(progress) if progress else None
        journal_domain = progress.get("domain_name") or progress.get("intended_domain")
        journal_lk_id = progress.get("livekit_outbound_trunk_id", "")
        journal_cred_sid = progress.get("twilio_credential_list_sid", "")

        trunk: dict[str, Any] | None = None
        clauver_trunks = select_clauver_trunks(twilio_list_trunks(inputs))
        if len(clauver_trunks) > 1:
            raise ProvisionError(
                "multiple Clauver outbound Twilio trunks found; resolve manually"
            )
        if journal_trunk_sid:
            trunk = fetch_twilio_trunk_by_sid(
                inputs,
                journal_trunk_sid,
                expected_domain=journal_domain or None,
            )
            if not trunk:
                raise ProvisionError(
                    "journal Twilio trunk id is missing or stale on the account"
                )
            if clauver_trunks and clauver_trunks[0]["sid"] != journal_trunk_sid:
                raise ProvisionError(
                    "journal names a different Twilio trunk than the Clauver trunk on account"
                )
        elif clauver_trunks:
            trunk = clauver_trunks[0]

        if trunk and progress:
            validate_journal_credential_list_attached(inputs, progress, trunk["sid"])

        domain_name = (trunk or {}).get("domain_name") or journal_domain or ""
        planned_domain = ""
        need_create_trunk = trunk is None
        if need_create_trunk:
            planned_domain = generate_planned_trunk_domain()
            domain_name = planned_domain

        cred_list_sid: str | None = None
        credentials_ok = False
        if trunk:
            cred_list_sid, credentials_ok = inspect_twilio_trunk_credentials(
                inputs, trunk["sid"]
            )
        if journal_cred_sid and cred_list_sid and journal_cred_sid != cred_list_sid:
            raise ProvisionError(
                "journal credential list SID does not match the list attached to the trunk"
            )
        need_credentials = need_create_trunk or not credentials_ok
        twilio_auth_reused = not need_credentials and bool(credentials_ok)

        if need_create_trunk:
            plan.twilio_actions.append(f"Create Twilio trunk domain {planned_domain}")
            plan.resources_to_create.append("twilio_trunk")
            plan.mutations_planned = True
        elif trunk:
            plan.reuse["twilio_trunk_sid"] = trunk["sid"]
            plan.reuse["twilio_domain"] = trunk.get("domain_name", "")
            plan.resources_to_reuse.append("twilio_trunk")
            plan.twilio_actions.append(f"Reuse Twilio trunk {trunk['sid']}")

        if need_credentials:
            plan.twilio_actions.append(
                "Create credential list, credential, and attach to Twilio trunk"
            )
            plan.resources_to_create.append("twilio_credential_list")
            plan.mutations_planned = True
        elif cred_list_sid:
            plan.reuse["twilio_credential_list_sid"] = cred_list_sid
            plan.resources_to_reuse.append("twilio_credential_list")
            plan.sip_auth_notes.append(
                "Twilio SIP password cannot be verified via API when credentials are reused"
            )

        plan.setup_metadata = json.loads(
            build_setup_metadata(inputs, domain_name=domain_name or planned_domain)
        )

        lk_items = await discover_livekit_outbound_trunks(inputs)
        lk_matches = (
            match_livekit_trunk(
                lk_items,
                domain_name=domain_name,
                phone_number=inputs.phone_number,
                username=inputs.sip_username,
                sip_password=inputs.sip_password,
            )
            if domain_name
            else []
        )

        if len(lk_matches) > 1:
            raise ProvisionError("ambiguous LiveKit outbound trunk matches")

        livekit_trunk_id = ""
        need_create_livekit = False
        if journal_lk_id:
            if lk_matches and getattr(lk_matches[0], "sip_trunk_id", "") != journal_lk_id:
                raise ProvisionError(
                    "journal LiveKit trunk id disagrees with matching trunk in project"
                )
            lk_verify = await verify_livekit_trunk_config(
                inputs,
                journal_lk_id,
                domain_name=domain_name,
                phone_number=inputs.phone_number,
                username=inputs.sip_username,
                sip_password=inputs.sip_password,
            )
            if not lk_verify.config_ok:
                raise ProvisionError(
                    "journal LiveKit trunk id exists but does not match domain, "
                    "exact caller ID set, or username; reconcile manually"
                )
            if lk_verify.password_verified is False:
                raise ProvisionError(
                    "journal LiveKit trunk auth_password does not match supplied password"
                )
            if lk_verify.password_verified is None:
                plan.sip_auth_notes.append(
                    "LiveKit SIP password not exposed by API; password not verified"
                )
            livekit_trunk_id = journal_lk_id
            plan.reuse["livekit_outbound_trunk_id"] = livekit_trunk_id
            plan.resources_to_reuse.append("livekit_outbound_trunk")
            plan.livekit_actions.append(f"Reuse LiveKit outbound trunk {livekit_trunk_id}")
        elif len(lk_matches) == 1:
            livekit_trunk_id = str(getattr(lk_matches[0], "sip_trunk_id", ""))
            lk_item = lk_matches[0]
            lk_check = livekit_trunk_matches(
                lk_item,
                domain_name=domain_name,
                phone_number=inputs.phone_number,
                username=inputs.sip_username,
                sip_password=inputs.sip_password,
            )
            if lk_check.password_verified is False:
                raise ProvisionError(
                    "matching LiveKit trunk auth_password does not match supplied password"
                )
            if lk_check.password_verified is None:
                plan.sip_auth_notes.append(
                    "LiveKit SIP password not exposed by API; password not verified"
                )
            plan.reuse["livekit_outbound_trunk_id"] = livekit_trunk_id
            plan.resources_to_reuse.append("livekit_outbound_trunk")
            plan.livekit_actions.append(f"Reuse LiveKit outbound trunk {livekit_trunk_id}")
        else:
            need_create_livekit = True
            plan.livekit_actions.append(
                f"Create LiveKit outbound trunk for domain {domain_name} "
                f"with caller ID {inputs.phone_number}"
            )
            plan.resources_to_create.append("livekit_outbound_trunk")
            plan.mutations_planned = True

        if not plan.mutations_planned:
            plan.warnings.append("No cloud mutations required after confirmation.")

        if need_create_livekit and twilio_auth_reused:
            confirm_reused_twilio_sip_password()

        confirm_apply(plan)

        if need_create_trunk:
            trunk = create_twilio_trunk(inputs, session, domain_name=planned_domain)
            domain_name = str(trunk.get("domain_name", ""))

        assert trunk is not None
        domain_name = str(trunk.get("domain_name") or domain_name)
        validate_domain_name(domain_name)

        if need_credentials:
            suffix = secrets.token_hex(3)
            create_twilio_credential_list(inputs, session, trunk["sid"], suffix)

        if need_create_livekit:
            livekit_trunk_id = await create_livekit_outbound_trunk(
                inputs, session, domain_name=domain_name
            )
        elif livekit_trunk_id:
            lk_verify = await verify_livekit_trunk_config(
                inputs,
                livekit_trunk_id,
                domain_name=domain_name,
                phone_number=inputs.phone_number,
                username=inputs.sip_username,
                sip_password=inputs.sip_password,
            )
            if not lk_verify.config_ok:
                raise ProvisionError(
                    "LiveKit trunk configuration could not be verified; reconcile manually"
                )
            if lk_verify.password_verified is False:
                raise ProvisionError(
                    "LiveKit trunk auth_password does not match supplied password"
                )
            reused_tags = append_resource_tag(
                progress.get("resources_reused", ""),
                "twilio_trunk,livekit_outbound_trunk,twilio_credential_list",
            )
            record_intent(
                session,
                inputs,
                status="complete",
                twilio_trunk_sid=trunk["sid"],
                domain_name=domain_name,
                livekit_outbound_trunk_id=livekit_trunk_id,
                resources_reused=reused_tags,
                setup_metadata=build_setup_metadata(inputs, domain_name=domain_name),
            )

        print()
        print("Setup summary (store these yourself; no .env write):")
        print(f"  Setup metadata: {build_setup_metadata(inputs, domain_name=domain_name)}")
        print(f"  Twilio trunk SID: {trunk['sid']}")
        print(f"  Twilio termination domain: {domain_name}")
        print(f"  LiveKit outbound trunk ID: {livekit_trunk_id}")
        print(f"  Outbound caller ID (LiveKit): {inputs.phone_number}")
        for note in plan.sip_auth_notes:
            print(f"  Note: {note}")
        print()
        print(f"  SIP_OUTBOUND_TRUNK_ID={livekit_trunk_id}")
        print(f"  Progress file (IDs and metadata only): {state_path}")
        return 0


def match_livekit_trunk(
    items: list[Any],
    *,
    domain_name: str,
    phone_number: str,
    username: str,
    sip_password: str | None = None,
) -> list[Any]:
    matches = []
    for item in items:
        result = livekit_trunk_matches(
            item,
            domain_name=domain_name,
            phone_number=phone_number,
            username=username,
            sip_password=sip_password,
        )
        if result.config_ok:
            matches.append(item)
    return matches


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preview or apply Clauver outbound Twilio + LiveKit SIP setup.",
    )
    sub = parser.add_subparsers(dest="command")

    preview = sub.add_parser(
        "preview",
        help="Dry-run checklist (default); no credentials or network required",
    )
    preview.add_argument(
        "--phone-number",
        help="Optional E.164 number to validate for the plan",
    )

    apply_cmd = sub.add_parser(
        "apply",
        help="Mutate cloud resources (requires exported env vars and confirmation)",
    )
    apply_cmd.add_argument("--phone-number", required=True)
    apply_cmd.add_argument("--sip-username", required=True)

    parser.set_defaults(command="preview")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "preview"

    if command == "preview":
        phone = None
        if getattr(args, "phone_number", None):
            phone = validate_e164(args.phone_number)
        print_preview(phone)
        return 0

    if command == "apply":
        return asyncio.run(run_apply(args))

    parser.error(f"unknown command {command}")
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ProvisionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except Exception as exc:
        print(f"Error: {_sanitize_exception(exc)}", file=sys.stderr)
        raise SystemExit(1) from exc
