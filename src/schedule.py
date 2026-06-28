import argparse
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import sleep
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from .config import BASE_URL, HEADERS, LOG_DIR, RACECARD_URL, REQUEST_RETRY, REQUEST_TIMEOUT
from .db import connect, init_db, save_schedule
from .parser import (
    build_race_id,
    extract_lineup,
    extract_race_conditions,
    normalize_text,
    parse_deadline_time,
    parse_japanese_date,
    parse_start_time,
)


JST = timezone(timedelta(hours=9))
RACECARD_LINK_RETRY = 5
RACECARD_DETAIL_RE = re.compile(r"/keirin/[^/]+/(?:racecard|racedata|raceresult)/(\d{10})/(\d+)/(\d+)")


@dataclass(frozen=True)
class ScheduleLink:
    url: str
    race_no: int


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / "schedule.log", mode="w", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def default_target_date() -> str:
    return datetime.now(JST).date().isoformat()


def fetch_html(url: str, retry: int = REQUEST_RETRY) -> str:
    last_error = None
    for attempt in range(1, retry + 1):
        try:
            response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            last_error = exc
            logging.warning("Fetch failed (%s/%s): %s", attempt, retry, url)
            sleep(attempt)
    raise RuntimeError(f"Failed to fetch {url}") from last_error


def fetch_rendered_html(url: str, output_name: str, selector: str, retry: int = REQUEST_RETRY) -> str:
    last_error = None
    output_path = LOG_DIR / output_name
    for attempt in range(1, retry + 1):
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(
                    user_agent=HEADERS["User-Agent"],
                    viewport={"width": 1366, "height": 900},
                )
                page.goto(url, wait_until="networkidle", timeout=REQUEST_TIMEOUT * 1000)
                try:
                    page.wait_for_selector(selector, timeout=10_000)
                except PlaywrightTimeoutError:
                    logging.warning("Rendered selector was not visible yet: %s", selector)
                html = page.content()
                browser.close()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(html, encoding="utf-8")
            return html
        except Exception as exc:
            last_error = exc
            logging.warning("Rendered fetch failed (%s/%s): %s", attempt, retry, url)
            sleep(attempt)
    raise RuntimeError(f"Failed to render {url}") from last_error


def racecard_list_urls(target_date: str) -> list[str]:
    compact_date = target_date.replace("-", "")
    return [
        f"{RACECARD_URL}/{compact_date}",
        RACECARD_URL,
    ]


def extract_racecard_links(html: str) -> list[ScheduleLink]:
    soup = BeautifulSoup(html, "html.parser")
    links: list[ScheduleLink] = []
    seen = set()
    for anchor in soup.select('a[href*="/keirin/"]'):
        href = anchor.get("href")
        if not href:
            continue
        url = urljoin(BASE_URL, href)
        match = RACECARD_DETAIL_RE.search(urlparse(url).path)
        if not match:
            continue
        race_no = int(match.group(3))
        racecard_url = re.sub(r"/raceresult/", "/racecard/", url)
        if racecard_url in seen:
            continue
        seen.add(racecard_url)
        links.append(ScheduleLink(url=racecard_url, race_no=race_no))
    return links


def fetch_racecard_links(target_date: str) -> list[ScheduleLink]:
    last_html = ""
    for attempt in range(1, RACECARD_LINK_RETRY + 1):
        for url in racecard_list_urls(target_date):
            logging.info("Rendering racecard list (%s/%s): %s", attempt, RACECARD_LINK_RETRY, url)
            html = fetch_rendered_html(url, "racecard_rendered.html", 'a[href*="/keirin/"]')
            last_html = html
            links = extract_racecard_links(html)
            if links:
                return links
            logging.warning("No racecard links found in rendered page: %s", url)
        if attempt < RACECARD_LINK_RETRY:
            wait_seconds = 60 * attempt
            logging.warning("Racecard links were empty. Retrying after %s seconds.", wait_seconds)
            sleep(wait_seconds)
    return extract_racecard_links(last_html)


