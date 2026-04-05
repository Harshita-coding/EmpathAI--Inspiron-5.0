"""
╔══════════════════════════════════════════════════════════════╗
║              EmpathAI — Final Production Version             ║
║                                                              ║
║  Features:                                                   ║
║  ✅ IMAP email reading (UNSEEN only, no duplicates)          ║
║  ✅ VADER sentiment analysis (Positive/Negative/Urgent)      ║
║  ✅ spaCy NLP (intent detection, entity extraction)          ║
║  ✅ Google Calendar API (create real events)                 ║
║  ✅ .ics fallback if Calendar API not configured             ║
║  ✅ Persistent state (survives restarts)                     ║
║  ✅ Open inbox — replies to all senders                      ║
║  ✅ Tone-aware replies                                       ║
║  ✅ Thread summarization (spaCy extractive NLP)              ║
║                                                              ║
║  Install dependencies:                                       ║
║  pip install vaderSentiment spacy google-api-python-client   ║
║               google-auth-httplib2 google-auth-oauthlib      ║
║  python -m spacy download en_core_web_sm                     ║
╚══════════════════════════════════════════════════════════════╝
"""

# ──────────────────────────────────────────────
# IMPORTS
# ──────────────────────────────────────────────
import imaplib
import smtplib
import email
import re
import json
import os
import time
import logging
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders

# NLP
import spacy
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

# Google Calendar
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# ──────────────────────────────────────────────
# LOGGING  (logs to console + empath_ai.log)
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("empath_ai.log", encoding="utf-8")
    ]
)
log = logging.getLogger("EmpathAI")

# ──────────────────────────────────────────────
# ① CONFIGURATION  — edit only this section
# ──────────────────────────────────────────────
EMAIL_ADDRESS = "bprisha123@gmail.com"        #using real gmail and app password for demo
EMAIL_PASSWORD = "nozt noma sslu hhzv"       # Gmail App Password (not your login password)

SPAM_KEYWORDS = [                              # still block obvious spam
    "unsubscribe", "click here", "buy now",
    "limited time", "free gift", "you have won",
]

CHECK_INTERVAL_SECONDS = 30                    # how often to poll inbox

# Google Calendar settings
USE_GOOGLE_CALENDAR = True                     # set False to use .ics fallback only
CALENDAR_CREDENTIALS_FILE = "credentials.json"  # downloaded from Google Cloud Console
CALENDAR_TOKEN_FILE       = "token.json"         # auto-created on first run
CALENDAR_ID               = "primary"            # "primary" = your main calendar
CALENDAR_SCOPES           = ["https://www.googleapis.com/auth/calendar"]

# Persistent state
STATE_FILE = "bot_state.json"

