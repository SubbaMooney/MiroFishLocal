<template>
  <footer class="agpl-footer" role="contentinfo">
    <span class="agpl-footer__text">
      MiroFish &mdash; AGPL-3.0 |
      <a
        :href="sourceUrl"
        target="_blank"
        rel="noopener noreferrer"
        class="agpl-footer__link"
      >
        Source-Code
      </a>
    </span>
  </footer>
</template>

<script setup>
// L7 (Audit-Folge): AGPL-3.0 Section 13 Network-Service-Klausel verlangt,
// dass User der Web-Instanz Zugang zum entsprechenden Source-Code haben.
// Standard-Repo-URL kann per VITE_SOURCE_URL ueberschrieben werden, damit
// Forks ihren eigenen Mirror eintragen koennen.
const sourceUrl =
  import.meta.env.VITE_SOURCE_URL ||
  'https://github.com/SubbaMooney/MiroFishLocal'
</script>

<style scoped>
.agpl-footer {
  position: fixed;
  bottom: 0;
  left: 0;
  right: 0;
  /* Hoehe muss zu --agpl-footer-h in App.vue passen, damit body
     genug Platz reserviert. Aktuell 32px = 0.4rem padding * 2 +
     ~1rem line-height der 0.7rem Schrift. */
  height: var(--agpl-footer-h, 32px);
  display: flex;
  align-items: center;
  justify-content: flex-end;
  padding: 0 1rem;
  background: rgba(255, 255, 255, 0.92);
  border-top: 1px solid #e5e5e5;
  font-family: inherit;
  font-size: 0.7rem;
  color: #555555;
  z-index: 50;
  pointer-events: auto;
  backdrop-filter: blur(2px);
}

.agpl-footer__link {
  color: #000000;
  text-decoration: underline;
}

.agpl-footer__link:hover {
  color: #444444;
}
</style>
