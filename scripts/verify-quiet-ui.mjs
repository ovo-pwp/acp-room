/* 零依赖真实浏览器验证：acp-room 页面「简化」是否生效
 * 用法：node scripts/verify-quiet-ui.mjs
 */
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const APP = 'http://127.0.0.1:8770'
const CDP_PORT = 9333
const CHROME = process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe'
const OUT_DIR = path.dirname(fileURLToPath(import.meta.url))
const SHOT = path.join(OUT_DIR, 'quiet-ui.png')

const results = []
function check(name, pass, detail) {
  results.push({ name, pass, detail })
  log(`${pass ? 'OK  ' : 'FAIL'}  ${name}${detail ? '   [' + detail + ']' : ''}`)
}
function log(s) {
  process.stdout.write(s + '\n')
  try { fs.appendFileSync(path.join(OUT_DIR, 'quiet-ui.log'), s + '\n') } catch {}
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

if (!fs.existsSync(CHROME)) { log('未找到 Chrome：' + CHROME + '（可用 CHROME_PATH 指定）'); process.exit(0) }
try {
  const r = await fetch(APP + '/')
  if (!r.ok) throw new Error('HTTP ' + r.status)
} catch (e) {
  log('acp-room 页面不可达：' + APP + ' — ' + e.message + '（请先 python server.py）')
  process.exit(0)
}

const userDataDir = path.join(os.tmpdir(), 'quiet-ui-' + Date.now())
const chrome = spawn(CHROME, [
  '--headless=new',
  `--remote-debugging-port=${CDP_PORT}`,
  `--user-data-dir=${userDataDir}`,
  '--no-first-run', '--no-default-browser-check',
  '--disable-gpu', '--disable-extensions',
  '--window-size=1536,900',
  'about:blank',
], { stdio: 'ignore', detached: false })

class CDP {
  constructor(url) { this.url = url; this.id = 0; this.pending = new Map(); this.handlers = new Map() }
  connect() {
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(this.url)
      ws.onopen = () => { this.ws = ws; resolve() }
      ws.onerror = () => reject(new Error('ws error'))
      ws.onmessage = (ev) => {
        let msg
        try { msg = JSON.parse(ev.data) } catch { return }
        if (msg.id && this.pending.has(msg.id)) {
          const { resolve: res, reject: rej } = this.pending.get(msg.id)
          this.pending.delete(msg.id)
          msg.error ? rej(new Error(JSON.stringify(msg.error))) : res(msg.result)
        } else if (msg.method) {
          for (const h of this.handlers.get(msg.method) || []) { try { h(msg.params) } catch (e) { log('handler err ' + e.message) } }
        }
      }
    })
  }
  send(method, params = {}, timeoutMs = 15000) {
    const id = ++this.id
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error('CDP 超时 ' + timeoutMs + 'ms: ' + method)) }, timeoutMs)
      this.pending.set(id, { resolve: (v) => { clearTimeout(timer); resolve(v) }, reject: (e) => { clearTimeout(timer); reject(e) } })
      this.ws.send(JSON.stringify({ id, method, params }))
    })
  }
  on(method, h) { if (!this.handlers.has(method)) this.handlers.set(method, []); this.handlers.get(method).push(h) }
  close() { try { this.ws.close() } catch {} }
}

