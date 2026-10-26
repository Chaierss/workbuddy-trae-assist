<script setup lang="ts">
import { computed, ref } from "vue";
import {
  BadgeCheck,
  Check,
  ChevronDown,
  CircleAlert,
  Clock,
  Coins,
  Layers,
  Loader2,
  Pencil,
  RefreshCw,
  Repeat2,
  Sparkles,
  Trash2,
} from "lucide-vue-next";
import StatusPill from "./StatusPill.vue";
import {
  formatAmount,
  formatExpiry,
  formatTime,
  type Account,
  type ActivityBrief,
  type CreditInfo,
  type MigrationPreview,
  type SyncAccountStatus,
} from "../api";
import { t, type MessageKey } from "../lib/i18n";

/** 卡片上的风险筛选口径，与号池页顶部的健康度卡片一致。 */
export type RiskKey = "relogin" | "expiring" | "low";
const LOW_CREDITS = 100;

const props = defineProps<{
  account: Account;
  isCurrent?: boolean;
  switchBlockedReason?: string | null;
  busy?: boolean;
  credit?: CreditInfo | null;
  /** 今日活动余量：null 表示还没拿到检测结果，不显示这一块。 */
  activity?: ActivityBrief | null;
  /** 正在执行这个账号的活动：进度条走流动光。 */
  activityRunning?: boolean;
  /** 刚执行完拿到的积分，几秒后自动消失。 */
  activityReward?: number | null;
  selected?: boolean;
  /** 切换迁移预检结果：null 未拉、loading 拉取中、none 拉取失败。 */
  preview?: MigrationPreview | "loading" | "none" | null;
  /** 会话同步状态：没采过 / 无镜像时不显示徽标。 */
  sync?: SyncAccountStatus | null;
  /** 卡片密度：compact 只留身份 + 主状态，次级信息收进舒适视图。 */
  density?: "compact" | "comfortable";
}>();

const emit = defineEmits<{
  switch: [account: Account];
  remove: [account: Account];
  toggle: [account: Account];
  editNote: [account: Account];
  filter: [risk: RiskKey];
  preview: [account: Account];
  /** 手动触发这个号的凭据续期 / 验活。 */
  refreshCredential: [account: Account];
}>();

const PALETTE = [
  "var(--color-avatar-1)",
  "var(--color-avatar-2)",
  "var(--color-avatar-3)",
  "var(--color-avatar-4)",
  "var(--color-avatar-5)",
  "var(--color-avatar-6)",
  "var(--color-avatar-7)",
  "var(--color-avatar-8)",
];

const avatarColor = computed(() => {
  let hash = 0;
  for (const char of props.account.id) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return PALETTE[hash % PALETTE.length];
});

const initial = computed(() => {
  const name = props.account.displayName.replace(/\s+/g, "");
  return name.slice(0, 1) || "?";
});

const stateMap: Record<string, { textKey: MessageKey; tone: "warn" } | null> = {
  usable: null,
  unverified: null,
  relogin_required: { textKey: "accounts.state.relogin", tone: "warn" },
  unknown: { textKey: "accounts.state.unknown", tone: "warn" },
};

/** 只有异常状态（需重新登录 / 未知）才显示徽标；正常/未切换过不显示。 */
const stateInfo = computed(() => stateMap[props.account.credentialState] ?? null);

/** 积分四态：加载中 / 不可查 / 查询失败 / 正常。 */
const creditState = computed(() => {
  const info = props.credit;
  if (!info) return "loading";
  if (!info.available) return "unavailable";
  if (info.error) return "error";
  return "ok";
});

const creditText = computed(() => {
  const info = props.credit;
  if (!info) return t("accounts.credit.loading");
  if (!info.available) return t("accounts.credit.unavailable");
  if (info.error) return t("accounts.credit.error");
  return formatCredits(info.credits);
});

/** 总积分展示：查得到 total 且 > 0 时显示「剩余 / 总」。 */
const totalText = computed(() => {
  const info = props.credit;
  if (!info?.available || info.error) return "";
  if (info.total === null || info.total === undefined || info.total <= 0) return "";
  return formatCredits(info.total);
});

