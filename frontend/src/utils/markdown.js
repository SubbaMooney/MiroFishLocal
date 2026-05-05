// Sicherer Markdown-Renderer (Fix H1 — Stored XSS via Markdown).
//
// Dieser Renderer wird von Step4Report.vue und Step5Interaction.vue
// genutzt. Quellen: LLM-Reports, Chat-Nachrichten, Interview-Antworten.
// Da diese Inhalte über v-html / innerHTML im DOM landen, MUSS
// jegliches HTML aus dem Eingabetext eliminiert werden, bevor wir
// daraus Markdown-HTML generieren.
//
// Strategie (Defense-in-Depth):
//   1. Eingabe HTML-escapen, bevor das Regex-Markdown-Mapping läuft
//      (verhindert dass <script>, <img onerror>, <svg onload> usw.
//      aus dem Quelltext erhalten bleiben).
//   2. Nach dem Markdown-Mapping das Ergebnis durch DOMPurify
//      schicken (entfernt verbleibende gefährliche Konstrukte und
//      reinigt z. B. javascript:-URLs, on*-Attribute, etc.).
//
// Die vorhandenen Custom-CSS-Klassen (md-h2/3/4/5, md-p, md-ul,
// md-ol, md-li, md-oli, md-quote, md-hr, code-block, inline-code)
// bleiben in der DOMPurify-Default-Allowlist erhalten — kein
// visueller Bruch.

import DOMPurify from 'dompurify'

// Memoization-Cache: Sections sind nach Generation final, aber Vue ruft
// renderMarkdown bei jedem Re-Render erneut auf (v-html cached nicht).
// Ohne Cache laufen ~14 Regex-Passes + DOMPurify auf jedem Tick fuer
// jede sichtbare Section — das ist die Hauptursache der "Forced Reflow"-
// Violations bei Reports mit 5x3-4KB Sections.
//
// LRU-aehnlicher Cache: Map preserved Insertion-Order, bei >200 Eintraegen
// loeschen wir den aeltesten. 200 reicht fuer Report (~5 Sections) +
// Chat-Verlauf (~50 Messages) + Buffer.
const _renderCache = new Map()
const _RENDER_CACHE_MAX = 200

