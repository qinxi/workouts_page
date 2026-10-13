"""
Sync activities from iGPSPORT (China site) by downloading the original FIT files.

Usage:
    python run_page/igpsport_sync.py <username> <password> [--since YYYY-MM-DD]

Activities up to the last one in run_page/seed_activities.json (history
imported from Strava) are kept from the seed; seeded rides that also exist as
FIT files are replaced by the FIT version.
"""

import argparse
import datetime
import json
import os
import sys
import time

import arrow
import requests
from config import FIT_FOLDER, JSON_FILE, SQL_FILE
from generator.db import Activity, init_db, update_or_create_activity
from gpxtrackposter.track_loader import TrackLoader
from sqlalchemy import func

from generator import Generator
from synced_data_file_logger import load_fit_name_mapping, save_fit_name_mapping

BASE_URL = "https://prod.zh.igpsport.com/service/"
LOGIN_URL = BASE_URL + "auth/account/login"
ACTIVITY_URL = BASE_URL + "web-gateway/web-analyze/activity/"
QUERY_URL = ACTIVITY_URL + "queryMyActivity"
DOWNLOAD_URL = ACTIVITY_URL + "getDownloadUrl/"
PAGE_SIZE = 20

SEED_FILE = os.path.join(
    os.path.dirname(os.path.realpath(__file__)), "seed_activities.json"
)


def parse_interval(s):
    if not s:
        return None
    days = 0
    if "day" in s:
        d, s = s.split(",", 1)
        days = int(d.split()[0])
        s = s.strip()
    h, m, sec = s.split(":")
    return datetime.timedelta(
        days=days, hours=int(h), minutes=int(m), seconds=float(sec)
    )


def seed_db(session, seed_file=SEED_FILE):
    """Insert activities recorded before iGPSPORT sync (idempotent)."""
    if not os.path.exists(seed_file):
        return
    with open(seed_file) as f:
        seed = json.load(f)
    added = 0
    for a in seed:
        if session.query(Activity).filter_by(run_id=int(a["run_id"])).first():
            continue
        session.add(
            Activity(
                run_id=int(a["run_id"]),
                name=a["name"],
                distance=a["distance"],
                moving_time=parse_interval(a["moving_time"]),
                elapsed_time=parse_interval(a["moving_time"]),
                type=a["type"],
                start_date=a["start_date"],
                start_date_local=a["start_date_local"],
                location_country=a["location_country"],
                summary_polyline=a["summary_polyline"],
                average_heartrate=a["average_heartrate"],
                average_speed=a["average_speed"],
                elevation_gain=a["elevation_gain"],
                source=a["source"],
            )
        )
        added += 1
    session.commit()
    print(f"seeded {added} activities from {os.path.basename(seed_file)}")


def drop_seed_duplicates(session, minutes=10):
    """Remove seeded (Strava) activities that are also covered by a FIT file."""
    rows = session.query(Activity).all()
    fits = [
        arrow.get(a.start_date) for a in rows if a.source != "strava" and a.start_date
    ]
    removed = 0
    for a in rows:
        if a.source != "strava":
            continue
        t = arrow.get(a.start_date)
        if any(abs((t - f).total_seconds()) <= minutes * 60 for f in fits):
            session.delete(a)
            removed += 1
    session.commit()
    print(f"removed {removed} seeded activities duplicated by fit files")


def sync_fit_tracks(session, names, ride_ids):
    """Save the given FIT files, using the iGPSPORT titles as activity names."""
    loader = TrackLoader()
    files = [
        os.path.join(FIT_FOLDER, f"{i}.fit")
        for i in ride_ids
        if os.path.exists(os.path.join(FIT_FOLDER, f"{i}.fit"))
    ]
    loaded = loader._load_data_tracks(files, loader.load_func_dict["fit"])
    tracks = loader._filter_tracks(list(loaded.values()))
    tracks = loader._merge_tracks(tracks)
    for t in tracks:
        ride_id = os.path.splitext(t.file_names[0])[0]
        t.name = names.get(ride_id) or t.name
        t.source = "igpsport"
        created = update_or_create_activity(session, t.to_namedtuple())
        sys.stdout.write("+" if created else ".")
    sys.stdout.flush()
    session.commit()


class IGPSport:
    def __init__(self, username, password):
        self.s = requests.Session()
        r = self.s.post(
            LOGIN_URL,
            json={"appId": "igpsport-web", "username": username, "password": password},
            timeout=30,
        )
        r.raise_for_status()
        token = (r.json().get("data") or {}).get("access_token")
        if not token:
            raise Exception("iGPSPORT login failed, check username/password")
        self.s.headers["Authorization"] = f"Bearer {token}"

    def activities(self, begin_time="", end_time=""):
        page = 1
        while True:
            r = self.s.get(
                QUERY_URL,
                params={
                    "pageNo": page,
                    "pageSize": PAGE_SIZE,
                    "beginTime": begin_time,
                    "endTime": end_time,
                    "sort": "1",
                    "reqType": "1",
                },
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()["data"]
            yield from data["rows"]
            if page >= data["totalPage"]:
                break
            page += 1

    def download_fit(self, ride_id, path):
        r = self.s.get(DOWNLOAD_URL + str(ride_id), timeout=30)
        r.raise_for_status()
        url = r.json().get("data")
        if not url:
            raise Exception(f"no download url for {ride_id}")
        # the file url is a signed (CDN) url, do not send the token to it
        f = requests.get(url, timeout=120)
        f.raise_for_status()
        with open(path, "wb") as out:
            out.write(f.content)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("username")
    parser.add_argument("password")
    parser.add_argument("--since", help="only sync activities on/after YYYY-MM-DD")
    args = parser.parse_args()

    os.makedirs(FIT_FOLDER, exist_ok=True)
    session = init_db(SQL_FILE)
    seed_db(session)

    names = load_fit_name_mapping()
    if args.since:
        begin = args.since
    else:
        # no title mapping yet (first run or lost cache): everything after the
        # Strava-era history; afterwards only what is newer than the last FIT
        last = None
        if names:
            last = (
                session.query(func.max(Activity.start_date_local))
                .filter(Activity.source != "strava")
                .scalar()
            )
        if not last:
            last = (
                session.query(func.max(Activity.start_date_local))
                .filter(Activity.source == "strava")
                .scalar()
            )
        begin = arrow.get(last).shift(days=-1).format("YYYY-MM-DD") if last else ""
    print(f"syncing iGPSPORT activities since {begin or 'the beginning'}")

    client = IGPSport(args.username, args.password)
    pending = []  # rides not processed yet (new, or still without a title)
    new_files = 0
    for row in client.activities(begin_time=begin):
        ride_id = str(row["rideId"])
        if ride_id not in names:
            pending.append(ride_id)
        names[ride_id] = row.get("title") or ""
        path = os.path.join(FIT_FOLDER, f"{ride_id}.fit")
        if os.path.exists(path):
            continue
        try:
            client.download_fit(ride_id, path)
        except Exception as e:
            print(f"failed to download {ride_id} ({row.get('title')}): {e}")
            if ride_id in pending:
                pending.remove(ride_id)
                del names[ride_id]
            continue
        new_files += 1
        print(f"downloaded {ride_id} {row.get('title')}")
        time.sleep(0.5)
    save_fit_name_mapping(names)

    print(f"{new_files} new fit files")
    session.close()
    generator = Generator(SQL_FILE)
    sync_fit_tracks(generator.session, names, pending)
    drop_seed_duplicates(generator.session)
    with open(JSON_FILE, "w") as f:
        json.dump(generator.load(), f, indent=0)


if __name__ == "__main__":
    main()
