<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { AlertTriangle, Layers, Play, RefreshCw, Upload } from "lucide-vue-next";
import SectionCard from "../../components/SectionCard.vue";
import StatusPill from "../../components/StatusPill.vue";
import ToggleSwitch from "../../components/ToggleSwitch.vue";
import {
  api,
  type Preferences,
  type SyncAccountStatus,
  type SyncItem,
  type SyncReport,
} from "../../api";
import { t } from "../../lib/i18n";

/**
 * 设置 → 会话同步：把「同一台机器上、不同账号之间的会话与网页版登录态保持一致」的
 * 入口摊开。默认只读取采集；应用（写客户端）必须客户端已退出，由后端判定并返回
 * `blocked`，前端不自己猜。
 *
 * 两边口径刻意分开：
 * - 采集 = 只读，客户端运行中也安全；
 * - 预演 = 只算不写；
 * - 应用 = 真写，会先备份 workbuddy.db。
 */
const SCOPES = ["workbuddy-cn", "trae-cn"];

const prefs = ref<Preferences | null>(null);
const statuses = ref<Record<string, SyncAccountStatus>>({});
const supported = ref<Record<string, { conversations: boolean; webview: boolean }>>({});
const blobBytes = ref(0);
const loading = ref(false);
const busy = ref("");
const message = ref("");
const messageError = ref(false);
const conflictsOpen = ref<Record<string, SyncItem[]>>({});

const accounts = computed(() =>
  Object.values(statuses.value).sort((a, b) => a.displayName.localeCompare(b.displayName)),
);

const conflicts = computed(() =>
  Object.values(statuses.value).filter((row) => row.conflicts > 0),
);

const conversationsSupported = computed(() =>
  Object.values(supported.value).some((item) => item.conversations),
);

async function loadStatus(): Promise<void> {
  loading.value = true;
  try {
    const next: Record<string, SyncAccountStatus> = {};
    const caps: Record<string, { conversations: boolean; webview: boolean }> = {};
    let bytes = 0;
    for (const scope of SCOPES) {
      try {
        const data = await api.syncStatus(scope);
        caps[scope] = data.supported;
        bytes = Math.max(bytes, data.blobBytes);
        for (const row of data.accounts) next[row.accountId] = row;
      } catch {
        // 某个平台取不到就跳过，不冒充「已同步」。
      }
    }
    statuses.value = next;
    supported.value = caps;
    blobBytes.value = bytes;
  } finally {
    loading.value = false;
  }
}

onMounted(async () => {
  try {
    prefs.value = await api.preferences();
  } catch {
    prefs.value = null;
  }
  void loadStatus();
});

async function toggleEnabled(): Promise<void> {
  if (!prefs.value) return;
  busy.value = "prefs";
  try {
    prefs.value = await api.savePreferences({
      sessionSyncEnabled: !prefs.value.sessionSyncEnabled,
    });
  } finally {
    busy.value = "";
  }
}

/** 后端状态是开放字符串，这里显式收敛成文案键，避免拼串穿透 i18n 的类型约束。 */
function stateLabel(state: string): string {
  switch (state) {
    case "capturing":
      return t("sync.state.capturing");
    case "applied":
      return t("sync.state.applied");
    case "blocked":
      return t("sync.state.blocked");
    case "failed":
      return t("sync.state.failed");
    default:
      return t("sync.state.idle");
  }
}

/** 统一展示一份报告：有错报错，否则用最后一步的说明（后端已写成中文）。 */
function describe(report: SyncReport): string {
  if (report.state === "unavailable") return t("sync.unavailable");
  if (report.error) return report.error;
  const last = report.steps[report.steps.length - 1];
  return last?.detail || t("sync.done");
}

async function run(account: SyncAccountStatus, action: "capture" | "plan" | "apply"): Promise<void> {
  busy.value = `${account.accountId}:${action}`;
  message.value = "";
  messageError.value = false;
  try {
    const report =
      action === "capture"
        ? await api.syncCapture(account.accountId)
        : await api.syncApply(account.accountId, action === "plan"); // 只有「预演」是 dryRun
    message.value = describe(report);
    messageError.value = !report.succeeded && report.state !== "planned";
    await loadStatus();
  } catch (error) {
    message.value = (error as Error).message;
    messageError.value = true;
  } finally {
    busy.value = "";
  }
}

