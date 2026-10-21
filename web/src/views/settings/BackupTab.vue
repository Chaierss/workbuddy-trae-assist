<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import {
  AlertTriangle,
  CheckCircle2,
  FileLock2,
  History,
  RotateCcw,
  ShieldAlert,
} from "lucide-vue-next";
import ConfirmDialog from "../../components/ConfirmDialog.vue";
import SectionCard from "../../components/SectionCard.vue";
import StatusPill from "../../components/StatusPill.vue";
import {
  api,
  formatBytes,
  formatTime,
  type ClientSettingsSnapshot,
  type GlobalBackupPart,
  type Preferences,
  type Snapshot,
} from "../../api";
import { t, type MessageKey } from "../../lib/i18n";
import { refreshAll, scope, snapshots } from "../../stores/app";

const busy = ref(false);
const message = ref<string | null>(null);
const failed = ref(false);

// ---------- 自动备份（默认开启，每天一份；所有快照一并滚动只留最近 7 份） ----------

const prefs = ref<Preferences | null>(null);
const prefsBusy = ref(false);

async function loadPrefs(): Promise<void> {
  try {
    prefs.value = await api.preferences();
  } catch (error) {
    prefs.value = null;
    message.value = (error as Error).message;
    failed.value = true;
  }
}

async function toggleAuto(): Promise<void> {
  if (!prefs.value) return;
  prefsBusy.value = true;
  try {
    prefs.value = await api.savePreferences({ autoBackup: !prefs.value.autoBackup });
  } catch (error) {
    message.value = (error as Error).message;
    failed.value = true;
  } finally {
    prefsBusy.value = false;
  }
}

// ---------- 客户端设置备份点（原「客户端设置」页的那一份，合并到这里） ----------

const clientSettings = ref<ClientSettingsSnapshot | null>(null);
const clientMessage = ref<string | null>(null);
const clientFailed = ref(false);

async function loadClientSettings(): Promise<void> {
  try {
    clientSettings.value = await api.clientSettings();
  } catch (error) {
    clientFailed.value = true;
    clientMessage.value = (error as Error).message;
  }
}

async function restoreClient(backupId: string): Promise<void> {
  busy.value = true;
  clientMessage.value = null;
  clientFailed.value = false;
  try {
    clientSettings.value = await api.restoreClientSettings(backupId);
    clientMessage.value = t("clientSettings.restored", { backup: backupId });
  } catch (error) {
    clientFailed.value = true;
    clientMessage.value = (error as Error).message;
  } finally {
    busy.value = false;
  }
}

// ---------- 全局备份（本工具自己的数据，按分区勾选） ----------

const parts = ref<GlobalBackupPart[]>([]);
const globalSnapshots = ref<Snapshot[]>([]);
const selectedParts = ref<string[]>([]);
/** 勾了含凭据的分区时先停在确认态：再点一次才真的写。 */
const confirming = ref(false);
const globalMessage = ref<string | null>(null);
const globalFailed = ref(false);

/** 本次勾选里含凭据的分区，用于确认文案与按钮态。 */
const riskySelected = computed(() =>
  parts.value.filter((part) => part.credential && selectedParts.value.includes(part.key)),
);

/** 快照来源徽标：manual / auto / pre-restore（其余历史值一律按切换前展示）。 */
function reasonLabel(reason: string): MessageKey {
  if (reason === "manual") return "settings.backup.reasonManual";
  if (reason === "auto") return "settings.backup.reasonAuto";
  if (reason === "pre-restore") return "settings.backup.reasonPreRestore";
  return "settings.backup.reasonPreswitch";
}

function reasonTone(reason: string): "info" | "brand" | "neutral" {
  if (reason === "manual") return "info";
  if (reason === "auto") return "brand";
  return "neutral";
}

/** 恢复确认：点「恢复」先弹确认框，确认后才会覆盖客户端目录。 */
const restoreTarget = ref<Snapshot | null>(null);

function askRestore(snapshot: Snapshot): void {
  restoreTarget.value = snapshot;
}

async function doRestore(): Promise<void> {
  const snapshot = restoreTarget.value;
  if (!snapshot) return;
  busy.value = true;
  message.value = null;
  failed.value = false;
  try {
    const result = await api.restoreBackup(snapshot.id);
    message.value = result.safetySnapshotId
      ? t("settings.backup.restoreDone", { safety: result.safetySnapshotId })
      : t("settings.backup.restoreDoneNoSafety");
    restoreTarget.value = null;
    await refreshAll();
  } catch (error) {
    failed.value = true;
    message.value = (error as Error).message;
    // 失败也收起确认框，否则错误信息被挡在弹窗后面看不见。
    restoreTarget.value = null;
  } finally {
    busy.value = false;
  }
}

