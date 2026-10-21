<script setup lang="ts">
import { computed } from "vue";
import { Database, HardDrive, ShieldCheck } from "lucide-vue-next";
import SectionCard from "../../components/SectionCard.vue";
import StatusPill from "../../components/StatusPill.vue";
import { clientDetections, health } from "../../stores/app";
import { t } from "../../lib/i18n";

const rows = computed(() => [
  { label: t("env.dataDir"), value: health.value?.dataDir ?? t("common.unknown"), icon: Database },
  { label: t("env.serverUrl"), value: window.location.origin, icon: ShieldCheck },
]);

function statusText(client: { installed: boolean; loggedIn: boolean; running: boolean }): string {
  if (!client.installed) return t("detect.notInstalled");
  if (!client.loggedIn) return t("detect.notLoggedIn");
  return client.running ? t("detect.running") : t("detect.ready");
}

function statusTone(client: { installed: boolean; loggedIn: boolean; running: boolean }): "neutral" | "brand" | "warn" {
  if (!client.installed) return "neutral";
  if (!client.loggedIn) return "warn";
  return client.running ? "warn" : "brand";
}
</script>

<template>
  <div class="space-y-4">
    <SectionCard :title="t('env.localTitle')" :subtitle="t('env.localSubtitle')">
      <ul class="divide-y divide-line-soft">
        <li
          v-for="row in rows"
          :key="row.label"
          class="flex items-start gap-3 py-3 first:pt-0 last:pb-0"
        >
          <component :is="row.icon" :size="15" class="mt-0.5 shrink-0 text-muted" />
          <span class="w-[92px] shrink-0 text-[12.5px] text-muted">{{ row.label }}</span>
          <span class="min-w-0 flex-1 break-all font-mono text-[12px] text-ink-soft">
            {{ row.value }}
          </span>
        </li>
        <li class="flex items-start gap-3 py-3">
          <HardDrive :size="15" class="mt-0.5 shrink-0 text-muted" />
          <span class="w-[92px] shrink-0 text-[12.5px] text-muted">{{ t("env.stateDir") }}</span>
          <div class="min-w-0 flex-1 space-y-1.5">
            <div
              v-for="client in clientDetections"
              :key="client.scope"
              class="break-all font-mono text-[11.5px] text-ink-soft"
            >
              <span class="mr-1 font-sans text-muted">{{ client.label }}：</span>
              {{ client.stateDir ?? t("env.notDetected") }}
            </div>
            <div v-if="!clientDetections.length" class="text-muted">{{ t("env.noClients") }}</div>
          </div>
        </li>
      </ul>
    </SectionCard>

    <SectionCard :title="t('env.clientsTitle')" :subtitle="t('env.clientsSubtitle')">
      <div class="space-y-2.5 text-[12.5px]">
        <div
          v-for="client in clientDetections"
          :key="client.scope"
          class="flex items-center justify-between gap-3"
        >
          <span class="min-w-0 truncate text-muted">{{ client.label }}</span>
          <StatusPill :tone="statusTone(client)" dot>
            {{ statusText(client) }}
          </StatusPill>
        </div>
        <p v-if="!clientDetections.length" class="text-muted">{{ t("env.noClients") }}</p>
      </div>
    </SectionCard>

    <SectionCard :title="t('env.boundaryTitle')" :subtitle="t('env.boundarySubtitle')">
      <ul class="space-y-2.5 text-[12.5px] leading-relaxed text-ink-soft">
        <li>
          <span class="font-medium text-ink">{{ t("env.boundary1.title") }}</span>
          {{ t("env.boundary1.desc") }}
        </li>
        <li>
          <span class="font-medium text-ink">{{ t("env.boundary2.title") }}</span>
          {{ t("env.boundary2.desc") }}
        </li>
        <li>
          <span class="font-medium text-ink">{{ t("env.boundary3.title") }}</span>
          {{ t("env.boundary3.desc") }}
        </li>
        <li>
          <span class="font-medium text-ink">{{ t("env.boundary4.title") }}</span>
          {{ t("env.boundary4.desc") }}
        </li>
      </ul>
    </SectionCard>
  </div>
</template>