# ──────────────────────────────────────────────
# ② STATE MANAGEMENT  — persists across restarts
# ──────────────────────────────────────────────
def load_state() -> tuple[set, set, dict]:
    """Load replied IDs, busy slots, and conversation memory from disk."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
            return (
                set(data.get("replied_ids",         [])),
                set(data.get("busy_slots",          [])),
                data.get("conversation_memory",      {}),
            )
        except (json.JSONDecodeError, KeyError):
            log.warning("State file corrupted — starting fresh.")
    return set(), set(), {}


def save_state(replied_ids: set, busy_slots: set, conversation_memory: dict):
    """Atomically write state to disk."""
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({
            "replied_ids":         list(replied_ids),
            "busy_slots":          list(busy_slots),
            "conversation_memory": conversation_memory,
        }, f, indent=2)
    os.replace(tmp, STATE_FILE)   # atomic replace — no partial writes


# ──────────────────────────────────────────────
# ③ NLP ENGINE
#    spaCy  → intent + entity extraction
#    VADER  → sentiment / tone detection
# ──────────────────────────────────────────────
try:
    nlp = spacy.load("en_core_web_sm")
    log.info("spaCy model loaded ✅")
except OSError:
    log.error("spaCy model missing. Run: python -m spacy download en_core_web_sm")
    raise

vader = SentimentIntensityAnalyzer()

# Intent keywords — grouped semantically
MEETING_INTENT_PHRASES = [
    "meet", "meeting", "catch up", "sync", "call", "chat",
    "schedule", "book", "appointment", "get together", "connect",
    "discussion", "talk", "session", "conference",
]

GREETING_INTENT_PHRASES = [
    "hello", "hi", "hey", "good morning", "good afternoon",
    "good evening", "how are you", "what's up",
]

CANCELLATION_PHRASES = [
    "cancel", "reschedule", "can't make it", "cannot make it",
    "won't be able", "postpone", "call off",
]

URGENCY_KEYWORDS = [
    "urgent", "asap", "immediately", "right away",
    "critical", "emergency", "as soon as possible",
]


def detect_intent(text: str) -> str:
    """
    Uses spaCy tokenization + lemmatization to detect intent.
    Returns: 'meeting' | 'greeting' | 'cancellation' | 'unknown'
    """
    doc = nlp(text.lower())
    lemmas = {token.lemma_ for token in doc if not token.is_stop}
    full_text = text.lower()

    # Check multi-word phrases first
    if any(phrase in full_text for phrase in CANCELLATION_PHRASES):
        return "cancellation"

    if any(phrase in full_text for phrase in MEETING_INTENT_PHRASES):
        return "meeting"

    # Use lemmas for single-word matching (handles "meetings" → "meeting")
    meeting_lemmas  = {"meet", "schedule", "appointment", "book", "sync", "call"}
    greeting_lemmas = {"hello", "hi", "hey"}

    if lemmas & meeting_lemmas:
        return "meeting"
    if any(phrase in full_text for phrase in GREETING_INTENT_PHRASES):
        return "greeting"
    if lemmas & greeting_lemmas:
        return "greeting"

    return "unknown"


def detect_tone(text: str) -> str:
    """
    VADER compound score + urgency keyword override.
    Returns: 'Urgent' | 'Positive' | 'Negative' | 'Neutral'
    """
    if any(kw in text.lower() for kw in URGENCY_KEYWORDS):
        return "Urgent"

    scores   = vader.polarity_scores(text)
    compound = scores["compound"]

    if compound >= 0.3:
        return "Positive"
    elif compound <= -0.3:
        return "Negative"
    return "Neutral"


def tone_opener(tone: str) -> str:
    """Returns a warm, tone-matched opening sentence."""
    return {
        "Urgent":   "I understand this is time-sensitive — ",
        "Positive": "Great to hear from you! ",
        "Negative": "I'm sorry to hear that. ",
        "Neutral":  "",
    }.get(tone, "")


def extract_time_nlp(text: str) -> str | None:
    """
    Two-pass time extraction:
      Pass 1 — spaCy TIME entities
      Pass 2 — regex fallback
    Always normalises to "H:MM AM/PM" format.
    """
    doc = nlp(text)

    # Pass 1: spaCy named entities labelled TIME
    for ent in doc.ents:
        if ent.label_ == "TIME":
            normalised = _normalise_time_string(ent.text)
            if normalised:
                return normalised

    # Pass 2: regex fallback
    match = re.search(r'\b(\d{1,2}(?::\d{2})?\s?(?:am|pm))\b', text, re.IGNORECASE)
    if match:
        return _normalise_time_string(match.group(1))

    # 24-hour format
    match24 = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', text)
    if match24:
        h, m = int(match24.group(1)), int(match24.group(2))
        suffix = "AM" if h < 12 else "PM"
        return f"{h % 12 or 12}:{m:02d} {suffix}"

    return None


def _normalise_time_string(raw: str) -> str | None:
    """Convert any time string to canonical 'H:MM AM/PM' format."""
    raw = raw.strip().upper().replace(" ", "")
    if not raw:
        return None

    # Ends with AM or PM
    if raw.endswith("AM") or raw.endswith("PM"):
        suffix = raw[-2:]
        digits = raw[:-2]
        if ":" not in digits:
            digits += ":00"
        return f"{int(digits.split(':')[0])}:{digits.split(':')[1]} {suffix}"

    return None


def extract_day_nlp(text: str) -> str | None:
    """
    Extracts a meeting day using spaCy DATE entities + relative term resolution.
    Returns a full date string like "Monday, June 16" or None.
    """
    today = datetime.now()
    weekdays = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
    text_lower = text.lower()

    # Relative terms
    if "day after tomorrow" in text_lower:
        d = today + timedelta(days=2)
        return d.strftime("%A, %B %d")
    if "tomorrow" in text_lower:
        d = today + timedelta(days=1)
        return d.strftime("%A, %B %d")
    if "today" in text_lower or "tonight" in text_lower:
        return today.strftime("%A, %B %d")

    # spaCy DATE entities
    doc = nlp(text)
    for ent in doc.ents:
        if ent.label_ == "DATE":
            for day in weekdays:
                if day.lower() in ent.text.lower():
                    # Find next occurrence of this weekday
                    target = weekdays.index(day)
                    delta  = (target - today.weekday()) % 7 or 7
                    d = today + timedelta(days=delta)
                    return d.strftime("%A, %B %d")

    # Direct weekday mentions
    for day in weekdays:
        if day.lower() in text_lower:
            target = weekdays.index(day)
            delta  = (target - today.weekday()) % 7 or 7
            d = today + timedelta(days=delta)
            return d.strftime("%A, %B %d")

    return None


# ──────────────────────────────────────────────
# ④ THREAD SUMMARIZER
#    Fetches all emails in a thread by matching
#    subject, then produces an extractive summary
#    using spaCy sentence scoring (TF-IDF style).
#    Also extracts key facts: who, when, topic.
# ──────────────────────────────────────────────

def fetch_thread_bodies(mail, subject: str, sender: str) -> list[dict]:
    """
    Search inbox + sent for all emails sharing this subject line.
    Returns a list of {role, body, date} dicts — oldest first.
    """
    thread = []

    # Clean subject for search (strip Re:/Fwd: prefixes)
    clean_subject = re.sub(r'^(re|fwd|fw):\s*', '', subject.strip(), flags=re.IGNORECASE)

    for folder in ["inbox", '"[Gmail]/Sent Mail"']:
        try:
            mail.select(folder)
            # Search by subject
            status, ids = mail.search(None, f'SUBJECT "{clean_subject}"')
            if status != "OK" or not ids[0]:
                continue

            for e_id in ids[0].split():
                _, msg_data = mail.fetch(e_id, "(RFC822)")
                for part in msg_data:
                    if not isinstance(part, tuple):
                        continue
                    msg  = email.message_from_bytes(part[1])
                    body = _extract_clean_body(msg)
                    date = msg.get("date", "")
                    frm  = msg.get("from", "")
                    role = "them" if sender in frm.lower() else "me"
                    if body.strip():
                        thread.append({"role": role, "body": body, "date": date})
        except Exception:
            pass  # folder may not exist, silently skip

    # Sort chronologically by date string (best-effort)
    thread.sort(key=lambda x: x.get("date", ""))
    return thread


def _extract_clean_body(msg) -> str:
    """Extract plain text body, stripping quoted reply chains (lines starting with >)."""
    raw = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                raw = part.get_payload(decode=True).decode(errors="ignore")
                break
    else:
        raw = msg.get_payload(decode=True).decode(errors="ignore")

    # Strip quoted lines (lines starting with > or ---)
    lines = raw.splitlines()
    clean = [l for l in lines if not l.strip().startswith(">") and not l.strip().startswith("---")]
    return " ".join(clean).strip()


def summarize_thread(thread: list[dict], max_sentences: int = 4) -> str:
    """
    Extractive summarization using spaCy:
    1. Score each sentence by word frequency (TF-style)
    2. Pick the top N sentences by score
    3. Return them in original order

    Also extracts and prepends key facts (who, intent, time).
    """
    if not thread:
        return "No thread history found."

    # Combine all bodies into one text block
    full_text = " ".join(msg["body"] for msg in thread)

    if len(full_text.strip()) < 50:
        return "Thread too short to summarize."

    doc = nlp(full_text)
    sentences = [sent.text.strip() for sent in doc.sents if len(sent.text.strip()) > 15]

    if not sentences:
        return full_text[:300] + "..." if len(full_text) > 300 else full_text

    # ── Word frequency scoring (stop words excluded) ──
    word_freq: dict[str, int] = {}
    for token in doc:
        if not token.is_stop and not token.is_punct and token.text.strip():
            word = token.text.lower()
            word_freq[word] = word_freq.get(word, 0) + 1

    # Normalise frequencies
    max_freq = max(word_freq.values(), default=1)
    word_freq = {w: f / max_freq for w, f in word_freq.items()}

    # Score each sentence
    sent_scores: dict[str, float] = {}
    for sent in sentences:
        sent_doc = nlp(sent)
        score = sum(
            word_freq.get(token.text.lower(), 0)
            for token in sent_doc
            if not token.is_stop and not token.is_punct
        )
        sent_scores[sent] = score

    # Pick top N sentences, preserve original order
    top_sents = sorted(sent_scores, key=sent_scores.get, reverse=True)[:max_sentences]
    summary_sents = [s for s in sentences if s in top_sents]

    # ── Key fact extraction ──
    facts = []

    # People mentioned
    people = [ent.text for ent in doc.ents if ent.label_ == "PERSON"]
    if people:
        facts.append(f"People: {', '.join(set(people))}")

    # Times mentioned
    times = [ent.text for ent in doc.ents if ent.label_ == "TIME"]
    if times:
        facts.append(f"Times discussed: {', '.join(set(times))}")

    # Dates mentioned
    dates = [ent.text for ent in doc.ents if ent.label_ == "DATE"]
    if dates:
        facts.append(f"Dates discussed: {', '.join(set(dates))}")

    # Tone of overall thread
    tone = detect_tone(full_text)
    facts.append(f"Overall tone: {tone}")

    facts_line = " | ".join(facts) if facts else ""
    summary    = " ".join(summary_sents)

    result = ""
    if facts_line:
        result += f"[{facts_line}]\n"
    result += summary

    return result.strip()


def get_thread_summary(mail, subject: str, sender: str) -> str:
    """
    Public entry point — fetches thread and returns a clean summary string.
    Logs the summary to the activity log.
    """
    thread = fetch_thread_bodies(mail, subject, sender)
    summary = summarize_thread(thread)
    log.info(f"  📋 Thread summary ({len(thread)} message(s)):\n     {summary}")
    return summary


# ──────────────────────────────────────────────
# ⑤ SCHEDULING ENGINE
# ──────────────────────────────────────────────
def is_busy(slot: str, busy_slots: set) -> bool:
    return slot in busy_slots


def suggest_free_slot(base_time: str, busy_slots: set) -> str:
    """Suggests the next free hour after base_time, skipping busy ones."""
    try:
        base = datetime.strptime(base_time, "%I:%M %p")
    except ValueError:
        base = datetime.strptime("12:00 PM", "%I:%M %p")

    for i in range(1, 8):
        candidate = (base + timedelta(hours=i)).strftime("%-I:%M %p")
        if candidate not in busy_slots:
            return candidate

    return "a mutually convenient time"


# ──────────────────────────────────────────────
# ⑤ GOOGLE CALENDAR INTEGRATION
# ──────────────────────────────────────────────
def get_calendar_service():
    """
    Returns an authenticated Google Calendar service object.
    On first run: opens browser for OAuth consent.
    Subsequently: uses saved token.json silently.
    """
    creds = None

    if os.path.exists(CALENDAR_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(CALENDAR_TOKEN_FILE, CALENDAR_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CALENDAR_CREDENTIALS_FILE):
                log.warning(
                    "credentials.json not found. "
                    "Download it from Google Cloud Console → APIs & Services → Credentials. "
                    "Falling back to .ics attachment."
                )
                return None
            flow  = InstalledAppFlow.from_client_secrets_file(
                CALENDAR_CREDENTIALS_FILE, CALENDAR_SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open(CALENDAR_TOKEN_FILE, "w") as token:
            token.write(creds.to_json())

    return build("calendar", "v3", credentials=creds)


def create_google_calendar_event(
    service,
    summary: str,
    date_str: str,   # e.g. "Monday, June 16"
    time_str: str,   # e.g. "3:00 PM"
    attendee_email: str,
) -> str | None:
    """
    Creates a real Google Calendar event and sends an invite to attendee_email.
    Returns the event HTML link, or None on failure.
    """
    try:
        # Parse date + time into datetime objects
        current_year = datetime.now().year
        dt_str  = f"{date_str} {current_year} {time_str}"
        dt_fmt  = "%A, %B %d %Y %I:%M %p"
        start   = datetime.strptime(dt_str, dt_fmt)
        end     = start + timedelta(hours=1)

        event = {
            "summary": summary,
            "start":   {"dateTime": start.isoformat(), "timeZone": "Asia/Kolkata"},
            "end":     {"dateTime": end.isoformat(),   "timeZone": "Asia/Kolkata"},
            "attendees": [{"email": attendee_email}],
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "email", "minutes": 60},
                    {"method": "popup", "minutes": 15},
                ],
            },
        }

        created = service.events().insert(
            calendarId=CALENDAR_ID,
            body=event,
            sendUpdates="all",   # sends Google Calendar invite to attendee
        ).execute()

        log.info(f"📅 Google Calendar event created: {created.get('htmlLink')}")
        return created.get("htmlLink")

    except Exception as e:
        log.error(f"Google Calendar error: {e}")
        return None


def create_ics_fallback(date_str: str, time_str: str) -> str | None:
    """
    Creates a .ics file as fallback when Google Calendar API is unavailable.
    Returns filename, or None on failure.
    """
    try:
        current_year = datetime.now().year
        dt_str = f"{date_str} {current_year} {time_str}"
        start  = datetime.strptime(dt_str, "%A, %B %d %Y %I:%M %p")
        end    = start + timedelta(hours=1)
        stamp  = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

        ics = (
            "BEGIN:VCALENDAR\n"
            "VERSION:2.0\n"
            "PRODID:-//EmpathAI//EN\n"
            "BEGIN:VEVENT\n"
            f"DTSTAMP:{stamp}\n"
            f"DTSTART:{start.strftime('%Y%m%dT%H%M%S')}\n"
            f"DTEND:{end.strftime('%Y%m%dT%H%M%S')}\n"
            "SUMMARY:Meeting Scheduled via EmpathAI\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        filename = "invite.ics"
        with open(filename, "w") as f:
            f.write(ics)
        return filename

    except Exception as e:
        log.error(f".ics creation failed: {e}")
        return None


# ──────────────────────────────────────────────
# ⑥ EMAIL ENGINE  (IMAP read + SMTP send)
# ──────────────────────────────────────────────
def send_reply(to: str, subject: str, body: str, ics_file: str | None = None):
    """Send reply email, optionally attaching a .ics calendar file."""
    msg = MIMEMultipart()
    msg["From"]    = EMAIL_ADDRESS
    msg["To"]      = to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    if ics_file and os.path.exists(ics_file):
        with open(ics_file, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{ics_file}"')
        msg.attach(part)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        server.sendmail(EMAIL_ADDRESS, to, msg.as_string())

    log.info(f"✅ Reply sent → {to}")


def get_email_body(msg) -> str:
    """Safely extract plain-text body from an email message object."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                return part.get_payload(decode=True).decode(errors="ignore")
    return msg.get_payload(decode=True).decode(errors="ignore")


