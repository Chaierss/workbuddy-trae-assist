<script setup lang="ts">
import { Inbox } from "lucide-vue-next";
import type { Component } from "vue";

/**
 * 空态：图标 + 一句主文案 + 可选补充说明 + **一个明确的下一步**。
 *
 * 只有一行灰字的空态看起来像「加载失败」；给出图标、原因和主按钮，用户才知道
 * 是「这里确实没有数据」以及接下来该点什么。`compact` 用于卡片内的局部空态。
 */
withDefaults(
  defineProps<{
    icon?: Component;
    title: string;
    hint?: string;
    compact?: boolean;
  }>(),
  { compact: false },
);
</script>

<template>
  <div
    class="flex flex-col items-center gap-2 text-center"
    :class="compact ? 'px-3 py-5' : 'px-5 py-9'"
  >
    <span
      class="grid place-items-center rounded-full border border-line bg-line-soft/70 text-muted"
      :class="compact ? 'size-7' : 'size-9'"
    >
      <component :is="icon ?? Inbox" :size="compact ? 14 : 16" :stroke-width="1.7" />
    </span>
    <p class="text-body-sm font-medium text-ink-soft">{{ title }}</p>
    <p v-if="hint" class="max-w-[380px] text-caption leading-relaxed text-muted">{{ hint }}</p>
    <div v-if="$slots.action" class="mt-1 flex flex-wrap items-center justify-center gap-2">
      <slot name="action" />
    </div>
  </div>
</template>
