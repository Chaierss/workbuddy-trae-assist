<script setup lang="ts">
import { Inbox } from "lucide-vue-next";
import type { Component } from "vue";

/**
 * 空态：图标 + 一句主文案 + 可选补充说明。
 *
 * 只有一行灰字的空态看起来像「加载失败」；给出图标和原因，用户才知道
 * 是「这里确实没有数据」而不是「页面坏了」。
 */
defineProps<{ icon?: Component; title: string; hint?: string }>();
</script>

<template>
  <div class="flex flex-col items-center gap-2 px-5 py-9 text-center">
    <span
      class="grid size-9 place-items-center rounded-full border border-line bg-line-soft/70 text-muted"
    >
      <component :is="icon ?? Inbox" :size="16" :stroke-width="1.7" />
    </span>
    <p class="text-body-sm font-medium text-ink-soft">{{ title }}</p>
    <p v-if="hint" class="max-w-[380px] text-caption leading-relaxed text-muted">{{ hint }}</p>
  </div>
</template>
