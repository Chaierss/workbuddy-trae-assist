<script setup lang="ts">
import { computed } from "vue";
import { Check, Copy, KeyRound, Pencil, SlidersHorizontal, X } from "lucide-vue-next";
import ConfirmDialog from "../../components/ConfirmDialog.vue";
import SectionCard from "../../components/SectionCard.vue";
import StatusPill from "../../components/StatusPill.vue";
import { formatTime, type GatewayKey } from "../../api";
import { t } from "../../lib/i18n";
import ModelScopePicker from "./ModelScopePicker.vue";
import { compact, useGateway } from "./useGateway";

/** Key 管理：签发（三段式模型范围）、改范围（无需重签）、吊销 / 恢复 / 删除、用量。 */
const store = useGateway();

const catalogModels = computed(() => store.status?.catalog.models ?? []);

/** 一把 Key 的范围摘要：「全部」/「能力：免费 + 图片」/「指定 3 个」。 */
type ScopeKey = "all" | "capability" | "list";
type CapKey = "free" | "image" | "long_context";

const SCOPE_BADGE: Record<
  ScopeKey,
  "gateway.keys.badge.all" | "gateway.keys.badge.capability" | "gateway.keys.badge.list"
> = {
  all: "gateway.keys.badge.all",
  capability: "gateway.keys.badge.capability",
  list: "gateway.keys.badge.list",
};

const CAP_LABEL: Record<
  CapKey,
  "gateway.scope.cap.free" | "gateway.scope.cap.image" | "gateway.scope.cap.long_context"
> = {
  free: "gateway.scope.cap.free",
  image: "gateway.scope.cap.image",
  long_context: "gateway.scope.cap.long_context",
};

function scopeBadge(key: GatewayKey): string {
  return t(SCOPE_BADGE[key.scope as ScopeKey] ?? SCOPE_BADGE.all);
}

function capLabel(capability: string): string {
  return t(CAP_LABEL[capability as CapKey] ?? CAP_LABEL.free);
}

function scopeSummary(key: GatewayKey): string {
  if (key.scope === "capability") {
    const caps = key.capabilities.map(capLabel).join(" + ");
    return caps ? t("gateway.keys.summaryCapability", { caps }) : t("gateway.keys.summaryNone");
  }
  if (key.scope === "list") {
    return key.models.length
      ? t("gateway.keys.summaryList", { count: key.models.length })
      : t("gateway.keys.summaryNone");
  }
  return t("gateway.keys.summaryAll");
}

/** 确认弹窗按当前动作切换文案与语气：吊销 / 恢复 / 删除 三态各不相同。 */
const pendingAction = computed(() => store.pending?.action ?? null);
const confirmTone = computed(() => (pendingAction.value === "delete" ? "danger" : "brand"));
const confirmTitle = computed(() => {
  if (pendingAction.value === "revoke") return t("gateway.keys.confirmRevokeTitle");
  if (pendingAction.value === "restore") return t("gateway.keys.confirmRestoreTitle");
  if (pendingAction.value === "delete") return t("gateway.keys.confirmDeleteTitle");
  return "";
});
const confirmText = computed(() => {
  if (pendingAction.value === "restore") return t("gateway.keys.restore");
  return pendingAction.value === "delete" ? t("common.delete") : t("gateway.keys.revoke");
});
const confirmMessage = computed(() => {
  const label = store.pending?.keyItem.label ?? "";
  if (pendingAction.value === "revoke") {
    return t("gateway.keys.confirmRevokeMessage", { label });
  }
  if (pendingAction.value === "restore") {
    return t("gateway.keys.confirmRestoreMessage", { label });
  }
  return t("gateway.keys.confirmDeleteMessage", { label });
});
</script>

