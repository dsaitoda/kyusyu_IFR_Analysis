"""One self-contained Google Earth Web KML: density, full day, 30-minute departure/arrival/unknown folders."""
import argparse
import math
import re
from collections import Counter
from pathlib import Path
from xml.sax.saxutils import escape

NAMES=(('alt0','ffff6600'),('alt1','ff00ccff'),('alt2','ff0000ff'),('alt3','ffb6599b'),('gap','ff00a5ff'))
COORDS=re.compile(r'(<coordinates>)([^<]+)(</coordinates>)')

def simplify(pts, tolerance=0.08):
    if len(pts)<3:return pts
    factor=60*math.cos(math.radians(sum(y for x,y in pts)/len(pts)))
    xy=[(x*factor,y*60) for x,y in pts]
    keep={0,len(xy)-1};stack=[(0,len(xy)-1)]
    while stack:
        left,right=stack.pop()
        x,y=xy[left];a,b=xy[right]
        dx,dy=a-x,b-y;den=dx*dx+dy*dy
        far,index=0,None
        for j in range(left+1,right):
            cx,cy=xy[j]
            t=max(0,min(1,((cx-x)*dx+(cy-y)*dy)/den)) if den else 0
            distance=(cx-x-t*dx)**2+(cy-y-t*dy)**2
            if distance>far:far,index=distance,j
        if index is not None and far>tolerance*tolerance:
            keep.add(index);stack.extend(((left,index),(index,right)))
    return [pts[k] for k in sorted(keep)]

def placemarks(path):
    with path.open(encoding='utf-8') as f:
        for row in f:
            row=row.strip()
            if row.startswith('<Placemark>'):yield row

def compact(row, stats, tolerance, is_polygon=False):
    if is_polygon:
        stats['placemarks']+=1
        return row
    def change(match):
        words=match.group(2).split()
        pts=[tuple(map(float,s.split(',')[:2])) for s in words]
        if len(pts)<2:return match.group(0)
        stats['vertices_before']+=len(pts)
        pts=simplify(pts,tolerance)
        stats['vertices_after']+=len(pts)
        return match.group(1)+' '.join(f'{lon:.5f},{lat:.5f},0' for lon,lat in pts)+match.group(3)
    stats['placemarks']+=1
    return COORDS.sub(change,row)

def merge(folder, date, tolerance):
    folder=Path(folder)
    target=folder/f'kyushu_web_onefile_{date}.kml'
    count=Counter()
    with target.open('w',encoding='utf-8') as out:
        out.write('<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="http://www.opengis.net/kml/2.2"><Document>')
        out.write(f'<name>九州航空交通 {escape(date)} JST</name>')
        out.write('<description>Observed ADS-B/MLAT; 13,000ft pressure altitude or lower; flight phases inferred; gap lines straight.</description>')
        for name,color in NAMES:
            out.write(f'<Style id="{name}"><LineStyle><color>{color}</color><width>2</width></LineStyle></Style>')
        def start(name,visible=False):
            out.write(f'<Folder><name>{escape(name)}</name><visibility>{int(visible)}</visibility>')
        start('01 交通密度5NM 09:00-17:00',True)
        for p in placemarks(folder/'traffic_density_5NM_0900_1700.kml'):
            out.write(compact(p,count,tolerance,True))
        out.write('</Folder>')
        start('02 全航跡・高度別（00:00-24:00、13,000ft以下）')
        for p in placemarks(folder/'all_tracks_below_13000ft.kml'):
            out.write(compact(p,count,tolerance))
        out.write('</Folder>')
        start('03 30分別の航跡 09:00-17:00')
        for i in range(16):
            a=540+i*30;b=a+30
            window=f'{a//60:02d}{a%60:02d}_{b//60:02d}{b%60:02d}'
            start(f'{a//60:02d}:{a%60:02d} - {b//60:02d}:{b%60:02d}')
            for category,label in [('departure','出発機（推定）'),('arrival','到着機（推定）'),('unknown','分類不明・通過')]:
                start(label)
                for p in placemarks(folder/'30min_0900_1700'/f'{window}_{category}.kml'):
                    # Source KML is generated with alt0/alt1/alt2/gap styles.
                    out.write(compact(p,count,tolerance))
                out.write('</Folder>')
            out.write('</Folder>')
        out.write('</Folder></Document></kml>')
    report=(f'Google Earth Web unified KML: {target.name}\n'
            f'Size: {target.stat().st_size:,} bytes\nPlacemarks: {count["placemarks"]:,}\n'
            f'Line vertices before: {count["vertices_before"]:,}\n'
            f'Line vertices after: {count["vertices_after"]:,}\n'
            f'Douglas-Peucker tolerance: {tolerance} NM\n'
            'This does not change Google Earth Web limits. Only one folder should be enabled at once.\n')
    (folder/'web_kml_stats.txt').write_text(report,encoding='utf-8')
    print(report)

if __name__=='__main__':
    ap=argparse.ArgumentParser()
    ap.add_argument('--output',default='results')
    ap.add_argument('--date',default='2026-10-17')
    ap.add_argument('--tolerance-nm',type=float,default=0.08)
    a=ap.parse_args()
    merge(a.output,a.date,a.tolerance_nm)
