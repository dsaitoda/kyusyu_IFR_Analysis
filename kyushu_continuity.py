# -*- coding: utf-8 -*-
"""ADSB.lol readsb daily trace archive continuity analyzer.

Input: vYYYY.MM.DD-planes-readsb-prod-0.tar.aa/.ab files (original split tar).
Outputs: CSV reports, KML markers, TXT summary, and optional histogram PNG.
The program does NOT download data automatically.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import math
import os
import re
import sqlite3
import tarfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
JST = timezone(timedelta(hours=9))  # UTC+9, no daylight saving in Japan
from xml.etree import ElementTree as ET

# ------------- USER SETTINGS (Spyder users can edit these) --------------
INPUT_DIR = Path(os.getenv("ADSB_INPUT_DIR", r"C:\ADSB_History"))
OUTPUT_DIR = Path(os.getenv("ADSB_OUTPUT_DIR", str(INPUT_DIR / "results")))
TARGET_DATE_JST = os.getenv("ADSB_DATE_JST", "2026-10-17")
# Kyushu and nearby sea / island approaches; this is a rectangular box,
# not an official FIR/ATC airspace boundary.
LAT_MIN, LAT_MAX = 29.2, 34.7
LON_MIN, LON_MAX = 128.0, 133.4
ALT_MAX_FT = 10000  # both ends must meet this for the altitude-limited report
GAP_THRESHOLD_SEC = 60
AIRPORT_RADIUS_NM = 20
# -----------------------------------------------------------------------

# Approximate aerodrome reference positions (only for a 20 NM proximity flag).
# Consult current aeronautical publications if using a location operationally.
AIRPORTS = {
    "RJFF Fukuoka": (33.5859, 130.4507),
    "RJFR Kitakyushu": (33.8459, 131.0347),
    "RJFO Oita": (33.4794, 131.7372),
    "RJFT Kumamoto": (32.8373, 130.8551),
    "RJFU Nagasaki": (32.9169, 129.9136),
    "RJFS Saga": (33.1497, 130.3022),
    "RJFE Fukue": (32.6663, 128.8328),
    "RJFM Miyazaki": (31.8772, 131.4486),
    "RJFK Kagoshima": (31.8034, 130.7194),
    "RJDA Amakusa": (32.4825, 130.1589),
    "RJFN Nyutabaru": (32.0836, 131.4514),
    "RJFZ Tsuiki": (33.6850, 131.0390),
    "RJFC Yakushima": (30.3856, 130.6592),
    "RJFG Tanegashima": (30.6051, 130.9912),
}


def inside_box(lat, lon):
    return LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX


def haversine_nm(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2)**2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2)**2
    return 3440.065 * (2 * math.asin(min(1, math.sqrt(h))))


def nearest_airport(lat, lon):
    return min(((name, haversine_nm(lat, lon, *coords)) for name, coords in AIRPORTS.items()), key=lambda x: x[1])


def time_jst(ts):
    return datetime.fromtimestamp(ts, JST).strftime("%Y-%m-%d %H:%M:%S")


def number_or_none(value):
    if value == "ground":
        return 0.0
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def build_date_window(date_str):
    day = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=JST)
    return day.timestamp(), (day + timedelta(days=1)).timestamp()


def discover_archive_parts(folder: Path, utc_date):
    """Return sorted split-tar paths, favoring prod-0 over staging.

    Validates that .aa/.ab etc are contiguous from aa, because an absent
    middle piece silently corrupts a stream and invalidates the statistics.
    """
    pattern = f"v{utc_date.strftime('%Y.%m.%d')}-planes-readsb-*.tar.*"
    all_paths = list(folder.glob(pattern))
    groups = defaultdict(list)
    for path in all_paths:
        match = re.fullmatch(r"(.+\.tar)\.([a-z]{2})", path.name)
        if match:
            groups[match.group(1)].append(path)
    if not groups:
        raise FileNotFoundError(f"{folder} に {pattern} (*.tar.aa/.ab) が見つかりません。")
    candidates = sorted(groups, key=lambda k: ("-prod-0.tar" not in k, "-staging-0.tar" not in k, k))
    selected = candidates[0]
    files = sorted(groups[selected])
    suffixes = [p.name.split(".tar.")[-1] for p in files]
    expected = [f"a{chr(ord('a')+i)}" for i in range(len(files))]
    if suffixes != expected:
        raise ValueError(f"{selected} の分割ファイルが連続していません: {suffixes}")
    if len(files) < 2:
        raise ValueError(f"{selected} は分割ファイルが1個だけです。ダウンロード不足の可能性があります。")
    print(f"Archive {utc_date.date()}: {len(files)} parts, size {sum(p.stat().st_size for p in files)/1e9:.2f} GB")
    return files


class ConcatenatedFile(io.RawIOBase):
    """Presents .tar.aa + .tar.ab as a single read-only streaming file."""
    def __init__(self, paths):
        self.paths = list(paths)
        self.part_idx = 0
        self.fp = open(self.paths[0], "rb")

    def readable(self):
        return True

    def readinto(self, b):
        dest = memoryview(b)
        written = 0
        while written < len(dest) and self.fp:
            chunk = self.fp.read(len(dest) - written)
            if chunk:
                dest[written:written+len(chunk)] = chunk
                written += len(chunk)
            else:
                self.fp.close()
                self.part_idx += 1
                self.fp = open(self.paths[self.part_idx], "rb") if self.part_idx < len(self.paths) else None
        return written

    def close(self):
        if self.fp:
            self.fp.close()
            self.fp = None
        super().close()


def init_database(dbpath):
    if dbpath.exists():
        dbpath.unlink()  # always recalculate from original archives
    db = sqlite3.connect(dbpath)
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")
    db.execute("CREATE TABLE positions(icao TEXT NOT NULL, ts INTEGER NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, alt REAL, flags INTEGER, flight TEXT, aircraft_type TEXT, source TEXT, PRIMARY KEY(icao,ts)) WITHOUT ROWID")
    return db


def extract_trace_points(obj, start_ts, end_ts):
    if not isinstance(obj, dict) or not isinstance(obj.get("trace"), list):
        return None
    icao = str(obj.get("icao") or "").strip().lower()
    if not icao:
        return None
    base = number_or_none(obj.get("timestamp"))
    if base is None:
        return None
    typ = str(obj.get("t") or "")[:16]
    raw = []
    has_local_point = False
    flight = ""
    for point in obj["trace"]:
        if not isinstance(point, list) or len(point) < 7:
            continue
        offset = number_or_none(point[0])
        lat, lon = number_or_none(point[1]), number_or_none(point[2])
        if offset is None or lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        ts = int(round(base + offset))
        if not start_ts <= ts < end_ts:
            continue
        altitude = number_or_none(point[3])
        flags = int(point[6]) if isinstance(point[6], (int, float)) else 0
        if len(point) > 8 and isinstance(point[8], dict):
            flight = str(point[8].get("flight") or flight).strip()[:20]
        source = str(point[9] or "")[:24] if len(point) > 9 else ""
        raw.append((icao, ts, lat, lon, altitude, flags, "", typ, source))
        if inside_box(lat, lon):
            has_local_point = True
    if not has_local_point:
        return None
    return [(icao, ts, lat, lon, alt, flags, flight, typ, src) for icao, ts, lat, lon, alt, flags, _, typ, src in raw]


def ingest_archive(db, paths, start_ts, end_ts):
    count = Counter()
    print(f"  読み込み開始: {paths[0].name}", flush=True)
    with ConcatenatedFile(paths) as parts:
        with tarfile.open(fileobj=parts, mode="r|") as tar:
            batch = []
            for member in tar:
                if not member.isfile():
                    continue
                name = member.name.lower()
                if ("/traces/" not in f"/{name}" or not (name.endswith(".json") or name.endswith(".json.gz"))):
                    continue
                count["trace_files"] += 1
                if member.size > 35_000_000:
                    count["oversized_files_skipped"] += 1
                    continue
                try:
                    stream = tar.extractfile(member)
                    if stream is None:
                        continue
                    raw = stream.read()
                    if raw.startswith(b"\x1f\x8b"):
                        raw = gzip.decompress(raw)
                    doc = json.loads(raw)
                    rows = extract_trace_points(doc, start_ts, end_ts)
                    if rows:
                        batch.extend(rows)
                        count["aircraft_files_with_kyushu"] += 1
                        count["candidate_rows"] += len(rows)
                    if count["trace_files"] % 5000 == 0:
                        db.executemany("INSERT OR IGNORE INTO positions VALUES(?,?,?,?,?,?,?,?,?)", batch)
                        db.commit()
                        batch.clear()
                        print(f"    traces: {count['trace_files']:,}, 地域内機体ファイル: {count['aircraft_files_with_kyushu']:,}", flush=True)
                except (OSError, EOFError, ValueError, json.JSONDecodeError, UnicodeError) as exc:
                    count["decode_error"] += 1
                    if count["decode_error"] <= 3:
                        print(f"    警告: {member.name}: {exc}")
            if batch:
                db.executemany("INSERT OR IGNORE INTO positions VALUES(?,?,?,?,?,?,?,?,?)", batch)
                db.commit()
    print(f"  完了: {dict(count)}", flush=True)
    return count


def range_category(dt):
    if dt <= 60:
        return "連続(60秒以下)"
    if dt <= 180:
        return "軽度(1-3分)"
    if dt <= 600:
        return "中程度(3-10分)"
    return "大きな欠損(10分超)"


def analyze(db, outpath, start_ts, end_ts):
    counts = Counter()
    aircraft_set = set()
    aircraft_with_gap = set()
    aircraft_with_comparable_pairs = set()
    aircraft_with_airport_return = set()
    gaps = []
    previous = None
    last_icao = None

    for icao, ts, lat, lon, alt, flags, flight, typ, source in db.execute("SELECT icao,ts,lat,lon,alt,flags,flight,aircraft_type,source FROM positions ORDER BY icao,ts"):
        record = (icao, ts, lat, lon, alt, flags, flight, typ, source)
        if inside_box(lat, lon):
            aircraft_set.add(icao)
            counts["observed_position_points_in_region"] += 1
            if (flags or 0) & 1:
                counts["stale_flag_points_in_region"] += 1
            if alt is None:
                counts["no_altitude_points_in_region"] += 1
        if icao != last_icao:
            previous = None
            last_icao = icao
        if previous is not None:
            a = previous
            if inside_box(a[2], a[3]) and inside_box(lat, lon):
                counts["all_inside_pairs"] += 1
                if (flags or 0) & 2:
                    counts["excluded_new_leg_pairs"] += 1
                elif not (start_ts <= a[1] < end_ts and start_ts <= ts < end_ts):
                    counts["excluded_out_of_window"] += 1
                else:
                    dt = ts - a[1]
                    if dt <= 0:
                        counts["nonpositive_dt"] += 1
                    else:
                        counts["valid_inside_pairs"] += 1
                        aircraft_with_comparable_pairs.add(icao)
                        counts["ALL_"+range_category(dt)] += 1
                        in_alt_scope = a[4] is not None and alt is not None and 0 <= a[4] <= ALT_MAX_FT and 0 <= alt <= ALT_MAX_FT
                        if in_alt_scope:
                            counts["alt_limited_pairs"] += 1
                            counts["ALT_"+range_category(dt)] += 1
                        if dt > GAP_THRESHOLD_SEC:
                            aircraft_with_gap.add(icao)
                            distance = haversine_nm(a[2],a[3],lat,lon)
                            airport, airport_dist = nearest_airport(lat,lon)
                            airborne = a[4] is not None and alt is not None and a[4] > 500 and alt > 500
                            airport_return = bool(in_alt_scope and airborne and dt > 180 and airport_dist <= AIRPORT_RADIUS_NM)
                            if airport_return:
                                aircraft_with_airport_return.add(icao)
                                counts["airport_return_events"] += 1
                            gaps.append({
                                "icao":icao, "flight":flight or a[6], "aircraft_type":typ or a[7],
                                "gap_seconds":dt, "gap_minutes":round(dt/60,2),
                                "time_before_jst":time_jst(a[1]), "time_after_jst":time_jst(ts),
                                "lat_before":a[2], "lon_before":a[3], "alt_before_ft":a[4],
                                "lat_after":lat, "lon_after":lon, "alt_after_ft":alt,
                                "straight_distance_nm":round(distance,2),
                                "implied_gs_kt":round(distance*3600/dt,1),
                                "reappear_nearest_airport":airport,
                                "reappear_airport_dist_nm":round(airport_dist,1),
                                "both_below_10000ft":in_alt_scope,
                                "airborne_to_airport_return_candidate":airport_return,
                                "reappearance_stale_flag":bool(flags & 1),
                                "source_before":a[8], "source_after":source,
                                "category":range_category(dt),
                            })
        previous = record

    counts["aircraft_in_region"] = len(aircraft_set)
    counts["aircraft_with_any_gap"] = len(aircraft_with_gap)
    counts["aircraft_with_comparable_pairs"] = len(aircraft_with_comparable_pairs)
    counts["aircraft_with_airport_return"] = len(aircraft_with_airport_return)
    counts["gap_events"] = len(gaps)

    output_csv = outpath / "gap_events.csv"
    if gaps:
        with output_csv.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(gaps[0].keys()))
            writer.writeheader()
            writer.writerows(sorted(gaps, key=lambda x: (x["time_before_jst"],x["icao"])))
    else:
        output_csv.write_text("no gaps found\n", encoding="utf-8")

    with (outpath / "summary.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["指標", "件数"])
        for key, val in sorted(counts.items()):
            writer.writerow([key, val])

    note = [
        f"九州 ADSB.lol 航跡の連続性: {TARGET_DATE_JST} JST",
        f"解析矩形: lat {LAT_MIN}-{LAT_MAX}, lon {LON_MIN}-{LON_MAX}",
        f"対象: すべての航空機（IFRを保証しない）; 高度限定集計は両端とも0-{ALT_MAX_FT} ft以下",
        f"記録間隔 {GAP_THRESHOLD_SEC}秒超を欠損候補として記録", "",
        f"解析範囲に現れた航空機（ICAOアドレス）: {counts['aircraft_in_region']:,}",
        f"記録位置点数（九州内）: {counts['observed_position_points_in_region']:,}",
        f"うち stale フラグの点数: {counts['stale_flag_points_in_region']:,}",
        f"うち高度不明の点数: {counts['no_altitude_points_in_region']:,}",
        f"九州内の有効連続点ペア: {counts['valid_inside_pairs']:,}",
        f"高度限定の点ペア: {counts['alt_limited_pairs']:,}",
        f"連続性を比較できた航空機: {counts['aircraft_with_comparable_pairs']:,}",
        f"欠損候補イベント（全高度）: {counts['gap_events']:,}",
        f"欠損候補があった航空機: {counts['aircraft_with_any_gap']:,} ({counts['aircraft_with_any_gap']/counts['aircraft_with_comparable_pairs']*100:.1f}% of comparable aircraft)" if counts["aircraft_with_comparable_pairs"] else "比較可能な航空機がありません",
        f"空港付近で空中再出現した候補（3分超、両端0-{ALT_MAX_FT}ft以下、500ft超）: {counts['airport_return_events']:,}",
        f"同候補の航空機: {counts['aircraft_with_airport_return']:,}",
        "",
        "全高度・時間間隔分類:",
    ]
    for key in ["連続(60秒以下)","軽度(1-3分)","中程度(3-10分)","大きな欠損(10分超)"]:
        note.append(f"  {key}: {counts['ALL_'+key]:,}")
    note.extend(["", "10000ft未満・時間間隔分類:"])
    for key in ["連続(60秒以下)","軽度(1-3分)","中程度(3-10分)","大きな欠損(10分超)"]:
        note.append(f"  {key}: {counts['ALT_'+key]:,}")
    note += [
        "", "注意:",
        "- 『欠損』は受信未記録区間の候補。受信機の遮蔽、収集側の記録間引き、地上滞在などは判別できません。",
        "- 機体識別子で集計しておりフライト数ではありません。新レグフラグ(bit 2)があると点ペアを除外します。",
        "- 同一機体の連続点は、九州の境界外にある点も保存して判定しています。",
        "- 両端が10000ft未満という条件だけを使い、高度フィルタをかけてから欠損を作り出さないようにしています。",
        "- モードSのみや位置が取れない機体は評価不能、地域内で一度も受信されなかった機体は数えられません。",
        "- 異なる日付・ソースの位置重複はICAOアドレスと1秒単位の時刻で統合しています。",
        "- 地上付近でのロストは真の着陸・離陸との区別が困難です。",
    ]
    (outpath/"summary.txt").write_text("\n".join(note)+"\n", encoding="utf-8-sig")
    for line in note[:12]: print(line)
    return gaps, counts


def export_kml(gaps, output_dir):
    K = "http://www.opengis.net/kml/2.2"
    ET.register_namespace("", K)
    root = ET.Element(f"{{{K}}}kml")
    document = ET.SubElement(root, f"{{{K}}}Document")
    ET.SubElement(document, f"{{{K}}}name").text = "九州ADS-B 受信途絶候補（実測点のみ）"
    for ident, color in [("gap1","ff00ffff"),("gap3","ff00a5ff"),("gap10","ff0000ff")]:
        style = ET.SubElement(document,f"{{{K}}}Style",id=ident)
        icon_style=ET.SubElement(style,f"{{{K}}}IconStyle")
        ET.SubElement(icon_style, f"{{{K}}}color").text=color
        ET.SubElement(icon_style, f"{{{K}}}scale").text="0.75"
    for event in gaps:
        if not event["both_below_10000ft"]:
            continue
        style = "gap10" if event["gap_seconds"] > 600 else "gap3" if event["gap_seconds"] > 180 else "gap1"
        for suffix, latkey, lonkey, timekey, altkey in [
            ("消失", "lat_before", "lon_before", "time_before_jst", "alt_before_ft"),
            ("再出現", "lat_after", "lon_after", "time_after_jst", "alt_after_ft")
        ]:
            item = ET.SubElement(document,f"{{{K}}}Placemark")
            ET.SubElement(item, f"{{{K}}}name").text = f"{event['icao']} {suffix}: {event['gap_minutes']}分"
            ET.SubElement(item, f"{{{K}}}description").text = (
                f"{event['flight']} | {event[timekey]} JST | {event[altkey]} ft | gap {event['gap_seconds']} s | "
                f"再出現点の最寄り空港 {event['reappear_nearest_airport']} ({event['reappear_airport_dist_nm']} NM) | "
                f"空港付近で再出現候補={event['airborne_to_airport_return_candidate']}"
            )
            ET.SubElement(item, f"{{{K}}}styleUrl").text = "#" + style
            point = ET.SubElement(item, f"{{{K}}}Point")
            ET.SubElement(point,f"{{{K}}}altitudeMode").text="clampToGround"
            ET.SubElement(point,f"{{{K}}}coordinates").text=f"{event[lonkey]},{event[latkey]},0"
    ET.ElementTree(root).write(output_dir/"gap_points_below_10000ft.kml", encoding="utf-8", xml_declaration=True)


def plot_summary(gaps, outpath):
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        return
    labels = ["1–3 min", "3–10 min", ">10 min"]
    values = [sum(x["both_below_10000ft"] and x["category"].startswith(prefix) for x in gaps)
              for prefix in ["軽度", "中程度", "大きな"]]
    fig, ax = plt.subplots(figsize=(7.5,4.5))
    ax.bar(labels, values)
    ax.set_ylabel("Gap events")
    ax.set_title(f"Kyushu ADS-B discontinuities below {ALT_MAX_FT:,} ft | {TARGET_DATE_JST} JST")
    for i,v in enumerate(values):
        ax.text(i, v, str(v), ha="center", va="bottom")
    fig.tight_layout()
    fig.savefig(outpath/"gap_histogram.png", dpi=175)
    plt.close(fig)


def main():
    start_ts,end_ts=build_date_window(TARGET_DATE_JST)
    print(f"対象 JST: {TARGET_DATE_JST} 00:00〜24:00; UTC {datetime.fromtimestamp(start_ts, timezone.utc)}〜{datetime.fromtimestamp(end_ts, timezone.utc)}")
    if not INPUT_DIR.exists():
        raise FileNotFoundError(f"INPUT_DIR が存在しません: {INPUT_DIR}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    utc_days = sorted({datetime.fromtimestamp(start_ts, timezone.utc).date(),
                       datetime.fromtimestamp(end_ts-1, timezone.utc).date()})
    archives = [discover_archive_parts(INPUT_DIR, datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc)) for d in utc_days]
    db = init_database(OUTPUT_DIR/"positions.sqlite")
    try:
        for parts in archives:
            ingest_archive(db, parts, start_ts, end_ts)
        gaps,_=analyze(db, OUTPUT_DIR, start_ts, end_ts)
        export_kml(gaps, OUTPUT_DIR)
        plot_summary(gaps, OUTPUT_DIR)
    finally:
        db.close()
    print(f"\n解析完了: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
