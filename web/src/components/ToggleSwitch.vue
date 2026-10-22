<script setup lang="ts">
/**
 * 统一开关：替代各页手写的 `h-6 w-10` 按钮 + 内部圆点。
 *
 * 之前同一开关在常规 / 客户端设置 / 计划任务 / 备份里复制了四遍，圆点位移
 * 还不一致（22px / 19px）。这里收敛成一份：绝对定位、左右各留 3px、位移 19px，
 * 关/开两态完全对称。
 */
withDefaults(
  defineProps<{
    modelValue: boolean;
    disabled?: boolean;
  }>(),
  { disabled: false },
);

const emit = defineEmits<{ "update:modelValue": [value: boolean] }>();
</script>

<template>
  <button
    type="button"
    role="switch"
    :aria-checked="modelValue"
    :disabled="disabled"
    class="relative h-6 w-10 shrink-0 rounded-full transition-colors disabled:cursor-not-allowed disabled:opacity-50"
    :class="modelValue ? 'bg-brand' : 'bg-line'"
    @click="emit('update:modelValue', !modelValue)"
  >
    <span
      class="absolute left-0 top-[3px] block size-4.5 rounded-full bg-white shadow-sm transition-transform duration-200"
      :class="modelValue ? 'translate-x-[19px]' : 'translate-x-[3px]'"
    />
  </button>
</template>
