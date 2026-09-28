"""Automatic Google sign-in for the bot, with no human step.

The flow is ported from test.py, which was written against a live page and is
known to work on this host. Two details in there are load-bearing and are kept
verbatim rather than "tidied up":

*   The identifier field is looked up as input[name='identifier'] /
    input#identifierId, never input[type='email']. Google renders the visible
    address box as type="text"; the only type="email" input on the page is the
    *hidden* echo of the address (id="hiddenEmail") on the password step, so
    waiting for type='email' times out.

*   The password field is input[name='Passwd'], never input[type='password'].
    Google's password step keeps a hidden "hiddenPassword" input in the DOM, so
    the generic selector matches an invisible box, the bot types into it, and
    nothing is submitted.

Google does not simply accept a scripted sign-in. When it refuses, it refuses
loudly -- a CAPTCHA, an "unusual traffic" interstitial, or a two-step prompt.
This module detects each of those and reports it, rather than looping on a page
that will never resolve. The caller turns that report into a plain-language
message; the bot never hangs and never silently degrades to an anonymous guest.
"""
import os
import re
import time

import config
from selenium.common.exceptions import StaleElementReferenceException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

try:
    import winreg
except ImportError:  # non-Windows: only the CHROME_VERSION_MAIN override works
    winreg = None

# Kept from test.py -- see the module docstring for why these and not others.
EMAIL_LOCATORS = (
    (By.CSS_SELECTOR, "input[name='identifier']"),
    (By.CSS_SELECTOR, "input#identifierId"),
)
PASSWORD_LOCATORS = (
    (By.CSS_SELECTOR, "input[name='Passwd']"),
    (By.CSS_SELECTOR, "input#passwd"),
    (By.CSS_SELECTOR, "input[aria-label='Enter your password']"),
)

# Google hands a scripted browser a challenge instead of a password box. Each of
# these is terminal: retrying the form on such a page cannot succeed.
CHALLENGE_PHRASES = (
    "this browser or app may not be secure",
    "couldn't sign you in",
    "unusual traffic",
    "confirm you're not a robot",
    "confirm you are not a robot",
    "not a robot",
    "recaptcha",
    "captcha",
    "two-step verification",
    "2-step verification",
    "verify it's you",
    "verify it is you",
    "suspicious activity",
    "your account has been temporarily disabled",
    "too many attempts",
)

# Pages that mean the password was accepted and Google is done with us.
# Every phrase here is one that appears only *after* a completed sign-in. Note
# what is NOT here: "google account", which also occurs on the login page itself
# ("Sign in with your Google account") and so cannot be used as evidence.
SIGNED_IN_PHRASES = (
    "sign out",
    "my account",
    "manage your google account",
    "your google account",
)

# Any of these in the URL means we are still mid-authentication, whatever the
# body text says. Google version-prefixes these paths, so "/v3/signin" has to be
# matched without assuming a fixed prefix.
GOOGLE_SIGNIN_URL_MARKERS = (
    "/signin",
    "/service-login",
    "accounts.google.com/signin",
    "accounts.google.com/o/oauth2",
    "accounts.google.com/multi",
    "/challenge",
)

GOOGLE_HOSTS = ("accounts.google.com", "myaccount.google.com", "accounts.youtube.com")


class LoginChallenge(Exception):
    """Google refused the scripted sign-in. The message is user-facing."""


def chrome_major_version():
    """Major version of the installed Chrome, e.g. 153 for 153.0.8010.54.

    undetected-chromedriver has to be told which ChromeDriver build to fetch, and
    its own detection is unreliable -- it happily downloads the next major's
    driver and then refuses to run. Reading the real version out of the registry
    keeps this working across Chrome auto-updates.
    """
    override = (config.CHROME_VERSION_MAIN or "").strip()
    if override.isdigit():
        return int(override)
    if winreg is None:
        raise RuntimeError(
            "Chrome version detection needs Windows. Set CHROME_VERSION_MAIN in "
            ".env to your Chrome major version (chrome://version)."
        )
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\Google\Chrome\BLBeacon") as key:
                return int(winreg.QueryValueEx(key, "version")[0].split(".")[0])
        except OSError:
            continue
    raise RuntimeError(
        "Could not determine the installed Chrome version. Set CHROME_VERSION_MAIN "
        "in .env to your Chrome major version (chrome://version)."
    )


def credentials_configured():
    """True when both halves of the credential are present."""
    return bool(config.GOOGLE_EMAIL and config.GOOGLE_PASSWORD)


