<script setup lang="ts">
/**
 * `interactive` 只给**整卡可点**的场景用（活动卡、账号卡之外的跳转卡）：
 * hover 时抬 1px + 阴影加深，鼠标移上去就知道能点。
 * 信息型卡片不要开，否则会把「可以点」这件事变成假话。
 */
withDefaults(
  defineProps<{
    title?: string;
    subtitle?: string;
    flush?: boolean;
    interactive?: boolean;
  }>(),
  { interactive: false },
);
</script>

<template>
  <section
    class="overflow-hidden rounded-card border border-line bg-surface shadow-card"
    :class="
      interactive
        ? 'transition-[box-shadow,transform] duration-150 hover:-translate-y-px hover:shadow-card-hover'
        : ''
    "
  >
    <header
      v-if="title || $slots.actions"
      class="flex items-center justify-between gap-4 border-b border-line-soft px-5 py-3.5"
    >
      <div class="min-w-0">
        <h2 class="truncate text-body font-semibold tracking-tight text-ink">{{ title }}</h2>
        <p v-if="subtitle" class="mt-0.5 truncate text-caption text-muted">{{ subtitle }}</p>
      </div>
      <div class="flex shrink-0 items-center gap-2">
        <slot name="actions" />
      </div>
    </header>
    <div :class="flush ? '' : 'px-5 py-4'">
      <slot />
    </div>
  </section>
</template>
