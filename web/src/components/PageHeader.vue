<script setup lang="ts">
import { RouterLink } from "vue-router";

/**
 * 主视图页头：标题 / 副标题 / 右侧动作的唯一写法。
 *
 * 四个主视图此前各写了一遍同样的 `h1 + p + flex 动作区`，间距与字号已经漂移
 * （Usage 的副标题是 `text-body-sm`，其余是 `text-body`）。改设计时只改这一处。
 */
withDefaults(
  defineProps<{
    title: string;
    subtitle?: string;
    /** 二级页的「返回」目标；主视图不传。 */
    backTo?: string;
    backLabel?: string;
  }>(),
  { backLabel: "" },
);
</script>

<template>
  <header class="flex flex-wrap items-start justify-between gap-x-4 gap-y-3">
    <div class="min-w-0">
      <RouterLink
        v-if="backTo"
        :to="backTo"
        class="mb-1 inline-flex items-center gap-1 text-body-sm text-muted transition-colors hover:text-ink"
      >
        ← {{ backLabel }}
      </RouterLink>
      <h1 class="text-display font-semibold tracking-[-0.02em] text-ink">{{ title }}</h1>
      <div v-if="$slots.subtitle" class="mt-1.5 max-w-[68ch] text-body leading-relaxed text-muted">
        <slot name="subtitle" />
      </div>
      <p v-else-if="subtitle" class="mt-1.5 max-w-[68ch] text-body leading-relaxed text-muted">
        {{ subtitle }}
      </p>
    </div>
    <div v-if="$slots.actions" class="flex shrink-0 flex-wrap items-center justify-end gap-2">
      <slot name="actions" />
    </div>
  </header>
</template>
