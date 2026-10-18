import csv, sqlite3, tempfile, zipfile, xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from kyushu_traffic import clip, point, run, JST

with tempfile.TemporaryDirectory() as folder:
    out=Path(folder)
    db=sqlite3.connect(out/'points.sqlite')
    db.execute('CREATE TABLE positions(icao TEXT NOT NULL, ts INTEGER NOT NULL, lat REAL, lon REAL, alt REAL, flags INTEGER, flight TEXT, aircraft_type TEXT, source TEXT, PRIMARY KEY(icao,ts))')
    anchor=datetime(2026,10,17,9,0,tzinfo=JST).timestamp()
    rows=[('abc000',int(anchor+dt),lat,lon,alt,0,'TEST01','C172','adsb_icao')
          for dt,lat,lon,alt in ((0,31.8772,131.4486,30),(180,31.90,131.46,900),(900,32.10,131.65,6000),(2100,32.45,131.98,11000))]
    rows += [('abc001',int(anchor+dt),lat,lon,alt,0,'TEST02','B738','mlat')
             for dt,lat,lon,alt in ((0,32.3,131.1,9800),(120,32.1,130.9,7000),(900,31.9,130.75,3000),(1500,31.8034,130.7194,300))]
    rows += [('abc002',int(anchor+dt),33.,131.,alt,0,'TEST03','A320','adsb_icao')
             for dt,alt in ((0,20000),(200,19000))]
    db.executemany('INSERT INTO positions VALUES (?,?,?,?,?,?,?,?,?)',rows)
    stats=run(db,out,'2026-10-17')
    for file in out.glob('**/*.kml'): ET.parse(file)
    paths=list((out/'30min_0900_1700').glob('*.kml'))
    assert len(paths)==48,len(paths)
    assert stats['legs_with_low_altitude']==2,stats
    assert stats['interpolated_edges']>=2,stats
    assert 'straight-line interpolation=True' in (out/'all_tracks_below_10000ft.kml').read_text()
    assert 'abc002' not in (out/'all_tracks_below_10000ft.kml').read_text()
    counts=list(csv.DictReader((out/'time_slot_counts_0900_1700.csv').open(encoding='utf-8-sig')))
    assert any(int(c['distinct_flight_legs'])>0 and c['category']=='departure' for c in counts),counts
    assert any(int(c['distinct_flight_legs'])>0 and c['category']=='arrival' for c in counts),counts
    assert zipfile.ZipFile(out/'kyushu_traffic_kml_bundle.zip').testzip() is None
    assert clip(point(anchor,32.,131.,9000),point(anchor+900,32.,131.3,11000),anchor,anchor+900)[1][3]==10000
print('All Kyushu traffic export synthetic tests passed')
