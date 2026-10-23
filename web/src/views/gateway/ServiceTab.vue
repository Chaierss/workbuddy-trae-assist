<script setup lang="ts">
import SectionCard from "../../components/SectionCard.vue";
import StatusPill from "../../components/StatusPill.vue";
import { t } from "../../lib/i18n";
import { useGateway } from "./useGateway";

/** 服务：开停开关 + 监听配置。改了不保存的不该看起来已生效，所以表单单独存。 */
const store = useGateway();

function saveForm(): void {
  void store.save({
    host: store.form.host,
    port: Number(store.form.port),
    allowNonLoopback: store.form.allowNonLoopback,
    maxHops: Number(store.form.maxHops),
    logKeep: Number(store.form.logKeep),
  });
}
</script>

<template>
  <div v-if="!store.status" class="py-8 text-center text-[12.5px] text-muted">
    {{ t("gateway.loading") }}
  </div>

  <div v-else class="grid gap-4 lg:grid-cols-2">
    <SectionCard :title="t('gateway.service.title')" :subtitle="t('gateway.service.subtitle')">
      <template #actions>
        <StatusPill :tone="store.status.enabled ? 'brand' : 'neutral'">
          {{ store.status.enabled ? t("gateway.service.enabled") : t("gateway.service.disabled") }}
        </StatusPill>
        <button
          type="button"
          class="h-6 w-10 rounded-full transition-colors disabled:opacity-60"
          :class="store.status.enabled ? 'bg-brand' : 'bg-line'"
          :disabled="store.saving"
          @click="store.save({ enabled: !store.status.enabled })"
        >
          <span
            class="block size-4.5 rounded-full bg-white transition-transform"
            :class="store.status.enabled ? 'translate-x-[22px]' : 'translate-x-[3px]'"
          ></span>
        </button>
      </template>

      <div class="space-y-3.5">
        <label class="flex items-center gap-3">
          <span class="w-24 shrink-0 text-[12.5px] text-ink-soft">{{ t("gateway.service.host") }}</span>
          <input
            v-model="store.form.host"
            type="text"
            spellcheck="false"
            class="h-8 min-w-0 flex-1 rounded-[9px] border border-line bg-surface px-3 text-[12.5px] text-ink placeholder:text-muted focus:border-[#d5d8d3] focus:outline-none"
          />
        </label>
        <label class="flex items-center gap-3">
          <span class="w-24 shrink-0 text-[12.5px] text-ink-soft">{{ t("gateway.service.port") }}</span>
          <input
            v-model.number="store.form.port"
            type="number"
            min="1024"
            max="65535"
            class="h-8 w-32 rounded-[9px] border border-line bg-surface px-3 text-[12.5px] text-ink focus:border-[#d5d8d3] focus:outline-none"
          />
        </label>
        <label class="flex items-center gap-3">
          <span class="w-24 shrink-0 text-[12.5px] text-ink-soft">{{ t("gateway.service.maxHops") }}</span>
          <input
            v-model.number="store.form.maxHops"
            type="number"
            min="1"
            max="8"
            class="h-8 w-32 rounded-[9px] border border-line bg-surface px-3 text-[12.5px] text-ink focus:border-[#d5d8d3] focus:outline-none"
          />
          <span class="text-[11.5px] text-muted">{{ t("gateway.service.maxHopsHint") }}</span>
        </label>
        <label class="flex items-center gap-3">
          <span class="w-24 shrink-0 text-[12.5px] text-ink-soft">{{ t("gateway.service.logKeep") }}</span>
          <input
            v-model.number="store.form.logKeep"
            type="number"
            min="50"
            max="5000"
            class="h-8 w-32 rounded-[9px] border border-line bg-surface px-3 text-[12.5px] text-ink focus:border-[#d5d8d3] focus:outline-none"
          />
          <span class="text-[11.5px] text-muted">{{ t("gateway.service.logKeepHint") }}</span>
        </label>

        <label class="flex items-start gap-3">
          <span class="w-24 shrink-0 pt-1 text-[12.5px] text-ink-soft">{{ t("gateway.service.nonLoopback") }}</span>
          <span class="flex min-w-0 flex-1 flex-col gap-1">
            <button
              type="button"
              class="h-6 w-10 shrink-0 rounded-full transition-colors"
              :class="store.form.allowNonLoopback ? 'bg-brand' : 'bg-line'"
              @click="store.form.allowNonLoopback = !store.form.allowNonLoopback"
            >
              <span
                class="block size-4.5 rounded-full bg-white transition-transform"
                :class="store.form.allowNonLoopback ? 'translate-x-[22px]' : 'translate-x-[3px]'"
              ></span>
            </button>
            <span class="text-[11.5px] leading-relaxed text-muted">
              {{ t("gateway.service.nonLoopbackHintPrefix") }} <code>0.0.0.0</code> {{ t("gateway.service.nonLoopbackHintSuffix") }}
            </span>
          </span>
        </label>

        <div class="flex items-center gap-2 pt-1">
          <button
            type="button"
            class="rounded-[9px] bg-brand px-3.5 py-2 text-[12.5px] font-medium text-white transition-colors hover:bg-brand-dark disabled:opacity-60"
            :disabled="store.saving || !store.dirty"
            @click="saveForm"
          >
            {{ store.saving ? t("gateway.service.saving") : t("gateway.service.save") }}
          </button>
          <span v-if="store.dirty" class="text-[11.5px] text-warn">{{ t("gateway.service.dirty") }}</span>
        </div>
      </div>
    </SectionCard>

    <SectionCard :title="t('gateway.service.diagTitle')" :subtitle="t('gateway.service.diagSubtitle')">
      <div class="space-y-3 text-[12.5px]">
        <div class="flex items-center justify-between gap-3 border-b border-line-soft pb-2.5">
          <span class="text-ink-soft">{{ t("gateway.service.listeningStatus") }}</span>
          <StatusPill :tone="store.status.running ? 'brand' : store.status.enabled ? 'danger' : 'neutral'">
            {{
              store.status.running
                ? t("gateway.listening")
                : store.status.enabled
                  ? t("gateway.startFailed")
                  : t("common.off")
            }}
          </StatusPill>
        </div>
        <div class="flex items-center justify-between gap-3 border-b border-line-soft pb-2.5">
          <span class="text-ink-soft">{{ t("gateway.service.bindScope") }}</span>
          <span class="text-muted">
            {{
              store.status.loopbackOnly
                ? t("gateway.service.loopbackOnly")
                : t("gateway.service.nonLoopbackReachable")
            }}
          </span>
        </div>
        <div class="flex items-center justify-between gap-3 border-b border-line-soft pb-2.5">
          <span class="text-ink-soft">{{ t("gateway.stat.forwardAccounts") }}</span>
          <span class="text-muted">{{ t("gateway.service.accountCount", { count: store.status.accounts }) }}</span>
        </div>
        <div class="flex items-center justify-between gap-3">
          <span class="text-ink-soft">{{ t("gateway.service.lastRequest") }}</span>
          <span class="text-muted">
            {{
              store.status.stats.lastRequestAt
                ? store.status.stats.lastRequestAt
                : t("gateway.service.noRequest")
            }}
          </span>
        </div>
        <div
          v-if="store.status.error"
          class="rounded-[10px] bg-danger-soft px-3 py-2 text-[11.5px] leading-relaxed text-danger"
        >
          {{ store.status.error }}
        </div>
      </div>
    </SectionCard>
  </div>
</template>
