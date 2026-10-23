<script setup lang="ts">
import { computed, onMounted, ref, type Component } from "vue";
import {
  CalendarClock,
  CalendarCheck,
  HeartPulse,
  Play,
  RotateCcw,
  Send,
  Sparkles,
  PackageCheck,
  ShieldCheck,
  Timer,
  Users,
} from "lucide-vue-next";
import SectionCard from "../../components/SectionCard.vue";
import SelectMenu from "../../components/SelectMenu.vue";
import ToggleSwitch from "../../components/ToggleSwitch.vue";
import AccountScopePicker, { type ScopeAccount } from "./AccountScopePicker.vue";
import { localeTag, t, type MessageKey } from "../../lib/i18n";
import { api, type RotationPolicy, type RotationState, type ScheduleSpec } from "../../api";

/**
 * 设置 → 计划任务：把「保活 / 自动签到」从布尔开关变成**真的会跑**的调度。
 *
 * 每个任务可配：启用、一天多个时点（HH:MM）、抖动半径（0/5/15/30 分钟）、
 * 账号范围（每个任务单独限定；不选任何账号 = 号池全部账号）。抖动在同一天内
 * 是稳定的，所以不会出现「同一档时点来回跑」。错过的时点启动后只补跑最近一个。
 */
/** 任务文案存 key；模块级常量里不能直接 `t()`，否则切语言不会更新。 */
interface TaskMeta {
  titleKey: MessageKey;
  descKey: MessageKey;
  icon: Component;
}

const META: Record<string, TaskMeta> = {
  keepalive: {
    titleKey: "schedule.keepaliveTitle",
    descKey: "schedule.keepaliveDesc",
    icon: HeartPulse,
  },
  checkin: {
    titleKey: "schedule.checkinTitle",
    descKey: "schedule.checkinDesc",
    icon: CalendarCheck,
  },
  activity_run: {
    titleKey: "schedule.activityRunTitle",
    descKey: "schedule.activityRunDesc",
    icon: Sparkles,
  },
  travel_depart: {
    titleKey: "schedule.travelDepartTitle",
    descKey: "schedule.travelDepartDesc",
    icon: Send,
  },
  travel_claim: {
    titleKey: "schedule.travelClaimTitle",
    descKey: "schedule.travelClaimDesc",
    icon: PackageCheck,
  },
  credential_refresh: {
    titleKey: "schedule.credentialTitle",
    descKey: "schedule.credentialDesc",
    icon: ShieldCheck,
  },
};

const JITTER_OPTIONS = [0, 5, 15, 30];
const jitterOptions = computed(() =>
  JITTER_OPTIONS.map((value) => ({
    value,
    label: t("schedule.jitterValue", { value }),
  })),
);

/**
 * 自动轮换（防积分过期）：背景每 15 分钟按策略判一次「切 / 不切」，理由写进台账。
 * **生效边界**：只改后续会话使用的默认账号，不热切当前会话 —— 客户端运行中一律不执行。
 */
type ThresholdField = Extract<
  keyof RotationPolicy,
  | "minUrgencyHours"
  | "cooldownMinutes"
  | "activeGuardMinutes"
  | "minRemainingCredits"
  | "minGapHours"
>;

const THRESHOLDS: {
  field: ThresholdField;
  labelKey: MessageKey;
  unitKey: MessageKey;
  hintKey: MessageKey;
}[] = [
  {
    field: "minUrgencyHours",
    labelKey: "schedule.urgencyLabel",
    unitKey: "schedule.unitHours",
    hintKey: "schedule.urgencyHint",
  },
  {
    field: "cooldownMinutes",
    labelKey: "schedule.cooldownLabel",
    unitKey: "schedule.unitMinutes",
    hintKey: "schedule.cooldownHint",
  },
  {
    field: "activeGuardMinutes",
    labelKey: "schedule.activeGuardLabel",
    unitKey: "schedule.unitMinutes",
    hintKey: "schedule.activeGuardHint",
  },
  {
    field: "minRemainingCredits",
    labelKey: "schedule.minCreditsLabel",
    unitKey: "schedule.unitPoints",
    hintKey: "schedule.minCreditsHint",
  },
  {
    field: "minGapHours",
    labelKey: "schedule.gapLabel",
    unitKey: "schedule.unitHours",
    hintKey: "schedule.gapHint",
  },
];

