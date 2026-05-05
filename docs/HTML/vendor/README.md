# Lokale Vendor-Dependencies fuer `docs/HTML/`

L8 (Audit) Hinweis: Die HTML-Pendants laden Mermaid via CDN
(`cdn.jsdelivr.net/npm/mermaid@11/...`). Das ist fuer Online-Browsing
unproblematisch, **bricht aber Air-Gap-Deployments** (z. B. wenn das Repo
auf einem isolierten Server gerendert werden soll).

## Lokales Vendoring (manuell)

Der Repo-Maintainer kann Mermaid einmalig vendoren:

```bash
mkdir -p docs/HTML/vendor
curl -sSL -o docs/HTML/vendor/mermaid.esm.min.mjs \
  "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs"
```

Anschliessend in den HTML-Files den Import-Pfad anpassen:

```diff
- import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
+ import mermaid from "./vendor/mermaid.esm.min.mjs";
```

## Subresource-Integrity (alternativ)

Wenn Online-Loading aktiv bleiben soll, aber CDN-Tampering ausgeschlossen
werden muss: SRI-Hash hinzufuegen. Hash via:

```bash
curl -sSL "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs" \
  | openssl dgst -sha384 -binary | openssl base64 -A
```

Dann als `integrity="sha384-<hash>"` im `<script type="module">`-Tag.

## Status

- 2026-05-05: L8 dokumentiert, Manual-Vendor-Pfad bereit. CDN bleibt
  Default fuer Online-Nutzung. Reaktivierungs-Trigger: bei Air-Gap-
  Deployment dieses README durcharbeiten.
