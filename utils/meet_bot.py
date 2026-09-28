"""Google Meet bot: a strict, observable state machine instead of "click and hope".

States
------
    LAUNCHING  -> browser is up, audio capture script is installed
    LOADING    -> Meet URL is open, waiting for Meet to decide what to show
    PREJOIN    -> the "Before you join" screen (guest name, device toggles, Join)
    JOINING    -> the bot is clicking "Ask to join" / "Join now"
    WAITING_FOR_ADMISSION -> the click landed and Meet is deciding / the host must admit
    IN_MEETING -> the bot is in the call; device toggles are re-asserted
    RECORDING  -> capture script is running and audio is being written/streamed
    FAILED / LEFT

The rule that matters: clicking "Join" is NOT being in the meeting. Every state
below the click is verified, and every refusal from Meet is reported with the
reason Meet actually displayed plus a screenshot, because "You can't join this
video call" is a server-side verdict about the bot's identity, not a Selenium
problem. Nothing in this file can make Meet accept an identity it rejects.
"""
import json
import os
import re
import shutil
import tempfile
import threading
import time
import wave

import psutil
from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchWindowException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import config
from utils import chrome_login, session_vault
from utils.audio_recorder import StreamingAudioRecorder, load_capture_script

# --------------------------------------------------------------------- states
STATE_LAUNCHING = "launching"
STATE_LOADING = "loading"
STATE_PREJOIN = "prejoin"
STATE_JOINING = "joining"
STATE_WAITING_FOR_ADMISSION = "waiting_for_admission"
STATE_IN_MEETING = "in_meeting"
STATE_RECORDING = "recording"
STATE_FAILED = "failed"
STATE_LEFT = "left"

# ----------------------------------------------------------------- page states
PAGE_LOADING = "loading"
PAGE_PREJOIN = "prejoin"
PAGE_LOBBY = "lobby"
PAGE_IN_MEETING = "in_meeting"
PAGE_BLOCKED = "blocked"
PAGE_ACCOUNT_CHOOSER = "account_chooser"
PAGE_UNKNOWN = "unknown"

BLOCKING_SCREEN_PHRASES = [
    "you can't join this video call",
    "check your meeting code",
    "this meeting has ended",
    "your account doesn't allow you to join",
    "you don't have permission to join",
    "you were removed from the call",
    "you've been removed from the call",
    "you have been removed from the call",
    "you've been kicked out of the call",
    "this meeting is full",
    "no more people can join this meeting",
    "the video call has ended",
]

# Shown when Google sends the bot off to a sign-in/account page. Note what is
# NOT here: "switch account" and "use another account" are the Meet pre-join
# account chip, which is on every signed-in pre-join screen, so matching them
# made the bot refuse a perfectly good "Ask to join" page.
ACCOUNT_CHOOSER_PHRASES = [
    "choose an account",
    "sign in to continue",
    "to continue to google meet",
    "continue to google meet",
    "your organization needs you to sign in",
    "you'll need to sign in",
]

# A chooser is only ever shown on Google's own sign-in hosts; Meet never renders
# one inline, so the URL is the reliable signal.
GOOGLE_ACCOUNT_HOSTS = (
    "accounts.google.com",
    "accounts.youtube.com",
    "myaccount.google.com",
    "oauth2.googleapis.com",
    "accounts.google.co.in",
)

# Meet put us in the waiting room: we clicked, the host still has to admit us.
LOBBY_PHRASES = [
    "let you in",
    "you'll join the call when",
    "waiting for the host",
    "wait for someone to let",
    "someone will let you in",
    "you are in the waiting room",
    "asking to join",
    "we'll let you know when",
]

# Ordered most specific first. Meet reuses one headline ("You can't join this
# video call") for every rejection and only the body text says why, so the body
# is what decides the guidance the user gets.
BLOCK_GUIDANCE = [
    (
        (
            "someone in the call may have restricted",
            "restricted who can join",
            "only people in your",
            "in your organization",
            "internal people only",
            "external participants",
            "outside your organization",
        ),
        "This call is restricted: the host or their Workspace only admits people they "
        "recognise, and the bot is not on that list.",
    ),
    (
        (
            "organizer to invite you",
            "ask your meeting organizer",
            "sign in to a different account",
            "sign in to google meet",
            "you must be signed in",
            "sign in to join",
        ),
        "This meeting admits signed-in accounts only; anonymous/guest entry is refused.",
    ),
    (
        (
            "this meeting has ended",
            "video call has ended",
            "check your meeting code",
            "invalid meeting",
            "does not exist",
        ),
        "The meeting code is wrong or the meeting has already finished.",
    ),
    (
        (
            "doesn't allow you to join",
            "not allowed to join",
            "you don't have permission to join",
            "you do not have permission to join",
        ),
        "The Google account the bot is signed in with is not permitted in this meeting "
        "(different Workspace, no Meet licence, or the account is suspended).",
    ),
    (
        (
            "meeting is full",
            "no more people",
            "at capacity",
            "too many participants",
        ),
        "The meeting has hit its participant limit.",
    ),
    (
        (
            "update your browser",
            "browser is not supported",
            "unsupported browser",
            "out of date",
            "no longer supported",
        ),
        "Meet rejected this Chrome build. Chrome and chromedriver must be a matching pair.",
    ),
]

FALLBACK_BLOCK_GUIDANCE = (
    "Meet refused the join without naming a reason. This is a server-side verdict on the "
    "bot's identity: in practice the meeting's access rules (invite-only, domain-restricted, "
    "or external participants blocked) are rejecting it."
)

JOIN_BUTTON_LABELS = ("ask to join", "join now")

CONSENT_BUTTON_LABELS = (
    "reject all",
    "accept all",
    "i agree",
    "agree to all",
)

GUEST_NAME_FIELD_CSS = [
    'input[aria-label*="name" i]',
    'input[placeholder*="name" i]',
    'div[role="textbox"][aria-label*="name" i]',
    'input[id="Name"]',
    'div[contenteditable="true"][data-placeholder*="name" i]',
]

TEMP_PROFILE_PREFIX = "meetbot_profile_"

MEET_CODE_RE = re.compile(r"meet\.google\.com/([a-z]{3}-[a-z]{4}-[a-z]{3})", re.IGNORECASE)

# Tracks Chrome instances this process currently owns (populated in
# setup_browser(), cleared in leave_meeting()). close_stale_automation_chrome()
# must never kill anything in here: doing so is exactly what produced
# "no such window: target window already closed" -- one bot's startup cleanup
# killing a DIFFERENT, still-active bot's chromedriver/chrome mid-session.
_active_lock = threading.Lock()
_active_driver_pids = set()
_active_profile_dirs = set()