/** 判定结论来自哪一条规则；与 `domain/rotation.py` 的 `rule` 取值一一对应。 */
const RULE_LABEL: Record<string, MessageKey> = {
  ok: "schedule.ruleOk",
  valid_accounts: "schedule.ruleNoValidAccounts",
  urgency: "schedule.ruleNotUrgent",
  already_target: "schedule.ruleAlreadyTarget",
  cooldown: "schedule.ruleCooldown",
  active_guard: "schedule.activeGuardLabel",
  value: "schedule.ruleLowValue",
  gap: "schedule.ruleGap",
};

/** 取判定结论文案：未知 rule 回落成原始值。 */
function ruleLabel(rule: string): string {
  return RULE_LABEL[rule] ? t(RULE_LABEL[rule]) : rule;
}

const tasks = ref<ScheduleSpec[]>([]);
const accounts = ref<ScopeAccount[]>([]);
const loading = ref(true);
const error = ref("");
const busy = ref<string | null>(null);
const flash = ref<{ key: string; text: string; tone: "ok" | "bad" } | null>(null);

const rotation = ref<RotationState | null>(null);
const rotationError = ref("");
const rotationBusy = ref(false);
const rotationFlash = ref<{ text: string; tone: "ok" | "bad" } | null>(null);

function metaOf(key: string): { title: string; desc: string; icon: Component } {
  const meta = META[key];
  return meta
    ? { title: t(meta.titleKey), desc: t(meta.descKey), icon: meta.icon }
    : { title: key, desc: "", icon: CalendarClock };
}

function formatMoment(value: string | null): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "—";
  return parsed.toLocaleString(localeTag(), {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** 执行日志着色：结果里出现「失败 N（N>0）」或异常时标红，其余保持中性。 */
function logTone(result: string): string {
  if (/失败\s*[1-9]/.test(result) || result.includes("异常")) return "text-danger";
  return "text-ink-soft";
}

/** 与后端 `scheduler.HISTORY_KEEP` 对齐：那里只留最近这么多条，界面直接照实写。 */
const LOG_LIMIT = 7;

async function load(): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    tasks.value = (await api.schedules()).tasks;
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause);
  } finally {
    loading.value = false;
  }
}

function replaceTask(updated: ScheduleSpec): void {
  tasks.value = tasks.value.map((item) => (item.key === updated.key ? updated : item));
}

async function update(
  task: ScheduleSpec,
  patch: Partial<Pick<ScheduleSpec, "enabled" | "times" | "jitterMinutes" | "accountIds">>,
): Promise<void> {
  busy.value = task.key;
  flash.value = null;
  try {
    const result = await api.updateSchedule(task.key, patch);
    replaceTask(result.task);
  } catch (cause) {
    flash.value = {
      key: task.key,
      text: cause instanceof Error ? cause.message : String(cause),
      tone: "bad",
    };
    await load();
  } finally {
    busy.value = null;
  }
}

function toggle(task: ScheduleSpec): void {
  void update(task, { enabled: !task.enabled });
}

function addTime(task: ScheduleSpec): void {
  const candidate = "09:00";
  const times = [...task.times, candidate];
  void update(task, { times });
}

function editTime(task: ScheduleSpec, index: number, value: string): void {
  if (!value) return;
  const times = task.times.map((item, position) => (position === index ? value : item));
  void update(task, { times });
}

function removeTime(task: ScheduleSpec, index: number): void {
  if (task.times.length <= 1) {
    flash.value = { key: task.key, text: t("schedule.keepOneTime"), tone: "bad" };
    return;
  }
  const times = task.times.filter((_item, position) => position !== index);
  void update(task, { times });
}

function setJitter(task: ScheduleSpec, value: number): void {
  void update(task, { jitterMinutes: value });
}

/** 账号范围即时保存；空数组 = 全部账号（后端同语义）。 */
function setScope(task: ScheduleSpec, accountIds: string[]): void {
  void update(task, { accountIds });
}

async function loadAccounts(): Promise<void> {
  try {
    const body = await api.mergedAccounts();
    accounts.value = body.accounts;
  } catch {
    // 号池读不到时选择器留空即可，任务本身的读写不受影响。
    accounts.value = [];
  }
}

