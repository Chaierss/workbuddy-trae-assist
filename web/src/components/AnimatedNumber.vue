<script setup lang="ts">
/**
 * 会滚动的数字。
 *
 * 只做一件事：`value` 变了就从中途滚到新值，并在滚动时轻轻跳一下。
 * 用在号池顶部的健康度卡片上 —— 数据是异步到的，直接「啪」一下换成新数字
 * 会让人分不清是刚更新还是看错了；滚一下既提示「这里变了」，又不打断视线。
 *
 * 三个刻意的取舍：
 * - 取整输出：这些计数没有小数，滚出 3.7 个账号只会让人出戏；
 * - `prefers-reduced-motion` 下直接落到目标值，不做位移和缩放；
 * - 用 `Math.round` + `tabular-nums`，滚动过程中宽度不抖。
 *
 * 时长压在 180ms：规则页那条「优雅设计」写的是动效不超过 200ms，620ms 的滚动
 * 会让进页时整块数字在眼前「爬」很久，比它想提示的信息更抢眼。
 */
import { onBeforeUnmount, onMounted, ref, watch } from "vue";

const props = withDefaults(
  defineProps<{ value: number; duration?: number; delay?: number }>(),
  {
    duration: 180,
    delay: 0,
  },
);

// 从 0 起算：号池数据多半在挂载前就到了，若以上来就是终值，整块进场会像静态图。
const shown = ref(0);
const bumped = ref(false);

let frame = 0;
let bumpTimer = 0;
let startTimer = 0;

function reduced(): boolean {
  return (
    typeof window !== "undefined" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

function tween(from: number, to: number): void {
  cancelAnimationFrame(frame);
  if (reduced() || from === to) {
    shown.value = to;
    return;
  }
  const started = performance.now();
  const step = (now: number): void => {
    // rAF 的时间戳是「这一帧的开始时刻」，可能比注册时的 performance.now() 还早，
    // 不夹一下会算出负的 t，小数字上就闪出一个 -1。
    const t = Math.min(Math.max((now - started) / props.duration, 0), 1);
    // easeOutCubic：起手快、收尾慢，读数不会在最后一段「爬」太久
    const eased = 1 - (1 - t) ** 3;
    shown.value = Math.round(from + (to - from) * eased);
    if (t < 1) frame = requestAnimationFrame(step);
  };
  frame = requestAnimationFrame(step);
}

watch(
  () => props.value,
  (to, from) => {
    tween(from, to);
    if (to === from) return;
    bumped.value = true;
    window.clearTimeout(bumpTimer);
    // 与 `.stat-bump` 的 200ms 对齐：多留一点余量，避免动画没播完就被摘掉。
    bumpTimer = window.setTimeout(() => {
      bumped.value = false;
    }, 220);
  },
);

// 挂载时也滚一次 0 → 当前值：进这页时数据通常已经在了，不滚就只剩静态数字。
// `delay` 与卡片的入场动画对齐，一排数字错开起步，不会六块一起「啪」。
onMounted(() => {
  if (props.delay > 0) {
    startTimer = window.setTimeout(() => tween(0, props.value), props.delay);
    return;
  }
  tween(0, props.value);
});

onBeforeUnmount(() => {
  cancelAnimationFrame(frame);
  window.clearTimeout(bumpTimer);
  window.clearTimeout(startTimer);
});
</script>

<template>
  <span class="inline-block tabular-nums" :class="{ 'stat-bump': bumped }">{{ shown }}</span>
</template>