def _normalize(text):
    """Lowercase, straighten curly quotes, collapse whitespace.

    Meet renders "You can't join this video call" with a typographic apostrophe,
    so matching ASCII apostrophes alone silently misses the one screen we most
    need to recognise.
    """
    if not text:
        return ""
    for smart in ("\u2019", "\u2018", "\u02bc", "\u02bb", "`"):
        text = text.replace(smart, "'")
    for dash in ("\u2013", "\u2014", "\u2212"):
        text = text.replace(dash, "-")
    return " ".join(text.lower().split())


def is_browser_alive(driver):
    """True only while WebDriver still has a live window/web view to talk to.

    Every browser call in this module is gated on this. Without it, a tab that
    Meet or a user closed turns the next find_element into
    "no such window: target window already closed" and that exception cascades
    into unrelated follow-on errors.
    """
    if driver is None:
        return False
    try:
        handles = driver.window_handles
        if not handles:
            return False
        try:
            current = driver.current_window_handle
        except (NoSuchWindowException, WebDriverException):
            current = None
        if current not in handles:
            driver.switch_to.window(handles[0])
        _ = driver.current_url
        return True
    except (NoSuchWindowException, WebDriverException):
        return False
    except Exception:
        return False


def match_first(text, phrases):
    """Return the first phrase present in already-normalized text."""
    for phrase in phrases:
        if phrase in text:
            return phrase
    return None


def explain_block(page_text):
    """(matched headline phrase, what it means) for a Meet refusal page."""
    text = _normalize(page_text)
    headline = match_first(text, BLOCKING_SCREEN_PHRASES) or "unrecognised Meet refusal"
    for needles, guidance in BLOCK_GUIDANCE:
        hit = match_first(text, needles)
        if hit:
            return headline, guidance
    return headline, FALLBACK_BLOCK_GUIDANCE


# The only experimental option that is a genuine Chrome option, and so the only
# one safe to forward to chromedriver via goog:chromeOptions. Everything else we
# set (detach, excludeSwitches, useAutomationExtension) is interpreted by
# Selenium's own launcher and rejected by chromedriver when handed to uc.
_CHROME_EXPERIMENTAL_OPTIONS = frozenset({"prefs"})


def _strip_selenium_only_options(browser_options):
    """Drop Selenium-only experimental options before handing them to uc.

    An allowlist rather than a denylist: any option that is not a real Chrome
    option is removed, so adding a new Selenium-specific option later cannot
    silently reintroduce a "cannot parse capability" failure at launch time.
    Anything dropped is printed, because that error is otherwise opaque.
    """
    experimental = getattr(browser_options, "_experimental_options", None)
    if not experimental:
        return
    for key in [k for k in experimental if k not in _CHROME_EXPERIMENTAL_OPTIONS]:
        del experimental[key]
        print(f"Dropped the Selenium-only Chrome option {key!r} for the undetected driver")


def configured_profile_dir():
    """Absolute path of the bot's persistent Chrome profile, if one is configured."""
    if not config.CHROME_USER_DATA_DIR or config.EPHEMERAL_PROFILE:
        return None
    configured = os.path.expandvars(os.path.expanduser(config.CHROME_USER_DATA_DIR))
    if not os.path.isabs(configured):
        configured = os.path.join(config.PROJECT_ROOT, configured)
    return os.path.abspath(configured)


def profile_is_signed_in(profile_dir, profile_directory="Default"):
    """True when the Chrome profile has a Google account attached.

    Read straight off disk so it works before the browser is even launched, and
    so the answer survives a browser that never loads.
    """
    if not profile_dir:
        return False
    prefs_path = os.path.join(profile_dir, profile_directory, "Preferences")
    try:
        with open(prefs_path, "r", encoding="utf-8") as handle:
            preferences = json.load(handle)
    except (OSError, ValueError):
        return False
    for account in preferences.get("account_info") or []:
        if not isinstance(account, dict):
            continue
        if account.get("email") or account.get("gaia_id"):
            return True
    return False


def profile_google_account(profile_dir, profile_directory="Default"):
    """The email of the account attached to the profile, or None."""
    if not profile_dir:
        return None
    prefs_path = os.path.join(profile_dir, profile_directory, "Preferences")
    try:
        with open(prefs_path, "r", encoding="utf-8") as handle:
            preferences = json.load(handle)
    except (OSError, ValueError):
        return None
    for account in preferences.get("account_info") or []:
        if isinstance(account, dict) and account.get("email"):
            return account["email"]
    return None


def ensure_silence_wav():
    """A one-second silent WAV. Chrome loops it as the bot's virtual microphone."""
    path = os.path.join(tempfile.gettempdir(), "meetbot_silence.wav")
    if not os.path.exists(path):
        with wave.open(path, "wb") as silence:
            silence.setnchannels(1)
            silence.setsampwidth(2)
            silence.setframerate(48000)
            silence.writeframes(b"\x00\x00" * 48000)
    return path


def _driver_owner_alive(proc):
    """True when the process that launched this chromedriver is still running.

    A chromedriver whose Python parent died is a true orphan: it keeps a port
    open and later Selenium calls can hang against it. A chromedriver whose
    parent is alive belongs to some other running app and is none of our
    business -- killing those is what breaks unrelated bots.
    """
    try:
        parent = proc.parent()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    if parent is None:
        return False
    return psutil.pid_exists(parent.pid)


