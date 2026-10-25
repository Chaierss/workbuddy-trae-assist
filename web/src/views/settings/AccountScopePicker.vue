<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { Check, ChevronDown, Search } from "lucide-vue-next";
import { t } from "../../lib/i18n";

/**
 * 账号范围选择器：多选 + 搜索 + 全选 / 清空。
 *
 * 语义与后端一致：**空数组 = 号池全部账号**。所以这里没有「全不选」——
 * 清空选择就是「全部账号」，界面上直接这么写，避免「全不选反而全跑」的错觉。
 */
export interface ScopeAccount {
  id: string;
  displayName: string;
  platform: string;
}

const props = withDefaults(
  defineProps<{
    modelValue: string[];
    accounts: ScopeAccount[];
    disabled?: boolean;
  }>(),
  { disabled: false },
);

const emit = defineEmits<{ "update:modelValue": [value: string[]] }>();

const PLATFORM_LABEL: Record<string, string> = {
  workbuddy: "WorkBuddy",
  trae: "Trae",
};

const open = ref(false);
const query = ref("");
const root = ref<HTMLElement | null>(null);
const trigger = ref<HTMLButtonElement | null>(null);
const panel = ref<HTMLElement | null>(null);
const searchInput = ref<HTMLInputElement | null>(null);
const placeUp = ref(false);
const anchor = ref({ left: 0, top: 0, bottom: 0, width: 0 });

const selected = computed(() => new Set(props.modelValue));

const summary = computed(() =>
  props.modelValue.length
    ? t("schedule.scopeSelected", { count: props.modelValue.length })
    : t("schedule.scopeAll"),
);

const filtered = computed(() => {
  const keyword = query.value.trim().toLowerCase();
  if (!keyword) return props.accounts;
  return props.accounts.filter((account) =>
    `${account.displayName} ${PLATFORM_LABEL[account.platform] ?? account.platform}`
      .toLowerCase()
      .includes(keyword),
  );
});

const allSelected = computed(
  () =>
    props.accounts.length > 0 &&
    props.accounts.every((account) => selected.value.has(account.id)),
);

const panelStyle = computed(() => {
  const width = Math.min(Math.max(anchor.value.width, 248), 340);
  const left = Math.max(8, Math.min(anchor.value.left, window.innerWidth - width - 8));
  const style: Record<string, string> = { width: `${width}px`, left: `${left}px` };
  if (placeUp.value) style.bottom = `${window.innerHeight - anchor.value.top + 6}px`;
  else style.top = `${anchor.value.bottom + 6}px`;
  return style;
});

function position(): void {
  const el = trigger.value;
  if (!el) return;
  const rect = el.getBoundingClientRect();
  anchor.value = { left: rect.left, top: rect.top, bottom: rect.bottom, width: rect.width };
  const estimated = panel.value?.offsetHeight || 300;
  const below = window.innerHeight - rect.bottom - 12;
  placeUp.value = below < estimated && rect.top - 12 > below;
}

function toggle(): void {
  if (props.disabled) return;
  open.value = !open.value;
}

function close(): void {
  open.value = false;
}

function toggleAccount(accountId: string): void {
  const next = selected.value.has(accountId)
    ? props.modelValue.filter((item) => item !== accountId)
    : [...props.modelValue, accountId];
  emit("update:modelValue", next);
}

/** 全选 = 明确列出全部账号；清空 = 回到「全部账号」语义。 */
function selectAll(): void {
  emit(
    "update:modelValue",
    props.accounts.map((account) => account.id),
  );
}

function clearAll(): void {
  emit("update:modelValue", []);
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") {
    event.stopPropagation();
    close();
  }
}

function onDocumentPointer(event: MouseEvent): void {
  const target = event.target as Node;
  if (root.value?.contains(target) || panel.value?.contains(target)) return;
  close();
}

function onViewportChange(): void {
  if (open.value) position();
}

watch(open, async (value) => {
  if (!value) return;
  query.value = "";
  await nextTick();
  position();
  searchInput.value?.focus();
});

onMounted(() => {
  document.addEventListener("mousedown", onDocumentPointer);
  window.addEventListener("scroll", onViewportChange, true);
  window.addEventListener("resize", onViewportChange);
});
onBeforeUnmount(() => {
  document.removeEventListener("mousedown", onDocumentPointer);
  window.removeEventListener("scroll", onViewportChange, true);
  window.removeEventListener("resize", onViewportChange);
});
</script>

