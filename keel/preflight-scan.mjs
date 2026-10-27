#!/usr/bin/env node
/**
 * preflight-scan —— 上线前静态漏洞扫描（零依赖，Node >= 18）。
 *
 * 定位：发布门禁，不是渗透测试。目的是把"提交进仓库的低级错误"在合并前挡住，
 * 而不是替代人工的越权/逻辑绕过高风险检查（那五项见 references/preflight.md）。
 *
 * 用法：
 *   node keel/preflight-scan.mjs                 人读报告
 *   node keel/preflight-scan.mjs --strict        error > 0 时退出码 1（CI / pre-release）
 *   node keel/preflight-scan.mjs --json          机器可读
 *   node keel/preflight-scan.mjs --include-dist  连构建产物一起扫（查前端打包泄露密钥）
 *   node keel/preflight-scan.mjs --root ../proj  指定项目根
 *
 * 豁免：项目根 .preflightignore
 *   SEC-09  src/lib/markdown.ts     # 渲染的是受信任的本地文档
 *   path:   tests/fixtures/         # 测试夹具含故意漏洞
 * 每条豁免必须写原因 —— 没原因的豁免等于关掉检测。
 *
 * 退出码：0 通过（或非 strict）/ 1 未通过 / 2 运行错误
 */
import { readFileSync, existsSync, readdirSync, statSync } from 'node:fs'
import { join, resolve, relative, extname, basename } from 'node:path'
import { fileURLToPath } from 'node:url'
import process from 'node:process'

const SELF = fileURLToPath(import.meta.url)

const HELP = `preflight-scan 上线前静态漏洞扫描
  --root <dir>       项目根（默认 cwd）
  --dirs <a,b,c>     指定扫描目录，覆盖 console.config.json 的 scanDirs
  --strict           error 级问题导致退出码 1
  --include-dist     同时扫描 dist/ build/ 产物
  --json             输出 JSON
  -h, --help         显示帮助`

const TEXT_EXTS = new Set([
  '.js', '.mjs', '.cjs', '.jsx', '.ts', '.tsx', '.vue', '.svelte', '.astro',
  '.py', '.go', '.java', '.kt', '.rs', '.php', '.rb', '.cs', '.swift',
  '.yml', '.yaml', '.json', '.toml', '.ini', '.conf', '.env', '.sh', '.bat', '.ps1',
  '.properties', '.sql', '.md',
])

const DEFAULT_EXCLUDE = new Set([
  'node_modules', 'dist', 'build', '.git', '.next', 'coverage', 'vendor',
  '__pycache__', '.venv', 'venv', 'target', '.idea', '.vscode', 'logs', '.ruff_cache',
])

/**
 * 规则表。
 *   level: error = 必阻断 / warn = 需人看一眼 / info = 提示
 *   exts:  限定文件后缀（不写 = 所有文本文件）
 *   unless: 行内出现这些词就跳过（降低误报，如从环境变量读取）
 *   perFile: 同一文件最多报几条（默认 3），避免刷屏
 */
