"""HTTP and Selenium configuration for the Hiperdino catalogue scraper."""

import re
import time
from typing import Optional
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests


BASE_URL = "https://www.hiperdino.es/"
ROBOTS_URL = "https://www.hiperdino.es/robots.txt"
USER_AGENT = "UOC-TipologiaDatos-AcademicScraper/1.0"
REQUEST_DELAY_SECONDS = 2
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml",
}


def canonicalize_url(url: str) -> str:
    """Remove query strings and fragments so requests use canonical URLs."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", ""))


class PoliteSession:
    """A requests session that enforces a delay and checks robots.txt."""

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.robots = RobotFileParser()
        self._last_request_at: Optional[float] = None

    def wait_for_next_request(self) -> None:
        """Wait until the courtesy interval has elapsed."""
        if self._last_request_at is None:
            return
        elapsed = time.monotonic() - self._last_request_at
        remaining = REQUEST_DELAY_SECONDS - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def record_request(self) -> None:
        """Record a completed HTTP or browser navigation."""
        self._last_request_at = time.monotonic()

    def can_fetch(self, url: str) -> bool:
        """Return whether robots.txt permits this user agent to fetch a URL."""
        return self.robots.can_fetch(USER_AGENT, canonicalize_url(url))

    def _get(self, url: str) -> requests.Response:
        self.wait_for_next_request()
        try:
            response = self.session.get(url, timeout=30, allow_redirects=False)
            if response.is_redirect:
                raise requests.HTTPError(
                    "Redirección no seguida para evitar consultar una URL no "
                    "verificada: {}".format(response.headers.get("Location", "")),
                    response=response,
                )
            response.raise_for_status()
            return response
        finally:
            self.record_request()

    def load_robots(self) -> None:
        """Download and parse the site's robots.txt rules."""
        response = self._get(ROBOTS_URL)
        self.robots.set_url(ROBOTS_URL)
        self.robots.parse(response.text.splitlines())

    def get(self, url: str) -> requests.Response:
        """Fetch a canonical URL only when robots.txt allows it."""
        canonical_url = canonicalize_url(url)
        if not self.can_fetch(canonical_url):
            raise PermissionError(
                "robots.txt no permite esta URL: {}".format(canonical_url)
            )
        return self._get(canonical_url)


