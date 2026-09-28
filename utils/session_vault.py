"""A ~180 KB copy of the Chrome files that actually hold the Google session.

A full Chrome user-data directory is ~190 MB, but only eight files inside it
are credentials. Everything else -- the optimisation-guide model store, the CRX
cache, the WASM TTS engine, the HTTP and GPU caches -- is downloaded or
regenerable data that has nothing to do with being signed in. On this machine
those eight files are 180,193 bytes: a 1108x reduction, with no behaviour change.

Why a file copy rather than reading cookies and re-injecting them
------------------------------------------------------------------
Chrome stores every cookie value DPAPI-encrypted and never writes it in the
clear. Unlocking it needs CryptProtectData, which on this host is not exported
by advapi32 (GetProcAddress returns NULL), and which would be tied to the
Windows user account anyway. So the vault never decrypts anything: it copies
Chrome's own encrypted SQLite blob into a throwaway profile and points Chrome
at it. Chrome reads and unlocks it exactly as it reads any other profile, and
the bot never holds a plaintext credential.

That also makes the vault a revocable secret. Signing out of Google, or
deleting bot_session/, invalidates it. A plaintext password in .env does not.

The flow
--------
    bot_session/                      8 files, 176 KB, kept forever
        |
        | seed() - copy 8 files into a temp profile
        v
    %TEMP%/meetbot_profile_xxxx/      full working profile, disposable
        |
        | Chrome launches, reads the cookies, reports "already signed in"
        v
    Google Meet -- bot joins, no password touched
        |
        | save() - capture the fresh session back into the vault
        v
    rmtree()                          temp profile deleted, vault survives

save() is what makes the password a once-a-year cost rather than a per-run one:
whatever session Google just granted is written back moments before the temp
profile is destroyed.
"""
import json
import os
import shutil
import sqlite3

import config

# The vault always stores profiles under this directory name, whatever the live
# profile happens to be called. Normalising it means status() has one layout to
# read, and seed() has one layout to map from -- so a bot running with
# --profile-directory="Profile 1" round-trips through the same vault.
VAULT_PROFILE_DIRECTORY = "Default"

# The files that constitute a signed-in Chrome profile. Paths are relative to a
# user-data directory, and always start with the profile directory ("Default").
#
#   Local State                        the DPAPI-wrapped AES key
#   Default/Preferences                account_info -> the signed-in email
#   Default/Secure Preferences         extension/content trust material
#   Default/Network/Cookies             the session itself
#   Default/Network/Network Persistent State
#   Default/Network/Device Bound Sessions   Google's DBSC session binding
#   Default/Network/Trust Tokens
#
# Cookies-journal is deliberately NOT here. It is a SQLite rollback journal for
# a *live, possibly mid-transaction* database, so it only ever describes how to
# undo a write that has not happened. Copying one next to a clean database
# invites SQLite to roll the snapshot back; save() takes a VACUUM INTO snapshot
# instead, which is self-contained. seed() also strips any stale journal it
# finds, so a vault written by an older build cannot poison a profile.
VAULT_FILES = (
    "Local State",
    os.path.join("Default", "Preferences"),
    os.path.join("Default", "Secure Preferences"),
    os.path.join("Default", "Network", "Cookies"),
    os.path.join("Default", "Network", "Network Persistent State"),
    os.path.join("Default", "Network", "Device Bound Sessions"),
    os.path.join("Default", "Network", "Trust Tokens"),
)

# Names that must never be copied into or out of a seeded profile.
_TRANSIENT_SUFFIXES = ("-journal", "-wal", "-shm")

# The two files without which the vault cannot authenticate anything.
REQUIRED_FILES = ("Local State", os.path.join("Default", "Network", "Cookies"))

# WebKit epoch (1601-01-01) to Unix epoch, in seconds. Chrome stores
# expires_utc as microseconds since 1601.
_WEBKIT_EPOCH_OFFSET = 11644473600

# Cookies that actually carry the Google login. Their expiry is what
# "how long is the vault good for" means.
AUTH_COOKIE_NAMES = ("SID", "__Secure-1PSID", "__Secure-3PSID", "ACCOUNT_CHOOSER")


