import { computed, ref } from "vue";
import { api, type Preferences } from "../api";
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
 * - 步骤状态**尽量从既有信号推导**（客户端检测、号池、`firstSwitchAt`），
 *   只有 `retention`（开自动签到 / 签发网关 Key）与 `daily_run`（试过一次一键执行）
 *   需要用户点一下才算完成。
 */

export const ONBOARDING_VERSION = 2;

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
      cta: saved.has("retention")
        ? t("onboarding.retention.done")
        : t("onboarding.retention.cta"),
      to: "/settings",
      done: saved.has("retention"),
      // 前 3 步没走完时，这一步只是预告：不显示按钮，避免把用户带去还看不懂的页面。
      locked: !lastThreeDone,
    },
    {
      step: "daily_run",
      title: t("onboarding.dailyRun.title"),
      hint: t("onboarding.dailyRun.hint"),
      cta: saved.has("daily_run")
        ? t("onboarding.dailyRun.done")
        : t("onboarding.dailyRun.cta"),
      to: "/activity",
      done: saved.has("daily_run"),
      // 上一项（auto 签到 / 网关）完成后才解锁：先能自己跑，再谈每天一键。
      locked: !(lastThreeDone && saved.has("retention")),
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
  if (inflight.value) return inflight.value;
  inflight.value = (async () => {
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
      if (prefs.onboardingDismissedAt || !onboardingVisible.value) {
        // 无事可做。
      } else if (completedCount.value === 0) {
        track("onboarding_step", { step: "install", status: "view" });
      }
      loaded.value = true;
    } catch {
      // 偏好读不到不影响主界面：清单干脆不出现，等下次再试。
    } finally {
      inflight.value = null;
    }
  })();
  return inflight.value;
}

/** 记录一步完成。`retention` 只能由用户点按钮完成。 */
export async function completeStep(step: OnboardingStep): Promise<void> {
  const prefs = onboardingPrefs.value;
  if (!prefs) return;
  const next = Array.from(new Set([...prefs.onboardingCompleted, step]));
  track("onboarding_step", { step, status: "complete" });
  onboardingPrefs.value = await api
    .savePreferences({ onboardingCompleted: next })
    .catch(() => ({ ...prefs, onboardingCompleted: next }));
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
  } catch {
    // 拉不到就维持旧快照；清单最多晚一步更新。
  }
}
