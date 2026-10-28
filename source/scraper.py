"""Extraction and dataset generation for the public Hiperdino catalogue."""

import argparse
import json
import re
import time
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Union
from urllib.parse import unquote, urljoin, urlsplit

import pandas as pd
from bs4 import BeautifulSoup, Tag

from session_config import (
    BASE_URL,
    PoliteSession,
    canonicalize_url,
    init_selenium_driver,
)


DATASET_DIR = Path(__file__).resolve().parents[1] / "dataset"
OUTPUT_CSV = DATASET_DIR / "hiperdino_cat.csv"
OUTPUT_CATEGORIES = DATASET_DIR / "categorias_descubiertas.txt"
DEBUG_HTML = DATASET_DIR / "debug_page.html"

PRODUCT_CARD_SELECTORS = (
    "li.product-list-item",
    "li.product-item",
    ".product-card",
    ".product-tile",
    ".product-item-info",
    ".df-card",
    "[data-product]",
    "article",
)
EXCLUDED_CATEGORY_KEYWORDS = (
    "empleo",
    "contacto",
    "politica",
    "cookies",
    "aviso-legal",
    "blog",
    "atencion-cliente",
    "quienes-somos",
    "tiendas",
    "folleto",
    "folletos",
    "noticias",
    "prensa",
    "trabaja",
    "contact",
    "account",
    "sorteo",
    "sorteos",
    "customer",
    "accionistas",
    "preguntas-frecuentes",
    "cuenta",
    "replacement",
    "introduce-tu-codigo",
)
PRODUCT_CATEGORY_KEYWORDS = (
    "alimentacion",
    "alimentario",
    "fresco",
    "congelado",
    "bebida",
    "refresco",
    "drogueria",
    "limpieza",
    "higiene",
    "perfumeria",
    "fruta",
    "verdura",
    "carne",
    "pescado",
    "marisco",
    "charcuteria",
    "panaderia",
    "lacteo",
    "queso",
    "huevo",
    "despensa",
    "conserva",
    "aceite",
    "cafe",
    "te",
    "mascota",
    "bebe",
    "marca-propia",
    "oferta",
    "producto",
    "supermercado",
    "hogar",
    "ocio",
    "ecologico",
    "gourmet",
    "infantil",
)
FALLBACK_CATEGORIES = [
    {
        "name": "Bebidas - Refrescos",
        "url": "https://www.hiperdino.es/c9510/bebidas/refrescos.html",
    },
    {
        "name": "Marca propia",
        "url": "https://www.hiperdino.es/c9510/marca-propia/marca-propia.html",
    },
]
PRODUCT_COLUMNS = [
    "Nombre",
    "Categoría",
    "Subcategoría",
    "Precio",
    "Precio/Unidad",
    "Marca",
    "Oferta",
    "EAN",
]


def _normalize_category_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", unquote(value).lower())
    without_accents = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    return re.sub(r"[_\s]+", "-", without_accents)


def _is_category_url(url: str, name: str = "") -> bool:
    """Keep product category links and discard unrelated site sections."""
    parts = urlsplit(url)
    if parts.netloc != urlsplit(BASE_URL).netloc:
        return False

    path = _normalize_category_text(parts.path)
    if re.match(r"^/c(?:\d+)?/.+", path) is None:
        return False

    searchable_text = "{} {}".format(path, _normalize_category_text(name))
    if any(keyword in searchable_text for keyword in EXCLUDED_CATEGORY_KEYWORDS):
        return False
    return any(keyword in searchable_text for keyword in PRODUCT_CATEGORY_KEYWORDS)


def _categories_from_html(
    html: str, client: PoliteSession
) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    categories: Dict[str, str] = {}
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if not href:
            continue
        candidate = canonicalize_url(urljoin(BASE_URL, href))
        name = anchor.get_text(" ", strip=True)
        if (
            name
            and _is_category_url(candidate, name)
            and client.can_fetch(candidate)
        ):
            categories.setdefault(candidate, name)
    return [
        {"name": name, "url": url}
        for url, name in categories.items()
    ]


