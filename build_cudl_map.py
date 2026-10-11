#!/usr/bin/env python3
"""Build an Apple Maps (MapKit JS) page locating the items in a CUDL IIIF collection.

Two collections are supported:

  aerial  Landscape Histories from the Air. Each photo's position is the
          "Origin Place" metadata, given as "lat lng".
  maps    The Maps collection. Each map's extent is given in its "Note(s)" as a
          rectangle in degrees, e.g. (E 0°06ʹ27ʺ--E 0°08ʹ12ʺ/N 52°11ʹ36ʺ--N 52°11ʹ21ʺ);
          the marker goes at its centre and the rectangle is drawn when the map's
          popup is open.

The script fetches the collection, then every manifest in it, and maps every item
that has a location. With --boundary only items inside a boundary are kept:
Cambridgeshire by default, or the first polygon in a GeoJSON file you supply. The
result is written as a single self-contained HTML page (map template + embedded data).

Manifests are cached in .cache/<collection>/ so re-runs only fetch new items; pass
--refresh to ignore the cache.

The MapKit JS token comes from --token, else $MAPKIT_TOKEN, else is borrowed from
../geoviewer/geojson_map.html.

Usage: python3 build_cudl_map.py [aerial|maps] [--boundary [GEOJSON]] [--title TEXT]
                                 [--refresh] [--token JWT] [--out FILE.html]
"""

import argparse
from html import escape as html_escape
import os
import json
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CUDL_VIEW_URL = 'https://cudl.lib.cam.ac.uk/view/{id}/1'
# OSM relation 87521 is the ceremonial county (includes Peterborough).
BOUNDARY_URL = ('https://nominatim.openstreetmap.org/lookup?osm_ids=R87521'
                '&format=geojson&polygon_geojson=1&polygon_threshold=0.0005')
USER_AGENT = 'cudl-aerial-map/1.0 (Mill Road History Society)'

HERE = Path(__file__).resolve().parent
CACHE_ROOT = HERE / '.cache'
BOUNDARY_FILE = HERE / 'cambridgeshire.geojson'
TEMPLATE_FILE = HERE / 'cudl_map_template.html'
GEOVIEWER_FILE = HERE.parent / 'geoviewer' / 'geojson_map.html'
DATA_PLACEHOLDER = '/*__ITEM_DATA__*/null'
CONFIG_PLACEHOLDER = '/*__MAP_CONFIG__*/null'
TOKEN_PLACEHOLDER = '__MAPKIT_TOKEN__'
TITLE_PLACEHOLDER = '__MAP_TITLE__'

