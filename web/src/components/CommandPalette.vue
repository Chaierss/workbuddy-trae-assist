<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
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
 * 由 AppShell 控制挂载，打开即聚焦输入框、Esc 关闭。
 */
const emit = defineEmits<{ close: [] }>();
const router = useRouter();

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
  })),
  ...GATEWAY_TABS.map((tab) => ({
    key: `gateway-${tab.key}`,
    labelKey: tab.labelKey,
    path: `/gateway?tab=${tab.key}`,
    icon: tab.icon,
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

onMounted(() => inputRef.value?.focus());

/** 分组标题只在类型第一次出现时显示。 */
function showGroup(index: number): boolean {
  if (index === 0) return true;
  return matches.value[index].kind !== matches.value[index - 1].kind;
}

function groupLabel(item: PaletteResult): string {
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
  const target = matches.value[index];
  if (!target) return;
  if (target.kind === "nav") {
    emit("close");
    void router.push(scopedPath(target.path));
    return;
  }
  if (target.kind === "account") {
    emit("close");
    void openSwitch(target.account, "command");
    return;
  }
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
      class="w-full max-w-[480px] overflow-hidden rounded-card border border-line bg-surface shadow-float"
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

      <ul v-else class="max-h-[360px] overflow-y-auto p-1.5">
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
                :is="item.kind === 'account' ? Users : item.icon"
                :size="16"
                :stroke-width="1.9"
                class="shrink-0"
              />
              <span class="flex-1 truncate">{{ item.label }}</span>
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
