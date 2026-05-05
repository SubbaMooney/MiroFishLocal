<template>
  <div class="toast-host" aria-live="polite" aria-atomic="true">
    <transition-group name="toast">
      <div
        v-for="t in toasts"
        :key="t.id"
        :class="['toast', `toast--${t.level}`]"
        role="status"
        @click="dismiss(t.id)"
      >
        <span class="toast__msg">{{ t.message }}</span>
        <button class="toast__close" :aria-label="$t('common.close') || 'Schliessen'" @click.stop="dismiss(t.id)">
          &times;
        </button>
      </div>
    </transition-group>
  </div>
</template>

<script setup>
import { onMounted, onBeforeUnmount, ref } from 'vue'
import { NOTIFY_EVENT } from '../utils/notify'

const toasts = ref([])
let nextId = 1

const dismiss = (id) => {
  toasts.value = toasts.value.filter(t => t.id !== id)
}

const onNotify = (event) => {
  const { level = 'info', message, timeoutMs = 6000 } = event.detail || {}
  if (!message) return
  const id = nextId++
  toasts.value = [...toasts.value, { id, level, message }]
  if (timeoutMs > 0) {
    setTimeout(() => dismiss(id), timeoutMs)
  }
}

onMounted(() => {
  window.addEventListener(NOTIFY_EVENT, onNotify)
})
onBeforeUnmount(() => {
  window.removeEventListener(NOTIFY_EVENT, onNotify)
})
</script>

<style scoped>
.toast-host {
  position: fixed;
  bottom: 1.5rem;
  right: 1.5rem;
  z-index: 9999;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  max-width: 24rem;
  pointer-events: none;
}

.toast {
  pointer-events: auto;
  display: flex;
  align-items: flex-start;
  gap: 0.75rem;
  padding: 0.75rem 1rem;
  border: 2px solid #000000;
  background: #ffffff;
  font-family: inherit;
  font-size: 0.875rem;
  line-height: 1.4;
  box-shadow: 4px 4px 0 0 #000000;
  cursor: pointer;
}

.toast--warn {
  background: #fff8e1;
}

.toast--error {
  background: #ffebee;
}

.toast__msg {
  flex: 1;
  word-break: break-word;
}

.toast__close {
  background: transparent;
  border: 0;
  font-size: 1.25rem;
  line-height: 1;
  padding: 0 0.25rem;
  cursor: pointer;
  color: inherit;
}

.toast-enter-active,
.toast-leave-active {
  transition: all 0.2s ease;
}

.toast-enter-from {
  opacity: 0;
  transform: translateX(2rem);
}

.toast-leave-to {
  opacity: 0;
  transform: translateX(2rem);
}
</style>