def wait_for_any(driver, locators, timeout=25, what="element", browser_alive=None):
    """First visible+enabled element matching any of `locators`.

    Google reworks this markup regularly, so every field is a list of selectors
    rather than one, and hidden decoys are skipped explicitly.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if browser_alive is not None and not browser_alive():
            raise WebDriverException("The browser window closed while waiting for " + what)
        for by, value in locators:
            try:
                elements = driver.find_elements(by, value)
            except WebDriverException:
                raise
            for element in elements:
                try:
                    if element.is_displayed() and element.is_enabled():
                        return element
                except StaleElementReferenceException:
                    continue
        time.sleep(0.25)
    tried = ", ".join(value for _, value in locators)
    raise LoginChallenge(
        f"Timed out after {int(timeout)}s waiting for {what} (tried: {tried}). "
        "Google changed its sign-in page, or it served a challenge instead."
    )


def _page_text(driver):
    """Visible text of the page, lowercased and whitespace-collapsed.

    Selenium's element.text comes back empty on Google's sign-in pages, whose
    content is assembled from nested/aria-only nodes. That silently blinds both
    detect_challenge() and is_signed_in(), so fall back to innerText and then to
    textContent, which do return the real string.
    """
    for getter in (
        lambda: driver.find_element(By.TAG_NAME, "body").text,
        lambda: driver.execute_script("return document.body ? document.body.innerText : ''"),
        lambda: driver.execute_script("return document.body ? document.body.textContent : ''"),
    ):
        try:
            text = _normalize(getter() or "")
        except (StaleElementReferenceException, WebDriverException):
            continue
        if text:
            return text
    return ""


# Google's own wording when the password is wrong. Checked only to make the
# error message accurate -- a wrong password is not distinguishable from a
# challenge by URL alone, since both keep you on the same challenge URL.
WRONG_PASSWORD_PHRASES = (
    "wrong password",
    "password is incorrect",
    "your password was wrong",
    "try again",
    "check your password",
)


def _normalize(text):
    if not text:
        return ""
    for smart in ("\u2019", "\u2018", "`"):
        text = text.replace(smart, "'")
    return " ".join(text.lower().split())


def detect_challenge(driver):
    """The challenge phrase Google is showing right now, or None."""
    text = _page_text(driver)
    for phrase in CHALLENGE_PHRASES:
        if phrase in text:
            return phrase
    # A challenge can also be pure markup with no readable label.
    if _has_captcha_iframe(driver):
        return "a CAPTCHA"
    return None


def _has_captcha_iframe(driver):
    try:
        frames = driver.find_elements(
            By.CSS_SELECTOR, "iframe[src*='recaptcha'], iframe[title*='reCAPTCHA' i]"
        )
    except (StaleElementReferenceException, WebDriverException):
        return False
    return any(frame.is_displayed() for frame in frames)


def is_signed_in(driver):
    """True when the page looks like a settled, signed-in Google account page.

    The URL guard is checked first and is deliberately broad. Google serves the
    login form from a rotating set of paths -- /signin/identifier,
    /v3/signin/challenge/pwd, /ServiceLogin, /challenge/... -- so matching one
    literal path lets a genuine login page through the guard and report a false
    positive on the phrase test below ("Sign in with your Google account"
    contains "google account").
    """
    url = (driver.current_url or "").lower()
    if any(marker in url for marker in GOOGLE_SIGNIN_URL_MARKERS):
        return False
    text = _page_text(driver)
    return any(phrase in text for phrase in SIGNED_IN_PHRASES)


def is_google_account_page(driver):
    url = (driver.current_url or "").lower()
    return any(host in url for host in GOOGLE_HOSTS)


def sign_in(driver, browser_alive=None, log=print):
    """Drive Google's sign-in form. Raises LoginChallenge on any refusal.

    Returns the URL the browser settled on. The caller is responsible for
    checking that this actually produced a session -- sign_in only reports that
    the form was submitted without being challenged.
    """
    if not credentials_configured():
        raise LoginChallenge(
            "A Google account is required but GOOGLE_EMAIL/GOOGLE_PASSWORD are not set."
        )

    if not is_google_account_page(driver):
        log("Opening the Google sign-in page...")
        driver.get("https://accounts.google.com/ServiceLogin")

    challenge = detect_challenge(driver)
    if challenge:
        raise LoginChallenge(_challenge_message(challenge))

    log("Entering email...")
    _fill(driver, EMAIL_LOCATORS, config.GOOGLE_EMAIL, "the email field", browser_alive)

    challenge = detect_challenge(driver)
    if challenge:
        raise LoginChallenge(_challenge_message(challenge))

    log("Entering password...")
    _fill(driver, PASSWORD_LOCATORS, config.GOOGLE_PASSWORD, "the password field", browser_alive)

    # Google decides what happens next asynchronously; wait for it to land
    # somewhere decisive rather than sleeping a fixed amount and hoping.
    #
    # The grace period matters: pressing Enter does not navigate instantly, and
    # the password box is still on screen while Google's XHR is in flight. A
    # check that fires immediately would see that box and report "rejected"
    # before the server had been asked anything -- which is how a correct
    # password looked like a wrong one. Only a password field that is *still*
    # there after Google has had time to react is a real rejection.
    submitted_at = time.time()
    deadline = time.time() + config.LOGIN_SETTLE_TIMEOUT
    while time.time() < deadline:
        if browser_alive is not None and not browser_alive():
            raise WebDriverException("The browser window closed during sign-in.")

        challenge = detect_challenge(driver)
        if challenge:
            raise LoginChallenge(_challenge_message(challenge))

        url = (driver.current_url or "").lower()
        if is_signed_in(driver) or "accounts.google.com/signin" not in url:
            # Either we are signed in, or Google moved us off the sign-in form
            # (to a consent screen, or straight back to the caller).
            if "consent" not in url and "signin" not in url:
                log(f"Google accepted the sign-in ({driver.current_url})")
                return driver.current_url

        if _password_field_present(driver):
            if time.time() - submitted_at < PASSWORD_SETTLE_GRACE_SECONDS:
                time.sleep(0.5)
                continue
            raise LoginChallenge(_password_rejected_message(driver))
        time.sleep(0.5)

    raise LoginChallenge(
        f"Google did not finish the sign-in within {config.LOGIN_SETTLE_TIMEOUT}s "
        f"(last page: {driver.current_url})."
    )


# How long to let Google's password check round-trip before treating a still-
# present password box as a rejection. Generous on purpose: a false "wrong
# password" is far more damaging than a slightly slower failure, because it
# sends the operator to fix credentials that are already correct.
PASSWORD_SETTLE_GRACE_SECONDS = 8.0


def _password_rejected_message(driver):
    """Distinguish 'your password is wrong' from 'Google wanted something else'."""
    text = _page_text(driver)
    for phrase in WRONG_PASSWORD_PHRASES:
        if phrase in text:
            return (
                "Google rejected the password and re-showed the sign-in form "
                f'(Google showed: "{phrase}"). Check GOOGLE_PASSWORD in .env.'
            )
    return (
        "Google did not accept the sign-in and returned to the password page "
        f"(still at {driver.current_url}). Either GOOGLE_PASSWORD is wrong, or "
        "Google required an extra step that unattended sign-in cannot complete. "
        "Sign in by hand once to fill the session vault, and the bot will not "
        "need the password again until that session expires."
    )


def _fill(driver, locators, value, what, browser_alive=None):
    field = wait_for_any(
        driver,
        locators,
        timeout=config.LOGIN_FIELD_TIMEOUT,
        what=what,
        browser_alive=browser_alive,
    )
    try:
        field.clear()
    except (StaleElementReferenceException, WebDriverException):
        pass
    # send_keys takes the value as a single string, so no character is
    # interpreted as a key sequence. Kept explicit so a password containing
    # "+", "^" or "%" cannot be mangled by the WebDriver key parser.
    field.send_keys(value)
    field.send_keys(Keys.RETURN)


def _password_field_present(driver):
    for by, value in PASSWORD_LOCATORS:
        try:
            for element in driver.find_elements(by, value):
                if element.is_displayed():
                    return True
        except (StaleElementReferenceException, WebDriverException):
            return False
    return False


CHALLENGE_ADVICE = {
    "this browser or app may not be secure": (
        "Google refused to accept an automated sign-in from this browser."
    ),
    "couldn't sign you in": "Google refused the sign-in outright.",
    "unusual traffic": "Google thinks this looks like automated traffic.",
    "recaptcha": "Google asked for a CAPTCHA, which a bot cannot solve.",
    "a captcha": "Google asked for a CAPTCHA, which a bot cannot solve.",
    "captcha": "Google asked for a CAPTCHA, which a bot cannot solve.",
    "two-step verification": (
        "The account has 2-Step Verification, which a scripted sign-in cannot complete."
    ),
    "2-step verification": (
        "The account has 2-Step Verification, which a scripted sign-in cannot complete."
    ),
    "verify it's you": "Google asked for a second factor verification.",
    "verify it is you": "Google asked for a second factor verification.",
    "not a robot": "Google asked to confirm a human is present.",
    "confirm you're not a robot": "Google asked to confirm a human is present.",
    "confirm you are not a robot": "Google asked to confirm a human is present.",
    "suspicious activity": "Google flagged the sign-in as suspicious activity.",
    "too many attempts": "Google has temporarily blocked further sign-in attempts.",
    "your account has been temporarily disabled": "Google has disabled the account.",
}


def _challenge_message(phrase):
    advice = CHALLENGE_ADVICE.get(phrase, "Google refused the automated sign-in.")
    return (
        f"{advice} (Google showed: \"{phrase}\")\n"
        "Automatic sign-in is not reliable against Google. Sign the bot in by hand "
        "once to fill the session vault, and it will not need the password again "
        "until that session expires."
    )
