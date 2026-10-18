# -*- coding: utf-8 -*-
"""Kyushu 10,000 ft (pressure altitude) ADS-B/MLAT traffic visualizations.

Uses the existing ADSB.lol SQLite positions database built by kyushu_continuity.py.
Does not pretend to identify IFR status or measure unseen/Mode-A-only traffic.
Output KML lines are clamped to the ground. Gaps are linear approximations.
"""
from __future__ import annotations
import argparse
import csv
import math
import sqlite3
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape

import kyushu_continuity as base

JST = timezone(timedelta(hours=9))
LAT0, LAT1, LON0, LON1 = base.LAT_MIN, base.LAT_MAX, base.LON_MIN, base.LON_MAX
MAX_ALT = 10000.0
GRID_NM = 5.0
LAT_PER_NM = 1 / 60.0
LON_PER_NM = 1 / (60.0 * math.cos(math.radians(32.0)))
MAX_CONNECT_SEC = 90 * 60  # prevent connecting separate flights/ground intervals
MAX_SPEED_KT = 850  # break obviously implausible jumps
SLOT_SECONDS = 30 * 60


def point(t, lat, lon, alt, flags=0, icao='', flight='', typ='', src=''):
    return (float(t), float(lat), float(lon), None if alt is None else float(alt), int(flags or 0), icao, flight or '', typ or '', src or '')


def interp(a, b, k):
    k = max(0., min(1., k))
    z = None if a[3] is None or b[3] is None else a[3] + k * (b[3] - a[3])
    return point(a[0] + k*(b[0]-a[0]), a[1]+k*(b[1]-a[1]), a[2]+k*(b[2]-a[2]), z, 0, a[5], a[6], a[7], a[8])


def clip(a, b, time_lo, time_hi, altitude=MAX_ALT):
    """Liang-Barsky clip of linear lon/lat/alt/time segment.

    Does not invent points beyond data endpoints. Returns (start,end) or None.
    """
    if a[3] is None or b[3] is None or b[0] <= a[0]:
        return None
    u0, u1 = 0., 1.
    constraints = ((a[0], b[0], time_lo, time_hi),
                   (a[1], b[1], LAT0, LAT1),
                   (a[2], b[2], LON0, LON1),
                   (a[3], b[3], -1000., altitude))
    for v0, v1, low, high in constraints:
        slope = v1 - v0
        if abs(slope) < 1e-12:
            if v0 < low or v0 > high:
                return None
        else:
            lo, hi = sorted(((low-v0)/slope, (high-v0)/slope))
            u0, u1 = max(u0, lo), min(u1, hi)
            if u0 >= u1 - 1e-12:
                return None
    return interp(a,b,u0), interp(a,b,u1)


def split_leg(previous, current):
    dt = current[0] - previous[0]
    if dt <= 0 or dt > MAX_CONNECT_SEC or (current[4] & 2):
        return True
    # Speed sanity check to avoid impossible joins; not a speed violation test.
    speed = base.haversine_nm(previous[1],previous[2],current[1],current[2])*3600/dt
    if speed > MAX_SPEED_KT:
        return True
    if current[6] and previous[6] and current[6] != previous[6]:
        return True
    return False


def flight_groups(db):
    sql = ('SELECT icao,ts,lat,lon,alt,flags,flight,aircraft_type,source '
           'FROM positions ORDER BY icao,ts')
    icao_old = None
    group = []
    leg_id = 0
    for icao,ts,lat,lon,alt,flags,flight,typ,src in db.execute(sql):
        p = point(ts, lat,lon,alt,flags,icao,flight,typ,src)
        if icao != icao_old:
            if group: yield (icao_old,leg_id,group)
            icao_old, leg_id, group = icao, 0, []
        elif group and split_leg(group[-1], p):
            yield (icao_old,leg_id,group)
            leg_id += 1
            group = []
        group.append(p)
    if group: yield (icao_old,leg_id,group)