// HTML-Escape: neutralisiert sämtliche Steuerzeichen, BEVOR die
// Regex-Markdown-Transformation HTML-Tags injiziert.
const escapeHtml = (input) => {
  return String(input)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

// Erzeugt aus Markdown-Text das HTML, das die bestehenden CSS-Regeln
// (md-h2/h3/h4/h5/md-p/md-ul/md-ol/md-li/md-oli/md-quote/md-hr/
// code-block/inline-code) erwarten. Identisch zur bisherigen
// Inline-Implementierung in Step4Report/Step5Interaction, aber:
//   - Eingabe ist HTML-escaped → keine User-Tags überleben
//   - Ergebnis wird mit DOMPurify nachsanitisiert
//
// Beispiel-Beweis: Input "<script>alert(1)</script>**fett**"
//   → escape → "&lt;script&gt;alert(1)&lt;/script&gt;**fett**"
//   → markdown → "<p class=\"md-p\">&lt;script&gt;alert(1)&lt;/script&gt;<strong>fett</strong></p>"
//   → kein ausführbares <script> mehr im DOM.
export const renderMarkdown = (content) => {
  if (!content) return ''

  // Cache-Hit-Path: identischer Input -> identischer Output, kein
  // Bedarf an erneuter Regex-Verarbeitung. Bei 5 Sections im Report
  // sparen wir bei jedem Re-Render 5x ~10ms = 50ms reflow-Zeit.
  const cached = _renderCache.get(content)
  if (cached !== undefined) return cached

  // 1) Sämtliche HTML-relevanten Zeichen aus dem Quelltext escapen.
  const escaped = escapeHtml(content)

  // 2) Führendes "## …" (Sektions-Header) entfernen — Sektionsüberschrift
  //    wird im Layout selbst gerendert.
  let processedContent = escaped.replace(/^##\s+.+\n+/, '')

  // 3) Code-Blöcke und Inline-Code.
  let html = processedContent.replace(
    /```(\w*)\n([\s\S]*?)```/g,
    '<pre class="code-block"><code>$2</code></pre>'
  )
  html = html.replace(/`([^`]+)`/g, '<code class="inline-code">$1</code>')

  // 4) Headings.
  html = html.replace(/^#### (.+)$/gm, '<h5 class="md-h5">$1</h5>')
  html = html.replace(/^### (.+)$/gm, '<h4 class="md-h4">$1</h4>')
  html = html.replace(/^## (.+)$/gm, '<h3 class="md-h3">$1</h3>')
  html = html.replace(/^# (.+)$/gm, '<h2 class="md-h2">$1</h2>')

  // 5) Quotes.
  html = html.replace(/^> (.+)$/gm, '<blockquote class="md-quote">$1</blockquote>')

  // 6) Listen (mit Sub-Level über data-level).
  html = html.replace(/^(\s*)- (.+)$/gm, (match, indent, text) => {
    const level = Math.floor(indent.length / 2)
    return `<li class="md-li" data-level="${level}">${text}</li>`
  })
  html = html.replace(/^(\s*)(\d+)\. (.+)$/gm, (match, indent, num, text) => {
    const level = Math.floor(indent.length / 2)
    return `<li class="md-oli" data-level="${level}">${text}</li>`
  })

  // 7) Listen-Wrapper.
  html = html.replace(/(<li class="md-li"[^>]*>.*?<\/li>\s*)+/g, '<ul class="md-ul">$&</ul>')
  html = html.replace(/(<li class="md-oli"[^>]*>.*?<\/li>\s*)+/g, '<ol class="md-ol">$&</ol>')

  // 8) Whitespace-Cleanup zwischen Listen-Items.
  html = html.replace(/<\/li>\s+<li/g, '</li><li')
  html = html.replace(/<ul class="md-ul">\s+/g, '<ul class="md-ul">')
  html = html.replace(/<ol class="md-ol">\s+/g, '<ol class="md-ol">')
  html = html.replace(/\s+<\/ul>/g, '</ul>')
  html = html.replace(/\s+<\/ol>/g, '</ol>')

  // 9) Inline-Formatting.
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
  html = html.replace(/\*(.+?)\*/g, '<em>$1</em>')
  html = html.replace(/_(.+?)_/g, '<em>$1</em>')

  // 10) Horizontal Rule.
  html = html.replace(/^---$/gm, '<hr class="md-hr">')

  // 11) Absatz-/Zeilenumbrüche.
  html = html.replace(/\n\n/g, '</p><p class="md-p">')
  html = html.replace(/\n/g, '<br>')
  html = '<p class="md-p">' + html + '</p>'

  // 12) Cleanup leerer/doppelter Wrapper.
  html = html.replace(/<p class="md-p"><\/p>/g, '')
  html = html.replace(/<p class="md-p">(<h[2-5])/g, '$1')
  html = html.replace(/(<\/h[2-5]>)<\/p>/g, '$1')
  html = html.replace(/<p class="md-p">(<ul|<ol|<blockquote|<pre|<hr)/g, '$1')
  html = html.replace(/(<\/ul>|<\/ol>|<\/blockquote>|<\/pre>)<\/p>/g, '$1')
  html = html.replace(/<br>\s*(<ul|<ol|<blockquote)/g, '$1')
  html = html.replace(/(<\/ul>|<\/ol>|<\/blockquote>)\s*<br>/g, '$1')
  html = html.replace(/<p class="md-p">(<br>\s*)+(<ul|<ol|<blockquote|<pre|<hr)/g, '$2')
  html = html.replace(/(<br>\s*){2,}/g, '<br>')
  html = html.replace(/(<\/ol>|<\/ul>|<\/blockquote>)<br>(<p|<div)/g, '$1$2')

  // 13) Fortlaufende Nummerierung über getrennte Single-Item-<ol>s.
  const tokens = html.split(/(<ol class="md-ol">(?:<li class="md-oli"[^>]*>[\s\S]*?<\/li>)+<\/ol>)/g)
  let olCounter = 0
  let inSequence = false
  for (let i = 0; i < tokens.length; i++) {
    if (tokens[i].startsWith('<ol class="md-ol">')) {
      const liCount = (tokens[i].match(/<li class="md-oli"/g) || []).length
      if (liCount === 1) {
        olCounter++
        if (olCounter > 1) {
          tokens[i] = tokens[i].replace('<ol class="md-ol">', `<ol class="md-ol" start="${olCounter}">`)
        }
        inSequence = true
      } else {
        olCounter = 0
        inSequence = false
      }
    } else if (inSequence) {
      if (/<h[2-5]/.test(tokens[i])) {
        olCounter = 0
        inSequence = false
      }
    }
  }
  html = tokens.join('')

  // 14) Final-Stage: DOMPurify als Defense-in-Depth.
  //     `data-level` ist ein eigenes data-Attribut und wird von
  //     DOMPurify per Default behalten. `class` und `start` ebenfalls.
  const sanitized = DOMPurify.sanitize(html, {
    ADD_ATTR: ['class', 'data-level', 'start']
  })

  // Cache-Insert mit LRU-Eviction.
  if (_renderCache.size >= _RENDER_CACHE_MAX) {
    const firstKey = _renderCache.keys().next().value
    _renderCache.delete(firstKey)
  }
  _renderCache.set(content, sanitized)
  return sanitized
}

// Sanitisiert beliebiges HTML, das per innerHTML in Vue-h()-Render
// gepatched wird. Verwendet konsequent dieselbe DOMPurify-Konfiguration.
export const sanitizeHtml = (html) => {
  if (!html) return ''
  return DOMPurify.sanitize(String(html), {
    ADD_ATTR: ['class', 'data-level', 'start']
  })
}

// Minimal-Formatter für Antwort-Text, der nur **fett** und Zeilenumbrüche
// abbildet (wie früher inline in Step4Report.qa-text), aber:
//   - Eingabe wird zuerst HTML-escaped
//   - Bold/<br>-Mapping läuft auf dem escapedem String
// Das Resultat sollte zusätzlich mit sanitizeHtml() durch DOMPurify
// gehen, bevor es per innerHTML in den DOM gelangt.
export const escapeAndFormatBoldNewline = (text) => {
  if (!text) return ''
  return escapeHtml(text)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\n/g, '<br>')
}
