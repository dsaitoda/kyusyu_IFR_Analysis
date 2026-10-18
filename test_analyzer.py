"""Synthetic-only regression tests. NOT real flight statistics."""
import gzip
import io
import json
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
import kyushu_continuity as analyzer


def unix(jst_hour, minute):
    return int(datetime(2026,10,17,jst_hour,minute,tzinfo=analyzer.JST).timestamp())


def trace(icao, samples):
    base=samples[0][0]
    obj={"icao":icao, "timestamp":base, "t":"C172", "trace": []}
    for ts,lat,lon,alt,flags in samples:
        obj["trace"].append([ts-base,lat,lon,alt,93,100,flags,0,{"flight":icao.upper()}])
    return obj


def save_archive(target, specs):
    buffer=io.BytesIO()
    with tarfile.open(fileobj=buffer,mode="w") as tf:
        for index,obj in enumerate(specs):
            data=gzip.compress(json.dumps(obj).encode("utf-8"))
            info=tarfile.TarInfo(f"2026/traces/6d/trace_full_{obj['icao']}_{index}.json")
            info.size=len(data)
            tf.addfile(info,io.BytesIO(data))
        junk=b"skip"
        info=tarfile.TarInfo("2026/heatmap/irrelevant.json")
        info.size=len(junk)
        tf.addfile(info,io.BytesIO(junk))
    archive=buffer.getvalue()
    midpoint=len(archive)//2+20
    Path(str(target)+".aa").write_bytes(archive[:midpoint])
    Path(str(target)+".ab").write_bytes(archive[midpoint:])


def run_test():
    with tempfile.TemporaryDirectory() as temp:
        work=Path(temp)
        # 08:58 / 09:03 JST straddle 16/17 UTC midnight.
        day16=[trace("abc555",[(unix(8,58),31.95,131.30,6500,0)])]
        day17=[
            trace("abc555",[(unix(9,3),31.89,131.42,3200,0)]),
            trace("abc111",[(unix(10,0),32.2,130.9,7000,0),(unix(10,8),31.90,131.43,4000,0)]),
            trace("abc222",[(unix(10,1),32.2,131.3,4000,0),(unix(10,1)+30,32.19,131.31,4000,0),(unix(10,2),32.18,131.32,4000,0)]),
            trace("abc333",[(unix(11,0),33.2,132.9,4000,0),(unix(11,0)+30,35.7,133.2,4000,0),(unix(11,4),33.2,132.9,4000,0)]),
            trace("abc444",[(unix(13,30),32.9,130.9,3500,0),(unix(13,45),32.9,130.9,3500,2)]),
            trace("abc666",[(unix(14,0),33.1,131.1,5000,0),(unix(14,0)+30,33.2,131.1,15000,0),(unix(14,0)+80,33.1,131.2,5000,0)]),
        ]
        for day,records in [(16,day16),(17,day17)]:
            save_archive(work/f"v2026.10.{day}-planes-readsb-prod-0.tar", records)
        start,end=analyzer.build_date_window("2026-10-17")
        db=analyzer.init_database(work/"synthetic.sqlite")
        for day in [16,17]:
            parts=analyzer.discover_archive_parts(work,datetime(2026,10,day,tzinfo=timezone.utc))
            analyzer.ingest_archive(db,parts,start,end)
        events,counts=analyzer.analyze(db,work,start,end)
        analyzer.export_kml(events,work)
        assert counts["aircraft_in_region"]==6,counts
        assert counts["gap_events"]==2,events
        assert counts["airport_return_events"]==2,events
        assert counts["excluded_new_leg_pairs"]==1,counts
        assert {e["icao"] for e in events}=={"abc111","abc555"},events
        assert (work/"gap_points_below_10000ft.kml").stat().st_size>100
        db.close()
        print("\nPASS: 2 internal gaps, 2 airport reappearance candidates, 0 false gaps across boundary/new flight/altitude transitions.")


if __name__=="__main__":
    run_test()
