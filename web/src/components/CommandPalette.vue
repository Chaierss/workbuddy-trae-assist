<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { CornerDownLeft, Search, Users } from "lucide-vue-next";
import { t } from "../lib/i18n";
import { NAV_DESTINATIONS, SETTINGS_DESTINATION } from "../lib/nav";
import { matchPalette, type PaletteResult } from "../lib/paletteResults";
import { openSwitch } from "../lib/switchFlow";
import { accounts, scopedPath } from "../stores/app";

/**
 * `Ctrl+K` 的落点：页面跳转 + 账号换号。
 * 由 AppShell 控制挂载，打开即聚焦输入框、Esc 关闭。
 */
const emit = defineEmits<{ close: [] }>();
const router = useRouter();

const keyword = ref("");
const cursor = ref(0);
const inputRef = ref<HTMLInputElement | null>(null);

const destinations = [...NAV_DESTINATIONS, SETTINGS_DESTINATION];

const matches = computed<PaletteResult[]>(() =>
  matchPalette(keyword.value, destinations, accounts.value),
);

onMounted(() => inputRef.value?.focus());

/** 分组标题只在类型第一次出现时显示。 */
function showGroup(index: number): boolean {
  if (index === 0) return true;
  return matches.value[index].kind !== matches.value[index - 1].kind;
}

function groupLabel(item: PaletteResult): string {
  return item.kind === "nav" ? t("palette.groupPages") : t("palette.groupAccounts");
}

function go(index: number): void {
  const target = matches.value[index];
  if (!target) return;
  emit("close");
  if (target.kind === "nav") {
    void router.push(scopedPath(target.path));
    return;
  }
  void openSwitch(target.account, "command");
}

function move(step: number): void {
  const count = matches.value.length;
  if (!count) return;
  cursor.value = (cursor.value + step + count) % count;
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") {
    emit("close");
    return;
  }
  if (event.key === "ArrowDown") {
    event.preventDefault();
    move(1);
    return;
  }
  if (event.key === "ArrowUp") {
    event.preventDefault();
    move(-1);
    return;
  }
  if (event.key === "Enter") {
    event.preventDefault();
    go(cursor.value);
  }
}

/** 输入变化后结果集变了，光标回到第一项，避免越界后回车跳错页。 */
function onInput(): void {
  cursor.value = 0;
}
</script>

<template>
  <div
    class="fixed inset-0 z-[var(--z-modal)] flex items-start justify-center bg-black/25 px-4 pt-[18vh]"
    @click.self="emit('close')"
  >
    <div
      class="w-full max-w-[440px] overflow-hidden rounded-card border border-line bg-surface shadow-float"
    >
      <div class="flex items-center gap-2.5 border-b border-line-soft px-4">
        <Search :size="15" :stroke-width="1.9" class="shrink-0 text-muted" />
        <input
          ref="inputRef"
          v-model="keyword"
          type="text"
          :placeholder="t('palette.placeholder')"
          class="h-11 flex-1 bg-transparent text-body text-ink outline-none placeholder:text-muted"
          @input="onInput"
          @keydown="onKeydown"
        />
        <kbd class="shrink-0 rounded-badge border border-line px-1.5 py-0.5 text-micro text-muted">
          Esc
        </kbd>
      </div>

      <p v-if="!matches.length" class="px-4 py-6 text-center text-body-sm text-muted">
        {{ keyword.trim() ? t("palette.empty") : t("palette.searchAccounts") }}
      </p>

      <ul v-else class="max-h-[320px] overflow-y-auto p-1.5">
        <template v-for="(item, index) in matches" :key="item.key">
          <li
            v-if="showGroup(index)"
            class="px-2.5 pb-1 pt-2 text-micro font-medium uppercase tracking-wide text-muted"
          >
            {{ groupLabel(item) }}
          </li>
          <li>
            <button
              type="button"
              class="flex w-full items-center gap-2.5 rounded-control px-2.5 py-2 text-left text-body transition-colors"
              :class="
                index === cursor
                  ? 'bg-brand-soft font-medium text-brand-dark'
                  : 'text-ink-soft hover:bg-line-soft hover:text-ink'
              "
              @mousemove="cursor = index"
              @click="go(index)"
            >
              <component
                :is="item.kind === 'nav' ? item.icon : Users"
                :size="16"
                :stroke-width="1.9"
                class="shrink-0"
              />
              <span class="flex-1 truncate">{{ item.label }}</span>
              <span v-if="item.kind === 'account'" class="shrink-0 text-micro text-muted">
                {{ item.hint }}
              </span>
              <CornerDownLeft v-if="index === cursor" :size="13" :stroke-width="1.9" />
            </button>
          </li>
        </template>
      </ul>
    </div>
  </div>
</template>
