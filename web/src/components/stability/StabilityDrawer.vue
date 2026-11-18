<script setup lang="ts">
import { computed } from "vue";
import { ShieldOff, X } from "lucide-vue-next";
import { api, type IsolationItem, type StabilityAccount } from "../../api";
import { formatTime } from "../../api";
import { t } from "../../lib/i18n";
import { toastError, toastSuccess } from "../../lib/toast";

/**
 * 稳定度抽屉：回答「这个号最近靠不靠得住」。
 *
 * 只展示本地已落盘记录，未知一律写「样本不足」；隔离只影响网关候选，
 * 不影响切号与数据 —— 这句话必须写在这里，避免用户误以为号坏了。
 */
const props = defineProps<{
  open: boolean;
  items: StabilityAccount[];
  isolation: IsolationItem[];
  days: 7 | 30;
  loading?: boolean;
}>();

const emit = defineEmits<{
  close: [];
  days: [days: 7 | 30];
  refresh: [];
}>();

const rows = computed(() =>
  [...props.items].sort((a, b) => {
    const left = a.switchRate ?? 1;
    const right = b.switchRate ?? 1;
    if (left !== right) return left - right;
    return a.displayName.localeCompare(b.displayName);
  }),
);

const activeIsolation = computed(() => props.isolation.filter((item) => item.active));

function metric(value: number | null, suffix = ""): string {
  return value === null ? t("accounts.stability.unknown") : `${value}${suffix}`;
}

function rate(value: number | null): string {
  return value === null ? t("accounts.stability.unknown") : `${Math.round(value * 100)}%`;
}

async function release(item: IsolationItem): Promise<void> {
  try {
    await api.releaseIsolation(item.accountId);
    toastSuccess(t("accounts.stability.released"));
    emit("refresh");
  } catch (cause) {
    toastError((cause as Error)?.message || t("accounts.stability.releaseFailed"));
  }
}
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="fixed inset-0 z-[60] bg-ink/20" @click.self="emit('close')">
      <aside
        class="absolute inset-y-0 right-0 flex w-full max-w-[560px] flex-col border-l border-line bg-surface shadow-2xl"
      >
        <header class="flex items-start gap-2 border-b border-line-soft px-4 py-3">
          <div class="min-w-0 flex-1">
            <div class="text-body font-semibold text-ink">{{ t("accounts.stability.title") }}</div>
            <div class="mt-0.5 text-caption text-muted">{{ t("accounts.stability.desc") }}</div>
          </div>
          <div class="flex shrink-0 items-center gap-1 rounded-control border border-line p-0.5">
            <button
              v-for="option in [7, 30] as const"
              :key="option"
              type="button"
              class="rounded-badge px-2 py-0.5 text-caption transition-colors"
              :class="
                days === option ? 'bg-brand-soft text-brand-dark' : 'text-muted hover:text-ink'
              "
              @click="emit('days', option)"
            >
              {{ t("accounts.stability.days", { days: option }) }}
            </button>
          </div>
          <button
            type="button"
            class="grid size-7 shrink-0 place-items-center rounded-control text-muted transition-colors hover:bg-line-soft hover:text-ink"
            :aria-label="t('common.close')"
            @click="emit('close')"
          >
            <X :size="15" :stroke-width="1.9" />
          </button>
        </header>

        <div class="flex-1 overflow-y-auto px-4 py-3">
          <div
            v-if="activeIsolation.length"
            class="mb-3 rounded-control border border-warn/30 bg-warn-soft px-3 py-2 text-body-sm text-warn"
          >
            <div class="font-medium">{{ t("accounts.stability.isolationTitle") }}</div>
            <div class="mt-0.5 text-caption leading-relaxed">{{ t("accounts.stability.isolationHint") }}</div>
            <div
              v-for="item in activeIsolation"
              :key="item.accountId"
              class="mt-2 flex items-center gap-2 text-caption"
            >
              <span class="min-w-0 flex-1 truncate">
                {{ rows.find((row) => row.accountId === item.accountId)?.displayName ?? item.accountId }}
                · {{ t("accounts.stability.isolatedUntil", { time: item.isolatedUntil ? formatTime(item.isolatedUntil) : "" }) }}
              </span>
              <button
                type="button"
                class="inline-flex shrink-0 items-center gap-1 rounded-badge border border-warn/40 px-1.5 py-0.5 transition-colors hover:bg-surface"
                @click="release(item)"
              >
                <ShieldOff :size="10" :stroke-width="2" />
                {{ t("accounts.stability.release") }}
              </button>
            </div>
          </div>

          <p v-if="loading && !rows.length" class="py-8 text-center text-body-sm text-muted">
            {{ t("accounts.stability.loading") }}
          </p>
          <p v-else-if="!rows.length" class="py-8 text-center text-body-sm text-muted">
            {{ t("accounts.stability.empty") }}
          </p>

          <div v-else class="divide-y divide-line-soft">
            <div v-for="row in rows" :key="row.accountId" class="py-2.5">
              <div class="flex items-center gap-2">
                <span class="min-w-0 flex-1 truncate text-body-sm font-medium text-ink">
                  {{ row.displayName }}
                </span>
                <span
                  class="shrink-0 text-caption tabular-nums"
                  :class="
                    row.switchRate === null
                      ? 'text-muted'
                      : row.switchRate >= 0.9
                        ? 'text-brand-dark'
                        : row.switchRate >= 0.7
                          ? 'text-warn'
                          : 'text-danger'
                  "
                >
                  {{ rate(row.switchRate) }}
                </span>
              </div>
              <div class="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-caption text-muted">
                <span>{{ t("accounts.stability.switch", { ok: row.switchOk, total: row.switchTotal }) }}</span>
                <span>{{ t("accounts.stability.loginInvalid", { count: metric(row.loginInvalid) }) }}</span>
                <span>{{ t("accounts.stability.refreshFailed", { count: metric(row.refreshFailed) }) }}</span>
                <span>{{ t("accounts.stability.gatewayFailed", { count: metric(row.gatewayFailed) }) }}</span>
              </div>
              <div v-if="row.lastReason" class="mt-1 truncate text-caption text-warn" :title="row.lastReason">
                {{ row.lastReason }}
                <span v-if="row.lastAt" class="text-muted"> · {{ formatTime(row.lastAt) }}</span>
              </div>
            </div>
          </div>
        </div>
      </aside>
    </div>
  </Teleport>
</template>