def identify_flight(points):
    """Tentative local takeoff/landing based on endpoint altitudes and airport distance.

    'departure', 'arrival', 'both' or 'unknown'; *not* IFR/VFR classification.
    """
    known = [p for p in points if p[3] is not None and p[3] >= -1000]
    if len(known) < 4 or known[-1][0] - known[0][0] < 90:
        return 'unknown', '', ''
    initial, final = known[0], known[-1]
    first_apt, first_d = base.nearest_airport(initial[1], initial[2])
    last_apt, last_d = base.nearest_airport(final[1], final[2])
    end_t = min(initial[0]+15*60, known[-1][0])
    start_t = max(final[0]-15*60, known[0][0])
    post = next((p for p in reversed(known) if p[0] <= end_t), known[-1])
    pre = next((p for p in known if p[0] >= start_t), known[0])
    departed = (first_d <= 12 and -1000 <= initial[3] <= 3500 and
                post[3] - initial[3] >= 600 and
                base.haversine_nm(initial[1],initial[2],post[1],post[2]) >= 2)
    arrived = (last_d <= 12 and -1000 <= final[3] <= 3500 and
               pre[3] - final[3] >= 600 and
               base.haversine_nm(pre[1],pre[2],final[1],final[2]) >= 2)
    if departed and arrived: return 'both', first_apt, last_apt
    if departed: return 'departure', first_apt, ''
    if arrived: return 'arrival', '', last_apt
    return 'unknown', '', ''


def phase_for_segment(kind, mid_t, first_t, last_t):
    if kind == 'both':
        return 'departure' if mid_t < (first_t+last_t)/2 else 'arrival'
    return kind


def kml_header(title, styles):
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
             f'<name>{escape(title)}</name>']
    for name, color, width in styles:
        parts.append(f'<Style id="{name}"><LineStyle><color>{color}</color><width>{width}</width></LineStyle></Style>')
    return ''.join(parts) + '\n'


def placemark(coords, title, description, style):
    if len(coords) < 2:
        return ''
    vertices = ' '.join(f'{lon:.6f},{lat:.6f},0' for lon,lat in coords)
    return (f'<Placemark><name>{escape(title)}</name><description>{escape(description)}</description>'
            f'<styleUrl>#{style}</styleUrl><LineString><tessellate>1</tessellate>'
            f'<altitudeMode>clampToGround</altitudeMode><coordinates>{vertices}</coordinates>'
            '</LineString></Placemark>\n')



def collect_segment(collection, key, a, b):
    """Join adjacent edges without joining over gaps or changing styles."""
    u, v = coords_of(a,b)
    lines = collection.setdefault(key, [])
    if lines and lines[-1][-1] == u:
        lines[-1].append(v)
    else:
        lines.append([u,v])


def multi_placemark(lines, title, description, style):
    parts=[f'<Placemark><name>{escape(title)}</name><description>{escape(description)}</description>',
           f'<styleUrl>#{style}</styleUrl><MultiGeometry>']
    for coords in lines:
        vertices=' '.join(f'{lon:.6f},{lat:.6f},0' for lon,lat in coords)
        parts.append('<LineString><tessellate>1</tessellate><altitudeMode>clampToGround</altitudeMode>'
                     f'<coordinates>{vertices}</coordinates></LineString>')
    parts.append('</MultiGeometry></Placemark>\n')
    return ''.join(parts)

def coords_of(a,b):
    return [(a[2],a[1]),(b[2],b[1])]


def grid_index(p):
    x = int(math.floor((p[2]-LON0)/LON_PER_NM/GRID_NM))
    y = int(math.floor((p[1]-LAT0)/LAT_PER_NM/GRID_NM))
    return x,y


def grid_sample(a,b,flight_id,category,grid,seen,slot_start,slot_end):
    # Sampling 30 s / 1 NM or finer avoids sample-density and gap-size artifacts.
    dist = base.haversine_nm(a[1],a[2],b[1],b[2])
    steps=max(1,min(12000,math.ceil(max((b[0]-a[0])/30,dist/1))))
    for i in range(steps):
        p=interp(a,b,(i+0.5)/steps)
        cell=grid_index(p)
        if cell[0] < 0 or cell[1] < 0:
            continue
        key=(cell, category)
        if (flight_id, key) not in seen:
            seen.add((flight_id,key))
            grid[key]['flights'] += 1
        # Accumulate flight-minutes, not the number of raw ADS-B points.
        grid[key]['minutes'] += (b[0]-a[0]) / 60/steps


