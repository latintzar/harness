# Source and dependencies

This clean source export comes from the Vitrina software Harness, originally extracted from Rocoto commit `faafa7cd1317db9dc1a9caa382131af3eb3e8c96`. `provenance/source-export.json` records the actual extraction tip and per-file original hashes. Earlier private Git history and operational receipts are outside the release.

## Bare runtime

Python 3.12 and `harness/requirements.txt` are authoritative for the shared runtime. `requirements-dev.txt` adds pinned pytest dependencies. The fresh shared installation was verified independently of the optional retail model stack. `layout.py` installs runtime, mechanics, SDK and exactly one selected vertical.

The page needs shared theme/mark/font/integration assets under `app/` and vendored frontend libraries under `harness/web/vendor/`. Licences/notices remain with the source. The packager declares shared assets and uses Git selection or the explicit clean-export inventory. It carries the selected vertical and shared requirements, with the maintained backend read/migration closure only for retail.

## Optional retail and custom compute

`harness/verticals/retail/requirements.txt` adds NumPy, Torch, Transformers, Hugging Face, OpenCV, AsyncSSH and PostgreSQL tooling. The bare runtime uses the shared requirements. Retail camera and large-model execution use operator-installed checkpoints and authorized inputs. The retained MediaPipe browser segmentation model and WASM have their existing vendored licence and manifest; they are optional retail frontend dependencies.

The retained custom-executor broker and historical migration adapters are source, rather than a commissioned service. They require their own operator installation, target selection, private authority and tests. Built artifacts exclude retired local agent adapters; OpenAI hosts the current agent core.

## Manufacturer and Orders

The client manufacturer product is prepared separately within Vitrina. Orders owns its database and publication API. Cross-product manufacturer integration tests refer to that separate source closure; the source export retains these tests as originally recorded, while the lean public verification command exercises the reusable framework and synthetic SDK verticals.

## Provider setup

The server consumes the operator's `ROCOTO_OPENAI_API_KEY`. API access, local admission, public MCP reachability, accounting and live execution are distinct checks. [Own-key setup](docs/OPENAI-SETUP.md) uses the public official Agents API contract. Existing company accounts, keys, funding records and deployment permissions remain private and supply no authority to a new installation.
