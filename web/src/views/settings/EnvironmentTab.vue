<script setup lang="ts">
import { computed, onMounted, ref, type Component } from "vue";
import {
  Database,
  Download,
  EyeOff,
  HardDrive,
  Loader2,
  Lock,
  Monitor,
  ShieldCheck,
  User,
} from "lucide-vue-next";
import SectionCard from "../../components/SectionCard.vue";
import StatusPill from "../../components/StatusPill.vue";
import { clientDetections, health } from "../../stores/app";
import { t, type MessageKey } from "../../lib/i18n";
import { api, type RestoreResult } from "../../api";

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

/** 诊断信息只展示状态与计数，不拉取任何凭据或账号标识。 */
const diag = ref<Record<string, any> | null>(null);
const diagLoading = ref(false);
const diagError = ref("");
const exporting = ref(false);
const restoreResult = ref<RestoreResult | null>(null);

const diagRows = computed(() => {
  const value = diag.value;
  if (!value) return [];
  const database = (value.database ?? {}) as Record<string, unknown>;
  const config = (value.config ?? {}) as Record<string, any>;
  const backups = (value.backups ?? {}) as Record<string, unknown>;
  const app = (value.app ?? {}) as Record<string, unknown>;
  return [
    {
      label: t("env.diagVersion"),
      value: `${app.version ?? "-"} · ${app.activityCatalogVersion ?? "-"}`,
    },
    {
      label: t("env.diagUptime"),
      value: t("env.diagMinutes", {
        minutes: Math.round(Number(app.uptimeSeconds ?? 0) / 60),
      }),
    },
    {
      label: t("env.diagDatabase"),
      value: `${database.quickCheck ?? "-"} · ${t("env.diagAccounts", {
        count: Number(database.accountsCount ?? 0),
      })}`,
    },
    {
      label: t("env.diagConfig"),
      value: t("env.diagCorrupt", {
        count: Array.isArray(config.corruptFiles) ? config.corruptFiles.length : 0,
      }),
    },
    {
      label: t("env.diagBackups"),
      value: t("env.diagBackupCount", {
        global: backups.global ? 1 : 0,
        pre: Number(backups.preMigrateCount ?? 0),
      }),
    },
  ];
});

async function loadDiagnostics(): Promise<void> {
  diagLoading.value = true;
  diagError.value = "";
  try {
    const [status, restore] = await Promise.all([
      api.diagnosticsHealth(),
      api.restoreStatus(),
    ]);
    diag.value = status;
    restoreResult.value = restore.result;
  } catch (error) {
    diagError.value = error instanceof Error ? error.message : String(error);
  } finally {
    diagLoading.value = false;
  }
}

async function exportBundle(): Promise<void> {
  exporting.value = true;
  diagError.value = "";
  try {
    const blob = await api.diagnosticsBundle();
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `chaierww-diagnostics-${new Date()
      .toISOString()
      .slice(0, 10)}.zip`;
    anchor.click();
    URL.revokeObjectURL(url);
  } catch (error) {
    diagError.value = error instanceof Error ? error.message : String(error);
  } finally {
    exporting.value = false;
  }
}

onMounted(() => {
  void loadDiagnostics();
});
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

    <SectionCard :title="t('env.diagTitle')" :subtitle="t('env.diagSubtitle')">
      <div class="space-y-3">
        <div v-if="restoreResult" class="rounded-control border border-line px-3 py-2 text-body-sm">
          <span :class="restoreResult.ok ? 'text-brand-dark' : 'text-danger'">
            {{
              restoreResult.ok
                ? t("env.restoreDone", { id: restoreResult.snapshotId })
                : t("env.restoreFailed", { error: restoreResult.error ?? "" })
            }}
          </span>
        </div>

        <div
          v-if="diagLoading"
          class="h-20 animate-pulse rounded-control bg-line-soft"
          aria-hidden="true"
        />
        <ul v-else-if="diagRows.length" class="divide-y divide-line-soft">
          <li
            v-for="row in diagRows"
            :key="row.label"
            class="flex items-center justify-between gap-3 py-2 text-body-sm first:pt-0 last:pb-0"
          >
            <span class="text-muted">{{ row.label }}</span>
            <span class="min-w-0 truncate font-mono text-ink-soft">{{ row.value }}</span>
          </li>
        </ul>
        <p v-else class="text-body-sm text-muted">{{ t("common.unknown") }}</p>

        <p v-if="diagError" class="text-body-sm text-danger">{{ diagError }}</p>

        <div class="flex flex-wrap items-center gap-2">
          <button
            type="button"
            class="inline-flex items-center gap-1.5 rounded-control border border-line px-3 py-1.5 text-body-sm text-ink-soft hover:border-brand hover:text-brand-dark disabled:opacity-60"
            :disabled="exporting"
            @click="exportBundle"
          >
            <Loader2 v-if="exporting" :size="14" class="animate-spin" />
            <Download v-else :size="14" />
            {{ exporting ? t("env.diagExporting") : t("env.diagExport") }}
          </button>
          <span class="text-caption text-muted">{{ t("env.diagPrivacy") }}</span>
        </div>
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
