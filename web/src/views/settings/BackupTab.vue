<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { AlertTriangle, CheckCircle2, FileLock2, RotateCcw, ShieldAlert } from "lucide-vue-next";
import ConfirmDialog from "../../components/ConfirmDialog.vue";
import SectionCard from "../../components/SectionCard.vue";
import StatusPill from "../../components/StatusPill.vue";
import ToggleSwitch from "../../components/ToggleSwitch.vue";
import {
  api,
  formatBytes,
  formatTime,
  type GlobalBackupPart,
  type Preferences,
  type RestorePending,
  type RestoreResult,
  type Snapshot,
} from "../../api";
import { desktop } from "../../lib/desktop";
import { t } from "../../lib/i18n";

// ---------- 全局备份（本工具自己的数据，按分区勾选） ----------
//
// 客户端数据快照（切换前 / 每日自动）与客户端设置备份点这两块已经从界面下线：
// 它们各自带一套「原地恢复」，和这里的「导出型备份」混在一页反而分不清哪个能回滚。
// 现在只留全局备份：按分区导出、可校验。恢复是「登记 → 重启时应用」，
// 不在运行中替换正在使用的数据库文件。

const busy = ref(false);

// ---------- 每日自动备份开关（`autoGlobalBackup`，默认开） ----------

const prefs = ref<Preferences | null>(null);
const prefsBusy = ref(false);

async function loadPrefs(): Promise<void> {
  try {
    prefs.value = await api.preferences();
  } catch (error) {
    // 开关拿不到就置灰，不拦着用户手动备份。
    prefs.value = null;
    globalFailed.value = true;
    globalMessage.value = (error as Error).message;
  }
}

async function toggleAuto(): Promise<void> {
  if (!prefs.value || prefsBusy.value) return;
  prefsBusy.value = true;
  try {
    prefs.value = await api.savePreferences({
      autoGlobalBackup: !prefs.value.autoGlobalBackup,
    });
  } catch (error) {
    globalFailed.value = true;
    globalMessage.value = (error as Error).message;
  } finally {
    prefsBusy.value = false;
  }
}

const parts = ref<GlobalBackupPart[]>([]);
const globalSnapshots = ref<Snapshot[]>([]);
const selectedParts = ref<string[]>([]);
/**
 * 待确认的含凭据分区（可能有多个）：勾它时不立刻生效，先在界面上停一拍。
 *
 * 语义是「勾选即确认」—— 确认一次就够了，之后手动「立即备份」不再弹二次确认，
 * 自动备份也把这个范围接过去。取消勾选不需要确认，直接生效。
 *
 * 关键是**勾的那一下必须看得见反馈**：待确认期间复选框走 indeterminate（横杠），
 * 否则用户勾完发现框没打勾、只在下面冒出一条字，只会以为界面坏了。
 */
const pendingParts = ref<string[]>([]);
const globalMessage = ref<string | null>(null);
const globalFailed = ref(false);

// ---------- 从全局备份恢复（登记 → 重启时应用） ----------
const restoreTarget = ref<Snapshot | null>(null);
const restorePending = ref<RestorePending | null>(null);
const restoreResult = ref<RestoreResult | null>(null);
const restoring = ref(false);

async function loadRestoreStatus(): Promise<void> {
  try {
    const status = await api.restoreStatus();
    restorePending.value = status.pending;
    restoreResult.value = status.result;
  } catch {
    // 恢复状态是辅助信息，读不到不影响备份列表。
  }
}

function askRestore(snapshot: Snapshot): void {
  restoreTarget.value = snapshot;
}

async function confirmRestore(): Promise<void> {
  const target = restoreTarget.value;
  if (!target) return;
  restoring.value = true;
  globalFailed.value = false;
  try {
    const staged = await api.stageGlobalRestore(target.id);
    restorePending.value = staged.pending;
    restoreTarget.value = null;
    globalMessage.value = t("settings.backup.restoreStaged", { id: target.id });
    if (desktop?.relaunch) {
      // 用户已在确认框里同意「立即重启并恢复」，这里直接交给主进程。
      await desktop.relaunch();
      return;
    }
    globalMessage.value = t("settings.backup.restoreRestartManual");
  } catch (error) {
    globalFailed.value = true;
    globalMessage.value = (error as Error).message;
  } finally {
    restoring.value = false;
  }
}

