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
  padding: 0.4rem 1rem;
  background: rgba(255, 255, 255, 0.92);
  border-top: 1px solid #e5e5e5;
  font-family: inherit;
  font-size: 0.7rem;
  color: #555555;
  text-align: right;
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
