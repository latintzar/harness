"""Rocoto backend config — all read from the environment (see deploy/.env)."""
from __future__ import annotations

import os


def _env(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


def _bool(key: str, default: bool) -> bool:
    v = (os.getenv(key) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _int(key: str, default: int) -> int:
    """Tolerant int env parse. A typo ("48h", "") never crashes import — it falls back to `default`
    and logs a warning (matching _bool's tolerance). Import must never take the whole service down."""
    v = (os.getenv(key) or "").strip()
    if not v:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        import logging
        logging.getLogger("rocoto.config").warning(
            "config: %s=%r is not a valid int — using default %r", key, v, default)
        return default


def _float(key: str, default: float) -> float:
    """Tolerant float env parse — same rationale as _int (never crash import on a bad value)."""
    v = (os.getenv(key) or "").strip()
    if not v:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        import logging
        logging.getLogger("rocoto.config").warning(
            "config: %s=%r is not a valid float — using default %r", key, v, default)
        return default


DEEPGRAM_API_KEY = _env("DEEPGRAM_API_KEY")
# Transcription language for inbound voice notes. English-first for the demo (nova-3 still reads
# code-switched Spanish product words). Set DEEPGRAM_LANGUAGE=multi to restore multilingual mode.
DEEPGRAM_LANGUAGE = _env("DEEPGRAM_LANGUAGE", "en")
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")

# ──────────── Supabase auth (identity provider for the onboarding sign-in) ────────────
# Supabase is the IdP (email+password / Google); the lead row is the app-side identity, married by
# leads.supabase_user_id. Every environment explicitly selects its project URL and anon key.
# The service-role secret belongs only in that environment's private backend configuration.
# Absent configuration disables remote auth.
SUPABASE_URL = _env("SUPABASE_URL")
SUPABASE_ANON_KEY = _env("SUPABASE_ANON_KEY")
SUPABASE_SERVICE_ROLE_KEY = _env("SUPABASE_SERVICE_ROLE_KEY")

# ElevenLabs Conversational AI — per-call voice autoreply (default OFF for everyone).
# A call only bridges to the agent when BOTH the per-customer DB toggle is true AND a key
# is configured. Missing key → we silently keep the Polly + Deepgram-live path. The Luis
# agent (Spanish) must have its audio format set to ulaw_8000 to match Twilio media streams.
ELEVENLABS_API_KEY = _env("ELEVENLABS_API_KEY")
# HMAC secret ElevenLabs signs its post-call webhook with (ElevenLabs-Signature header). Verifies
# POST /api/agent/postcall. Unset → the endpoint accepts unsigned (dev); set in prod.
ELEVENLABS_WEBHOOK_SECRET = _env("ELEVENLABS_WEBHOOK_SECRET")
ELEVENLABS_AGENT_ID = _env("ELEVENLABS_AGENT_ID", "agent_4301kvee7jtjefpsczmqgc6qj0ht")
ELEVENLABS_WS_BASE = _env("ELEVENLABS_WS_BASE", "wss://api.elevenlabs.io")
# Native voice mode (mic → Deepgram STT → fast LLM → ElevenLabs TTS → playback). The TTS voice + the
# low-latency model used for spoken turns. eleven_flash_v2_5 keeps TTS latency low.
ELEVENLABS_TTS_VOICE = _env("ELEVENLABS_TTS_VOICE", "EXAVITQu4vr4xnSDxMaL")  # 'Sarah' — warm, neutral
# (B3, 2026-07-26) ELEVENLABS_DEMO_AGENT_ID is DELETED. There is ONE voice mind — the Custom-LLM
# runtime agent registered by voice_toggle.py. A separate discovery/demo ConvAI agent was a SECOND
# MIND: on 2026-07-26 it answered live calls with the demo greeting while our mind was dead, turning a
# hard crash into mediocre behaviour nobody noticed. No fallback agent id is read anywhere.
VOICE_CHAT_MODEL = _env("VOICE_CHAT_MODEL", "claude-haiku-4-5-20251001")     # fast turns for low latency

# Post-call 2-channel diarization. When true, the call is ALSO recorded dual-channel server-side
# and re-transcribed with Deepgram multichannel (Hermes-style: channel 0 = caller, 1 = our side).
# DEFAULT OFF: recording every call is a two-party-consent / GDPR concern in Spain, and keeping it
# off makes the OFF/Polly path byte-identical to pre-call-backend behaviour. Enable per box
# (VOICE_POSTCALL_DIARIZE=true) once a spoken recording-consent line is in the greeting — the
# feature is fully built and ready behind this flag.
VOICE_POSTCALL_DIARIZE = _bool("VOICE_POSTCALL_DIARIZE", False)

TWILIO_ACCOUNT_SID = _env("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = _env("TWILIO_AUTH_TOKEN")
# Twilio API Key (SK.../secret) — PREFERRED for REST auth when set (survives auth-token rotation).
# Webhook-signature validation (RequestValidator) still uses TWILIO_AUTH_TOKEN — API keys can't sign webhooks.
TWILIO_API_KEY_SID = _env("TWILIO_API_KEY_SID")
TWILIO_API_KEY_SECRET = _env("TWILIO_API_KEY_SECRET")


def twilio_rest_auth() -> tuple[str, str]:
    """(username, password) for Twilio REST basic auth. API key (SK+secret) when configured, else
    account SID + auth token. Twilio REST accepts either; API keys are the rotation-safe default."""
    if TWILIO_API_KEY_SID and TWILIO_API_KEY_SECRET:
        return (TWILIO_API_KEY_SID, TWILIO_API_KEY_SECRET)
    return (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


# WhatsApp sender on Twilio, "whatsapp:+E.164" form.
ROCOTO_WHATSAPP_FROM = _env("ROCOTO_WHATSAPP_FROM", "whatsapp:+15559193021")
# Voice caller-ID for OUTBOUND calls (operator taps "Call" → Rocoto dials the customer FROM this
# number, so caller-ID is the business and the call rides the same bridge/recording as inbound).
# Must be a Twilio voice-capable number we own; falls back to the WhatsApp sender's E.164.
TWILIO_VOICE_FROM = _env("TWILIO_VOICE_FROM") or ROCOTO_WHATSAPP_FROM.replace("whatsapp:", "")
# DEPRECATED, and deliberately no longer read on the verification path. Every Twilio webhook is
# signature-checked, always; the one bypass is ROCOTO_ALLOW_UNSIGNED_WEBHOOKS (below), which prod
# ignores. Kept only so an old .env carrying VALIDATE_TWILIO=false cannot silently open the door.
VALIDATE_TWILIO = _bool("VALIDATE_TWILIO", True)

# ── The WhatsApp RAIL SWITCH (2026-07-21, the 63112 outage): 'twilio' (BSP, default) | 'cloud'
# (Meta Cloud API direct on OUR WABA — whatsapp_cloud.py). Flipping to 'cloud' requires the number
# to be REGISTERED to us (whatsapp_cloud.register_number — a deliberate, Peter-gated migration step;
# a number holds ONE registration, so this takes it off Twilio). All sends route through
# twilio_wa.send_whatsapp regardless — the switch lives inside it. ──
ROCOTO_WA_RAIL = _env("ROCOTO_WA_RAIL", "cloud")   # WhatsApp = Meta Cloud ONLY (Twilio WA rail retired 2026-07-23); telephony still Twilio
WA_CLOUD_TOKEN = _env("WA_CLOUD_TOKEN", "")                      # system-user token (whatsapp_* scopes)
WA_CLOUD_PHONE_ID = _env("WA_CLOUD_PHONE_ID", "1144962938711312")  # +1 555 919 3021 ("Sancho y Panza")
WA_CLOUD_WABA_ID = _env("WA_CLOUD_WABA_ID", "3417832945040413")
WA_CLOUD_VERIFY_TOKEN = _env("WA_CLOUD_VERIFY_TOKEN", "")        # webhook GET-handshake secret

# ── EMBEDDED SIGNUP (2026-08-17) — how a CUSTOMER connects their own number ─────────────────────
# The three values the Connect flow needs. `WA_CLOUD_APP_ID` and `WA_ES_CONFIG_ID` are public (they
# ride in the browser); `WA_CLOUD_APP_SECRET` never leaves the box — it is one half of the
# code→token exchange, and a leaked app secret is every customer's WhatsApp at once.
WA_CLOUD_APP_ID = _env("WA_CLOUD_APP_ID", "")
WA_CLOUD_APP_SECRET = _env("WA_CLOUD_APP_SECRET", "")
WA_ES_CONFIG_ID = _env("WA_ES_CONFIG_ID", "")
# Meta's own v4 page writes `feature_type` while its implementation snippet writes `featureType`.
# That inconsistency is in THEIR docs, so the client sends both spellings rather than betting.
WA_ES_COEXISTENCE_FEATURE = "whatsapp_business_app_onboarding"

# ── CALLS: what a caller is told, and where the rest of it lives ───────────────────────────────
# The privacy URL is part of a LEGAL NOTICE spoken to a customer, so it is configuration rather
# than a literal buried in a sentence: if the page moves, the notice must move with it.
PRIVACY_URL = _env("ROCOTO_PRIVACY_URL", "soyrocoto.com/privacidad")
BRAND_NAME = _env("ROCOTO_BRAND_NAME", "Rocoto")
# Recording is OFF until a deployment turns it on. Recording somebody's customers by default is
# not a default anyone should be able to arrive at by omission — it is a decision with an AEPD
# balancing test behind it (legitimate interest, documented BEFORE the first recording).
CALL_RECORDING_ENABLED = _env("ROCOTO_CALL_RECORDING", "false").lower() in ("1", "true", "yes")

# THE HUMAN HOLD (Raquel, 2026-08-19): how long Rocoto stays quiet on a thread after the boss's own
# message. 30 min covers «enderezar la conversación» without a forgotten takeover killing the thread
# — expiry IS the handback, and every further boss message re-bumps it.
HUMAN_HOLD_MINUTES = int(_env("ROCOTO_HUMAN_HOLD_MINUTES", "30") or "30")
# X-Hub-Signature-256 on the Meta rail. The WABA lives under the same Meta app whose secret is
# WA_CLOUD_APP_SECRET, so that is the default; FB_APP_SECRET is read first for a deployment that
# names it separately. Production's env carries WA_CLOUD_APP_SECRET and no FB_APP_SECRET (audit 2026-09-07).
FB_APP_SECRET = _env("FB_APP_SECRET", "") or WA_CLOUD_APP_SECRET

# ──────────── Landing "See it live" demo call (see demo_call.py) ────────────
# Places a REAL Twilio call to a website visitor and streams the live transcript to their browser.
# LANDS DARK: everything below is inert until DEMO_CALL_ENABLED=true AND the ONE runtime voice mind
# is available (B3 — there is no demo agent id any more).
DEMO_CALL_ENABLED = _bool("DEMO_CALL_ENABLED", False)
# Caller ID for the demo call. A verified +34 (ES mobile) gets picked up; falls back to the WhatsApp
# sender's E.164 when unset. Peter's verified caller ID: +34627506263.
DEMO_CALLER_ID = _env("DEMO_CALLER_ID") or TWILIO_VOICE_FROM
# +1 fallback caller ID retried when the primary CLI is blocked/failed by the carrier.
DEMO_CALLER_ID_FALLBACK = _env("DEMO_CALLER_ID_FALLBACK") or ROCOTO_WHATSAPP_FROM.replace("whatsapp:", "")
DEMO_MAX_SECONDS = int(_env("DEMO_MAX_SECONDS", "300") or "300")   # hard cap per demo call
DEMO_DAILY_CAP = int(_env("DEMO_DAILY_CAP", "25") or "25")         # global demo calls / 24h (cost cap)
DEMO_PHONE_CAP = int(_env("DEMO_PHONE_CAP", "9999") or "9999")     # per-phone demo calls / 24h (loose; global cap is the real backstop)
DEMO_AUDIO_GAIN = float(_env("DEMO_AUDIO_GAIN", "2.6") or "2.6")   # amplify the agent's TTS (ConvAI output is quiet on phones)
DEMO_ALLOWED_COUNTRIES = _env("DEMO_ALLOWED_COUNTRIES", "")        # comma list of ISO region codes; empty = any country
# Region-matched caller IDs: a UK handset only rings from a UK number, an IT one from an IT number, etc.
# Format: "GB:+447460083152,ES:+34627506263,IT:+39...". Falls back to DEMO_CALLER_ID for unmapped regions.
def _parse_caller_ids(s: str) -> dict:
    m = {}
    for part in (s or "").split(","):
        if ":" in part:
            k, v = part.split(":", 1)
            k, v = k.strip().upper(), v.strip()
            if k and v:
                m[k] = v
    return m
DEMO_CALLER_IDS = _parse_caller_ids(_env("DEMO_CALLER_IDS", ""))
# Founder / QA numbers that bypass the short-window + per-phone caps (so we can test freely).
# Comma-separated, any format — matched on digits only. Still subject to the global daily cost cap.
DEMO_TEST_NUMBERS = _env("DEMO_TEST_NUMBERS", "")
# wa.me number for the post-call CTA (digits only). Defaults to the WhatsApp sender.
DEMO_WA_NUMBER = _env("DEMO_WA_NUMBER") or ROCOTO_WHATSAPP_FROM.replace("whatsapp:+", "").replace("whatsapp:", "")
DEMO_NOTIFY_PHONE = _env("DEMO_NOTIFY_PHONE")                      # Peter's WhatsApp for lead pings
DEMO_NOTIFY_EMAIL = _env("DEMO_NOTIFY_EMAIL")    # explicitly configured operator recipient
# WHERE THE LEAD ALERTS GO (brain/lead_notify.py, 2026-08-14): the /precios calculator submission and
# every new account. HOW IT RELATES TO DEMO_NOTIFY_EMAIL, which is right above it and is NOT the same
# thing: that one belongs to the landing's «See it live» demo CALL — demo_call.py and brain/wa_watch.py
# ping it the moment a stranger's call ends, and it is paired with DEMO_NOTIFY_PHONE so the two
# channels of that one feature stay together. THIS one is the commercial funnel, which has a different
# life: it will eventually go to a shared sales inbox, or to a CRM address, while the demo ping stays
# on Peter's own phone. Separating them now means that day is one env var and not a code change.
# It DEFAULTS to DEMO_NOTIFY_EMAIL so the box needs no new value to start alerting, and so the address
# Peter reads is written down in exactly one place.
NOTIFY_EMAIL = _env("NOTIFY_EMAIL") or DEMO_NOTIFY_EMAIL
DEMO_TURNSTILE_SECRET = _env("DEMO_TURNSTILE_SECRET")             # optional Cloudflare Turnstile
# CORS origins allowed to hit /api/demo/* directly (the landing domains). Comma list.
DEMO_ALLOWED_ORIGINS = _env(
    "DEMO_ALLOWED_ORIGINS",
    "https://soyrocoto.com,https://www.soyrocoto.com,https://sanchoypanza.com",
)
# WhatsApp intro template content SIDs (from the Twilio Content API, once approved).
WA_TEMPLATE_INTRO_ES = _env("WA_TEMPLATE_INTRO_ES")
WA_TEMPLATE_INTRO_EN = _env("WA_TEMPLATE_INTRO_EN")
# Inbound WhatsApp lead flow (Marco talks to strangers). Own flag, off by default (WP3b).
DEMO_WA_LEADS_ENABLED = _bool("DEMO_WA_LEADS_ENABLED", False)

# Local Postgres on the prod box (co-located with the backend). Prod's real DB URL comes from the
# env (unchanged); this default only affects fresh/dev.
DATABASE_URL = _env("DATABASE_URL", "postgresql://rocoto@localhost:5432/rocoto")
# The EMPLOYEE-RUNTIME pool connects as the least-privilege, NON-owner role `rocoto_rt` so that
# FORCE ROW LEVEL SECURITY actually binds it (the owner/superuser bypasses RLS — SPEC §12.2). Set
# this to the rocoto_rt DSN at come-alive. If unset it falls back to DATABASE_URL (the owner) and
# db.init() logs a LOUD warning that the RLS wall is app-code-only until it is repointed.
RUNTIME_DATABASE_URL = _env("RUNTIME_DATABASE_URL")
# ROCOTO_REQUIRE_RLS: make that fallback FATAL instead of a warning. With it on, db.init() refuses to
# boot unless the runtime pool is a genuinely separate, non-owner role — so "the RLS wall is
# app-code-only" cannot be a state the box quietly runs in for months. Off by default (dev, tests,
# and the come-alive step that creates the roles); ON on the box, per the deploy note.
REQUIRE_RLS = _bool("ROCOTO_REQUIRE_RLS", False)

# Bearer key the Android app sends on every /api/* and /media call. If unset, open (dev only).
ROCOTO_API_KEY = _env("ROCOTO_API_KEY")

# ──────────── Owner / RBAC + test-mode (TEST-MODE-AND-ACCESS spec §3, §6) ────────────
def _lower_set(key: str, default: str = "") -> "frozenset[str]":
    """Parse a comma-separated env allowlist into a lowercased, trimmed set. Owner/admin elevation is
    EXACT-match against these (never a domain wildcard — a one-char domain bug is a full privilege
    escalation, spec §2.5); resolve_role lowercases the incoming email, so the list must be lower too."""
    raw = _env(key, default)
    return frozenset(p.strip().lower() for p in raw.split(",") if p.strip())


# The single master owner (superuser). Checked first in resolve_role and auto-included in the owner set.
# NO DEFAULT, deliberately: who owns this box is a deployment fact, and a source default is a standing
# grant that follows the code into every environment it is copied to (the old one shipped four
# addresses, two of them on domains the company does not use — an owner allowlist nobody could revoke
# by editing an .env). Unset here means resolve_role elevates nobody; production is covered by
# assert_prod_ready(), which refuses to boot with an empty owner set.
MASTER_OWNER_EMAIL = _env("MASTER_OWNER_EMAIL").lower()
# Explicit OWNER allowlist (full data + test tooling). Only a VERIFIED email elevates (Supabase email
# confirmation is off → an unverified claimed address must never elevate; owners sign in with Google).
OWNER_EMAILS = _lower_set("OWNER_EMAILS") | (
    {MASTER_OWNER_EMAIL} if MASTER_OWNER_EMAIL else frozenset())
# Optional ADMIN allowlist (elevated but not owner) — empty for now; add addresses in the box .env.
ADMIN_EMAILS = _lower_set("ADMIN_EMAILS", "")
# Master switch for the /api/test/* seeder endpoints (land-dark). Owner-gated regardless. Safe to leave
# TRUE on the live box: every wipe is WHERE is_test=true, allowlisted + count-verified (spec §5).
TEST_MODE_ENABLED = _bool("TEST_MODE_ENABLED", False)

# ──────────── Catchment / delivery-area step — FREE map stack (no Mapbox) ────────────
# The onboarding catchment map is Leaflet + CARTO Positron tiles + Photon geocoding — all KEYLESS and
# called directly from the browser (map + search light up immediately, no client token). Each covered
# neighbourhood renders as a simple map PIN at its centre — no drive-time isochrones, no ORS, no key.

# Public callback/media origin; deployments set their own reachable backend origin.
PUBLIC_BASE_URL = _env("PUBLIC_BASE_URL", "http://127.0.0.1:8092")
# NOTE: /opt/rocoto/media path is infra (separate migration); only the env var NAME is renamed.
MEDIA_DIR = _env("ROCOTO_MEDIA_DIR", "/opt/rocoto/media")

# ── Rocoto onboarding / CRM / waitlist (brain/onboarding.py + brain/waitlist.py). ──
# SINGLE canonical definition (a duplicate block below used to re-blank ROCOTO_CAL_SLUG → '' and
# break booking; de-duped 2026-07-08). All env-overridable; VALUES for secrets live in .env.local /
# the prod /opt/rocoto/.env, NEVER committed.
# Public site the onboarding emails + wizard links point at (distinct from PUBLIC_BASE_URL, which is
# the backend origin Twilio signs webhooks against). onboarding.py reads THIS one.
ONBOARD_PUBLIC_BASE_URL = _env("ONBOARD_PUBLIC_BASE_URL", "https://soyrocoto.com")
# Resend From for onboarding mail. soyrocoto.com is now Resend-VERIFIED, so default there.
ONBOARD_EMAIL_FROM = _env("ONBOARD_EMAIL_FROM", "Rocoto <hello@soyrocoto.com>")
# Reply-To on onboarding mail (a human inbox distinct from the noreply-style sender).
ONBOARD_EMAIL_REPLY_TO = _env("ONBOARD_EMAIL_REPLY_TO", "hello@soyrocoto.com")
# cal.com booking: full "<username>/<event-slug>" for the embed. Account = leo-casa (the API key's user).
ROCOTO_CAL_SLUG = _env("ROCOTO_CAL_SLUG", "leo-casa/rocoto-alta")
# cal.com event-type id for the API v2 booking calls (brain/calbook.py). rocoto-alta = 6252480.
ROCOTO_CAL_EVENT_ID = int(_env("ROCOTO_CAL_EVENT_ID", "6252480") or "6252480")
# cal.com API v2 credentials — VALUES live in .env.local / the prod env, NEVER committed. The API key
# works now; the OAuth Platform client (id/secret) is created but pending approval — API key until then.
ROCOTO_CAL_API_KEY = _env("ROCOTO_CAL_API_KEY", "")
ROCOTO_CAL_CLIENT_ID = _env("ROCOTO_CAL_CLIENT_ID", "")
ROCOTO_CAL_CLIENT_SECRET = _env("ROCOTO_CAL_CLIENT_SECRET", "")
# Cal webhook HMAC secret, selected independently for each environment.
ROCOTO_CAL_WEBHOOK_SECRET = _env("ROCOTO_CAL_WEBHOOK_SECRET")
# Shared secret the ElevenLabs ConvAI server-tools (/api/agent/*) + post-call webhook send as a
# bearer/HMAC. When unset the agent endpoints stay OPEN in dev but require it in prod (see app.py).
ROCOTO_AGENT_TOOL_SECRET = _env("ROCOTO_AGENT_TOOL_SECRET", "")
# Founder-programme setup-fee cohort size (informational; the charge itself is OFF for now).
ONBOARD_FIRST_N = int(_env("ONBOARD_FIRST_N", "20") or "20")
# The post-upload transcript↔SKU understand pass (brain/understand.py) — Opus 4.8 (Peter's call).
UNDERSTAND_MODEL = _env("UNDERSTAND_MODEL", "claude-opus-4-8")
# The catalogue file → SKU rows parse (brain/skuparse.py) — Sonnet 4.6.
SKUPARSE_MODEL = _env("SKUPARSE_MODEL", "claude-sonnet-4-6")
# The fast catalogue legit-check gate + the silent 18↔80 numeric self-check — Haiku.
ONBOARD_GATE_MODEL = _env("ONBOARD_GATE_MODEL", "claude-haiku-4-5-20251001")
# The transcript+notes → ficha intake extraction (brain/onboarding.extract_ficha) — one Sonnet pass
# (Peter, 2026-07-24: "transcript-based intake, WITH my own notes"). Structured/forced-tool, temp 0.
FICHA_MODEL = _env("FICHA_MODEL", "claude-sonnet-4-6")

# Claude models (direct Anthropic API). Sonnet 4.6 for clean diarized transcripts + image OCR.
POLISH_MODEL = _env("TRANSCRIPT_POLISH_MODEL", "claude-sonnet-4-6")
OCR_MODEL = _env("OCR_MODEL", "claude-sonnet-4-6")
# The "customize your agent" chat is high-level reasoning → Opus 4.8 (Peter).
AGENT_CHAT_MODEL = _env("AGENT_CHAT_MODEL", "claude-opus-4-8")

# FCM push (HTTP v1). Service-account JSON path; push no-ops gracefully if unset.
FCM_PROJECT_ID = _env("FCM_PROJECT_ID", "sancho-y-panza")
FCM_CREDENTIALS_PATH = _env("FCM_CREDENTIALS_PATH", "/opt/rocoto/fcm-service-account.json")

# ──────────── Phase-A orders → Excel (see product/PHASE-A-orders-to-excel.md) ────────────
# Ungrounded extraction model (no catalogue match yet). Sonnet 4.6 reads informal Spanish orders.
ORDER_EXTRACT_MODEL = _env("ORDER_EXTRACT_MODEL", "claude-sonnet-4-6")
# Fallback model when the primary is overloaded (529) — usually has spare capacity. Set "" to disable.
ORDER_EXTRACT_FALLBACK_MODEL = _env("ORDER_EXTRACT_FALLBACK_MODEL", "claude-haiku-4-5-20251001")
# Run extraction on inbound messages automatically. Off → only the manual /api/orders/extract route.
ORDER_EXTRACTION_ENABLED = _bool("ORDER_EXTRACTION_ENABLED", True)
# Resend HTTP API for the daily Excel dispatch (no SDK; httpx already a dep).
RESEND_API_KEY = _env("RESEND_API_KEY")
DISPATCH_FROM_EMAIL = _env("DISPATCH_FROM_EMAIL") or ONBOARD_EMAIL_FROM
# Dispatch requires an explicit recipient from the request or this environment.
DISPATCH_DEFAULT_EMAIL = _env("DISPATCH_DEFAULT_EMAIL")
# Default scheduled-send time (HH:MM, box local time) when cadence = 'scheduled'.
DISPATCH_DEFAULT_TIME = _env("DISPATCH_DEFAULT_TIME", "03:50")

# ──────────── Order-brain (grounded order intake; see ORDER-BRAIN-REGISTER.md) ────────────
# Master switch. When true the durable queue + 5-scale pipeline runs; Phase-A stays the fallback.
ORDER_BRAIN_ENABLED = _bool("ORDER_BRAIN_ENABLED", False)
# Demo seed/reset endpoints (/api/admin/*). Turn OFF before demoing to a real business.
SEED_ENABLED = _bool("SEED_ENABLED", False)
# Single-tenant for now (one pork manufacturer). All brain queries are keyed by this for isolation.
# TODO(rename): re-key brain data before flipping to 'rocoto'
BRAIN_TENANT = _env("BRAIN_TENANT", "rocoto")

# ── THE CREATOR LINE, AND THE ONE DELIBERATE EXCEPTION TO IT (2026-07-27) ─────────────────────────
# `ROCOTO_CREATOR_NUMBERS` is Peter's own WhatsApp. Everything he sends on it is a BOSS-CHANNEL
# instruction ("llámame", "prueba esto"), so the order-brain intake refuses it: minting a food order
# out of "call me" is worse than not minting one. That rule is CORRECT and stays.
#
# But it is also the exact number the seeded world hands to a demo customer, and Peter's own
# acceptance test is "WhatsApp Rocoto from my phone PRETENDING TO BE THE CUSTOMER and land in the
# order brain". So the rule needs an EXCEPTION that is explicit, scoped, reversible and off by
# default — never a hole punched in the rule itself. There are exactly two ways to earn it, and both
# leave a trace on the flight recorder (`ops.order_intake_*`, app._creator_order_route):
#
#   1. STATE (preferred — this is the one that makes the seeded world work with no env change):
#      the tenant's identity already holds a **seeded** customer profile for this number. That is a
#      real row on a real table, keyed by tenant, marked `customer_profiles.seeded=true`, that the
#      demo teardown deletes — so the exception appears and disappears WITH the demo world and can
#      never survive into a live distributor's tenant. See brain/identity.customer_role().
#   2. THIS LIST (the explicit override, for a box with no seeded world): a creator number named
#      here is deliberately treated as a CUSTOMER by the order intake. EMPTY BY DEFAULT.
#
# NOT used: the tier ladder. `runtime.ingest_inbound` pins `tier_override='creator'` for these
# numbers, so `scope_node.tier` answers "creator" by construction — asking it whether Peter is a
# customer would always answer no. The seeded customer_profile is the identity signal that actually
# carries information here, which is why it is mechanism (1).
CREATOR_NUMBERS_RAW = _env("ROCOTO_CREATOR_NUMBERS", "+447578244644")
TEST_CUSTOMER_NUMBERS_RAW = _env("ROCOTO_TEST_CUSTOMER_NUMBERS", "")


def phone_digits(phone: str) -> str:
    """A phone reduced to its digits — the ONLY safe way to compare '+44 7578 244644' with
    'whatsapp:+447578244644' with '447578244644'. (runtime.py / agent_reply.py keep their own raw
    string sets; those are not this module's to change, and they only ever see canonical E.164.)"""
    return "".join(ch for ch in (phone or "") if ch.isdigit())


def _digit_set(raw: str) -> frozenset:
    return frozenset(d for d in (phone_digits(n) for n in (raw or "").split(",")) if d)


CREATOR_DIGITS = _digit_set(CREATOR_NUMBERS_RAW)
TEST_CUSTOMER_DIGITS = _digit_set(TEST_CUSTOMER_NUMBERS_RAW)


def is_creator_number(phone: str) -> bool:
    """Is this the founder's own line? (digit-normalised, so no formatting can slip past it)"""
    d = phone_digits(phone)
    return bool(d) and d in CREATOR_DIGITS


def is_test_customer_number(phone: str) -> bool:
    """Is this creator number explicitly allowlisted to act as a CUSTOMER (mechanism 2 above)?"""
    d = phone_digits(phone)
    return bool(d) and d in TEST_CUSTOMER_DIGITS

# Self-hosted grounding models (ONNX/CPU via fastembed). Dense embedder must be 1024-dim to match
# the catalogue_skus.embedding vector(1024) column. The catalogue and the mentions are SPANISH, so
# the default is the MULTILINGUAL bge-m3 (also 1024-dim → same column, no migration; §5.7a). The
# old English-only bge-large-en-v1.5 is one env var away: BRAIN_EMBED_MODEL=BAAI/bge-large-en-v1.5.
# ⚠️ CHANGING THIS REQUIRES RE-EMBEDDING catalogue_skus (tests/eval_retrieval.py reembed) — query
# and passage vectors must come from the same encoder.
BRAIN_EMBED_MODEL = _env("BRAIN_EMBED_MODEL", "BAAI/bge-m3")
# MULTILINGUAL, and it is not a preference — it is the fix for a wrong-SKU bug (2026-07-27).
# `BAAI/bge-reranker-base` is an ENGLISH cross-encoder, and the cross-encoder DECIDES the pick (the
# dense arm scores 0.60-0.67 across an entire Spanish catalogue, i.e. no signal). On Spanish it read
# «sarta» as a fragment of ser-RAN-a / a-SAD-a, so a customer asking for chorizo got a jamón —
# confidently, with a wide enough margin that the doubt faculty saw no ambiguity to raise. Measured
# on the real rows, English vs multilingual:
#     «dos sartas»      PALETA SERRANA DESHUESADA  ->  CHORIZO SARTA EXTRA 500 G
#     «sartas»          PECHUGA DE PAVO ASADA      ->  CHORIZO SARTA EXTRA 500 G
#     «sartas de york»  PALETA SERRANA DESHUESADA  ->  CHORIZO SARTA EXTRA 500 G
# Same size class (1.11 GB vs 1.04 GB). NOTE the other half is still English —
# BRAIN_EMBED_MODEL=bge-large-en-v1.5 — and changing THAT means re-embedding every catalogue, so it
# is a separate, planned migration and not a config flip.
BRAIN_RERANK_MODEL = _env("BRAIN_RERANK_MODEL", "jinaai/jina-reranker-v2-base-multilingual")
BRAIN_EMBED_DIM = int(_env("BRAIN_EMBED_DIM", "1024") or "1024")

# Pipeline models: 5 scale-readers + live-call loop = cheap Haiku; synth/decision/personalise = Sonnet.
SCALE_READER_MODEL = _env("SCALE_READER_MODEL", "claude-haiku-4-5-20251001")
SYNTH_MODEL = _env("SYNTH_MODEL", "claude-sonnet-4-6")
DECISION_MODEL = _env("DECISION_MODEL", "claude-sonnet-4-6")
PERSONALISE_MODEL = _env("PERSONALISE_MODEL", "claude-sonnet-4-6")
PROFILE_MODEL = _env("PROFILE_MODEL", "claude-sonnet-4-6")
LIVECALL_MODEL = _env("LIVECALL_MODEL", "claude-haiku-4-5-20251001")

# Burst debounce: coalesce a customer's rapid-fire messages into one order. Urgency short-circuits.
BRAIN_DEBOUNCE_SECONDS = int(_env("BRAIN_DEBOUNCE_SECONDS", "120") or "120")

# Reverse-location demo seed (Stage 7): when on, an unknown caller's map lands on this real place
# (a real restaurant — default Botín, Madrid, the world's oldest restaurant). Toggleable at runtime
# via Founder Settings (/api/admin/location-seed). Point LOCATION_SEED_PHONE at a real hermes number
# to enrich from that business instead.
LOCATION_SEED_ENABLED = _bool("LOCATION_SEED_ENABLED", False)
LOCATION_SEED_PHONE = _env("LOCATION_SEED_PHONE")
LOCATION_SEED_NAME = _env("LOCATION_SEED_NAME", "Restaurante Botín, Madrid")
LOCATION_SEED_LAT = float(_env("LOCATION_SEED_LAT", "40.41464") or "40.41464")
LOCATION_SEED_LNG = float(_env("LOCATION_SEED_LNG", "-3.70898") or "-3.70898")

# ──────────── Medusa v2 — commerce system-of-record (medusa-integration.md) ────────────
# Rocoto's brain CREATES orders in Medusa at approve-time via the transactional outbox; the
# Medusa Admin API is reached over LOCALHOST ONLY (the admin token never leaves the box).
MEDUSA_BASE_URL = _env("MEDUSA_BASE_URL", "http://127.0.0.1:9000")
# Admin auth: email/password (emailpass → JWT, cached). The crown-jewel admin credential.
MEDUSA_ADMIN_EMAIL = _env("MEDUSA_ADMIN_EMAIL", "backend@sancho.local")
MEDUSA_ADMIN_PASSWORD = _env("MEDUSA_ADMIN_PASSWORD")
# The four commerce-primitive ids printed by medusa/scripts/bootstrap-sancho.ts.
MEDUSA_REGION_ID = _env("MEDUSA_REGION_ID")
MEDUSA_SALES_CHANNEL_ID = _env("MEDUSA_SALES_CHANNEL_ID")
MEDUSA_STOCK_LOCATION_ID = _env("MEDUSA_STOCK_LOCATION_ID")
MEDUSA_CURRENCY_CODE = _env("MEDUSA_CURRENCY_CODE", "eur")
MEDUSA_PUBLISHABLE_KEY = _env("MEDUSA_PUBLISHABLE_KEY")
# Shared secret the Medusa product subscriber sends to /internal/catalogue/* (localhost guard).
MEDUSA_SUBSCRIBER_TOKEN = _env("MEDUSA_SUBSCRIBER_TOKEN", "sancho-subscriber-dev")

# IS_PROD makes require_key FAIL-CLOSED (503 when ROCOTO_API_KEY unset) instead of falling open.
# Default false (dev/tests); set IS_PROD=true on the box.
IS_PROD = _bool("IS_PROD", False)
ENVIRONMENT = _env("ROCOTO_ENVIRONMENT", "production" if IS_PROD else "development")
RELEASE_COMMIT = _env("ROCOTO_RELEASE_COMMIT")
RELEASE_PRODUCT_SHA256 = _env("ROCOTO_RELEASE_PRODUCT_SHA256")
WORKER_EFFECT_MODE = _env("ROCOTO_WORKER_EFFECT_MODE", "paused")
if WORKER_EFFECT_MODE not in {"paused", "enabled"}:
    raise RuntimeError("ROCOTO_WORKER_EFFECT_MODE must be paused or enabled")
if ENVIRONMENT not in {"development", "staging", "production"}:
    raise RuntimeError("ROCOTO_ENVIRONMENT must be development, staging or production")
if (ENVIRONMENT == "production") != IS_PROD:
    raise RuntimeError("ROCOTO_ENVIRONMENT and IS_PROD must select the same environment")

# ──────────── The one webhook bypass, and the list production must carry ────────────
# ROCOTO_ALLOW_UNSIGNED_WEBHOOKS: accept a webhook nobody signed. It exists so a laptop can replay a
# captured Meta/Twilio body without holding the provider's secret, and for NOTHING else — prod ignores
# it (see allow_unsigned_webhooks below), so setting it on the box changes nothing.
ALLOW_UNSIGNED_WEBHOOKS = _bool("ROCOTO_ALLOW_UNSIGNED_WEBHOOKS", False)


def allow_unsigned_webhooks() -> bool:
    """Whether a webhook handler may skip its signature check. The ONE bypass, and it is dev-only:
    under IS_PROD the answer is False whatever the environment says. A function rather than a
    constant so the two flags are read at request time — a test (and an operator's `systemctl set-
    environment`) can move either one, and neither can outlive a restart into production."""
    return bool(ALLOW_UNSIGNED_WEBHOOKS) and not IS_PROD


# Secrets with no safe absence: unset, each one turns a door that verifies into a door that trusts.
_PROD_REQUIRED = (
    "ROCOTO_API_KEY",             # /api/* and /media fall open without it (require_key)
    "ROCOTO_AGENT_TOOL_SECRET",   # the ConvAI server tools fall open without it (require_agent_secret)
    "FB_APP_SECRET",              # X-Hub-Signature-256 on the Meta rail, which is always on
    "WA_CLOUD_VERIFY_TOKEN",      # Meta's GET handshake — without it the webhook cannot be (re)subscribed
)
# A rail's webhook secret is required only when that rail is configured at all: (rail key, its secret).
_PROD_REQUIRED_WITH = (
    ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"),          # signs every Twilio webhook
    ("ELEVENLABS_API_KEY", "ELEVENLABS_WEBHOOK_SECRET"),  # signs the ConvAI post-call webhook
    ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"),       # signs every Stripe event
)


def missing_prod_secrets() -> list[str]:
    """The NAMES production is missing, in the order they are declared. Read through globals() so a
    test (and an operator reading a live process) sees the values the running module actually holds."""
    g = globals()
    missing = [name for name in _PROD_REQUIRED if not (g.get(name) or "")]
    if not g.get("OWNER_EMAILS"):
        missing.append("OWNER_EMAILS")   # an empty owner set means nobody can administer the box
    missing += [secret for rail, secret in _PROD_REQUIRED_WITH
                if (g.get(rail) or "") and not (g.get(secret) or "")]
    return missing


def assert_prod_ready() -> None:
    """Refuse to start production with a hole where a secret belongs (called from app startup).

    Every name below has a fail-open shape behind it, and each one is invisible while it is wrong: an
    unset webhook secret does not break a single request, it just stops asking who sent it. So the
    misconfiguration is made LOUD at the only moment it is cheap — boot — and the error names the
    NAMES, because an operator reading a crash loop needs the list to set, not a description of it.
    A no-op outside IS_PROD: dev and CI run with none of these."""
    if not IS_PROD:
        return
    missing = missing_prod_secrets()
    if missing:
        raise RuntimeError(
            "IS_PROD=true and these environment values are unset: " + ", ".join(missing)
            + ". Set them in the box's environment file (deploy/.env) and restart; "
            "each one is a door that would otherwise accept anything.")


# ROCOTO_EGRESS_SANDBOX: a HARD kill-switch (class-E floor). When set, EVERY real outbound rail —
# WhatsApp (both rails), voice call, email — is suppressed at the network-send moment and returns a
# fake ok. This is what the REHEARSAL environment runs under (run_all_vitals.sh / rocoto_deploy.sh set
# it): a vital or rehearsal turn can NEVER reach a real party, independent of scope/lineage/egress_stub.
# Default OFF — the live box MUST send. Never set on the prod service.
EGRESS_SANDBOX = _bool("ROCOTO_EGRESS_SANDBOX", False)

# ──────────── «Prueba la IA» — WP-E (SITE-V2-PLAN.md §6): the public try-chat widget ────────────
# PUBLIC, unauthenticated, on the real runtime (runtime.run_turn), pinned to a scope that always
# starts with "test.try." (→ lineage=test, egress_stub=True) and grounds against its OWN dedicated,
# fictional catalogue — NEVER config.BRAIN_TENANT. Grounding a random internet visitor's chat against
# Peter's REAL supplier catalogue/prices would be a live data leak, not a demo. See tenancy.tenant_of's
# "test.try." special-case and backend/try_chat.py (the seeder + the endpoint's core logic).
TRY_CHAT_ENABLED = _bool("TRY_CHAT_ENABLED", True)
TRY_CHAT_DEMO_TENANT = _env("TRY_CHAT_DEMO_TENANT", "trychat_demo")
TRY_CHAT_MAX_TURNS = int(_env("TRY_CHAT_MAX_TURNS", "14") or "14")            # hard cap per session
TRY_CHAT_IP_LIMIT = int(_env("TRY_CHAT_IP_LIMIT", "40") or "40")             # turns per IP per window
TRY_CHAT_IP_WINDOW_S = float(_env("TRY_CHAT_IP_WINDOW_S", "600") or "600")   # 10 min sliding window
TRY_CHAT_SESSION_LIMIT = int(_env("TRY_CHAT_SESSION_LIMIT", "20") or "20")   # turns per session per window
TRY_CHAT_SESSION_WINDOW_S = float(_env("TRY_CHAT_SESSION_WINDOW_S", "300") or "300")

# ──────────── /demo — «Prueba Rocoto con tus productos» (§17.56, demo_flow.py) ────────────
# The per-visitor demo: a stranger uploads THEIR catalogue, answers 5 persona questions, and chats
# against a per-session tenant (`demo_<sid>`, scope `test.demo.<sid>`). Same isolation family as
# try-chat above (lineage=test, egress_stub, RLS by scope GUC) but each session gets its OWN world,
# so the knobs are its own — sized wider than try-chat because a /demo visitor has invested a file
# upload and five answers, and cutting them at 14 turns wastes exactly the moment the demo exists for.
DEMO_FLOW_ENABLED = _bool("DEMO_FLOW_ENABLED", True)
# ── §21/§22 · THE OPEN DEMO HAS CLOSED (WEBSITE-COMMERCIAL-MODEL.md, company repo; Peter 2026-08-28) ──
# «the /entrar demo-mint door CLOSES (existing accounts resume; unknown emails get the closed
# message, behind a reopenable config switch)».
#
# THIS SWITCH GOVERNS ONE ACT: THE PUBLIC MINT. `demo_auth.sign_in` is the only exchange in the
# codebase that turns an address nobody has ever seen into a workspace — a tenant, an operator seat
# and an owner membership — with no invitation, no card and no human in the loop. That was «la
# prueba abierta», and while /demo's own screen now shows a closed period to anonymous visitors,
# the sign-in behind it kept minting for anybody who found it.
#
# WHAT IT DELIBERATELY DOES NOT TOUCH, said out loud because a switch that quietly takes more than
# its name is worse than no switch. SIGNING IN IS UNCHANGED: an address that already owns a
# workspace resumes exactly as it did the day before — same session, same role, same tenant, same
# flow state, through both doors. Carlos notices nothing. Nor does it touch `sup_api.sup_alta`
# (the alta CTA keeps its own `ROCOTO_SELF_SERVE`, a separate decision that has always been off),
# `provisioning.provision` itself, or any of the internal/ops rails that mint a world for a
# customer Peter is setting up (brain/tenants.py, brain/golive.py, test_account.py).
#
# DEFAULT CLOSED, ENV-OVERRIDABLE: `DEMO_PROVISION_OPEN=1` in /opt/rocoto/.env reopens the mint for
# a fair, a push or a named campaign — a service restart, not a code change and not a deploy.
DEMO_PROVISION_OPEN = _bool("DEMO_PROVISION_OPEN", False)
DEMO_MAX_TURNS = int(_env("DEMO_MAX_TURNS", "30") or "30")                    # hard cap per session
DEMO_IP_LIMIT = int(_env("DEMO_IP_LIMIT", "60") or "60")                     # turns per IP per window
DEMO_IP_WINDOW_S = float(_env("DEMO_IP_WINDOW_S", "600") or "600")           # 10 min sliding window
DEMO_SESSION_LIMIT = int(_env("DEMO_SESSION_LIMIT", "30") or "30")           # turns per session per window
DEMO_SESSION_WINDOW_S = float(_env("DEMO_SESSION_WINDOW_S", "300") or "300")
DEMO_SESSION_TTL_DAYS = int(_env("DEMO_SESSION_TTL_DAYS", "14") or "14")     # sliding — portal.py's law
# THE ABSOLUTE 24h SPEND CEILING (2026-08-09 security remediation, F11). Everything above is a
# per-IP or per-session bucket, and neither is a ceiling — this surface had no equivalent of
# DEMO_DAILY_CAP (the phone demo) or ROCOTO_REPLY_DAILY_CAP (the WhatsApp rail), which is the
# codebase's own stated answer to «many distinct clients can still drive unbounded spend». Counted
# separately per kind (llm | media) so a flood of photos cannot starve the chat. 0 disables.
DEMO_SPEND_DAILY_CAP = int(_env("DEMO_SPEND_DAILY_CAP", "600") or "600")
# Where the magic link lands. Peter, 2026-08-08: the /demo flow lives in the PANEL SPA
# (app.soyrocoto.com — the same host sup_api's cookies are pinned to), NOT on the landing; the
# panel's vercel.json already SPA-catches /demo/* and proxies /api/* to this backend.
DEMO_PUBLIC_BASE_URL = _env("DEMO_PUBLIC_BASE_URL", "https://app.soyrocoto.com")

# ── §17.56 build-order item 5 · VOICE NOTES, PHOTOS, AND THE PHONE HANDOFF (demo_media/demo_phone)
# The two inputs a real customer actually uses after text, and the QR that lets a phone send them.
#
# THE BYTE CAPS ARE THE COST FENCE, and they are refused BEFORE the body is read (intake_api's law).
# A demo door is reachable by anybody who scanned a QR, so the ceiling has to bite at the socket,
# not after 500 MB has been pulled through the box. Sized off what the rails actually produce:
#   · a 60–90 s MediaRecorder opus note is 200–400 KB; a 60 s 16-kHz mono WAV is ~1,9 MB → 5 MB is
#     generous for every honest clip and still caps the worst-case Deepgram bill per request.
#   · a modern phone JPEG is 3–6 MB → 12 MB at the door, then downscaled to MAX_SIDE before the
#     vision call (Anthropic's own guidance: ~1568 px on the long edge is the cost/quality knee).
DEMO_MEDIA_ENABLED = _bool("DEMO_MEDIA_ENABLED", True)
DEMO_VOICE_MAX_BYTES = int(_env("DEMO_VOICE_MAX_BYTES", str(5 * 1024 * 1024)) or str(5 * 1024 * 1024))
DEMO_VOICE_MAX_SECONDS = float(_env("DEMO_VOICE_MAX_SECONDS", "120") or "120")
DEMO_PHOTO_MAX_BYTES = int(_env("DEMO_PHOTO_MAX_BYTES", str(12 * 1024 * 1024)) or str(12 * 1024 * 1024))
DEMO_PHOTO_MAX_SIDE = int(_env("DEMO_PHOTO_MAX_SIDE", "1600") or "1600")
# Uploads per window — TIGHTER than the chat limits on purpose: a media turn costs a Deepgram or a
# vision call before the mind is even reached, so the volume guard has to sit in front of it.
DEMO_MEDIA_LIMIT = int(_env("DEMO_MEDIA_LIMIT", "12") or "12")
DEMO_MEDIA_WINDOW_S = float(_env("DEMO_MEDIA_WINDOW_S", "600") or "600")
DEMO_MEDIA_IP_LIMIT = int(_env("DEMO_MEDIA_IP_LIMIT", "40") or "40")
DEMO_MEDIA_IP_WINDOW_S = float(_env("DEMO_MEDIA_IP_WINDOW_S", "600") or "600")

# THE PHONE LINK (§17.20's two clocks, kept): a 15-minute SLIDING window any phone activity extends,
# under a HARD cap the signature itself carries — so a link photographed off a screen in a shared
# office dies on its own even if the DB row survives, and the desk re-minting rotates the nonce,
# which is the revocation a stateless HMAC alone cannot give you.
DEMO_PHONE_TTL_S = float(_env("DEMO_PHONE_TTL_S", "900") or "900")          # 15 min, sliding
DEMO_PHONE_HARD_CAP_S = float(_env("DEMO_PHONE_HARD_CAP_S", "7200") or "7200")   # 2 h, in the signature
# How stale the desk's SSE stamp may be before the phone stops claiming «tu ordenador lo está viendo».
# 45 s = two missed 20 s sync heartbeats plus slack: one dropped beat must not read as a closed tab.
DEMO_DESK_PRESENCE_S = float(_env("DEMO_DESK_PRESENCE_S", "45") or "45")
# Dedicated HMAC secret for the phone link. Absent → consent.py's ladder (ROCOTO_API_KEY), so the
# feature works on day one and rotating to its own secret invalidates ONLY outstanding phone links.
DEMO_PHONE_SIGNING_SECRET = _env("DEMO_PHONE_SIGNING_SECRET")

# §17.56 Q1 `behaviour.doubts='amount'` — the line-value split: a doubted line worth at least this
# many euros goes to the TEAM, a smaller one to the customer (qty×price when known, else the order's
# total stands in). ⚠️ Peter has NOT blessed this number — 200 € is a placeholder default so the
# knob exists and vitals can exercise it; the real threshold is his call (and may become per-tenant).
DOUBT_AMOUNT_THRESHOLD_EUR = float(_env("DOUBT_AMOUNT_THRESHOLD_EUR", "200") or "200")

# ──────────── Feature flags — land DARK, flip per-feature on the box (default OFF) ────────────
# MEDUSA_SYNC_ENABLED: the outbox still RECORDS intents, but the drainer no-ops until this is on.
MEDUSA_SYNC_ENABLED = _bool("MEDUSA_SYNC_ENABLED", False)
SHOP_ENABLED = _bool("SHOP_ENABLED", False)
SCHEDULED_TASKS_ENABLED = _bool("SCHEDULED_TASKS_ENABLED", False)
STRIPE_ENABLED = _bool("STRIPE_ENABLED", False)
# NOTIFY_ENABLED (Stage 5a): the customer-comms loop (SMS via Twilio + email via Resend, fired on
# submitted/accepted/questions). Land DARK — default OFF. With it off, notify.notify_customer() is a
# no-op (records nothing, sends nothing) so the accept/submit paths are safe before the providers are
# fully wired (Twilio Messaging Service + an approved WhatsApp template are multi-day external deps).
NOTIFY_ENABLED = _bool("NOTIFY_ENABLED", False)
# Outbound SMS sender (E.164 or a Twilio Messaging Service SID via TWILIO_MESSAGING_SERVICE_SID). Paid
# SMS only ever fires to an OTP-verified phone (toll-fraud guard, enforced in notify.py).
TWILIO_SMS_FROM = _env("TWILIO_SMS_FROM") or TWILIO_VOICE_FROM
TWILIO_MESSAGING_SERVICE_SID = _env("TWILIO_MESSAGING_SERVICE_SID")
# Transactional email sender for customer notifications (Resend; reuses RESEND_API_KEY). Falls back to
# the dispatch sender domain, which is the verified Resend domain on the box.
NOTIFY_FROM_EMAIL = _env("NOTIFY_FROM_EMAIL") or DISPATCH_FROM_EMAIL

# ── WhatsApp auto-reply rails (voice_reply.py) — land DARK, default OFF ──
# Two independent rails, one per inbound form:
#   • VOICE rail: a prospect's WhatsApp VOICE NOTE → Rocoto replies with a voice note in his cloned
#     voice (never text). Gated on ROCOTO_VOICE_REPLY_ENABLED.
#   • TEXT rail: a prospect's WhatsApp TEXT → Rocoto replies with a text message. Gated on
#     ROCOTO_TEXT_REPLY_ENABLED. (This is the basic conversational sales line — NOT out of scope.)
# Each rail is inert until its own flag is true on the box; with a flag off, that inbound path is
# byte-identical to today. Do NOT flip the voice flag on until the round-trip harness
# (voice_reply.py __main__) passes there. The outbound /wamedia sweep runs regardless of both flags.
# The TEXT-rail flag (its own line so it isn't buried in the voice-flag comment):
ROCOTO_TEXT_REPLY_ENABLED = _bool("ROCOTO_TEXT_REPLY_ENABLED", False)
# The VOICE-rail flag:
ROCOTO_VOICE_REPLY_ENABLED = _bool("ROCOTO_VOICE_REPLY_ENABLED", False)
# Reply-generation model. Fast + cheap by default (the same low-latency model native voice turns use);
# the reply is short/WhatsApp-length so Haiku is plenty. Overridable if a smarter tail is ever wanted.
ROCOTO_VOICE_REPLY_MODEL = _env("ROCOTO_VOICE_REPLY_MODEL", VOICE_CHAT_MODEL)
# Rocoto's approved ElevenLabs voice for the spoken reply. Falls back to the shared TTS voice so the
# feature works out-of-the-box; set ROCOTO_VOICE_ID once Rocoto's own clone is approved (the wizard's
# "Elige la voz de Rocoto"). This is a plain TTS voice_id (NOT the ConvAI agent id).
ROCOTO_VOICE_ID = _env("ROCOTO_VOICE_ID") or ELEVENLABS_TTS_VOICE
# Per-phone cooldown (seconds) between auto-replies on EITHER rail (shared). DEFAULT 0 = OFF: the
# debounce coalesces bursts and the supersede/assistant-row guards already stop double-replying to the
# SAME burst, so a time-since-last-reply gate is redundant for de-dup and HARMFUL for a live
# back-and-forth — a genuine new inbound arriving inside the window was being silently dropped (no
# reply, no typing bubble). Conversation must always flow; the GLOBAL daily cap (ROCOTO_REPLY_DAILY_CAP)
# is the real cost backstop. Knob kept: set >0 to re-enable a per-phone throttle if ever needed.
ROCOTO_VOICE_REPLY_COOLDOWN_SEC = _int("ROCOTO_VOICE_REPLY_COOLDOWN_SEC", 0)
# GLOBAL daily cap on auto-replies across BOTH rails (process-level 24h rolling counter). The per-phone
# cooldown only throttles a single sender; on a PUBLIC line, many distinct numbers can still drive
# unbounded Anthropic+ElevenLabs+Twilio spend. This is the real backstop (mirrors DEMO_DAILY_CAP): once
# this many replies have been SENT in the trailing 24h, both rails log+skip before the LLM call until
# the window rolls off. Fail-soft. Set 0 to disable the cap.
ROCOTO_REPLY_DAILY_CAP = _int("ROCOTO_REPLY_DAILY_CAP", 500)
# TTL (hours) for outbound /wamedia files before the periodic sweep deletes them. Twilio's CDN fetches
# the media within seconds of send, so anything older is dead weight. 48h is generous headroom for
# the cockpit to have replayed it. Set 0 to disable the sweep. Tolerant parse (a typo like "48h" must
# never crash import — the sweep runs regardless of the reply flags).
ROCOTO_WAMEDIA_TTL_HOURS = _int("ROCOTO_WAMEDIA_TTL_HOURS", 48)
# ── Reply guard thresholds (were hardcoded in voice_reply.py; promoted to config per review F15) ──
# Max debounce hooks that may park in the quiescence window per phone at once (excess bail immediately;
# a surviving hook still covers the burst). Bounds task pile-up when a phone floods with messages.
ROCOTO_MAX_DEBOUNCE_INFLIGHT = _int("ROCOTO_MAX_DEBOUNCE_INFLIGHT", 6)
# Never auto-reply to an inbound older than this (seconds). Bounds the startup pending-transcript sweep:
# a restart re-fires the reply hook on 'pending' rows, and replying to an hours-old message would drop
# a ghost reply into a conversation that has moved on. 15 min default.
ROCOTO_MAX_NOTE_AGE_SEC = _int("ROCOTO_MAX_NOTE_AGE_SEC", 15 * 60)

# ── Reply debounce (Workstream B) ──
# Quiescence window (seconds) waited after the LAST inbound before Rocoto generates a reply. A burst
# of double/triple messages therefore collapses into ONE considered reply: each inbound's hook sleeps
# this window, then re-checks the thread — only the newest inbound survives the supersede guard and
# replies; the older hooks drop their drafts. Applies to BOTH the text and voice-note rails. 0 disables
# the wait (reply fires immediately, pre-B behaviour). Keep modest — this is added latency before every
# reply. Set below the ~25s typing-indicator TTL so the bubble covers debounce + generation.
ROCOTO_REPLY_DEBOUNCE_SEC = _float("ROCOTO_REPLY_DEBOUNCE_SEC", 8.0)

# ── Typing indicator (Workstream C) ──
# Show WhatsApp's "typing…" bubble while Rocoto generates a reply. Twilio's v3 Indicators endpoint
# (Public Beta on our account, empirically confirmed 2026-07-10) — POST Typing.json with the inbound
# message SID; it also marks that message READ (blue ticks) and auto-clears on our reply or after ~25s.
# Public Beta = no SLA → every call is fail-soft (a failure never blocks or delays the reply). Default
# ON (item C verdict: SUPPORTED, build it); flip off per box if the beta misbehaves.
ROCOTO_TYPING_INDICATOR_ENABLED = _bool("ROCOTO_TYPING_INDICATOR_ENABLED", True)

# ── PIN: the capability that EXISTS in Meta's schema but is not enabled for our WABA ──────────────
# Empirically established 2026-07-26: the Cloud API *validates* the pin shape
# {"type":"pin","pin":{"type":"pin"|"unpin","message_id":wamid,"expiration_days":1..30}} — it is not
# an unknown field — but EXECUTING it answers 131000 on our WABA (built, not enabled for us), while
# the public docs list pinning as unsupported. Schema and docs disagree, so the honest state is
# NOT-YET-AVAILABLE, and the thing we actually want is to KNOW the day it opens instead of
# re-discovering it by accident (that discovery cost real probes).
#
# THE ONE SWITCH. Off (the default) the capability is INERT end to end: whatsapp_cloud.pin_message /
# unpin_message refuse with PinDisabledError before building a payload, and the worker's weekly probe
# returns immediately without a single DB read or network call. Nothing in the process can emit a pin.
# Turning it ON does NOT give the mind a hand — it only arms brain/pin_watch's once-a-week probe
# against OUR OWN founder thread. The mind's hand stays the DISABLED manifest_tool row 'pin_message',
# which only Peter enables, and only after the watcher tells him pinning actually works.
ROCOTO_WA_PIN_ENABLED = _bool("ROCOTO_WA_PIN_ENABLED", False)
# How often the worker may attempt the capability probe. Weekly by default, and enforced DURABLY (a
# system_flags timestamp, not a process clock) so restart-looping can never turn it into a spammer.
ROCOTO_PIN_PROBE_SCAN_SEC = _int("ROCOTO_PIN_PROBE_SCAN_SEC", 7 * 24 * 3600)

# ── Re-engagement drip (brain/drip.py) — land DARK, default OFF ──
# The abandoned-signup email ladder (+2h / +1d / +3d / +7d, then stop). With it off, brain.drip
# run-due is a no-op (enrols/records nothing, sends nothing) so nothing fires before the copy +
# Supabase redirect URLs are verified. Enrolment/completion hooks are all guarded by this flag.
DRIP_ENABLED = _bool("DRIP_ENABLED", False)
DRIP_STEP1_HOURS = float(_env("DRIP_STEP1_HOURS", "2") or "2")   # first touch (2h; set 0.5 for 30-min)
DRIP_BATCH_LIMIT = int(_env("DRIP_BATCH_LIMIT", "200") or "200")
DRIP_WA_NUMBER = _env("DRIP_WA_NUMBER", "34627506263")          # wa.me digits for the "talk to Peter" CTA

# Pay-link path segment (the bit after the storefront origin). Read here so the `/shop`→`/store` rename
# is a ONE-LINE config flip coordinated with the storefront session — never a hardcoded `/shop` in code.
SHOP_PAY_PATH = _env("SHOP_PAY_PATH", "/shop/pay")
# Basket access-token TTL in days (the public read/pay link lifetime; expires on accept/pay regardless).
BASKET_TOKEN_TTL_DAYS = int(_env("BASKET_TOKEN_TTL_DAYS", "14") or "14")

# ─────── Stripe (Rocoto's own clean layer — NOT Leo's payments package) + Twilio Verify OTP ───────
# STRIPE_SECRET_KEY: the platform secret (sk_test_… in test mode, sk_live_… live). The Connect
# onboarding (brain/stripe_connect.py) uses it; Medusa's payment-stripe provider reads STRIPE_API_KEY.
STRIPE_SECRET_KEY = _env("STRIPE_SECRET_KEY") or _env("STRIPE_API_KEY")
STRIPE_PUBLISHABLE_KEY = _env("STRIPE_PUBLISHABLE_KEY")
STRIPE_APPLICATION_FEE_PERCENT = _env("STRIPE_APPLICATION_FEE_PERCENT", "")

# ──────────── Rocoto onboarding SETUP FEE — the €50 one-time 'alta' that gates the waitlist ────────
# A real Stripe Product+Price (created ready in the dashboard) charged via Stripe Checkout at the end
# of the wizard, with invoice_creation ON so Stripe issues a proper invoice + PDF. brain/setupfee.py
# owns the flow. TEST tokens (owner ?test=1) use the *_TEST price + keys so a walkthrough never touches
# real money; when no test keys are set, a test token gracefully skips payment (direct waitlist join).
ROCOTO_SETUP_FEE_EUR = _env("ROCOTO_SETUP_FEE_EUR", "50")
ROCOTO_SETUP_FEE_PRICE_ID = _env("ROCOTO_SETUP_FEE_PRICE_ID")
ROCOTO_SETUP_FEE_PRICE_ID_TEST = _env("ROCOTO_SETUP_FEE_PRICE_ID_TEST")
# Live webhook signing secret for POST /api/stripe/webhook (checkout.session.completed / invoice.paid).
# The TEST variant reuses STRIPE_WEBHOOK_SECRET_TEST (defined in the subscriptions block below).
STRIPE_WEBHOOK_SECRET = _env("STRIPE_WEBHOOK_SECRET")
# M1M2 WP-4: how often the worker's billing scan walks the live tenants (brain/billing.py). The
# meter is DRIVEN by durable billing_run rows, not this cadence — this only paces the no-op scans.
ROCOTO_BILLING_SCAN_SEC = _int("ROCOTO_BILLING_SCAN_SEC", 600)

# ──────────── Stripe SUBSCRIPTIONS — OUR SaaS billing of the supplier (brain/stripe_subs.py) ────────
# DISTINCT from Connect above: Connect PAYS OUT the supplier's shop sales; THIS bills the supplier for
# their AI employees (Marco/orders tiered, Hugo/deliveries flat, Carmen/storefront = a 0.3% enable, no
# recurring price). TEST MODE: explicitly prefer the *_TEST keys (we never bill suppliers in test mode
# off the live key). Falls back to the shared STRIPE_SECRET_KEY/STRIPE_PUBLISHABLE_KEY so a one-env-var
# swap goes live. The webhook secret guards POST /api/subs/webhook signature verification.
STRIPE_SECRET_KEY_TEST = _env("STRIPE_SECRET_KEY_TEST")
STRIPE_PUBLISHABLE_KEY_TEST = _env("STRIPE_PUBLISHABLE_KEY_TEST")
STRIPE_WEBHOOK_SECRET_TEST = _env("STRIPE_WEBHOOK_SECRET_TEST")
# The subscriptions layer reads THIS secret (test-first). Live = set STRIPE_SECRET_KEY / unset *_TEST.
STRIPE_SUBS_SECRET_KEY = STRIPE_SECRET_KEY_TEST or STRIPE_SECRET_KEY
STRIPE_SUBS_PUBLISHABLE_KEY = STRIPE_PUBLISHABLE_KEY_TEST or STRIPE_PUBLISHABLE_KEY
STRIPE_SUBS_WEBHOOK_SECRET = STRIPE_WEBHOOK_SECRET_TEST or _env("STRIPE_WEBHOOK_SECRET")

# ──────────── Stripe PEDIDOS billing mode (brain/pedidos_billing.py, PEDIDOS-PRICING-BUILD.md §5) ────
# Selects which Stripe key-pair (live vs *_TEST) Pedidos Checkout/Portal/webhooks use. This is a
# DELIBERATE config decision, never derived from `tenants.status` — status only flips 'test' -> 'live'
# at the post-purchase WhatsApp-binding step, which happens AFTER a tenant can already pay. Keying
# Stripe mode off tenant status would route a brand-new paying customer's FIRST Checkout to TEST
# Stripe and lose the money. 'live' | 'test'.
PEDIDOS_STRIPE_MODE = _env("PEDIDOS_STRIPE_MODE", "live")

# Where Checkout / Billing-Portal return the supplier — the in-app subscriptions screen (deep-linkable).
SUBS_RETURN_URL = _env("SUBS_RETURN_URL", "https://sanchoypanza.com/app/subscriptions")
# The public storefront origin (onboarding return/refresh URLs land here).
STOREFRONT_URL = _env("STOREFRONT_URL")
# Twilio Verify service for the public-shop OTP (the supplier's own Twilio account).
TWILIO_VERIFY_SERVICE_SID = _env("TWILIO_VERIFY_SERVICE_SID")

# (The Rocoto onboarding / CRM / waitlist config lives in ONE canonical block above — the duplicate
#  that used to sit here and re-blank ROCOTO_CAL_SLUG was removed 2026-07-08.)

# ── Wave-3 supplier web app: route optimisation (VROOM + self-hosted OSRM, loopback) ─────────────
# The VROOM HTTP solver endpoint (deploy/route-stack/). Unset = the routes surface answers with a
# typed «vroom_no_configurado» error — never a fabricated route. Installed at deploy by the main
# loop (compose + runbook only in this tree; nothing here starts containers).
ROCOTO_VROOM_URL = _env("ROCOTO_VROOM_URL")  # e.g. http://127.0.0.1:3000

# ──────────── Dover (the free ATS behind soyrocoto.com/join) — JOIN-PAGE-SPEC-2026-09-04.md ────────
# The /join conversation pushes a finished application to Dover as well as emailing NOTIFY_EMAIL and
# writing the `join_applicants` row. BOTH names must be set for the push to happen at all: absent,
# join.py logs one line and skips it, and the email plus the row still carry the whole application.
# A push failure is written into join_applicants.dover_error and never surfaces to the candidate.
DOVER_API_KEY = _env("DOVER_API_KEY")
DOVER_JOB_ID = _env("DOVER_JOB_ID")
