"""Poll App Store and Google Play reviews and post new ones to Slack.

Env vars:
  SLACK_WEBHOOK_URL            required
  APPLE_APP_ID                 numeric Apple ID; iOS is skipped if unset
  ASC_ISSUER_ID, ASC_KEY_ID, ASC_PRIVATE_KEY   App Store Connect API key
  GPLAY_PACKAGE_NAME           defaults to com.dubdub.app
  GPLAY_SERVICE_ACCOUNT_JSON   service account key JSON; Android is skipped if unset
  BACKFILL                     "1" posts existing reviews on the first run instead of only recording them

Flags:
  --dry-run   print Slack payloads instead of posting, and do not write state
"""

import json
import os
import sys
import time
from pathlib import Path

import jwt
import requests
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

STATE_FILE = Path(__file__).parent / "state.json"
MAX_SEEN_IDS = 1000
DRY_RUN = "--dry-run" in sys.argv


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    if DRY_RUN:
        return
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def stars(rating):
    rating = int(rating or 0)
    return "★" * rating + "☆" * (5 - rating)


def post_to_slack(review):
    low = int(review["rating"] or 0) <= 2
    header = f"{':rotating_light: ' if low else ''}{review['platform']}  {stars(review['rating'])}"
    meta = [review["author"] or "Anonymous"]
    if review.get("version"):
        meta.append(f"v{review['version']}")
    if review.get("territory"):
        meta.append(review["territory"])
    if review.get("date"):
        meta.append(review["date"])

    text = ""
    if review.get("title"):
        text += f"*{review['title']}*\n"
    text += review.get("body") or "_(no text)_"

    payload = {
        "text": f"{header}: {(review.get('body') or '')[:150]}",
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*{header}*"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": " · ".join(meta)}]},
            {"type": "divider"},
        ],
    }

    if DRY_RUN:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    resp = requests.post(os.environ["SLACK_WEBHOOK_URL"], json=payload, timeout=15)
    if resp.status_code == 429:
        time.sleep(int(resp.headers.get("Retry-After", "5")))
        resp = requests.post(os.environ["SLACK_WEBHOOK_URL"], json=payload, timeout=15)
    resp.raise_for_status()
    time.sleep(1.1)  # Slack webhooks allow about 1 message per second


# ---------- App Store ----------

def asc_token():
    key = os.environ["ASC_PRIVATE_KEY"].replace("\\n", "\n")
    now = int(time.time())
    return jwt.encode(
        {"iss": os.environ["ASC_ISSUER_ID"], "iat": now, "exp": now + 1200, "aud": "appstoreconnect-v1"},
        key,
        algorithm="ES256",
        headers={"kid": os.environ["ASC_KEY_ID"], "typ": "JWT"},
    )


def fetch_ios(app_id, pages=2):
    headers = {"Authorization": f"Bearer {asc_token()}"}
    url = f"https://api.appstoreconnect.apple.com/v1/apps/{app_id}/customerReviews"
    params = {"sort": "-createdDate", "limit": 200}
    reviews = []
    for _ in range(pages):
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("data", []):
            a = item["attributes"]
            reviews.append({
                "id": item["id"],
                "platform": "iOS",
                "rating": a.get("rating"),
                "title": a.get("title"),
                "body": a.get("body"),
                "author": a.get("reviewerNickname"),
                "territory": a.get("territory"),
                "date": (a.get("createdDate") or "")[:10],
            })
        url = data.get("links", {}).get("next")
        params = None
        if not url:
            break
    return reviews


# ---------- Google Play ----------

def fetch_android(package):
    info = json.loads(os.environ["GPLAY_SERVICE_ACCOUNT_JSON"])
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/androidpublisher"]
    )
    session = AuthorizedSession(creds)
    url = f"https://androidpublisher.googleapis.com/androidpublisher/v3/applications/{package}/reviews"
    params = {"maxResults": 100}
    reviews = []
    while True:
        resp = session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("reviews", []):
            user = next((c["userComment"] for c in item.get("comments", []) if "userComment" in c), {})
            seconds = int(user.get("lastModified", {}).get("seconds", 0))
            reviews.append({
                "id": item["reviewId"],
                "platform": "Android",
                "rating": user.get("starRating"),
                "title": None,
                "body": (user.get("text") or "").strip(),
                "author": item.get("authorName"),
                "version": user.get("appVersionName"),
                "territory": user.get("reviewerLanguage"),
                "date": time.strftime("%Y-%m-%d", time.gmtime(seconds)) if seconds else None,
            })
        token = data.get("tokenPagination", {}).get("nextPageToken")
        if not token:
            break
        params = {"maxResults": 100, "token": token}
    return reviews


# ---------- main ----------

def process(platform, fetch, state):
    reviews = fetch()
    seen = state.get(platform)
    first_run = seen is None
    seen = list(seen or [])
    seen_set = set(seen)

    new = [r for r in reviews if r["id"] not in seen_set]
    new.reverse()  # oldest first so Slack reads in order

    if first_run and os.environ.get("BACKFILL") != "1":
        print(f"{platform}: first run, recorded {len(new)} existing reviews without posting")
    else:
        for r in new:
            post_to_slack(r)
            seen.append(r["id"])
            state[platform] = seen[-MAX_SEEN_IDS:]
            save_state(state)  # save after each post so a crash never reposts
        print(f"{platform}: posted {len(new)} new reviews")

    seen.extend(r["id"] for r in new if r["id"] not in seen)
    state[platform] = list(dict.fromkeys(seen))[-MAX_SEEN_IDS:]
    save_state(state)


def main():
    if not DRY_RUN and not os.environ.get("SLACK_WEBHOOK_URL"):
        sys.exit("SLACK_WEBHOOK_URL is not set")

    state = load_state()
    failures = []

    app_id = os.environ.get("APPLE_APP_ID")
    if app_id:
        try:
            process("ios", lambda: fetch_ios(app_id), state)
        except Exception as e:
            failures.append(f"iOS: {e}")
    else:
        print("iOS: skipped, APPLE_APP_ID not set")

    if os.environ.get("GPLAY_SERVICE_ACCOUNT_JSON"):
        package = os.environ.get("GPLAY_PACKAGE_NAME", "com.dubdub.app")
        try:
            process("android", lambda: fetch_android(package), state)
        except Exception as e:
            failures.append(f"Android: {e}")
    else:
        print("Android: skipped, GPLAY_SERVICE_ACCOUNT_JSON not set")

    if failures:
        for f in failures:
            print(f"ERROR {f}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