async function cancelPendingRestore(): Promise<void> {
  try {
    await api.cancelRestore();
    restorePending.value = null;
    globalMessage.value = t("settings.backup.restoreCancelled");
  } catch (error) {
    globalFailed.value = true;
    globalMessage.value = (error as Error).message;
  }
}

function isPending(key: string): boolean {
  return pendingParts.value.includes(key);
}

/** 本次勾选里含凭据的分区，用于提示用户「自动备份也会收录」。 */
const riskySelected = computed(() =>
  parts.value.filter((part) => part.credential && selectedParts.value.includes(part.key)),
);

/** 已收录的含凭据分区名，文案里直接念出来，别让用户对「它」猜。 */
const riskyNames = computed(() => riskySelected.value.map((part) => part.label).join("、"));

/** 备份说明只留全局备份这一条，其余讲的都是已经下线的快照恢复。 */
const NOTES = ["settings.backup.rule4"] as const;

async function loadGlobal(): Promise<void> {
  try {
    const data = await api.globalBackups();
    parts.value = data.parts;
    globalSnapshots.value = data.snapshots;
    // 首次进来按后端给的「自动备份范围」勾选：它默认就是不含凭据的 config，
    // 且用户之前确认过的含凭据分区会保留在这里，刷新页面不会丢。
    if (!selectedParts.value.length) selectedParts.value = [...data.autoParts];
  } catch (error) {
    globalFailed.value = true;
    globalMessage.value = (error as Error).message;
  }
}

/** 把当前勾选写进偏好，自动备份从此用这个范围。 */
async function persistParts(next: string[]): Promise<void> {
  try {
    prefs.value = await api.savePreferences({ autoGlobalParts: next });
  } catch (error) {
    globalFailed.value = true;
    globalMessage.value = (error as Error).message;
    throw error;
  }
}

/**
 * 勾选分区。含凭据分区不直接勾上，先进入确认态（再点一次＝放弃，退回未选）；
 * 取消勾选直接生效，但要留一句反馈，不然「勾掉了」和没反应长得一样。
 */
async function togglePart(key: string): Promise<void> {
  const part = parts.value.find((item) => item.key === key);
  const selected = selectedParts.value.includes(key);

  if (selected) {
    const next = selectedParts.value.filter((item) => item !== key);
    selectedParts.value = next;
    pendingParts.value = pendingParts.value.filter((item) => item !== key);
    globalFailed.value = false;
    globalMessage.value = t("settings.backup.partRemoved", { name: part?.label ?? key });
    try {
      await persistParts(next);
    } catch {
      /* 提示已在 persistParts 里写好，勾选状态保持用户看到的最新值 */
    }
    return;
  }

  // 已在待确认态：再点一次视为放弃，不必非得去找「取消」按钮。
  if (isPending(key)) {
    cancelPart(key);
    return;
  }

  if (part?.credential) {
    // 含凭据分区：停在确认态，等用户明确点头。多个可以同时挂着。
    pendingParts.value = [...pendingParts.value, key];
    return;
  }

  const next = [...selectedParts.value, key];
  selectedParts.value = next;
  try {
    await persistParts(next);
  } catch {
    /* 同上 */
  }
}

/** 确认纳入含凭据分区：勾上 + 写进自动备份范围。 */
async function confirmPart(key: string): Promise<void> {
  if (!isPending(key)) return;
  const next = [...selectedParts.value, key];
  try {
    await persistParts(next);
    selectedParts.value = next;
    pendingParts.value = pendingParts.value.filter((item) => item !== key);
    globalFailed.value = false;
    globalMessage.value = t("settings.backup.partAdded", {
      name: parts.value.find((item) => item.key === key)?.label ?? key,
    });
  } catch {
    /* 失败时保持待确认态，用户可以重试 */
  }
}

function cancelPart(key?: string): void {
  pendingParts.value = key
    ? pendingParts.value.filter((item) => item !== key)
    : [];
}

