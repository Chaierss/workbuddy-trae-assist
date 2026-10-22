<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { AlertTriangle, CheckCircle2, FileLock2, ShieldAlert } from "lucide-vue-next";
import SectionCard from "../../components/SectionCard.vue";
import StatusPill from "../../components/StatusPill.vue";
import ToggleSwitch from "../../components/ToggleSwitch.vue";
import {
  api,
  formatBytes,
  formatTime,
  type GlobalBackupPart,
  type Preferences,
  type Snapshot,
} from "../../api";
import { t } from "../../lib/i18n";

// ---------- 全局备份（本工具自己的数据，按分区勾选） ----------
//
// 客户端数据快照（切换前 / 每日自动）与客户端设置备份点这两块已经从界面下线：
// 它们各自带一套「原地恢复」，和这里的「导出型备份」混在一页反而分不清哪个能回滚。
// 现在只留全局备份：按分区导出、可校验，不做原地恢复。

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
/** 勾了含凭据的分区时先停在确认态：再点一次才真的写。 */
const confirming = ref(false);
const globalMessage = ref<string | null>(null);
const globalFailed = ref(false);

/** 本次勾选里含凭据的分区，用于确认文案与按钮态。 */
const riskySelected = computed(() =>
  parts.value.filter((part) => part.credential && selectedParts.value.includes(part.key)),
);

/** 备份说明只留全局备份这一条，其余讲的都是已经下线的快照恢复。 */
const NOTES = ["settings.backup.rule4"] as const;

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
  void loadGlobal();
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
            class="inline-flex cursor-pointer select-none items-center gap-2 text-[12px] text-ink-soft"
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
        </div>
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
      :title="t('settings.backup.rulesTitle')"
      :subtitle="t('settings.backup.rulesSubtitle')"
    >
      <ul class="space-y-2 text-[12.5px] leading-relaxed text-ink-soft">
        <li v-for="note in NOTES" :key="note">{{ t(note) }}</li>
      </ul>
    </SectionCard>
  </div>
</template>
