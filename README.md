# EmpathAI--Inspiron-5.0
An AI-powered email assistant that reads incoming emails, understands intent and tone, and automatically responds with context-aware, human-like replies — while also scheduling meetings seamlessly.

# Features

✨ Smart Email Processing

Reads UNSEEN emails only (no duplicate replies)
Filters spam using keyword detection

🧠 Natural Language Understanding

Uses spaCy for:
Intent detection (meeting, greeting, cancellation)
Entity extraction (date, time)
Uses VADER for:
Tone detection (Positive, Negative, Neutral, Urgent)

📅 Automated Scheduling

Detects meeting requests
Checks availability
Suggests alternative time slots
Creates:
✅ Google Calendar events (if configured)
✅ .ics file fallback

💬 Context-Aware Replies

Maintains conversation memory
Handles follow-ups like “5pm works”
Adjusts tone dynamically

🧵 Thread Intelligence (Advanced)

Fetches full email threads
Generates extractive summaries
Extracts key info:
People
Dates
Times
Overall tone

💾 Persistent State

Remembers:
Replied emails
Busy time slots
Ongoing conversations

# Tech Stack
Python
spaCy (NLP engine)
VADER Sentiment Analysis
IMAP & SMTP (Email handling)
Google Calendar API
JSON (state persistence)

# Installation
pip install vaderSentiment spacy google-api-python-client \
            google-auth-httplib2 google-auth-oauthlib

python -m spacy download en_core_web_sm

# Setup
1. Gmail App Password

Generate an App Password and update:

EMAIL_ADDRESS = "your_email@gmail.com"
EMAIL_PASSWORD = "your_app_password"

2. Google Calendar (Optional but Recommended)
Go to Google Cloud Console
Enable Google Calendar API
Download credentials.json
Place it in project root

# Running the Project
Run the code in the terminal

The bot will:
Check inbox every 30 seconds
Process new emails
Reply automatically

# Workflow:
📥 Fetch unread emails using IMAP
🔍 Analyze email using NLP:
Detect intent
Detect tone
Extract time & date
🧵 (Optional) Summarize email thread for context
🤖 Generate smart reply
📤 Send response via SMTP
📅 Schedule meeting if required
💾 Save state for continuity

# How It Works
Fetch unread emails using IMAP
Analyze email using NLP:
Detect intent
Detect tone
Extract time & date
(Optional) Summarize email thread for context
Generate smart reply
Send response via SMTP
Schedule meeting if required
Save state for continuity

# Important Notes
⚠️ Do NOT upload credentials.json or passwords to GitHub
Use .gitignore to protect sensitive files
This project uses Gmail App Password (not your real password)

# Use Cases
Automated email assistant
Smart scheduling bot
Productivity tool
AI-powered inbox management

# 🔮 Future Improvements
Web dashboard UI
Multi-user support
Better spam detection (ML-based)
Voice assistant integration
Learning user preferences over time


# Authors
Harshita Jain 
Prisha Banerjee
Aarya Pargaonkar
Purva Kawathe
Built with caffeine, chaos, and curiosity during a hackathon 🧃⚡

# 🏁 Final Note

EmpathAI isn’t just replying to emails.
It’s quietly decoding intent, reading between the lines, and turning messy human communication into structured action.

Like a polite, invisible intern… who never sleeps.
