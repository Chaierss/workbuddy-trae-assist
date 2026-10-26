<script setup lang="ts">
/**
 * 骨架：加载态必须与最终布局同形，否则首屏会「跳一下」。
 *
 * 三个形态覆盖现有页面：
 * - `text`：段落（标题 + N 行）
 * - `card`：账号卡 / 活动卡（头图块 + 两行）
 * - `row`：列表行 / 表格行（左侧两列 + 右侧一个短块）
 *
 * 纯装饰，对读屏隐藏；页面的加载语义由父级 `role="status"` 或文案承担。
 */
withDefaults(
  defineProps<{
    variant?: "text" | "card" | "row";
    lines?: number;
  }>(),
  { variant: "text", lines: 3 },
);
</script>

<template>
  <div aria-hidden="true">
    <div v-if="variant === 'card'" class="rounded-card border border-line bg-surface p-4">
      <div class="flex items-center gap-3">
        <div class="size-9 animate-pulse rounded-full bg-line-soft" />
        <div class="min-w-0 flex-1 space-y-2">
          <div class="h-3 w-1/3 animate-pulse rounded bg-line-soft" />
          <div class="h-2.5 w-1/2 animate-pulse rounded bg-line-soft" />
        </div>
      </div>
      <div class="mt-4 h-2.5 w-4/5 animate-pulse rounded bg-line-soft" />
    </div>

    <div v-else-if="variant === 'row'" class="flex items-center gap-3 px-5 py-3">
      <div class="h-3 w-16 animate-pulse rounded bg-line-soft" />
      <div class="h-3 w-24 animate-pulse rounded bg-line-soft" />
      <div class="h-3 flex-1 animate-pulse rounded bg-line-soft" />
      <div class="h-3 w-10 animate-pulse rounded bg-line-soft" />
    </div>

    <div v-else class="space-y-2">
      <div class="h-3 w-1/4 animate-pulse rounded bg-line-soft" />
      <div
        v-for="line in lines"
        :key="line"
        class="h-2.5 animate-pulse rounded bg-line-soft"
        :class="line === lines ? 'w-3/5' : 'w-full'"
      />
    </div>
  </div>
</template>
