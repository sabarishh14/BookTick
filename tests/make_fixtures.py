"""Save trimmed copies of real District pages for the parser tests.

Run it again when District changes its pages and the live test fails:
    python tests/make_fixtures.py

It keeps District's real structure but trims long lists and strings, so the files
stay small enough to keep in the repo.
"""
import json
import os
import re
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from booktick import district  # noqa: E402
from booktick.core import now_ist  # noqa: E402

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
CINEMA_URL = "https://www.district.in/movies/inox-phoenix-market-city-formerly-jazz-cinemas-velachery-chennai-CD1020779"
CITY = "chennai"
MAX_ITEMS = 12
MAX_TEXT = 120
# Big parts of the pages the parsers never read.
DROP_KEYS = {"footerSeoData", "seo", "seoData", "spotlight_videos", "video_data", "description", "reason_to_watch",
             "filterBuckets", "amenities", "cinemaMeta", "preSeatSelectBottomSheet", "mandatoryItems", "tags",
             "pageMeta", "emojisMap", "languagePreferenceMap", "initialState"}


def trim(node, depth=0):
    if isinstance(node, dict):
        return {k: trim(v, depth + 1) for k, v in node.items() if k not in DROP_KEYS}
    if isinstance(node, list):
        return [trim(v, depth + 1) for v in node[:MAX_ITEMS]]
    if isinstance(node, str) and len(node) > MAX_TEXT:
        return node[:MAX_TEXT]
    return node


def save(name, page, ld_blocks=5):
    data = trim(district.next_data(page))
    ld = re.findall(r'<script type="application/ld\+json"[^>]*>.*?</script>', page, re.DOTALL)[:ld_blocks]
    html = (f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data, ensure_ascii=False)}</script>\n'
            + "\n".join(ld) + "\n")
    path = os.path.join(HERE, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"{name}: {len(html) // 1024} KB")


def main():
    os.makedirs(HERE, exist_ok=True)
    date = str((now_ist() + timedelta(days=1)).date())
    save("cinema_page.html", district.fetch_html(district.query_url(CINEMA_URL, date)))
    city_page = district.fetch_html(f"https://www.district.in/movies/cinemas-in-{CITY}")
    save("city_page.html", city_page, ld_blocks=0)
    movie = district.parse_city_page(city_page)["movies"][0]
    save("city_movie_page.html", district.fetch_html(district.city_movie_url(movie["url"], CITY, date)), ld_blocks=0)
    with open(os.path.join(HERE, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"date": date, "cinema_url": CINEMA_URL, "city": CITY, "movie": movie}, f, indent=2)


if __name__ == "__main__":
    main()
