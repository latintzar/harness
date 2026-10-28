# CHANNELS

Messages leave Rocoto through a verified account and recipient, then replies return to the same saved conversation as ordinary messages with their real author and channel. The shared messaging store, outbox and signal dispatcher own that lifecycle. A vertical selects optional WhatsApp, Viber, email and voice capabilities in its manifest.

## Selection and readiness

[BuiltInCapabilities](builtin_capabilities.py) constructs only selected provider factories and pollers. [messaging_composition.py](messaging_composition.py) installs those adapters into the same [Messaging](messaging.py) service and exposes its exact `channel_names`. Disabled capabilities construct neither provider nor poller.

Readiness reports selection, installation, configuration, provider readiness, spending readiness and host readiness independently. The host marks a channel ready after its enrollment, signed ingress and durable outbox path are attached. Actual account credentials, callback commissioning and spending grants are deployment facts; a mock transport proof establishes the code boundary.

## Recipients and conversation authority

Contacts are verified against the selected account and workspace. A participant-reach agent's recipients come from its own current participant bindings. A member's removal, consent change, contact generation, account change or workspace revocation withdraws later delivery. Retail additionally requires its live observation lane for participant alerts.

Each message has a durable outbox identity. The send path checks current actor, conversation, recipient, account, quota and monetary admission again immediately before transport. The original effect receipt survives restart. A timeout or ambiguous provider response remains uncertain and holds its reservation; recovery reconciles that receipt before any replacement effect.

## Replies through the common signal chain

A provider signature is verified before event normalization or workspace construction. [messaging_signals.py](messaging_signals.py) enqueues a stable event identity through the common signal ledger. The inbox remains pending while the signal queues; the ordinary saved message is its admission receipt. Reconciliation records that receipt without launching a second turn.

Original channel, account, provider event, contact, sender and destination stay immutable. The dispatcher keeps the claimed signal's authority during an active reply even after message admission. Source-supplied alert context stays attached to the original payload; a chain step cannot replace it with broader authority. Duplicate callbacks reuse the same inbox, signal and ordinary message.

## Adapters

| Channel | Current contract |
| --- | --- |
| WhatsApp | Meta templates/messages with signed callbacks, verified member enrollment and current template/account readiness. |
| Viber | Bot text messages with signed events, subscription verification and retained outgoing `tracking_data`. |
| Email | Unipile v2 send plus signed `email.new` callbacks, exact conversation correlation and bounded text. |
| Voice | A bounded Twilio call with Say/Gather, signed speech/status callbacks and the original Call SID. |

Email accepts a reply only when the authenticated parent message identifies the original account, outbox and current contact binding. RFC In-Reply-To maps to the authenticated outgoing response's Message-ID; an explicit provider parent ID is also supported. A sender address alone grants no conversation. Unrelated content is discarded before chat admission. Replies use the admitted incoming identity selected by the account's `email_reply_id`: `provider` for Gmail/Outlook or `rfc822` for IMAP. The default is `provider`.

Voice has a commissioned maximum of 10–120 seconds and reserves the corresponding whole-minute units. A signed speech reply must match the original Call SID. The callback returns terminal TwiML after admission. This capability is a finite spoken exchange; streaming realtime conversation remains a separate capability.

Phone-channel priority and eligibility use the installed provider and recipient facts. SMS and any additional channel require their own implemented, selected adapter and commissioning evidence.

## Count and money limits

All outgoing paths, including verification codes, greetings, participant alerts, agent answers and signed endpoints, reserve against the same workspace limit of 200 sends per UTC day. A stable outbox identity consumes one unit; uncertain attempts retain it. Current authority is rechecked after quota admission and before the HTTP request.

Existing phone delivery uses its private [phone spending grant](phone_spend.py). Email and voice use [capability_spend.py](capability_spend.py): a positive commissioned upper unit price, maximum request, daily and total money limit, expiry and Stop. Reserve occurs before each HTTP request in the messaging database. Sent, failed and uncertain attempts conservatively retain the upper-cost hold; a provably unstarted attempt can release it. Account totals survive grant rotation.

The private `ROCOTO_CAPABILITY_SPEND_CONFIG` file binds `schema_version`, `environment_id`, `company_scope` and a list of grants. Each grant binds `grant_id`, `channel`, `account_id`, `expires_at`, `stopped`, `unit_cost_microusd`, `maximum_request_microusd`, `daily_microusd` and `total_microusd`. The maximum request is at most 1,000,000 micro-USD. Missing, mismatched, expired or stopped grants hold delivery. [Cost safety](../../docs/COST-SAFETY.md) is the operational authority.

## Private configuration and secrets

WhatsApp uses [messaging_config.py](messaging_config.py). Other capabilities use `ROCOTO_BUILTIN_CAPABILITIES_CONFIG`, an absolute owner-only file binding schema, environment, workspace and selected account entries. Entries name account, sender, callback URL, commissioning facts and a private credentials file; voice adds `maximum_seconds`, email optionally adds `email_reply_id`.

Credential files repeat the exact environment/workspace/account/capability binding. Email uses its API key and webhook secret; voice and Viber use their authentication token. Regular files and parent directories must be private, owner-held and symlink-free. Rotation rereads the private file while preserving the original binding. Public status exposes safe readiness rather than credentials.

[Signed outgoing webhooks](CONNECTIONS.md#registered-outgoing-endpoints) use a registered owner-authorized endpoint and the same send ledger. Incoming hooks use the [signal chain](README.md#wakes-and-the-signal-chain). Their copy-once signing secret appears only in the private, identity-fenced control.

## Evidence

Messaging, participant, delivery, spend, signal, provider and composition tests cover exact reply attribution, duplicate callbacks, restart, revocation across awaits, quota races, uncertain attempts, selected-only factories and private credential modes. Email/voice wire tests use local mock transports and verify exact provider request fields and signatures. Live account, send and reply proof belongs to the selected deployment's commissioning receipt.
