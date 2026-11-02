<script setup lang="ts">
import { ref, watch } from "vue";
import { CircleAlert, History, Loader2 } from "lucide-vue-next";
import { api, formatTime, type CredentialProbeEntry } from "../api";
import { t, type MessageKey } from "../lib/i18n";

/**
 * 凭据巡检历史（只读时间线）。
 *
 * 每条记录只有「时间 + 结论 + 上游原文 + 是否续期成功 + 下次重试」，
 * 后端刻意不存任何 token，这里也没有可展示的敏感字段。
 */
const props = defineProps<{
  open: boolean;
  accountId: string | null;
  accountName?: string;
}>();

const emit = defineEmits<{ close: [] }>();

const loading = ref(false);
const error = ref("");
const items = ref<CredentialProbeEntry[]>([]);

watch(
  () => [props.open, props.accountId] as const,
  async ([open, accountId]) => {
    if (!open || !accountId) return;
    loading.value = true;
    error.value = "";
    items.value = [];
    try {
      const data = await api.credentialProbes(accountId);
      items.value = [...data.items].reverse();
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause);
    } finally {
      loading.value = false;
    }
  },
);

const STATUS_LABEL: Record<string, string> = {
  refreshed: "dialog.history.statusRefreshed",
  ok: "dialog.history.statusOk",
  noop: "dialog.history.statusNoop",
  relogin_required: "dialog.history.statusRelogin",
  unsupported: "dialog.history.statusUnsupported",
  failed: "dialog.history.statusFailed",
};

function statusLabel(status: string): string {
  const key = STATUS_LABEL[status];
  return key ? t(key as MessageKey) : status;
}

function statusTone(entry: CredentialProbeEntry): string {
  if (entry.status === "failed") return "text-danger";
  if (entry.status === "relogin_required") return "text-warn";
  if (entry.refreshed || entry.status === "refreshed" || entry.status === "ok") {
    return "text-brand-dark";
  }
  return "text-ink-soft";
}

function close(): void {
  emit("close");
}
</script>

<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="fixed inset-0 z-[var(--z-modal)] flex items-center justify-center bg-black/30 p-4 sm:p-6"
      @click.self="close"
    >
      <div
        class="max-h-[calc(100vh-2rem)] w-full max-w-[min(520px,calc(100vw-2rem))] overflow-y-auto rounded-card border border-line bg-surface p-5 shadow-float"
      >
        <h2 class="flex items-center gap-2 text-title font-semibold text-ink">
          <History :size="17" :stroke-width="1.9" class="text-brand" />
          {{ t("dialog.history.title", { name: accountName || accountId || "" }) }}
        </h2>
        <p class="mt-1.5 text-body-sm leading-relaxed text-muted">
          {{ t("dialog.history.desc") }}
        </p>

        <div v-if="loading" class="mt-4 flex items-center gap-2 text-body-sm text-muted">
          <Loader2 :size="14" class="animate-spin text-brand" />
          {{ t("dialog.history.loading") }}
        </div>
        <div v-else-if="error" class="mt-4 text-body-sm text-danger">
          {{ t("dialog.history.error", { message: error }) }}
        </div>
        <div v-else-if="!items.length" class="mt-4 text-body-sm text-muted">
          {{ t("dialog.history.empty") }}
        </div>
        <ul v-else class="mt-4 space-y-2">
          <li
            v-for="(entry, index) in items"
            :key="`${entry.at}-${index}`"
            class="flex items-start gap-2 border-b border-line-soft pb-2 last:border-0 last:pb-0"
          >
            <CircleAlert
              :size="13"
              class="mt-0.5 shrink-0"
              :class="statusTone(entry)"
            />
            <div class="min-w-0 flex-1">
              <div class="flex flex-wrap items-baseline gap-x-2">
                <span class="text-body-sm font-medium" :class="statusTone(entry)">
                  {{ statusLabel(entry.status) }}
                </span>
                <span class="text-caption tabular-nums text-muted">{{ formatTime(entry.at) }}</span>
                <span v-if="entry.code" class="text-caption text-muted">· {{ entry.code }}</span>
              </div>
              <div v-if="entry.detail" class="mt-0.5 break-words text-caption text-ink-soft">
                {{ entry.detail }}
              </div>
              <div v-if="entry.nextRetryAt" class="mt-0.5 text-caption text-muted">
                {{ t("dialog.history.nextRetry", { time: formatTime(entry.nextRetryAt) }) }}
              </div>
            </div>
          </li>
        </ul>

        <div class="mt-5 flex justify-end">
          <button
            type="button"
            class="rounded-control border border-line px-3 py-1.5 text-body-sm text-ink-soft transition-colors hover:border-line-strong hover:text-ink"
            @click="close"
          >
            {{ t("dialog.history.close") }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>
