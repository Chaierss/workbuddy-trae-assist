<script setup lang="ts">
import { computed } from "vue";
import { FlaskConical, ShieldCheck } from "lucide-vue-next";
import { t, type MessageKey } from "../lib/i18n";
import { resolveMode, setMode, type ModeSource } from "../lib/devMode";

/**
 * 模式角标：让「现在开的是哪个形态」始终可见，hover 能说清判定来源。
 *
 * 点击切换要整页刷新（`setMode` 内部 reload），所以这里不做局部状态同步：
 * 半开发半线上的界面比纯线上更危险。
 */
const state = computed(() => resolveMode());
const sourceKey = computed<MessageKey>(
  () => `settings.mode.source.${state.value.source}` as MessageKey,
);

const dev = computed(() => state.value.mode === "dev");
const label = computed(() => {
  if (!dev.value) return t("settings.mode.prod");
  return state.value.elevated ? t("settings.mode.devTemp") : t("settings.mode.dev");
});

const hint = computed(() => {
  const base = t("settings.mode.hint", { source: t(sourceKey.value) });
  const extra = state.value.elevated ? t("settings.mode.hintElevated") : "";
  return `${base}${extra} · ${t("settings.mode.toggle")}`;
});

function toggle(): void {
  const next = dev.value ? "prod" : "dev";
  if (!setMode(next)) window.alert(t("settings.mode.denied"));
}
</script>

<template>
  <button
    type="button"
    class="flex w-full items-center gap-2 rounded-control px-2 py-1.5 text-left text-caption transition-colors"
    :class="dev ? 'bg-warn-soft text-warn hover:bg-warn-soft/80' : 'text-muted hover:bg-line-soft hover:text-ink'"
    :title="hint"
    @click="toggle"
  >
    <FlaskConical v-if="dev" :size="13" class="shrink-0" />
    <ShieldCheck v-else :size="13" class="shrink-0" />
    <span class="min-w-0 flex-1 truncate font-medium">{{ label }}</span>
    <span class="shrink-0 font-mono text-micro opacity-70">
      {{ t(sourceKey) }}
    </span>
  </button>
</template>