# ──────────────────────────────────────────────
# ⑦ REPLY LOGIC  — the brain of the bot
# ──────────────────────────────────────────────
def build_reply(
    sender:               str,
    subject:              str,
    body:                 str,
    busy_slots:           set,
    conversation_memory:  dict,
    calendar_service,
    thread_summary:       str | None = None,
) -> tuple[str | None, set, dict]:
    """
    Decides what reply to send based on NLP intent + sentiment.
    thread_summary: extractive summary of the full thread (if available).
    Enriches NLP context so even short follow-up emails are understood correctly.
    Returns ((reply_text, ics_file), updated_busy_slots, updated_conversation_memory).
    First element is None if no reply should be sent.
    """
    # Enrich NLP context with thread summary if available
    # e.g. a follow-up saying just "ok, 5pm?" is understood because the thread
    # already established it's a meeting request about Tuesday
    content    = subject + " " + body
    if thread_summary:
        content = content + " " + thread_summary

    intent     = detect_intent(content)
    tone       = detect_tone(content)
    opener     = tone_opener(tone)
    time_found = extract_time_nlp(content)
    day_found  = extract_day_nlp(content)

    log.info(f"  Intent: {intent} | Tone: {tone} | Time: {time_found} | Day: {day_found}")
    if thread_summary:
        log.info(f"  🧵 Thread context used: {thread_summary[:120]}...")

    reply      = None
    ics_to_attach = None

    # ── CANCELLATION ──────────────────────────────────────────
    if intent == "cancellation":
        if sender in conversation_memory:
            del conversation_memory[sender]
        reply = f"{opener}No problem at all — the meeting has been cancelled. Feel free to reach out when you'd like to reschedule."

    # ── ONGOING CONVERSATION (waiting for time/confirmation) ──
    elif sender in conversation_memory:
        context = conversation_memory[sender]

        if time_found:
            slot = f"{day_found or 'today'} @ {time_found}"

            if not is_busy(time_found, busy_slots):
                # Confirm and book
                reply = (
                    f"{opener}Perfect — meeting confirmed for "
                    f"{day_found + ' at ' if day_found else ''}{time_found}! "
                    f"I've sent a calendar invite. See you then! 😊"
                )
                busy_slots.add(time_found)

                # Create calendar event
                if USE_GOOGLE_CALENDAR and calendar_service:
                    create_google_calendar_event(
                        calendar_service,
                        f"Meeting with {sender}",
                        day_found or datetime.now().strftime("%A, %B %d"),
                        time_found,
                        sender,
                    )
                else:
                    ics_to_attach = create_ics_fallback(
                        day_found or datetime.now().strftime("%A, %B %d"),
                        time_found,
                    )

                del conversation_memory[sender]

            else:
                alt = suggest_free_slot(time_found, busy_slots)
                reply = (
                    f"{opener}I'm already booked at {time_found}. "
                    f"Would {alt} work for you instead?"
                )
                conversation_memory[sender]["waiting_for"] = "reschedule_confirmation"

        else:
            # Still waiting for a time
            reply = f"{opener}Could you let me know what time works best for you?"

    # ── FRESH MEETING REQUEST ──────────────────────────────────
    elif intent == "meeting":

        if time_found:
            if not is_busy(time_found, busy_slots):
                reply = (
                    f"{opener}{'Meeting confirmed for ' + day_found + ' at ' if day_found else 'Meeting confirmed at '}"
                    f"{time_found}! I'll send over a calendar invite. 📅"
                )
                busy_slots.add(time_found)

                if USE_GOOGLE_CALENDAR and calendar_service:
                    create_google_calendar_event(
                        calendar_service,
                        f"Meeting with {sender}",
                        day_found or datetime.now().strftime("%A, %B %d"),
                        time_found,
                        sender,
                    )
                else:
                    ics_to_attach = create_ics_fallback(
                        day_found or datetime.now().strftime("%A, %B %d"),
                        time_found,
                    )

            else:
                alt = suggest_free_slot(time_found, busy_slots)
                reply = (
                    f"{opener}I'm not available at {time_found}. "
                    f"Would {alt} suit you instead?"
                )
                conversation_memory[sender] = {"waiting_for": "reschedule_confirmation"}

        elif day_found:
            reply = (
                f"{opener}{day_found} works for me! "
                f"What time were you thinking?"
            )
            conversation_memory[sender] = {"waiting_for": "time", "day": day_found}

        else:
            reply = f"{opener}Sure, I'd love to meet! What day and time works best for you?"
            conversation_memory[sender] = {"waiting_for": "day_and_time"}

    # ── GREETING ──────────────────────────────────────────────
    elif intent == "greeting":
        reply = f"{opener}Hi! Great to hear from you. How can I help?"

    # ── NO RECOGNISED INTENT ──────────────────────────────────
    else:
        log.info("  → No matching intent, skipping.")
        return None, busy_slots, conversation_memory

    return (reply, ics_to_attach), busy_slots, conversation_memory


