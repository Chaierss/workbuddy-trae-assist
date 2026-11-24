<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import {
  Activity,
  CornerDownLeft,
  DatabaseBackup,
  HeartPulse,
  RefreshCw,
  ScanFace,
  Search,
  Users,
} from "lucide-vue-next";
import ConfirmDialog from "./ConfirmDialog.vue";
import { api } from "../api";
import { t } from "../lib/i18n";
import { NAV_DESTINATIONS, SETTINGS_DESTINATION } from "../lib/nav";
import type { NavDestination } from "../lib/nav";
import { matchPalette, type PaletteAction, type PaletteResult } from "../lib/paletteResults";
import { toastError, toastSuccess } from "../lib/toast";
import { openSwitch } from "../lib/switchFlow";
import { accounts, scope, scopedPath } from "../stores/app";
import { runTask } from "../stores/tasks";
import { SETTINGS_TABS, SETTINGS_DEFAULT_KEY } from "../views/settings/tabs";
import { GATEWAY_TABS } from "../views/gateway/tabs";

/**
 * `Ctrl+K` 的落点：页面跳转 + 设置/网关分栏 + 账号换号 + 可直接执行的维护动作。
 *
 * 两种形态：
 *   modal   浏览器形态的居中弹层（原来的样子）
 *   inline  桌面端顶栏里的「点开即输入」：原地变成输入框，结果从顶栏下方展开
 *
 * 由 AppShell 控制挂载，打开即聚焦输入框、Esc 关闭。
 */
const props = withDefaults(defineProps<{ variant?: "modal" | "inline" }>(), {
  variant: "modal",
});
const emit = defineEmits<{ close: [] }>();
const router = useRouter();

const inline = computed(() => props.variant === "inline");
const rootRef = ref<HTMLElement | null>(null);
const keyword = ref("");
const cursor = ref(0);
const inputRef = ref<HTMLInputElement | null>(null);
const pending = ref<PaletteAction | null>(null);

const destinations: NavDestination[] = [
  ...NAV_DESTINATIONS,
  SETTINGS_DESTINATION,
  ...SETTINGS_TABS.filter((tab) => tab.key !== SETTINGS_DEFAULT_KEY).map((tab) => ({
    key: `settings-${tab.key}`,
    labelKey: tab.labelKey,
    path: `/settings?tab=${tab.key}`,
    icon: tab.icon,
    keywords: [tab.key],
  })),
  ...GATEWAY_TABS.map((tab) => ({
    key: `gateway-${tab.key}`,
    labelKey: tab.labelKey,
    path: `/gateway?tab=${tab.key}`,
    icon: tab.icon,
    keywords: [tab.key],
  })),
];

/** 五个维护动作：前四个会写上游或落盘，健康中心只是跳转。 */
const actions = computed<PaletteAction[]>(() => [
  {
    key: "login-probe",
    label: t("palette.action.loginProbe"),
    hint: t("palette.action.loginProbeHint"),
    icon: ScanFace,
    run: () =>
      runTask({
        kind: "login-probe",
        title: t("palette.action.loginProbe"),
        run: () => api.probeLogin(),
        message: t("palette.action.loginProbeDone"),
      }),
  },
  {
    key: "credential-refresh",
    label: t("palette.action.refresh"),
    hint: t("palette.action.refreshHint"),
    icon: RefreshCw,
    destructive: true,
    run: () =>
      runTask({
        kind: "credential-refresh",
        title: t("palette.action.refresh"),
        run: () => api.refreshCredentials("force"),
        message: t("palette.action.refreshDone"),
      }),
  },
  {
    key: "backup",
    label: t("palette.action.backup"),
    hint: t("palette.action.backupHint"),
    icon: DatabaseBackup,
    run: () =>
      runTask({
        kind: "backup",
        title: t("palette.action.backup"),
        run: () => api.createBackup(scope.value),
        message: t("palette.action.backupDone"),
      }),
  },
  {
    key: "run-activities",
    label: t("palette.action.runActivities"),
    hint: t("palette.action.runActivitiesHint"),
    icon: Activity,
    destructive: true,
    run: () =>
      runTask({
        kind: "run-activities",
        title: t("palette.action.runActivities"),
        run: () => api.runActivities(),
        message: t("palette.action.runActivitiesDone"),
      }),
  },
  {
    key: "health",
    label: t("palette.action.health"),
    hint: t("palette.action.healthHint"),
    icon: HeartPulse,
    run: async () => {
      await router.push(scopedPath("/health"));
    },
  },
]);