def close_stale_automation_chrome():
    """Clear only our own leftovers: orphaned chromedrivers and Chrome holding a bot profile."""
    with _active_lock:
        active_pids = set(_active_driver_pids)
        active_dirs = set(_active_profile_dirs)

    bot_dirs = {configured_profile_dir()}
    bot_dirs.discard(None)

    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info["name"] or "").lower()
            pid = proc.info["pid"]
            # Orphaned chromedriver.exe processes never get killed by the
            # chrome.exe match below (chromedriver survives its browser dying),
            # and they keep a local port open that a later session's Selenium
            # commands can silently stall against. Clear these out -- but never
            # one this process owns (see _active_driver_pids) and never one
            # another live process owns.
            if "chromedriver" in name:
                if pid in active_pids:
                    continue
                if _driver_owner_alive(proc):
                    continue
                proc.kill()
                continue
            if "chrome" not in name:
                continue
            cmdline = proc.info["cmdline"] or []
            user_data_dir = next(
                (arg.split("=", 1)[1] for arg in cmdline if arg.startswith("--user-data-dir=")),
                None,
            )
            if not user_data_dir:
                continue
            # A leftover Chrome still holding a bot profile blocks the next run
            # from opening that profile at all, which is a silent "bot never
            # gets into the meeting". The throwaway profiles and the configured
            # dedicated profile are both ours to reclaim; the user's personal
            # Chrome (default user-data-dir) is never touched.
            if not bot_dirs or not (user_data_dir in bot_dirs or TEMP_PROFILE_PREFIX in user_data_dir):
                continue
            if user_data_dir in active_dirs:
                continue
            print(f"Closing leftover Chrome holding the bot profile {user_data_dir}")
            proc.kill()
            # Killing the browser is not enough: leave_meeting() is what normally
            # deletes a throwaway profile, and a hard-killed run never reaches it.
            # Without this the temp folders pile up in %TEMP%, each carrying a
            # full cache. The session has already been vaulted by then, so
            # nothing of value is lost -- this only reclaims disk.
            if TEMP_PROFILE_PREFIX in user_data_dir:
                _remove_stale_profile_dir(user_data_dir)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


def _remove_stale_profile_dir(user_data_dir):
    """Delete an orphaned throwaway profile directory. Best effort."""
    if not os.path.isdir(user_data_dir):
        return
    try:
        shutil.rmtree(user_data_dir, ignore_errors=True)
    except OSError as error:
        print(f"Could not remove the orphaned bot profile {user_data_dir}: {error}")
        return
    if not os.path.isdir(user_data_dir):
        print(f"Removed the orphaned bot profile {user_data_dir}")


