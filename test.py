import os
import sys
import time
try:
    import winreg
except ImportError:  # non-Windows: fall back to the CHROME_VERSION_MAIN override
    winreg = None
from dotenv import load_dotenv
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import StaleElementReferenceException

# 1. Load environment variables
load_dotenv()
GOOGLE_EMAIL = os.getenv("GOOGLE_EMAIL")
GOOGLE_PASSWORD = os.getenv("GOOGLE_PASSWORD")
MEET_URL = os.getenv("MEET_URL")

# Google's sign-in page does not use <input type="email"> for the visible
# identifier field any more -- it renders type="text" name="identifier". The
# only type="email" input on the page is the *hidden* echo of the address on
# the *password* step (id="hiddenEmail"), which is why waiting for it times out.
EMAIL_LOCATORS = [
    (By.CSS_SELECTOR, "input[name='identifier']"),
    (By.CSS_SELECTOR, "input#identifierId"),
]
# Deliberately NOT input[type='password']: that also matches Google's hidden
# "hiddenPassword" field, which is still present on the password step, so the
# bot would type the password into an invisible box and submit nothing.
PASSWORD_LOCATORS = [
    (By.CSS_SELECTOR, "input[name='Passwd']"),
    (By.CSS_SELECTOR, "input#passwd"),
    (By.CSS_SELECTOR, "input[aria-label='Enter your password']"),
]
JOIN_LOCATORS = [
    (By.XPATH, "//span[contains(text(),'Join now') or contains(text(),'Ask to join')]"),
    (By.XPATH, "//span[contains(text(),'Join anyway')]"),
]


def chrome_major_version():
    """Major version of the installed Chrome, e.g. 153 for 153.0.8010.54.

    undetected_chromedriver has to be told which ChromeDriver build to fetch.
    Its own auto-detection is unreliable (it happily downloads the *next*
    major's driver and then fails with "This version of ChromeDriver only
    supports Chrome version 154"), so reading the real version out of the
    registry keeps this script working after Chrome auto-updates.
    """
    override = (os.getenv("CHROME_VERSION_MAIN") or "").strip()
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


def wait_for_any(driver, locators, timeout=25, what="element"):
    """First visible+enabled element matching any of `locators`.

    Google reworks this markup regularly, so every field is looked up by a list
    of selectors instead of one. Hidden decoy fields are skipped explicitly
    rather than left to the caller to guess about.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        for by, value in locators:
            for element in driver.find_elements(by, value):
                try:
                    if element.is_displayed() and element.is_enabled():
                        return element
                except StaleElementReferenceException:
                    continue
        time.sleep(0.25)
    tried = ", ".join(value for _, value in locators)
    # Dump what the page actually offers: when Google changes the markup again,
    # this line is enough to pick the new selector without re-debugging.
    try:
        found = ", ".join(
            f"type={e.get_attribute('type')} name={e.get_attribute('name')} id={e.get_attribute('id')}"
            for e in driver.find_elements(By.CSS_SELECTOR, "input")
        ) or "<none>"
    except StaleElementReferenceException:
        found = "<page changed while reading>"
    raise RuntimeError(
        f"Timed out after {timeout}s waiting for {what}.\n"
        f"  url     : {driver.current_url}\n"
        f"  tried   : {tried}\n"
        f"  on page : {found}"
    )


def run_meet_bot():
    # 2. Configure Undetected ChromeDriver options
    options = uc.ChromeOptions()

    # Pass media permissions so Google Meet doesn't block with popups
    options.add_argument("--use-fake-ui-for-media-stream")
    options.add_argument("--disable-notifications")

    # Optional: Point to a persistent profile directory to reuse session cookies
    # options.add_argument("--user-data-dir=./chrome_profile")

    version_main = chrome_major_version()
    print(f"[*] Launching browser instance (Chrome {version_main})...")
    driver = uc.Chrome(options=options, version_main=version_main)

    try:
        # 3. Navigate to Google Sign-In Page
        print("[*] Navigating to Google Accounts login...")
        driver.get("https://accounts.google.com/ServiceLogin")

        # 4. Enter Email
        print("[*] Entering email...")
        email_input = wait_for_any(driver, EMAIL_LOCATORS, what="the email field")
        email_input.clear()
        email_input.send_keys(GOOGLE_EMAIL)
        email_input.send_keys(Keys.RETURN)

        # 5. Enter Password
        print("[*] Entering password...")
        password_input = wait_for_any(driver, PASSWORD_LOCATORS, what="the password field")
        password_input.clear()
        password_input.send_keys(GOOGLE_PASSWORD)
        password_input.send_keys(Keys.RETURN)

        # Pause briefly to ensure sign-in processing finishes
        time.sleep(5)

        # 6. Navigate to Google Meet
        print(f"[*] Navigating to Google Meet room: {MEET_URL}")
        driver.get(MEET_URL)

        # 7. Turn off Mic and Camera before entering
        # Keyboard shortcuts in Google Meet: Ctrl + D (Mic), Ctrl + E (Camera)
        time.sleep(3)
        body = driver.find_element(By.TAG_NAME, "body")
        body.send_keys(Keys.CONTROL + "d")  # Mute Mic
        body.send_keys(Keys.CONTROL + "e")  # Mute Camera
        time.sleep(1)

        # 8. Click "Join now" or "Ask to join"
        print("[*] Attempting to join meeting...")
        join_button = wait_for_any(driver, JOIN_LOCATORS, what="the join button")
        join_button.click()
        print("[+] Bot joined the Google Meet session successfully.")

        # Keep browser open during the meeting session
        if sys.stdin.isatty():
            input("\n[Press ENTER in terminal to exit bot and close browser]\n")
        else:
            time.sleep(600)

    except Exception as e:
        # Selenium pads its exceptions with a chromedriver stack trace that hides
        # the useful first line, so print the type and leading message instead.
        print(f"[!] {type(e).__name__}: {str(e).split('Stacktrace')[0].strip()}")
    finally:
        driver.quit()
        # uc's Chrome.__del__ unconditionally calls quit() a second time, which
        # then fails on the already-dead driver handle (OSError: [WinError 6]).
        # Shadowing quit on the instance makes that finaliser a no-op.
        driver.quit = lambda: None


if __name__ == "__main__":
    missing = [
        name
        for name, value in (
            ("GOOGLE_EMAIL", GOOGLE_EMAIL),
            ("GOOGLE_PASSWORD", GOOGLE_PASSWORD),
            ("MEET_URL", MEET_URL),
        )
        if not value
    ]
    if missing:
        print(f"[!] Missing required .env values: {', '.join(missing)}")
        raise SystemExit(1)
    run_meet_bot()
