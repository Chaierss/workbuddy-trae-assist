<script setup lang="ts">
import { computed, type Component } from "vue";
import { Database, EyeOff, HardDrive, Lock, Monitor, ShieldCheck, User } from "lucide-vue-next";
import SectionCard from "../../components/SectionCard.vue";
import StatusPill from "../../components/StatusPill.vue";
import { clientDetections, health } from "../../stores/app";
import { t, type MessageKey } from "../../lib/i18n";

const rows = computed(() => [
  { label: t("env.dataDir"), value: health.value?.dataDir ?? t("common.unknown"), icon: Database },
  { label: t("env.serverUrl"), value: window.location.origin, icon: ShieldCheck },
]);

interface BoundaryItem {
  titleKey: MessageKey;
  descKey: MessageKey;
  icon: Component;
}

const boundaries: BoundaryItem[] = [
  { titleKey: "env.boundary1.title", descKey: "env.boundary1.desc", icon: Monitor },
  { titleKey: "env.boundary2.title", descKey: "env.boundary2.desc", icon: User },
  { titleKey: "env.boundary3.title", descKey: "env.boundary3.desc", icon: Lock },
  { titleKey: "env.boundary4.title", descKey: "env.boundary4.desc", icon: EyeOff },
];

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
          <span class="w-[92px] shrink-0 text-body-sm text-muted">{{ row.label }}</span>
          <span class="min-w-0 flex-1 break-all font-mono text-body-sm text-ink-soft">
            {{ row.value }}
          </span>
        </li>
        <li class="flex items-start gap-3 py-3">
          <HardDrive :size="15" class="mt-0.5 shrink-0 text-muted" />
          <span class="w-[92px] shrink-0 text-body-sm text-muted">{{ t("env.stateDir") }}</span>
          <div class="min-w-0 flex-1 space-y-1.5">
            <div
              v-for="client in clientDetections"
              :key="client.scope"
              class="break-all font-mono text-caption text-ink-soft"
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
      <div class="space-y-2.5 text-body-sm">
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
      <ul class="space-y-3">
        <li v-for="item in boundaries" :key="item.titleKey" class="flex items-start gap-3">
          <span
            class="mt-0.5 grid size-8 shrink-0 place-items-center rounded-control bg-brand-soft text-brand-dark"
          >
            <component :is="item.icon" :size="15" :stroke-width="1.9" />
          </span>
          <div class="min-w-0 text-body-sm leading-relaxed">
            <div class="font-medium text-ink">{{ t(item.titleKey) }}</div>
            <div class="text-muted">{{ t(item.descKey) }}</div>
          </div>
        </li>
      </ul>
    </SectionCard>
  </div>
</template>