# ──────────────────────────────────────────────
# ⑧ MAIN POLLING LOOP
# ──────────────────────────────────────────────
def check_inbox(replied_ids: set, busy_slots: set, conversation_memory: dict, calendar_service):
    """Connect to inbox, process all UNSEEN emails, send replies."""
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        mail.select("inbox")
    except imaplib.IMAP4.error as e:
        log.error(f"IMAP login failed: {e}")
        return replied_ids, busy_slots, conversation_memory

    status, messages = mail.search(None, "UNSEEN")
    if status != "OK" or not messages[0]:
        log.info("📭 No new emails.")
        mail.logout()
        return replied_ids, busy_slots, conversation_memory

    ids = messages[0].split()
    log.info(f"📬 {len(ids)} new email(s) found.")

    for e_id in ids:
        uid = e_id.decode()

        if uid in replied_ids:
            log.info(f"  ⏭ Already replied to UID {uid}")
            continue

        _, msg_data = mail.fetch(e_id, "(RFC822)")
        replied = False

        for part in msg_data:
            if not isinstance(part, tuple):
                continue

            msg      = email.message_from_bytes(part[1])
            subject  = str(msg.get("subject", "(No Subject)"))
            from_raw = msg.get("from", "")

            raw_match = re.findall(r'<(.+?)>', from_raw)
            sender    = (raw_match[0] if raw_match else from_raw).strip().lower()

            log.info(f"\n  📩 From: {sender}")
            log.info(f"     Subject: {subject}")

            # ── Spam filter (blocks obvious bulk mail, allows everyone else) ──
            body_preview = get_email_body(msg).lower()
            if any(kw in subject.lower() + " " + body_preview for kw in SPAM_KEYWORDS):
                log.info("  ⏭ Spam detected — skipped.")
                continue

            body = body_preview  # already extracted above

            # ── Thread summarization ──
            # Summarize when: subject has Re: prefix (reply chain)
            # OR sender already has an ongoing conversation
            is_reply_thread = (
                subject.lower().startswith("re:") or
                sender in conversation_memory
            )
            thread_summary = None
            if is_reply_thread:
                thread_summary = get_thread_summary(mail, subject, sender)

            result, busy_slots, conversation_memory = build_reply(
                sender, subject, body, busy_slots, conversation_memory,
                calendar_service, thread_summary
            )

            if result is None:
                continue

            reply_text, ics_file = result
            if not reply_text:
                continue

            send_reply(sender, "Re: " + subject, reply_text, ics_file)

            mail.store(e_id, '+FLAGS', '\\Seen')
            replied_ids.add(uid)
            save_state(replied_ids, busy_slots, conversation_memory)
            replied = True

        if not replied:
            log.info(f"  ⏭ UID {uid}: no reply sent (skipped or filtered).")

    mail.logout()
    return replied_ids, busy_slots, conversation_memory


