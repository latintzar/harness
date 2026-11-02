# Atelier native navigation

Atelier native navigation is a 56px bar for web previews, with an accessible home logo, project chooser and documentation link. The chooser opens current projects and Web Experiences directly.

Copy the complete bundle, including `atelier-nav-fonts/` and its notices, into your app’s public files. Load the script after the document body exists:

```html
<script defer src="./native-nav/atelier-nav.js" data-project="vitrina"></script>
```

Reserve `--atelier-nav-height: 56px` in your layout. The component keeps its styles in a shadow root and registers its own Archivo font family. Your app’s typography and provider configuration keep their existing settings.

## Edit the projects

Edit `atelier-nav.catalog.json`, then run the included Python 3 generator from this folder:

```sh
python3 atelier-nav.generate.py
```

The generator uses only the adjacent snapshot, template, icons, CSS and assets. It regenerates `atelier-nav.js` and `atelier-nav.manifest.json`; `--check` verifies the generated files. Keep the current script’s `data-project` ID in your edited catalog. All inputs travel with this folder.

Another host reads its adjacent files. Official Atelier hosts use the matching immutable `/assets/native-nav/releases/<version>/` bundle. To force adjacent assets on any host, add `data-asset-base="./"` to the script tag.

## Interaction

The native modal is a glass grid on desktop and the full available screen on phones. It includes safe-area spacing, close and Escape controls, focus containment and focus return. Closing restores the document’s previous scroll setting.

## Credits

Peter Neyra’s code retains `atelier-nav.LICENSE` (MIT). Lucide retains `atelier-nav.LUCIDE-LICENSE` (ISC). Archivo retains `atelier-nav-fonts/Archivo-OFL.txt`. The exact original asterisk SVG retains its embedded DejaVu glyph credit. `atelier-nav.PROVENANCE.json` identifies these sources; the manifest records their bytes.