def write_density(grid, outdir, date_str):
    entries = sorted(((cell[0],cell[1],mode,int(v['flights']),round(v['minutes'],2))
                     for (cell,mode),v in grid.items() if v['flights']),
                     key=lambda x:(x[0],x[1],x[2]))
    with (outdir/'grid_5NM_0900_1700.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f)
        w.writerow(['grid_x','grid_y','category','distinct_track_legs','flight_minutes','center_lat','center_lon'])
        for x,y,category,count,minutes in entries:
            w.writerow([x,y,category,count,minutes,round(LAT0+(y+.5)*GRID_NM*LAT_PER_NM,6),
                        round(LON0+(x+.5)*GRID_NM*LON_PER_NM,6)])
    with (outdir/'traffic_density_5NM_0900_1700.kml').open('w',encoding='utf-8') as f:
        f.write(kml_header(f'Kyushu 5NM traffic density 09-17 JST {date_str}',[]))
        colors=['8b42bd42','9942cde9','aa00a5ff','bb0000ff']  # aabbggrr: green / yellow / orange / red
        all_counts=[v['flights'] for (cell,cat),v in grid.items() if cat=='all' and v['flights']]
        all_counts.sort()
        if all_counts:
            cuts=[all_counts[min(len(all_counts)-1,int(len(all_counts)*q))] for q in (.50,.75,.90)]
        else: cuts=[0,0,0]
        for ((x,y),cat),v in sorted(grid.items()):
            if cat != 'all' or not v['flights']: continue
            n=v['flights']; ci=sum(n>cut for cut in cuts)
            lon_a=LON0+x*GRID_NM*LON_PER_NM
            lon_b=lon_a+GRID_NM*LON_PER_NM
            lat_a=LAT0+y*GRID_NM*LAT_PER_NM
            lat_b=lat_a+GRID_NM*LAT_PER_NM
            label=f'{n} tracks / {v["minutes"]:.0f} flight-min'
            f.write(f'<Placemark><name>{label}</name><description>5 NM nominal grid, JST 09-17; all observed tracks below 10000 ft. Quartile-relative color.</description>'
                    f'<Style><LineStyle><color>66111111</color><width>0.5</width></LineStyle><PolyStyle><color>{colors[ci]}</color></PolyStyle></Style>'
                    '<Polygon><tessellate>1</tessellate><altitudeMode>clampToGround</altitudeMode><outerBoundaryIs><LinearRing><coordinates>'
                    f'{lon_a:.6f},{lat_a:.6f},0 {lon_b:.6f},{lat_a:.6f},0 {lon_b:.6f},{lat_b:.6f},0 {lon_a:.6f},{lat_b:.6f},0 {lon_a:.6f},{lat_a:.6f},0'
                    '</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>\n')
        f.write('</Document></kml>')
    return len([1 for (cell,cat),v in grid.items() if cat=='all' and v['flights']])


def render_map(grid,outdir,date_str):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        return
    modes=('all','departure','arrival')
    titles=('All observed tracks', 'Likely local departures', 'Likely local arrivals')
    n_x=math.ceil((LON1-LON0)/LON_PER_NM/GRID_NM)
    n_y=math.ceil((LAT1-LAT0)/LAT_PER_NM/GRID_NM)
    fig,axes=plt.subplots(1,3,figsize=(16,6),sharex=True,sharey=True,layout='constrained')
    for ax,mode,title in zip(axes,modes,titles):
        a=np.full((n_y,n_x),np.nan)
        for ((x,y),m),counts in grid.items():
            if m==mode and 0 <=x<n_x and 0<=y<n_y and counts['flights']:
                a[y,x]=counts['flights']
        im=ax.imshow(a,extent=(LON0,LON0+n_x*GRID_NM*LON_PER_NM,LAT0,LAT0+n_y*GRID_NM*LAT_PER_NM),origin='lower',cmap='YlOrRd',aspect='auto',interpolation='none')
        ax.set_xlim(LON0,LON1);ax.set_ylim(LAT0,LAT1)
        for apt,(lat,lon) in base.AIRPORTS.items():
            ax.plot(lon,lat,'.',color='black',markersize=3)
            if apt.split()[0] in ('RJFF','RJFM','RJFK','RJFT','RJFU','RJFO'):
                ax.annotate(apt.split()[0],(lon,lat),xytext=(2,2),textcoords='offset points',fontsize=7,color='black')
        ax.set_title(title)
        ax.set_xlabel('Longitude (E)')
        fig.colorbar(im,ax=ax,shrink=.65,label='Distinct recorded flight legs / cell')
    axes[0].set_ylabel('Latitude (N)')
    fig.suptitle(f'Kyushu observed traffic below 10,000 ft, 09:00–17:00 JST · {date_str}\n'
                 '5 NM grid | not an airspace risk map; ADS-B/MLAT coverage incomplete',fontsize=12)
    fig.savefig(outdir/'traffic_density_5NM_0900_1700.png',dpi=150)
    plt.close(fig)


def run(db, outdir:Path, date_str:str):
    outdir=Path(outdir); outdir.mkdir(parents=True,exist_ok=True)
    day=datetime.strptime(date_str,'%Y-%m-%d').replace(tzinfo=JST)
    day_lo=day.timestamp(); day_hi=(day+timedelta(days=1)).timestamp()
    slot_lo=(day+timedelta(hours=9)).timestamp()
    slot_hi=(day+timedelta(hours=17)).timestamp()
    types=['departure','arrival','unknown']
    styles_all=[('alt0','ffff6600',2.0),('alt1','ff00ccff',2.0),('alt2','ff0000ff',2.0),('gap','ff00a5ff',2.0)]
    styles_slot=[('departure','ffff6600',2.6),('arrival','ff0000ff',2.6),('unknown','ffaaaaaa',1.7),('gap','ff00a5ff',2.0)]
    all_path=outdir/'all_tracks_below_10000ft.kml'
    bucket_folder=outdir/'30min_0900_1700';bucket_folder.mkdir(exist_ok=True)
    handles={}
    stats=Counter();grid=defaultdict(lambda: {'flights':0,'minutes':0.0})
    seen=set(); slot_tracks=defaultdict(set)
    with all_path.open('w',encoding='utf-8') as allfile:
        allfile.write(kml_header(f'All recorded Kyushu tracks <=10000ft {date_str} JST',styles_all))
        try:
            for i in range(16):
                t=day+timedelta(hours=9,minutes=30*i)
                slot_label=t.strftime('%H%M')+'_'+(t+timedelta(minutes=30)).strftime('%H%M')
                for category in types:
                    path=bucket_folder/f'{slot_label}_{category}.kml'
                    h=path.open('w',encoding='utf-8');h.write(kml_header(f'{date_str} JST {slot_label} {category} <=10000ft',styles_slot))
                    handles[(i,category)]=h
            for icao,leg,points in flight_groups(db):
                stats['legs_all']+=1
                if len(points)<2:continue
                phase,dep_apt,arr_apt=identify_flight(points)
                ident=f'{icao}-{leg}'
                had_low=False
                first_t,last_t=points[0][0],points[-1][0]
                all_routes={}
                slot_routes={}
                for a,b in zip(points,points[1:]):
                    if split_leg(a,b): continue
                    ab=clip(a,b,day_lo,day_hi)
                    if not ab:continue
                    had_low=True
                    dt=b[0]-a[0]
                    mid_alt=(ab[0][3]+ab[1][3])/2
                    if dt>60:style='gap';stats['interpolated_edges']+=1
                    else:style='alt0' if mid_alt<=3000 else 'alt1' if mid_alt<=6000 else 'alt2'
                    descr=(f'icao={icao}; flight={a[6] or b[6]}; type={a[7]}; source={a[8]}; '
                           f'original interval={int(dt)} seconds; straight-line interpolation={dt>60}; '
                           f'phase={phase}; origin={dep_apt}; destination={arr_apt}')
                    collect_segment(all_routes,style,*ab)
                    stats['all_kml_segments']+=1
                    # Clip independently per 30-min bin: flights crossing a slot boundary
                    # are split at exact UTC timestamps and not duplicated as full tracks.
                    lo=max(ab[0][0],slot_lo);hi=min(ab[1][0],slot_hi)
                    if hi <= lo: continue
                    i0=max(0,int((lo-slot_lo)//SLOT_SECONDS))
                    i1=min(15,int(math.ceil((hi-slot_lo)/SLOT_SECONDS))-1)
                    for i in range(i0,i1+1):
                        st=slot_lo+i*SLOT_SECONDS
                        cut=clip(ab[0],ab[1],st,st+SLOT_SECONDS)
                        if not cut:continue
                        c=phase_for_segment(phase,(cut[0][0]+cut[1][0])/2,first_t,last_t)
                        collect_segment(slot_routes,(i,c,'gap' if dt>60 else c),*cut)
                        stats['time_kml_segments']+=1
                        slot_tracks[(i,c)].add(ident)
                        # Grid: all traffic + a category-specific layer, independent
                        # of raw ADS-B sample frequency.
                        grid_sample(*cut,ident,'all',grid,seen,slot_lo,slot_hi)
                        grid_sample(*cut,ident,c,grid,seen,slot_lo,slot_hi)
                metadata=(f'icao={icao}; callsign={points[0][6]}; type={points[0][7]}; '
                          f'phase={phase}; possible origin={dep_apt}; possible destination={arr_apt}')
                for style,lines in all_routes.items():
                    allfile.write(multi_placemark(lines,ident,metadata+'; straight-line interpolation='+str(style=='gap'),style))
                for (i,c,style),lines in slot_routes.items():
                    handles[(i,c)].write(multi_placemark(lines,ident,metadata+'; straight-line interpolation='+str(style=='gap'),style))
                if had_low:
                    stats['legs_with_low_altitude']+=1
                    stats['phase_'+phase]+=1
        finally:
            for h in handles.values():
                h.write('</Document></kml>');h.close()
            allfile.write('</Document></kml>')
    with (outdir/'time_slot_counts_0900_1700.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f);w.writerow(['start_JST','end_JST','category','distinct_flight_legs'])
        for i in range(16):
            t=day+timedelta(hours=9,minutes=30*i)
            for c in types:
                w.writerow([t.strftime('%H:%M'),(t+timedelta(minutes=30)).strftime('%H:%M'),c,len(slot_tracks[(i,c)])])
    nonempty=write_density(grid,outdir,date_str)
    render_map(grid,outdir,date_str)
    with (outdir/'traffic_readme.txt').open('w',encoding='utf-8') as f:
        f.write(f'Date JST: {date_str}\nBounds: lat {LAT0}-{LAT1}, lon {LON0}-{LON1}\n')
        f.write('Observed ADS-B/MLAT traces only. NOT all IFR flights. NOT airspace closures or collision-risk estimate.\n')
        f.write('Altitude is barometric ft (may differ from QNH indicated altitude). Ground-clamped KML.\n')
        f.write('All tracks are ground-projected, broken at the day/geographic/10,000-ft boundaries.\n')
        f.write('Record gaps <=90 min within the same inferred flight leg are straight-line interpolated, orange.\n')
        f.write('Gaps >90 min, new flight-leg flags, speed >850 kt, and callsign changes are NOT connected.\n')
        f.write('Departures/arrivals are APPROXIMATIONS based on local-airport proximity, altitude trends,\n')
        f.write('and observed track endpoints. Missing coverage can cause misclassification.\n')
        f.write('Flights with neither verified endpoint are in *_unknown.kml; both local endpoints split at midtrack.\n')
        f.write('30-minute slots contain just the observed/interpolated segments WITHIN that slot (not the full flight).\n')
        f.write('5NM KML grid colors show relative percentile of distinct recorded legs for 09-17 JST;\n')
        f.write('separate CSV contains absolute flights and approximate flight-minutes.\n')
        f.write('Source: ADSB.lol globe_history readsb archive; observe ODbL terms before sharing.\n\n')
        for key,val in sorted(stats.items()):f.write(f'{key}: {val}\n')
        f.write(f'nonempty_5nm_grid_cells: {nonempty}\n')
    # Archive separately; ZIP/KMZ remain compact even when 32 source KMLs are large.
    zip_path=outdir/'kyushu_traffic_kml_bundle.zip'
    with zipfile.ZipFile(zip_path,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as z:
        for path in [all_path,outdir/'traffic_density_5NM_0900_1700.kml',
                     outdir/'grid_5NM_0900_1700.csv',outdir/'time_slot_counts_0900_1700.csv',
                     outdir/'traffic_readme.txt']+sorted(bucket_folder.glob('*.kml')):
            z.write(path,arcname=str(path.relative_to(outdir)))
    print('Kyushu traffic export:',dict(stats),'grid_cells=',nonempty,'zip=',zip_path.stat().st_size,flush=True)
    return stats


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--db',default='results/positions.sqlite')
    parser.add_argument('--output',default='results')
    parser.add_argument('--date',default='2026-10-17')
    args=parser.parse_args()
    with sqlite3.connect(args.db) as db:
        run(db,Path(args.output),args.date)

if __name__=='__main__':main()