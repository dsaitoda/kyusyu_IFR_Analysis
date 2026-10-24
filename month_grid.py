# -*- coding: utf-8 -*-
"""Monthly Kyushu aircraft-density pipeline.

- prepare: calendar month beginning at the requested JST date, exclusive next-month anniversary
- daily: per-day unique observed flight legs per 2.5-NM cell, 1000-ft band, JST hour
- merge: one KMZ with raster overlays, CSV, and a small vector KML overview

NO assumption that observed ADS-B/MLAT equals total IFR traffic.
"""
from __future__ import annotations

import argparse
import base64
import csv
import gzip
import json
import math
import os
import sqlite3
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, date, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

import kyushu_traffic as tr

STEP_NM = 2.5
ALT_STEP = 1000
ALT_COUNT = 13
CATEGORIES = ('departure', 'arrival', 'unknown')
LAT0, LAT1, LON0, LON1 = tr.LAT0, tr.LAT1, tr.LON0, tr.LON1
LAT_STEP = STEP_NM / 60
LON_STEP = STEP_NM / (60 * math.cos(math.radians(32.0)))
NX = math.ceil((LON1 - LON0) / LON_STEP)
NY = math.ceil((LAT1 - LAT0) / LAT_STEP)
METRICS = ('passes', 'unique_aircraft')


def month_end(start):
    """One calendar month, start inclusive and anniversary exclusive.

    For a start on the 29/30/31st, use the last day of the next month if needed.
    """
    year, month = start.year + (start.month == 12), (start.month % 12) + 1
    next_next = date(year + (month == 12), (month % 12) + 1, 1)
    last = (next_next - timedelta(days=1)).day
    return date(year, month, min(start.day, last))


def interval(start_text):
    start = date.fromisoformat(start_text)
    end = month_end(start)
    if end > datetime.now(tr.JST).date():
        raise ValueError(f'Complete month not available yet: {start} until {end} (exclusive, JST)')
    dates = []
    d = start
    while d < end:
        dates.append(d.isoformat())
        d += timedelta(days=1)
    return end.isoformat(), dates


def prepare(start_text, github_output=None):
    end, days = interval(start_text)
    print(f'JST month [{start_text}, {end}) = {len(days)} days')
    payload = dict(start=start_text, end=end, dates=days)
    if github_output:
        with open(github_output, 'a', encoding='utf-8') as out:
            out.write('dates=' + json.dumps(days, separators=(',', ':')) + '\n')
            out.write('end=' + end + '\n')
    return payload


def cell_xy(lon, lat):
    x = math.floor((lon - LON0) / LON_STEP)
    y = math.floor((lat - LAT0) / LAT_STEP)
    if 0 <= x < NX and 0 <= y < NY:
        return x, y
    return None