const creditTooltip = computed(() => {
  const info = props.credit;
  if (!info) return t("accounts.credit.checking");
  if (!info.available) return info.error ?? t("accounts.credit.noCredential");
  if (info.error) return info.error;
  const parts = [
    info.total
      ? t("accounts.credit.remainingOfTotal", { total: formatCredits(info.total) })
      : t("accounts.credit.remaining"),
  ];
  if (info.expiresAt) parts.push(formatExpiry(info.expiresAt));
  if (info.updatedAt) parts.push(t("accounts.credit.updatedAt", { time: formatTime(info.updatedAt) }));
  return parts.join(t("accounts.credit.join"));
});

const platformLabel = computed(() => {
  if (props.account.platform === "trae") return "Trae";
  if (props.account.platform === "workbuddy") return "WorkBuddy";
  return props.account.platform;
});

/** 订阅 / 套餐档位徽标。 */
const subscriptionLabel = computed(() => {
  const info = props.credit;
  if (!info?.available || info.error) return "";
  return info.subscription ?? (info.isPaid ? t("accounts.subscription.paid") : "");
});

/** 积分包明细（有名字、剩余、总额、到期）。 */
const packageRows = computed(() => {
  const info = props.credit;
  if (!info?.available || info.error) return [];
  const packages = info.packages ?? [];
  return packages
    .filter((p) => p.name)
    .slice(0, 8)
    .map((p) => {
      const remaining = p.remaining ?? 0;
      const total = p.total ?? null;
      const ratio = total && total > 0 ? Math.min(remaining / total, 1) : null;
      return {
        name: p.name,
        remaining: formatCredits(remaining),
        total: total !== null ? formatCredits(total) : null,
        expires: p.expiresAt ? formatExpiry(p.expiresAt) : "",
        kind: p.kind,
        ratio,
      };
    });
});

/** 只有查到可用积分、且有到期日时才显示到期徽标。 */
const expiryLabel = computed(() => {
  const info = props.credit;
  if (!info?.available || !info.expiresAt) return "";
  return t("accounts.expiry", { date: formatExpiry(info.expiresAt) });
});

const expiryClasses = computed(() =>
  props.credit?.expiringSoon ? "text-danger" : "text-muted",
);

/** 积分包明细默认折叠，有内容时可展开。 */
const packagesOpen = ref(false);
const hasPackages = computed(() => packageRows.value.length > 0);

/** 余量比例 = 剩余 / 总额；查不到总额时返回 null（不画条，避免拿 0 冒充）。 */
const creditRatio = computed(() => {
  const info = props.credit;
  if (!info?.available || info.error) return null;
  if (!info.total || info.total <= 0 || info.credits === null) return null;
  return Math.max(0, Math.min(info.credits / info.total, 1));
});

/** 余量条的配色档位：<15% 泛红、<40% 琥珀、其余品牌绿。 */
const creditTone = computed<"low" | "mid" | "normal">(() => {
  const ratio = creditRatio.value;
  if (ratio === null) return "normal";
  if (ratio <= 0.15) return "low";
  if (ratio <= 0.4) return "mid";
  return "normal";
});

/** 卡片上的风险徽标：点击即按同一口径筛选号池。 */
const risks = computed<Array<{ key: RiskKey; label: string; tone: string }>>(() => {
  const out: Array<{ key: RiskKey; label: string; tone: string }> = [];
  if (props.account.credentialState !== "usable") {
    out.push({
      key: "relogin",
      label: t("accounts.health.relogin"),
      tone: "border-warn/30 bg-warn-soft text-warn",
    });
  }
  const info = props.credit;
  const usable = info?.available && !info.error;
  if (usable && info.expiringSoon) {
    out.push({
      key: "expiring",
      label: t("accounts.health.expiring"),
      tone: "border-danger/25 bg-danger-soft text-danger",
    });
  }
  if (usable && info.credits !== null && info.credits !== undefined && info.credits < LOW_CREDITS) {
    out.push({
      key: "low",
      label: t("accounts.health.low"),
      tone: "border-warn/30 bg-warn-soft text-warn",
    });
  }
  return out;
});