const matches = computed<PaletteResult[]>(() =>
  matchPalette(keyword.value, destinations, accounts.value, actions.value),
);

/**
 * 没有任何匹配时，不要只留一句「没有匹配」：
 * 给三个不依赖输入的最常用去处，让用户下一步总有点得动的东西。
 */
const fallbackResults = computed<PaletteResult[]>(() => {
  if (matches.value.length) return [];
  const results: PaletteResult[] = [];
  const settings = destinations.find((item) => item.key === "settings");
  if (settings) {
    results.push({
      kind: "nav",
      key: `nav:${settings.key}`,
      label: t(settings.labelKey),
      path: settings.path,
      icon: settings.icon,
    });
  }
  for (const key of ["backup", "health"]) {
    const action = actions.value.find((item) => item.key === key);
    if (action) {
      results.push({
        kind: "action",
        key: `action:${action.key}`,
        label: action.label,
        hint: action.hint,
        icon: action.icon,
        action,
      });
    }
  }
  return results;
});

/* ---------- 最近使用 ----------
   空关键词时，把最近打开过的页面/动作顶到最前：面板第一格永远是「上次要去的地方」。
   只存 key 不存路径 —— 作用域会变，跳转地址必须每次现算。 */
const RECENT_KEY = "chaierww.palette.recent.v1";
const RECENT_LIMIT = 4;

function readRecentKeys(): string[] {
  try {
    const parsed = JSON.parse(localStorage.getItem(RECENT_KEY) ?? "[]");
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((item): item is string => typeof item === "string")
      .slice(0, RECENT_LIMIT);
  } catch {
    return [];
  }
}

const recentKeys = ref<string[]>(readRecentKeys());

function rememberRecent(key: string): void {
  const plain = key.replace(/^recent:/, "");
  if (!plain.startsWith("nav:") && !plain.startsWith("action:")) return;
  recentKeys.value = [plain, ...recentKeys.value.filter((item) => item !== plain)].slice(
    0,
    RECENT_LIMIT,
  );
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify(recentKeys.value));
  } catch {
    // 隐私模式下写不进就算了，它只是加速入口。
  }
}

const recentResults = computed<PaletteResult[]>(() => {
  if (keyword.value.trim()) return [];
  const results: PaletteResult[] = [];
  for (const key of recentKeys.value) {
    const nav = destinations.find((item) => `nav:${item.key}` === key);
    if (nav) {
      results.push({
        kind: "nav",
        key: `recent:${key}`,
        label: t(nav.labelKey),
        path: nav.path,
        icon: nav.icon,
        recent: true,
      });
      continue;
    }
    const action = actions.value.find((item) => `action:${item.key}` === key);
    if (action) {
      results.push({
        kind: "action",
        key: `recent:${key}`,
        label: action.label,
        hint: action.hint,
        icon: action.icon,
        action,
        recent: true,
      });
    }
  }
  return results;
});

/** 空关键词 = 最近使用 + 去重后的全部页面；有关键词时才交给匹配函数。 */
const orderedResults = computed<PaletteResult[]>(() => {
  if (keyword.value.trim()) return matches.value;
  const seen = new Set(recentKeys.value);
  return [...recentResults.value, ...matches.value.filter((item) => !seen.has(item.key))];
});

const showingFallback = computed(
  () => orderedResults.value.length === 0 && fallbackResults.value.length > 0,
);
/** 列表与光标都用这一份：没有匹配时显示兜底建议，键盘操作不会指到空数组。 */
const visibleResults = computed<PaletteResult[]>(() =>
  orderedResults.value.length ? orderedResults.value : fallbackResults.value,
);

/** 内联形态没有遮罩，点空白处要能收起；面板内的点击不受影响。 */
function onPointerDown(event: PointerEvent): void {
  if (!inline.value) return;
  const root = rootRef.value;
  if (!root || !(event.target instanceof Node) || root.contains(event.target)) return;
  emit("close");
}

onMounted(() => {
  inputRef.value?.focus();
  if (inline.value) document.addEventListener("pointerdown", onPointerDown);
});

