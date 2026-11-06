<script setup lang="ts">
import { reactive, ref } from 'vue'
import { api, saveToken } from '../api'
import type { User } from '../types'

const emit = defineEmits<{ authenticated: [user: User] }>()
const mode = ref<'login' | 'register'>('login')
const busy = ref(false)
const error = ref('')
const form = reactive({ username: '', password: '', confirm: '' })
async function submit() {
  error.value = ''
  if (!form.username.trim() || !form.password) { error.value = '请输入用户名和密码。'; return }
  if (mode.value === 'register' && !/^[\p{L}\p{N}_-]{3,64}$/u.test(form.username.trim())) { error.value = '用户名需为 3–64 位中文、字母、数字、_ 或 -。'; return }
  if (mode.value === 'register' && (form.password.length < 10 || new TextEncoder().encode(form.password).length > 72)) { error.value = '密码至少 10 位，且 UTF-8 编码不能超过 72 字节。'; return }
  if (mode.value === 'register' && form.password !== form.confirm) { error.value = '两次输入的密码不一致。'; return }
  busy.value = true
  try {
    const result = await api<{ token: string; user: User }>(`/auth/${mode.value}`, 'POST', { username: form.username.trim(), password: form.password })
    saveToken(result.token)
    emit('authenticated', result.user)
  } catch (e) { error.value = e instanceof Error ? e.message : '操作失败，请重试。' }
  finally { busy.value = false }
}
</script>

<template>
  <main class="auth-layout">
    <section class="auth-story">
      <div class="brand"><span class="brand-mark">d<span>↗</span></span><span>dataflow<span class="brand-sub">DATA CLEANING PIPELINE</span></span></div>
      <div class="auth-story-copy"><span class="eyebrow light">从原始数据，到可靠结果</span><h1>让每一份数据，<br />清晰可用。</h1><p>将上传、清洗、校验和交付连成一条可追踪的流水线。<br />规则可复用，过程有记录，结果可验证。</p></div>
      <div class="auth-pipeline"><span>01 <b>上传</b></span><i>→</i><span>02 <b>清洗</b></span><i>→</i><span>03 <b>交付</b></span></div>
      <div class="auth-footer">CSV · Excel · Parquet <span>STRUCTURED DATA, SIMPLIFIED.</span></div>
    </section>
    <section class="auth-form-area">
      <form class="auth-form" @submit.prevent="submit">
        <span class="eyebrow">欢迎使用 DATAFLOW</span><h2>{{ mode === 'login' ? '登录你的工作空间' : '创建你的工作空间' }}</h2>
        <p class="muted">{{ mode === 'login' ? '继续管理文件、清洗规则和执行任务。' : '注册后即可上传文件并运行第一条流水线。' }}</p>
        <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="auth-error" />
        <label class="field"><span>用户名</span><el-input v-model="form.username" autocomplete="username" placeholder="请输入用户名" maxlength="64" :disabled="busy" /></label>
        <label class="field"><span>密码</span><el-input v-model="form.password" type="password" :autocomplete="mode === 'login' ? 'current-password' : 'new-password'" show-password placeholder="请输入密码" maxlength="128" :disabled="busy" /></label>
        <label v-if="mode === 'register'" class="field"><span>确认密码</span><el-input v-model="form.confirm" type="password" autocomplete="new-password" show-password placeholder="再次输入密码" maxlength="128" :disabled="busy" /></label>
        <p v-if="mode === 'register'" class="form-note">用户名 3–64 位，支持中文、字母、数字、_ 和 -；密码至少 10 位，UTF-8 编码不超过 72 字节。</p>
        <el-button type="primary" native-type="submit" class="auth-submit" :loading="busy">{{ mode === 'login' ? '登 录' : '创建账户' }} <span>→</span></el-button>
        <p class="auth-switch">{{ mode === 'login' ? '还没有账户？' : '已经有账户？' }}<el-button link type="primary" :disabled="busy" @click="mode = mode === 'login' ? 'register' : 'login'; error = ''">{{ mode === 'login' ? '注册账户' : '返回登录' }}</el-button></p>
      </form>
    </section>
  </main>
</template>