class GoogleMeetBot:
    def __init__(self):
        self.browser = None
        self.audio_recorder = None
        self.meeting_is_active = False
        self.last_error = None
        self.profile_dir = None
        self.temporary_profile = False
        self.driver_pid = None
        self._cleanup_done = False
        self.state = STATE_LAUNCHING
        self.meeting_url = None
        self.session_id = None
        self.meeting_code = None
        self.page_account = None
        self._page_text = ""
        self._ever_on_meeting_page = False
        self._block_text = ""

    # ------------------------------------------------------------------ state
    def _set_state(self, state, note=None):
        if state != self.state:
            print(f"[{self.state} -> {state}]")
            self.state = state
        if note:
            print(note)

    def _fail(self, message):
        self.last_error = message
        self._set_state(STATE_FAILED)
        print(message)
        return False

    def _browser_alive(self):
        return is_browser_alive(self.browser)

    # ---------------------------------------------------------------- browser
    def setup_browser(self):
        close_stale_automation_chrome()
        time.sleep(1)

        self._cleanup_done = False
        self.last_error = None
        self._set_state(STATE_LAUNCHING)

        # Precedence: vault-backed throwaway profile, then the legacy
        # CHROME_USER_DATA_DIR / EPHEMERAL_PROFILE pair. The vault wins whenever
        # SESSION_VAULT_DIR is set, even when it is still empty: a fresh
        # throwaway profile is exactly what automatic sign-in needs, because the
        # session it earns is captured into the vault on the way out.
        if session_vault.vault_dir():
            self.profile_dir = tempfile.mkdtemp(prefix=TEMP_PROFILE_PREFIX)
            self.temporary_profile = True
            seeded = session_vault.seed(session_vault.vault_dir(), self.profile_dir)
            if seeded:
                print(f"Seeded a throwaway profile from the session vault ({seeded} files)")
            else:
                print(
                    "The session vault is empty; the bot will sign in itself and "
                    "populate it for next time."
                )
        elif config.EPHEMERAL_PROFILE:
            self.profile_dir = tempfile.mkdtemp(prefix=TEMP_PROFILE_PREFIX)
            self.temporary_profile = True
        elif config.CHROME_USER_DATA_DIR:
            self.profile_dir = configured_profile_dir()
            os.makedirs(self.profile_dir, exist_ok=True)
        else:
            self.profile_dir = None

        browser_options = Options()
        # NOTE: "detach" is deliberately NOT set here. It is a chromedriver
        # service flag, not a Chrome option, and webdriver.Chrome is the only
        # thing that translates it correctly. undetected_chromedriver instead
        # forwards experimental options verbatim into goog:chromeOptions, and
        # chromedriver then refuses to start with
        #     "unrecognized chrome option: detach".
        # _launch() therefore adds it per-driver, on the stock path only.
        browser_options.add_argument("--use-fake-ui-for-media-stream")
        # No echo: the bot's Chrome plays nothing to the speakers, and its microphone
        # is a silent virtual device, so the bot can never feed audio back.
        browser_options.add_argument("--mute-audio")
        browser_options.add_argument("--use-fake-device-for-media-stream")
        browser_options.add_argument(f"--use-file-for-fake-audio-capture={ensure_silence_wav()}")
        browser_options.add_argument("--autoplay-policy=no-user-gesture-required")
        browser_options.add_argument("--start-maximized")
        browser_options.add_argument("--no-first-run")
        browser_options.add_argument("--no-default-browser-check")
        browser_options.add_argument("--no-sandbox")
        browser_options.add_argument("--disable-dev-shm-usage")
        browser_options.add_argument("--disable-blink-features=AutomationControlled")
        browser_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        browser_options.add_experimental_option("useAutomationExtension", False)
        browser_options.add_argument("--lang=en-US")

        if self.profile_dir:
            browser_options.add_argument(f"--user-data-dir={self.profile_dir}")
            browser_options.add_argument(f"--profile-directory={config.CHROME_PROFILE_DIRECTORY}")

        media_permissions = {
            "intl.accept_languages": "en-US",
            "profile.default_content_setting_values": {
                "media_stream_mic": 1,
                "media_stream_camera": 1,
                "notifications": 2
            }
        }
        browser_options.add_experimental_option("prefs", media_permissions)

        try:
            print("Setting up Chrome browser...")
            self.browser = self._launch(browser_options)
            self._register_active()
            self._install_capture_script()
            print("Chrome setup successful")
            return True

        except Exception as error:
            # If Chrome started but setup failed, do not leave an orphaned driver.
            self._fail(f"Chrome setup failed: {error}")
            self.leave_meeting()
            return False

    def _launch(self, browser_options):
        """Start Chrome, preferring undetected_chromedriver when we may need to sign in.

        A plain webdriver.Chrome is enough to join a meeting with a valid
        session, but Google's sign-in form rejects a stock Selenium-controlled
        browser ("This browser or app may not be secure"). undetected_chromedriver
        patches that away, so it is used whenever automatic sign-in is
        configured -- which is also why CHROME_VERSION_MAIN matters: uc has to be
        told which ChromeDriver build to fetch and its own detection guesses
        wrong across Chrome auto-updates.

        Falls back to the stock driver if uc is unavailable, so a missing
        optional dependency degrades to the old behaviour instead of failing.
        """
        if not self._needs_undetected():
            # webdriver.Chrome is the one path that understands the "detach"
            # experimental option, so it is set here rather than in
            # setup_browser() where it would leak into the uc capabilities.
            browser_options.add_experimental_option("detach", True)
            return webdriver.Chrome(options=browser_options)

        try:
            import undetected_chromedriver as uc
        except ImportError:
            print(
                "undetected-chromedriver is not installed; falling back to the "
                "standard driver. Automatic sign-in will likely be refused by Google."
            )
            browser_options.add_experimental_option("detach", True)
            return webdriver.Chrome(options=browser_options)

        try:
            version_main = chrome_login.chrome_major_version()
        except RuntimeError as error:
            print(f"{error} Falling back to the standard driver.")
            browser_options.add_experimental_option("detach", True)
            return webdriver.Chrome(options=browser_options)

        print(f"Launching undetected Chrome (Chrome {version_main})...")
        # Selenium-only experimental options must be stripped before the options
        # reach uc. They are consumed by Selenium's *own* driver launcher, which
        # decides which command-line switches to emit; since uc launches Chrome
        # itself, they are never interpreted and chromedriver rejects them as
        # unknown Chrome options:
        #     "unrecognized chrome option: excludeSwitches"  (likewise detach,
        #     useAutomationExtension)
        # Nothing is lost by dropping them: the switches they suppress
        # (--enable-automation, --disable-extensions) are added by Selenium, not
        # by chromedriver, so with uc they are absent anyway. The real
        # anti-detection arguments below are ordinary Chrome flags and are
        # untouched.
        _strip_selenium_only_options(browser_options)

        # from_options() re-wraps the same arguments and experimental options in
        # uc's own ChromeOptions, which is what lets uc find the prefs blob and
        # the user-data-dir before it builds capabilities. Passing the plain
        # selenium Options straight through works for the arguments but skips
        # that handling. Note that it shares the underlying dicts with
        # browser_options, which is fine: this object is not reused afterwards.
        uc_options = uc.ChromeOptions.from_options(browser_options)
        # NB: uc's Chrome.__del__ calls quit() a second time after leave_meeting()
        # has already quit, which fails on the dead handle (OSError: [WinError 6]).
        # It is shadowed in leave_meeting(), *after* the real quit, not here --
        # shadowing it now would make our own teardown a no-op.
        return uc.Chrome(options=uc_options, version_main=version_main)

    def _needs_undetected(self):
        """True when this run may have to type the configured password in.

        Checked before the browser starts, and the profile has already been
        seeded by then, so the honest test is the profile itself rather than the
        vault's file listing: if the seeded profile already carries an account,
        no form is ever shown and the stock Selenium driver is fine. That skips
        undetected-chromedriver -- which patches the binary and spawns a
        patched-driver cache -- on every ordinary run after the first.
        """
        if not config.AUTO_LOGIN or not chrome_login.credentials_configured():
            return False
        if self.profile_dir and profile_is_signed_in(
            self.profile_dir, config.CHROME_PROFILE_DIRECTORY
        ):
            return False
        return True


    def _install_capture_script(self):
        """Inject window.__meetRec at document start so it exists in every Meet page."""
        if not self._browser_alive():
            return False
        try:
            self.browser.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
            )
            self.browser.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": load_capture_script()},
            )
            return True
        except Exception as error:
            print(f"Could not pre-install the audio capture script: {error}")
            return False

    def _ensure_capture_script(self):
        """Verify window.__meetRec is live in the current page; re-inject if not.

        Meet sometimes lands in a fresh tab, and a script injected only on the
        first document never runs there -- recording then silently captures
        nothing.
        """
        if not self._browser_alive():
            return False
        try:
            if self.browser.execute_script("return !!window.__meetRec;"):
                return True
        except Exception:
            return False
        try:
            self._install_capture_script()
            self.browser.execute_script(load_capture_script())
            return bool(self.browser.execute_script("return !!window.__meetRec;"))
        except Exception as error:
            print(f"Could not restore the audio capture script: {error}")
            return False

    # ------------------------------------------------------------------- join
    def join_meeting(self, meeting_url, session_id=None):
        """Walk LAUNCHING -> PREJOIN -> JOINING -> ADMISSION -> IN_MEETING.

        A refusal from Meet is retried (a fresh load sometimes clears it) and,
        when it persists, reported with Meet's own reason plus a screenshot.
        """
        self.meeting_url = meeting_url
        self.session_id = session_id or self.session_id
        self.meeting_code = self._extract_code(meeting_url)
        self.last_error = None
        self._block_text = ""

        if not self.setup_browser():
            return False

        if not self._preflight_profile():
            return False

        # Zero human intervention: if the session vault had nothing usable,
        # sign in with the configured credentials before touching the meeting
        # URL, so the rest of the join runs with a real Google identity.
        if self._should_sign_in():
            if not self.sign_in_to_google():
                return False

        for attempt in range(1, config.JOIN_ATTEMPTS + 1):
            self._block_text = ""
            if attempt > 1:
                self._set_state(STATE_LOADING, f"Retrying the join (attempt {attempt}/{config.JOIN_ATTEMPTS})")
            if not self._open_meeting():
                return False

            page, text = self.read_page_state()
            if page == PAGE_BLOCKED:
                self._block_text = text
                if attempt < config.JOIN_ATTEMPTS:
                    print("Meet refused this attempt; reloading the meeting to try again")
                    continue
                return self._fail(self._blocked_message(text))
            if page == PAGE_ACCOUNT_CHOOSER:
                # Google bounced us to a sign-in page. If automatic sign-in is
                # configured, try it once; the profile is then genuinely signed
                # in, so reload the meeting and continue the normal join. If it
                # is not configured, this is the original terminal failure.
                if self._should_sign_in():
                    if not self.sign_in_to_google():
                        return False
                    if attempt < config.JOIN_ATTEMPTS:
                        continue
                    return self._fail(self.last_error or "Google sign-in did not produce a session.")
                return self._fail(self._account_chooser_message(text))
            if page == PAGE_IN_MEETING:
                # Rejoined straight into the call (Meet restored the session).
                return self._finish_join()

            self._set_state(STATE_PREJOIN)
            self.dismiss_consent_banners()
            self.turn_off_microphone()
            self.turn_off_camera()
            if config.BOT_DISPLAY_NAME:
                self.enter_guest_name()

            if config.AUTO_CLICK_JOIN:
                self._set_state(STATE_JOINING)
                if not self.attempt_to_join():
                    if self._block_text and attempt < config.JOIN_ATTEMPTS:
                        print("Meet refused the join click; reloading the meeting to try again")
                        continue
                    if not self.last_error:
                        self._fail(
                            "Could not find a usable 'Join now' / 'Ask to join' button. "
                            "Meet may require sign-in, the host may have restricted guest "
                            "access, or the page never finished loading."
                        )
                    else:
                        self._set_state(STATE_FAILED)
                    return False
            else:
                print("Manual mode: admit the bot in its Chrome window...")

            if not self.wait_until_in_meeting(timeout=config.JOIN_WAIT_TIMEOUT):
                if self._block_text and attempt < config.JOIN_ATTEMPTS:
                    print("Meet ejected the bot after the join click; trying again")
                    continue
                self._set_state(STATE_FAILED)
                return False

            return self._finish_join()

        return self._fail(self.last_error or "Google Meet would not let the bot into the meeting.")

    def _finish_join(self):
        # Mute controls are safe to apply after entry too; no long sleeps.
        self.turn_off_microphone()
        self.turn_off_camera()
        self.meeting_is_active = True
        self.last_error = None
        self._set_state(STATE_IN_MEETING, "Meeting joined successfully")
        return True

    # --------------------------------------------------------------- sign-in
    def _should_sign_in(self):
        """True when this run is allowed to type the configured password in.

        Gated on AUTO_LOGIN, both credentials being present, and the profile not
        already carrying an account. The last condition is what makes the vault
        worthwhile: once it is populated the password is never used again.
        """
        if not config.AUTO_LOGIN:
            return False
        if not chrome_login.credentials_configured():
            return False
        if self.page_account:
            return False
        return True

    def sign_in_to_google(self):
        """Sign the bot's Chrome in, then persist the session into the vault.

        Returns True only when the profile is genuinely signed in afterwards --
        Chrome accepting the form is not enough, because Google can hand back a
        challenge that looks like a success page. The check is the same
        account_info read used everywhere else, so there is one definition of
        "signed in" in this codebase.
        """
        if not self._browser_alive():
            return self._fail("Chrome is not available; cannot sign in to Google.")

        self._set_state(STATE_LOADING, "Signing in to Google")
        try:
            chrome_login.sign_in(self.browser, browser_alive=self._browser_alive)
        except chrome_login.LoginChallenge as challenge:
            return self._fail(self._sign_in_failure_message(str(challenge)))
        except (NoSuchWindowException, WebDriverException) as error:
            return self._fail(f"Chrome closed or errored during the Google sign-in: {error}")
        except Exception as error:
            return self._fail(f"Google sign-in failed unexpectedly: {error}")

        # Do not trust the page: confirm the profile now carries an account.
        if not profile_is_signed_in(self.profile_dir, config.CHROME_PROFILE_DIRECTORY):
            return self._fail(
                "Google accepted the sign-in form but the browser profile still has no "
                "Google account attached. The sign-in did not take effect."
            )
        self.page_account = profile_google_account(
            self.profile_dir, config.CHROME_PROFILE_DIRECTORY
        )
        print(f"Signed in to Google as {self.page_account}")

        # Capture it now so later runs need no password at all. This is the step
        # that makes automatic sign-in a once-a-year cost rather than a per-run one.
        self._save_session_vault()
        return True

    def _save_session_vault(self):
        vault_path = session_vault.vault_dir()
        if not vault_path or not self.profile_dir or not self.page_account:
            return
        try:
            saved = session_vault.save(self.profile_dir, vault_path)
        except Exception as error:
            print(f"Could not save the session to the vault: {error}")
            return
        if saved:
            status = session_vault.status(vault_path)
            expiry = status.get("earliest_auth_expiry") or "unknown"
            print(
                f"Session saved to the vault ({status.get('cookie_count', 0)} cookies, "
                f"good until {expiry}). Later runs will not need the password."
            )

    def _sign_in_failure_message(self, detail):
        """A refusal from Google, in the operator's terms rather than a stack trace."""
        shot = self.capture_diagnostics("sign_in_failed")
        message = (
            "Automatic Google sign-in was refused, so the bot cannot join this meeting "
            f"as a signed-in account.\n  {detail}"
        )
        if shot:
            message += f"\n  Screenshot: {shot}"
        return message

    def _preflight_profile(self):
        """Warn (or refuse) when the bot is anonymous and the meeting likely needs an identity."""
        if not self.profile_dir:
            print(
                "No Chrome profile configured: the bot will join as an anonymous guest "
                "(any meeting that requires a signed-in account will refuse it)."
            )
            return True
        if profile_is_signed_in(self.profile_dir, config.CHROME_PROFILE_DIRECTORY):
            self.page_account = profile_google_account(self.profile_dir, config.CHROME_PROFILE_DIRECTORY)
            print(f"Bot Chrome profile is signed into Google as {self.page_account}")
            return True
        if self._should_sign_in():
            print(
                "No Google session in this profile yet; the bot will sign in itself "
                "with the configured credentials."
            )
            return True
        if config.REQUIRE_SIGNED_IN_PROFILE:
            return self._fail(
                self._identity_hint()
                + " REQUIRE_SIGNED_IN_PROFILE is on, so the join was not attempted."
            )
        print(
            "WARNING: " + self._identity_hint()
            + " Meetings that are invite-only or domain-restricted will refuse it."
        )
        return True

    def _open_meeting(self):
        """Navigate to the Meet URL and wait until Meet commits to a page state."""
        self._set_state(STATE_LOADING)
        if not self._browser_alive():
            return self._fail("Chrome is not available (no live window) before the meeting was opened.")
        try:
            print(f"Opening meeting: {self.meeting_url}")
            self.browser.get(self.meeting_url)
        except (NoSuchWindowException, WebDriverException) as error:
            return self._fail(f"Chrome window closed while opening the meeting: {error}")
        except Exception as error:
            return self._fail(f"Could not open {self.meeting_url}: {error}")

        deadline = time.time() + config.PREJOIN_LOAD_TIMEOUT
        while time.time() < deadline:
            if not self._browser_alive():
                return self._fail("Chrome closed or lost its window while the meeting was loading.")
            page, _ = self.read_page_state()
            if page != PAGE_LOADING and page != PAGE_UNKNOWN:
                return True
            time.sleep(0.5)

        shot = self.capture_diagnostics("load_timeout")
        message = (
            f"Google Meet did not show a usable page within {config.PREJOIN_LOAD_TIMEOUT}s. "
            "Check the meeting URL, network access and whether the Chrome window is still open."
        )
        if shot:
            message += f" Screenshot: {shot}"
        return self._fail(message)

    def _extract_code(self, url):
        match = MEET_CODE_RE.search(url or "")
        return match.group(1).lower() if match else None

    def _left_meeting_page(self):
        """True once Meet has kicked the bot off the meeting URL (home screen)."""
        if not self._ever_on_meeting_page or not self.meeting_code:
            return False
        if not self._browser_alive():
            return False
        try:
            current = self.browser.current_url or ""
        except Exception:
            return False
        return self.meeting_code not in current.lower()

    # ---------------------------------------------------------- page reading
    def read_page_state(self):
        """Classify what Meet is showing right now. Returns (state, normalized text)."""
        if not self._browser_alive():
            return PAGE_UNKNOWN, self._page_text
        try:
            raw = self.browser.find_element(By.TAG_NAME, "body").text or ""
        except Exception:
            raw = ""
        text = _normalize(raw)
        self._page_text = text

        if self.meeting_code and self.meeting_code in self._current_url_lower():
            self._ever_on_meeting_page = True

        if match_first(text, BLOCKING_SCREEN_PHRASES):
            return PAGE_BLOCKED, text
        if self._on_google_account_page() or match_first(text, ACCOUNT_CHOOSER_PHRASES):
            return PAGE_ACCOUNT_CHOOSER, text
        if self._is_in_meeting():
            return PAGE_IN_MEETING, text
        if match_first(text, LOBBY_PHRASES):
            return PAGE_LOBBY, text
        for label in JOIN_BUTTON_LABELS:
            if self._find_join_button(label) is not None:
                return PAGE_PREJOIN, text
        return PAGE_LOADING, text

    def _current_url_lower(self):
        try:
            return (self.browser.current_url or "").lower()
        except Exception:
            return ""

    def _on_google_account_page(self):
        """True when Google has bounced the bot to its own sign-in/account page."""
        url = self._current_url_lower()
        return any(host in url for host in GOOGLE_ACCOUNT_HOSTS)

    def _page_has_loaded_controls(self):
        """True once Meet has rendered controls or a recognizable blocking page."""
        return self.read_page_state()[0] in (
            PAGE_PREJOIN,
            PAGE_IN_MEETING,
            PAGE_BLOCKED,
            PAGE_ACCOUNT_CHOOSER,
            PAGE_LOBBY,
        )

    def detect_blocking_screen(self):
        state, text = self.read_page_state()
        if state != PAGE_BLOCKED:
            return None
        return match_first(text, BLOCKING_SCREEN_PHRASES)

    def detect_account_chooser(self):
        state, text = self.read_page_state()
        if state != PAGE_ACCOUNT_CHOOSER:
            return None
        return match_first(text, ACCOUNT_CHOOSER_PHRASES)

    def _is_in_meeting(self):
        if not self._browser_alive():
            return False
        try:
            for element in self.browser.find_elements(
                By.CSS_SELECTOR,
                "button[aria-label*='Leave call' i], [role='button'][aria-label*='Leave call' i], "
                "button[aria-label*='Leave meeting' i], [role='button'][aria-label*='Leave meeting' i]",
            ):
                if element.is_displayed():
                    return True
        except (NoSuchWindowException, WebDriverException):
            return False
        except Exception:
            return False
        return False

    # ------------------------------------------------------------ diagnostics
    def capture_diagnostics(self, tag):
        """Save what Meet actually displayed, so a refusal is diagnosable after the fact."""
        if not config.CAPTURE_JOIN_DIAGNOSTICS or not self._browser_alive():
            return None
        try:
            directory = os.path.join(config.DIAGNOSTICS_DIR, self.session_id or "adhoc")
            os.makedirs(directory, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            screenshot = os.path.join(directory, f"{stamp}_{tag}.png")
            self.browser.save_screenshot(screenshot)
            with open(os.path.join(directory, f"{stamp}_{tag}.txt"), "w", encoding="utf-8") as handle:
                handle.write(f"url: {self.browser.current_url}\n")
                handle.write(f"title: {self.browser.title}\n")
                handle.write(f"meeting_code: {self.meeting_code}\n")
                handle.write(f"profile_dir: {self.profile_dir}\n\n")
                handle.write(self._page_text or "(no page text captured)\n")
            print(f"Meet diagnostics saved: {screenshot}")
            return screenshot
        except Exception as error:
            print(f"Could not save Meet diagnostics: {error}")
            return None

    def _identity_hint(self):
        """What Meet thinks the bot is -- the thing it just refused."""
        if not self.profile_dir:
            return (
                "The bot runs an anonymous/guest Chrome profile with no Google account, so Meet "
                "sees it as an unidentified guest."
            )
        if self.temporary_profile:
            if session_vault.is_populated():
                return (
                    "The bot runs a throwaway Chrome profile seeded from its session "
                    "vault, so the account it presents is whatever the vault holds."
                )
            if self._should_sign_in():
                return (
                    "The bot runs a throwaway Chrome profile and signs in with the "
                    "credentials in .env, so Meet sees it as that Google account."
                )
            return (
                "The bot runs a throwaway Chrome profile with no Google account, so Meet sees it "
                "as an unidentified guest."
            )
        account = profile_google_account(self.profile_dir, config.CHROME_PROFILE_DIRECTORY)
        if account:
            return f"The bot Chrome profile is signed into Google as {account}."
        return (
            f"The bot Chrome profile '{self.profile_dir}' is NOT signed into a Google account. "
            f'Open Chrome with --user-data-dir="{self.profile_dir}" once, sign a dedicated bot '
            "account in, then start the bot."
        )

    def _blocked_message(self, page_text):
        headline, guidance = explain_block(page_text)
        shot = self.capture_diagnostics("blocked")
        message = f"Google Meet would not let the bot in ({headline}). {guidance} {self._identity_hint()}"
        if shot:
            message += f" Screenshot: {shot}"
        return message

    def _account_chooser_message(self, page_text):
        phrase = match_first(_normalize(page_text), ACCOUNT_CHOOSER_PHRASES)
        shot = self.capture_diagnostics("account_chooser")
        where = self._current_url_lower() or "an unknown page"
        if config.CHROME_USER_DATA_DIR and not config.EPHEMERAL_PROFILE:
            guidance = (
                "Google sent the bot to a sign-in page, so it never reached the meeting. "
                "Sign the configured dedicated bot profile in manually, then start the bot; "
                "the bot must never join under a personal account."
            )
        else:
            guidance = "This bot is configured for a fresh guest profile; use a meeting that permits guests."
        message = f"Google Meet asked the bot to pick an account at {where} ({phrase or 'sign-in page'}). {guidance}"
        if shot:
            message += f" Screenshot: {shot}"
        return message

    # ------------------------------------------------------------- join steps
    def wait_until_in_meeting(self, timeout=None):
        """Verify the bot is actually in the call; the click alone proves nothing."""
        timeout = config.JOIN_WAIT_TIMEOUT if timeout is None else timeout
        self._set_state(STATE_WAITING_FOR_ADMISSION)
        deadline = time.time() + timeout
        print(f"Watching for the bot to enter the meeting room (timeout {int(timeout)}s)...")
        lobby_announced = False
        rejoins = 0

        while time.time() < deadline:
            if not self._browser_alive():
                return self._fail("Chrome closed or lost its meeting window while the bot was joining.")

            page, text = self.read_page_state()

            if page == PAGE_IN_MEETING:
                print("Bot is inside the meeting room")
                self.last_error = None
                return True
            if page == PAGE_BLOCKED:
                self._block_text = text
                return self._fail(self._blocked_message(text))
            if page == PAGE_ACCOUNT_CHOOSER:
                return self._fail(self._account_chooser_message(text))
            if page == PAGE_LOBBY:
                if not lobby_announced:
                    lobby_announced = True
                    print("Meet put the bot in the lobby: waiting for the host to admit it")
            elif page == PAGE_PREJOIN and config.AUTO_CLICK_JOIN and rejoins < 2:
                # Meet sometimes returns to the pre-join screen after the first
                # click (device check re-render, name field validation). The join
                # did not happen, so click again instead of waiting out the clock.
                for label in JOIN_BUTTON_LABELS:
                    button = self._find_join_button(label)
                    if button is not None and self._click_join(button, label):
                        rejoins += 1
                        print(f"Join button was showing again; clicked again ({rejoins}/2)")
                        break
            if page in (PAGE_LOADING, PAGE_UNKNOWN) and self._left_meeting_page():
                shot = self.capture_diagnostics("ejected")
                message = (
                    "Meet returned the bot to its home screen, so the join was refused. "
                    f"{self._identity_hint()}"
                )
                if shot:
                    message += f" Screenshot: {shot}"
                self._block_text = text
                return self._fail(message)

            time.sleep(min(1.5, max(0, deadline - time.time())))

        return self._fail(
            self.last_error
            or (
                f"Timed out after {int(timeout)}s waiting for the bot to enter the meeting. "
                "If Meet showed the lobby, the host must admit the bot; if Meet never moved past "
                "the pre-join screen, the join click did not register."
            )
        )

    def attempt_to_join(self, timeout=None):
        """Click 'Ask to join' / 'Join now', re-opening the meeting if Meet ejects us."""
        timeout = config.JOIN_BUTTON_TIMEOUT if timeout is None else timeout
        deadline = time.time() + timeout
        self._block_text = ""

        while time.time() < deadline:
            if not self._browser_alive():
                self.last_error = "Chrome closed while looking for the Meet join button."
                print(self.last_error)
                return False

            page, text = self.read_page_state()

            if page == PAGE_BLOCKED:
                self._block_text = text
                self.last_error = self._blocked_message(text)
                print(self.last_error)
                return False
            if page == PAGE_ACCOUNT_CHOOSER:
                self.last_error = self._account_chooser_message(text)
                print(self.last_error)
                return False
            if page in (PAGE_LOBBY, PAGE_IN_MEETING):
                # Already past the click; wait_until_in_meeting takes it from here.
                self.last_error = None
                return True
            if self._left_meeting_page():
                print("Meet sent the bot back to the home screen; re-opening the meeting")
                if not self._open_meeting():
                    return False
                continue

            for label in JOIN_BUTTON_LABELS:
                button = self._find_join_button(label)
                if button is not None and self._click_join(button, label):
                    return True

            time.sleep(1)

        self.last_error = (
            f"No visible 'Join now' or 'Ask to join' button within {int(timeout)}s. "
            "Meet may be showing an interstitial, requiring sign-in, or the button is disabled."
        )
        print(self.last_error)
        return False

    def _find_join_button(self, label):
        if not self._browser_alive():
            return None
        try:
            elements = self.browser.find_elements(
                By.CSS_SELECTOR, "button, [role='button'], [role='link']"
            )
        except (NoSuchWindowException, WebDriverException):
            return None

        for element in elements:
            try:
                if not element.is_displayed() or not element.is_enabled():
                    continue
                candidates = [
                    element.text or "",
                    element.get_attribute("aria-label") or "",
                    element.get_attribute("data-tooltip") or "",
                ]
                text = " ".join(part.replace("\n", " ").strip().lower() for part in candidates)
                if label in text:
                    return element
            except (StaleElementReferenceException, WebDriverException):
                continue
            except Exception:
                continue
        return None

    def _click_join(self, button, label):
        try:
            button.click()
            print(f"Clicked '{label.title()}'")
            return True
        except (StaleElementReferenceException, WebDriverException):
            pass
        except Exception:
            pass

        # Selenium's click is refused when Meet overlays the button; a scripted
        # click goes through the same handler the user's mouse would.
        try:
            self.browser.execute_script("arguments[0].click();", button)
            print(f"Clicked '{label.title()}' (scripted)")
            return True
        except Exception:
            pass

        fresh = self._find_join_button(label)
        if fresh is None:
            return False
        try:
            fresh.click()
            print(f"Clicked '{label.title()}'")
            return True
        except Exception as error:
            self.last_error = f"Could not click '{label}': {error}"
            return False

    def dismiss_consent_banners(self):
        """Click through cookie/consent interstitials that can cover the join button."""
        for label in CONSENT_BUTTON_LABELS:
            if not self._browser_alive():
                return
            try:
                elements = self.browser.find_elements(By.CSS_SELECTOR, "button, [role='button']")
            except (NoSuchWindowException, WebDriverException):
                return
            for element in elements:
                try:
                    if not element.is_displayed():
                        continue
                    text = (element.text or "").strip().lower()
                    if text == label:
                        element.click()
                        print(f"Dismissed consent dialog ({label})")
                        time.sleep(0.5)
                        return
                except (StaleElementReferenceException, WebDriverException):
                    continue
                except Exception:
                    continue

    def enter_guest_name(self):
        if not config.BOT_DISPLAY_NAME:
            return
        if not self._browser_alive():
            return
        try:
            name_field = WebDriverWait(self.browser, 8).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ", ".join(GUEST_NAME_FIELD_CSS)))
            )
            if not name_field.is_displayed():
                return
            current = (name_field.get_attribute("value") or "").strip()
            if current.lower() == config.BOT_DISPLAY_NAME.strip().lower():
                print("Guest name already set")
                return
            name_field.click()
            if current:
                name_field.clear()
            name_field.send_keys(config.BOT_DISPLAY_NAME)
            name_field.send_keys(Keys.TAB)
            print(f"Set bot display name to '{config.BOT_DISPLAY_NAME}'")
        except TimeoutException:
            print("No guest name field found; already joining under a signed-in account")
        except Exception as error:
            print(f"Could not set bot name: {error}")

    def turn_off_microphone(self):
        self._turn_off_device("microphone")

    def turn_off_camera(self):
        self._turn_off_device("camera")

    def _turn_off_device(self, device):
        """Mute a device if Meet exposes an unmuted control; never sleep blindly."""
        if not self._browser_alive():
            print(f"Cannot toggle {device}: browser window is unavailable")
            return

        # Match accessible labels used by Meet. The controls can vary by locale/UI.
        selectors = [
            f'button[aria-label*="Turn off {device}" i]',
            f'[role="button"][aria-label*="Turn off {device}" i]',
            f'button[aria-label*="{device}" i][data-is-muted="false"]',
            f'[role="button"][aria-label*="{device}" i][data-is-muted="false"]',
        ]
        for selector in selectors:
            try:
                for button in self.browser.find_elements(By.CSS_SELECTOR, selector):
                    try:
                        if button.is_displayed() and button.is_enabled():
                            button.click()
                            print(f"{device.capitalize()} disabled")
                            return
                    except (StaleElementReferenceException, WebDriverException):
                        continue
            except (NoSuchWindowException, WebDriverException):
                print(f"Browser disappeared while toggling {device}")
                return
            except Exception:
                continue
        print(f"{device.capitalize()} already off or control not found")

    # -------------------------------------------------------------- recording
    def start_recording(self, session_id):
        if not self.meeting_is_active:
            print("No active meeting to record")
            return False
        if not self._browser_alive():
            return self._fail("Chrome is not available; cannot start recording")
        if not self._ensure_capture_script():
            print("Audio capture script is not present in the Meet page; recording will be empty")
        try:
            print(f"Starting dual-path recording: {session_id}")
            self.audio_recorder = StreamingAudioRecorder(self.browser, ws_url=config.AUDIO_WS_URL)
            started = self.audio_recorder.start_recording(
                session_id, config.RECORDINGS_DIR, browser_ws_url=config.AUDIO_WS_URL
            )
            if not started:
                print("Recording failed to start")
                self._set_state(STATE_FAILED)
                return False
            self._set_state(STATE_RECORDING, "Recording started (WAV + live streaming)")
            return True
        except Exception as error:
            print(f"Recording error: {error}")
            self._set_state(STATE_FAILED)
            return False

    def stop_recording(self):
        audio_file_path = None
        if self.audio_recorder:
            print("Stopping recording...")
            audio_file_path = self.audio_recorder.stop_recording()
            print(f"Audio saved: {audio_file_path}")
        return audio_file_path

    # ---------------------------------------------------------------- teardown
    def _register_active(self):
        """Mark this bot's chromedriver PID + profile dir as in-use so a later
        session's close_stale_automation_chrome() never kills it out from
        under a still-running join/recording/teardown."""
        try:
            pid = self.browser.service.process.pid
        except Exception:
            pid = None
        self.driver_pid = pid
        with _active_lock:
            if pid is not None:
                _active_driver_pids.add(pid)
            if self.profile_dir:
                _active_profile_dirs.add(self.profile_dir)

    def _unregister_active(self):
        with _active_lock:
            if self.driver_pid is not None:
                _active_driver_pids.discard(self.driver_pid)
            if self.profile_dir:
                _active_profile_dirs.discard(self.profile_dir)
        self.driver_pid = None

    def leave_meeting(self):
        """Idempotently stop recording, leave Meet if possible, and close Chrome."""
        if self._cleanup_done:
            return
        self._cleanup_done = True
        print("Leaving meeting...")
        try:
            if self.browser and self._browser_alive():
                self.find_and_click_leave_button()
        except Exception as error:
            print(f"Could not leave gracefully: {error}")
        finally:
            if self.browser:
                try:
                    self.browser.quit()
                    print("Browser closed")
                except Exception as error:
                    print(f"Browser quit reported (already closed?): {error}")
                # undetected-chromedriver's __del__ calls quit() again on a dead
                # handle. Neutralise it only now that the real quit has happened.
                try:
                    self.browser.quit = lambda: None
                except (AttributeError, TypeError):
                    pass
                self.browser = None
            self.meeting_is_active = False
            self._unregister_active()

            # Persist the Google session the browser just held, so the next run
            # can reuse it and never touch the password again. This MUST happen
            # before the throwaway profile is deleted below, and after quit() so
            # Chrome has flushed its cookies to disk.
            if self.temporary_profile and self.page_account:
                self._save_session_vault()

            # Persistent profiles contain the bot's Google login and must survive.
            if self.temporary_profile and self.profile_dir and os.path.isdir(self.profile_dir):
                shutil.rmtree(self.profile_dir, ignore_errors=True)
                print("Temporary bot profile removed")
            self.profile_dir = None
            self.temporary_profile = False
            self._set_state(STATE_LEFT)

    def find_and_click_leave_button(self):
        if not self._browser_alive():
            return False

        leave_button_selectors = [
            'button[aria-label*="Leave call" i]',
            '[role="button"][aria-label*="Leave call" i]',
            'button[aria-label*="Leave meeting" i]',
            '[role="button"][aria-label*="Leave meeting" i]',
            '[data-testid*="leave"]',
        ]
        for selector in leave_button_selectors:
            try:
                buttons = self.browser.find_elements(By.CSS_SELECTOR, selector)
                for leave_button in buttons:
                    try:
                        if leave_button.is_displayed() and leave_button.is_enabled():
                            leave_button.click()
                            print("Left meeting via button")
                            return True
                    except (StaleElementReferenceException, WebDriverException):
                        continue
            except (NoSuchWindowException, WebDriverException):
                return False
            except Exception:
                continue
        return False