# ──────────────────────────────────────────────
# ⑨ ENTRY POINT
# ──────────────────────────────────────────────
if __name__ == "__main__":
    log.info("━" * 60)
    log.info("  EmpathAI — Final Version Starting")
    log.info("━" * 60)

    # Load persistent state
    replied_ids, busy_slots, conversation_memory = load_state()
    log.info(f"  Loaded {len(replied_ids)} replied IDs | {len(busy_slots)} busy slots")

    # Initialise Google Calendar (optional — falls back gracefully)
    calendar_service = None
    if USE_GOOGLE_CALENDAR:
        log.info("  Connecting to Google Calendar...")
        calendar_service = get_calendar_service()
        if calendar_service:
            log.info("  Google Calendar connected ✅")
        else:
            log.warning("  Google Calendar unavailable — will use .ics fallback.")

    log.info(f"  Polling inbox every {CHECK_INTERVAL_SECONDS}s. Ctrl+C to stop.\n")

    while True:
        try:
            log.info(f"🔍 Checking inbox [{datetime.now().strftime('%H:%M:%S')}]")
            replied_ids, busy_slots, conversation_memory = check_inbox(
                replied_ids, busy_slots, conversation_memory, calendar_service
            )
        except KeyboardInterrupt:
            log.info("\n👋 EmpathAI stopped by user.")
            break
        except Exception as e:
            log.error(f"Unexpected error: {e}", exc_info=True)
        finally:
            time.sleep(CHECK_INTERVAL_SECONDS)
