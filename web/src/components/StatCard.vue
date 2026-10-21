<script setup lang="ts">
import type { Component } from "vue";

defineProps<{
  label: string;
  value: string;
  hint?: string;
  icon?: Component;
  /** 强调态：用于「需要处理」这类需要用户注意的数字。 */
  tone?: "neutral" | "brand" | "warn" | "danger";
}>();
</script>

<template>
  <div
    class="group rounded-[var(--radius-card)] border border-line bg-surface px-5 py-4 shadow-card transition-colors hover:border-[#d7dad5]"
  >
    <div class="flex items-center gap-2">
      <span
        v-if="icon"
        class="grid size-6 shrink-0 place-items-center rounded-[7px] border"
        :class="
          tone === 'warn'
            ? 'border-warn/20 bg-warn-soft text-warn'
            : tone === 'danger'
              ? 'border-danger/20 bg-danger-soft text-danger'
              : tone === 'brand'
                ? 'border-brand/20 bg-brand-soft text-brand'
                : 'border-line bg-line-soft text-ink-soft'
        "
      >
        <component :is="icon" :size="13" :stroke-width="1.9" />
      </span>
      <span class="truncate text-[11.5px] font-medium tracking-wide text-muted">{{ label }}</span>
    </div>
    <div class="mt-2.5 text-[27px] leading-none font-semibold tracking-tight tabular-nums text-ink">
      {{ value }}
    </div>
    <div v-if="hint" class="mt-2 truncate text-[11.5px] text-muted">{{ hint }}</div>
  </div>
</template>
