import { computed, ref } from "vue";
import {
  api,
  type ActivityHistory,
  type Preferences,
  type ScheduleSpec,
} from "../api";
import { accounts, clientDetections } from "../stores/app";
import { t } from "./i18n";
import { track } from "./metrics";

/**
 * 首启引导：3+1 步的可推进清单。
 *
 * 设计约束（方案 §1.5）：
 * - **不阻挡**：可折叠、可跳过、只出现一次；任何一步都不锁死主界面。
 * - **不打扰老用户**：首次读取时如果号池已有账号，直接视为全部完成并持久化，
 *   清单不会突然出现在用了半年的人眼前。
 * - 步骤状态**尽量从既有信号推导**（客户端检测、号池、`firstSwitchAt`、
 *   计划任务历史、网关 Key 数量、活动执行记录）。完成态不再靠用户手动点，
 *   信号读不到时提供「重试」而不是放行。
 */

export const ONBOARDING_VERSION = 3;

export const ONBOARDING_STEPS = [
  "install",
  "signin",
  "first_switch",
  "retention",
  "daily_run",
] as const;
export type OnboardingStep = (typeof ONBOARDING_STEPS)[number];

export interface OnboardingItem {
  step: OnboardingStep;
  title: string;
  hint: string;
  /** 完成该步骤的主按钮文案。 */
  cta: string;
  /** 站内跳转地址（不带作用域前缀，由调用方补）。 */
  to: string;
  done: boolean;
  /** 还轮不到这一步：只展示说明，不给可点的按钮。 */
  locked?: boolean;
}

/** 当前偏好（只读快照）。组件外的布局与侧栏都可以读它判断是否展示清单。 */
export const onboardingPrefs = ref<Preferences | null>(null);
const loaded = ref(false);
const inflight = ref<Promise<void> | null>(null);

// ---------- 完成态信号（只读，不持久化） ----------
// retention / daily_run 的完成由真实运行信号推导：签到有过历史 / 网关发过 Key /
// 活动执行有过成功记录，才打勾。信号读失败不猜、不放行，只提供重试。
const scheduleTasks = ref<ScheduleSpec[]>([]);
const gatewayKeyCount = ref(0);
const activityOkCount = ref(0);
const signalsLoaded = ref(false);
/** 信号拉取失败：为不完整的一步显示「重试」，避免把「读不到」当成「没做过」。 */
export const signalsFailed = ref(false);
const signalsInflight = ref(false);

const completion = computed<OnboardingStep[]>(() => {
  const saved = (onboardingPrefs.value?.onboardingCompleted ?? []) as string[];
  return ONBOARDING_STEPS.filter((step) => saved.includes(step));
});

export const anyClientInstalled = computed(() =>
  clientDetections.value.some((client) => client.installed),
);

/** 清单是否应该出现在页面上。 */
export const onboardingVisible = computed(() => {
  const prefs = onboardingPrefs.value;
  if (!prefs || prefs.onboardingDismissedAt) return false;
  const done = items.value.filter((item) => item.done).length;
  return done < ONBOARDING_STEPS.length;
});

/** 四个步骤的当前态。`done` 每次由信号重算，避免持久化值与现实打架。 */
export const items = computed<OnboardingItem[]>(() => {
  const prefs = onboardingPrefs.value;
  const saved = new Set(completion.value);
  const hasAccounts = accounts.value.length > 0;
  const installed = anyClientInstalled.value;
  const firstSwitch = Boolean(prefs?.firstSwitchAt);
  const lastThreeDone = installed && hasAccounts && firstSwitch;

  // checkin 默认已启用，用「历史里有记录」判断它跑过，而不是 enabled 字段。
  const checkinRan =
    scheduleTasks.value.some((task) => task.key === "checkin" && task.history.length > 0) ||
    gatewayKeyCount.value > 0;
  const retentionDone = saved.has("retention") || checkinRan;
  const dailyRunDone = saved.has("daily_run") || activityOkCount.value > 0;

  return [
    {
      step: "install",
      title: t("onboarding.install.title"),
      hint: t("onboarding.install.hint"),
      cta: installed ? t("onboarding.install.done") : t("onboarding.install.cta"),
      to: "/accounts",
      done: installed,
    },
    {
      step: "signin",
      title: t("onboarding.signin.title"),
      hint: t("onboarding.signin.hint"),
      cta: hasAccounts ? t("onboarding.signin.done") : t("onboarding.signin.cta"),
      to: "/accounts",
      done: hasAccounts,
    },
    {
      step: "first_switch",
      title: t("onboarding.firstSwitch.title"),
      hint: t("onboarding.firstSwitch.hint"),
      cta: firstSwitch ? t("onboarding.firstSwitch.done") : t("onboarding.firstSwitch.cta"),
      to: "/accounts",
      done: firstSwitch,
    },
    {
      step: "retention",
      title: t("onboarding.retention.title"),
      hint: t("onboarding.retention.hint"),
      cta: retentionDone
        ? t("onboarding.retention.done")
        : t("onboarding.retention.cta"),
      to: "/settings?tab=schedule",
      done: retentionDone,
      // 前 3 步没走完时，这一步只是预告：不显示按钮，避免把用户带去还看不懂的页面。
      locked: !lastThreeDone,
    },
    {
      step: "daily_run",
      title: t("onboarding.dailyRun.title"),
      hint: t("onboarding.dailyRun.hint"),
      cta: dailyRunDone
        ? t("onboarding.dailyRun.done")
        : t("onboarding.dailyRun.cta"),
      to: "/activity",
      done: dailyRunDone,
      // 上一项（auto 签到 / 网关）完成后才解锁：先能自己跑，再谈每天一键。
      locked: !(lastThreeDone && retentionDone),
    },
  ];
});

