import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import i18n from './i18n'

// Selbst-gehostete Fonts via @fontsource (kein CDN-Egress, alles lokal gebundlet)
// Inter: Latin only, Weights 300-700
import '@fontsource/inter/300.css'
import '@fontsource/inter/400.css'
import '@fontsource/inter/500.css'
import '@fontsource/inter/600.css'
import '@fontsource/inter/700.css'
// JetBrains Mono: variable wght (Latin)
import '@fontsource-variable/jetbrains-mono'
// Noto Sans SC: Chinese subset, Weights 300-900 (volles SC subset reicht)
import '@fontsource/noto-sans-sc/300.css'
import '@fontsource/noto-sans-sc/400.css'
import '@fontsource/noto-sans-sc/500.css'
import '@fontsource/noto-sans-sc/700.css'
import '@fontsource/noto-sans-sc/800.css'
import '@fontsource/noto-sans-sc/900.css'
// Space Grotesk: variable wght (Latin)
import '@fontsource-variable/space-grotesk'

const app = createApp(App)

app.use(router)
app.use(i18n)

app.mount('#app')
