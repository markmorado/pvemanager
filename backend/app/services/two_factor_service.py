"""Two-Factor Authentication (TOTP) service.

Self-service, panel-side 2FA independent of Proxmox. Uses time-based one-time
passwords (RFC 6238) compatible with Google Authenticator, Authy, 1Password,
etc. Backup (recovery) codes are single-use and stored only as SHA-256 hashes.
"""

import hashlib
import hmac
import io
import secrets
import threading
import time
from typing import Dict, List, Optional

import pyotp
import qrcode
import qrcode.image.svg

# Number of one-time recovery codes generated when 2FA is enabled.
BACKUP_CODES_COUNT = 10
# Accept codes from the adjacent time step to tolerate clock drift (~±30s).
TOTP_VALID_WINDOW = 1


def generate_secret() -> str:
    """Generate a new base32 TOTP secret."""
    return pyotp.random_base32()


def build_otpauth_uri(secret: str, username: str, issuer: str = "PVEmanager") -> str:
    """Build an otpauth:// provisioning URI for authenticator apps."""
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


# Last accepted time step per user. RFC 6238 §5.2: a code must not be accepted
# twice — otherwise a shoulder-surfed or phished code stays usable for the
# whole ±1-step window (~90 s). Kept in memory: the panel runs one worker.
_last_used_step: Dict[object, int] = {}
_last_used_lock = threading.Lock()


def verify_totp(secret: str, code: str, *, replay_key: Optional[object] = None) -> bool:
    """Verify a 6-digit TOTP code against the secret.

    With ``replay_key`` (e.g. the user id) a code is accepted only once, and
    never for a time step older than the last accepted one.
    """
    if not secret or not code:
        return False
    code = code.strip().replace(" ", "")
    if not code.isdigit():
        return False
    try:
        totp = pyotp.TOTP(secret)
        now = time.time()
        for offset in range(-TOTP_VALID_WINDOW, TOTP_VALID_WINDOW + 1):
            at = now + offset * totp.interval
            if not hmac.compare_digest(totp.at(at), code):
                continue
            if replay_key is None:
                return True
            step = int(at // totp.interval)
            with _last_used_lock:
                if step <= _last_used_step.get(replay_key, -1):
                    return False
                _last_used_step[replay_key] = step
            return True
        return False
    except Exception:
        return False


def qr_svg(uri: str) -> str:
    """Render the provisioning URI as an inline SVG string (no Pillow needed)."""
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode("utf-8")


def generate_backup_codes(count: int = BACKUP_CODES_COUNT) -> List[str]:
    """Generate human-friendly single-use recovery codes (format: xxxx-xxxx)."""
    codes = []
    for _ in range(count):
        raw = secrets.token_hex(4)  # 8 hex chars
        codes.append(f"{raw[:4]}-{raw[4:]}")
    return codes


def hash_backup_code(code: str) -> str:
    """SHA-256 hash of a normalized backup code (case/format-insensitive)."""
    normalized = code.strip().lower().replace("-", "").replace(" ", "")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def hash_backup_codes(codes: List[str]) -> List[str]:
    return [hash_backup_code(c) for c in codes]


def consume_backup_code(code: str, stored_hashes: List[str]) -> bool:
    """Check a backup code against stored hashes; remove it in-place if matched.

    Returns True and mutates ``stored_hashes`` (removing the used hash) on a
    match, otherwise returns False and leaves the list unchanged.
    """
    if not code or not stored_hashes:
        return False
    h = hash_backup_code(code)
    if h in stored_hashes:
        stored_hashes.remove(h)
        return True
    return False
