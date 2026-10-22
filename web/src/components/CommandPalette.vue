<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { CornerDownLeft, Search } from "lucide-vue-next";
import { t } from "../lib/i18n";
import { NAV_DESTINATIONS, SETTINGS_DESTINATION, type NavDestination } from "../lib/nav";
import { scopedPath } from "../stores/app";

/**
 * `Ctrl+K` 的落点：页面跳转面板（没有跨页搜索，就只做导航，不摆假搜索框）。
 * 由 AppShell 控制挂载，打开即聚焦输入框、Esc 关闭。
 */
const emit = defineEmits<{ close: [] }>();
const router = useRouter();

const keyword = ref("");
const cursor = ref(0);
const inputRef = ref<HTMLInputElement | null>(null);

const all: NavDestination[] = [...NAV_DESTINATIONS, SETTINGS_DESTINATION];

const matches = computed(() => {
  const query = keyword.value.trim().toLowerCase();
  if (!query) return all;
  return all.filter(
    (item) =>
      t(item.labelKey).toLowerCase().includes(query) ||
      item.key.includes(query) ||
      item.path.includes(query),
  );
});

onMounted(() => inputRef.value?.focus());

function go(index: number): void {
  const target = matches.value[index];
  if (!target) return;
  emit("close");
  void router.push(scopedPath(target.path));
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
    class="fixed inset-0 z-[1001] flex items-start justify-center bg-black/25 px-4 pt-[18vh]"
    @click.self="emit('close')"
  >
    <div
      class="w-full max-w-[420px] overflow-hidden rounded-[12px] border border-line bg-surface shadow-[0_18px_48px_-12px_rgba(28,30,28,0.35)]"
    >
      <div class="flex items-center gap-2.5 border-b border-line-soft px-4">
        <Search :size="15" :stroke-width="1.9" class="shrink-0 text-muted" />
        <input
          ref="inputRef"
          v-model="keyword"
          type="text"
          :placeholder="t('palette.placeholder')"
          class="h-11 flex-1 bg-transparent text-[13px] text-ink outline-none placeholder:text-muted"
          @input="onInput"
          @keydown="onKeydown"
        />
        <kbd class="shrink-0 rounded-[5px] border border-line px-1.5 py-0.5 text-[10.5px] text-muted">
          Esc
        </kbd>
      </div>

      <p v-if="!matches.length" class="px-4 py-6 text-center text-[12.5px] text-muted">
        {{ t("palette.empty") }}
      </p>

      <ul v-else class="max-h-[320px] overflow-y-auto p-1.5">
        <li v-for="(item, index) in matches" :key="item.key">
          <button
            type="button"
            class="flex w-full items-center gap-2.5 rounded-[9px] px-2.5 py-2 text-left text-[13px] transition-colors"
            :class="
              index === cursor
                ? 'bg-brand-soft font-medium text-brand-dark'
                : 'text-ink-soft hover:bg-line-soft hover:text-ink'
            "
            @mousemove="cursor = index"
            @click="go(index)"
          >
            <component :is="item.icon" :size="16" :stroke-width="1.9" class="shrink-0" />
            <span class="flex-1 truncate">{{ t(item.labelKey) }}</span>
            <CornerDownLeft v-if="index === cursor" :size="13" :stroke-width="1.9" />
          </button>
        </li>
      </ul>
    </div>
  </div>
</template>
