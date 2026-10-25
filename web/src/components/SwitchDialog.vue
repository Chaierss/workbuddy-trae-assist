<script setup lang="ts">
import { computed } from "vue";
import { AlertTriangle, Check, Copy, Info, X } from "lucide-vue-next";
import type { MigrationPreview, SwitchReport } from "../api";
import { t } from "../lib/i18n";

const props = defineProps<{
  open: boolean;
  report: SwitchReport | null;
  busy?: boolean;
  error?: string | null;
  /** 迁移预检结果（确认态展示，只读推演）。 */
  preview?: MigrationPreview | null;
  /** true = 处于「预检确认」态：尚未执行切换，等用户确认。 */
  confirming?: boolean;
}>();

const emit = defineEmits<{ close: []; confirm: [] }>();

/**
 * 切成功了、但凭据本身有先天缺陷（缺 refresh token 之类）。
 * 这类问题不写清楚，用户会在下次 token 过期时以为「切号把登录态弄坏了」。
 */
const warnings = computed(() => props.report?.warnings ?? []);

const title = computed(() => {
  if (props.confirming) return props.busy ? t("switch.title.checking") : t("switch.title.confirm");
  if (props.busy) return t("switch.title.running");
  if (props.error) return t("switch.title.failed");
  if (!props.report) return t("switch.title.default");
  if (props.report.dryRun) return t("switch.title.dryRun");
  return props.report.succeeded ? t("switch.title.success") : t("switch.title.incomplete");
});

const alert = computed<{ tone: "ok" | "warn" | "danger"; text: string } | null>(() => {
  if (props.error) return { tone: "danger", text: props.error };
  const report = props.report;
  if (!report) return null;
  if (report.error) {
    return { tone: report.rollbackFailed ? "danger" : "warn", text: report.error };
  }
  if (report.dryRun) {
    return report.plan?.requiresAppExit
      ? { tone: "warn", text: t("switch.alert.dryRunBusy") }
      : { tone: "ok", text: t("switch.alert.dryRunOk") };
  }
  if (report.succeeded) {
    return { tone: "ok", text: t("switch.alert.success") };
  }
  return null;
});

const files = computed(() => props.report?.plan?.files ?? []);

const migration = computed(() => props.report?.migration ?? null);

/** 迁移摘要：把各块迁移结果拼成可读文案。 */
const migrationSummary = computed(() => {
  const m = migration.value;
  if (!m) return null;
  const parts: string[] = [];
  if (m.memory) {
    if (m.memory.error) parts.push(t("switch.migration.memoryError", { error: m.memory.error }));
    else parts.push(t("switch.migration.memoryMerged", { count: m.memory.appended ?? 0 }));
  }
  if (m.connectors) {
    if (m.connectors.error) parts.push(t("switch.migration.connectorError", { error: m.connectors.error }));
    else if (m.connectors.changed) parts.push(t("switch.migration.connectorAdded", { count: m.connectors.addedKeys ?? 0 }));
    else parts.push(t("switch.migration.connectorNone"));
  }
  if (m.sessions) {
    if (m.sessions.error) parts.push(t("switch.migration.sessionError", { error: m.sessions.error }));
    else if (m.sessions.errorCount) parts.push(t("switch.migration.sessionCopiedPartial", { copied: m.sessions.copiedCount ?? 0, failed: m.sessions.errorCount }));
    else parts.push(t("switch.migration.sessionCopied", { count: m.sessions.copiedCount ?? 0 }));
  }
  if (m.profile && !m.profile.error) {
    parts.push(t("switch.migration.profileMerged", { count: m.profile.mergedCount ?? 0 }));
  }
  if (m.workspaces && !m.workspaces.error) {
    parts.push(t("switch.migration.workspaceMerged", { count: m.workspaces.mergedCount ?? 0 }));
  }
  return parts;
});

