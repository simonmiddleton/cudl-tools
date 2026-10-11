#!/usr/bin/env python3
"""Build an Apple Maps (MapKit JS) page locating the CUDL aerial photographs.

Fetches the IIIF collection, then every manifest in it, reads the "Origin Place"
coordinates and maps every photo that has a location. With --boundary only the
photos inside a boundary are kept: Cambridgeshire by default, or the first polygon
in a GeoJSON file you supply. The result is written as a single self-contained
HTML page (map template + embedded data).

Manifests are cached in .cache/ so re-runs only fetch new items; pass --refresh
to ignore the cache.

The MapKit JS token comes from --token, else $MAPKIT_TOKEN, else is borrowed from
../geoviewer/geojson_map.html.

Usage: python3 build_aerial_map.py [--boundary [GEOJSON]] [--title TEXT] [--refresh]
                                   [--token JWT] [--out aerial_map.html]
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

COLLECTION_URL = 'https://cudl.lib.cam.ac.uk/iiif/collection/landscapehistories'
CUDL_VIEW_URL = 'https://cudl.lib.cam.ac.uk/view/{id}/1'
# OSM relation 87521 is the ceremonial county (includes Peterborough).
BOUNDARY_URL = ('https://nominatim.openstreetmap.org/lookup?osm_ids=R87521'
                '&format=geojson&polygon_geojson=1&polygon_threshold=0.0005')
USER_AGENT = 'cudl-aerial-map/1.0 (Mill Road History Society)'

HERE = Path(__file__).resolve().parent
CACHE_DIR = HERE / '.cache' / 'manifests'
BOUNDARY_FILE = HERE / 'cambridgeshire.geojson'
TEMPLATE_FILE = HERE / 'aerial_map_template.html'
GEOVIEWER_FILE = HERE.parent / 'geoviewer' / 'geojson_map.html'
DATA_PLACEHOLDER = '/*__PHOTO_DATA__*/null'
TOKEN_PLACEHOLDER = '__MAPKIT_TOKEN__'
TITLE_PLACEHOLDER = '__MAP_TITLE__'

COORD_RE = re.compile(r'(-?\d+(?:\.\d+)?)[\s,]+(-?\d+(?:\.\d+)?)')
DMY_RE = re.compile(r'\b(\d{1,2})/(\d{1,2})/(\d{4})\b')
ISO_RE = re.compile(r'\b(\d{4})-(\d{1,2})-(\d{1,2})\b')
YEAR_RE = re.compile(r'\b(\d{4})\b')


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


def load_collection():
    path = CACHE_DIR.parent / 'collection.json'
    try:
        data = fetch(COLLECTION_URL, retries=2)
        path.write_bytes(data)
    except Exception as e:
        if not path.exists():
            raise
        print(f'Collection fetch failed ({e}); using cached copy', file=sys.stderr)
        data = path.read_bytes()
    return json.loads(data)


def manifest_id(url):
    return url.rstrip('/').rsplit('/', 1)[-1]


def load_manifest(url, refresh):
    path = CACHE_DIR / f'{manifest_id(url)}.json'
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


def parse_date(text):
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


def extract(manifest):
    meta = {m['label']: m['value'] for m in manifest.get('metadata', []) if isinstance(m.get('value'), str)}
    m = COORD_RE.search(meta.get('Origin Place', ''))
    if not m:
        return None
    lat, lng = float(m.group(1)), float(m.group(2))
    item_id = manifest_id(manifest['@id'])
    try:
        service = manifest['sequences'][0]['canvases'][0]['images'][0]['resource']['service']['@id']
        thumb = f'{service}/full/!400,400/0/default.jpg'
    except (KeyError, IndexError, TypeError):
        thumb = None
    sort_key, date = parse_date(meta.get('Date of Creation'))
    return {
        'id': item_id,
        'title': meta.get('Title') or manifest.get('label', item_id),
        'classmark': meta.get('Classmark', ''),
        'date': date,
        'sort': sort_key,
        'lat': round(lat, 6),
        'lng': round(lng, 6),
        'thumb': thumb,
        'url': CUDL_VIEW_URL.format(id=item_id),
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
    ap.add_argument('--boundary', nargs='?', const=str(BOUNDARY_FILE), metavar='GEOJSON',
                    help='only keep photos inside this boundary (Cambridgeshire if no file given)')
    ap.add_argument('--title', help='map heading (default depends on --boundary)')
    ap.add_argument('--refresh', action='store_true', help='re-fetch manifests even if cached')
    ap.add_argument('--out', default=str(HERE / 'aerial_map.html'), help='output HTML file')
    ap.add_argument('--token', help='MapKit JS token (default: $MAPKIT_TOKEN or geoviewer\'s)')
    ap.add_argument('--workers', type=int, default=3, help='parallel manifest downloads')
    args = ap.parse_args()
    token = mapkit_token(args.token)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    boundary = Path(args.boundary).resolve() if args.boundary else None
    polygons = load_boundary(boundary) if boundary else None
    if args.title:
        title = args.title
    elif boundary == BOUNDARY_FILE:
        title = 'Cambridgeshire aerial photographs'
    else:
        title = 'Aerial photographs'

    print(f'Fetching collection {COLLECTION_URL}', file=sys.stderr)
    collection = load_collection()
    urls = [m['@id'] for m in collection.get('manifests', [])]
    print(f'{len(urls)} manifests', file=sys.stderr)

    def work(url):
        try:
            return url, load_manifest(url, args.refresh), None
        except Exception as e:
            return url, None, e

    photos, no_location, outside, failed = [], [], 0, []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for n, (url, manifest, err) in enumerate(pool.map(work, urls), 1):
            if n % 100 == 0:
                print(f'  {n}/{len(urls)}', file=sys.stderr)
            if err:
                failed.append((url, err))
                continue
            photo = extract(manifest)
            if photo is None:
                no_location.append(manifest_id(url))
            elif polygons and not in_boundary(photo['lng'], photo['lat'], polygons):
                outside += 1
            else:
                photos.append(photo)

    photos.sort(key=lambda p: (p['sort'] or '9999', p['id']))
    kept = f'{len(photos)} mapped' + (f', {outside} outside boundary' if polygons else '')
    print(f'{kept}, {len(no_location)} without location, {len(failed)} failed', file=sys.stderr)
    for url, err in failed:
        print(f'  FAILED {url}: {err}', file=sys.stderr)

    data = json.dumps(photos, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
    html = TEMPLATE_FILE.read_text()
    if DATA_PLACEHOLDER not in html:
        sys.exit(f'{TEMPLATE_FILE.name} is missing the {DATA_PLACEHOLDER} placeholder')
    html = (html.replace(DATA_PLACEHOLDER, data)
                .replace(TOKEN_PLACEHOLDER, token)
                .replace(TITLE_PLACEHOLDER, html_escape(title)))
    Path(args.out).write_text(html)
    print(f'Wrote {args.out}', file=sys.stderr)


if __name__ == '__main__':
    main()