def _handle_postal_code_and_cookies(driver) -> None:
    """Accept the cookie notice and select the Canary Islands postal code."""
    from selenium.common.exceptions import (
        ElementNotInteractableException,
        NoSuchElementException,
        StaleElementReferenceException,
        TimeoutException,
    )
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    cookie_selector = (
        "#onetrust-accept-btn-handler, "
        "[id*='cookie'] button, [class*='cookie'] button, "
        "[id*='consent'] button, [class*='consent'] button, "
        "[id*='privacy'] button, [class*='privacy'] button, "
        "[id*='privacidad'] button, [class*='privacidad'] button, "
        "[data-testid*='cookie'] button"
    )
    cookie_buttons = driver.find_elements(By.CSS_SELECTOR, cookie_selector)
    for button in cookie_buttons:
        try:
            if not button.is_displayed() or not button.is_enabled():
                continue
            label = " ".join(
                str(button.get_attribute(attribute) or "")
                for attribute in ("aria-label", "value", "title", "id")
            )
            label += " " + button.text
            if not re.search(
                r"\b(aceptar|accept|permitir|allow|consentir)\b",
                label,
                flags=re.IGNORECASE,
            ):
                continue

            WebDriverWait(driver, 3).until(EC.element_to_be_clickable(button))
            driver.execute_script("arguments[0].click();", button)
            print("Se aceptó el aviso de cookies/privacidad.")
            break
        except (
            ElementNotInteractableException,
            NoSuchElementException,
            StaleElementReferenceException,
            TimeoutException,
        ):
            continue

    postal_markers = ("postal", "postcode", "zip", "codigo-postal", "postalcode")
    accept_pattern = re.compile(
        r"\b(aceptar|confirmar|continuar|guardar|seleccionar|aplicar|entrar)\b",
        flags=re.IGNORECASE,
    )
    try:
        WebDriverWait(driver, 8).until(
            lambda browser: any(
                container.is_displayed()
                and any(
                    field.is_displayed() and field.is_enabled()
                    for field in container.find_elements(By.CSS_SELECTOR, "input")
                )
                for container in browser.find_elements(
                    By.CSS_SELECTOR,
                    "[role='dialog'], form, .modal, [class*='modal'], "
                    "[class*='popup']",
                )
            )
        )
    except TimeoutException:
        return

    dialogs = driver.find_elements(
        By.CSS_SELECTOR,
        "[role='dialog'], form, .modal, [class*='modal'], [class*='popup']",
    )
    for container in dialogs:
        try:
            if not container.is_displayed():
                continue
            normalized_text = re.sub(
                r"[_\s]+", "-", (container.text or "").lower()
            )
            if not any(
                marker in normalized_text
                for marker in ("postal", "codigo-postal", "tienda", "codigo")
            ):
                continue

            visible_fields = [
                field
                for field in container.find_elements(By.CSS_SELECTOR, "input")
                if field.is_displayed() and field.is_enabled()
            ]
            field = next(
                (
                    candidate
                    for candidate in visible_fields
                    if any(
                        marker
                        in re.sub(
                            r"[_\s]+",
                            "-",
                            " ".join(
                                str(candidate.get_attribute(attribute) or "")
                                for attribute in (
                                    "name",
                                    "id",
                                    "placeholder",
                                    "aria-label",
                                    "autocomplete",
                                )
                            ).lower(),
                        )
                        for marker in postal_markers
                    )
                ),
                visible_fields[0] if len(visible_fields) == 1 else None,
            )
            if field is None:
                continue

            field.clear()
            field.send_keys("35001")
            buttons = container.find_elements(
                By.CSS_SELECTOR, "button, input[type='submit'], [role='button']"
            )
            buttons.extend(
                driver.find_elements(By.CSS_SELECTOR, "[data-myaction='checkCp']")
            )
            for button in buttons:
                label = " ".join(
                    str(button.get_attribute(attribute) or "")
                    for attribute in ("aria-label", "value", "title")
                )
                label += " " + button.text
                if (
                    button.is_displayed()
                    and button.is_enabled()
                    and (
                        accept_pattern.search(label)
                        or button.get_attribute("data-myaction") == "checkCp"
                    )
                ):
                    WebDriverWait(driver, 3).until(
                        EC.element_to_be_clickable(button)
                    )
                    driver.execute_script("arguments[0].click();", button)
                    print("Código postal 35001 enviado.")
                    try:
                        WebDriverWait(driver, 8).until(
                            EC.invisibility_of_element(container)
                        )
                    except TimeoutException:
                        print(
                            "Aviso: el diálogo de ubicación sigue visible "
                            "tras enviar el código postal."
                        )
                    return
        except (
            ElementNotInteractableException,
            NoSuchElementException,
            StaleElementReferenceException,
        ):
            continue


def init_selenium_driver(headless: bool = True):
    """Start Chrome, open the shop, accept cookies, and select postal code 35001."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.support.ui import WebDriverWait

    options = Options()
    if headless:
        options.add_argument("--headless")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--user-agent={}".format(USER_AGENT))
    driver = webdriver.Chrome(options=options)
    initialized = False
    try:
        driver.get(BASE_URL)
        WebDriverWait(driver, 15).until(
            lambda browser: browser.execute_script(
                "return document.readyState"
            )
            in ("interactive", "complete")
        )
        time.sleep(2)
        _handle_postal_code_and_cookies(driver)
        initialized = True
        return driver
    finally:
        if not initialized:
            driver.quit()
