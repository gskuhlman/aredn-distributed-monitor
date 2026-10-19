"""Download an offline base map for the Channel Map tab.

Extracts the area around the mesh from the newest Protomaps daily world
build (OpenStreetMap data, ODbL) into maps/area.pmtiles using the go-pmtiles
CLI (https://github.com/protomaps/go-pmtiles/releases). Only the tiles inside
the bounding box are downloaded, not the whole planet. Rerun it to refresh
the map; the app picks the file up without a restart.

    python fetch_offline_map.py --dry-run          # show area and size only
    python fetch_offline_map.py                    # around the mesh, zoom 0-15
    python fetch_offline_map.py --radius-km 100 --maxzoom 14
    python fetch_offline_map.py --bbox -92.2,41.6,-91.2,42.4
"""
import argparse
import json
import math
import os
import shutil
import sqlite3
import statistics
import subprocess
import sys
import urllib.request

import config

BUILDS_URL = 'https://build-metadata.protomaps.dev/builds.json'
BUILD_BASE = 'https://build.protomaps.com/'


def _haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def mesh_bbox(radius_km, margin_km):
    """Bounding box of located nodes within radius_km of the median node
    position (drops far-away tunnel nodes), plus a margin."""
    conn = sqlite3.connect(f'file:{config.DATABASE_PATH}?mode=ro', uri=True)
    rows = conn.execute('SELECT lat, lon FROM nodes WHERE lat IS NOT NULL AND lon IS NOT NULL').fetchall()
    conn.close()
    points = [(float(la), float(lo)) for la, lo in rows
              if la and lo and -90 <= float(la) <= 90 and -180 <= float(lo) <= 180]
    if not points:
        sys.exit('No located nodes in the database; pass --bbox west,south,east,north.')
    clat = statistics.median(p[0] for p in points)
    clon = statistics.median(p[1] for p in points)
    near = [p for p in points if _haversine_km(clat, clon, *p) <= radius_km]
    dlat = margin_km / 111.0
    dlon = margin_km / (111.0 * math.cos(math.radians(clat)))
    west = min(p[1] for p in near) - dlon
    east = max(p[1] for p in near) + dlon
    south = min(p[0] for p in near) - dlat
    north = max(p[0] for p in near) + dlat
    print(f'{len(near)} of {len(points)} located nodes are within {radius_km:g} km of '
          f'{clat:.4f},{clon:.4f}')
    return west, south, east, north


def latest_build():
    with urllib.request.urlopen(BUILDS_URL, timeout=20) as resp:
        builds = json.load(resp)
    keys = sorted(b['key'] for b in builds if b.get('key', '').endswith('.pmtiles'))
    if not keys:
        sys.exit('No Protomaps builds listed.')
    return BUILD_BASE + keys[-1]


def find_cli():
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.path.join(here, 'tools', 'pmtiles.exe'), os.path.join(here, 'tools', 'pmtiles')):
        if os.path.isfile(candidate):
            return candidate
    return shutil.which('pmtiles')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--bbox', help='west,south,east,north (default: around the mesh)')
    parser.add_argument('--radius-km', type=float, default=60, help='nodes this close to the mesh centre set the area')
    parser.add_argument('--margin-km', type=float, default=15, help='extra border around those nodes')
    parser.add_argument('--maxzoom', type=int, default=15, help='highest zoom level (15 = street detail)')
    parser.add_argument('--source', help='.pmtiles URL or file to extract from (default: newest daily build)')
    parser.add_argument('--dry-run', action='store_true', help='show the area and download size only')
    args = parser.parse_args()

    if args.bbox:
        bbox = tuple(float(v) for v in args.bbox.split(','))
        if len(bbox) != 4:
            sys.exit('--bbox needs west,south,east,north')
    else:
        bbox = mesh_bbox(args.radius_km, args.margin_km)
    west, south, east, north = bbox
    width = _haversine_km(south, west, south, east)
    height = _haversine_km(south, west, north, west)
    print(f'Area: {west:.4f},{south:.4f},{east:.4f},{north:.4f} (about {width:.0f} x {height:.0f} km), '
          f'zoom 0-{args.maxzoom}')

    cli = find_cli()
    if not cli:
        sys.exit('The pmtiles CLI was not found. Download go-pmtiles for your OS from\n'
                 'https://github.com/protomaps/go-pmtiles/releases and put pmtiles(.exe)\n'
                 'in the tools/ folder or on PATH.')
    source = args.source or latest_build()
    os.makedirs(config.OFFLINE_MAP_DIR, exist_ok=True)
    target = os.path.join(config.OFFLINE_MAP_DIR, config.OFFLINE_MAP_FILE)
    temp = target + '.part'
    cmd = [cli, 'extract', source, temp, f'--bbox={west:.5f},{south:.5f},{east:.5f},{north:.5f}',
           f'--maxzoom={args.maxzoom}']
    if args.dry_run:
        cmd.append('--dry-run')
    print('Source:', source)
    result = subprocess.run(cmd)
    if result.returncode:
        sys.exit(result.returncode)
    if not args.dry_run:
        os.replace(temp, target)  # the app keeps serving the old map until now
        print(f'Saved {target} ({os.path.getsize(target) / 1e6:.0f} MB)')


if __name__ == '__main__':
    main()
