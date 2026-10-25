<script setup lang="ts">
import { t } from "../lib/i18n";

withDefaults(
  defineProps<{
    open: boolean;
    title: string;
    message: string;
    confirmText?: string;
    tone?: "brand" | "danger";
    busy?: boolean;
    /** 层级：默认与 --z-modal 一致；叠在其它模态里时传更大的值（如回收站里 1002）。 */
    zIndex?: number;
  }>(),
  // 默认直接压到模态层：50 会落在侧栏（z-200）和下拉（z-900）下面。
  { tone: "brand", busy: false, zIndex: 1001 },
);

const emit = defineEmits<{ confirm: []; cancel: [] }>();
</script>

<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="fixed inset-0 grid place-items-center bg-black/25 p-4"
      :style="{ zIndex }"
      @click.self="emit('cancel')"
    >
      <div
        class="max-h-[calc(100vh-2rem)] w-full max-w-[min(430px,calc(100vw-2rem))] overflow-y-auto rounded-card border border-line bg-surface p-5"
      >
        <h3 class="text-title font-semibold text-ink">{{ title }}</h3>
        <p class="mt-2 whitespace-pre-line text-body leading-relaxed text-ink-soft">{{ message }}</p>
        <div class="mt-5 flex justify-end gap-2">
          <button
            type="button"
            class="rounded-control border border-line px-3.5 py-2 text-body text-ink-soft transition-colors hover:border-line-strong hover:text-ink"
            :disabled="busy"
            @click="emit('cancel')"
          >
            {{ t("common.cancel") }}
          </button>
          <button
            type="button"
            class="rounded-control px-3.5 py-2 text-body font-medium text-white transition-colors disabled:opacity-60"
            :class="tone === 'danger' ? 'bg-danger hover:bg-danger-dark' : 'bg-brand hover:bg-brand-dark'"
            :disabled="busy"
            @click="emit('confirm')"
          >
            {{ busy ? t("dialog.processing") : confirmText || t("common.confirm") }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>
