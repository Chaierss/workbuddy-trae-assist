<script setup lang="ts" generic="T extends string | number">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { Check, ChevronDown } from "lucide-vue-next";

/**
 * 下拉选择：统一替代原生 `<select>`。
 *
 * 原生 select 的弹层由系统绘制，改不了样式也做不了动画；这里换成按钮 + 浮层，
 * 并统一了几件事：
 * - 浮层 Teleport 到 body，避免被卡片 `overflow-hidden` 裁掉；
 * - 空间不够时自动向上翻，宽度按触发器宽度、右侧越界时左移；
 * - 浮层进出有淡入 + 轻微上移/缩放（`popover` 过渡，见 style.css）；
 * - 键盘可用：↑/↓ 移动、Enter 选中、Esc 关闭；点击外部关闭。
 */
export interface SelectOption<V> {
  value: V;
  label: string;
  disabled?: boolean;
}

const props = withDefaults(
  defineProps<{
    modelValue: T;
    options: SelectOption<T>[];
    /** 没有匹配选项时的占位文案（也可当「全部」空值项的标签）。 */
    placeholder?: string;
    size?: "md" | "sm" | "xs";
    disabled?: boolean;
    title?: string;
  }>(),
  { size: "md" },
);

const emit = defineEmits<{ "update:modelValue": [value: T] }>();

const open = ref(false);
const root = ref<HTMLElement | null>(null);
const trigger = ref<HTMLButtonElement | null>(null);
const panel = ref<HTMLElement | null>(null);
const activeIndex = ref(-1);
const placeUp = ref(false);
const anchor = ref({ left: 0, top: 0, bottom: 0, width: 0 });

const selected = computed(() => props.options.find((item) => item.value === props.modelValue));
const selectedLabel = computed(() => selected.value?.label ?? props.placeholder ?? "");

const triggerClass = computed(() => {
  if (props.size === "xs") return "gap-1 rounded-badge px-1.5 py-0.5 text-body-sm";
  if (props.size === "sm") return "gap-1.5 h-8 rounded-control px-2 text-body-sm";
  return "gap-2 rounded-control px-3 py-2 text-body";
});

const panelStyle = computed(() => {
  const width = Math.min(Math.max(anchor.value.width, 168), 320);
  const left = Math.max(8, Math.min(anchor.value.left, window.innerWidth - width - 8));
  const style: Record<string, string> = { width: `${width}px`, left: `${left}px` };
  if (placeUp.value) style.bottom = `${window.innerHeight - anchor.value.top + 6}px`;
  else style.top = `${anchor.value.bottom + 6}px`;
  return style;
});

function position(): void {
  const el = trigger.value;
  if (!el) return;
  const rect = el.getBoundingClientRect();
  anchor.value = { left: rect.left, top: rect.top, bottom: rect.bottom, width: rect.width };
  const estimated = panel.value?.offsetHeight || props.options.length * 30 + 8;
  const below = window.innerHeight - rect.bottom - 12;
  placeUp.value = below < estimated && rect.top - 12 > below;
}

function toggle(): void {
  if (props.disabled) return;
  open.value = !open.value;
}

function close(): void {
  open.value = false;
}

function choose(option: SelectOption<T>): void {
  if (option.disabled) return;
  emit("update:modelValue", option.value);
  close();
}

function move(step: number): void {
  const total = props.options.length;
  if (!total) return;
  let next = activeIndex.value;
  for (let i = 0; i < total; i += 1) {
    next = (next + step + total) % total;
    if (!props.options[next]?.disabled) break;
  }
  activeIndex.value = next;
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") {
    event.stopPropagation();
    close();
    return;
  }
  if (!open.value) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp" || event.key === "Enter") {
      event.preventDefault();
      open.value = true;
    }
    return;
  }
  if (event.key === "ArrowDown") {
    event.preventDefault();
    move(1);
  } else if (event.key === "ArrowUp") {
    event.preventDefault();
    move(-1);
  } else if (event.key === "Enter") {
    event.preventDefault();
    const option = props.options[activeIndex.value];
    if (option) choose(option);
  } else if (event.key === "Tab") {
    close();
  }
}

function onDocumentPointer(event: MouseEvent): void {
  const target = event.target as Node;
  if (root.value?.contains(target) || panel.value?.contains(target)) return;
  close();
}

function onViewportChange(): void {
  if (open.value) position();
}

watch(open, async (value) => {
  if (!value) return;
  await nextTick();
  position();
  activeIndex.value = Math.max(
    0,
    props.options.findIndex((item) => item.value === props.modelValue),
  );
  panel.value?.focus();
});

watch(
  () => props.modelValue,
  () => {
    if (open.value) position();
  },
);

onMounted(() => {
  document.addEventListener("mousedown", onDocumentPointer);
  window.addEventListener("scroll", onViewportChange, true);
  window.addEventListener("resize", onViewportChange);
});
onBeforeUnmount(() => {
  document.removeEventListener("mousedown", onDocumentPointer);
  window.removeEventListener("scroll", onViewportChange, true);
  window.removeEventListener("resize", onViewportChange);
});
</script>

<template>
  <div ref="root" class="relative">
    <button
      ref="trigger"
      type="button"
      class="flex w-full items-center border border-line bg-surface text-left text-ink transition-[border-color,box-shadow,color] duration-150 hover:border-line-strong disabled:cursor-not-allowed disabled:opacity-60"
      :class="[triggerClass, open ? 'border-brand' : '']"
      :disabled="disabled"
      :title="title"
      aria-haspopup="listbox"
      :aria-expanded="open"
      @click="toggle"
      @keydown="onKeydown"
    >
      <span class="min-w-0 flex-1 truncate" :class="selected ? '' : 'text-muted'">
        {{ selectedLabel }}
      </span>
      <ChevronDown
        :size="size === 'xs' ? 12 : 13"
        :stroke-width="2"
        class="shrink-0 text-muted transition-transform duration-150"
        :class="open ? 'rotate-180' : ''"
      />
    </button>

    <Teleport to="body">
      <Transition name="popover">
        <div
          v-if="open"
          ref="panel"
          class="fixed z-[900] max-h-[300px] overflow-y-auto rounded-control border border-line bg-surface p-1 shadow-[var(--shadow-float)]"
          :style="panelStyle"
          role="listbox"
          tabindex="-1"
          @keydown="onKeydown"
        >
          <button
            v-for="(option, index) in options"
            :key="option.value"
            type="button"
            role="option"
            :aria-selected="option.value === modelValue"
            class="flex w-full items-center gap-2 rounded-control px-2.5 py-1.5 text-left text-body-sm transition-colors"
            :class="[
              option.disabled ? 'cursor-not-allowed opacity-45' : '',
              index === activeIndex ? 'bg-line-soft' : '',
              option.value === modelValue ? 'font-medium text-brand-dark' : 'text-ink-soft',
            ]"
            :title="option.label"
            @mouseenter="activeIndex = index"
            @click="choose(option)"
          >
            <span class="min-w-0 flex-1 truncate">{{ option.label }}</span>
            <Check
              v-if="option.value === modelValue"
              :size="13"
              :stroke-width="2.2"
              class="shrink-0 text-brand"
            />
          </button>
          <div v-if="!options.length" class="px-2.5 py-2 text-body-sm text-muted">
            {{ placeholder }}
          </div>
        </div>
      </Transition>
    </Teleport>
  </div>
</template>
