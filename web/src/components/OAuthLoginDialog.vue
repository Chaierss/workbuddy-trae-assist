<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from "vue";
import { ExternalLink, QrCode, Loader2 } from "lucide-vue-next";
import { api } from "../api";
import { t } from "../lib/i18n";

type Platform = "workbuddy" | "trae";

const props = defineProps<{ open: boolean; platform: Platform }>();
const emit = defineEmits<{ close: []; imported: [] }>();

/**
 * 扫码登录：发起 → 打开授权页 → 轮询结果 → 入库。
 * 后端进程内存保存 loginId→会话，前端负责打开授权页与轮询。
 * 两个平台共用同一套交互：WorkBuddy 走 /api/oauth，Trae 走 /api/trae/oauth。
 */
const state = reactive({
  busy: false,
  loginId: null as string | null,
  uri: "",
  error: "",
  done: false,
  accountName: "",
});

const platformLabel = computed(() => (props.platform === "trae" ? "Trae" : "WorkBuddy"));

let timer: number | null = null;

function reset(): void {
  state.busy = false;
  state.loginId = null;
  state.uri = "";
  state.error = "";
  state.done = false;
  state.accountName = "";
}

function clearTimer(): void {
  if (timer !== null) {
    window.clearTimeout(timer);
    timer = null;
  }
}

async function start(): Promise<void> {
  state.busy = true;
  state.error = "";
  try {
    const res =
      props.platform === "trae" ? await api.traeOauthStart() : await api.oauthStart();
    state.loginId = res.loginId;
    state.uri = res.verificationUri;
    // 打开授权页让用户扫码
    window.open(res.verificationUri, "_blank", "noopener,noreferrer");
    poll();
  } catch (error) {
    state.error = (error as Error).message;
  } finally {
    state.busy = false;
  }
}

async function poll(): Promise<void> {
  if (!state.loginId) return;
  try {
    const res =
      props.platform === "trae"
        ? await api.traeOauthStatus(state.loginId)
        : await api.oauthStatus(state.loginId);
    if (res.done) {
      if (res.result) {
        state.done = true;
        state.accountName = res.result.account.displayName;
      } else if (res.error) {
        state.error = res.error;
      }
      return;
    }
    timer = window.setTimeout(poll, 1500);
  } catch (error) {
    state.error = (error as Error).message;
  }
}

function close(): void {
  clearTimer();
  const imported = state.done;
  const loginId = state.loginId;
  // 取消未完成的 Trae 登录，释放 17388 回调端口。
  if (props.platform === "trae" && loginId && !imported) {
    void api.traeOauthCancel(loginId).catch(() => {});
  }
  reset();
  emit("close");
  if (imported) emit("imported");
}

watch(
  () => props.open,
  (open) => {
    if (open) reset();
    else clearTimer();
  },
);

onBeforeUnmount(clearTimer);
</script>

<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="fixed inset-0 z-[500] flex items-center justify-center bg-black/30 p-6"
      @click.self="close"
    >
      <div class="w-full max-w-[440px] rounded-[14px] border border-line bg-surface p-5 shadow-[0_16px_48px_-12px_rgba(28,30,28,0.28)]">
        <h2 class="flex items-center gap-2 text-[15px] font-semibold text-ink">
          <QrCode :size="17" :stroke-width="1.9" class="text-brand" />
          {{ t("dialog.oauth.title", { platform: platformLabel }) }}
        </h2>
        <p class="mt-1.5 text-[12px] leading-relaxed text-muted">
          {{ t("dialog.oauth.desc", { platform: platformLabel }) }}
        </p>

        <!-- 未发起 -->
        <div v-if="!state.loginId && !state.done" class="mt-4">
          <button
            type="button"
            class="flex w-full items-center justify-center gap-2 rounded-[10px] bg-brand px-3.5 py-2.5 text-[13px] font-medium text-white transition-colors hover:bg-brand-dark disabled:opacity-60"
            :disabled="state.busy"
            @click="start"
          >
            <Loader2 v-if="state.busy" :size="14" class="animate-spin" />
            {{ state.busy ? t("dialog.oauth.starting") : t("dialog.oauth.start") }}
          </button>
        </div>

        <!-- 已发起，等待扫码 -->
        <div v-else-if="state.loginId && !state.done" class="mt-4 space-y-3">
          <div class="rounded-[10px] border border-line bg-line-soft/40 p-3">
            <div class="flex items-center gap-2 text-[12px] text-muted">
              <Loader2 :size="13" class="animate-spin text-brand" />
              {{ t("dialog.oauth.waiting") }}
            </div>
            <a
              :href="state.uri"
              target="_blank"
              rel="noopener noreferrer"
              class="mt-2 flex items-center gap-1.5 break-all text-[12px] text-brand underline-offset-2 hover:underline"
            >
              <ExternalLink :size="13" :stroke-width="1.9" />
              {{ t("dialog.oauth.reopen") }}
            </a>
          </div>
        </div>

        <!-- 完成 -->
        <div v-else-if="state.done" class="mt-4 rounded-[10px] border border-brand-soft bg-brand-soft p-3 text-[13px] text-brand-dark">
          {{ t("dialog.oauth.imported", { name: state.accountName }) }}
        </div>

        <p v-if="state.error" class="mt-3 text-[12px] text-danger">{{ state.error }}</p>

        <div class="mt-4 flex justify-end">
          <button
            type="button"
            class="rounded-[9px] border border-line px-3.5 py-2 text-[12.5px] text-ink-soft transition-colors hover:border-[#d5d8d3] hover:text-ink"
            @click="close"
          >
            {{ state.done ? t("dialog.oauth.done") : t("common.cancel") }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>