async function createGlobal(): Promise<void> {
  // 勾选时已经确认过，这里不再二次确认，直接备份。
  busy.value = true;
  globalMessage.value = null;
  globalFailed.value = false;
  try {
    const snapshot = await api.createGlobalBackup(
      selectedParts.value,
      t("settings.backup.noteManual"),
      true,
    );
    globalMessage.value = t("settings.backup.backupDone", {
      count: snapshot.fileCount,
      size: formatBytes(snapshot.totalBytes),
    });
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
  void loadGlobal();
  void loadRestoreStatus();
});
</script>

<template>
  <div class="space-y-4">
    <SectionCard
      :title="t('settings.backup.globalTitle')"
      :subtitle="t('settings.backup.globalSubtitle', { count: globalSnapshots.length })"
      flush
    >
      <template #actions>
        <div class="flex items-center gap-3">
          <label
            class="inline-flex cursor-pointer select-none items-center gap-2 text-body-sm text-ink-soft"
            :title="t('settings.backup.autoGlobalHint')"
          >
            <ToggleSwitch
              :model-value="!!prefs?.autoGlobalBackup"
              :disabled="!prefs || prefsBusy"
              @update:model-value="() => toggleAuto()"
            />
            {{ t("settings.backup.autoGlobalLabel") }}
          </label>
          <button
            type="button"
            class="rounded-control border border-line px-2.5 py-1.5 text-body-sm text-ink-soft transition-colors hover:border-line-strong hover:text-ink disabled:opacity-50"
            :disabled="busy || selectedParts.length === 0"
            @click="createGlobal"
          >
            {{ t("settings.backup.backupNow") }}
          </button>
        </div>
      </template>

      <div class="border-b border-line-soft px-5 py-2">
        <div v-for="part in parts" :key="part.key" class="py-1.5">
          <label class="flex cursor-pointer items-start gap-2.5">
            <!-- 待确认期间走 indeterminate（横杠）：勾下去必须看得见「已接住，等你确认」 -->
            <input
              type="checkbox"
              class="mt-0.5 size-[15px] accent-[var(--color-brand)]"
              :checked="selectedParts.includes(part.key) || isPending(part.key)"
              :indeterminate="isPending(part.key)"
              @change="togglePart(part.key)"
            />
            <span class="min-w-0 flex-1">
              <span class="flex items-center gap-2 text-body font-medium text-ink">
                {{ part.label }}
                <StatusPill v-if="part.credential" tone="warn">
                  {{ t("settings.backup.credential") }}
                </StatusPill>
              </span>
              <span class="mt-0.5 block truncate text-caption text-muted">
                {{ part.files.join(" · ") }}
              </span>
            </span>
          </label>

          <!-- 确认块贴着被勾的那一项，而不是甩到列表底部让人看不见 -->
          <div
            v-if="isPending(part.key)"
            class="mt-1.5 ml-[22px] flex items-start gap-2 rounded-control bg-warn-soft px-3 py-2 text-body-sm leading-relaxed text-warn"
          >
            <ShieldAlert :size="14" :stroke-width="1.9" class="mt-0.5 shrink-0" />
            <div class="min-w-0 flex-1">
              <p>{{ t("settings.backup.partConfirm", { names: part.label }) }}</p>
              <div class="mt-1.5 flex items-center gap-2">
                <button
                  type="button"
                  class="rounded-control bg-warn px-2.5 py-1 text-caption font-medium text-white transition-opacity hover:opacity-90"
                  @click="confirmPart(part.key)"
                >
                  {{ t("settings.backup.confirmPart") }}
                </button>
                <button
                  type="button"
                  class="rounded-control border border-line px-2.5 py-1 text-caption text-ink-soft transition-colors hover:text-ink"
                  @click="cancelPart(part.key)"
                >
                  {{ t("settings.backup.cancelPart") }}
                </button>
              </div>
            </div>
          </div>
        </div>

        <!-- 已收录含凭据分区：把后果摆明，别用灰字糊过去 -->
        <p
          v-if="riskySelected.length"
          class="mt-1.5 flex items-start gap-2 rounded-control bg-warn-soft/70 px-3 py-2 text-caption leading-relaxed text-warn"
        >
          <ShieldAlert :size="13" :stroke-width="1.9" class="mt-0.5 shrink-0" />
          <span>{{ t("settings.backup.autoScopeNote", { names: riskyNames }) }}</span>
        </p>
      </div>

      <div v-if="globalMessage" class="border-b border-line-soft px-5 py-2.5">
        <p
          class="flex items-center gap-1.5 text-body-sm"
          :class="globalFailed ? 'text-danger' : 'text-brand-dark'"
        >
          <AlertTriangle v-if="globalFailed" :size="13" />
          <CheckCircle2 v-else :size="13" />
          {{ globalMessage }}
        </p>
      </div>

      <div v-if="restorePending" class="border-b border-line-soft bg-warn-soft/60 px-5 py-2.5">
        <p class="flex flex-wrap items-center gap-2 text-body-sm text-warn">
          <RotateCcw :size="13" />
          {{ t("settings.backup.restorePending", { id: restorePending.snapshotId }) }}
          <button
            type="button"
            class="rounded-control border border-warn/40 px-2 py-0.5 text-caption hover:bg-warn/10"
            @click="cancelPendingRestore"
          >
            {{ t("settings.backup.restoreCancel") }}
          </button>
        </p>
      </div>
      <p
        v-else-if="restoreResult"
        class="border-b border-line-soft px-5 py-2.5 text-body-sm"
        :class="restoreResult.ok ? 'text-brand-dark' : 'text-danger'"
      >
        {{
          restoreResult.ok
            ? t("settings.backup.restoreDone", { id: restoreResult.snapshotId })
            : t("settings.backup.restoreFailed", { error: restoreResult.error ?? "" })
        }}
      </p>

      <p v-if="!globalSnapshots.length" class="px-5 py-8 text-center text-body text-muted">
        {{ t("settings.backup.globalEmpty") }}
      </p>

      <ul v-else class="divide-y divide-line-soft">
        <li
          v-for="snapshot in globalSnapshots"
          :key="snapshot.id"
          class="flex flex-wrap items-center gap-3 px-5 py-2.5"
        >
          <FileLock2 :size="14" :stroke-width="1.9" class="shrink-0 text-muted" />
          <span class="text-body-sm text-ink-soft">{{ formatTime(snapshot.createdAt) }}</span>
          <span class="text-body-sm text-muted">
            {{ t("settings.backup.fileCount", { count: snapshot.fileCount }) }} ·
            {{ formatBytes(snapshot.totalBytes) }}
          </span>
          <StatusPill :tone="snapshot.verified ? 'brand' : 'warn'">
            {{ snapshot.verified ? t("settings.backup.verified") : t("settings.backup.unverified") }}
          </StatusPill>
          <button
            type="button"
            class="ml-auto rounded-control border border-warn/40 px-2.5 py-1 text-caption text-warn transition-colors hover:bg-warn-soft disabled:opacity-50"
            :disabled="busy || restoring"
            @click="askRestore(snapshot)"
          >
            {{ t("settings.backup.restore") }}
          </button>
          <button
            type="button"
            class="rounded-control border border-line px-2.5 py-1 text-caption text-ink-soft transition-colors hover:border-line-strong hover:text-ink disabled:opacity-50"
            :disabled="busy"
            @click="verifyGlobal(snapshot)"
          >
            {{ t("settings.backup.verify") }}
          </button>
        </li>
      </ul>
    </SectionCard>

    <SectionCard
      :title="t('settings.backup.rulesTitle')"
      :subtitle="t('settings.backup.rulesSubtitle')"
    >
      <ul class="space-y-2 text-body-sm leading-relaxed text-ink-soft">
        <li v-for="note in NOTES" :key="note">{{ t(note) }}</li>
      </ul>
    </SectionCard>
    <ConfirmDialog
      :open="restoreTarget !== null"
      :title="t('settings.backup.restoreConfirmTitle')"
      :message="
        restoreTarget
          ? t('settings.backup.restoreConfirmBody', {
              time: formatTime(restoreTarget.createdAt),
            })
          : ''
      "
      :confirm-text="t('settings.backup.restoreConfirm')"
      tone="danger"
      :busy="restoring"
      @confirm="confirmRestore"
      @cancel="restoreTarget = null"
    />
  </div>
</template>