/** 预检摘要：把「切换会迁移多少」直接摊在卡片上，少点一次对话框。 */
const previewText = computed(() => {
  const preview = props.preview;
  if (!preview) return "";
  if (preview === "loading") return t("accounts.preview.loading");
  if (preview === "none") return t("accounts.preview.failed");
  if (!preview.valid) return preview.reason ?? t("accounts.preview.invalid");
  const parts: string[] = [];
  if (preview.sessions) {
    if (preview.sessions.error) parts.push(t("accounts.preview.sessionError"));
    else parts.push(t("accounts.preview.sessions", { count: preview.sessions.copyCount }));
  }
  if (preview.memory) parts.push(t("accounts.preview.memory", { count: preview.memory.wouldAppend }));
  if (preview.connectors) {
    parts.push(t("accounts.preview.connectors", { count: preview.connectors.addedKeys }));
  }
  return parts.length ? parts.join(" · ") : t("accounts.preview.none");
});

/** 会话同步徽标：没有镜像、没有待应用、没有冲突时整行不占位。 */
const syncLabel = computed(() => {
  const state = props.sync;
  if (!state) return "";
  if (!state.items && !state.pending && !state.conflicts) return "";
  const parts = [t("accounts.sync.mirrored", { count: state.items })];
  if (state.pending) parts.push(t("accounts.sync.pending", { count: state.pending }));
  if (state.conflicts) parts.push(t("accounts.sync.conflicts", { count: state.conflicts }));
  return parts.join(" · ");
});

const syncTooltip = computed(() => {
  const state = props.sync;
  if (!state) return "";
  return state.lastError || t("accounts.sync.tooltip");
});

/** 凭据到期时间：客户端信封凭据没有这个值，整行不显示。 */
const credentialExpiryLabel = computed(() => {
  const iso = props.account.credentialExpiresAt;
  if (!iso) return "";
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  return t("accounts.credential.expiry", { time: formatTime(iso) });
});

const credentialExpired = computed(() => {
  const iso = props.account.credentialExpiresAt;
  if (!iso) return false;
  const at = new Date(iso);
  return !Number.isNaN(at.getTime()) && at.getTime() <= Date.now();
});

/** compact 下收起次级行；凭据已过期、同步冲突这类异常仍然保留。 */
const showSecondary = computed(() => props.density !== "compact");

/** 悬浮卡片时才拉预检，避免进页面就把所有账号的库都打开。 */
function requestPreview(): void {
  if (props.isCurrent) return;
  if (!showSecondary.value) return;
  if (!props.preview) emit("preview", props.account);
}

function formatCredits(value: number | null): string {
  if (value === null) return "—";
  return formatAmount(value);
}
</script>