async function runNow(task: ScheduleSpec): Promise<void> {
  busy.value = task.key;
  flash.value = null;
  try {
    const result = await api.runScheduleNow(task.key);
    if (result.task) replaceTask(result.task);
    flash.value = { key: task.key, text: result.result, tone: "ok" };
  } catch (cause) {
    flash.value = {
      key: task.key,
      text: cause instanceof Error ? cause.message : String(cause),
      tone: "bad",
    };
  } finally {
    busy.value = null;
  }
}

async function loadRotation(): Promise<void> {
  rotationError.value = "";
  try {
    rotation.value = await api.rotation();
  } catch (cause) {
    rotationError.value = cause instanceof Error ? cause.message : String(cause);
  }
}

async function updateRotation(patch: Partial<RotationPolicy>): Promise<void> {
  rotationBusy.value = true;
  rotationFlash.value = null;
  try {
    rotation.value = await api.updateRotation(patch);
    rotationFlash.value = { text: t("schedule.saved"), tone: "ok" };
  } catch (cause) {
    rotationFlash.value = {
      text: cause instanceof Error ? cause.message : String(cause),
      tone: "bad",
    };
    await loadRotation();
  } finally {
    rotationBusy.value = false;
  }
}

function toggleRotation(): void {
  if (!rotation.value) return;
  void updateRotation({ enabled: !rotation.value.policy.enabled });
}

function setThreshold(field: ThresholdField, value: string): void {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < 0) {
    rotationFlash.value = { text: t("schedule.thresholdInvalid"), tone: "bad" };
    return;
  }
  void updateRotation({ [field]: parsed });
}

async function evaluateRotation(dryRun: boolean): Promise<void> {
  rotationBusy.value = true;
  rotationFlash.value = null;
  try {
    const result = await api.runRotation(dryRun);
    rotation.value = result.state;
    if (result.skipped) {
      rotationFlash.value = { text: result.skipped, tone: "bad" };
    } else if (result.decision) {
      const verdict =
        result.decision.action === "switch"
          ? t("schedule.verdictSwitch")
          : t("schedule.verdictHold");
      rotationFlash.value = {
        text: result.detail
          ? t("schedule.verdictWithDetail", {
              verdict,
              reason: result.decision.reason,
              detail: result.detail,
            })
          : t("schedule.verdict", { verdict, reason: result.decision.reason }),
        tone: "ok",
      };
    }
  } catch (cause) {
    rotationFlash.value = {
      text: cause instanceof Error ? cause.message : String(cause),
      tone: "bad",
    };
    await loadRotation();
  } finally {
    rotationBusy.value = false;
  }
}

onMounted(() => {
  void load();
  void loadAccounts();
  void loadRotation();
});
</script>