def extract_schedule_meta(html: str, detail_url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    page_text = "\n".join(soup.stripped_strings)
    url_match = RACECARD_DETAIL_RE.search(urlparse(detail_url).path)
    if not url_match:
        raise ValueError(f"Unsupported racecard URL: {detail_url}")

    race_date = parse_japanese_date(page_text) or url_match.group(1)[:8]
    if race_date and re.fullmatch(r"\d{8}", race_date):
        race_date = f"{race_date[:4]}-{race_date[4:6]}-{race_date[6:8]}"
    race_conditions = extract_race_conditions(soup)
    lineup = extract_lineup(html)
    venue = extract_venue(soup, page_text)
    race_no = int(url_match.group(3))
    race_id = build_race_id(race_date, venue, race_no, detail_url)

    return {
        "race_id": race_id,
        "race_date": race_date,
        "venue": venue,
        "race_no": race_no,
        "event_name": race_conditions.get("event_name"),
        "race_title": race_conditions.get("race_title"),
        "race_class": race_conditions.get("race_class"),
        "start_time": parse_start_time(page_text),
        "deadline_time": parse_deadline_time(page_text),
        "status": race_conditions.get("status"),
        "distance": race_conditions.get("distance"),
        "laps": race_conditions.get("laps"),
        "weather": race_conditions.get("weather"),
        "temperature": race_conditions.get("temperature"),
        "wind_direction": race_conditions.get("wind_direction"),
        "wind_speed": race_conditions.get("wind_speed"),
        "lineup_text": lineup.get("lineup_text"),
        "lineup": lineup.get("lineup"),
        "detail_url": detail_url,
    }


def extract_venue(soup: BeautifulSoup, page_text: str) -> str | None:
    title = soup.find("title")
    candidates = []
    if title and title.string:
        candidates.append(title.string)
    candidates.append(page_text)
    for text in candidates:
        match = re.search(r"([^\s]+)競輪", text)
        if match:
            return match.group(1)
        match = re.search(r"([^\s]+)遶ｶ霈ｪ", text)
        if match:
            return match.group(1)
    return None


def extract_entries(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    table_entries = extract_entry_rows_from_tables(soup)
    if table_entries:
        return table_entries
    return extract_entry_rows_from_text(soup)


def extract_entry_rows_from_tables(soup: BeautifulSoup) -> list[dict]:
    entries: list[dict] = []
    for row in soup.select("tr"):
        cell_elements = row.find_all(["td", "th"])
        cells = [normalize_text(cell.get_text(" ", strip=True)) for cell in cell_elements]
        if len(cells) < 3 or not cells[0].isdigit():
            continue
        racer_link = row.select_one('a[href*="/keirin/cyclist/"]')
        racer_name = normalize_text(racer_link.get_text(" ", strip=True)) if racer_link else None
        racer_cell_index = None
        if racer_link:
            for index, cell in enumerate(cell_elements):
                if racer_link in cell.select('a[href*="/keirin/cyclist/"]'):
                    racer_cell_index = index
                    break
        if not racer_name:
            text_candidates = [cell for cell in cells if re.search(r"[\u4e00-\u9fff]{2,}", cell)]
            racer_name = text_candidates[0] if text_candidates else None
        if not racer_name:
            continue
        meta = parse_entry_meta(" ".join(cells), racer_name)
        if racer_cell_index is not None:
            meta.update({
                key: value
                for key, value in parse_detailed_entry_cells(cells, racer_cell_index).items()
                if value is not None
            })
        entries.append({
            "car_no": entry_car_no(cells, racer_cell_index),
            "racer_name": racer_name,
            **meta,
        })
    return dedupe_entries(entries)


def extract_entry_rows_from_text(soup: BeautifulSoup) -> list[dict]:
    texts = [normalize_text(text) for text in soup.stripped_strings]
    entries: list[dict] = []
    for index, text in enumerate(texts):
        if not re.fullmatch(r"\d", text):
            continue
        window = " ".join(texts[index:index + 12])
        name_match = re.search(r"([\u4e00-\u9fff]{2,}(?:\s+[\u4e00-\u9fff]{1,})?)", window)
        if not name_match:
            continue
        racer_name = normalize_text(name_match.group(1))
        meta = parse_entry_meta(window, racer_name)
        entries.append({
            "car_no": int(text),
            "racer_name": racer_name,
            **meta,
        })
    return dedupe_entries(entries)


def parse_entry_meta(text: str, racer_name: str) -> dict:
    compact = text.replace(racer_name, " ")
    prefecture = None
    racer_class = None
    age = None
    term = None
    meta_match = re.search(
        r"(?P<prefecture>\S+)\s+(?P<class>[ASL]\d?)\s+(?P<age>\d{1,2})歳\s+(?P<term>\d+)期",
        compact,
    )
    if meta_match:
        prefecture = meta_match.group("prefecture")
        racer_class = meta_match.group("class")
        age = int(meta_match.group("age"))
        term = int(meta_match.group("term"))
    score = first_float_after_labels(text, ["競走得点", "得点"])
    rates = [float(value) for value in re.findall(r"(\d{1,3}\.\d)%", text)]
    return {
        "class": racer_class,
        "prefecture": prefecture,
        "age": age,
        "term": term,
        "gear_ratio": first_float_after_labels(text, ["ギア"]),
        "leg_type": first_choice(text, ["逃", "捲", "差", "追", "両"]),
        "score": score,
        "win_rate": rates[0] if len(rates) > 0 else None,
        "quinella_rate": rates[1] if len(rates) > 1 else None,
        "trifecta_rate": rates[2] if len(rates) > 2 else None,
        "comment": None,
    }


def parse_detailed_entry_cells(cells: list[str], racer_cell_index: int) -> dict:
    return {
        "score": float_at(cells, racer_cell_index + 2),
        "start_count": int_at(cells, racer_cell_index + 3),
        "home_count": int_at(cells, racer_cell_index + 4),
        "back_count": int_at(cells, racer_cell_index + 5),
        "leg_type": choice_at(cells, racer_cell_index + 6, ["逃", "捲", "差", "追", "両"]),
        "escape_count": int_at(cells, racer_cell_index + 7),
        "makuri_count": int_at(cells, racer_cell_index + 8),
        "sashi_count": int_at(cells, racer_cell_index + 9),
        "mark_count": int_at(cells, racer_cell_index + 10),
        "first_count": int_at(cells, racer_cell_index + 11),
        "second_count": int_at(cells, racer_cell_index + 12),
        "third_count": int_at(cells, racer_cell_index + 13),
        "outside_count": int_at(cells, racer_cell_index + 14),
        "win_rate": float_at(cells, racer_cell_index + 15),
        "quinella_rate": float_at(cells, racer_cell_index + 16),
        "trifecta_rate": float_at(cells, racer_cell_index + 17),
        "gear_ratio": float_at(cells, racer_cell_index + 18),
        "comment": text_at(cells, racer_cell_index + 19),
    }


def entry_car_no(cells: list[str], racer_cell_index: int | None) -> int:
    if racer_cell_index and racer_cell_index > 0 and cells[racer_cell_index - 1].isdigit():
        return int(cells[racer_cell_index - 1])
    return int(cells[0])


def int_at(items: list[str], index: int) -> int | None:
    if index < 0 or index >= len(items):
        return None
    text = items[index].replace(",", "")
    if re.fullmatch(r"\d+", text):
        return int(text)
    return None


def float_at(items: list[str], index: int) -> float | None:
    if index < 0 or index >= len(items):
        return None
    text = items[index].replace(",", "")
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    return None


def text_at(items: list[str], index: int) -> str | None:
    if index < 0 or index >= len(items):
        return None
    return items[index] or None


def choice_at(items: list[str], index: int, choices: list[str]) -> str | None:
    if index < 0 or index >= len(items):
        return None
    return first_choice(items[index], choices)


def first_float_after_labels(text: str, labels: list[str]) -> float | None:
    for label in labels:
        match = re.search(rf"{label}\s*(\d+(?:\.\d+)?)", text)
        if match:
            return float(match.group(1))
    return None


def first_choice(text: str, choices: list[str]) -> str | None:
    for choice in choices:
        if choice in text:
            return choice
    return None


def dedupe_entries(entries: list[dict]) -> list[dict]:
    deduped = {}
    for entry in entries:
        car_no = entry.get("car_no")
        if car_no and 1 <= int(car_no) <= 9:
            deduped[int(car_no)] = entry
    return [deduped[key] for key in sorted(deduped)]


def collect(target_date: str | None = None) -> dict:
    target_date = target_date or default_target_date()
    logging.info("Collecting keirin racecards for %s", target_date)
    links = fetch_racecard_links(target_date)
    logging.info("Found %s racecard links", len(links))
    if not links:
        raise RuntimeError("No racecard links were found in rendered racecard page")

    stats = {"found": len(links), "saved": 0, "skipped": 0, "failed": 0}
    with connect() as conn:
        init_db(conn)
        for link in links:
            try:
                detail_html = fetch_rendered_html(link.url, "racecard_detail_rendered.html", "body")
                race = extract_schedule_meta(detail_html, link.url)
                if race["race_date"] != target_date:
                    stats["skipped"] += 1
                    continue
                entries = extract_entries(detail_html)
                if not entries:
                    raise ValueError("Race entry rows were not found")
                save_schedule(conn, race, entries)
                stats["saved"] += 1
                logging.info("Saved racecard: %s", race["race_id"])
            except Exception:
                stats["failed"] += 1
                logging.exception("Failed to collect racecard: %s", link.url)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect keirin racecards")
    parser.add_argument("--date", help="Target date in YYYY-MM-DD. Default: today")
    args = parser.parse_args()
    setup_logging()
    stats = collect(args.date)
    logging.info("Completed: %s", stats)


if __name__ == "__main__":
    main()
