"""Strict pure boundaries for the Common Room fixture protocol."""
from dataclasses import dataclass
from pathlib import Path
import hashlib, hmac, json, math, re, stat

MAX_BODY = 16384

class RoomError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)

@dataclass(frozen=True)
class Actor:
    id: str
    role: str

    def __post_init__(self):
        if (type(self.id) is not str or not re.fullmatch("[a-z][a-z0-9_-]{0,63}", self.id)
            or type(self.role) is not str or self.role not in {"owner", "worker", "reviewer", "observer"}):
            raise RoomError("identity", "Participant identity and role must be bounded supported strings.")

def authenticate(token, tokens):
    if isinstance(token, str) and 0 < len(token) <= 512:
        for known, actor in tokens.items():
            if hmac.compare_digest(token.encode("utf-8"), known.encode("utf-8")):
                return actor
    raise RoomError("unauthenticated", "A valid participant session is required.")

def canonical(value):
    try:
        result = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        result.encode("utf-8")
        return result
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise RoomError("bad_json", "Content must be finite, valid JSON.") from exc

def decode_request(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_BODY:
        raise RoomError("too_large", "Request exceeds the allowed size.")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise RoomError("bad_json", "Duplicate JSON keys are refused.")
            result[key] = value
        return result
    def constant(value):
        raise RoomError("bad_json", "Non-finite JSON values are refused.")
    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise RoomError("bad_json", "Request must be unambiguous UTF-8 JSON.") from exc
    if not isinstance(result, dict):
        raise RoomError("bad_json", "Request must be a JSON object.")
    # Decoder exponent overflow and escaped lone surrogates must be caught too.
    canonical(result)
    return result

def fingerprint(action, payload):
    return hashlib.sha256(canonical({"action": action, "payload": payload}).encode("utf-8")).hexdigest()

def _text(value, name, limit, nonempty=True):
    if not isinstance(value, str) or len(value) > limit or (nonempty and not value.strip()):
        raise RoomError("invalid", name + " is missing or outside its limit.")
    if any(ord(c) < 32 and c not in "\n\r\t" for c in value):
        raise RoomError("invalid", name + " contains control characters.")
    return value

def safe_artifact(root, relative, expected_hash=None, require_file=True):
    """Resolve a bounded, regular artifact under a trusted, operator-owned root."""
    if not isinstance(relative, str) or not 0 < len(relative) <= 240:
        raise RoomError("artifact", "Invalid artifact path.")
    if any(c in relative for c in "\\:%") or relative.startswith("/"):
        raise RoomError("artifact", "Artifacts must use plain relative paths.")
    parts = relative.split("/")
    reserved = {"CON", "PRN", "AUX", "NUL"} | {p+str(i) for p in ("COM", "LPT") for i in range(1, 10)}
    for part in parts:
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,79}", part)
            or part.endswith((".", " ")) or part.split(".")[0].upper() in reserved):
            raise RoomError("artifact", "Unsafe artifact path component.")
    try:
        root = Path(root)
        if root.is_symlink() or root.is_junction():
            raise RoomError("artifact", "Linked roots are refused.")
        base = root.resolve(strict=True)
        if not base.is_dir():
            raise RoomError("artifact", "Artifact root is not a directory.")
        candidate = base
        for part in parts:
            candidate = candidate / part
            if candidate.is_symlink() or candidate.is_junction():
                raise RoomError("artifact", "Linked artifacts are refused.")
        resolved = candidate.resolve(strict=require_file)
        resolved.relative_to(base)
        if require_file or expected_hash is not None:
            info = resolved.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1048576:
                raise RoomError("artifact", "Artifact must be a regular file of at most 1 MiB.")
        if expected_hash is not None:
            if not isinstance(expected_hash, str) or not re.fullmatch("[0-9a-f]{64}", expected_hash):
                raise RoomError("artifact", "A SHA-256 digest is required.")
            with resolved.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if not hmac.compare_digest(actual, expected_hash):
                raise RoomError("artifact", "Artifact digest does not match.")
        return resolved
    except (OSError, ValueError, RuntimeError) as exc:
        if isinstance(exc, RoomError):
            raise
        raise RoomError("artifact", "Artifact is missing or outside the allowed root.") from exc

