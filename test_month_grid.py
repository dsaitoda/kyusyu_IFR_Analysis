import csv
import gzip
import json
import sqlite3
import tempfile
from datetime import datetime, date
from pathlib import Path
from month_grid import daily, month_end, cell_xy, sample_segment, interval
import kyushu_traffic as traffic

assert month_end(date(2026, 9, 1)) == date(2026, 10, 1)
assert month_end(date(2026, 1, 31)) == date(2026, 2, 28)
assert len(interval('2026-09-01')[1]) == 30
assert cell_xy(traffic.LON0 - 1, traffic.LAT0) is None

with tempfile.TemporaryDirectory() as temp:
    folder=Path(temp)
    path=folder/'positions.sqlite'
    db=sqlite3.connect(path)
    db.execute('CREATE TABLE positions(icao TEXT, ts INTEGER, lat REAL, lon REAL, alt REAL, flags INTEGER, flight TEXT, aircraft_type TEXT, source TEXT, PRIMARY KEY(icao,ts))')
    t=int(datetime(2026,9,1,9,tzinfo=traffic.JST).timestamp())
    rows=[
      ('aabbcc',t+0,31.8772,131.4486,500,0,'TEST1','C172','adsb_icao'),
      ('aabbcc',t+300,31.9200,131.4600,1600,0,'TEST1','C172','adsb_icao'),
      ('aabbcc',t+600,31.9800,131.4700,2800,0,'TEST1','C172','adsb_icao'),
      ('aabbcc',t+900,32.0300,131.4900,4200,0,'TEST1','C172','adsb_icao'),
    ]
    db.executemany('INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?)', rows)
    db.commit()
    db.close()
    gz=daily(path,'2026-09-01',folder/'partial')
    with gzip.open(gz,'rt') as stream:
      obj=json.load(stream)
    assert obj['date']=='2026-09-01'
    assert obj['stats']['flight_legs_with_positions']==1
    assert any(r[0]=='departure' and r[1]==0 and r[2]==9 for r in obj['records'])
    assert any(r[0]=='all' and r[2]==-1 for r in obj['records'])
    assert all(r[5]==1 and r[6]==['aabbcc'] for r in obj['records'])
print('Monthly grid tests passed')