def load_categories() -> List[Dict[str, str]]:
    """Load and validate category URLs saved in the project dataset directory."""
    categories: Dict[str, str] = {}
    for line in OUTPUT_CATEGORIES.read_text(encoding="utf-8-sig").splitlines():
        match = re.search(r"https?://\S+", line)
        if not match:
            continue

        url = canonicalize_url(match.group(0).rstrip(".,;)"))
        name = line[: match.start()].strip()
        name = re.sub(r"^\d+\s*[\.)]\s*", "", name)
        name = re.sub(r"\s*[—–-]\s*$", "", name).strip()
        if name and _is_category_url(url, name):
            categories.setdefault(url, name)
    return [{"name": name, "url": url} for url, name in categories.items()]


def save_category_list(categories: List[Dict[str, str]]) -> None:
    OUTPUT_CATEGORIES.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "{}. {} — {}".format(index, category["name"], category["url"])
        for index, category in enumerate(categories, start=1)
    ]
    OUTPUT_CATEGORIES.write_text("\n".join(lines) + "\n", encoding="utf-8")


def get_main_category_urls(
    client: PoliteSession, use_selenium: bool = False
) -> List[Dict[str, str]]:
    """Load cached categories or discover them from the shop homepage."""
    if OUTPUT_CATEGORIES.exists():
        categories = [
            category
            for category in load_categories()
            if client.can_fetch(category["url"])
        ]
        print("Categorías cargadas desde la caché local: {}".format(OUTPUT_CATEGORIES))
        if not categories:
            raise RuntimeError(
                "La caché existe, pero no contiene categorías válidas de productos. "
                "Revisa o elimina {} para volver a descubrirlas.".format(
                    OUTPUT_CATEGORIES
                )
            )
        return categories

    if not client.can_fetch(BASE_URL):
        raise PermissionError("robots.txt no permite esta URL: {}".format(BASE_URL))

    if use_selenium:
        from selenium.common.exceptions import TimeoutException
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait

        driver = init_selenium_driver()
        client.record_request()
        try:
            try:
                WebDriverWait(driver, 10).until(
                    lambda browser: browser.find_elements(
                        By.CSS_SELECTOR, "a[href*='/c']"
                    )
                )
            except TimeoutException:
                return []
            categories = _categories_from_html(driver.page_source, client)
        finally:
            driver.quit()
    else:
        response = client.get(BASE_URL)
        categories = _categories_from_html(response.text, client)

    if categories:
        save_category_list(categories)
        print("Categorías descubiertas desde la web: {}".format(BASE_URL))
    else:
        print("No se encontraron categorías válidas en la web: {}".format(BASE_URL))
    return categories


def _first_text(card: Tag, selectors: List[str]) -> str:
    for selector in selectors:
        element = card.select_one(selector)
        if element is not None:
            value = element.get("content") or element.get_text(" ", strip=True)
            if value:
                return str(value).strip()
    return ""


def _price_text(card: Tag) -> str:
    value = _first_text(
        card,
        [
            "[itemprop='price']",
            ".price--final .price__text",
            ".product__price .price__text",
            ".price",
            ".product-price",
            ".product__price",
        ],
    )
    if value:
        return value

    match = re.search(
        r"\d+[,.]\d{2}\s*€|€\s*\d+[,.]\d{2}",
        card.get_text(" ", strip=True),
    )
    return match.group(0) if match else ""


