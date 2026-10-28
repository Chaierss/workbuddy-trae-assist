#!/usr/bin/env node
/**
 * keel 体检脚本 —— 零依赖，Node >= 18，与项目语言/框架无关（只读文本）。
 *
 * 用法：
 *   node keel/console-check.mjs                 人读报告
 *   node keel/console-check.mjs --strict        有 error 级问题时退出码 1（给 CI / pre-commit）
 *   node keel/console-check.mjs --json          机器可读（给设置页 / dashboard）
 *   node keel/console-check.mjs --fail-on-unused 未使用组件也算失败（默认只 warn）
 *   node keel/console-check.mjs --root ../proj  指定项目根
 *   node keel/console-check.mjs --version       打印脚本版本（与 package.json 必须一致，有测试守着）
 *
 * 设计原则：只有"机器能确定错了"的项才是 error。未使用组件需要人判断，永远是 warn。
 */
import { readFileSync, existsSync, readdirSync, statSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { join, resolve, relative, extname, basename, dirname } from 'node:path'
import process from 'node:process'

/** 技能版本。改这里必须同步改 package.json / SKILL.md frontmatter，测试会校验三者一致。 */
const KEEL_VERSION = '2.1.0'

const HELP = `keel 体检
  --root <dir>       项目根（默认 cwd）
  --strict           error 级问题导致退出码 1
  --fail-on-unused   未使用组件也导致退出码 1（隐含 --strict，别以为不写 --strict 就没门禁）
  --json             输出 JSON
  --version          打印脚本版本
  -h, --help         显示帮助`

const DEFAULT_CONFIG = {
  consoleDir: 'keel',
  srcDir: 'src',
  componentsDirs: ['src/components'],
  wikiDir: 'docs/wiki',
  sourceExts: ['.vue', '.tsx', '.jsx', '.ts', '.js'],
  componentExts: ['.vue', '.tsx', '.jsx', '.svelte'],
  excludeDirs: ['node_modules', 'dist', 'build', '.git', '.next', 'coverage'],
  testPatterns: ['.test.', '.spec.', '.stories.'],
  demoSuffix: '.demo',
}

function parseArgs(argv) {
  const out = { root: process.cwd(), strict: false, json: false, failOnUnused: false }
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i]
    if (arg === '--root') out.root = argv[i += 1]
    else if (arg === '--strict') out.strict = true
    else if (arg === '--json') out.json = true
    else if (arg === '--fail-on-unused') out.failOnUnused = true
    else if (arg === '--version') {
      console.log(`keel ${KEEL_VERSION}`)
      process.exit(0)
    }
    else if (arg === '-h' || arg === '--help') {
      console.log(HELP)
      process.exit(0)
    }
  }
  out.root = resolve(out.root)
  return out
}

const posix = (p) => p.split('\\').join('/')

/** 三段式版本比较，够用即可（本技能不会出现 prerelease）。返回 -1 / 0 / 1 */
function compareVersion(a, b) {
  const pa = String(a).split('.').map((n) => parseInt(n, 10) || 0)
  const pb = String(b).split('.').map((n) => parseInt(n, 10) || 0)
  for (let i = 0; i < 3; i += 1) {
    const diff = (pa[i] || 0) - (pb[i] || 0)
    if (diff !== 0) return diff < 0 ? -1 : 1
  }
  return 0
}

function readJson(path) {
  try {
    // Windows 编辑器常写出带 BOM 的 UTF-8，JSON.parse 会直接失败，先剥掉
    const raw = readFileSync(path, 'utf8').replace(/^\uFEFF/, '')
    return { ok: true, data: JSON.parse(raw) }
  } catch (error) {
    return { ok: false, error: error.message }
  }
}

/**
 * 文件是否被 .gitignore 排除（相对项目根）。
 * 用途：docs/ 下有一部分文档是「本地保留、不进版本库」的内部资料，CI 检出后
 * 本来就没有它们；这属于设计如此，不能当成缺失。只有「没被忽略却不在」
 * 才是真的丢文件。git 不可用或不是仓库时返回 false（退化为原来的严格判断）。
 */
