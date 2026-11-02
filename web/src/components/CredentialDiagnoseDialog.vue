<script setup lang="ts">
import { ref, watch } from "vue";
import { CheckCircle2, Loader2, Stethoscope, XCircle } from "lucide-vue-next";
import { api, type CredentialDiagnoseResult } from "../api";
import { t } from "../lib/i18n";

/**
 * 凭据一键诊断（只读）。
 *
 * 后端六步逐项给出结论：能不能读、能不能续、设备是否本机、到期情况、
 * 是否被退避 / 熔断、只读验活是否通过。这里只负责展示，不提供任何写操作 ——
 * 想把自动续期恢复回来，走号池卡片上的「继续自动续期」。
 */
const props = defineProps<{
  open: boolean;
  accountId: string | null;
  accountName?: string;
}>();

const emit = defineEmits<{ close: [] }>();

const loading = ref(false);
const error = ref("");
const result = ref<CredentialDiagnoseResult | null>(null);

watch(
  () => [props.open, props.accountId] as const,
  async ([open, accountId]) => {
    if (!open || !accountId) return;
    loading.value = true;
    error.value = "";
    result.value = null;
    try {
      result.value = await api.diagnoseCredential(accountId);
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause);
    } finally {
      loading.value = false;
    }
  },
);

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
        class="max-h-[calc(100vh-2rem)] w-full max-w-[min(480px,calc(100vw-2rem))] overflow-y-auto rounded-card border border-line bg-surface p-5 shadow-float"
      >
        <h2 class="flex items-center gap-2 text-title font-semibold text-ink">
          <Stethoscope :size="17" :stroke-width="1.9" class="text-brand" />
          {{ t("dialog.diagnose.title", { name: accountName || accountId || "" }) }}
        </h2>
        <p class="mt-1.5 text-body-sm leading-relaxed text-muted">
          {{ t("dialog.diagnose.desc") }}
        </p>

        <div v-if="loading" class="mt-4 flex items-center gap-2 text-body-sm text-muted">
          <Loader2 :size="14" class="animate-spin text-brand" />
          {{ t("dialog.diagnose.loading") }}
        </div>
        <div v-else-if="error" class="mt-4 text-body-sm text-danger">
          {{ t("dialog.diagnose.error", { message: error }) }}
        </div>
        <div v-else-if="result" class="mt-4 space-y-3">
          <ul class="space-y-1.5">
            <li
              v-for="step in result.steps"
              :key="step.key"
              class="flex items-start gap-2 text-body-sm"
            >
              <CheckCircle2
                v-if="step.ok"
                :size="14"
                class="mt-0.5 shrink-0 text-brand"
              />
              <XCircle v-else :size="14" class="mt-0.5 shrink-0 text-danger" />
              <div class="min-w-0 flex-1">
                <span class="text-ink-soft">{{ step.detail }}</span>
              </div>
              <span
                class="shrink-0 text-caption"
                :class="step.ok ? 'text-brand-dark' : 'text-danger'"
              >
                {{ step.ok ? t("dialog.diagnose.stepOk") : t("dialog.diagnose.stepBad") }}
              </span>
            </li>
          </ul>
          <div class="rounded-control border border-line bg-line-soft/40 p-3">
            <div class="text-caption font-medium text-ink-soft">
              {{ t("dialog.diagnose.conclusion") }}
            </div>
            <div class="mt-0.5 text-body-sm text-ink">{{ result.conclusion }}</div>
            <div class="mt-2 text-caption font-medium text-ink-soft">
              {{ t("dialog.diagnose.suggest") }}
            </div>
            <div class="mt-0.5 text-body-sm text-ink">{{ result.suggest }}</div>
          </div>
        </div>

        <div class="mt-5 flex justify-end">
          <button
            type="button"
            class="rounded-control border border-line px-3 py-1.5 text-body-sm text-ink-soft transition-colors hover:border-line-strong hover:text-ink"
            @click="close"
          >
            {{ t("dialog.diagnose.close") }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>