async function loadItems(account: SyncAccountStatus): Promise<SyncItem[]> {
  try {
    const data = await api.syncItems(account.accountId);
    return data.items;
  } catch (error) {
    message.value = (error as Error).message;
    messageError.value = true;
    return [];
  }
}

async function openConflicts(account: SyncAccountStatus): Promise<void> {
  const list = await loadItems(account);
  conflictsOpen.value = {
    ...conflictsOpen.value,
    [account.accountId]: list.filter((item) => item.conflict),
  };
}

async function resolve(
  account: SyncAccountStatus,
  item: SyncItem,
  action: "keepSource" | "keepMirror" | "keepBoth",
): Promise<void> {
  busy.value = `${account.accountId}:${item.stableKey}`;
  try {
    const report = await api.syncResolve(account.accountId, item.stableKey, action);
    message.value = describe(report);
    messageError.value = !report.succeeded;
    await openConflicts(account);
    await loadStatus();
  } catch (error) {
    message.value = (error as Error).message;
    messageError.value = true;
  } finally {
    busy.value = "";
  }
}

function formatBytes(bytes: number): string {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(value >= 10 || index === 0 ? 0 : 1)} ${units[index]}`;
}
</script>

<template>
  <div class="space-y-4">
    <SectionCard :title="t('sync.title')" :subtitle="t('sync.subtitle')">
      <div class="flex items-start justify-between gap-4">
        <div class="min-w-0">
          <p class="text-body-sm font-medium text-ink">{{ t("sync.master.label") }}</p>
          <p class="mt-0.5 text-caption leading-relaxed text-muted">
            {{ t("sync.master.desc") }}
          </p>
        </div>
        <ToggleSwitch
          :model-value="prefs?.sessionSyncEnabled ?? false"
          :disabled="!prefs || busy === 'prefs'"
          @update:model-value="toggleEnabled"
        />
      </div>

      <div class="mt-4 grid gap-2 border-t border-line-soft pt-3 text-caption text-muted sm:grid-cols-3">
        <p>{{ t("sync.cap.conversations") }}</p>
        <p>{{ t("sync.cap.webview") }}</p>
        <p>{{ t("sync.cap.boundary") }}</p>
      </div>

      <p v-if="!conversationsSupported" class="mt-3 text-caption text-warn">
        {{ t("sync.unavailable") }}
      </p>
      <p v-if="blobBytes" class="mt-2 text-caption text-muted">
        {{ t("sync.storage", { size: formatBytes(blobBytes) }) }}
      </p>
    </SectionCard>

    <SectionCard :title="t('sync.accounts.title')" :subtitle="t('sync.accounts.subtitle')">
      <p
        v-if="message"
        class="mb-3 rounded-control px-3 py-2 text-caption leading-relaxed"
        :class="messageError ? 'bg-warn-soft text-warn' : 'bg-brand-soft text-brand-dark'"
      >
        {{ message }}
      </p>

      <p v-if="loading && !accounts.length" class="text-caption text-muted">
        {{ t("sync.loading") }}
      </p>
      <p v-else-if="!accounts.length" class="text-caption text-muted">
        {{ t("sync.accounts.empty") }}
      </p>

      <ul v-else class="divide-y divide-line-soft">
        <li v-for="account in accounts" :key="account.accountId" class="py-2.5">
          <div class="flex flex-wrap items-center gap-2">
            <Layers :size="13" :stroke-width="1.9" class="shrink-0 text-muted" />
            <span class="min-w-0 truncate text-body-sm font-medium text-ink">
              {{ account.displayName }}
            </span>
            <StatusPill :tone="account.conflicts ? 'warn' : account.pending ? 'info' : 'brand'">
              {{ stateLabel(account.state) }}
            </StatusPill>
            <span class="text-caption text-muted">
              {{ t("sync.row.counts", { items: account.items, pending: account.pending }) }}
            </span>

            <div class="ml-auto flex shrink-0 items-center gap-1">
              <button
                type="button"
                class="inline-flex items-center gap-1 rounded-control border border-line px-2 py-[3px] text-caption text-ink-soft transition-colors hover:border-line-strong disabled:opacity-50"
                :disabled="Boolean(busy)"
                @click="run(account, 'capture')"
              >
                <RefreshCw :size="11" :stroke-width="2" />
                {{ t("sync.action.capture") }}
              </button>
              <button
                type="button"
                class="inline-flex items-center gap-1 rounded-control border border-line px-2 py-[3px] text-caption text-ink-soft transition-colors hover:border-line-strong disabled:opacity-50"
                :disabled="Boolean(busy)"
                @click="run(account, 'plan')"
              >
                <Play :size="11" :stroke-width="2" />
                {{ t("sync.action.plan") }}
              </button>
              <button
                type="button"
                class="inline-flex items-center gap-1 rounded-control border border-brand/30 bg-brand-soft/50 px-2 py-[3px] text-caption text-brand-dark transition-colors hover:bg-brand-soft disabled:opacity-50"
                :disabled="Boolean(busy) || account.pending === 0"
                :title="account.pending === 0 ? t('sync.action.nothingToApply') : t('sync.action.applyHint')"
                @click="run(account, 'apply')"
              >
                <Upload :size="11" :stroke-width="2" />
                {{ t("sync.action.apply") }}
              </button>
            </div>
          </div>

          <p v-if="account.lastError" class="mt-1 text-caption leading-relaxed text-warn">
            {{ account.lastError }}
          </p>
          <p v-if="account.conflicts" class="mt-1 flex items-center gap-1 text-caption text-warn">
            <AlertTriangle :size="11" :stroke-width="2" />
            {{ t("sync.row.conflicts", { count: account.conflicts }) }}
            <button
              type="button"
              class="underline underline-offset-2 hover:text-ink"
              @click="openConflicts(account)"
            >
              {{ t("sync.row.review") }}
            </button>
          </p>

          <ul
            v-if="conflictsOpen[account.accountId]?.length"
            class="mt-2 space-y-1.5 rounded-control bg-line-soft/40 px-2 py-1.5"
          >
            <li
              v-for="item in conflictsOpen[account.accountId]"
              :key="item.stableKey"
              class="flex flex-wrap items-center gap-1.5"
            >
              <span class="min-w-0 flex-1 truncate text-caption text-ink-soft" :title="item.title">
                {{ item.title }}
              </span>
              <button
                type="button"
                class="rounded-control border border-line px-1.5 py-[1px] text-micro text-ink-soft hover:border-line-strong"
                :disabled="Boolean(busy)"
                @click="resolve(account, item, 'keepSource')"
              >
                {{ t("sync.resolve.keepSource") }}
              </button>
              <button
                type="button"
                class="rounded-control border border-line px-1.5 py-[1px] text-micro text-ink-soft hover:border-line-strong"
                :disabled="Boolean(busy)"
                @click="resolve(account, item, 'keepMirror')"
              >
                {{ t("sync.resolve.keepMirror") }}
              </button>
              <button
                type="button"
                class="rounded-control border border-line px-1.5 py-[1px] text-micro text-ink-soft hover:border-line-strong"
                :disabled="Boolean(busy)"
                @click="resolve(account, item, 'keepBoth')"
              >
                {{ t("sync.resolve.keepBoth") }}
              </button>
            </li>
          </ul>
        </li>
      </ul>
    </SectionCard>

    <SectionCard :title="t('sync.conflicts.title')" :subtitle="t('sync.conflicts.subtitle')">
      <p v-if="!conflicts.length" class="text-caption text-muted">
        {{ t("sync.conflicts.empty") }}
      </p>
      <p v-else class="text-caption leading-relaxed text-warn">
        {{ t("sync.conflicts.hint", { count: conflicts.length }) }}
      </p>
    </SectionCard>
  </div>
</template>
