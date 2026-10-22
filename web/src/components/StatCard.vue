<script setup lang="ts">
import type { Component } from "vue";
import { RouterLink } from "vue-router";
import AnimatedNumber from "./AnimatedNumber.vue";

defineProps<{
  label: string;
  /** 纯文本值（如「已连接」这类非数字态）；给了 `number` 就忽略它。 */
  value?: string;
  /** 数字值：用滚动动效渲染，与号池健康度卡片同一套。 */
  number?: number;
  hint?: string;
  icon?: Component;
  /** 强调态：用于「需要处理」这类需要用户注意的数字。 */
  tone?: "neutral" | "brand" | "warn" | "danger";
  /** 点击跳转（可选）：数字不该只是展示，能点过去就点过去。 */
  to?: string;
}>();
</script>

<template>
  <component
    :is="to ? RouterLink : 'div'"
    :to="to"
    class="group block rounded-[var(--radius-card)] border border-line bg-surface px-5 py-4 shadow-card transition-all duration-200"
    :class="to ? 'hover:-translate-y-px hover:border-[#d7dad5] hover:shadow-card-hover' : 'hover:border-[#d7dad5]'"
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
      <AnimatedNumber v-if="number !== undefined" :value="number" />
      <template v-else>{{ value }}</template>
    </div>
    <div v-if="hint" class="mt-2 truncate text-[11.5px] text-muted">{{ hint }}</div>
  </component>
</template>