COORD_RE = re.compile(r'(-?\d+(?:\.\d+)?)[\s,]+(-?\d+(?:\.\d+)?)')
DMY_RE = re.compile(r'\b(\d{1,2})/(\d{1,2})/(\d{4})\b')
ISO_RE = re.compile(r'\b(\d{4})-(\d{1,2})-(\d{1,2})\b')
YEAR_RE = re.compile(r'\b(\d{4})\b')
# One degrees/minutes/seconds value with its hemisphere, e.g. "W 0°00ʹ13ʺ".
DMS = r'([NSEW])\s*(\d{1,3})°\s*(?:(\d{1,2})[ʹ\'′]\s*)?(?:(\d{1,2})[ʺ"″]?)?'
RECT_RE = re.compile(rf'{DMS}\s*-+\s*{DMS}\s*/\s*{DMS}\s*-+\s*{DMS}')
# Catalogue shorthand for an unknown last digit(s): '184-' (1840s), '18uu' (1800s).
DECADE_RE = re.compile(r'\b(\d{3})-(?!\d)')
CENTURY_RE = re.compile(r'\b(\d{2})(?:uu|--)(?!\d)')
RANGE_RE = re.compile(r'\b(\d{4})\??\s*(?:-|–|and|to)\s*(\d{2,4})\b')
MONTHS = {m: i for i, m in enumerate(
    ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'], 1)}
MONTH_RE = re.compile(r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?', re.I)
DAY_RE = re.compile(r'\b(\d{1,2})(?:st|nd|rd|th)?\b')


def fetch(url, retries=5):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except Exception as e:
            if attempt == retries - 1:
                raise
            # CUDL sits behind a rate-limiting firewall that answers 403/429 when pushed.
            throttled = isinstance(e, urllib.error.HTTPError) and e.code in (403, 429)
            time.sleep((15 if throttled else 2) * 2 ** attempt)


def load_collection(url, cache_dir):
    path = cache_dir / 'collection.json'
    try:
        data = fetch(url, retries=2)
        path.write_bytes(data)
    except Exception as e:
        if not path.exists():
            raise
        print(f'Collection fetch failed ({e}); using cached copy', file=sys.stderr)
        data = path.read_bytes()
    return json.loads(data)


def manifest_id(url):
    return url.rstrip('/').rsplit('/', 1)[-1]


def load_manifest(url, cache_dir, refresh):
    path = cache_dir / f'{manifest_id(url)}.json'
    if path.exists() and not refresh:
        return json.loads(path.read_text())
    data = fetch(url.replace('http://', 'https://', 1))
    path.write_bytes(data)
    return json.loads(data)


def load_boundary(path):
    if path == BOUNDARY_FILE and not path.exists():
        print('Fetching Cambridgeshire boundary…', file=sys.stderr)
        path.write_bytes(fetch(BOUNDARY_URL))
    geojson = json.loads(path.read_text())
    if geojson.get('type') == 'FeatureCollection':
        geojson = geojson['features'][0]
    geom = geojson.get('geometry', geojson)
    # Normalise to a list of polygons, each a list of rings of [lng, lat].
    return geom['coordinates'] if geom['type'] == 'MultiPolygon' else [geom['coordinates']]


def in_ring(lng, lat, ring):
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lng < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def in_boundary(lng, lat, polygons):
    for rings in polygons:
        if in_ring(lng, lat, rings[0]) and not any(in_ring(lng, lat, hole) for hole in rings[1:]):
            return True
    return False


# --- Dates -------------------------------------------------------------------

def parse_photo_date(text):
    """Return (sort key, display text) for '05/07/1973 p.m.', '1973-07-05 a.m.' or just '1946'.

    The sort key is 'YYYY-MM-DD' plus 'a'/'p' for morning/afternoon, '' when there is no year.
    """
    text = (text or '').strip()
    ampm = 'a' if 'a.m.' in text else 'p' if 'p.m.' in text else ''
    suffix = ' a.m.' if ampm == 'a' else ' p.m.' if ampm == 'p' else ''
    if m := DMY_RE.search(text):
        day, month, year = m.groups()
    elif m := ISO_RE.search(text):
        year, month, day = m.groups()
    elif m := YEAR_RE.search(text):
        return m.group(1), text
    else:
        return '', text
    day, month = int(day), int(month)
    return f'{year}-{month:02d}-{day:02d}{ampm}', f'{day:02d}/{month:02d}/{year}{suffix}'


def parse_vague_date(text):
    """Return (sort key, display text) for catalogue dates such as '[1900?]',
    '[between 1850 and 1860?]', '1820-1830', 'Oct[ober] 12[th] [18]52', '[184-?]'.

    Square brackets mark text supplied by the cataloguer; they are dropped from the
    display, which keeps any '?' and words like 'approximately'. Ranges sort by their
    first year; the key is 'YYYY-MM-DD' with 00 for an unknown month or day, followed
    by the end year of a range so that e.g. 1850 sorts before 1850–1870.
    """
    display = re.sub(r'\s+', ' ', re.sub(r'[\[\]]', '', text or '')).strip(' .,;')
    if not display:
        return '', ''
    if m := DECADE_RE.search(display):
        start = m.group(1) + '0'
        display = DECADE_RE.sub(lambda m: f'{m.group(1)}0s', display, count=1)
        return f'{start}-00-00~{m.group(1)}9', display
    if m := CENTURY_RE.search(display):
        start = m.group(1) + '00'
        display = CENTURY_RE.sub(lambda m: f'{m.group(1)}00s', display, count=1)
        return f'{start}-00-00~{m.group(1)}99', display
    if m := RANGE_RE.search(display):
        start, end = m.group(1), m.group(2)
        if len(end) < 4:                     # '1850-60'
            end = start[:4 - len(end)] + end
        return f'{start}-00-00~{end}', display
    if m := DMY_RE.search(display):
        day, month, year = m.groups()
        return f'{year}-{int(month):02d}-{int(day):02d}', display
    m = YEAR_RE.search(display)
    if not m:
        return '', display
    year = m.group(1)
    month = day = 0
    if mm := MONTH_RE.search(display):
        month = MONTHS[mm.group(1).lower()]
        # A day is a 1-2 digit number next to the month, not part of the year.
        rest = re.sub(r'([a-z])(\d)', r'\1 \2', display.replace(year, ' '), flags=re.I)
        if dm := DAY_RE.search(rest):
            if 1 <= int(dm.group(1)) <= 31:
                day = int(dm.group(1))
    return f'{year}-{month:02d}-{day:02d}', display


# --- Locations ---------------------------------------------------------------

def dms_to_degrees(hemi, deg, mins, secs):
    value = int(deg) + int(mins or 0) / 60 + int(secs or 0) / 3600
    return -value if hemi in 'SW' else value


def parse_rectangles(text):
    """Return [west, south, east, north] covering every rectangle in the text, or None."""
    boxes = []
    for m in RECT_RE.finditer(text or ''):
        g = m.groups()
        lng1, lng2 = dms_to_degrees(*g[0:4]), dms_to_degrees(*g[4:8])
        lat1, lat2 = dms_to_degrees(*g[8:12]), dms_to_degrees(*g[12:16])
        boxes.append((min(lng1, lng2), min(lat1, lat2), max(lng1, lng2), max(lat1, lat2)))
    if not boxes:
        return None
    return [round(min(b[0] for b in boxes), 6), round(min(b[1] for b in boxes), 6),
            round(max(b[2] for b in boxes), 6), round(max(b[3] for b in boxes), 6)]


# --- Manifests ---------------------------------------------------------------

def metadata(manifest):
    return {m['label']: m['value'] for m in manifest.get('metadata', []) if isinstance(m.get('value'), str)}


def base_item(manifest, meta):
    item_id = manifest_id(manifest['@id'])
    try:
        service = manifest['sequences'][0]['canvases'][0]['images'][0]['resource']['service']['@id']
        thumb = f'{service}/full/!400,400/0/default.jpg'
    except (KeyError, IndexError, TypeError):
        thumb = None
    return {
        'id': item_id,
        'title': meta.get('Title') or manifest.get('label', item_id),
        'classmark': meta.get('Classmark', ''),
        'thumb': thumb,
        'url': CUDL_VIEW_URL.format(id=item_id),
    }


def extract_photo(manifest):
    meta = metadata(manifest)
    m = COORD_RE.search(meta.get('Origin Place', ''))
    if not m:
        return None
    sort_key, date = parse_photo_date(meta.get('Date of Creation'))
    return {**base_item(manifest, meta), 'date': date, 'sort': sort_key,
            'lat': round(float(m.group(1)), 6), 'lng': round(float(m.group(2)), 6)}


def extract_map(manifest):
    meta = metadata(manifest)
    bbox = parse_rectangles(meta.get('Note(s)'))
    if not bbox:
        return None
    w, s, e, n = bbox
    sort_key, date = parse_vague_date(meta.get('Date of Creation') or meta.get('Date of Publication'))
    return {**base_item(manifest, meta), 'date': date, 'sort': sort_key,
            'lat': round((s + n) / 2, 6), 'lng': round((w + e) / 2, 6), 'bbox': bbox}


COLLECTIONS = {
    'aerial': {
        'url': 'https://cudl.lib.cam.ac.uk/iiif/collection/landscapehistories',
        'cache': 'landscapehistories',
        'extract': extract_photo,
        'out': 'aerial_map.html',
        'title': 'Aerial photographs',
        'config': {
            'collectionName': 'Landscape Histories from the Air',
            'collectionUrl': 'https://cudl.lib.cam.ac.uk/collections/landscapehistories/1',
            'noun': 'photo', 'nounPlural': 'photos',
        },
    },
    'maps': {
        'url': 'https://cudl.lib.cam.ac.uk/iiif/collection/maps',
        'cache': 'maps',
        'extract': extract_map,
        'out': 'maps_map.html',
        'title': 'Maps',
        'config': {
            'collectionName': 'Maps',
            'collectionUrl': 'https://cudl.lib.cam.ac.uk/collections/maps/1',
            'noun': 'map', 'nounPlural': 'maps',
        },
    },
}


def mapkit_token(arg):
    token = arg or os.environ.get('MAPKIT_TOKEN')
    if not token and GEOVIEWER_FILE.exists():
        m = re.search(r"APPLE_MAPS_TOKEN = '([^']+)'", GEOVIEWER_FILE.read_text())
        token = m and m.group(1)
    if not token:
        sys.exit('No MapKit JS token: pass --token or set MAPKIT_TOKEN')
    return token


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('collection', nargs='?', choices=COLLECTIONS, default='aerial',
                    help='which CUDL collection to map (default: aerial)')
    ap.add_argument('--boundary', nargs='?', const=str(BOUNDARY_FILE), metavar='GEOJSON',
                    help='only keep items inside this boundary (Cambridgeshire if no file given)')
    ap.add_argument('--title', help='map heading (default depends on collection and --boundary)')
    ap.add_argument('--refresh', action='store_true', help='re-fetch manifests even if cached')
    ap.add_argument('--out', help='output HTML file (default: aerial_map.html or maps_map.html)')
    ap.add_argument('--token', help='MapKit JS token (default: $MAPKIT_TOKEN or geoviewer\'s)')
    ap.add_argument('--workers', type=int, default=3, help='parallel manifest downloads')
    args = ap.parse_args()
    spec = COLLECTIONS[args.collection]
    token = mapkit_token(args.token)
    out = Path(args.out) if args.out else HERE / spec['out']

    cache_dir = CACHE_ROOT / spec['cache']
    manifest_dir = cache_dir / 'manifests'
    manifest_dir.mkdir(parents=True, exist_ok=True)
    boundary = Path(args.boundary).resolve() if args.boundary else None
    polygons = load_boundary(boundary) if boundary else None
    if args.title:
        title = args.title
    elif boundary == BOUNDARY_FILE:
        title = f'Cambridgeshire {spec["title"].lower()}'
    else:
        title = spec['title']

    print(f'Fetching collection {spec["url"]}', file=sys.stderr)
    collection = load_collection(spec['url'], cache_dir)
    urls = [m['@id'] for m in collection.get('manifests', [])]
    print(f'{len(urls)} manifests', file=sys.stderr)

    def work(url):
        try:
            return url, load_manifest(url, manifest_dir, args.refresh), None
        except Exception as e:
            return url, None, e

    items, no_location, outside, failed = [], [], 0, []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for n, (url, manifest, err) in enumerate(pool.map(work, urls), 1):
            if n % 100 == 0:
                print(f'  {n}/{len(urls)}', file=sys.stderr)
            if err:
                failed.append((url, err))
                continue
            item = spec['extract'](manifest)
            if item is None:
                no_location.append(manifest_id(url))
            elif polygons and not in_boundary(item['lng'], item['lat'], polygons):
                outside += 1
            else:
                items.append(item)

    items.sort(key=lambda p: (p['sort'] or '9999', p['id']))
    kept = f'{len(items)} mapped' + (f', {outside} outside boundary' if polygons else '')
    print(f'{kept}, {len(no_location)} without location, {len(failed)} failed', file=sys.stderr)
    for url, err in failed:
        print(f'  FAILED {url}: {err}', file=sys.stderr)

    def js(value):
        return json.dumps(value, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')

    html = TEMPLATE_FILE.read_text()
    for placeholder in (DATA_PLACEHOLDER, CONFIG_PLACEHOLDER):
        if placeholder not in html:
            sys.exit(f'{TEMPLATE_FILE.name} is missing the {placeholder} placeholder')
    html = (html.replace(DATA_PLACEHOLDER, js(items))
                .replace(CONFIG_PLACEHOLDER, js(spec['config']))
                .replace(TOKEN_PLACEHOLDER, token)
                .replace(TITLE_PLACEHOLDER, html_escape(title)))
    out.write_text(html)
    print(f'Wrote {out}', file=sys.stderr)


if __name__ == '__main__':
    main()