def sample_segment(a, b):
    """Segment in local day and bounding region, 0-13000 ft.

    Every <=0.35 NM or <=15 s yields conservative grid-cell coverage
    relative to the original straight-line assumption.
    """
    distance = tr.base.haversine_nm(a[1], a[2], b[1], b[2])
    steps = max(1, math.ceil(max(distance / 0.35, (b[0] - a[0]) / 15)))
    for i in range(steps + 1):
        p = tr.interp(a, b, i / steps)
        cell = cell_xy(p[2], p[1])
        if cell is None or p[3] is None or not 0 <= p[3] <= 13000:
            continue
        band = min(ALT_COUNT - 1, int(p[3] // ALT_STEP))
        hour = datetime.fromtimestamp(p[0], tr.JST).hour
        yield cell, band, hour, p[0]


def daily(db_path, day_text, dest):
    """Save sparse per-day counts. Different legs of same ICAO count separately."""
    d = date.fromisoformat(day_text)
    d0 = datetime(d.year, d.month, d.day, tzinfo=tr.JST).timestamp()
    d1 = d0 + 86400
    seen = defaultdict(set)  # (phase,band,hour,x,y) -> track IDs
    aircraft = defaultdict(set)  # -> ICAO24 unique IDs
    stats = Counter()
    with sqlite3.connect(db_path) as db:
        for icao, leg, points in tr.flight_groups(db):
            if len(points) < 2:
                continue
            phase, origin, dest_apt = tr.identify_flight(points)
            track = f'{icao}:{leg}'
            first, last = points[0][0], points[-1][0]
            has_segment = False
            for a, b in zip(points, points[1:]):
                if tr.split_leg(a, b):
                    continue
                clipped = tr.clip(a, b, d0, d1, altitude=13000)
                if clipped is None:
                    continue
                has_segment = True
                if b[0] - a[0] > 60:
                    stats['interpolated_segments'] += 1
                for (x, y), band, hour, timestamp in sample_segment(*clipped):
                    category = tr.phase_for_segment(phase, timestamp, first, last)
                    for cat in (category, 'all'):
                        for h in (hour, -1):  # -1 = all hours combined
                            key = (cat, band, h, x, y)
                            seen[key].add(track)
                            aircraft[key].add(icao)
            if has_segment:
                stats['flight_legs_with_positions'] += 1
                stats['category_'+phase] += 1
    records = [[cat, band, hour, x, y, len(legs), sorted(aircraft[cat,band,hour,x,y])]
               for (cat, band, hour, x, y), legs in sorted(seen.items())]
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / f'{day_text}.json.gz'
    with gzip.open(target, 'wt', encoding='utf-8') as out:
        json.dump({'date': day_text, 'records': records, 'stats': dict(stats)},
                  out, separators=(',', ':'))
    print(day_text, 'cells', len(records), 'stats', dict(stats), 'size', target.stat().st_size)
    return target


def heat_color(n, maximum):
    """Absolute-count logarithmic legend, RGBA; zero transparent.

    Same color mapping across all layers (fixed monthly max), not per-layer ranking.
    """
    if n <= 0:
        return (0, 0, 0, 0)
    ratio = math.log1p(n) / math.log1p(max(1, maximum))
    stops = [(36, 109, 191), (66, 188, 160), (248, 225, 92), (246, 130, 46), (196, 43, 45)]
    k = min(3, int(ratio * 4))
    f = max(0, min(1, ratio * 4 - k))
    red = round(stops[k][0]*(1-f)+stops[k+1][0]*f)
    green = round(stops[k][1]*(1-f)+stops[k+1][1]*f)
    blue = round(stops[k][2]*(1-f)+stops[k+1][2]*f)
    return (red, green, blue, 173)


def raster_image(counts, maximum):
    from PIL import Image
    pixels = Image.new('RGBA', (NX, NY), (0, 0, 0, 0))
    for (x, y), value in counts.items():
        pixels.putpixel((x, NY-1-y), heat_color(value, maximum))
    return pixels


def overlay(name, filename):
    return ('<GroundOverlay><name>'+escape(name)+'</name><visibility>0</visibility>'
            '<Icon><href>'+escape(filename)+'</href></Icon>'
            f'<LatLonBox><north>{LAT0 + NY*LAT_STEP:.7f}</north>'
            f'<south>{LAT0:.7f}</south>'
            f'<east>{LON0+NX*LON_STEP:.7f}</east>'
            f'<west>{LON0:.7f}</west></LatLonBox></GroundOverlay>')


def kml_folder(name, inner, visible=False):
    return ('<Folder><name>'+escape(name)+'</name><visibility>'
            + ('1' if visible else '0')+'</visibility>'+inner+'</Folder>')


def export_summary_vector(results, maximum, output):
    """Compact single KML overview: only 100 strongest cells per category and 1k band.

    Complete 2.5NM grids (including 24 hourly views) are in the KMZ and CSV.
    """
    kml = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>月間交通密度サマリー</name>',
           '<description>Top 100 grid cells per category and altitude. FULL grid in KMZ/CSV.</description>']
    for cat in CATEGORIES:
        layers = []
        for band in range(ALT_COUNT):
            positions = sorted(
                ((v, x, y) for (c, a, h, x, y), v in results.items()
                 if c == cat and a == band and h == -1),
                reverse=True
            )[:100]
            placemarks=[]
            for n, x, y in positions:
                r,g,b,alpha = heat_color(n, maximum)
                color = f'{alpha:02x}{b:02x}{g:02x}{r:02x}'
                lo_x,hi_x=LON0+x*LON_STEP,LON0+(x+1)*LON_STEP
                lo_y,hi_y=LAT0+y*LAT_STEP,LAT0+(y+1)*LAT_STEP
                coords=f'{lo_x:.6f},{lo_y:.6f},0 {hi_x:.6f},{lo_y:.6f},0 {hi_x:.6f},{hi_y:.6f},0 {lo_x:.6f},{hi_y:.6f},0 {lo_x:.6f},{lo_y:.6f},0'
                placemarks.append(
                    f'<Placemark><name>{n} flight legs</name><Style><LineStyle><color>66000000</color>'
                    f'<width>0.3</width></LineStyle><PolyStyle><color>{color}</color></PolyStyle>'
                    f'</Style><Polygon><outerBoundaryIs><LinearRing><coordinates>{coords}</coordinates>'
                    '</LinearRing></outerBoundaryIs></Polygon></Placemark>'
                )
            layers.append(kml_folder(f'{band*1000}-{(band+1)*1000} ft', ''.join(placemarks)))
        kml.append(kml_folder(cat, ''.join(layers)))
    kml.append('</Document></kml>')
    Path(output).write_text(''.join(kml),encoding='utf-8')


def merge(start_text, partial_dir, output):
    end, expected = interval(start_text)
    partial_dir, output = Path(partial_dir), Path(output)
    output.mkdir(parents=True,exist_ok=True)
    csv_path = output / f'monthly_grid_{start_text}_to_{end}.csv'
    present = {p.stem.replace('.json','') for p in partial_dir.glob('*.json.gz')}
    missing = sorted(set(expected)-present)
    if missing:
        raise RuntimeError('Missing days: '+', '.join(missing))
    counts = Counter()
    icaos = defaultdict(set)
    stats = Counter()
    for day in expected:
        with gzip.open(partial_dir/f'{day}.json.gz', 'rt', encoding='utf-8') as inp:
            record=json.load(inp)
        if record['date']!=day:
            raise ValueError('Unexpected date in daily artifact')
        for cat,band,hour,x,y,legs,ids in record['records']:
            key=(cat,band,hour,x,y)
            counts[key]+=legs
            icaos[key].update(ids)
        stats.update(record.get('stats',{}))
    maximum=max(counts.values(),default=1)
    with csv_path.open('w',encoding='utf-8-sig',newline='') as dest:
        writer=csv.writer(dest)
        writer.writerow(['category','altitude_min_ft','altitude_max_ft','hour_jst','grid_x','grid_y',
                         'center_lat','center_lon','flight_leg_passes','distinct_icao_aircraft'])
        for (cat,band,hour,x,y),n in sorted(counts.items()):
            writer.writerow([cat,band*ALT_STEP,(band+1)*ALT_STEP,
                             'ALL' if hour==-1 else f'{hour:02d}:00-{(hour+1)%24:02d}:00',
                             x,y,round(LAT0+(y+0.5)*LAT_STEP,6),
                             round(LON0+(x+0.5)*LON_STEP,6),n,len(icaos[cat,band,hour,x,y])])
    from PIL import Image
    import io
    output_kmz=output/f'kyushu_monthly_heatmaps_{start_text}_to_{end}.kmz'
    doc=['<?xml version="1.0" encoding="UTF-8"?>',
         '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
         f'<name>九州 2.5NM・1000ft別月間交通量 {start_text}〜{end} JST</name>',
         '<description>Observed ADS-B/MLAT tracks only, IFR/VFR not verified. '
         'Pixel color is an absolute logarithmic scale using same maximum across all layers. '
         'Each grid value counts distinct daily track legs; see CSV for unique ICAO count.</description>']
    picture_count=0
    with zipfile.ZipFile(output_kmz,'w',compression=zipfile.ZIP_DEFLATED,allowZip64=True) as z:
        for cat in ('all',)+CATEGORIES:
            sub=[]
            for hour in [-1]+list(range(24)):
                altitude=[]
                for band in range(ALT_COUNT):
                    values={(x,y):n for (c,b,h,x,y),n in counts.items()
                            if c==cat and b==band and h==hour and n>0}
                    if not values:
                        continue
                    name=f'{cat}_{hour:02d}_{band:02d}.png'
                    buffer=io.BytesIO()
                    raster_image(values,maximum).save(buffer,format='PNG',optimize=True)
                    z.writestr('images/'+name,buffer.getvalue())
                    title=f'{band*1000}-{(band+1)*1000} ft'
                    altitude.append(overlay(title,'images/'+name))
                    picture_count+=1
                if altitude:
                    title='全時間帯' if hour==-1 else f'{hour:02d}:00-{(hour+1)%24:02d}:00 JST'
                    sub.append(kml_folder(title,''.join(altitude)))
            doc.append(kml_folder(cat,''.join(sub)))
        doc.append('</Document></kml>')
        z.writestr('doc.kml',''.join(doc))
    vector=output/f'kyushu_monthly_overview_{start_text}_to_{end}.kml'
    export_summary_vector(counts, maximum, vector)
    info={'start_jst':start_text,'end_exclusive_jst':end,'days':len(expected),
          'nominal_cell_nm':STEP_NM,'altitude_step_ft':ALT_STEP,'altitude_max_ft':13000,
          'hours':24,'grid_rows':len(counts),'raster_layers':picture_count,
          'max_flight_leg_passes_per_cell':maximum,
          'daily_flight_legs_observed_sum':stats['flight_legs_with_positions'],
          'count_definition':'Sum of distinct daily track legs visiting each grid/band/hour; not simultaneous aircraft',
          'unknown_included':True,'stats':dict(stats)}
    (output/'monthly_summary.json').write_text(json.dumps(info,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print(json.dumps(info,ensure_ascii=False))
    return info


def main():
    p=argparse.ArgumentParser()
    sub=p.add_subparsers(dest='operation',required=True)
    pre=sub.add_parser('prepare');pre.add_argument('--start',required=True)
    daily_p=sub.add_parser('daily');daily_p.add_argument('--day',required=True)
    daily_p.add_argument('--db',default='results/positions.sqlite')
    daily_p.add_argument('--out',default='partial')
    merge_p=sub.add_parser('merge');merge_p.add_argument('--start',required=True)
    merge_p.add_argument('--partials',default='partial')
    merge_p.add_argument('--out',default='month_results')
    a=p.parse_args()
    if a.operation=='prepare':
        prepare(a.start,os.environ.get('GITHUB_OUTPUT'))
    elif a.operation=='daily':
        daily(a.db,a.day,a.out)
    else:
        merge(a.start,a.partials,a.out)


if __name__=='__main__':
    main()
