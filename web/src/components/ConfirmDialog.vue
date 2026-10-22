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
  }>(),
  { tone: "brand", busy: false },
);

const emit = defineEmits<{ confirm: []; cancel: [] }>();
</script>

<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="fixed inset-0 z-50 grid place-items-center bg-black/25 p-4"
      @click.self="emit('cancel')"
    >
      <div class="w-full max-w-[430px] rounded-[16px] border border-line bg-surface p-5">
        <h3 class="text-[15px] font-semibold text-ink">{{ title }}</h3>
        <p class="mt-2 whitespace-pre-line text-[13px] leading-relaxed text-ink-soft">{{ message }}</p>
        <div class="mt-5 flex justify-end gap-2">
          <button
            type="button"
            class="rounded-[9px] border border-line px-3.5 py-2 text-[13px] text-ink-soft transition-colors hover:border-[#d5d8d3] hover:text-ink"
            :disabled="busy"
            @click="emit('cancel')"
          >
            {{ t("common.cancel") }}
          </button>
          <button
            type="button"
            class="rounded-[9px] px-3.5 py-2 text-[13px] font-medium text-white transition-colors disabled:opacity-60"
            :class="tone === 'danger' ? 'bg-danger hover:bg-[#982724]' : 'bg-brand hover:bg-brand-dark'"
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