/** 恢复规则条目：存 key 不存译文，切换语言时随 `t()` 更新。 */
const RESTORE_RULES: MessageKey[] = [
  "settings.backup.rule1",
  "settings.backup.rule2",
  "settings.backup.rule3",
  "settings.backup.rule4",
  "settings.backup.rule5",
];

async function loadGlobal(): Promise<void> {
  try {
    const data = await api.globalBackups();
    parts.value = data.parts;
    globalSnapshots.value = data.snapshots;
    // 首次进来按后端给的默认分区勾选（不含凭据），之后不再覆盖用户的选择。
    if (!selectedParts.value.length) selectedParts.value = [...data.defaultParts];
  } catch (error) {
    globalFailed.value = true;
    globalMessage.value = (error as Error).message;
  }
}

function togglePart(key: string): void {
  confirming.value = false;
  selectedParts.value = selectedParts.value.includes(key)
    ? selectedParts.value.filter((item) => item !== key)
    : [...selectedParts.value, key];
}

async function createBackup(): Promise<void> {
  busy.value = true;
  message.value = null;
  failed.value = false;
  try {
    const snapshot = await api.createBackup(scope.value, t("settings.backup.noteManual"));
    message.value = t("settings.backup.backupDone", {
      count: snapshot.fileCount,
      size: formatBytes(snapshot.totalBytes),
    });
    await refreshAll();
  } catch (error) {
    failed.value = true;
    message.value = (error as Error).message;
  } finally {
    busy.value = false;
  }
}

async function verify(snapshot: Snapshot): Promise<void> {
  busy.value = true;
  message.value = null;
  failed.value = false;
  try {
    const result = await api.verifyBackup(snapshot.id);
    failed.value = !result.verified;
    message.value = result.verified ? t("settings.backup.verifyOk") : t("settings.backup.verifyBad");
    await refreshAll();
  } catch (error) {
    failed.value = true;
    message.value = (error as Error).message;
  } finally {
    busy.value = false;
  }
}

async function createGlobal(): Promise<void> {
  // 第一次点击只做确认：含凭据的分区不允许「顺手就备份」。
  if (riskySelected.value.length && !confirming.value) {
    confirming.value = true;
    return;
  }
  busy.value = true;
  globalMessage.value = null;
  globalFailed.value = false;
  try {
    const snapshot = await api.createGlobalBackup(
      selectedParts.value,
      t("settings.backup.noteManual"),
      confirming.value,
    );
    globalMessage.value = t("settings.backup.backupDone", {
      count: snapshot.fileCount,
      size: formatBytes(snapshot.totalBytes),
    });
    confirming.value = false;
    await loadGlobal();
  } catch (error) {
    globalFailed.value = true;
    globalMessage.value = (error as Error).message;
  } finally {
    busy.value = false;
  }
}

async function verifyGlobal(snapshot: Snapshot): Promise<void> {
  busy.value = true;
  globalMessage.value = null;
  globalFailed.value = false;
  try {
    const result = await api.verifyBackup(snapshot.id);
    globalFailed.value = !result.verified;
    globalMessage.value = result.verified
      ? t("settings.backup.verifyOk")
      : t("settings.backup.verifyBad");
    await loadGlobal();
  } catch (error) {
    globalFailed.value = true;
    globalMessage.value = (error as Error).message;
  } finally {
    busy.value = false;
  }
}

onMounted(() => {
  void loadPrefs();
  void loadClientSettings();
  void loadGlobal();
});
</script>

