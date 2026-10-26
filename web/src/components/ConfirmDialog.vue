<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import { t } from "../lib/i18n";

/**
 * 确认弹层。除了外观，这里还负责三件键盘/读屏的事：
 * - `role="dialog"` + `aria-modal` + 标题/说明关联，读屏能正确播报；
 * - Esc 关闭（等同取消）；
 * - Tab / Shift+Tab 圈在弹层内，关闭后焦点回到触发元素。
 */
const props = withDefaults(
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

const panel = ref<HTMLElement | null>(null);
let restoreFocus: HTMLElement | null = null;

const titleId = computed(() => `confirm-title-${props.title.replace(/\W+/g, "-")}`);
const messageId = computed(() => `confirm-message-${props.title.replace(/\W+/g, "-")}`);

function focusable(): HTMLElement[] {
  if (!panel.value) return [];
  return Array.from(
    panel.value.querySelectorAll<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  );
}

watch(
  () => props.open,
  async (open) => {
    if (open) {
      restoreFocus = document.activeElement as HTMLElement | null;
      await nextTick();
      const nodes = focusable();
      // 危险操作的默认焦点给「取消」，避免回车误删。
      const target = props.tone === "danger" ? nodes[0] : nodes[nodes.length - 1];
      (target ?? panel.value)?.focus();
    } else {
      restoreFocus?.focus?.();
      restoreFocus = null;
    }
  },
);

function onKeydown(event: KeyboardEvent): void {
  if (!props.open) return;
  if (event.key === "Escape") {
    event.stopPropagation();
    if (!props.busy) emit("cancel");
    return;
  }
  if (event.key !== "Tab") return;
  const nodes = focusable();
  if (!nodes.length) {
    event.preventDefault();
    panel.value?.focus();
    return;
  }
  const first = nodes[0];
  const last = nodes[nodes.length - 1];
  const active = document.activeElement as HTMLElement | null;
  if (event.shiftKey && (active === first || active === panel.value)) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && active === last) {
    event.preventDefault();
    first.focus();
  }
}
</script>

<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="fixed inset-0 grid place-items-center bg-black/25 p-4"
      :style="{ zIndex }"
      @click.self="emit('cancel')"
      @keydown="onKeydown"
    >
      <div
        ref="panel"
        role="dialog"
        aria-modal="true"
        :aria-labelledby="titleId"
        :aria-describedby="messageId"
        tabindex="-1"
        class="max-h-[calc(100vh-2rem)] w-full max-w-[min(430px,calc(100vw-2rem))] overflow-y-auto rounded-card border border-line bg-surface p-5"
      >
        <h3 :id="titleId" class="text-title font-semibold text-ink">{{ title }}</h3>
        <p :id="messageId" class="mt-2 whitespace-pre-line text-body leading-relaxed text-ink-soft">
          {{ message }}
        </p>
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
