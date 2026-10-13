"""
Sync activities from iGPSPORT (China site) by downloading the original FIT files.

Usage:
    python run_page/igpsport_sync.py <username> <password> [--since YYYY-MM-DD]

Activities older than the last one in the local db (seeded from
run_page/seed_activities.json) are skipped so that history imported from Strava
is not duplicated.
"""

import argparse
import datetime
import json
import os
import time

import arrow
import requests
from config import FIT_FOLDER, JSON_FILE, SQL_FILE
from generator.db import Activity, init_db
from sqlalchemy import func

from utils import make_activities_file

BASE_URL = "https://prod.zh.igpsport.com/service/"
LOGIN_URL = BASE_URL + "auth/account/login"
ACTIVITY_URL = BASE_URL + "web-gateway/web-analyze/activity/"
QUERY_URL = ACTIVITY_URL + "queryMyActivity"
DOWNLOAD_URL = ACTIVITY_URL + "getDownloadUrl/"
PAGE_SIZE = 20

SEED_FILE = os.path.join(os.path.dirname(os.path.realpath(__file__)), "seed_activities.json")


def parse_interval(s):
    if not s:
        return None
    days = 0
    if "day" in s:
        d, s = s.split(",", 1)
        days = int(d.split()[0])
        s = s.strip()
    h, m, sec = s.split(":")
    return datetime.timedelta(days=days, hours=int(h), minutes=int(m), seconds=float(sec))


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

    last_local = session.query(func.max(Activity.start_date_local)).scalar()
    if args.since:
        begin = args.since
    elif last_local:
        begin = arrow.get(last_local).shift(days=-1).format("YYYY-MM-DD")
    else:
        begin = ""
    print(f"syncing iGPSPORT activities since {begin or 'the beginning'}")

    client = IGPSport(args.username, args.password)
    new_files = 0
    for row in client.activities(begin_time=begin):
        ride_id = row["rideId"]
        path = os.path.join(FIT_FOLDER, f"{ride_id}.fit")
        start = str(row.get("startTime", ""))
        # skip what is already covered by the Strava-era history
        if last_local and not args.since and start and start <= last_local:
            continue
        if os.path.exists(path):
            continue
        try:
            client.download_fit(ride_id, path)
        except Exception as e:
            print(f"failed to download {ride_id} ({row.get('title')}): {e}")
            continue
        new_files += 1
        print(f"downloaded {ride_id} {start} {row.get('title')}")
        time.sleep(0.5)

    print(f"{new_files} new fit files")
    make_activities_file(SQL_FILE, FIT_FOLDER, JSON_FILE, "fit")


if __name__ == "__main__":
    main()
