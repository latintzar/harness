# Third-party notices

The root MIT licence covers Peter Neyra's original code. Third-party dependencies and assets retain their own licences and attribution.

| Material | Retained evidence |
|---|---|
| Shared Python dependencies | Pinned in `harness/requirements.txt`; their installed distributions carry upstream licences. Retail adds its own requirement file. |
| Mermaid 12.1.0 | MIT notice under `harness/web/vendor/mermaid-12.1.0/LICENSE`; exact vendored bytes and archive integrity in `rich-content-vendors.json`. |
| D3, DOMPurify, Highlight.js, KaTeX and Marked | Existing licence files and version/hash manifests under `harness/web/vendor/` are retained. KaTeX's distributed fonts retain its notices. |
| Fraunces | SIL Open Font License, `app/public/fonts/fraunces/OFL.txt`; copyright The Fraunces Project Authors. |
| Retail Inter/Lucide/Base UI kit | Existing `LICENSE-inter.txt`, `LICENSE-lucide.txt`, `LICENSE-baseui.txt` and provenance under `harness/verticals/retail/web/control-room/kit/`. |
| Retail MediaPipe browser bundle/model | Existing licence and file manifest under `harness/verticals/retail/web/control-room/vendor/mediapipe-0.10.22-rc.20250304/`; these upstream assets keep their own licence. |
| Chat/Search dreamscape artwork | Original generated UI artwork with source hashes and authored prompts in `harness/web/assets/workspace-backgrounds.json`; machine-local source paths are omitted. |
| Monet, Water Lilies (1906) | Existing public-domain reproduction and attribution record in `harness/web/assets/monet-water-lilies.json`, identifying the Art Institute of Chicago work and Wikimedia source. |
| Integration names and marks | Existing Axis/Gmail/Square/Shopify UI marks identify their integrations; these names and marks retain their respective owners. |
| OpenAI API/catalogue metadata | Service integration and discovery metadata retain their upstream provenance; the repository licence covers the original adapter/framework implementation. Use your own project, account grants and key. |

Source recordings, imported third-party camera datasets, benchmark outputs, retail camera-vendor mark files and source-company demonstration branding are omitted pending their own release review. The retail code expects an operator's authorized inputs rather than a bundled footage corpus.
