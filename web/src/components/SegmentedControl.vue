<script setup lang="ts">
/**
 * 分段控件：视图切换、平台筛选、时间窗口这类「多选一」都用它。
 *
 * 视觉上是一块浅灰轨道 + 被选中的白色浮起片（阴影来自 --shadow-card），
 * 比「边框盒子里把选中项染灰」更能表达当前选中层级，也不用每个页面
 * 再各写一套 px/py/圆角的魔法数字。
 */
interface Option {
  value: string;
  label: string;
  title?: string;
}

withDefaults(defineProps<{ modelValue: string; options: Option[]; label?: string; size?: "sm" | "md" }>(), {
  size: "md",
});

const emit = defineEmits<{ "update:modelValue": [value: string] }>();
</script>

<template>
  <div
    class="inline-flex items-center gap-0.5 rounded-control border border-line bg-line-soft/60 p-0.5"
    role="tablist"
    :aria-label="label"
  >
    <button
      v-for="option in options"
      :key="option.value"
      type="button"
      role="tab"
      :aria-selected="option.value === modelValue"
      class="shrink-0 rounded-control transition-[background-color,color,box-shadow] duration-150"
      :class="[
        size === 'sm' ? 'px-2 py-1 text-caption' : 'px-2.5 py-1.5 text-body-sm',
        option.value === modelValue
          ? 'bg-surface font-medium text-ink shadow-card'
          : 'text-muted hover:text-ink-soft',
      ]"
      :title="option.title"
      @click="emit('update:modelValue', option.value)"
    >
      {{ option.label }}
    </button>
  </div>
</template>
