<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { t } from "../lib/i18n";

/** 单个账号的短文本编辑：备注（默认）与分组共用同一个弹窗。 */
const props = defineProps<{
  open: boolean;
  accountName: string;
  note: string;
  busy?: boolean;
  /** 标题 / 说明 / 占位文案；不传则沿用备注那一套。 */
  title?: string;
  desc?: string;
  placeholder?: string;
  maxLength?: number;
}>();

const emit = defineEmits<{ save: [note: string]; close: [] }>();

const value = ref("");
const limit = computed(() => props.maxLength ?? 200);

watch(
  () => props.open,
  (open) => {
    if (open) value.value = props.note;
  },
);

function save(): void {
  emit("save", value.value.trim());
}
</script>

<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="fixed inset-0 z-[var(--z-modal)] grid place-items-center bg-black/25 p-4"
      @click.self="emit('close')"
    >
      <div
        class="max-h-[calc(100vh-2rem)] w-full max-w-[min(420px,calc(100vw-2rem))] overflow-y-auto rounded-card border border-line bg-surface p-5"
      >
        <h3 class="text-title font-semibold text-ink">{{ title ?? t("dialog.note.title") }}</h3>
        <p class="mt-1 text-body-sm text-muted">
          {{ desc ?? t("dialog.note.desc", { name: accountName }) }}
        </p>
        <input
          v-model="value"
          type="text"
          :maxlength="limit"
          :placeholder="placeholder ?? t('dialog.note.placeholder')"
          class="mt-3 w-full rounded-control border border-line bg-surface px-3 py-2 text-body text-ink outline-none transition-colors placeholder:text-muted focus:border-brand"
          @keyup.enter="save"
        />
        <div class="mt-4 flex justify-end gap-2">
          <button
            type="button"
            class="rounded-control border border-line px-3.5 py-2 text-body text-ink-soft transition-colors hover:border-line-strong hover:text-ink"
            :disabled="busy"
            @click="emit('close')"
          >
            {{ t("common.cancel") }}
          </button>
          <button
            type="button"
            class="rounded-control bg-brand px-3.5 py-2 text-body font-medium text-white transition-colors hover:bg-brand-dark disabled:opacity-60"
            :disabled="busy"
            @click="save"
          >
            {{ busy ? t("dialog.saving") : t("common.save") }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>