onBeforeUnmount(() => {
  document.removeEventListener("pointerdown", onPointerDown);
});

/** 分组：最近使用单独成组，其余按类型分。 */
function groupOf(item: PaletteResult): string {
  return item.recent ? "recent" : item.kind;
}

/** 分组标题只在类型第一次出现时显示。 */
function showGroup(index: number): boolean {
  if (index === 0) return true;
  return groupOf(visibleResults.value[index]) !== groupOf(visibleResults.value[index - 1]);
}

function groupLabel(item: PaletteResult): string {
  if (item.recent) return t("palette.groupRecent");
  if (item.kind === "nav") return t("palette.groupPages");
  if (item.kind === "action") return t("palette.groupActions");
  return t("palette.groupAccounts");
}

async function execute(action: PaletteAction): Promise<void> {
  try {
    await action.run();
    toastSuccess(action.successMessage ?? t("palette.action.done", { label: action.label }));
  } catch (cause) {
    toastError((cause as Error)?.message || t("palette.action.failed", { label: action.label }));
  }
}

function go(index: number): void {
  const target = visibleResults.value[index];
  if (!target) return;
  if (target.kind === "nav") {
    rememberRecent(target.key);
    emit("close");
    void router.push(scopedPath(target.path));
    return;
  }
  if (target.kind === "account") {
    emit("close");
    void openSwitch(target.account, "command");
    return;
  }
  rememberRecent(target.key);
  // 破坏性动作先收起面板再弹确认，避免两个模态叠在一起。
  if (target.action.destructive) {
    pending.value = target.action;
    return;
  }
  emit("close");
  void execute(target.action);
}

function confirmAction(): void {
  const action = pending.value;
  pending.value = null;
  emit("close");
  if (action) void execute(action);
}