function isGitIgnored(root, rel) {
  const res = spawnSync('git', ['check-ignore', '-q', '--', rel], { cwd: root })
  return res.status === 0
}

function walkFiles(root, dirs, exts, excludeDirs, acc = []) {
  for (const dir of dirs) {
    const abs = resolve(root, dir)
    if (!existsSync(abs)) continue
    walkInto(abs, exts, excludeDirs, acc, root)
  }
  return acc
}

function walkInto(absDir, exts, excludeDirs, acc, root) {
  let entries
  try {
    entries = readdirSync(absDir, { withFileTypes: true })
  } catch {
    return
  }
  for (const entry of entries) {
    const abs = join(absDir, entry.name)
    if (entry.isDirectory()) {
      if (excludeDirs.includes(entry.name)) continue
      walkInto(abs, exts, excludeDirs, acc, root)
    } else if (entry.isFile()) {
      if (!exts.includes(extname(entry.name))) continue
      acc.push({ abs, rel: posix(relative(root, abs)) })
    }
  }
}

const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

function main() {
  const args = parseArgs(process.argv.slice(2))
  const root = args.root
  const issues = []

  const push = (id, level, message, items = []) => issues.push({ id, level, message, items })

  // ---------- 配置 ----------
  // 叫 CFG-01 而不是 MODE-01：这里查的是"配置就位且可解析"，与可见性边界无关。
  // 边界（哪些产物 / 谁能开 / 哪些字段）是人的判断，机器判定不了内容对错，所以不设门禁项，
  // 只把它写进 references/visibility.md 的验收清单，由人工逐条过。
  const consoleDir = resolve(root, DEFAULT_CONFIG.consoleDir)
  const configPath = join(consoleDir, 'console.config.json')
  let config = { ...DEFAULT_CONFIG }
  let configReady = false
  if (!existsSync(consoleDir)) {
    push('CFG-01', 'error', `缺少 ${DEFAULT_CONFIG.consoleDir}/ 目录（唯一真相源应集中在此）`)
  } else {
    const loaded = readJson(configPath)
    if (loaded.ok) {
      config = { ...DEFAULT_CONFIG, ...loaded.data }
      configReady = true
      push('CFG-01', 'ok', 'console.config.json 已就位')
    } else {
      push('CFG-01', 'error', `console.config.json ${existsSync(configPath) ? '解析失败：' + loaded.error : '不存在，使用默认扫描范围'}`)
    }
  }

  // ---------- 模板版本（提醒升级，不阻断） ----------
  // 拷贝制的代价：技能更新了，项目里的 keel/ 不会自动变。这里只负责"告诉你现在差多少"，
  // 不替用户改文件——模板只是起点，用户可能已经改过一半，自动合并的风险远大于收益。
  if (configReady) {
    const declared = typeof config.keelVersion === 'string' ? config.keelVersion.trim() : ''
    if (!declared) {
      push('VER-01', 'warn', `未声明模板版本（当前技能 ${KEEL_VERSION}），无法判断是否需要迁移；在 console.config.json 加 "keelVersion": "${KEEL_VERSION}"`)
    } else if (compareVersion(declared, KEEL_VERSION) === 0) {
      push('VER-01', 'ok', `模板版本 ${declared}（最新）`)
    } else if (compareVersion(declared, KEEL_VERSION) < 0) {
      push('VER-01', 'warn', `模板版本 ${declared}，当前技能 ${KEEL_VERSION}：有更新，先看 references/upgrading.md 再改，别整目录覆盖`)
    } else {
      push('VER-01', 'warn', `模板版本 ${declared} 比本脚本 ${KEEL_VERSION} 新：脚本该更新了，否则会漏检新增检查项`)
    }
  }

  const sourceFiles = walkFiles(root, [config.srcDir], config.sourceExts, config.excludeDirs)
  const componentFiles = walkFiles(root, config.componentsDirs, config.componentExts, config.excludeDirs)

  // ---------- 规则 ----------
  const rulesPath = join(consoleDir, 'rules.json')
  let rules = null
  if (!existsSync(rulesPath)) {
    push('RULE-01', 'error', '缺少 rules.json（规则与术语的唯一真相源）')
  } else {
    const loaded = readJson(rulesPath)
    if (!loaded.ok) {
      push('RULE-01', 'error', `rules.json 解析失败：${loaded.error}`)
    } else {
      rules = loaded.data
      const modes = Array.isArray(rules.pageModes) ? rules.pageModes : []
      const terms = Array.isArray(rules.terms) ? rules.terms : []
      const list = Array.isArray(rules.rules) ? rules.rules : []
      const missingVersion = !rules.version
      push('RULE-01', missingVersion ? 'warn' : 'ok',
        `rules.json：${modes.length} 个页面形态 / ${terms.length} 条术语 / ${list.length} 条规则${missingVersion ? '（缺 version，AI 上下文无法判断新旧）' : ` · 版本 ${rules.version}`}`)

      const ids = new Set()
      const badIds = []
      const badFields = []
      const noSource = []
      const missingAvoid = []
      for (const rule of list) {
        if (!rule.id || ids.has(rule.id)) badIds.push(rule.id || '(空 id)')
        ids.add(rule.id)
        if (!rule.title || !rule.summary || !rule.category || !rule.severity) badFields.push(rule.id || '(空 id)')
        if (!rule.source) noSource.push(rule.id || '(空 id)')
        if (!['must', 'should'].includes(rule.severity)) badFields.push(`${rule.id}(severity=${rule.severity})`)
      }
      // 页面形态与术语是同一类东西（共同语言），字段要求一致：正名 + 定义 + 不要怎么叫
      for (const entry of [...modes.map((m) => ({ ...m, kind: '形态' })), ...terms.map((t) => ({ ...t, kind: '术语' }))]) {
        if (!entry.name || !entry.definition) badFields.push(`${entry.kind} ${entry.key || '?'}`)
        if (!entry.avoid || !entry.avoid.length) missingAvoid.push(entry.name || entry.key || '?')
      }
      push('RULE-02', badIds.length || badFields.length ? 'error' : 'ok',
        badIds.length ? `规则 id 重复/缺失：${badIds.join('、')}` : badFields.length ? `规则字段不完整：${badFields.join('、')}` : '规则 id 唯一、必填字段齐全')
      push('RULE-03', missingAvoid.length ? 'warn' : 'ok',
        missingAvoid.length ? `词条缺 avoid（最容易产生误解的字段）：${missingAvoid.join('、')}` : '词条均含 definition / signal / avoid')
      push('RULE-04', noSource.length ? 'warn' : 'ok',
        noSource.length ? `规则缺 source（无法核对出处）：${noSource.join('、')}` : '规则均带 source')

      // AI 归纳出来的候选条目一律 status: draft —— 没有出处的候选就是幻觉，不能进 AI 上下文
      const drafts = [...list, ...modes, ...terms].filter((e) => e.status === 'draft')
      const noEvidence = drafts.filter((e) => !Array.isArray(e.evidence) || !e.evidence.length)
      push('RULE-05', noEvidence.length ? 'warn' : 'ok',
        noEvidence.length
          ? `${noEvidence.length} 条 draft 缺 evidence（AI 归纳的条目必须能指回具体文件）：${noEvidence.map((e) => e.id || e.name || e.key || '?').join('、')}`
          : drafts.length
            ? `${drafts.length} 条待确认（draft），确认前不进 AI 上下文导出`
            : '没有待确认条目')
    }
  }

  // ---------- 组件 ----------
  const registryPath = join(consoleDir, 'components.json')
  let registry = []
  if (!existsSync(registryPath)) {
    push('COMP-01', 'error', '缺少 components.json（组件注册表）')
  } else {
    const loaded = readJson(registryPath)
    if (!loaded.ok) {
      push('COMP-01', 'error', `components.json 解析失败：${loaded.error}`)
    } else {
      registry = (Array.isArray(loaded.data) ? loaded.data : []).filter((e) => e && e.path)
      const missing = registry.filter((entry) => !existsSync(resolve(root, entry.path))).map((entry) => `${entry.key} → ${entry.path}`)
      push('COMP-01', missing.length ? 'error' : 'ok',
        missing.length ? `注册表指向的文件不存在：${missing.join('、')}` : `注册表 ${registry.length} 条，path 全部存在`)
    }
  }

  // 实际组件文件（排除 index 出口、demo、测试）
  const isDemoOrTest = (name) =>
    name.includes(config.demoSuffix) || config.testPatterns.some((p) => name.includes(p))
  const componentDirSet = config.componentsDirs.map((d) => posix(d))

  const actualComponents = componentFiles.filter((file) => {
    const name = basename(file.rel)
    if (isDemoOrTest(name)) return false
    const bare = name.slice(0, name.length - extname(name).length)
    if (bare === 'index' && componentDirSet.some((d) => file.rel.startsWith(`${d}/`))) return false
    return true
  })

  const registeredPaths = new Set(registry.map((entry) => posix(entry.path)))
  const unregistered = actualComponents.filter((file) => !registeredPaths.has(file.rel)).map((file) => file.rel)
  push('COMP-02', unregistered.length ? 'error' : 'ok',
    unregistered.length ? `${unregistered.length} 个组件未登记（目录里有、注册表没有）` : '目录内组件全部已登记',
    unregistered)

  // demo 存在性
  const demoMissing = []
  for (const entry of registry) {
    if (entry.demo === null) continue
    const dir = dirname(entry.path)
    const base = basename(entry.path).slice(0, basename(entry.path).length - extname(entry.path).length)
    if (entry.demo === 'auto') {
      const found = config.sourceExts.some((ext) => existsSync(resolve(root, `${dir}/${base}${config.demoSuffix}${ext}`)))
      if (!found) demoMissing.push(`${entry.key}（期望 ${dir}/${base}${config.demoSuffix}.*）`)
    } else if (typeof entry.demo === 'string' && !existsSync(resolve(root, entry.demo))) {
      demoMissing.push(`${entry.key} → ${entry.demo}`)
    }
  }
  push('COMP-03', demoMissing.length ? 'warn' : 'ok',
    demoMissing.length ? `${demoMissing.length} 个组件声明了预览但 demo 文件不存在` : 'demo 文件齐全',
    demoMissing)

  // 引用扫描
  const consoleDirRel = posix(config.consoleDir)
  const usageCorpus = sourceFiles.filter((file) => {
    if (file.rel.startsWith(`${consoleDirRel}/`)) return false
    if (isDemoOrTest(basename(file.rel))) return false
    return true
  })

  const usage = []
  for (const entry of registry) {
    const base = basename(entry.path).slice(0, basename(entry.path).length - extname(entry.path).length)
    const names = String(entry.exportName || '')
      .split('/')
      .map((n) => n.trim())
      .filter((n) => n && n.length > 1)
    // Vue/SFC 的 import 带后缀（`.../StatusPill.vue`），只匹配裸名字会把所有
    // 组件误报成「未使用」。这里允许可选的 `.vue/.tsx/...` 后缀。
    const patterns = [new RegExp(`from\\s+['"][^'"]*${escapeRe(base)}(?:\\.\\w+)?['"]`)]
    for (const name of names) {
      patterns.push(new RegExp(`(?:import|export)\\s+(?:type\\s+)?\\{[^}]*\\b${escapeRe(name)}\\b[^}]*\\}`))
      patterns.push(new RegExp(`from\\s+['"][^'"]*${escapeRe(name)}['"]`))
    }
    if (entry.manualUsage && entry.manualUsage.length) {
      usage.push({ key: entry.key, path: entry.path, count: entry.manualUsage.length, files: entry.manualUsage })
      continue
    }
    const hits = usageCorpus
      .filter((file) => {
        if (file.rel === posix(entry.path)) return false
        const content = safeRead(file.abs)
        return patterns.some((re) => re.test(content))
      })
      .map((file) => file.rel)
    usage.push({ key: entry.key, path: entry.path, count: hits.length, files: hits })
  }

  const unused = usage.filter((item) => item.count === 0 && !registry.find((e) => e.key === item.key)?.deprecated)
  push('COMP-04', unused.length ? 'warn' : 'ok',
    unused.length ? `${unused.length} 个已登记组件没有任何引用（候选下架，先标 deprecated 再删）` : '已登记组件均有引用',
    unused.map((item) => item.key))

  // ---------- Wiki ----------
  const wikiListPath = join(consoleDir, 'wiki.json')
  if (!existsSync(wikiListPath)) {
    push('WIKI-01', 'error', '缺少 wiki.json（wiki 导航清单）')
  } else {
    const loaded = readJson(wikiListPath)
    if (!loaded.ok) {
      push('WIKI-01', 'error', `wiki.json 解析失败：${loaded.error}`)
    } else {
      const files = Array.isArray(loaded.data) ? loaded.data : []
      const missing = []
      const ignored = []
      const empty = []
      for (const item of files) {
        const abs = resolve(root, config.wikiDir, item.name)
        if (!existsSync(abs)) {
          // 被 .gitignore 排除的内部文档在 CI 检出里天然不存在，不算缺失；
          // 否则每次 CI 都会因为「本地有、仓库没有」而误红。
          if (isGitIgnored(root, posix(join(config.wikiDir, item.name)))) ignored.push(item.name)
          else missing.push(item.name)
        } else if (readFileSync(abs, 'utf8').trim().length < 50) empty.push(item.name)
      }
      const localOnly = ignored.length
        ? `；另 ${ignored.length} 篇为 .gitignore 排除的内部文档（本地留存，检出后不存在属正常）：${ignored.join('、')}`
        : ''
      const problems = []
      if (missing.length) problems.push(`清单里的文档不存在：${missing.join('、')}`)
      if (empty.length) problems.push(`文档内容过少（<50 字符）：${empty.join('、')}`)
      push('WIKI-01', missing.length ? 'error' : empty.length ? 'warn' : 'ok',
        problems.length ? problems.join('；') + localOnly : `${files.length - ignored.length} 篇 wiki 文档就位${localOnly}`)
      if (!missing.length) {
        // 用相对 wikiDir 的完整路径比对，而不是 basename —— 否则 policies/ 这类
        // 子目录文档永远匹配不上清单，每次体检都误报「未登记」。
        const onDisk = walkFiles(root, [config.wikiDir], ['.md'], config.excludeDirs)
          .map((f) => f.rel.slice(config.wikiDir.length + 1))
        const orphan = onDisk.filter((name) => !files.some((item) => item.name === name))
        push('WIKI-02', orphan.length ? 'warn' : 'ok',
          orphan.length ? `目录里有未登记的文档（页面看不到）：${orphan.join('、')}` : '目录与清单一致',
          orphan)
      }
    }
  }

  // ---------- Wiki 代码引用（引用式内联） ----------
  // 手粘贴的代码会过期，而且过期了没人发现。所以 wiki 里贴真实实现一律用引用：
  //   <!-- keel:code src/core/rank.ts 12-40 -->
  // 页面按行号内联渲染，源文件改了页面跟着变。这里只做一件事：确认引用还指得着。
  const CODE_REF = /<!--\s*keel:code\s+([^\s>]+)(?:\s+(\d+)\s*-\s*(\d+))?\s*-->/g
  const wikiFiles = walkFiles(root, [config.wikiDir], ['.md'], config.excludeDirs)
  const badRefs = []
  const longRefs = []
  let refCount = 0
  for (const file of wikiFiles) {
    for (const match of safeRead(file.abs).matchAll(CODE_REF)) {
      refCount += 1
      const [, target, from, to] = match
      const abs = resolve(root, target)
      if (!existsSync(abs)) {
        badRefs.push(`${file.rel} → ${target}（文件不存在）`)
        continue
      }
      if (!from || !to) continue
      const total = safeRead(abs).split('\n').length
      const start = Number(from)
      const end = Number(to)
      if (start < 1 || end < start || end > total) {
        badRefs.push(`${file.rel} → ${target}:${start}-${end}（行号越界，该文件共 ${total} 行）`)
      } else if (end - start + 1 > 120) {
        longRefs.push(`${file.rel} → ${target}:${start}-${end}（${end - start + 1} 行）`)
      }
    }
  }
  push('WIKI-03', badRefs.length ? 'error' : longRefs.length ? 'warn' : 'ok',
    badRefs.length
      ? `代码引用已失效（页面会显示空白或错行）：${badRefs.join('；')}`
      : longRefs.length
        ? `代码引用过长，读者会直接跳过：${longRefs.join('；')}`
        : refCount
          ? `${refCount} 处代码引用均指向真实文件与行号`
          : '还没有代码引用（贴真实实现时用 <!-- keel:code 路径 起-止 -->，别手粘贴）',
    badRefs)

  // ---------- 接口文档（仅探测） ----------
  const apiHints = []
  for (const file of sourceFiles) {
    const content = safeRead(file.abs).slice(0, 200000)
    if (/openapi_url|SwaggerModule|swagger-jsdoc|drf[-_]spectacular|swag init/i.test(content)) apiHints.push(file.rel)
    if (apiHints.length >= 3) break
  }
  push('API-01', apiHints.length ? 'ok' : 'info',
    apiHints.length ? `检测到接口文档自动生成入口：${apiHints.join('、')}` : '未检测到 OpenAPI/Swagger 生成入口（若项目无后端可忽略）')

  // ---------- 输出 ----------
  const stats = {
    registered: registry.length,
    actual: actualComponents.length,
    unregistered: unregistered.length,
    unused: unused.length,
    coverage: actualComponents.length
      ? Math.round(((actualComponents.length - unregistered.length) / actualComponents.length) * 100)
      : 100,
  }
  const errors = issues.filter((i) => i.level === 'error')
  const warns = issues.filter((i) => i.level === 'warn')

  if (args.json) {
    console.log(JSON.stringify({ root: posix(root), issues, stats, usage }, null, 2))
  } else {
    console.log(`\nkeel 体检 · ${posix(root)}\n`)
    const mark = { ok: '  ok  ', error: ' error', warn: ' warn ', info: ' info ' }
    for (const issue of issues) {
      console.log(`[${mark[issue.level]}] ${issue.id}  ${issue.message}`)
      for (const item of issue.items) console.log(`            · ${item}`)
    }
    console.log('')
    console.log(`组件：已登记 ${stats.registered} · 实际 ${stats.actual} · 未登记 ${stats.unregistered} · 未使用 ${stats.unused} · 覆盖率 ${stats.coverage}%`)
    console.log(`\n摘要：error ${errors.length} · warn ${warns.length}\n`)
  }

  const failed = errors.length > 0 || (args.failOnUnused && unused.length > 0)
  // --fail-on-unused 隐含 --strict：只写 --fail-on-unused 却拿到 exit 0 是最坏的静默失守
  process.exit((args.strict || args.failOnUnused) && failed ? 1 : 0)
}

const readCache = new Map()
function safeRead(abs) {
  if (readCache.has(abs)) return readCache.get(abs)
  let content = ''
  try {
    if (statSync(abs).size < 2 * 1024 * 1024) content = readFileSync(abs, 'utf8')
  } catch {
    content = ''
  }
  readCache.set(abs, content)
  return content
}

main()