/** 预检摘要：确认态展示「本次将复制 / 跳过 / 合并」多少。 */
const previewLines = computed(() => {
  const p = props.preview;
  if (!p || !p.valid) return [];
  const lines: string[] = [];
  if (p.memory) lines.push(t("switch.preview.memory", { append: p.memory.wouldAppend, skip: p.memory.wouldSkip }));
  if (p.connectors && !p.connectors.skipped) {
    lines.push(t("switch.preview.connector", { added: p.connectors.addedKeys, dropped: p.connectors.droppedDupe }));
  }
  if (p.sessions) lines.push(t("switch.preview.session", { copy: p.sessions.copyCount, skip: p.sessions.skipCount }));
  if (p.profile) lines.push(t("switch.preview.profile", { count: p.profile.mergeCount }));
  if (p.workspaces) lines.push(t("switch.preview.workspace", { count: p.workspaces.mergeCount }));
  return lines;
});

const previewNote = computed(() => {
  const p = props.preview;
  if (!p) return null;
  if (!p.valid) return p.reason ?? t("switch.preview.noData");
  if (!previewLines.value.length) return t("switch.preview.nothingToMigrate");
  return null;
});

const duplicateGroups = computed(() => props.preview?.duplicates ?? []);
const duplicateCount = computed(() =>
  duplicateGroups.value.reduce((sum, group) => sum + group.dups.length, 0),
);
</script>