let cdp = null
const consoleErrors = []
try {
  let wsUrl = null
  for (let i = 0; i < 60; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`)).json()
      const page = list.find((t) => t.type === 'page' && t.webSocketDebuggerUrl)
      if (page) { wsUrl = page.webSocketDebuggerUrl; break }
    } catch {}
    await sleep(250)
  }
  if (!wsUrl) throw new Error('拿不到 CDP page target')
  cdp = new CDP(wsUrl)
  await cdp.connect()
  await cdp.send('Page.enable')
  await cdp.send('Runtime.enable')
  await cdp.send('Log.enable')
  cdp.on('Log.entryAdded', ({ entry }) => { if (entry.level === 'error') consoleErrors.push(entry.text) })
  cdp.on('Runtime.exceptionThrown', ({ exceptionDetails }) => {
    consoleErrors.push(exceptionDetails?.exception?.description || exceptionDetails?.text || 'exception')
  })

  const ev = async (expression) => {
    const r = await cdp.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
    if (r.exceptionDetails) throw new Error('eval 失败: ' + (r.exceptionDetails.exception?.description || expression))
    return r.result.value
  }
  const goto = async (url, settleMs = 3000) => {
    await cdp.send('Page.navigate', { url })
    for (let i = 0; i < 60; i++) {
      if ((await ev('document.readyState').catch(() => null)) === 'complete') break
      await sleep(100)
    }
    await sleep(settleMs)
  }

  // 1) 首屏（干净 profile → localStorage 空 → 触发「首次收成最简」）
  await goto(APP + '/', 3500)

  check('P2 重复控件行已隐藏',
    await ev(`(() => { const p = document.getElementById('p2-panel'); if (!p) return false
      const row = p.querySelector('.p2-row'); return !!row && getComputedStyle(row).display === 'none' })()`) === true)

  check('P2 进度条（.p2-track）仍保留',
    await ev(`!!document.querySelector('#p2-panel .p2-track')`) === true)

  const bodyCls = await ev(`document.body.className || '(空)'`)
  check('右侧栏 P7/P13 默认收起',
    await ev(`!document.body.classList.contains('p7-rail-open') && !document.body.classList.contains('p13-rail-open')`) === true,
    'body.class=' + bodyCls)

  check('添加 agent 表单默认收起',
    await ev(`(() => { const b = document.getElementById('p14-body'); return !!b && getComputedStyle(b).display === 'none' })()`) === true)

  // 收起 ≠ 锁死：点标题仍应展开表单
  await ev(`document.getElementById('p14-toggle').click(); true`)
  await sleep(300)
  check('点「添加 agent」仍可展开',
    await ev(`getComputedStyle(document.getElementById('p14-body')).display !== 'none'`) === true)
  await ev(`document.getElementById('p14-toggle').click(); true`)
  await sleep(300)

  const perfWrap = await ev(`(() => { const el = document.getElementById('p16-perf'); if (!el) return 'p16 不在 DOM'
    const d = el.closest('details.shell-sec'); if (!d) return '没被包裹'
    const s = d.querySelector('summary'); return (s ? s.textContent : '(无 summary)') + '|open=' + d.open })()`)
  check('算力对比折进 <details> 且默认收起', perfWrap === '算力对比|open=false', perfWrap)

  check('侧栏三块折叠（模型与配置 / 技能 / 算力对比）',
    (await ev(`JSON.stringify([...document.querySelectorAll('aside details.shell-sec summary')].map(s => s.textContent))`))
      .includes('算力对比') === true)

  check('首次收起偏好已落盘 shell-quiet-v1',
    (await ev(`localStorage.getItem('shell-quiet-v1')`)) === '1')

  // 2) 模式切换：讨论模式隐藏「轮数 / 自动接话」，说话模式恢复
  const labelState = () => ev(`(() => {
    const one = (id) => { const el = document.getElementById(id)
      const lb = el && el.closest && el.closest('label')
      return !lb ? 'no-label' : getComputedStyle(lb).display === 'none' ? 'hidden' : 'shown' }
    return one('rounds') + '/' + one('auto') })()`)

  check('说话模式：轮数与自动接话可见', (await labelState()) === 'shown/shown', await labelState())

  await ev(`document.getElementById('p12-disc').click(); true`)
  await sleep(700)
  check('讨论模式：轮数与自动接话隐藏', (await labelState()) === 'hidden/hidden', await labelState())

  await ev(`document.getElementById('p12-chat').click(); true`)
  await sleep(700)
  check('切回说话模式：控件恢复', (await labelState()) === 'shown/shown', await labelState())

  // 3) 点右栏 tab 仍能展开（简化没有把功能锁死）
  await ev(`document.getElementById('p13-tab') && document.getElementById('p13-tab').click(); true`)
  await sleep(600)
  const railOpen = await ev(`document.body.classList.contains('p13-rail-open')`)
  const hasTab = await ev(`!!document.getElementById('p13-tab')`)
  if (hasTab) check('健康面板 tab 仍可展开', railOpen === true, 'p13-rail-open=' + railOpen)
  else log('SKIP 健康面板 tab 不存在（服务端可能没起 /api/health）')
  await ev(`(function(){ const c = document.querySelector('#p13-close'); if (c) c.click() })(); true`)
  await sleep(400)

  // 4) 布局宽度：右栏收起时，正文应拿满「视口 - 侧栏」（不断绝对像素，视口会变）
  const widths = await ev(`(() => {
    const main = document.querySelector('main'), aside = document.querySelector('aside')
    const b = (el) => el ? Math.round(el.getBoundingClientRect().width) : 0
    return JSON.stringify({ main: b(main), aside: b(aside), inner: innerWidth,
      rails: document.body.classList.contains('p7-rail-open') || document.body.classList.contains('p13-rail-open') }) })()`)
  log('宽度 ' + widths)
  const w = JSON.parse(widths)
  if (w.rails) check('宽度对照：右栏已收起', false, '测量时右栏是展开的，重新加载后重试')
  else check('右栏收起时正文拿满剩余宽度', w.main >= w.inner - w.aside - 40,
    'main=' + w.main + ' 期望>=' + (w.inner - w.aside - 40) + ' (inner=' + w.inner + ' aside=' + w.aside + ')')

  // 5) 刷新一次：不应把用户手动展开的东西再打回去（quietFirstRun 只跑第一次）
  await ev(`(function(){ const t = document.getElementById('p7-aud-tab'); if (t) t.click() })(); true`)
  await sleep(500)
  const beforeReload = await ev(`document.body.classList.contains('p7-rail-open')`)
  await goto(APP + '/', 3000)
  const afterReload = await ev(`document.body.classList.contains('p7-rail-open')`)
  if (beforeReload) check('刷新后保留手动展开（不再强制收起）', afterReload === true, 'before=' + beforeReload + ' after=' + afterReload)
  else log('SKIP 刷新前后对照（P7 tab 不可点）')

  // 截图：先回到"最简形态"（把刚才为验证可展开性点开的右栏收掉）
  await ev(`(function(){ const c = document.querySelector('#p7-audit .p7-aud-close'); if (c) c.click() })(); true`)
  await sleep(600)
  const { data } = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false })
  fs.writeFileSync(SHOT, Buffer.from(data, 'base64'))
  log('截图：' + SHOT)

  // 控制台错误（排除预期的网络噪音）
  const noise = /Failed to load resource|favicon|DevTools/i
  const real = consoleErrors.filter((e) => !noise.test(e))
  check('无未捕获的 JS 异常', real.length === 0, real.slice(0, 3).join(' | ') || '0 条')
} catch (e) {
  check('脚本执行', false, e.message)
} finally {
  const failed = results.filter((r) => !r.pass)
  log('\n== 结果：' + (results.length - failed.length) + '/' + results.length + ' 通过 ==')
  for (const f of failed) log('  ✖ ' + f.name + (f.detail ? '  [' + f.detail + ']' : ''))
  cdp?.close()
  try { chrome.kill() } catch {}
  await sleep(300)
  try { fs.rmSync(userDataDir, { recursive: true, force: true }) } catch (e) { log('清理失败：' + e.message) }
  process.exit(failed.length ? 1 : 0)
}
