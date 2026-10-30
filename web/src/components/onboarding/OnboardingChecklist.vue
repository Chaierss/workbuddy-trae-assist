<script setup lang="ts">
import { ref } from "vue";
import { RouterLink } from "vue-router";
import { Check, ChevronDown, Sparkles, X } from "lucide-vue-next";
import {
  completeStep,
  completedCount,
  dismissOnboarding,
  items,
  onboardingVisible,
  progressLabel,
  ONBOARDING_STEPS,
  type OnboardingStep,
} from "../../lib/onboarding";
import { scopedPath } from "../../stores/app";
import { t } from "../../lib/i18n";

/**
 * 首启清单：3+1 步，可折叠、可跳过、完成后自动消失。
 *
 * 不阻挡主界面：它只是概览页顶部的一张卡，所有步骤都能从导航里独立完成。
 * 老用户（号池已有账号）不会看到它 —— 判定在 `lib/onboarding.ts` 里完成并持久化。
 */
const collapsed = ref(false);

const total = ONBOARDING_STEPS.length;

function to(raw: string): string {
  return scopedPath(raw);
}

async function markDone(step: OnboardingStep): Promise<void> {
  await completeStep(step);
}
</script>

<template>
  <section
    v-if="onboardingVisible"
    class="rounded-card border border-line bg-surface p-0 shadow-card"
    aria-labelledby="onboarding-title"
  >
    <header class="flex items-center gap-3 border-b border-line-soft px-5 py-3.5">
      <span class="grid size-7 shrink-0 place-items-center rounded-full bg-brand-soft text-brand">
        <Sparkles :size="14" :stroke-width="1.9" />
      </span>
      <div class="min-w-0 flex-1">
        <h2 id="onboarding-title" class="truncate text-body font-semibold text-ink">
          {{ t("onboarding.title") }}
        </h2>
        <p class="truncate text-caption text-muted">{{ progressLabel }}</p>
      </div>

      <!-- 进度条：四段，完成一段填一段；比一个百分比数字更容易一眼看懂。 -->
      <div class="hidden shrink-0 items-center gap-1 sm:flex" :aria-hidden="true">
        <span
          v-for="step in total"
          :key="step"
          class="h-1 w-5 rounded-full"
          :class="step <= completedCount ? 'bg-brand' : 'bg-line'"
        />
      </div>

      <button
        type="button"
        class="shrink-0 rounded p-1 text-muted transition-colors hover:text-ink"
        :aria-label="collapsed ? t('onboarding.expand') : t('onboarding.collapse')"
        @click="collapsed = !collapsed"
      >
        <ChevronDown
          :size="14"
          :stroke-width="2"
          class="transition-transform duration-150"
          :class="collapsed ? '' : 'rotate-180'"
        />
      </button>
      <button
        type="button"
        class="shrink-0 rounded p-1 text-muted transition-colors hover:text-ink"
        :aria-label="t('onboarding.skip')"
        @click="dismissOnboarding"
      >
        <X :size="14" :stroke-width="2" />
      </button>
    </header>

    <ol v-if="!collapsed" class="divide-y divide-line-soft">
      <li
        v-for="(item, index) in items"
        :key="item.step"
        class="flex flex-wrap items-start gap-x-3 gap-y-2 px-5 py-3"
      >
        <span
          class="mt-0.5 grid size-5 shrink-0 place-items-center rounded-full border text-micro font-semibold"
          :class="
            item.done
              ? 'border-brand bg-brand text-white'
              : 'border-line bg-line-soft text-muted'
          "
          :aria-label="item.done ? t('onboarding.done') : ''"
        >
          <Check v-if="item.done" :size="11" :stroke-width="3" />
          <template v-else>{{ index + 1 }}</template>
        </span>

        <div class="min-w-0 flex-1">
          <p class="text-body-sm font-medium" :class="item.done ? 'text-muted line-through' : 'text-ink'">
            {{ item.title }}
          </p>
          <p class="mt-0.5 text-caption leading-relaxed text-muted">{{ item.hint }}</p>
        </div>

        <div v-if="!item.done" class="flex shrink-0 items-center gap-2">
          <span v-if="item.locked" class="text-caption text-muted">
            {{
              item.step === "daily_run"
                ? t("onboarding.dailyRun.later")
                : t("onboarding.retention.later")
            }}
          </span>

          <template v-else>
            <RouterLink
              :to="to(item.to)"
              class="inline-flex items-center rounded-control border border-line px-2.5 py-1.5 text-caption text-ink-soft transition-colors hover:border-line-strong hover:text-ink"
            >
              {{ item.cta }}
            </RouterLink>
            <button
              v-if="item.step === 'retention' || item.step === 'daily_run'"
              type="button"
              class="inline-flex items-center rounded-control px-2.5 py-1.5 text-caption text-brand transition-colors hover:bg-brand-soft"
              @click="markDone(item.step)"
            >
              {{
                item.step === "retention"
                  ? t("onboarding.retention.markDone")
                  : t("onboarding.dailyRun.markDone")
              }}
            </button>
          </template>
        </div>
      </li>
    </ol>
  </section>
</template>