<template>
  <div ref="root" class="relative">
    <button
      ref="trigger"
      type="button"
      class="flex w-full items-center gap-1.5 rounded-badge border border-line bg-surface px-1.5 py-0.5 text-left text-body-sm text-ink transition-colors duration-150 hover:border-line-strong disabled:cursor-not-allowed disabled:opacity-60"
      :class="open ? 'border-brand' : ''"
      :disabled="disabled"
      :title="t('schedule.scopeHint')"
      aria-haspopup="dialog"
      :aria-expanded="open"
      @click="toggle"
      @keydown="onKeydown"
    >
      <span class="min-w-0 flex-1 truncate" :class="modelValue.length ? '' : 'text-muted'">
        {{ summary }}
      </span>
      <ChevronDown
        :size="12"
        :stroke-width="2"
        class="shrink-0 text-muted transition-transform duration-150"
        :class="open ? 'rotate-180' : ''"
      />
    </button>

    <Teleport to="body">
      <Transition name="popover">
        <div
          v-if="open"
          ref="panel"
          class="fixed z-[900] rounded-control border border-line bg-surface p-2 shadow-[var(--shadow-float)]"
          :style="panelStyle"
          role="dialog"
          @keydown="onKeydown"
        >
          <div class="flex items-center gap-1.5 rounded-control border border-line-soft px-2 py-1">
            <Search :size="12" :stroke-width="2" class="shrink-0 text-muted" />
            <input
              ref="searchInput"
              v-model="query"
              type="text"
              class="min-w-0 flex-1 bg-transparent text-body-sm text-ink outline-none placeholder:text-muted"
              :placeholder="t('schedule.scopeSearch')"
            />
          </div>

          <div class="mt-1.5 flex items-center gap-1">
            <button
              type="button"
              class="rounded-badge border border-line px-1.5 py-0.5 text-caption text-ink-soft transition-colors hover:border-brand hover:text-brand-dark disabled:cursor-not-allowed disabled:opacity-45"
              :disabled="allSelected"
              @click="selectAll"
            >
              {{ t("schedule.scopeSelectAll") }}
            </button>
            <button
              type="button"
              class="rounded-badge border border-line px-1.5 py-0.5 text-caption text-ink-soft transition-colors hover:border-brand hover:text-brand-dark disabled:cursor-not-allowed disabled:opacity-45"
              :disabled="!modelValue.length"
              @click="clearAll"
            >
              {{ t("schedule.scopeSelectNone") }}
            </button>
            <span class="ml-auto text-caption text-muted">{{ summary }}</span>
          </div>

          <div class="mt-1 max-h-[240px] overflow-y-auto">
            <div v-if="!accounts.length" class="px-2 py-2 text-body-sm text-muted">
              {{ t("schedule.scopeEmpty") }}
            </div>
            <div v-else-if="!filtered.length" class="px-2 py-2 text-body-sm text-muted">
              {{ t("schedule.scopeNoMatch") }}
            </div>
            <button
              v-for="account in filtered"
              :key="account.id"
              type="button"
              class="flex w-full items-center gap-2 rounded-control px-2 py-1.5 text-left text-body-sm text-ink-soft transition-colors hover:bg-line-soft"
              @click="toggleAccount(account.id)"
            >
              <span
                class="grid size-3.5 shrink-0 place-items-center rounded-badge border"
                :class="
                  selected.has(account.id)
                    ? 'border-brand bg-brand text-white'
                    : 'border-line bg-surface'
                "
              >
                <Check
                  v-if="selected.has(account.id)"
                  :size="9"
                  :stroke-width="3"
                  class="shrink-0"
                />
              </span>
              <span class="min-w-0 flex-1 truncate">{{ account.displayName }}</span>
              <span class="shrink-0 text-micro text-muted">
                {{ PLATFORM_LABEL[account.platform] ?? account.platform }}
              </span>
            </button>
          </div>

          <p class="mt-1 border-t border-line-soft pt-1.5 text-caption leading-relaxed text-muted">
            {{ t("schedule.scopeHint") }}
          </p>
        </div>
      </Transition>
    </Teleport>
  </div>
</template>