<template>
  <div>
    <SectionCard
      :title="t('gateway.keys.title')"
      :subtitle="t('gateway.keys.subtitle')"
      :flush="false"
    >
      <template #actions>
        <input
          v-model="store.newLabel"
          type="text"
          :placeholder="t('gateway.keys.labelPlaceholder')"
          class="h-8 w-40 rounded-[9px] border border-line bg-surface px-3 text-[12.5px] text-ink placeholder:text-muted focus:border-[#d5d8d3] focus:outline-none"
        />
        <button
          type="button"
          class="inline-flex items-center gap-1.5 rounded-[9px] bg-brand px-3 py-2 text-[12.5px] font-medium text-white transition-colors hover:bg-brand-dark disabled:opacity-60"
          :disabled="store.busyKey === 'new'"
          @click="store.issue"
        >
          <KeyRound :size="14" :stroke-width="1.9" />
          {{ t("gateway.keys.issue") }}
        </button>
      </template>

      <!-- 新 Key 的模型范围：三段式，默认「全部」（目录新上的模型自动可用） -->
      <div class="mb-3">
        <div class="mb-1.5 flex items-center gap-2">
          <SlidersHorizontal :size="14" :stroke-width="1.9" class="text-muted" />
          <span class="text-[12px] font-medium text-ink">{{ t("gateway.keys.limitModels") }}</span>
        </div>
        <ModelScopePicker
          v-model:scope="store.newScope"
          v-model:capabilities="store.newCapabilities"
          v-model:models="store.newModels"
          :catalog="catalogModels"
          @refresh="store.refreshModels"
        />
      </div>

      <div v-if="store.issued" class="mb-3 rounded-[10px] border border-brand/30 bg-brand-soft px-3.5 py-3">
        <div class="flex items-center justify-between gap-2">
          <span class="text-[12px] font-medium text-brand-dark">{{ t("gateway.keys.issuedTitle") }}</span>
          <div class="flex items-center gap-1">
            <button
              type="button"
              class="inline-flex items-center gap-1 rounded-[7px] px-2 py-1 text-[11.5px] text-brand-dark transition-colors hover:bg-white/70 dark:hover:bg-white/10"
              @click="store.copy(store.issued, 'issued')"
            >
              <component :is="store.copied === 'issued' ? Check : Copy" :size="13" :stroke-width="1.9" />
              {{ store.copied === "issued" ? t("common.copied") : t("common.copy") }}
            </button>
            <button
              type="button"
              class="rounded-[7px] px-2 py-1 text-[11.5px] text-muted transition-colors hover:text-ink"
              @click="store.issued = null"
            >
              {{ t("common.close") }}
            </button>
          </div>
        </div>
        <div class="mt-1.5 break-all font-mono text-[12px] text-brand-dark">{{ store.issued }}</div>
        <div class="mt-1 text-[11px] text-muted">{{ t("gateway.keys.issuedFootnote") }}</div>
      </div>

      <div v-if="!store.keys.length" class="py-6 text-center text-[12.5px] text-muted">
        {{ t("gateway.keys.empty") }}
      </div>

      <ul v-else class="divide-y divide-line-soft">
        <li
          v-for="key in store.keys"
          :key="key.id"
          class="group/row flex flex-wrap items-center gap-3 py-3 text-[12.5px]"
        >
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-2">
              <!-- 行内改名：回车保存、Esc 取消；只改备注，Key 与明文不动 -->
              <template v-if="store.renaming === key.id">
                <input
                  v-model="store.renameValue"
                  autofocus
                  type="text"
                  maxlength="60"
                  :aria-label="t('gateway.keys.rename')"
                  class="h-7 w-44 rounded-[8px] border border-line bg-surface px-2 text-[12.5px] text-ink focus:border-[#d5d8d3] focus:outline-none"
                  @keydown.enter.prevent="store.saveKeyLabel"
                  @keydown.esc="store.cancelRename"
                />
                <button
                  type="button"
                  class="grid size-6 place-items-center rounded-[7px] text-brand transition-colors hover:bg-brand-soft disabled:opacity-50"
                  :disabled="store.busyKey === key.id"
                  :title="t('common.save')"
                  @click="store.saveKeyLabel"
                >
                  <Check :size="13" :stroke-width="2.2" />
                </button>
                <button
                  type="button"
                  class="grid size-6 place-items-center rounded-[7px] text-muted transition-colors hover:bg-line-soft hover:text-ink"
                  :title="t('common.cancel')"
                  @click="store.cancelRename"
                >
                  <X :size="13" :stroke-width="2" />
                </button>
              </template>
              <template v-else>
                <span
                  class="max-w-[220px] cursor-text truncate font-medium"
                  :class="key.active ? 'text-ink' : 'text-muted'"
                  :title="t('gateway.keys.renameHint')"
                  @dblclick="store.beginRename(key)"
                >
                  {{ key.label }}
                </span>
                <button
                  type="button"
                  class="grid size-6 shrink-0 place-items-center rounded-[7px] text-muted opacity-0 transition-opacity group-hover/row:opacity-100 hover:text-ink focus-visible:opacity-100"
                  :title="t('gateway.keys.rename')"
                  :aria-label="t('gateway.keys.rename')"
                  @click="store.beginRename(key)"
                >
                  <Pencil :size="11.5" :stroke-width="1.9" />
                </button>
              </template>
              <StatusPill :tone="key.active ? 'brand' : 'neutral'">
                {{ key.active ? t("gateway.keys.active") : t("gateway.keys.revoked") }}
              </StatusPill>
              <StatusPill :tone="key.scope === 'all' ? 'neutral' : 'info'">
                {{ scopeBadge(key) }}
              </StatusPill>
            </div>
            <div class="mt-0.5 truncate font-mono text-[11.5px] text-muted">{{ key.mask }}</div>
            <div class="mt-0.5 truncate text-[11px] text-muted" :title="scopeSummary(key)">
              {{ t("gateway.keys.availableModels", { models: scopeSummary(key) }) }}
            </div>
          </div>
          <div class="shrink-0 text-right text-[11.5px] text-muted">
            <div>
              {{ t("gateway.keys.usageRow", { requests: key.requests, tokens: compact(key.tokens) }) }}
            </div>
            <div>{{ t("gateway.keys.lastUsed", { time: formatTime(key.lastUsedAt) }) }}</div>
          </div>
          <button
            type="button"
            class="shrink-0 rounded-[9px] border border-line px-2.5 py-1.5 text-[11.5px] text-ink-soft transition-colors hover:border-brand/50 hover:text-brand-dark disabled:opacity-60"
            :disabled="store.keyUsageBusy === key.id"
            @click="store.openKeyUsage(key)"
          >
            {{ t("gateway.keys.viewUsage") }}
          </button>
          <button
            type="button"
            class="shrink-0 rounded-[9px] border border-line px-2.5 py-1.5 text-[11.5px] text-ink-soft transition-colors hover:border-brand/50 hover:text-brand-dark disabled:opacity-60"
            :disabled="store.busyKey === key.id"
            @click="store.beginEditScope(key)"
          >
            {{ t("gateway.keys.editScope") }}
          </button>
          <button
            v-if="key.active"
            type="button"
            class="shrink-0 rounded-[9px] border border-line px-2.5 py-1.5 text-[11.5px] text-ink-soft transition-colors hover:border-warn/50 hover:text-warn disabled:opacity-60"
            :disabled="store.busyKey === key.id"
            @click="store.pending = { keyItem: key, action: 'revoke' }"
          >
            {{ t("gateway.keys.revoke") }}
          </button>
          <template v-else>
            <button
              type="button"
              class="shrink-0 rounded-[9px] border border-line px-2.5 py-1.5 text-[11.5px] text-ink-soft transition-colors hover:border-brand/50 hover:text-brand-dark disabled:opacity-60"
              :disabled="store.busyKey === key.id"
              @click="store.pending = { keyItem: key, action: 'restore' }"
            >
              {{ t("gateway.keys.restore") }}
            </button>
            <button
              type="button"
              class="shrink-0 rounded-[9px] border border-line px-2.5 py-1.5 text-[11.5px] text-muted transition-colors hover:border-danger/50 hover:text-danger disabled:opacity-60"
              :disabled="store.busyKey === key.id"
              @click="store.pending = { keyItem: key, action: 'delete' }"
            >
              {{ t("common.delete") }}
            </button>
          </template>
        </li>
      </ul>
    </SectionCard>

    <ConfirmDialog
      :open="store.pending !== null"
      :tone="confirmTone"
      :title="confirmTitle"
      :message="confirmMessage"
      :confirm-text="confirmText"
      :busy="store.pending !== null && store.busyKey === store.pending.keyItem.id"
      @cancel="store.pending = null"
      @confirm="store.confirmKeyAction"
    />

    <!-- 编辑模型范围：只改范围，Key 不变，所以客户端无需换配置 -->
    <Teleport to="body">
      <div
        v-if="store.editing"
        class="fixed inset-0 z-[400] grid place-items-center bg-black/25 p-4"
        @click.self="store.editing = null"
      >
        <div class="w-full max-w-[560px] rounded-[16px] border border-line bg-surface p-5">
          <h3 class="text-[15px] font-semibold text-ink">{{ t("gateway.keys.editScopeTitle") }}</h3>
          <p class="mt-1 text-[12px] text-muted">
            {{ t("gateway.keys.editScopeDesc", { label: store.editing.keyItem.label }) }}
          </p>
          <div class="mt-3">
            <ModelScopePicker
              v-model:scope="store.editing.scope"
              v-model:capabilities="store.editing.capabilities"
              v-model:models="store.editing.models"
              :catalog="catalogModels"
              @refresh="store.refreshModels"
            />
          </div>
          <div class="mt-4 flex justify-end gap-2">
            <button
              type="button"
              class="rounded-[9px] border border-line px-3.5 py-2 text-[13px] text-ink-soft transition-colors hover:border-[#d5d8d3] hover:text-ink"
              @click="store.editing = null"
            >
              {{ t("common.cancel") }}
            </button>
            <button
              type="button"
              class="rounded-[9px] bg-brand px-3.5 py-2 text-[13px] font-medium text-white transition-colors hover:bg-brand-dark disabled:opacity-60"
              :disabled="store.busyKey === store.editing.keyItem.id"
              @click="store.saveKeyScope"
            >
              {{ t("common.save") }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>

  </div>
</template>