<template>
  <article
    class="card-halo group relative flex flex-col overflow-hidden rounded-card border bg-surface p-2 transition-all duration-200"
    :class="
      isCurrent
        ? 'card-current border-brand/50'
        : selected
          ? 'card-halo--selected border-brand/35'
          : 'border-line hover:-translate-y-0.5 hover:border-line-strong'
    "
    @mouseenter="requestPreview"
    @focusin="requestPreview"
  >
    <!-- 当前登录：左侧 3px 品牌竖条做定位锚点，配合 .card-current 的 1px 品牌内沿。 -->
    <div
      v-if="isCurrent"
      class="absolute -left-px top-0 h-full w-[3px] bg-brand"
      aria-hidden="true"
    />

    <!-- 头部：勾选 + 头像 + 名称（+ 当前登录标记）+ 平台 / 套餐 -->
    <div class="flex items-start gap-1.5">
      <button
        type="button"
        class="mt-0.5 grid size-[14px] shrink-0 place-items-center rounded-badge border transition-colors"
        :class="
          selected
            ? 'border-brand bg-brand text-white'
            : 'border-line-strong bg-surface text-transparent hover:border-brand'
        "
        :aria-label="selected ? t('accounts.clearSelection') : t('accounts.selectAccount')"
        @click="emit('toggle', account)"
      >
        <Check :size="10" :stroke-width="3" />
      </button>

      <div
        class="grid size-6 shrink-0 place-items-center rounded-full text-micro font-semibold text-white"
        :style="{ backgroundColor: avatarColor }"
      >
        {{ initial }}
      </div>

      <div class="min-w-0 flex-1">
        <div class="flex items-center gap-1">
          <span class="truncate text-body-sm font-semibold leading-tight text-ink">
            {{ account.displayName }}
          </span>
          <template v-if="isCurrent">
            <BadgeCheck
              :size="12"
              :stroke-width="2"
              class="shrink-0 text-brand"
              :aria-label="t('accounts.currentLogin')"
            />
            <span class="shrink-0 text-micro font-medium leading-none text-brand-dark">
              {{ t("accounts.currentLogin") }}
            </span>
          </template>
        </div>
        <div class="mt-[1px] flex items-center gap-1">
          <span
            class="shrink-0 rounded-full bg-line-soft px-1.5 py-[1px] text-micro font-medium leading-none text-ink-soft"
          >
            {{ platformLabel }}
          </span>
          <span
            v-if="subscriptionLabel"
            class="shrink-0 truncate rounded-full bg-brand-soft px-1.5 py-[1px] text-micro font-medium leading-none text-brand-dark"
            :title="subscriptionLabel"
          >
            {{ subscriptionLabel }}
          </span>
        </div>
      </div>
    </div>

    <!-- 备注 + 上次使用压成一行：两块信息都很轻，各占一行太浪费高度 -->
    <button
      type="button"
      class="group/note mt-1 flex min-w-0 items-center gap-1 self-start text-left text-micro leading-none text-muted transition-colors hover:text-ink"
      :title="t('accounts.editNote')"
      @click="emit('editNote', account)"
    >
      <Pencil
        :size="9.5"
        :stroke-width="2"
        class="shrink-0 opacity-0 transition-opacity group-hover/note:opacity-100"
      />
      <span class="truncate">{{ account.note || t("accounts.addNote") }}</span>
      <span
        v-if="account.lastUsedAt"
        class="flex shrink-0 items-center gap-0.5 text-muted/80"
        :title="t('accounts.lastUsed', { time: formatTime(account.lastUsedAt) })"
      >
        <Clock :size="9" :stroke-width="2" />{{ formatTime(account.lastUsedAt) }}
      </span>
    </button>

    <!-- 凭据：到期时间 + 可否自动续期；信封凭据没有过期时间，整行不显示 -->
    <div
      v-if="(credentialExpiryLabel || account.credentialRefreshable) && (showSecondary || credentialExpired)"
      class="mt-1 flex items-center gap-1 text-micro leading-none text-muted"
    >
      <Clock :size="9" :stroke-width="2" class="shrink-0" />
      <span :class="credentialExpired ? 'text-danger' : ''">
        {{ credentialExpiryLabel || t("accounts.credential.noExpiry") }}
      </span>
      <span v-if="account.credentialRefreshable" class="shrink-0 text-brand-dark">
        · {{ t("accounts.credential.auto") }}
      </span>
    </div>

    <!-- 积分：一行数字 + 一行细条。原先把「剩余积分」标签、余量/已用百分比各占一行，
         四行说的其实是同一件事，压掉两行不影响读数。 -->
    <div class="mt-1.5 rounded-control bg-line-soft/45 px-2 py-1" :title="creditTooltip">
      <div class="flex items-baseline gap-1">
        <Coins :size="10.5" :stroke-width="1.9" class="shrink-0 self-center text-muted" />
        <span
          class="text-title font-semibold leading-none tabular-nums transition-colors"
          :class="
            creditState === 'ok'
              ? 'text-ink'
              : creditState === 'error'
                ? 'text-warn'
                : 'text-muted'
          "
        >
          {{ creditText }}
        </span>
        <span v-if="creditState === 'ok'" class="text-micro leading-none text-muted">
          {{ t("accounts.credits") }}
        </span>
        <span v-if="creditState === 'ok' && totalText" class="text-micro leading-none text-muted">
          {{ t("accounts.totalShort", { total: totalText }) }}
        </span>
        <span v-if="expiryLabel" class="ml-auto shrink-0 text-micro leading-none" :class="expiryClasses">
          {{ expiryLabel }}
        </span>
        <CircleAlert
          v-else-if="creditState === 'unavailable' || creditState === 'error'"
          :size="13"
          :stroke-width="1.9"
          class="ml-auto shrink-0 self-center text-muted"
        />
      </div>

      <!-- 余量条：宽度随余量缓动，条上一道循环扫光；低于阈值自动泛红 -->
      <div v-if="creditRatio !== null" class="mt-1 flex items-center gap-1.5">
        <div
          class="credit-track h-1 flex-1 rounded-full bg-line"
          :class="{ 'credit-track--low': creditTone === 'low' }"
        >
          <div
            class="credit-fill h-full rounded-full"
            :class="`credit-fill--${creditTone}`"
            :style="{ width: `${Math.round(creditRatio * 100)}%` }"
          />
        </div>
        <span class="shrink-0 text-micro leading-none tabular-nums text-muted">
          {{ Math.round(creditRatio * 100) }}%
        </span>
      </div>
    </div>

    <!-- 今日活动：一行「活动 + 分段条 + 完成数」；只有真的还能做点什么时才多一行说明 -->
    <div v-if="activity" class="mt-1">
      <div class="flex items-center gap-1.5 text-micro leading-none">
        <span class="flex shrink-0 items-center gap-1 text-muted">
          <Sparkles
            :size="10.5"
            :stroke-width="2"
            class="shrink-0"
            :class="activityRunning ? 'text-brand' : ''"
          />
          {{ t("accounts.activity.title") }}
        </span>
        <!-- 每项活动一个小段：绿=已完成、浅绿=可自动、灰=需手动；执行中待做的段在闪 -->
        <span class="relative flex h-1 flex-1 items-stretch gap-[2px]">
          <span
            v-for="(state, index) in activity.states"
            :key="index"
            class="min-w-[3px] flex-1 rounded-full transition-colors duration-500"
            :class="[
              state === 'done' ? 'bg-brand' : '',
              state === 'auto' ? (activityRunning ? 'activity-seg--running' : 'bg-brand/30') : '',
              state === 'manual' || state === 'unknown' ? 'bg-line-strong' : '',
            ]"
          />
        </span>
        <span
          v-if="activityReward"
          class="reward-pop shrink-0 rounded-full bg-brand-soft px-1.5 py-[1px] text-micro font-medium leading-none text-brand-dark"
        >
          +{{ activityReward }}
        </span>
        <span
          class="shrink-0 tabular-nums"
          :class="activityRunning ? 'text-brand' : 'text-ink-soft'"
        >
          {{ activity.done }}/{{ activity.total }}
        </span>
        <Loader2 v-if="activityRunning" :size="10" class="shrink-0 animate-spin text-brand" />
      </div>
      <!-- 未完成 / 有手动项才占第二行：全绿时 1/1 已经说明问题，不再重复「今日已完成」 -->
      <div
        v-if="activityRunning || activity.automated || activity.manual"
        class="mt-0.5 flex items-center gap-2 text-micro leading-none text-muted"
      >
        <span v-if="activityRunning" class="text-brand">{{ t("accounts.activity.running") }}</span>
        <span v-else-if="activity.automated">
          {{ t("accounts.activity.todo", { count: activity.automated }) }}
        </span>
        <span v-if="activity.manual">{{ t("accounts.activity.manual", { count: activity.manual }) }}</span>
      </div>
    </div>

    <!-- 积分包 / 套餐明细（可展开） -->
    <div v-if="hasPackages && showSecondary" class="mt-0.5">
      <button
        type="button"
        class="flex w-full items-center justify-between rounded-control px-0.5 py-0 text-micro leading-relaxed text-muted transition-colors hover:text-ink-soft"
        @click="packagesOpen = !packagesOpen"
      >
        <span>{{ t("accounts.packages", { count: packageRows.length }) }}</span>
        <ChevronDown
          :size="12"
          :stroke-width="2"
          class="shrink-0 transition-transform"
          :class="packagesOpen ? 'rotate-180' : ''"
        />
      </button>

      <div v-if="packagesOpen" class="mt-1 space-y-1.5 px-1">
        <div
          v-for="row in packageRows"
          :key="row.name"
          class="text-caption"
        >
          <div class="flex items-center justify-between gap-2">
            <span class="min-w-0 truncate text-ink-soft" :title="row.name">
              {{ row.name }}
            </span>
            <span class="flex shrink-0 items-center gap-1.5 tabular-nums">
              <span class="font-medium text-ink-soft">{{ row.remaining }}</span>
              <span v-if="row.total" class="text-muted">/ {{ row.total }}</span>
            </span>
          </div>
          <div class="mt-1 flex items-center gap-2">
            <div class="h-1 flex-1 overflow-hidden rounded-full bg-line-soft">
              <div
                class="h-full rounded-full bg-brand/70"
                :style="{ width: `${Math.round((row.ratio ?? 0) * 100)}%` }"
              />
            </div>
            <span v-if="row.expires" class="shrink-0 text-micro text-muted">
              {{ row.expires }}
            </span>
          </div>
        </div>
      </div>
    </div>

    <!-- 风险徽标：点一下按同类筛号池；无风险时不占位 -->
    <div v-if="risks.length || stateInfo" class="mt-1 flex flex-wrap items-center gap-1">
      <button
        v-for="risk in risks"
        :key="risk.key"
        type="button"
        class="rounded-full border px-1.5 py-[1px] text-micro font-medium leading-none transition-opacity hover:opacity-80"
        :class="risk.tone"
        @click="emit('filter', risk.key)"
      >
        {{ risk.label }}
      </button>
      <StatusPill v-if="stateInfo" :tone="stateInfo.tone">
        {{ t(stateInfo.textKey) }}
      </StatusPill>
    </div>

    <!-- 会话同步：只在有镜像 / 有待应用 / 有冲突时占一行，平时不干扰 -->
    <div
      v-if="syncLabel && (showSecondary || Boolean(sync?.conflicts))"
      class="mt-1 flex items-center gap-1 truncate text-micro leading-none text-muted"
      :title="syncTooltip"
    >
      <Layers :size="9.5" :stroke-width="2" class="shrink-0" />
      <span class="truncate" :class="sync?.conflicts ? 'text-warn' : ''">{{ syncLabel }}</span>
    </div>

    <!-- 切换预检：悬停卡片时按需拉取，直接显示会迁移多少 -->
    <div
      v-if="previewText && showSecondary"
      class="mt-1 flex items-center gap-1 truncate text-micro leading-none text-muted"
      :title="previewText"
    >
      <Loader2 v-if="preview === 'loading'" :size="10" class="shrink-0 animate-spin" />
      <span class="truncate">{{ previewText }}</span>
    </div>

    <!-- 底部操作 -->
    <div class="mt-auto flex items-center gap-1 border-t border-line-soft pt-1">
      <button
        type="button"
        class="inline-flex flex-1 items-center justify-center gap-1 rounded-control px-2 py-[3px] text-caption transition-colors disabled:opacity-50"
        :class="
          switchBlockedReason
            ? 'cursor-not-allowed border border-line text-muted'
            : 'border border-brand/30 bg-brand-soft/50 text-brand-dark hover:bg-brand-soft hover:text-brand'
        "
        :disabled="busy || Boolean(switchBlockedReason)"
        :title="switchBlockedReason ?? t('accounts.switchTo')"
        @click="emit('switch', account)"
      >
        <Repeat2 :size="11" :stroke-width="2" /> {{ t("accounts.switch") }}
      </button>
      <button
        v-if="account.credentialRefreshable"
        type="button"
        class="grid size-[22px] shrink-0 place-items-center rounded-badge text-muted transition-colors hover:bg-brand-soft hover:text-brand-dark disabled:opacity-50"
        :disabled="busy"
        :title="t('accounts.credential.refreshNow')"
        @click="emit('refreshCredential', account)"
      >
        <RefreshCw :size="12" :stroke-width="1.9" />
      </button>
      <button
        type="button"
        class="grid size-[22px] shrink-0 place-items-center rounded-badge text-muted transition-colors hover:bg-danger-soft hover:text-danger disabled:opacity-50"
        :disabled="busy"
        :title="t('common.delete')"
        @click="emit('remove', account)"
      >
        <Trash2 :size="12" :stroke-width="1.9" />
      </button>
    </div>
  </article>
</template>
