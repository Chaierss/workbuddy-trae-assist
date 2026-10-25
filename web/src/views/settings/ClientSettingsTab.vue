<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { FileCog, Save, ShieldCheck } from "lucide-vue-next";
import SectionCard from "../../components/SectionCard.vue";
import StatusPill from "../../components/StatusPill.vue";
import ToggleSwitch from "../../components/ToggleSwitch.vue";
import { t } from "../../lib/i18n";
import { api, type ClientSettingSpec, type ClientSettingsSnapshot } from "../../api";

/**
 * 设置 → 客户端设置：改上游客户端 `settings.json` 里**登记过**的几个键。
 *
 * 写入前自动落备份点。备份点列表已经从「备份」页下线（那一页只留全局备份），
 * 界面上不再提供浏览与恢复入口，接口与磁盘上的备份点都还在。
 * 三道关：白名单（未登记的键一律不碰）、格式指纹（读取后文件被改过就拒绝写）、
 * 字节级最小补丁（除目标值那一段，其它字节逐字节不变），写入前还会自动备份。
 * 前端只负责把「当前值 → 待改值」摆清楚，判定全在后端。
 */
const snapshot = ref<ClientSettingsSnapshot | null>(null);
const draft = ref<Record<string, string | boolean>>({});
const loading = ref(true);
const error = ref("");
const busy = ref(false);
const flash = ref<{ text: string; tone: "ok" | "bad" } | null>(null);

function currentOf(spec: ClientSettingSpec): string | boolean {
  return spec.present && spec.value !== null ? spec.value : spec.default;
}

function resetDraft(): void {
  const next: Record<string, string | boolean> = {};
  for (const spec of snapshot.value?.specs ?? []) next[spec.key] = currentOf(spec);
  draft.value = next;
}

/** 「当前值 → 待改值」的差异；只把这些发给后端。 */
const pending = computed(() => {
  const items: { spec: ClientSettingSpec; before: string | boolean; after: string | boolean }[] = [];
  for (const spec of snapshot.value?.specs ?? []) {
    const before = currentOf(spec);
    const after = draft.value[spec.key];
    if (after !== undefined && after !== before) items.push({ spec, before, after });
  }
  return items;
});

function render(value: string | boolean | null): string {
  if (typeof value === "boolean") {
    return value ? t("clientSettings.valueOn") : t("clientSettings.valueOff");
  }
  if (value === null || value === "") return t("clientSettings.unset");
  return value;
}

async function load(): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    snapshot.value = await api.clientSettings();
    resetDraft();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause);
  } finally {
    loading.value = false;
  }
}

async function save(): Promise<void> {
  if (!snapshot.value || !snapshot.value.fingerprint) return;
  const updates: Record<string, string | boolean> = {};
  for (const item of pending.value) updates[item.spec.key] = item.after;
  if (!Object.keys(updates).length) return;

  busy.value = true;
  flash.value = null;
  try {
    const result = await api.writeClientSettings(updates, snapshot.value.fingerprint);
    snapshot.value = result.settings;
    resetDraft();
    flash.value = {
      text: result.backupId
        ? t("clientSettings.savedWithBackup", {
            count: result.changed.length,
            backup: result.backupId,
          })
        : t("clientSettings.noChange"),
      tone: "ok",
    };
  } catch (cause) {
    flash.value = { text: cause instanceof Error ? cause.message : String(cause), tone: "bad" };
    await load();
  } finally {
    busy.value = false;
  }
}

function setValue(key: string, value: string | boolean): void {
  draft.value = { ...draft.value, [key]: value };
}

onMounted(() => {
  void load();
});
</script>