<template>
  <div class="space-y-4">
    <SectionCard
      :title="t('schedule.title')"
      :subtitle="t('schedule.subtitle')"
    >
      <p v-if="loading" class="py-6 text-center text-[13px] text-muted">
        {{ t("schedule.loading") }}
      </p>
      <p v-else-if="error" class="py-6 text-center text-[13px] text-rose-500">{{ error }}</p>
      <div v-else class="divide-y divide-line-soft">
        <div v-for="task in tasks" :key="task.key" class="space-y-3 py-4">
          <div class="flex items-start gap-3">
            <span
              class="grid size-9 shrink-0 place-items-center rounded-[10px] bg-brand-soft text-brand-dark"
            >
              <component :is="metaOf(task.key).icon" :size="17" :stroke-width="1.9" />
            </span>
            <div class="min-w-0 flex-1">
              <div class="text-[13px] font-medium text-ink">{{ metaOf(task.key).title }}</div>
              <div class="mt-0.5 text-[12px] text-muted">{{ metaOf(task.key).desc }}</div>
            </div>
            <ToggleSwitch
              class="ml-2"
              :model-value="task.enabled"
              :disabled="busy === task.key"
              @update:model-value="() => toggle(task)"
            />
          </div>

          <div class="ml-12 space-y-2 rounded-[10px] border border-line-soft bg-line-soft/40 p-2.5">
            <div class="flex flex-wrap items-center gap-2">
              <span class="inline-flex items-center gap-1 text-[12px] text-muted">
                <Timer :size="13" :stroke-width="1.9" />{{ t("schedule.times") }}
              </span>
              <span
                v-for="(time, index) in task.times"
                :key="`${task.key}-${index}`"
                class="inline-flex items-center gap-1 rounded-md border border-line-soft bg-surface px-1.5 py-0.5"
              >
                <input
                  type="time"
                  :value="time"
                  class="w-[74px] bg-transparent text-[12px] tabular-nums text-ink outline-none"
                  :disabled="busy === task.key"
                  @change="editTime(task, index, ($event.target as HTMLInputElement).value)"
                />
                <button
                  type="button"
                  class="text-[13px] leading-none text-muted hover:text-rose-500"
                  :disabled="busy === task.key"
                  :title="t('schedule.removeTime')"
                  @click="removeTime(task, index)"
                >
                  ×
                </button>
              </span>
              <button
                type="button"
                class="rounded-md border border-dashed border-line px-1.5 py-0.5 text-[12px] text-muted hover:border-brand hover:text-brand-dark"
                :disabled="busy === task.key"
                @click="addTime(task)"
              >
                {{ t("schedule.addTime") }}
              </button>
              <span class="ml-2 inline-flex items-center gap-1 text-[12px] text-muted">
                {{ t("schedule.jitter") }}
                <SelectMenu
                  :model-value="task.jitterMinutes"
                  :options="jitterOptions"
                  size="xs"
                  :disabled="busy === task.key"
                  class="w-[76px]"
                  @update:model-value="setJitter(task, $event)"
                />
              </span>
              <span class="ml-2 inline-flex items-center gap-2 text-[12px] text-muted">
                <button
                  type="button"
                  class="inline-flex items-center gap-1 rounded-md border border-line px-2 py-0.5 text-[12px] text-ink hover:border-brand hover:text-brand-dark"
                  :disabled="busy === task.key"
                  @click="runNow(task)"
                >
                  <Play :size="12" :stroke-width="2" />{{ t("schedule.runNow") }}
                </button>
              </span>
            </div>

            <!-- 账号范围：不选任何账号 = 号池全部账号（与后端语义一致） -->
            <div class="flex flex-wrap items-center gap-2 border-t border-line-soft pt-2">
              <span class="inline-flex items-center gap-1 text-[12px] text-muted">
                <Users :size="13" :stroke-width="1.9" />{{ t("schedule.scopeLabel") }}
              </span>
              <AccountScopePicker
                :model-value="task.accountIds"
                :accounts="accounts"
                :disabled="busy === task.key"
                class="w-[176px]"
                @update:model-value="setScope(task, $event)"
              />
            </div>

            <div class="space-y-1 border-t border-line-soft pt-2 text-[12px] text-muted">
              <div>
                {{
                  t("schedule.nextRun", {
                    value: task.enabled ? formatMoment(task.nextRunAt) : t("schedule.disabled"),
                  })
                }}
              </div>
              <div
                v-if="flash && flash.key === task.key"
                :class="flash.tone === 'ok' ? 'text-brand-dark' : 'text-rose-500'"
              >
                {{ flash.text }}
              </div>

              <!-- 执行日志：后端只保留最近 7 次（scheduler.HISTORY_KEEP） -->
              <div class="pt-1">
                <div class="mb-1 text-[11.5px] font-medium text-ink-soft">
                  {{ t("schedule.logTitle", { count: LOG_LIMIT }) }}
                </div>
                <div v-if="!task.history.length" class="text-muted">
                  {{ t("schedule.logEmpty") }}
                </div>
                <ul v-else class="space-y-0.5">
                  <li
                    v-for="(entry, index) in [...task.history].reverse()"
                    :key="`${task.key}-log-${index}`"
                    class="flex items-start gap-2"
                  >
                    <span class="shrink-0 tabular-nums text-muted">{{ formatMoment(entry.at) }}</span>
                    <span
                      class="min-w-0 flex-1 truncate"
                      :class="logTone(entry.result)"
                      :title="entry.result"
                    >
                      {{ entry.result }}
                    </span>
                  </li>
                </ul>
              </div>
            </div>
          </div>
        </div>
      </div>
    </SectionCard>

    <SectionCard
      :title="t('schedule.rotationTitle')"
      :subtitle="t('schedule.rotationSubtitle')"
    >
      <p v-if="rotationError" class="py-6 text-center text-[13px] text-rose-500">
        {{ rotationError }}
      </p>
      <div v-else-if="!rotation" class="py-6 text-center text-[13px] text-muted">
        {{ t("schedule.rotationLoading") }}
      </div>
      <div v-else class="space-y-4">
        <div class="flex items-start gap-3">
          <span
            class="grid size-9 shrink-0 place-items-center rounded-[10px] bg-brand-soft text-brand-dark"
          >
            <RotateCcw :size="17" :stroke-width="1.9" />
          </span>
          <div class="min-w-0 flex-1">
            <div class="text-[13px] font-medium text-ink">
              {{ t("schedule.rotationScopeTitle", { scope: rotation.policy.scope }) }}
            </div>
            <div class="mt-0.5 text-[12px] text-muted">
              {{ t("schedule.rotationDesc") }}
            </div>
          </div>
          <ToggleSwitch
            class="ml-2"
            :model-value="rotation.policy.enabled"
            :disabled="rotationBusy"
            @update:model-value="toggleRotation"
          />
        </div>

        <div class="grid grid-cols-1 gap-2 sm:grid-cols-2">
          <label
            v-for="item in THRESHOLDS"
            :key="item.field"
            class="flex items-center justify-between gap-2 rounded-md border border-line-soft px-2 py-1.5"
            :title="t(item.hintKey)"
          >
            <span class="text-[12px] text-muted">{{ t(item.labelKey) }}</span>
            <span class="inline-flex items-center gap-1">
              <input
                type="number"
                min="0"
                step="1"
                :value="rotation.policy[item.field]"
                class="w-[70px] rounded-md border border-line-soft bg-surface px-1.5 py-0.5 text-right text-[12px] tabular-nums text-ink outline-none"
                :disabled="rotationBusy"
                @change="setThreshold(item.field, ($event.target as HTMLInputElement).value)"
              />
              <span class="text-[12px] text-muted">{{ t(item.unitKey) }}</span>
            </span>
          </label>
        </div>

        <div class="flex flex-wrap items-center gap-2">
          <button
            type="button"
            class="inline-flex items-center gap-1 rounded-md border border-line px-2 py-0.5 text-[12px] text-ink hover:border-brand hover:text-brand-dark"
            :disabled="rotationBusy"
            @click="evaluateRotation(false)"
          >
            <Play :size="12" :stroke-width="2" />{{ t("schedule.evaluateNow") }}
          </button>
          <button
            type="button"
            class="rounded-md border border-dashed border-line px-2 py-0.5 text-[12px] text-muted hover:border-brand hover:text-brand-dark"
            :disabled="rotationBusy"
            @click="evaluateRotation(true)"
          >
            {{ t("schedule.dryRun") }}
          </button>
          <span v-if="rotationFlash" :class="rotationFlash.tone === 'ok' ? 'text-brand-dark' : 'text-rose-500'" class="text-[12px]">
            {{ rotationFlash.text }}
          </span>
        </div>

        <div class="space-y-1 text-[12px] text-muted">
          <div>
            {{
              t("schedule.rotationLast", {
                evaluated: formatMoment(rotation.lastEvaluatedAt),
                switched: formatMoment(rotation.lastSwitchAt),
              })
            }}
          </div>
          <div v-if="!rotation.history.length">{{ t("schedule.historyEmpty") }}</div>
          <div v-else class="space-y-1">
            <div
              v-for="(entry, index) in rotation.history.slice(-5).reverse()"
              :key="`${entry.at}-${index}`"
              class="rounded-md border border-line-soft px-2 py-1"
            >
              <span class="tabular-nums">{{ formatMoment(entry.at) }}</span>
              ·
              <span :class="entry.action === 'switch' ? 'text-brand-dark' : ''">
                {{ entry.action === "switch" ? t("schedule.verdictSwitch") : t("schedule.verdictNo") }}
                （{{ ruleLabel(entry.rule) }}）{{ entry.targetName ? `→ ${entry.targetName}` : "" }}
              </span>
              <div class="text-muted">
                {{ entry.reason }}
                <span v-if="entry.executed" class="text-brand-dark">· {{ t("schedule.executed") }}</span>
                <span v-else-if="entry.action === 'switch'">· {{ entry.detail }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </SectionCard>

    <p class="text-[12px] leading-relaxed text-muted">
      {{ t("schedule.footnoteA")
      }}<strong class="font-medium text-ink">{{ t("schedule.footnoteStrong")
      }}</strong>{{ t("schedule.footnoteB") }}
    </p>
  </div>
</template>
