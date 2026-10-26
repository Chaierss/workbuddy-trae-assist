<script setup lang="ts">
import { Search, X } from "lucide-vue-next";
import { ref } from "vue";
import { t } from "../lib/i18n";

/**
 * 搜索输入：与 `SelectMenu` / `SegmentedControl` 同高、同圆角。
 *
 * 以前各页手写 `h-8` + `max-w-[360px]`，和旁边的下拉对不齐。两档尺寸：
 * - `sm`：32px，用于工具栏一行内
 * - `md`：36px，用于页头下方的独立筛选行
 */
const props = withDefaults(
  defineProps<{
    modelValue: string;
    placeholder?: string;
    size?: "sm" | "md";
    /** 无障碍标签；省略时用 placeholder。 */
    label?: string;
  }>(),
  { placeholder: "", size: "sm", label: "" },
);

const emit = defineEmits<{ "update:modelValue": [string] }>();

const input = ref<HTMLInputElement | null>(null);

function clear(): void {
  emit("update:modelValue", "");
  input.value?.focus();
}
</script>

<template>
  <label
    class="inline-flex items-center gap-1.5 rounded-control border border-line bg-surface px-2.5 text-ink-soft transition-colors focus-within:border-line-strong hover:border-line-strong"
    :class="size === 'md' ? 'h-9' : 'h-8'"
  >
    <Search :size="13" :stroke-width="1.9" class="shrink-0 text-muted" />
    <input
      ref="input"
      :value="props.modelValue"
      type="search"
      :placeholder="props.placeholder"
      :aria-label="props.label || props.placeholder"
      class="min-w-0 flex-1 bg-transparent text-body-sm text-ink outline-none placeholder:text-muted [&::-webkit-search-cancel-button]:hidden"
      @input="emit('update:modelValue', ($event.target as HTMLInputElement).value)"
    />
    <button
      v-if="props.modelValue"
      type="button"
      class="shrink-0 rounded p-0.5 text-muted transition-colors hover:text-ink"
      :aria-label="t('common.clear')"
      @click="clear"
    >
      <X :size="12" :stroke-width="2" />
    </button>
  </label>
</template>