export const completedCount = computed(() => items.value.filter((item) => item.done).length);
export const progressLabel = computed(() =>
  t("onboarding.progress", {
    done: completedCount.value,
    total: ONBOARDING_STEPS.length,
  }),
);

/** 读一次偏好并按「老用户不打扰」规则做一次初始化；重复调用只发一次请求。 */
export async function ensureOnboarding(): Promise<void> {
  if (loaded.value) return;
  const pending = inflight.value;
  if (pending) return pending;
  const task = (async () => {
    try {
      const prefs = await api.preferences();
      onboardingPrefs.value = prefs;
      if (prefs.onboardingVersion < ONBOARDING_VERSION) {
        const oldUser = accounts.value.length > 0;
        const patch: Partial<Preferences> = {
          onboardingVersion: ONBOARDING_VERSION,
          onboardingCompleted: oldUser ? [...ONBOARDING_STEPS] : [],
        };
        onboardingPrefs.value = await api.savePreferences(patch).catch(() => prefs);
      }
      if (!prefs.onboardingDismissedAt && onboardingVisible.value && completedCount.value === 0) {
        track("onboarding_step", { step: "install", status: "view" });
      }
      loaded.value = true;
      await ensureOnboardingSignals();
    } catch {
      // 偏好读不到不影响主界面：清单干脆不出现，等下次再试。
    } finally {
      inflight.value = null;
    }
  })();
  inflight.value = task;
  return task;
}

/**
 * 拉取「完成态」信号。只在前 3 步已完成且未加载时自动拉；
 * `force=true`（供重试按钮 / 设置保存后调用）则无条件重拉。
 * 任一失败置 `signalsFailed`，其余成功仍采用 —— 不因一个接口失败就整页放弃。
 */
export async function ensureOnboardingSignals(force = false): Promise<void> {
  const prefs = onboardingPrefs.value;
  const firstThreeDone =
    anyClientInstalled.value && accounts.value.length > 0 && Boolean(prefs?.firstSwitchAt);
  const needed = force || (!signalsLoaded.value && firstThreeDone);
  if (!needed || signalsInflight.value) return;
  signalsInflight.value = true;
  signalsFailed.value = false;
  try {
    const [schedules, keys, history] = await Promise.allSettled([
      api.schedules(),
      api.gatewayKeys(),
      api.activityHistory(14),
    ]);
    if (schedules.status === "fulfilled") {
      scheduleTasks.value = schedules.value.tasks ?? [];
    }
    if (keys.status === "fulfilled") {
      gatewayKeyCount.value = (keys.value.keys ?? []).length;
    }
    if (history.status === "fulfilled") {
      activityOkCount.value = sumActivityOk(history.value);
    }
    signalsFailed.value =
      schedules.status === "rejected" || keys.status === "rejected" || history.status === "rejected";
  } finally {
    signalsLoaded.value = true;
    signalsInflight.value = false;
  }
}

/** 强制重拉：设置保存 / 建网关 Key / 活动执行后调用，让清单尽快反映真实进度。 */
export function refreshOnboardingSignals(): void {
  signalsLoaded.value = false;
  void ensureOnboardingSignals(true);
}

function sumActivityOk(history: ActivityHistory): number {
  return (history.series ?? []).reduce(
    (total, series) => total + Number(series.ok ?? 0),
    0,
  );
}

/** 跳过 / 收起：只写一次，之后不再自动出现。 */
export async function dismissOnboarding(): Promise<void> {
  const prefs = onboardingPrefs.value;
  if (!prefs) return;
  track("onboarding_step", { step: "install", status: "skip" });
  onboardingPrefs.value = await api
    .savePreferences({ onboardingDismissedAt: new Date().toISOString() })
    .catch(() => prefs);
}

/** 重新拉一次偏好。切换成功后调用：`firstSwitchAt` 由后端写入，本地快照要同步。 */
export async function refreshOnboarding(): Promise<void> {
  try {
    onboardingPrefs.value = await api.preferences();
    await ensureOnboardingSignals();
  } catch {
    // 拉不到就维持旧快照；清单最多晚一步更新。
  }
}

/** 重新显示引导：清空跳过时间与已完成进度，回到概览即可看到。 */
export async function restartOnboarding(): Promise<void> {
  onboardingPrefs.value = await api.savePreferences({ resetOnboarding: true });
  signalsLoaded.value = false;
  signalsFailed.value = false;
}