def _parse_products_from_html(html: str, category: str) -> List[Dict[str, object]]:
    """Extract product data from HTML cards when GTM data is unavailable."""
    soup = BeautifulSoup(html, "lxml")
    cards = []
    for selector in PRODUCT_CARD_SELECTORS:
        cards = soup.select(selector)
        if cards:
            break

    products: List[Dict[str, object]] = []
    for card in cards:
        name = _first_text(
            card,
            [
                "[itemprop='name']",
                ".description__text.name",
                ".product__description .name",
                ".product-item-name",
                ".product-name",
                "h2",
                "h3",
            ],
        )
        price = _price_text(card)
        if not name or not price:
            continue

        card_text = card.get_text(" ", strip=True)
        offer_node = card.select_one(
            ".special-price, .discount, [class*='offer'], [class*='promo']"
        )
        is_offer = bool(offer_node) or bool(
            re.search(
                r"\b(oferta|promoci[oó]n)\b", card_text, flags=re.IGNORECASE
            )
        )
        products.append(
            {
                "Nombre": name,
                "Categoría": category,
                "Subcategoría": "",
                "Precio": price,
                "Precio/Unidad": _first_text(
                    card,
                    [
                        ".price-per-unit",
                        "[class*='unit-price']",
                        "[class*='price-unit']",
                    ],
                ),
                "Marca": _first_text(
                    card, ["[itemprop='brand']", ".brand", "[class*='brand']"]
                ),
                "Oferta": "Sí" if is_offer else "No",
                "EAN": "",
            }
        )
    return products


def parse_gtm_product_data(
    html_or_driver: Union[str, object], category: str = ""
) -> List[Dict[str, object]]:
    """Parse GTM product JSON from HTML or a Selenium driver into eight fields."""
    if isinstance(html_or_driver, str):
        html = html_or_driver
    else:
        html = getattr(html_or_driver, "page_source", None)
        if not isinstance(html, str):
            raise TypeError("Se esperaba HTML o un WebDriver con page_source.")

    assignment = re.search(r"\bgtmProductDataObject\s*=\s*", html)
    if assignment is None:
        return _parse_products_from_html(html, category)

    json_text = html[assignment.end() :].lstrip()
    try:
        items, _ = json.JSONDecoder().raw_decode(json_text)
    except json.JSONDecodeError as error:
        raise ValueError(
            "No se pudo deserializar gtmProductDataObject como JSON."
        ) from error
    if not isinstance(items, dict):
        raise ValueError("gtmProductDataObject no contiene un objeto JSON.")

    products: List[Dict[str, object]] = []
    for item in items.values():
        if not isinstance(item, dict):
            continue
        name = item.get("name", "")
        if not name:
            continue
        products.append(
            {
                "Nombre": name,
                "Categoría": category or item.get("category", ""),
                "Subcategoría": item.get("category_name_sap", ""),
                "Precio": item.get("final_price", ""),
                "Precio/Unidad": item.get("price_text", ""),
                "Marca": item.get("label_brand") or str(name).split()[0],
                "Oferta": (
                    "Sí"
                    if item.get("special_price")
                    or item.get("promotion_description")
                    else "No"
                ),
                "EAN": item.get("ean", ""),
            }
        )
    return products


def _find_product_elements(driver):
    from selenium.webdriver.common.by import By

    for selector in PRODUCT_CARD_SELECTORS:
        elements = driver.find_elements(By.CSS_SELECTOR, selector)
        if elements:
            return elements
    return []


def _find_next_page_button(driver):
    from selenium.webdriver.common.by import By

    selectors = (
        ".hiperdino-pager__item--next a",
        ".pages-item-next a",
        "a[rel='next']",
        "a.next",
        ".action.next",
        "button[aria-label*='iguiente']",
        "a[aria-label*='iguiente']",
    )
    for selector in selectors:
        for element in driver.find_elements(By.CSS_SELECTOR, selector):
            if not element.is_displayed() or not element.is_enabled():
                continue
            classes = " ".join(
                (
                    element.get_attribute("class") or "",
                    element.get_attribute("aria-disabled") or "",
                    element.get_attribute("disabled") or "",
                )
            ).lower()
            parent = element.find_element(By.XPATH, "..")
            classes += " " + (parent.get_attribute("class") or "").lower()
            if "disabled" in classes or element.get_attribute("aria-disabled") == "true":
                continue
            return element
    return None


