<script setup lang="ts">
import { computed } from "vue";

const props = withDefaults(
  defineProps<{
    tone?: "neutral" | "brand" | "warn" | "danger" | "info";
    dot?: boolean;
  }>(),
  { tone: "neutral", dot: false },
);

const TONE: Record<string, string> = {
  neutral: "bg-line-soft text-ink-soft",
  brand: "bg-brand-soft text-brand-dark",
  warn: "bg-warn-soft text-warn",
  danger: "bg-danger-soft text-danger",
  info: "bg-info-soft text-info",
};

const classes = computed(() => TONE[props.tone] ?? TONE.neutral);
</script>

<template>
  <span
    class="inline-flex items-center gap-1.5 rounded-full px-2.5 py-[3px] text-body-sm leading-none whitespace-nowrap"
    :class="classes"
  >
    <span v-if="dot" class="size-1.5 rounded-full bg-current" />
    <slot />
  </span>
</template>
