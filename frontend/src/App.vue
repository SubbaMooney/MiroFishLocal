<template>
  <router-view />
  <ToastHost />
  <AgplFooter />
</template>

<script setup>
// 使用 Vue Router 来管理页面
import ToastHost from './components/ToastHost.vue'
import AgplFooter from './components/AgplFooter.vue'
</script>

<style>
/* 全局样式重置 */
* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

/* AGPL-Footer ist position:fixed -- globale Hoehe als CSS-Variable
   damit alle Layout-Berechnungen (View-Hoehen + Footer selbst) aus
   einer Quelle leben. Views mit height:100vh muessen
   calc(100vh - var(--agpl-footer-h)) verwenden; scrollende Views
   mit min-height:100vh greifen auf body padding-bottom zurueck. */
:root {
  --agpl-footer-h: 32px;
}

body {
  padding-bottom: var(--agpl-footer-h);
}

#app {
  font-family: 'JetBrains Mono', 'Space Grotesk', 'Noto Sans SC', monospace;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  color: #000000;
  background-color: #ffffff;
}

/* 滚动条样式 */
::-webkit-scrollbar {
  width: 8px;
  height: 8px;
}

::-webkit-scrollbar-track {
  background: #f1f1f1;
}

::-webkit-scrollbar-thumb {
  background: #000000;
}

::-webkit-scrollbar-thumb:hover {
  background: #333333;
}

/* 全局按钮样式 */
button {
  font-family: inherit;
}
</style>