def _render_category_page(
    driver, client: PoliteSession, category_url: str
) -> str:
    from selenium.common.exceptions import TimeoutException
    from selenium.webdriver.support.ui import WebDriverWait

    canonical_url = canonicalize_url(category_url)
    if not client.can_fetch(canonical_url):
        raise PermissionError(
            "robots.txt no permite esta URL: {}".format(canonical_url)
        )

    client.wait_for_next_request()
    driver.get(canonical_url)
    client.record_request()
    time.sleep(3)
    driver.execute_script("window.scrollTo(0, 800);")
    time.sleep(2)
    try:
        WebDriverWait(driver, 10).until(_find_product_elements)
    except TimeoutException:
        pass

    product_elements = _find_product_elements(driver)
    print(
        "   └─ Elementos de producto encontrados en Selenium: {}".format(
            len(product_elements)
        )
    )
    for index, element in enumerate(product_elements[:5], start=1):
        classes = element.get_attribute("class") or "(sin clase)"
        print("      Contenedor {}: {}".format(index, classes))
    return driver.page_source


def scrape_catalog(
    use_selenium: bool = False,
    limit: Optional[int] = None,
    max_pages_per_category: int = 3,
) -> pd.DataFrame:
    """Scrape categories and return a DataFrame with the eight product fields."""
    client = PoliteSession()
    client.load_robots()
    categories = get_main_category_urls(client, use_selenium=use_selenium)
    if not categories:
        categories = FALLBACK_CATEGORIES
        print("No se encontraron categorías; se usarán las URLs de contingencia.")

    if limit is not None:
        categories = categories[:limit]
        print(
            "Límite de prueba activo: se procesarán {} categorías.".format(
                len(categories)
            )
        )

    print("🔍 Categorías encontradas: {}. Iniciando extracción...".format(len(categories)))
    products: List[Dict[str, object]] = []

    if use_selenium:
        from selenium.common.exceptions import TimeoutException
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        if not client.can_fetch(BASE_URL):
            raise PermissionError("robots.txt no permite esta URL: {}".format(BASE_URL))
        driver = init_selenium_driver()
        client.record_request()
        try:
            for index, category in enumerate(categories, start=1):
                print(
                    "[{}/{}] Renderizando categoría: {}...".format(
                        index, len(categories), category["name"]
                    )
                )
                html = _render_category_page(driver, client, category["url"])
                seen_pages = set()
                category_path = urlsplit(
                    canonicalize_url(category["url"])
                ).path

                for page_number in range(1, max_pages_per_category + 1):
                    if index == 1 and page_number == 1:
                        DEBUG_HTML.parent.mkdir(parents=True, exist_ok=True)
                        DEBUG_HTML.write_text(html, encoding="utf-8")
                        print(
                            "HTML renderizado de depuración guardado en {}".format(
                                DEBUG_HTML
                            )
                        )

                    extracted = parse_gtm_product_data(html, category["name"])
                    if not extracted:
                        print(
                            "   └─ Página {} sin productos; se detiene esta categoría."
                            .format(page_number)
                        )
                        break

                    page_signature = tuple(
                        (
                            str(item.get("EAN", "")),
                            str(item.get("Nombre", "")),
                            str(item.get("Precio", "")),
                        )
                        for item in extracted
                    )
                    if page_signature in seen_pages:
                        print(
                            "   └─ La página {} repite productos; se detiene para "
                            "evitar duplicados.".format(page_number)
                        )
                        break

                    seen_pages.add(page_signature)
                    products.extend(extracted)
                    print(
                        "   └─ Página {}: {} productos extraídos.".format(
                            page_number, len(extracted)
                        )
                    )
                    if page_number == max_pages_per_category:
                        print(
                            "   └─ Alcanzado el límite de {} páginas para esta "
                            "categoría.".format(max_pages_per_category)
                        )
                        break

                    next_button = _find_next_page_button(driver)
                    if next_button is None:
                        print("   └─ No hay botón de página siguiente.")
                        break
                    next_href = next_button.get_attribute("href")
                    if not next_href:
                        print(
                            "   └─ El enlace siguiente no expone una URL verificable; "
                            "no se hará clic."
                        )
                        break

                    next_url = urljoin(driver.current_url, next_href)
                    next_parts = urlsplit(next_url)
                    if (
                        next_parts.netloc != urlsplit(BASE_URL).netloc
                        or next_parts.path != category_path
                    ):
                        print(
                            "   └─ El enlace siguiente sale de la categoría actual; "
                            "se omite."
                        )
                        break
                    if not client.can_fetch(next_url):
                        print(
                            "   └─ robots.txt no permite la página siguiente; "
                            "se detiene la categoría: {}".format(next_url)
                        )
                        break

                    previous_url = driver.current_url
                    client.wait_for_next_request()
                    driver.execute_script("arguments[0].click();", next_button)
                    client.record_request()
                    try:
                        WebDriverWait(driver, 10).until(
                            EC.url_changes(previous_url)
                        )
                    except TimeoutException:
                        print(
                            "   └─ La navegación a la página siguiente no se "
                            "confirmó; se detiene la categoría."
                        )
                        break

                    time.sleep(2)
                    driver.execute_script("window.scrollTo(0, 800);")
                    time.sleep(2)
                    try:
                        WebDriverWait(driver, 10).until(_find_product_elements)
                    except TimeoutException:
                        pass
                    html = driver.page_source
        finally:
            driver.quit()
    else:
        for index, category in enumerate(categories, start=1):
            print(
                "[{}/{}] Descargando categoría: {}...".format(
                    index, len(categories), category["name"]
                )
            )
            response = client.get(category["url"])
            extracted = parse_gtm_product_data(response.text, category["name"])
            print("   └─ Extraídos {} productos.".format(len(extracted)))
            products.extend(extracted)

    if not products:
        raise RuntimeError(
            "No se extrajeron productos. Revisa el HTML actual y los selectores "
            "de tarjetas y campos antes de guardar el CSV."
        )
    return pd.DataFrame(products, columns=PRODUCT_COLUMNS)