def vault_dir():
    """Absolute path of the vault directory, or None when it is disabled."""
    configured = (config.SESSION_VAULT_DIR or "").strip()
    if not configured:
        return None
    configured = os.path.expandvars(os.path.expanduser(configured))
    if not os.path.isabs(configured):
        configured = os.path.join(config.PROJECT_ROOT, configured)
    return os.path.abspath(configured)


def is_populated(directory=None):
    """True when the vault holds a usable session snapshot."""
    directory = directory or vault_dir()
    if not directory:
        return False
    return all(
        os.path.isfile(os.path.join(directory, relative))
        for relative in REQUIRED_FILES
    )


def _remap(relative, source_directory, target_directory):
    """Rewrite a profile-relative path to sit under a different profile directory.

    "Default/Network/Cookies" -> "Profile 1/Network/Cookies", or the reverse.

    The leading component is *replaced*, never dropped. Dropping it is the bug
    this function exists to prevent: Chrome would look in <profile>/Network/
    Cookies, find nothing, and silently open the meeting as a guest.
    """
    parts = relative.split(os.sep)
    if len(parts) > 1 and parts[0] in (source_directory, VAULT_PROFILE_DIRECTORY):
        return os.path.join(target_directory, *parts[1:])
    return relative


def seed(vault_path, profile_dir, profile_directory=None):
    """Copy the vault into a throwaway profile. Returns the number of files copied.

    profile_directory is the subdirectory of profile_dir to populate; it
    defaults to config.CHROME_PROFILE_DIRECTORY so the seeded layout matches
    the --profile-directory flag the browser is launched with.
    """
    if not is_populated(vault_path):
        return 0

    profile_directory = profile_directory or config.CHROME_PROFILE_DIRECTORY

    copied = 0
    for relative in VAULT_FILES:
        source = os.path.join(vault_path, relative)
        if not os.path.isfile(source):
            continue
        mapped = _remap(relative, VAULT_PROFILE_DIRECTORY, profile_directory)
        destination = os.path.join(profile_dir, mapped)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        shutil.copy2(source, destination)
        # A leftover journal/wal beside a freshly copied database describes
        # writes that were rolled back; SQLite would replay it and could
        # resurrect a stale session. Remove it rather than inherit it.
        for suffix in _TRANSIENT_SUFFIXES:
            _remove_quietly(destination + suffix)
        copied += 1
    return copied


def save(profile_dir, vault_path=None, profile_directory=None):
    """Capture the live profile's session into the vault.

    Called just before the temp profile is deleted. Uses VACUUM INTO to take a
    consistent SQLite snapshot, so this is safe even if Chrome still has the
    database open -- a plain file copy of a live SQLite file can tear.
    """
    vault_path = vault_path or vault_dir()
    if not vault_path or not profile_dir:
        return 0

    profile_directory = profile_directory or config.CHROME_PROFILE_DIRECTORY
    os.makedirs(vault_path, exist_ok=True)

    copied = 0
    for relative in VAULT_FILES:
        # Map the live profile's directory onto the vault's canonical "Default",
        # so one vault is readable by status() regardless of --profile-directory.
        source = os.path.join(profile_dir, _remap(relative, profile_directory, VAULT_PROFILE_DIRECTORY))
        if not os.path.isfile(source):
            continue
        destination = os.path.join(vault_path, relative)
        os.makedirs(os.path.dirname(destination), exist_ok=True)

        if relative.endswith("Cookies"):
            # Consistent snapshot rather than a torn copy of a live database.
            if not _snapshot_cookies(source, destination):
                continue
        else:
            try:
                shutil.copy2(source, destination)
            except OSError as error:
                print(f"Could not vault {relative}: {error}")
                continue
        # Never leave a transient sidecar next to a database we just wrote.
        for suffix in _TRANSIENT_SUFFIXES:
            _remove_quietly(destination + suffix)
        copied += 1

    if copied:
        _write_meta(vault_path, profile_directory)
    return copied


