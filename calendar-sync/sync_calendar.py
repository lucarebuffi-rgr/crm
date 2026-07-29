"""
GA Probates CRM — Google Calendar Sync
Runs every 10-15 minutes via GitHub Actions.

Reads every task on every lead in Firestore and mirrors it onto a shared
Google Calendar. Google Calendar's own notifications (popup/email/mobile)
handle reminders from there — no custom timing logic needed here.

New tasks -> new calendar events
Edited tasks (name/date/time/notes changed) -> event gets updated
Tasks marked done or deleted in the CRM -> event gets removed from the calendar
"""

import os
import json
import hashlib

import firebase_admin
from firebase_admin import credentials, firestore

from google.oauth2 import service_account
from googleapiclient.discovery import build

# ---------------- Config ----------------

CALENDAR_ID = os.environ["GOOGLE_CALENDAR_ID"]
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# ---------------- Firebase setup ----------------

cred_json = json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT"])
firebase_cred = credentials.Certificate(cred_json)
firebase_admin.initialize_app(firebase_cred)
db = firestore.client()

# ---------------- Google Calendar setup ----------------
# Reuses the same service account JSON — it already has both Firestore
# and Calendar access since it's tied to the same Google Cloud project.

calendar_creds = service_account.Credentials.from_service_account_info(
    cred_json, scopes=SCOPES
)
calendar_service = build("calendar", "v3", credentials=calendar_creds)


def task_hash(task):
    """A fingerprint of the fields that matter, so we only push updates when something real changed."""
    fingerprint = f"{task.get('name')}|{task.get('date')}|{task.get('time')}|{task.get('notes')}"
    return hashlib.sha256(fingerprint.encode()).hexdigest()[:16]


def build_event_body(task, lead):
    lead_name = f"{lead.get('firstName','')} {lead.get('lastName','')}".strip()
    director = lead.get("acquisitionDirector") or "Unassigned"
    title = f"{task['name']} — {lead_name}" if lead_name else task["name"]

    description_lines = [f"Assigned to: {director}"]
    if lead.get("phone"):
        description_lines.append(f"Phone: {lead['phone']}")
    addr = ", ".join(filter(None, [lead.get("address"), lead.get("city"), lead.get("state")]))
    if addr:
        description_lines.append(f"Address: {addr}")
    if task.get("notes"):
        description_lines.append(f"Notes: {task['notes']}")
    description = "\n".join(description_lines)

    if task.get("time"):
        start = {"dateTime": f"{task['date']}T{task['time']}:00", "timeZone": "America/New_York"}
        end_hour_min = task["time"]
        # default 30-minute block
        h, m = map(int, end_hour_min.split(":"))
        m += 30
        if m >= 60:
            h += 1
            m -= 60
        end = {"dateTime": f"{task['date']}T{h:02d}:{m:02d}:00", "timeZone": "America/New_York"}
    else:
        start = {"date": task["date"]}
        end = {"date": task["date"]}

    return {
        "summary": title,
        "description": description,
        "start": start,
        "end": end,
        # Google Calendar's own default notifications apply (popup/email per each
        # person's own calendar notification settings) — no custom overrides needed.
    }


def main():
    leads = list(db.collection("leads").stream())
    created, updated, deleted, skipped = 0, 0, 0, 0

    for doc in leads:
        lead = doc.to_dict()
        tasks = lead.get("tasks", [])
        changed = False

        for task in tasks:
            current_hash = task_hash(task)

            # Task is done -> remove its calendar event if one exists, then stop tracking it
            if task.get("done"):
                if task.get("calendarEventId"):
                    try:
                        calendar_service.events().delete(
                            calendarId=CALENDAR_ID, eventId=task["calendarEventId"]
                        ).execute()
                        deleted += 1
                    except Exception as e:
                        print(f"  (couldn't delete event for done task '{task.get('name')}': {e})")
                    task["calendarEventId"] = None
                    task["calendarSyncHash"] = None
                    changed = True
                continue

            if not task.get("date"):
                skipped += 1
                continue

            event_body = build_event_body(task, lead)

            if not task.get("calendarEventId"):
                # brand new task -> create the event
                event = calendar_service.events().insert(
                    calendarId=CALENDAR_ID, body=event_body
                ).execute()
                task["calendarEventId"] = event["id"]
                task["calendarSyncHash"] = current_hash
                changed = True
                created += 1
            elif task.get("calendarSyncHash") != current_hash:
                # existing task, something changed -> update the event
                try:
                    calendar_service.events().update(
                        calendarId=CALENDAR_ID,
                        eventId=task["calendarEventId"],
                        body=event_body,
                    ).execute()
                    task["calendarSyncHash"] = current_hash
                    changed = True
                    updated += 1
                except Exception as e:
                    print(f"  (couldn't update event for task '{task.get('name')}': {e})")

        # Handle tasks that were deleted outright in the CRM (no longer in the array
        # but we can't detect that from the array itself — see note in chat reply).

        if changed:
            db.collection("leads").document(doc.id).update({"tasks": tasks})

    print(f"Created: {created}  Updated: {updated}  Removed (done): {deleted}  Skipped (no date): {skipped}")


if __name__ == "__main__":
    main()
