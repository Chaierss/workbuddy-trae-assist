<script setup lang="ts">
import { AlertTriangle, CheckCircle2, Info, X } from "lucide-vue-next";
import { dismissToast, toasts } from "../lib/toast";
import { t } from "../lib/i18n";

/**
 * 轻提示容器：挂在 AppShell 顶部，全局只有一个。
 * 位置在右上（不与页头动作抢视线），队列最多 3 条，错误不自动消失。
 */
</script>

<template>
  <Teleport to="body">
    <div
      class="pointer-events-none fixed right-4 top-[calc(var(--header-height)+12px)] z-[900] flex w-[min(360px,calc(100vw-2rem))] flex-col gap-2"
      role="status"
      aria-live="polite"
    >
      <TransitionGroup
        enter-active-class="transition duration-150 ease-out"
        enter-from-class="-translate-y-1 opacity-0"
        leave-active-class="transition duration-150 ease-in"
        leave-to-class="translate-y-1 opacity-0"
      >
        <div
          v-for="item in toasts"
          :key="item.id"
          class="pointer-events-auto flex items-start gap-2 rounded-card border bg-surface px-3.5 py-2.5 shadow-float"
          :class="
            item.tone === 'error'
              ? 'border-danger/35'
              : item.tone === 'success'
                ? 'border-brand/30'
                : 'border-line'
          "
        >
          <CheckCircle2
            v-if="item.tone === 'success'"
            :size="15"
            :stroke-width="1.9"
            class="mt-0.5 shrink-0 text-brand"
          />
          <AlertTriangle
            v-else-if="item.tone === 'error'"
            :size="15"
            :stroke-width="1.9"
            class="mt-0.5 shrink-0 text-danger"
          />
          <Info v-else :size="15" :stroke-width="1.9" class="mt-0.5 shrink-0 text-info" />

          <p class="min-w-0 flex-1 whitespace-pre-line text-body-sm leading-relaxed text-ink-soft">
            {{ item.message }}
          </p>

          <button
            type="button"
            class="shrink-0 rounded p-0.5 text-muted transition-colors hover:text-ink"
            :aria-label="t('common.close')"
            @click="dismissToast(item.id)"
          >
            <X :size="13" :stroke-width="2" />
          </button>
        </div>
      </TransitionGroup>
    </div>
  </Teleport>
</template>