def validate_message(actor, message, authority, participants):
    if not isinstance(actor, Actor) or participants.get(actor.id) != actor or actor.id not in authority["members"]:
        raise RoomError("forbidden", "Sender is not a member of this authority.")
    if authority.get("revoked") or authority.get("paused"):
        raise RoomError("held", "This authority is paused or revoked.")
    allowed = {"title", "body", "recipient", "authority_id", "kind", "acceptance", "refs", "chain_depth", "parent_id"}
    if not isinstance(message, dict) or set(message) - allowed:
        raise RoomError("invalid", "Unknown fields, including asserted identities, are refused.")
    required = {"title", "body", "recipient", "authority_id", "kind", "acceptance"}
    if required - set(message):
        raise RoomError("invalid", "Required message fields are missing.")
    if message["authority_id"] != authority["id"]:
        raise RoomError("forbidden", "Message authority does not match.")
    recipient = message["recipient"]
    if not isinstance(recipient, str) or recipient not in participants or recipient not in authority["members"]:
        raise RoomError("forbidden", "Recipient is not a member of this authority.")
    if message["kind"] not in authority["capabilities"]:
        raise RoomError("forbidden", "This capability has not been granted.")
    depth = message.get("chain_depth", 0)
    if type(depth) is not int or not 0 <= depth <= 4:
        raise RoomError("budget", "Task chains are limited to four links.")
    parent = message.get("parent_id")
    if parent is not None:
        _text(parent, "Parent task", 80)
    refs = message.get("refs", [])
    if not isinstance(refs, list) or len(refs) > 8:
        raise RoomError("artifact", "At most eight artifact references are allowed.")
    checked = []
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) != {"path", "sha256"}:
            raise RoomError("artifact", "Artifacts need a path and SHA-256 only.")
        safe_artifact(authority["artifact_root"], ref["path"], ref["sha256"])
        checked.append(dict(ref))
    result = {
        "title": _text(message["title"], "Title", 120),
        "body": _text(message["body"], "Body", 4000),
        "acceptance": _text(message["acceptance"], "Acceptance", 2000),
        "recipient": recipient, "authority_id": authority["id"], "kind": message["kind"],
        "sender": actor.id, "owner": authority["owner"], "refs": checked,
        "chain_depth": depth, "parent_id": parent,
    }
    if len(canonical(result).encode("utf-8")) > MAX_BODY:
        raise RoomError("too_large", "Message exceeds the request size budget.")
    return result

@dataclass(frozen=True)
class Event:
    seq: int
    kind: str
    actor: str
    task_id: str | None
    payload_json: str
    timestamp: float

    @property
    def payload(self):
        return json.loads(self.payload_json)

    def as_dict(self):
        return {"seq": self.seq, "kind": self.kind, "actor": self.actor,
                "task_id": self.task_id, "payload": self.payload, "timestamp": self.timestamp}

def make_event(seq, kind, actor, task_id, payload, timestamp):
    if (type(seq) is not int or seq < 1 or type(kind) is not str
        or not re.fullmatch("[a-z_]{1,32}", kind)):
        raise RoomError("event", "Invalid event sequence or kind.")
    if not isinstance(actor, Actor) or not isinstance(payload, dict):
        raise RoomError("event", "An authenticated actor and object payload are required.")
    if task_id is not None and (type(task_id) is not str or not re.fullmatch("[A-Za-z0-9][A-Za-z0-9_-]{0,79}", task_id)):
        raise RoomError("event", "Task identity must be a bounded string or null.")
    try:
        valid_time = type(timestamp) in (int, float) and math.isfinite(timestamp) and timestamp >= 0
    except (TypeError, OverflowError):
        valid_time = False
    if not valid_time:
        raise RoomError("event", "Invalid event time.")
    encoded = canonical(payload)
    if len(encoded.encode("utf-8")) > MAX_BODY:
        raise RoomError("too_large", "Event exceeds the size budget.")
    return Event(seq, kind, actor.id, task_id, encoded, timestamp)

def verify_order(events, after=0):
    for event in events:
        if event.seq != after + 1:
            raise RoomError("event", "Event order is missing, duplicated or reversed.")
        after = event.seq
    return after