function move(step: number): void {
  const count = visibleResults.value.length;
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

/**
 * 命中高亮：只给字面命中的片段加粗。
 * 拼音 / 英文别名命中时标题里没有对应文字，不硬凑位置。
 */
function segments(label: string): { text: string; hit: boolean }[] {
  const query = keyword.value.trim().toLowerCase();
  if (!query) return [{ text: label, hit: false }];
  const index = label.toLowerCase().indexOf(query);
  if (index < 0) return [{ text: label, hit: false }];
  return [
    { text: label.slice(0, index), hit: false },
    { text: label.slice(index, index + query.length), hit: true },
    { text: label.slice(index + query.length), hit: false },
  ];
}
</script>

<template>
  <div
    ref="rootRef"
    :class="
      inline
        ? 'palette-inline absolute inset-x-0 top-0'
        : 'fixed inset-0 z-[var(--z-modal)] flex items-start justify-center bg-black/25 px-4 pt-[16vh]'
    "
    @click.self="inline ? undefined : emit('close')"
  >
    <div
      :class="
        inline
          ? 'overflow-hidden rounded-[7px] border border-line bg-surface shadow-float'
          : 'w-full max-w-[560px] overflow-hidden rounded-card border border-line bg-surface shadow-float'
      "
    >
      <div
        :class="
          inline
            ? 'flex h-[26px] items-center gap-2 px-2.5'
            : 'flex items-center gap-2.5 border-b border-line-soft px-4'
        "
      >
        <Search :size="inline ? 13 : 15" :stroke-width="1.9" class="shrink-0 text-muted" />
        <input
          ref="inputRef"
          v-model="keyword"
          type="text"
          :placeholder="t('palette.placeholder')"
          :class="
            inline
              ? 'palette-input h-full min-w-0 flex-1 bg-transparent text-caption text-ink outline-none placeholder:text-muted'
              : 'palette-input h-11 flex-1 bg-transparent text-body text-ink outline-none placeholder:text-muted'
          "
          @input="onInput"
          @keydown="onKeydown"
        />
        <kbd class="shrink-0 rounded-badge border border-line px-1.5 py-0.5 text-micro text-muted">
          Esc
        </kbd>
      </div>

      <p
        v-if="!visibleResults.length"
        :class="
          inline
            ? 'px-3 py-4 text-center text-body-sm text-muted'
            : 'px-4 py-6 text-center text-body-sm text-muted'
        "
      >
        {{ keyword.trim() ? t("palette.empty") : t("palette.searchAccounts") }}
      </p>

      <template v-else>
        <p
          v-if="showingFallback"
          :class="inline ? 'px-3 pb-1 pt-2 text-micro text-muted' : 'px-4 pb-1 pt-3 text-micro text-muted'"
        >
          {{ t("palette.tryThese") }}
        </p>

        <ul
          :class="[
            'max-h-[360px] overflow-y-auto p-1.5',
            inline && !showingFallback ? 'border-t border-line-soft' : '',
          ]"
        >
          <template v-for="(item, index) in visibleResults" :key="item.key">
          <li
            v-if="showGroup(index)"
            class="px-2.5 pb-1 pt-2 text-micro font-medium text-muted"
          >
            {{ groupLabel(item) }}
          </li>
          <li>
            <button
              type="button"
              class="flex w-full items-center gap-2.5 rounded-control px-2.5 py-2 text-left text-body transition-colors"
              :class="
                index === cursor
                  ? 'bg-brand-soft text-ink'
                  : 'text-ink-soft hover:bg-line-soft hover:text-ink'
              "
              @mousemove="cursor = index"
              @click="go(index)"
            >
              <component
                :is="item.kind === 'account' ? Users : item.icon"
                :size="16"
                :stroke-width="1.9"
                class="shrink-0"
                :class="index === cursor ? 'text-brand' : ''"
              />
              <span class="flex min-w-0 flex-1 flex-col">
                <span class="truncate">
                  <template v-for="(segment, segmentIndex) in segments(item.label)" :key="segmentIndex">
                    <strong v-if="segment.hit" class="font-semibold text-brand-dark">{{ segment.text }}</strong>
                    <template v-else>{{ segment.text }}</template>
                  </template>
                </span>
                <!-- 动作补一行说明：只写「续期凭据」看不出它会动什么，副标题说清楚。 -->
                <span v-if="item.kind === 'action'" class="truncate text-micro text-muted">
                  {{ item.hint }}
                </span>
              </span>
              <span
                v-if="item.kind === 'action'"
                class="shrink-0 rounded-full bg-line-soft px-1.5 py-[1px] text-micro text-muted"
              >
                {{ item.action.destructive ? t("palette.executeConfirm") : t("palette.execute") }}
              </span>
              <span v-else-if="item.kind === 'account'" class="shrink-0 text-micro text-muted">
                {{ item.hint }}
              </span>
              <CornerDownLeft v-if="index === cursor" :size="13" :stroke-width="1.9" />
            </button>
          </li>
          </template>
        </ul>
      </template>

      <!-- 键盘指引：只在弹层形态出现，内联形态的顶栏已经够窄 -->
      <div
        v-if="!inline"
        class="flex items-center gap-3 border-t border-line-soft px-4 py-2 text-micro text-muted"
      >
        <span class="flex items-center gap-1">
          <kbd class="rounded-badge border border-line px-1 py-0.5 text-[10px] leading-none">↑</kbd>
          <kbd class="rounded-badge border border-line px-1 py-0.5 text-[10px] leading-none">↓</kbd>
          {{ t("palette.kbSelect") }}
        </span>
        <span class="flex items-center gap-1">
          <kbd class="rounded-badge border border-line px-1 py-0.5 text-[10px] leading-none">↵</kbd>
          {{ t("palette.kbOpen") }}
        </span>
        <span class="ml-auto flex items-center gap-1">
          <kbd class="rounded-badge border border-line px-1 py-0.5 text-[10px] leading-none">Esc</kbd>
          {{ t("palette.kbClose") }}
        </span>
      </div>
    </div>

    <ConfirmDialog
      :open="pending !== null"
      :title="pending?.label ?? ''"
      :message="t('palette.confirmBody', { label: pending?.label ?? '' })"
      :confirm-text="t('palette.confirmExecute')"
      :tone="'danger'"
      @confirm="confirmAction"
      @cancel="pending = null"
    />
  </div>
</template>

<style scoped>
/*
 * 浏览器默认焦点环会在面板里再套一圈亮绿描边，和面板自身的边框叠成双层。
 * 输入框已经在弹层里、光标可见，这里去掉多余的一圈，只留面板边框。
 */
.palette-input:focus,
.palette-input:focus-visible {
  outline: none !important;
  box-shadow: none !important;
}
</style>