const RULES = [
  {
    id: 'SEC-01',
    level: 'error',
    title: '私钥文件内容进入仓库',
    re: /-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----/,
    why: '私钥一旦进过 git 历史，删文件也没用，必须重置密钥。',
    fix: '换成密钥管理服务 / 环境变量，并从 git 历史中清除。',
  },
  {
    id: 'SEC-02',
    level: 'error',
    title: '疑似硬编码凭证',
    re: /(?:api[_-]?key|secret[_-]?key|access[_-]?key|auth[_-]?token|password|passwd|pwd)\s*[:=]\s*['"`]([A-Za-z0-9_\-./+]{12,})['"`]/i,
    unless: /process\.env|import\.meta\.env|os\.getenv|os\.environ|getenv\(|\$\{|\bxxx+\b|your[_-]?|placeholder|example|sample|dummy|fake|<[A-Z_]+>/i,
    why: '凭证进代码库 = 进 git 历史 = 永久泄露。',
    fix: '改为从环境变量或配置中心读取，并轮换这条已泄露的凭证。',
  },
  {
    id: 'SEC-03',
    level: 'error',
    title: '云/服务商密钥特征串',
    re: /\b(?:AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}|AIza[0-9A-Za-z\-_]{35}|xox[baprs]-[A-Za-z0-9-]{10,})\b/,
    why: '这些前缀有明确归属，扫到基本就是真泄露。',
    fix: '立即吊销并轮换。',
  },
  {
    id: 'SEC-04',
    level: 'error',
    title: '动态代码执行',
    re: /\b(?:eval\s*\(|new\s+Function\s*\(|setTimeout\s*\(\s*['"`])/,
    exts: ['.js', '.mjs', '.cjs', '.jsx', '.ts', '.tsx', '.vue', '.svelte'],
    why: '拼接的字符串一旦含用户输入，等于任意代码执行。',
    fix: '改用显式分支或 JSON.parse；确实需要时限定白名单。',
  },
  {
    id: 'SEC-04B',
    level: 'error',
    title: '动态代码执行（Python）',
    re: /\b(?:eval|exec)\s*\(/,
    exts: ['.py'],
    why: '拼接的字符串一旦含用户输入，等于任意代码执行。',
    fix: '改用 ast.literal_eval 或显式分支。',
  },
  {
    id: 'SEC-05',
    level: 'error',
    title: '命令注入风险',
    re: /\b(?:subprocess\.[a-z_]+\([^)]*shell\s*=\s*True|os\.system\s*\(|os\.popen\s*\(|Runtime\.getRuntime\(\)\.exec|child_process[^)]*\.exec\s*\()/,
    why: '拼接的命令串可被注入任意命令。',
    fix: '用数组参数 + shell=False；或对参数做严格白名单校验。',
  },
  {
    id: 'SEC-06',
    level: 'error',
    title: '不安全反序列化',
    re: /\b(?:pickle\.loads?\s*\(|cPickle\.loads?\s*\(|marshal\.loads\s*\()/,
    exts: ['.py'],
    why: '反序列化不可信数据 = 直接 RCE。',
    fix: '改用 json；必须用 pickle 时只对本地可信文件使用并加签名校验。',
  },
  {
    id: 'SEC-07',
    level: 'warn',
    title: 'yaml.load 未指定 SafeLoader',
    re: /yaml\.load\s*\((?![^)]*SafeLoader)[^)]*\)/,
    exts: ['.py'],
    why: 'PyYAML 的 load 默认可能构造任意对象。',
    fix: '改为 yaml.safe_load()。',
  },
  {
    id: 'SEC-08',
    level: 'error',
    title: '关闭证书/主机名校验',
    re: /(?:verify\s*=\s*False|rejectUnauthorized\s*:\s*false|InsecureSkipVerify\s*:\s*true|NODE_TLS_REJECT_UNAUTHORIZED\s*=\s*['"]?0)/,
    why: '关掉校验等于给中间人攻击开门。',
    fix: '挂载正确的 CA；自签环境把 CA 配进信任链，而不是关校验。',
  },
  {
    id: 'SEC-09',
    level: 'warn',
    title: 'XSS 注入点',
    re: /(?:\.innerHTML\s*=|v-html\s*=|dangerouslySetInnerHTML|\[innerHTML\]\s*=)/,
    why: '把外部内容当 HTML 插入。用在静态常量上无害，用在用户输入上就是 XSS。',
    fix: '先消毒（DOMPurify 等），或改用文本节点渲染。',
  },
  {
    id: 'SEC-10',
    level: 'error',
    title: '调试开关/断点残留',
    re: /(?:\bDEBUG\s*=\s*True|debug\s*=\s*True|\bbreakpoint\s*\(\s*\)|pdb\.set_trace\s*\(|import\s+pdb\b)/,
    why: 'DEBUG 模式会泄露堆栈与内部变量；断点会让请求挂住。',
    fix: '删掉，或改为由环境变量控制且默认关闭。',
  },
  {
    id: 'SEC-11',
    level: 'error',
    title: 'CORS 通配',
    re: /(?:allow_origins\s*=\s*\[\s*['"]\*['"]|origin\s*:\s*['"]\*['"]|Access-Control-Allow-Origin['"]?\s*[,:]\s*['"]\*)/,
    why: '通配 + Cookie 凭证 = 任意站点可代用户发请求。',
    fix: '改成显式域名白名单；确需通配时禁止携带 credentials。',
  },
  {
    id: 'SEC-12',
    level: 'warn',
    title: 'SQL 语句疑似字符串拼接',
    re: /\b(?:SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM)\b[^\n]{0,200}?(?:\+\s*[A-Za-z_$]|\$\{|\bformat\s*\(|%\s*\()/i,
    why: '拼接 SQL 是最经典的注入入口。',
    fix: '改用参数化查询（占位符 + 绑定参数）。',
  },
  {
    id: 'SEC-13',
    level: 'error',
    title: '默认口令 / 弱口令',
    re: /(?:password\s*[:=]\s*['"](?:admin|root|123456|12345678|password|passwd|changeme|test)['"]|['"]admin['"]\s*:\s*['"]admin['"])/i,
    why: '默认口令是渗透测试第一发子弹。',
    fix: '首次登录强制改密；种子账号用随机密码并打印到部署日志。',
  },
  {
    id: 'SEC-14',
    level: 'warn',
    title: '认证被绕过 / 注明 no-auth',
    re: /(?:@(?:login_required|auth)\b.*\b(?:skip|disable)|#\s*(?:no[_-]?auth|skip[_-]?auth|FIXME.*auth)|TODO.*(?:auth|permission|权限))/i,
    why: '临时跳过鉴权的代码最容易被忘在线上。',
    fix: '确认是否还需要；需要就补，不需要就删注释并留 issue。',
  },
  {
    id: 'SEC-15',
    level: 'warn',
    title: '路径拼接使用了外部输入',
    re: /(?:open|readFile|readFileSync|send_file|sendFile|FileInputStream)\s*\(\s*(?:os\.path\.join\(\s*[^)]*(?:request|req\.|params|query|body)|path\.join\(\s*(?:req\.|request|params|query))/,
    why: '`../` 可穿出预期目录，读到任意文件。',
    fix: '规范化后校验前缀是否在允许目录内。',
  },
  {
    id: 'SEC-16',
    level: 'warn',
    title: '外部 URL 直接请求（SSRF 风险）',
    re: /(?:requests\.(?:get|post)\s*\(\s*(?:url|target|webhook|endpoint)\b|fetch\s*\(\s*(?:url|target|webhook)\b|axios\.(?:get|post)\s*\(\s*(?:url|target))/,
    why: '用户可控的 URL 能打到内网（127.0.0.1、169.254.169.254 元数据）。',
    fix: '校验协议与域名白名单，禁止解析到内网网段。',
  },
  {
    id: 'SEC-17',
    level: 'info',
    title: '安全响应头中间件',
    re: /(?:X-Content-Type-Options|X-Frame-Options|Strict-Transport-Security|Content-Security-Policy)/,
    why: '有这个说明项目配过安全头，用于确认是否存在。',
    fix: '—',
  },
]

function parseArgs(argv) {
  const out = { root: process.cwd(), strict: false, json: false, includeDist: false, dirs: null }
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i]
    if (arg === '--root') out.root = argv[i += 1]
    else if (arg === '--dirs') out.dirs = String(argv[i += 1] || '').split(',').map((s) => s.trim()).filter(Boolean)
    else if (arg === '--strict') out.strict = true
    else if (arg === '--json') out.json = true
    else if (arg === '--include-dist') out.includeDist = true
    else if (arg === '-h' || arg === '--help') {
      console.log(HELP)
      process.exit(0)
    }
  }
  out.root = resolve(out.root)
  return out
}

const posix = (p) => p.split('\\').join('/')

function readJsonSafe(path, fallback) {
  try {
    // 同 console-check：容忍 Windows 编辑器写出的 BOM
    return JSON.parse(readFileSync(path, 'utf8').replace(/^\uFEFF/, ''))
  } catch {
    return fallback
  }
}

/** 解析 .preflightignore：`SEC-09 path/sub  # 原因` 或 `path: dir/` */
function loadIgnore(root) {
  const path = join(root, '.preflightignore')
  const rules = []
  if (!existsSync(path)) return { rules, path }
  for (const raw of readFileSync(path, 'utf8').split(/\r?\n/)) {
    const line = raw.split('#')[0].trim()
    if (!line) continue
    if (/^path\s*:/i.test(line)) {
      rules.push({ ruleId: '*', target: line.replace(/^path\s*:/i, '').trim() })
    } else {
      const [ruleId, ...rest] = line.split(/\s+/)
      if (rest.length) rules.push({ ruleId, target: rest.join(' ') })
    }
  }
  return { rules, path }
}

function isIgnored(ignore, ruleId, rel) {
  return ignore.rules.some(
    (entry) =>
      rel.includes(entry.target) && (entry.ruleId === '*' || entry.ruleId === ruleId),
  )
}

function collectFiles(root, dirs, exclude, acc = []) {
  for (const dir of dirs) {
    const abs = resolve(root, dir)
    if (!existsSync(abs)) continue
    walk(abs, root, exclude, acc)
  }
  return acc
}

function walk(absDir, root, exclude, acc) {
  let entries
  try {
    entries = readdirSync(absDir, { withFileTypes: true })
  } catch {
    return
  }
  for (const entry of entries) {
    const abs = join(absDir, entry.name)
    if (entry.isDirectory()) {
      if (exclude.has(entry.name)) continue
      walk(abs, root, exclude, acc)
    } else if (entry.isFile()) {
      const ext = extname(entry.name).toLowerCase()
      if (!TEXT_EXTS.has(ext) && !entry.name.startsWith('.env')) continue
      let size = 0
      try {
        size = statSync(abs).size
      } catch {
        continue
      }
      if (size > 2 * 1024 * 1024) continue
      acc.push({ abs, rel: posix(relative(root, abs)), name: entry.name })
    }
  }
}

function fileChecks(root, files) {
  const findings = []
  const add = (id, level, title, file, line, text, why, fix) =>
    findings.push({ id, level, title, file, line, text, why, fix })

  // SEC-20：.env 存在于仓库但未被忽略
  const gitignorePath = join(root, '.gitignore')
  const gitignore = existsSync(gitignorePath) ? readFileSync(gitignorePath, 'utf8') : ''
  const ignoresEnv = /(^|\n)\s*\.env(\s|\n|$)/.test(gitignore) || /\.env\*?/.test(gitignore)
  for (const name of ['.env', '.env.local', '.env.production']) {
    if (!existsSync(join(root, name))) continue
    if (!ignoresEnv) {
      add('SEC-20', 'error', '.env 存在但未被 .gitignore 忽略', name, 0, '',
        '.env 一旦被提交，其中的数据库口令、JWT 密钥全部进入 git 历史。',
        '把 .env 加入 .gitignore，提交 .env.example 代替，并轮换已泄露的值。')
    }
  }
  if (!existsSync(gitignorePath)) {
    add('SEC-21', 'warn', '缺少 .gitignore', '.gitignore', 0, '',
      '没有忽略规则时，node_modules / .env / 构建产物都可能被提交。', '补一份最小 .gitignore。')
  }

  // SEC-22：锁文件
  const lockfiles = ['package-lock.json', 'pnpm-lock.yaml', 'yarn.lock', 'poetry.lock', 'go.sum', 'Cargo.lock']
  const hasPkg = existsSync(join(root, 'package.json'))
  if (hasPkg && !lockfiles.some((f) => existsSync(join(root, f)))) {
    add('SEC-22', 'warn', '有 package.json 但缺少锁文件', 'package.json', 0, '',
      '依赖版本不可复现，今天审计通过明天可能就带漏洞。', '提交锁文件，且禁止通配版本号。')
  }

  // SEC-23：构建产物里的密钥（仅 --include-dist 时由调用方传入 dist 文件）
  return { findings, gitignorePath }
}

function main() {
  const args = parseArgs(process.argv.slice(2))
  const root = args.root

  let exitCode = 0
  try {
    const consoleDir = join(root, 'keel')
    const config = readJsonSafe(join(consoleDir, 'console.config.json'), {})

    const scanDirs = args.dirs && args.dirs.length
      ? args.dirs
      : Array.isArray(config.scanDirs) && config.scanDirs.length
        ? config.scanDirs
        : [config.srcDir || 'src', 'backend', 'frontend', 'app', 'server', 'api'].filter((d) =>
            existsSync(join(root, d)),
          )

    const exclude = new Set([...(config.excludeDirs || []), ...DEFAULT_EXCLUDE])
    if (args.includeDist) {
      exclude.delete('dist')
      exclude.delete('build')
      scanDirs.push('dist', 'build')
    } else {
      exclude.add('dist')
      exclude.add('build')
    }

    const files = collectFiles(root, scanDirs, exclude).filter(
      (file) => resolve(file.abs) !== resolve(SELF) && !file.rel.startsWith('keel/'),
    )

    // 根目录的 .env 系列不在 scanDirs 里，但它们是泄露重灾区，单独补进来扫
    for (const name of ['.env', '.env.local', '.env.production', '.env.development']) {
      const abs = join(root, name)
      if (existsSync(abs) && !files.some((f) => f.rel === name)) {
        files.push({ abs, rel: name, name })
      }
    }

    const ignore = loadIgnore(root)
    const findings = []

    // 扫 0 个文件时的"通过"是假绿灯：目录配错、或者项目根本不是这个结构。
    // 这种绿灯比报错更危险，所以直接判 error。
    if (!files.length) {
      findings.push({
        id: 'SEC-00',
        level: 'error',
        title: '没有扫描到任何文件',
        file: '.',
        line: 0,
        text: `扫描目录：${scanDirs.join(' / ') || '(空)'}`,
        why: '目录配错时候脚本会"通过"，这个绿灯是假的，等于关掉了整道门禁。',
        fix: '用 --dirs 指定真实源码目录，或检查 keel/console.config.json 的 scanDirs。',
      })
    }

    for (const file of files) {
      const ext = extname(file.name).toLowerCase()
      let content
      try {
        content = readFileSync(file.abs, 'utf8')
      } catch {
        continue
      }
      const lines = content.split(/\r?\n/)

      for (const rule of RULES) {
        if (rule.exts && !rule.exts.includes(ext) && !file.name.startsWith('.env')) continue
        if (isIgnored(ignore, rule.id, file.rel)) continue
        let hits = 0
        for (let i = 0; i < lines.length; i += 1) {
          if (hits >= 3) break
          const line = lines[i]
          if (line.length > 2000) continue
          if (rule.unless && rule.unless.test(line)) continue
          if (!rule.re.test(line)) {
            // 重置 lastIndex 以兼容带 g 标志的情况（本表未使用 g，保险起见）
            rule.re.lastIndex = 0
            continue
          }
          rule.re.lastIndex = 0
          hits += 1
          findings.push({
            id: rule.id,
            level: rule.level,
            title: rule.title,
            file: file.rel,
            line: i + 1,
            text: line.trim().slice(0, 160),
            why: rule.why,
            fix: rule.fix,
          })
        }
      }
    }

    const fileLevel = fileChecks(root, files)
    findings.push(...fileLevel.findings)

    // 构建产物专项：只有 --include-dist 才有意义
    if (args.includeDist) {
      const distFiles = collectFiles(root, ['dist', 'build'], new Set()).filter((f) =>
        /\.(?:js|css|html|map)$/.test(f.name),
      )
      let leaked = 0
      for (const file of distFiles) {
        let content = ''
        try {
          content = readFileSync(file.abs, 'utf8')
        } catch {
          continue
        }
        for (const rule of RULES.slice(0, 3)) {
          if (rule.re.test(content)) {
            rule.re.lastIndex = 0
            leaked += 1
            findings.push({
              id: 'SEC-30',
              level: 'error',
              title: '构建产物中出现密钥特征',
              file: file.rel,
              line: 0,
              text: `${rule.id} 命中`,
              why: '前端产物是公开的。打进 bundle 的"密钥"等于贴在首页上。',
              fix: '改由服务端代理调用，前端不持有任何长期凭证。',
            })
            break
          }
        }
        if (leaked >= 10) break
      }
      if (distFiles.length && !leaked) {
        findings.push({
          id: 'SEC-30',
          level: 'info',
          title: '构建产物已扫描，未发现密钥特征',
          file: 'dist/', line: 0, text: '', why: '', fix: '',
        })
      }
    }

    // 汇总
    const order = { error: 0, warn: 1, info: 2 }
    findings.sort((a, b) => order[a.level] - order[b.level] || a.file.localeCompare(b.file) || a.line - b.line)
    const counts = {
      error: findings.filter((f) => f.level === 'error').length,
      warn: findings.filter((f) => f.level === 'warn').length,
      info: findings.filter((f) => f.level === 'info').length,
    }
    const byRule = {}
    for (const f of findings) byRule[f.id] = (byRule[f.id] || 0) + 1

    if (args.json) {
      console.log(JSON.stringify({ root: posix(root), scannedFiles: files.length, counts, byRule, findings, ignored: ignore.rules }, null, 2))
    } else {
      console.log(`\npreflight-scan · ${posix(root)}`)
      console.log(`扫描 ${files.length} 个文件${args.includeDist ? '（含构建产物）' : ''}${ignore.rules.length ? ` · 豁免 ${ignore.rules.length} 条` : ''}\n`)

      if (!findings.length) {
        console.log('  未发现问题。\n')
      }

      let lastLevel = ''
      for (const f of findings) {
        if (f.level !== lastLevel) {
          const label = { error: '必须修复', warn: '需人工确认', info: '提示' }[f.level]
          console.log(`—— ${label} ——`)
          lastLevel = f.level
        }
        const at = f.line ? `${f.file}:${f.line}` : f.file
        console.log(`[${f.id}] ${f.title}`)
        console.log(`       位置  ${at}`)
        if (f.text) console.log(`       代码  ${f.text}`)
        if (f.why) console.log(`       风险  ${f.why}`)
        if (f.fix) console.log(`       改法  ${f.fix}`)
        console.log('')
      }

      console.log(`摘要：error ${counts.error} · warn ${counts.warn} · info ${counts.info}`)
      if (counts.error) console.log('结论：未通过。error 必须清零才允许切线上模式。')
      else if (counts.warn) console.log('结论：可上线，但 warn 需逐条确认并记录。')
      else console.log('结论：通过。')
      console.log('\n提醒：本脚本查不出越权(IDOR)、业务逻辑绕过、权限只做在前端。')
      console.log('那五项见 references/preflight.md 的 L5，必须人工过。\n')
    }

    exitCode = args.strict && counts.error > 0 ? 1 : 0
  } catch (error) {
    console.error('preflight-scan 运行失败：', error?.message || error)
    exitCode = 2
  }

  process.exit(exitCode)
}

main()