def _snapshot_cookies(source, destination):
    """Write a consistent copy of Chrome's cookie database to destination.

    VACUUM INTO refuses to overwrite, so it cannot target the live path: the
    second save() of the session would fail with "output file already exists" and
    the vault would silently keep an ever-staler snapshot. Vacuuming into a
    sibling temp file and then os.replace() makes the operation idempotent *and*
    atomic -- a crash mid-save leaves the previous good vault intact.
    """
    temporary = f"{destination}.vacuum-{os.getpid()}.tmp"
    _remove_quietly(temporary)
    try:
        with sqlite3.connect(_sqlite_uri(source), uri=True) as reader:
            reader.execute("VACUUM INTO ?", (temporary,))
        os.replace(temporary, destination)
    except (sqlite3.Error, OSError) as error:
        _remove_quietly(temporary)
        print(
            f"Could not snapshot the cookie database ({error}); "
            "falling back to a plain file copy."
        )
        return _plain_copy(source, destination)
    return True


def _plain_copy(source, destination):
    """Last resort when SQLite refuses. Still better than losing the session."""
    _remove_quietly(destination)
    try:
        shutil.copy2(source, destination)
    except OSError as error:
        print(f"Could not copy the cookie database: {error}")
        return False
    return True


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _read_meta(directory):
    """The vault manifest, or {} when it is missing or unreadable."""
    try:
        with open(os.path.join(directory, "meta.json"), "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def _sqlite_uri(path):
    """A read-only file: URI that survives spaces, '?' and '#' in the path.

    The profile lives under %TEMP%, so a space in the username is normal. A URI
    with an unescaped space makes sqlite3 raise, which would have looked like a
    "no session" failure rather than a path problem.
    """
    from urllib.parse import quote

    absolute = os.path.abspath(path).replace("\\", "/")
    if not absolute.startswith("/"):
        absolute = "/" + absolute
    return "file:" + quote(absolute, safe="/:") + "?mode=ro"


def status(directory=None):
    """Everything the UI or a CLI needs to say about the vault.

    Never returns a cookie value -- only names, hosts, counts and expiries.
    """
    directory = directory or vault_dir()
    if not directory:
        return {"enabled": False, "populated": False}

    result = {
        "enabled": True,
        "populated": is_populated(directory),
        "email": _read_email(directory),
        "cookie_count": 0,
        "auth_cookies": {},
        "earliest_auth_expiry": None,
    }
    if not result["populated"]:
        return result

    cookies = _read_cookies(directory)
    result["cookie_count"] = len(cookies)
    result["auth_cookies"] = {
        name: {"host": host, "expires": expiry}
        for name, host, expiry in cookies
        if name in AUTH_COOKIE_NAMES
    }
    expiries = [info["expires"] for info in result["auth_cookies"].values() if info["expires"]]
    if expiries:
        result["earliest_auth_expiry"] = min(expiries)
    return result


def _read_email(directory):
    """The signed-in email, straight out of the vaulted Preferences."""
    preferences = os.path.join(directory, "Default", "Preferences")
    try:
        with open(preferences, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return None
    for account in document.get("account_info") or []:
        if isinstance(account, dict) and account.get("email"):
            return account["email"]
    return None


def _read_cookies(directory):
    """(name, host, 'YYYY-MM-DD' expiry) for every vaulted cookie.

    Reads only metadata columns. encrypted_value is never selected, so a cookie
    value cannot leak out of the vault through a status call or a log line.
    """
    database = os.path.join(directory, "Default", "Network", "Cookies")
    if not os.path.isfile(database):
        return []
    try:
        connection = sqlite3.connect(_sqlite_uri(database), uri=True)
    except sqlite3.Error:
        return []
    try:
        rows = connection.execute(
            "SELECT name, host_key, expires_utc FROM cookies"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()

    cookies = []
    for name, host, expires_utc in rows:
        if not expires_utc:
            cookies.append((name, host, None))
            continue
        unix = expires_utc / 1_000_000 - _WEBKIT_EPOCH_OFFSET
        cookies.append((name, host, _format_day(unix)))
    return cookies


def _format_day(unix_seconds):
    from datetime import datetime, timezone

    try:
        return datetime.fromtimestamp(unix_seconds, timezone.utc).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return None


def _write_meta(vault_path, profile_directory):
    """Record what is in the vault, in a form a human can read."""
    document = {
        "profile_directory": profile_directory,
        "schema": 1,
    }
    document.update(
        {key: value for key, value in status(vault_path).items() if key != "enabled"}
    )
    try:
        with open(os.path.join(vault_path, "meta.json"), "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
    except OSError as error:
        print(f"Could not write the vault manifest: {error}")