<template>
  <div class="space-y-4">
    <SectionCard
      :title="t('settings.backup.autoTitle')"
      :subtitle="t('settings.backup.autoSubtitle')"
    >
      <div class="flex items-start gap-3">
        <div class="min-w-0 flex-1">
          <p class="text-[13px] font-medium text-ink">{{ t("settings.backup.autoLabel") }}</p>
          <p class="mt-0.5 text-[11.5px] leading-relaxed text-muted">
            {{ t("settings.backup.autoHint") }}
          </p>
        </div>
        <button
          type="button"
          class="mt-0.5 h-6 w-10 shrink-0 rounded-full transition-colors disabled:opacity-60"
          :class="prefs?.autoBackup ? 'bg-brand' : 'bg-line'"
          :disabled="!prefs || prefsBusy"
          :aria-pressed="!!prefs?.autoBackup"
          :title="t('settings.backup.autoLabel')"
          @click="toggleAuto"
        >
          <span
            class="block size-4.5 rounded-full bg-white transition-transform"
            :class="prefs?.autoBackup ? 'translate-x-[22px]' : 'translate-x-[3px]'"
          ></span>
        </button>
      </div>
    </SectionCard>

    <SectionCard
      :title="t('settings.backup.title')"
      :subtitle="t('settings.backup.subtitle', { count: snapshots.length })"
      flush
    >
      <template #actions>
        <button
          type="button"
          class="rounded-[8px] border border-line px-2.5 py-1.5 text-[12px] text-ink-soft transition-colors hover:border-[#d5d8d3] hover:text-ink disabled:opacity-50"
          :disabled="busy"
          @click="createBackup"
        >
          {{ t("settings.backup.backupNow") }}
        </button>
      </template>

      <div v-if="message" class="border-b border-line-soft px-5 py-2.5">
        <p
          class="flex items-center gap-1.5 text-[12px]"
          :class="failed ? 'text-danger' : 'text-brand-dark'"
        >
          <AlertTriangle v-if="failed" :size="13" />
          <CheckCircle2 v-else :size="13" />
          {{ message }}
        </p>
      </div>

      <p v-if="!snapshots.length" class="px-5 py-8 text-center text-[13px] text-muted">
        {{ t("settings.backup.empty") }}
      </p>

      <ul v-else class="divide-y divide-line-soft">
        <li
          v-for="snapshot in snapshots"
          :key="snapshot.id"
          class="flex flex-wrap items-center gap-3 px-5 py-2.5"
        >
          <StatusPill :tone="reasonTone(snapshot.reason)">
            {{ t(reasonLabel(snapshot.reason)) }}
          </StatusPill>
          <span class="text-[12px] text-ink-soft">{{ formatTime(snapshot.createdAt) }}</span>
          <span class="text-[12px] text-muted">
            {{ t("settings.backup.fileCount", { count: snapshot.fileCount }) }} ·
            {{ formatBytes(snapshot.totalBytes) }}
          </span>
          <StatusPill :tone="snapshot.verified ? 'brand' : 'warn'">
            {{ snapshot.verified ? t("settings.backup.verified") : t("settings.backup.unverified") }}
          </StatusPill>
          <div class="ml-auto flex items-center gap-2">
            <button
              type="button"
              class="inline-flex items-center gap-1 rounded-[8px] border border-line px-2.5 py-1 text-[11.5px] text-ink-soft transition-colors hover:border-[#d5d8d3] hover:text-ink disabled:opacity-50"
              :disabled="busy"
              @click="askRestore(snapshot)"
            >
              <RotateCcw :size="11" :stroke-width="2" />{{ t("settings.backup.restore") }}
            </button>
            <button
              type="button"
              class="rounded-[8px] border border-line px-2.5 py-1 text-[11.5px] text-ink-soft transition-colors hover:border-[#d5d8d3] hover:text-ink disabled:opacity-50"
              :disabled="busy"
              @click="verify(snapshot)"
            >
              {{ t("settings.backup.verify") }}
            </button>
          </div>
        </li>
      </ul>
    </SectionCard>

    <SectionCard
      :title="t('settings.backup.globalTitle')"
      :subtitle="t('settings.backup.globalSubtitle', { count: globalSnapshots.length })"
      flush
    >
      <template #actions>
        <button
          type="button"
          class="rounded-[8px] border px-2.5 py-1.5 text-[12px] transition-colors disabled:opacity-50"
          :class="
            confirming
              ? 'border-danger text-danger'
              : 'border-line text-ink-soft hover:border-[#d5d8d3] hover:text-ink'
          "
          :disabled="busy || selectedParts.length === 0"
          @click="createGlobal"
        >
          {{ confirming ? t("settings.backup.backupNowRisky") : t("settings.backup.backupNow") }}
        </button>
      </template>

      <div class="border-b border-line-soft px-5 py-2">
        <label
          v-for="part in parts"
          :key="part.key"
          class="flex cursor-pointer items-start gap-2.5 py-1.5"
        >
          <input
            type="checkbox"
            class="mt-0.5 size-[15px] accent-[var(--color-brand)]"
            :checked="selectedParts.includes(part.key)"
            @change="togglePart(part.key)"
          />
          <span class="min-w-0 flex-1">
            <span class="flex items-center gap-2 text-[13px] font-medium text-ink">
              {{ part.label }}
              <StatusPill v-if="part.credential" tone="warn">{{ t("settings.backup.credential") }}</StatusPill>
            </span>
            <span class="mt-0.5 block truncate text-[11.5px] text-muted">
              {{ part.files.join(" · ") }}
            </span>
          </span>
        </label>

        <p
          v-if="confirming"
          class="mt-1.5 flex items-start gap-2 rounded-[9px] bg-warn-soft px-3 py-2 text-[12px] leading-relaxed text-warn"
        >
          <ShieldAlert :size="14" :stroke-width="1.9" class="mt-0.5 shrink-0" />
          <span>
            {{
              t("settings.backup.riskyWarn", {
                names: riskySelected.map((part) => part.label).join("、"),
              })
            }}
          </span>
        </p>
      </div>

      <div v-if="globalMessage" class="border-b border-line-soft px-5 py-2.5">
        <p
          class="flex items-center gap-1.5 text-[12px]"
          :class="globalFailed ? 'text-danger' : 'text-brand-dark'"
        >
          <AlertTriangle v-if="globalFailed" :size="13" />
          <CheckCircle2 v-else :size="13" />
          {{ globalMessage }}
        </p>
      </div>

      <p v-if="!globalSnapshots.length" class="px-5 py-8 text-center text-[13px] text-muted">
        {{ t("settings.backup.globalEmpty") }}
      </p>

      <ul v-else class="divide-y divide-line-soft">
        <li
          v-for="snapshot in globalSnapshots"
          :key="snapshot.id"
          class="flex flex-wrap items-center gap-3 px-5 py-2.5"
        >
          <FileLock2 :size="14" :stroke-width="1.9" class="shrink-0 text-muted" />
          <span class="text-[12px] text-ink-soft">{{ formatTime(snapshot.createdAt) }}</span>
          <span class="text-[12px] text-muted">
            {{ t("settings.backup.fileCount", { count: snapshot.fileCount }) }} ·
            {{ formatBytes(snapshot.totalBytes) }}
          </span>
          <StatusPill :tone="snapshot.verified ? 'brand' : 'warn'">
            {{ snapshot.verified ? t("settings.backup.verified") : t("settings.backup.unverified") }}
          </StatusPill>
          <button
            type="button"
            class="ml-auto rounded-[8px] border border-line px-2.5 py-1 text-[11.5px] text-ink-soft transition-colors hover:border-[#d5d8d3] hover:text-ink disabled:opacity-50"
            :disabled="busy"
            @click="verifyGlobal(snapshot)"
          >
            {{ t("settings.backup.verify") }}
          </button>
        </li>
      </ul>
    </SectionCard>

    <SectionCard
      :title="t('settings.backup.clientBackupsTitle')"
      :subtitle="t('settings.backup.clientBackupsSubtitle')"
      flush
    >
      <div v-if="clientMessage" class="border-b border-line-soft px-5 py-2.5">
        <p
          class="flex items-center gap-1.5 text-[12px]"
          :class="clientFailed ? 'text-danger' : 'text-brand-dark'"
        >
          <AlertTriangle v-if="clientFailed" :size="13" />
          <CheckCircle2 v-else :size="13" />
          {{ clientMessage }}
        </p>
      </div>

      <p
        v-if="clientSettings && !clientSettings.available"
        class="px-5 py-8 text-center text-[13px] text-muted"
      >
        {{ clientSettings.message }}
      </p>
      <p
        v-else-if="clientSettings && !clientSettings.backups.length"
        class="px-5 py-8 text-center text-[13px] text-muted"
      >
        {{ t("clientSettings.noBackups") }}
      </p>
      <ul v-else-if="clientSettings" class="divide-y divide-line-soft">
        <li
          v-for="point in clientSettings.backups"
          :key="point.id"
          class="flex flex-wrap items-center gap-3 px-5 py-2.5"
        >
          <History :size="14" :stroke-width="1.9" class="shrink-0 text-muted" />
          <span class="font-mono text-[12px] text-ink-soft">{{ point.id }}</span>
          <span class="text-[12px] text-muted">
            {{ formatTime(point.createdAt) }} · {{ formatBytes(point.size) }}
          </span>
          <button
            type="button"
            class="ml-auto inline-flex items-center gap-1 rounded-[8px] border border-line px-2.5 py-1 text-[11.5px] text-ink-soft transition-colors hover:border-[#d5d8d3] hover:text-ink disabled:opacity-50"
            :disabled="busy"
            @click="restoreClient(point.id)"
          >
            <RotateCcw :size="11" :stroke-width="2" />{{ t("clientSettings.restore") }}
          </button>
        </li>
      </ul>
    </SectionCard>

    <SectionCard
      :title="t('settings.backup.rulesTitle')"
      :subtitle="t('settings.backup.rulesSubtitle')"
    >
      <ul class="space-y-2 text-[12.5px] leading-relaxed text-ink-soft">
        <li v-for="rule in RESTORE_RULES" :key="rule">{{ t(rule) }}</li>
      </ul>
    </SectionCard>

    <ConfirmDialog
      :open="restoreTarget !== null"
      :title="t('settings.backup.restoreTitle')"
      :message="t('settings.backup.restoreConfirm')"
      :confirm-text="t('settings.backup.restore')"
      tone="danger"
      :busy="busy"
      @confirm="doRestore"
      @cancel="restoreTarget = null"
    />
  </div>
</template>