def save_to_csv(df: pd.DataFrame, output_path: Union[str, Path]) -> None:
    """Save the dataset as a UTF-8-with-BOM CSV file."""
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(destination, index=False, encoding="utf-8-sig")


def list_categories(use_selenium: bool = False) -> List[Dict[str, str]]:
    """Discover and save category URLs without downloading product pages."""
    client = PoliteSession()
    client.load_robots()
    categories = get_main_category_urls(client, use_selenium=use_selenium)
    if not categories:
        raise RuntimeError(
            "No se descubrieron categorías de catálogo; no se ha creado la lista."
        )

    save_category_list(categories)
    print("Categorías de catálogo descubiertas ({}):".format(len(categories)))
    for index, category in enumerate(categories, start=1):
        print("{}. {} — {}".format(index, category["name"], category["url"]))
    print("Lista guardada en {}".format(OUTPUT_CATEGORIES))
    return categories


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("debe ser un número entero") from error
    if number < 1:
        raise argparse.ArgumentTypeError("debe ser mayor que cero")
    return number


def main() -> None:
    """Run the scraper command-line interface."""
    parser = argparse.ArgumentParser(
        description="Scraper académico del catálogo Hiperdino"
    )
    parser.add_argument(
        "--list-categories",
        action="store_true",
        help="Descubre, muestra y guarda categorías; no descarga productos.",
    )
    parser.add_argument(
        "--selenium",
        action="store_true",
        help="Usa Chrome headless para renderizar el catálogo.",
    )
    parser.add_argument(
        "--limit",
        type=_positive_int,
        help="Procesa solo las primeras N categorías (por ejemplo, --limit 3).",
    )
    parser.add_argument(
        "--max-pages-per-category",
        type=_positive_int,
        default=3,
        help="Máximo de páginas Selenium por categoría (por defecto: 3).",
    )
    args = parser.parse_args()

    if args.list_categories:
        list_categories(use_selenium=args.selenium)
        return

    catalog = scrape_catalog(
        use_selenium=args.selenium,
        limit=args.limit,
        max_pages_per_category=args.max_pages_per_category,
    )
    save_to_csv(catalog, OUTPUT_CSV)
    print("Guardados {} productos en {}".format(len(catalog), OUTPUT_CSV))


if __name__ == "__main__":
    main()
