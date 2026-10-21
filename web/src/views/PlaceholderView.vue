<script setup lang="ts">
import { Construction } from "lucide-vue-next";
import StatusPill from "../components/StatusPill.vue";
import { t } from "../lib/i18n";

defineProps<{ title: string; stage: string; detail: string }>();
</script>

<template>
  <div>
    <h1 class="text-[22px] font-semibold tracking-tight text-ink">{{ title }}</h1>
    <p class="mt-1 text-[12.5px] text-muted">{{ detail }}</p>

    <div
      class="mt-6 flex flex-col items-center gap-3 rounded-[var(--radius-card)] border border-dashed border-line bg-surface px-6 py-16 text-center"
    >
      <Construction :size="22" :stroke-width="1.7" class="text-muted" />
      <StatusPill tone="info">{{ t("placeholder.stage", { stage }) }}</StatusPill>
      <p class="max-w-[380px] text-[13px] leading-relaxed text-ink-soft">
        {{ t("placeholder.notImplemented") }}
      </p>
      <p class="text-[12px] text-muted">{{ t("placeholder.scope") }}</p>
    </div>
  </div>
</template>