<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="fixed inset-0 z-[var(--z-modal)] grid place-items-center bg-black/25 p-4"
      @click.self="!busy && emit('close')"
    >
      <!-- 遮罩与面板必须压在侧栏（z-200）和下拉（z-900）之上；小窗口下高度按视口
           收住，内容在面板内部滚动，顶/底不会被裁掉。 -->
      <div
        class="flex max-h-[calc(100vh-2rem)] w-full max-w-[min(560px,calc(100vw-2rem))] flex-col rounded-card border border-line bg-surface"
      >
        <header class="flex items-center justify-between gap-3 border-b border-line-soft px-5 py-3.5">
          <h3 class="text-title font-semibold text-ink">{{ title }}</h3>
          <button
            type="button"
            class="grid size-7 place-items-center rounded-control text-muted transition-colors hover:bg-line-soft hover:text-ink"
            :disabled="busy"
            @click="emit('close')"
          >
            <X :size="15" />
          </button>
        </header>

        <div class="flex-1 overflow-y-auto px-5 py-4">
          <div
            v-if="alert"
            class="mb-4 flex items-start gap-2 rounded-control px-3 py-2.5 text-body-sm leading-relaxed"
            :class="{
              'bg-brand-soft text-brand-dark': alert.tone === 'ok',
              'bg-warn-soft text-warn': alert.tone === 'warn',
              'bg-danger-soft text-danger': alert.tone === 'danger',
            }"
          >
            <AlertTriangle :size="14" class="mt-0.5 shrink-0" />
            <span>{{ alert.text }}</span>
          </div>

          <!-- 预检确认态：只读推演，等用户确认后再执行 -->
          <div v-if="confirming">
            <div v-if="busy" class="py-8 text-center text-body text-muted">{{ t("switch.checkingHint") }}</div>
            <template v-else>
              <div
                v-if="previewNote"
                class="mb-3 flex items-start gap-2 rounded-control bg-line-soft px-3 py-2.5 text-body-sm text-muted"
              >
                <Info :size="14" class="mt-0.5 shrink-0" />
                <span>{{ previewNote }}</span>
              </div>
              <div v-else>
                <div class="mb-1.5 text-body-sm font-medium text-ink-soft">{{ t("switch.preview.heading") }}</div>
                <div class="space-y-1">
                  <div
                    v-for="(line, index) in previewLines"
                    :key="index"
                    class="flex items-start gap-2 rounded-control bg-line-soft px-2.5 py-1.5 text-caption text-ink-soft"
                  >
                    <Copy :size="12" class="mt-0.5 shrink-0 text-brand" />
                    <span class="min-w-0 flex-1">{{ line }}</span>
                  </div>
                </div>
              </div>

              <div
                v-if="duplicateCount"
                class="mt-3 flex items-start gap-2 rounded-control bg-warn-soft px-3 py-2.5 text-body-sm text-warn"
              >
                <AlertTriangle :size="14" class="mt-0.5 shrink-0" />
                <span>
                  {{ t("switch.duplicateWarning", { count: duplicateCount }) }}
                </span>
              </div>

              <p class="mt-3 text-body-sm leading-relaxed text-muted">
                {{ t("switch.confirmNote") }}
              </p>
            </template>
          </div>

          <template v-else>
            <div v-if="busy" class="py-8 text-center text-body text-muted">{{ t("switch.processing") }}</div>

            <ol v-else-if="report" class="space-y-0">
              <li
                v-for="(step, index) in report.steps"
                :key="index"
                class="flex items-start gap-2.5 border-b border-line-soft py-2 text-body-sm last:border-b-0"
              >
                <span
                  class="mt-0.5 grid size-4 shrink-0 place-items-center rounded-full"
                  :class="step.ok ? 'bg-brand-soft text-brand' : 'bg-danger-soft text-danger'"
                >
                  <Check v-if="step.ok" :size="10" :stroke-width="3" />
                  <X v-else :size="10" :stroke-width="3" />
                </span>
                <span class="w-[132px] shrink-0 truncate text-ink-soft">{{ step.step }}</span>
                <span class="min-w-0 flex-1 text-muted">{{ step.detail }}</span>
              </li>
            </ol>

            <div v-if="files.length && !busy" class="mt-4">
              <div class="mb-1.5 text-body-sm font-medium text-ink-soft">{{ t("switch.files.heading") }}</div>
              <div class="space-y-1">
                <div
                  v-for="file in files"
                  :key="file.path"
                  class="flex items-center gap-2 rounded-control bg-line-soft px-2.5 py-1.5 text-caption"
                >
                  <span
                    class="shrink-0 rounded px-1.5 py-0.5 font-mono text-micro"
                    :class="
                      file.action === 'override' || file.action === 'create'
                        ? 'bg-warn-soft text-warn'
                        : 'bg-white text-muted'
                    "
                  >
                    {{ file.action }}
                  </span>
                  <span class="min-w-0 flex-1 truncate text-ink-soft" :title="file.path">
                    {{ file.path }}
                  </span>
                  <span class="shrink-0 text-muted">{{ file.size }} B</span>
                </div>
              </div>
            </div>

            <!-- 数据迁移摘要：切换成功后各块的迁移结果 -->
            <div v-if="migration && !busy" class="mt-4">
              <div class="mb-1.5 text-body-sm font-medium text-ink-soft">{{ t("switch.migration.heading") }}</div>
              <div class="space-y-1">
                <div
                  v-for="(line, index) in migrationSummary"
                  :key="index"
                  class="flex items-start gap-2 rounded-control bg-line-soft px-2.5 py-1.5 text-caption text-ink-soft"
                >
                  <span class="mt-0.5 shrink-0 text-brand">✓</span>
                  <span class="min-w-0 flex-1">{{ line }}</span>
                </div>
              </div>
            </div>

            <!-- 切换成功但有隐患：缺 refresh token 的账号，token 过期后要重新扫码 -->
            <div
              v-if="warnings.length && !busy"
              class="mt-4 flex items-start gap-2 rounded-control bg-warn-soft px-3 py-2.5 text-body-sm leading-relaxed text-warn"
            >
              <AlertTriangle :size="14" class="mt-0.5 shrink-0" />
              <div class="min-w-0 flex-1 space-y-1">
                <p v-for="(warning, index) in warnings" :key="index">{{ warning }}</p>
              </div>
            </div>
          </template>
        </div>

        <footer class="flex justify-end gap-2 border-t border-line-soft px-5 py-3">
          <button
            v-if="confirming"
            type="button"
            class="rounded-control border border-line px-3.5 py-2 text-body text-ink-soft transition-colors hover:border-line-strong hover:text-ink"
            :disabled="busy"
            @click="emit('close')"
          >
            {{ t("common.cancel") }}
          </button>
          <button
            v-if="confirming"
            type="button"
            class="rounded-control bg-brand px-4 py-2 text-body font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
            :disabled="busy"
            @click="emit('confirm')"
          >
            {{ t("switch.confirm") }}
          </button>
          <button
            v-if="!confirming"
            type="button"
            class="rounded-control border border-line px-3.5 py-2 text-body text-ink-soft transition-colors hover:border-line-strong hover:text-ink"
            :disabled="busy"
            @click="emit('close')"
          >
            {{ t("common.close") }}
          </button>
        </footer>
      </div>
    </div>
  </Teleport>
</template>
