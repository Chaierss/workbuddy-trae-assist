<script setup lang="ts">
import { t } from "../../lib/i18n";
import type { KeyLimits } from "./useGateway";

/**
 * 一把 Key 的三项额度：日 token / 日请求 / 每分钟请求（0 = 不限）。
 * 签发与「编辑额度」弹窗共用，避免两处各写一套输入框。
 */
const limits = defineModel<KeyLimits>({ required: true });

const INPUT_CLASS =
  "h-8 w-full rounded-[9px] border border-line bg-surface px-3 text-[12.5px] text-ink " +
  "placeholder:text-muted focus:border-[#d5d8d3] focus:outline-none";
</script>

<template>
  <div>
    <div class="grid gap-2 sm:grid-cols-3">
      <label class="flex flex-col gap-1">
        <span class="text-[11.5px] text-muted">{{ t("gateway.keys.dailyTokens") }}</span>
        <input
          v-model.number="limits.dailyTokenLimit"
          type="number"
          min="0"
          max="10000000"
          :placeholder="t('gateway.keys.unlimited')"
          :class="INPUT_CLASS"
        />
      </label>
      <label class="flex flex-col gap-1">
        <span class="text-[11.5px] text-muted">{{ t("gateway.keys.dailyRequests") }}</span>
        <input
          v-model.number="limits.dailyRequestLimit"
          type="number"
          min="0"
          max="10000000"
          :placeholder="t('gateway.keys.unlimited')"
          :class="INPUT_CLASS"
        />
      </label>
      <label class="flex flex-col gap-1">
        <span class="text-[11.5px] text-muted">{{ t("gateway.keys.rpm") }}</span>
        <input
          v-model.number="limits.rpmLimit"
          type="number"
          min="0"
          max="600000"
          :placeholder="t('gateway.keys.unlimited')"
          :class="INPUT_CLASS"
        />
      </label>
    </div>
    <p class="mt-1 text-[11px] text-muted">{{ t("gateway.keys.limitsHint") }}</p>
  </div>
</template>
