<script setup lang="ts">
defineProps<{ title?: string; subtitle?: string; flush?: boolean }>();
</script>

<template>
  <section
    class="overflow-hidden rounded-card border border-line bg-surface shadow-card"
  >
    <header
      v-if="title || $slots.actions"
      class="flex items-center justify-between gap-4 border-b border-line-soft px-5 py-3.5"
    >
      <div class="min-w-0">
        <h2 class="truncate text-body font-semibold tracking-tight text-ink">{{ title }}</h2>
        <p v-if="subtitle" class="mt-0.5 truncate text-caption text-muted">{{ subtitle }}</p>
      </div>
      <div class="flex shrink-0 items-center gap-2">
        <slot name="actions" />
      </div>
    </header>
    <div :class="flush ? '' : 'px-5 py-4'">
      <slot />
    </div>
  </section>
</template>