<template>
  <div class="space-y-4">
    <SectionCard
      :title="t('clientSettings.title')"
      :subtitle="t('clientSettings.subtitle')"
    >
      <p v-if="loading" class="py-6 text-center text-body text-muted">
        {{ t("clientSettings.loading") }}
      </p>
      <p v-else-if="error" class="py-6 text-center text-body text-rose-500">{{ error }}</p>

      <div v-else-if="snapshot && !snapshot.available" class="space-y-2 py-4">
        <div class="flex items-center gap-2">
          <StatusPill tone="neutral">{{ t("clientSettings.unreadable") }}</StatusPill>
          <span class="text-body-sm text-muted">{{ snapshot.message }}</span>
        </div>
        <p class="text-body-sm leading-relaxed text-muted">
          {{ t("clientSettings.pathLabel")
          }}<span class="font-mono text-ink-soft">{{ snapshot.path }}</span>
          <br />{{ t("clientSettings.pathHint") }}
        </p>
      </div>

      <div v-else-if="snapshot" class="space-y-4">
        <div class="space-y-3 divide-y divide-line-soft">
          <div v-for="spec in snapshot.specs" :key="spec.key" class="flex items-start gap-3 pt-3">
            <div class="min-w-0 flex-1">
              <div class="flex items-center gap-2">
                <span class="text-body font-medium text-ink">{{ spec.label }}</span>
                <span class="font-mono text-caption text-muted">{{ spec.key }}</span>
                <StatusPill v-if="!spec.present" tone="neutral">{{
                  t("clientSettings.notInFile")
                }}</StatusPill>
              </div>
              <div class="mt-0.5 text-body-sm text-muted">{{ spec.hint }}</div>
            </div>

            <ToggleSwitch
              v-if="spec.kind === 'bool'"
              class="mt-0.5 shrink-0"
              :model-value="!!draft[spec.key]"
              :disabled="busy"
              @update:model-value="(value) => setValue(spec.key, value)"
            />
            <input
              v-else
              :value="draft[spec.key]"
              type="text"
              class="w-[200px] shrink-0 rounded-md border border-line-soft bg-surface px-2 py-1 text-body-sm text-ink outline-none"
              :disabled="busy"
              @input="setValue(spec.key, ($event.target as HTMLInputElement).value)"
            />
          </div>
        </div>

        <div v-if="pending.length" class="rounded-md border border-line-soft px-3 py-2">
          <div class="mb-1 text-body-sm font-medium text-ink-soft">
            {{ t("clientSettings.pendingTitle") }}
          </div>
          <div v-for="item in pending" :key="item.spec.key" class="text-body-sm text-muted">
            {{ item.spec.label }}：
            <span class="text-muted">{{ render(item.before) }}</span>
            →
            <span class="text-brand-dark">{{ render(item.after) }}</span>
          </div>
        </div>

        <div class="flex flex-wrap items-center gap-2">
          <button
            type="button"
            class="inline-flex items-center gap-1 rounded-md border border-line px-2.5 py-1 text-body-sm text-ink hover:border-brand hover:text-brand-dark disabled:opacity-50"
            :disabled="busy || !pending.length"
            @click="save"
          >
            <Save :size="12" :stroke-width="2" />{{ t("clientSettings.save") }}
          </button>
          <button
            type="button"
            class="rounded-md border border-dashed border-line px-2.5 py-1 text-body-sm text-muted hover:border-brand hover:text-brand-dark disabled:opacity-50"
            :disabled="busy || !pending.length"
            @click="resetDraft"
          >
            {{ t("clientSettings.discard") }}
          </button>
          <span
            v-if="flash"
            class="text-body-sm"
            :class="flash.tone === 'ok' ? 'text-brand-dark' : 'text-rose-500'"
          >
            {{ flash.text }}
          </span>
        </div>
      </div>
    </SectionCard>

    <p class="flex items-start gap-1.5 text-body-sm leading-relaxed text-muted">
      <ShieldCheck :size="13" class="mt-0.5 shrink-0" />
      <span>
        {{ t("clientSettings.fingerprintNoteA") }}
        <FileCog :size="12" class="inline -mt-0.5" />
        {{ t("clientSettings.fingerprintNoteB") }}
      </span>
    </p>
  </div>
</template>
